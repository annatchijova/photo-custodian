"""Build an offline, review-only HTML snapshot of a catalog."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
from argparse import Namespace
from html import escape

from .advisor import AdviceUnavailable
from .catalog import Catalog
from .exact_duplicates import _catalog_fingerprint, _valid_hash_predicate, find_exact_duplicates
from .photo_categories import CATEGORIES, suggest_category
from .visual_review import find_versions, image_rows, read_photo

MAX_PREVIEWS_PER_GROUP = 8
MAX_CATEGORY_REQUESTS = 10

_STYLE = """
:root{color-scheme:light dark;font:1rem/1.6 system-ui,sans-serif}
body{max-width:75rem;margin:auto;padding:1.25rem}
nav{display:flex;gap:1rem;flex-wrap:wrap}
section{margin-block:2rem}
details{border:1px solid GrayText;border-radius:.5rem;padding:.6rem;margin-block:.7rem}
summary{cursor:pointer;padding:.3rem;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,15rem),1fr));gap:.75rem}
article{border:1px solid GrayText;padding:.7rem;overflow-wrap:anywhere}
img{width:100%;height:12rem;object-fit:contain}
button,select{font:inherit;min-height:2.5rem;max-width:100%}
label{display:block;margin-block:.5rem}
:focus-visible{outline:3px solid Highlight;outline-offset:3px}
"""
_SCRIPT = """
document.querySelector('#export').addEventListener('click', () => {
  const choices = [...document.querySelectorAll('select[data-group]')]
    .filter(select => select.value)
    .map(select => ({
      group: select.dataset.group,
      keep_file_id: select.value,
      path: select.selectedOptions[0].textContent
    }));
  if (!choices.length) {
    document.querySelector('#export-status').textContent = 'Choose a file in at least one group.';
    return;
  }
  const data = {schema_version: 1, purpose: 'review-notes', choices};
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'photo-custodian-review.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  document.querySelector('#export-status').textContent = 'Review notes exported.';
});
document.querySelector('#import').addEventListener('change', async event => {
  const status = document.querySelector('#export-status');
  const file = event.currentTarget.files[0];
  event.currentTarget.value = '';
  if (!file) return;
  try {
    if (file.size > 1000000) throw new Error();
    const notes = JSON.parse(await file.text());
    if (notes.schema_version !== 1 || notes.purpose !== 'review-notes' ||
        !Array.isArray(notes.choices)) throw new Error();
    const groups = new Map([...document.querySelectorAll('select[data-group]')]
      .map(select => [select.dataset.group, select]));
    const restored = new Map();
    for (const choice of notes.choices) {
      const select = groups.get(choice.group);
      if (!select || restored.has(select) || ![...select.options].some(option =>
          option.value && option.value === choice.keep_file_id &&
          option.textContent === choice.path)) throw new Error();
      restored.set(select, choice.keep_file_id);
    }
    for (const [select, value] of restored) select.value = value;
    status.textContent = `Restored ${restored.size} review choices.`;
  } catch {
    status.textContent = 'Could not restore choices. Use notes from this report page.';
  }
});
"""


def _image_errors() -> tuple[type[Exception], ...]:
    from PIL import Image

    return (OSError, ValueError, SyntaxError, Image.DecompressionBombError)


def _preview(row, *, photo_id: int) -> str:
    if row is None:
        return ""
    try:
        uri = read_photo(row, preview=True)
    except _image_errors():
        return "<p>Preview unavailable.</p>"
    return f'<img loading="lazy" alt="Preview of photo {photo_id}" src="{uri}">'


def _group(group_id: str, title: str, members, rows, *, omitted: int = 0) -> str:
    cards = []
    options = ['<option value="">Leave undecided</option>']
    for index, (file_id, path, info) in enumerate(members):
        safe_path = escape(path)
        preview = _preview(rows.get(file_id), photo_id=file_id) if index < MAX_PREVIEWS_PER_GROUP else ""
        cards.append(f"<article>{preview}<p>{safe_path}</p><p>ID {file_id} · {info}</p></article>")
        options.append(f'<option value="{file_id}">{safe_path}</option>')
    coverage = f"<p>{omitted} additional paths are outside this report.</p>" if omitted else ""
    return (
        f"<details><summary>{escape(title)}</summary>{coverage}"
        f'<div class="grid">{"".join(cards)}</div>'
        f'<label>File to keep for this group <select data-group="{group_id}">'
        f'{"".join(options)}</select></label></details>'
    )


def _categories(connection: sqlite3.Connection, rows, selected: list[int], model: str) -> tuple[list[str], bool]:
    if not selected:
        return [], False
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return ["<li>OpenAI categorization unavailable. Set OPENAI_API_KEY.</li>"], True
    results = []
    for file_id in selected:
        try:
            preview = read_photo(rows[file_id], preview=True)
            category = suggest_category(preview, api_key=key, model=model)
        except (AdviceUnavailable, *_image_errors()) as error:
            # API errors are sanitized by the transport. Photo errors can include paths.
            reason = str(error) if isinstance(error, AdviceUnavailable) else "Preview unavailable."
            results.append(f"<li>Photo {file_id} · {escape(reason)}. Remaining requests skipped.</li>")
            return results, True
        connection.execute("""
            INSERT INTO photo_categories(file_id, fingerprint, model, category)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET fingerprint = excluded.fingerprint,
                model = excluded.model, category = excluded.category
        """, (file_id, json.dumps(_catalog_fingerprint(rows[file_id])), model, category))
        connection.commit()
        results.append(f"<li>Photo {file_id} · {escape(category)}</li>")
    return results, False


def write_report(catalog: Catalog, args: Namespace) -> int:
    if not 1 <= args.limit <= 100 or not 0 <= args.offset <= 2**63 - 1:
        raise ValueError("Use --limit 1–100 and a non-negative --offset.")
    if args.comparison_budget < 1:
        raise ValueError("--comparison-budget must be positive.")
    selected = list(dict.fromkeys(args.categorize or []))
    if len(selected) > MAX_CATEGORY_REQUESTS:
        raise ValueError(f"Select at most {MAX_CATEGORY_REQUESTS} photos per report.")
    output = args.output.expanduser()
    if output.suffix.lower() != ".html" or output.exists() or output.is_symlink():
        raise ValueError("--output must be a new .html file.")
    try:
        import PIL.Image
    except ImportError:
        raise RuntimeError("Install the review extra with uv sync --locked --extra review.") from None

    rows = image_rows(catalog.connection)
    if any(file_id not in rows for file_id in selected):
        raise ValueError("--categorize requires active photo IDs from this catalog.")
    print("Checking exact duplicates…", file=sys.stderr, flush=True)
    exact = find_exact_duplicates(catalog, group_limit=args.limit, group_offset=args.offset)
    aliases = {
        row[0]: row[1]
        for row in catalog.connection.execute(f"""
            SELECT file_id, representative FROM (
                SELECT f.id AS file_id, MIN(f.id) OVER (
                    PARTITION BY h.digest, f.size_bytes
                ) AS representative
                FROM file_hashes h JOIN files f ON f.id = h.file_id
                WHERE {_valid_hash_predicate()} AND f.media_kind = 'image'
            ) WHERE file_id != representative
        """)
    }
    print("Comparing photo versions…", file=sys.stderr, flush=True)
    versions, analyzed, skipped, capped = find_versions(
        catalog.connection, rows, aliases, comparison_budget=args.comparison_budget
    )
    end = args.offset + args.limit
    exact_cards = [
        _group(f"exact-{group.sha256}", f"{group.file_count} identical paths · "
               f"{group.reclaimable_bytes:,} potentially recoverable bytes",
               [(item.file_id, item.path, f"{group.size_bytes:,} bytes") for item in group.files],
               rows, omitted=group.files_truncated)
        for group in exact.groups
    ]
    visual_cards = [
        _group(f"visual-{group[0].file_id}", f"{len(group)} visually similar photos",
               [(photo.file_id, photo.path, f"{photo.width} × {photo.height} · {photo.size:,} bytes")
                for photo in sorted(group, key=lambda photo: (-photo.width * photo.height, photo.file_id))],
               rows)
        for group in versions[args.offset:end]
    ]
    catalog.flush()
    categories, category_failed = _categories(catalog.connection, rows, selected, args.model)
    suggestions = {category: [] for category in CATEGORIES}
    saved = catalog.connection.execute("""
        SELECT f.*, c.fingerprint, c.category, c.model
        FROM photo_categories c JOIN files f ON f.id = c.file_id
        WHERE f.status = 'active' AND f.media_kind = 'image'
        ORDER BY c.category, f.id
    """)
    for row in saved:
        if row["category"] in suggestions and row["fingerprint"] == json.dumps(_catalog_fingerprint(row)):
            suggestions[row["category"]].append(
                f"<li>ID {row['id']} · {escape(row['relative_path'])} · {escape(row['model'])}</li>"
            )
    category_groups = "".join(
        f"<details><summary>{escape(category)} ({len(items)})</summary><ul>"
        + "".join(items[args.offset:end]) + "</ul></details>"
        for category, items in suggestions.items() if items[args.offset:end]
    )
    latest_scans = catalog.connection.execute("""
        SELECT COUNT(*) FROM scan_runs AS run
        WHERE run.id = (SELECT MAX(id) FROM scan_runs WHERE source_id = run.source_id)
          AND run.status != 'complete'
    """).fetchone()[0]
    warning = bool(
        skipped or capped or latest_scans or exact.status != "complete" or category_failed
    )
    inventory_items = [
        f"<li>ID {row['file_id']} · {escape(row['relative_path'])}</li>"
        for row in list(rows.values())[args.offset:end]
    ]
    status = "Review coverage warning" if warning else "Review snapshot"
    script_hash = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
    html = f"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'sha256-{script_hash}'; base-uri 'none'; form-action 'none'">
<title>Photo Custodian · Review</title><style>{_STYLE}</style>
<main>
<h1>Review your media library</h1>
<p><strong>{status}</strong> · Start with one group. Compare the files and record the one you want to keep.
Choices are notes for later verification. Download them before closing this page.</p>
<nav aria-label="Review sections"><a href="#exact">Exact copies</a>
<a href="#versions">Photo versions</a><a href="#categories">Categories</a></nav>
<p>Save choices before you leave. A choice applies to its group only.</p>
<button id="export" type="button">Download review choices</button>
<label>Restore choices from this page <input id="import" type="file" accept=".json"></label>
<p id="export-status" role="status"></p>
<section id="exact"><h2>Exact copies</h2>
<p>SHA-256 inventory snapshot. Showing {len(exact_cards)} of {exact.total_group_count} groups,
after skipping {args.offset}. Refresh the inventory and verify current files before any cleanup.</p>
<p>Hash errors: {exact.error_count}. Changed candidates: {exact.stale_files}.
Sources with incomplete last scan: {latest_scans}.</p>
{''.join(exact_cards) or '<p>No exact groups on this page.</p>'}
</section>
<section id="versions"><h2>Photo versions to compare</h2>
<p>Similar appearance can mean a resize or re-encode, or a different photo.
Compare details before choosing. Dimensions are shown for comparison.</p>
<p>Compared {analyzed:,} of {len(rows):,} catalogued photo paths.
Skipped {skipped:,} unreadable, unsupported, changed, or low-detail photos.
Collapsed {len(aliases):,} byte-identical paths.
{'Comparison limit reached. Increase --comparison-budget to analyze more photos.' if capped else ''}
Showing {len(visual_cards)} of {len(versions)} groups after skipping {args.offset}.
Up to {MAX_PREVIEWS_PER_GROUP} previews per group.</p>
{''.join(visual_cards) or '<p>No visual groups on this page.</p>'}
</section>
<section id="categories"><h2>Suggested categories</h2>
<p>OpenAI labels are suggestions for the selected photo thumbnails. Category assignments do not move files.</p>
{'<ul>' + ''.join(categories) + '</ul>' if categories else ''}
<p>Saved suggestions for the scanned snapshot, grouped by label. Offset {args.offset}.</p>
{category_groups or '<p>No saved suggestions on this page.</p>'}
<details><summary>Photo IDs on this page</summary><ul>{''.join(inventory_items)}</ul></details>
</section>
</main><script>{_SCRIPT}</script></html>
"""
    with output.open("x", encoding="utf-8") as handle:
        handle.write(html)
    print(f"Review report: {str(output)!r}")
    if warning:
        print("WARN: See coverage and categorization details in the report.", file=sys.stderr)
    return int(warning)
