"""Resumable archive analysis, local retrieval, and review plans."""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import sys
from argparse import Namespace
from html import escape
from pathlib import Path

from . import archive_advisor
from .advisor import AdviceRejected, AdviceUnavailable
from .archive_evidence import EvidenceUnavailable, extract
from .catalog import Catalog
from .derivative_lineage import link_row
from .inference import select_inference
from .intake import _write
from .registered_sources import require_attached_source

ANALYSIS_VERSION = 1
KINDS = {
    "all": "(f.media_kind IN ('image', 'video') OR f.extension IN "
           "('.pdf', '.txt', '.eml', '.mp3', '.m4a', '.aac', '.wav', '.ogg', '.opus', '.flac', '.amr'))",
    "image": "f.media_kind = 'image'",
    "video": "f.media_kind = 'video'",
    "pdf": "f.extension = '.pdf'",
    "text": "f.extension IN ('.txt', '.eml')",
    "audio": "f.extension IN ('.mp3', '.m4a', '.aac', '.wav', '.ogg', '.opus', '.flac', '.amr')",
}
_FIELDS = """
SELECT f.*, f.id AS file_id, s.root_path_bytes, s.root_path,
       json_array(f.size_bytes, f.mtime_ns, f.ctime_ns, f.device, f.inode) AS identity,
       a.status AS analysis_status, a.scope, a.extracted_text, a.advice, a.model
FROM files f JOIN sources s ON s.id = f.source_id
LEFT JOIN archive_analysis a ON a.file_id = f.id AND a.version = ?
  AND a.fingerprint = json_array(f.size_bytes, f.mtime_ns, f.ctime_ns, f.device, f.inode)
WHERE f.status = 'active'
"""


def _source(source: Path) -> bytes:
    resolved = source.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Source must be a mounted or exported directory.")
    return os.fsencode(resolved)


def _save(catalog: Catalog, row: sqlite3.Row, *, status: str, scope: str,
          extracted: str = "", advice: dict | None = None, model: str = "") -> None:
    catalog.connection.execute("""
        INSERT INTO archive_analysis
            (file_id, version, fingerprint, status, scope, extracted_text, advice, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id, version, fingerprint) DO UPDATE SET status = excluded.status,
            scope = excluded.scope, extracted_text = excluded.extracted_text,
            advice = excluded.advice, model = excluded.model
        WHERE archive_analysis.status != 'advised' OR excluded.status = 'advised'
    """, (row["file_id"], ANALYSIS_VERSION, row["identity"], status, scope, extracted,
          json.dumps(advice or {}, ensure_ascii=True), model))
    catalog.flush()


def analyze_batch(catalog: Catalog, args: Namespace) -> int:
    if not 1 <= args.limit <= 100:
        raise ValueError("Use a batch limit from 1 to 100.")
    if args.ocr_language not in ("eng", "spa", "eng+spa"):
        raise ValueError("Use eng, spa or eng+spa for OCR.")
    inference = select_inference(args)
    if args.source_id is not None and not 1 <= args.source_id <= 2**63 - 1:
        raise ValueError("Use a positive catalog source ID.")
    selector = "s.id = ?" if args.source_id is not None else "s.root_path_bytes = ?"
    value = args.source_id if args.source_id is not None else _source(args.source)
    source = catalog.connection.execute(f"""
        SELECT s.*, m.filesystem_uuid, m.folder_bytes
        FROM sources s LEFT JOIN registered_mounts m ON m.source_id = s.id
        WHERE {selector}
    """, (value,)).fetchone()
    if source is None:
        raise ValueError("Scan this source first.")
    require_attached_source(source)
    if source["filesystem_uuid"] is None:
        _source(Path(os.fsdecode(source["root_path_bytes"])))
    states = ["a.file_id IS NULL"]
    if inference:
        if args.retry_triage:
            states.append("a.status = 'extracted'")
        else:
            states.append("""a.status = 'extracted' AND
                CASE WHEN json_valid(a.advice) THEN json_extract(a.advice, '$._triage_state')
                     END IS NOT 'rejected'""")
    if args.retry_unavailable:
        states.append("a.status = 'unavailable'")
    rows = catalog.connection.execute(
        _FIELDS + f" AND f.source_id = ? AND {KINDS[args.kind]}"
        + " AND (" + " OR ".join(states) + ") ORDER BY f.id LIMIT ?",
        (ANALYSIS_VERSION, source["id"], args.limit),
    ).fetchall()

    if not rows:
        print("No pending items in this source and file type.")
        return 3
    complete = unavailable = rejected = incomplete = unverified_links = 0
    lineage_cache: dict = {}
    for row in rows:
        if link_row(catalog.connection, row, lineage_cache) == "unverified":
            unverified_links += 1
        try:
            evidence = extract(row, ocr_language=args.ocr_language, whisper_model=args.whisper_model)
        except (EvidenceUnavailable, OSError, ImportError) as error:
            require_attached_source(source)
            if isinstance(error, EvidenceUnavailable):
                reason = str(error)
            elif isinstance(error, ImportError):
                reason = "Install the review extra for photo decoding."
            else:
                reason = "Source file could not be read."
            previous_scope = row["scope"]
            if previous_scope and row["analysis_status"] == "extracted":
                reason = f"{previous_scope}. Current read unavailable: {reason}"
            _save(catalog, row, status="unavailable", scope=reason,
                  extracted=row["extracted_text"] or "",
                  advice={"_coverage": {"text": "unavailable", "visual": False}})
            unavailable += 1
            continue
        coverage = {"text": evidence.text_coverage, "visual": bool(evidence.image)}
        if evidence.text_coverage == "unavailable":
            incomplete += 1
        _save(catalog, row, status="extracted", scope=evidence.scope, extracted=evidence.text,
              advice={"_coverage": coverage})
        if inference:
            try:
                advisory = archive_advisor.analyze(evidence, inference=inference)
            except AdviceRejected:
                _save(catalog, row, status="extracted", scope=evidence.scope,
                      extracted=evidence.text, advice={"_coverage": coverage, "_triage_state": "rejected"},
                      model=f"{inference.provider}:{inference.model}")
                rejected += 1
                continue
            except AdviceUnavailable as error:
                print(f"{inference.provider} paused after {complete} items. Local evidence saved. {error}", file=sys.stderr)
                return 2
            _save(catalog, row, status="advised", scope=evidence.scope, extracted=evidence.text,
                  advice={**advisory, "_coverage": {
                      **coverage, "model_text_truncated": len(evidence.text) > 16_000,
                  }},
                  model=f"{inference.provider}:{inference.model}")
        complete += 1
    print(f"Batch complete: {complete} processed, {unavailable} unavailable, "
          f"{incomplete} with unavailable text extraction, {rejected} rejected by the model, "
          f"{unverified_links} with unverified derivative lineage.")
    return int(bool(unavailable or rejected or incomplete or unverified_links))


def compare_photos(catalog: Catalog, args: Namespace) -> int:
    ids = list(dict.fromkeys(args.ids))
    inference = select_inference(args)
    if inference is None or not 2 <= len(ids) <= 6 or any(not 1 <= item <= 2**63 - 1 for item in ids):
        raise ValueError("Select --openai or --ollama and 2 to 6 distinct positive photo IDs.")
    candidates = []
    for item in ids:
        row = catalog.connection.execute(
            _FIELDS + " AND f.id = ? AND f.media_kind = 'image'", (ANALYSIS_VERSION, item)
        ).fetchone()
        if row is None:
            raise ValueError(f"Photo ID {item} is not active in this catalog.")
        candidates.append((item, extract(row, use_ocr=False)))
    proposal = archive_advisor.compare(candidates, inference=inference)
    print(json.dumps({
        "schema_version": 1, "purpose": "advisory-keep-proposal", "photos_reviewed": ids,
        "provider": inference.provider, "model": inference.model, **proposal,
    }, indent=2))
    return 0


def archive_report(catalog: Catalog, args: Namespace) -> int:
    if not 1 <= args.limit <= 100 or not 0 <= args.offset <= 2**63 - 1:
        raise ValueError("Use --limit 1–100 and a non-negative --offset.")
    query = args.query.strip()
    if len(query) > 500:
        raise ValueError("Search text is limited to 500 characters.")
    where = ""
    parameters: list[int | str] = [ANALYSIS_VERSION]
    if query:
        where += """ AND (instr(lower(f.relative_path), lower(?)) > 0
            OR instr(lower(coalesce(a.extracted_text, '')), lower(?)) > 0
            OR instr(lower(coalesce(a.advice, '')), lower(?)) > 0)"""
        parameters.extend([query] * 3)
    if args.file_id is not None:
        if not 1 <= args.file_id <= 2**63 - 1:
            raise ValueError("--file-id must be a positive catalog ID.")
        where += " AND f.id = ?"
        parameters.append(args.file_id)
    if args.case_lane:
        where += " AND CASE WHEN json_valid(a.advice) THEN json_extract(a.advice, ?) END <> ''"
        parameters.append(f"$.{args.case_lane}_lead")
    if args.category:
        where += " AND CASE WHEN json_valid(a.advice) THEN json_extract(a.advice, '$.category') END = ?"
        parameters.append(args.category)
    rows = catalog.connection.execute(
        _FIELDS + where + " ORDER BY f.id LIMIT ? OFFSET ?",
        (*parameters, args.limit, args.offset),
    ).fetchall()
    cards = []
    for row in rows:
        if args.output is None:
            result = dict(row)
            fields = ("file_id", "root_path", "relative_path", "analysis_status", "scope", "model")
            if args.show_content:
                fields += ("extracted_text", "advice")
            print(json.dumps({field: result[field] for field in fields}))
            continue
        advice = json.loads(row["advice"] or "{}") if args.show_content else {}
        pieces = []
        for field, label in (
            ("category", "Category"), ("title", "Proposed title"), ("summary", "Summary"),
            ("important_information", "Important information"),
            ("saved_reason_hypothesis", "Possible reason for saving"),
            ("suggested_rule", "Suggested filing rule"),
            ("vision_transcription", "Text estimated by the vision model"),
            ("defense_lead", "Possible lead for defense review"),
            ("complaint_lead", "Possible lead for complaint review"),
        ):
            if advice.get(field):
                pieces.append(f"<dt>{label}</dt><dd>{escape(advice[field])}</dd>")
        path = escape(str(Path(row["root_path"]) / row["relative_path"]))
        scope = escape(row["scope"] or "Pending extraction")
        details = ""
        if args.show_content:
            raw = escape(row["extracted_text"] or "")
            details = (
                f"<details><summary>Show extracted text and suggestions</summary>"
                f"<dl>{''.join(pieces)}</dl><pre>{raw}</pre></details>"
            )
        cards.append(f"<article><h2>ID {row['file_id']}</h2><p>{path}</p><p>{scope}</p>"
                     f"{details}</article>")
    if args.output is not None:
        output = args.output.expanduser()
        if output.suffix.lower() != ".html":
            raise ValueError("--output must be a new .html file.")
        body = "".join(cards) or "<p>No entries on this page.</p>"
        html = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'">
<title>Archive review</title><style>
body{{color-scheme:light dark;font:1rem/1.5 system-ui;max-width:68rem;margin:auto;padding:1rem}}
article{{border-block-start:1px solid GrayText;overflow-wrap:anywhere}} pre{{white-space:pre-wrap}}
dt{{font-weight:600}}summary{{cursor:pointer;min-height:2rem}}</style>
<main><h1>Archive review</h1><p>{len(cards)} entries from offset {args.offset}.</p>
<p>Extracted text is evidence from a bounded sample. Keep and filing proposals are advisory.</p>
{body}</main></html>"""
        _write(output, html.encode("utf-8"))
    return 0


def workflow_script(args: Namespace) -> None:
    if not 1 <= args.batches <= 100:
        raise ValueError("Use 1 to 100 workflow batches.")
    mountpoint = args.mountpoint.expanduser().resolve() if args.mountpoint else None
    catalog = str(args.catalog.expanduser().resolve())
    base = ["uv", "run", "photo-custodian"]
    analyze = [*base, "analyze", "--catalog", catalog, "--kind", args.kind]
    report = [*base, "archive", "--catalog", catalog, "--output", str(args.output)]
    if args.source_id is None:
        source = os.fsdecode(_source(args.source))
        if mountpoint and not Path(source).is_relative_to(mountpoint):
            raise ValueError("The source must be inside the specified mountpoint.")
        scan = [*base, "scan", "--catalog", catalog, "--", source]
        analyze.append("--source=" + source)
    else:
        if mountpoint is None or not 1 <= args.source_id <= 2**63 - 1:
            raise ValueError("A registered workflow requires a positive source ID and --mountpoint.")
        locator = ["--source-id", str(args.source_id)]
        scan = [*base, "source-scan", str(args.source_id), "--catalog", catalog,
                "--mount", str(mountpoint)]
        analyze.extend(locator)
        output = args.output.expanduser().resolve()
        report = [*base, "case-handoff", "--catalog", catalog, *locator, "--output", str(output)]
    if args.openai:
        analyze.append("--openai")
    if args.ollama:
        analyze.append("--ollama")
    if args.model:
        analyze.extend(["--model", args.model])
    print("#!/bin/sh")
    print("# Review this workflow and run it from the Photo Custodian project.")
    print("set -eu")
    if mountpoint and args.source_id is None:
        print(shlex.join(["mountpoint", "-q", "--", str(mountpoint)]))
    print(shlex.join(scan))
    print("batch=0")
    print("status=0")
    print("warnings=0")
    print(f'while [ "$batch" -lt {args.batches} ]; do')
    if mountpoint and args.source_id is None:
        print("    " + shlex.join(["mountpoint", "-q", "--", str(mountpoint)]))
    print("    status=0")
    print("    " + shlex.join(analyze) + " || status=$?")
    print('    case "$status" in 0) ;; 1) warnings=1 ;; *) break ;; esac')
    print("    batch=$((batch + 1))")
    print("done")
    print(shlex.join(report))
    print('case "$status" in 0|1|3) exit "$warnings" ;; *) exit "$status" ;; esac')
