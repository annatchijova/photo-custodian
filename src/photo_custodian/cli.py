from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .advisor import AdviceUnavailable, next_step, request_advice
from .catalog import Catalog
from .exact_duplicates import find_exact_duplicates
from .scanner import scan_sources


def default_catalog_path() -> Path:
    explicit = os.environ.get("PHOTO_CUSTODIAN_CATALOG")
    if explicit:
        return Path(explicit).expanduser()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "photo-custodian" / "catalog.sqlite3"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photo-custodian",
        description="Inventory local media without modifying source libraries.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="inventory one or more directory roots")
    scan_parser.add_argument("roots", nargs="+", type=Path)
    scan_parser.add_argument("--catalog", type=Path, default=default_catalog_path())
    scan_parser.add_argument("--json", action="store_true", dest="as_json")

    inventory_parser = subparsers.add_parser(
        "inventory", help="summarize active files in the catalog"
    )
    inventory_parser.add_argument("--catalog", type=Path, default=default_catalog_path())
    inventory_parser.add_argument("--json", action="store_true", dest="as_json")

    duplicates_parser = subparsers.add_parser(
        "duplicates", help="find review-only duplicate candidates"
    )
    duplicate_subparsers = duplicates_parser.add_subparsers(
        dest="duplicate_kind", required=True
    )
    exact_parser = duplicate_subparsers.add_parser(
        "exact", help="hash same-size files and report byte-identical groups"
    )
    exact_parser.add_argument("--catalog", type=Path, default=default_catalog_path())
    exact_parser.add_argument("--limit", type=int, default=100, dest="group_limit")
    exact_parser.add_argument("--json", action="store_true", dest="as_json")
    agent_parser = subparsers.add_parser("agent", help="get one next step for organizing media")
    agent_parser.add_argument("--catalog", type=Path, default=default_catalog_path())
    agent_parser.add_argument("--openai", action="store_true", help="send inventory totals to OpenAI")
    agent_parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"))
    report_parser = subparsers.add_parser("report", help="build a local HTML review report")
    report_parser.add_argument("--catalog", type=Path, default=default_catalog_path())
    report_parser.add_argument("--output", type=Path, required=True)
    report_parser.add_argument("--limit", type=int, default=25, help="groups per section (1–100)")
    report_parser.add_argument("--offset", type=int, default=0, help="groups to skip per section")
    from .visual_review import DEFAULT_COMPARISONS

    report_parser.add_argument("--comparison-budget", type=int, default=DEFAULT_COMPARISONS,
                               help="maximum visual candidate comparisons")
    report_parser.add_argument("--categorize", type=int, nargs="+", metavar="PHOTO_ID",
                               help="send thumbnails of up to 10 selected photo IDs to OpenAI")
    report_parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"))
    from .archive import KINDS

    analyze_parser = subparsers.add_parser("analyze", help="extract and optionally interpret archive content")
    analyze_parser.add_argument("--catalog", type=Path, default=default_catalog_path())
    analyze_parser.add_argument("--source", type=Path, required=True)
    analyze_parser.add_argument("--kind", choices=KINDS, default="all")
    analyze_parser.add_argument("--limit", type=int, default=100)
    analyze_parser.add_argument("--ocr-language", default="eng+spa")
    analyze_parser.add_argument("--retry-unavailable", action="store_true")
    analyze_parser.add_argument("--openai", action="store_true", help="send bounded evidence to OpenAI")
    analyze_parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"))

    archive_parser = subparsers.add_parser("archive", help="search extracted text and generate a review")
    archive_parser.add_argument("--catalog", type=Path, default=default_catalog_path())
    archive_parser.add_argument("--query", default="")
    archive_parser.add_argument("--category")
    archive_parser.add_argument("--limit", type=int, default=25)
    archive_parser.add_argument("--offset", type=int, default=0)
    archive_parser.add_argument("--output", type=Path, help="write a local HTML archive page")

    compare_parser = subparsers.add_parser("compare", help="get an advisory keep proposal for 2–6 photos")
    compare_parser.add_argument("--catalog", type=Path, default=default_catalog_path())
    compare_parser.add_argument("ids", nargs="+", type=int)
    compare_parser.add_argument("--openai", action="store_true", help="send selected photo previews to OpenAI")
    compare_parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"))

    workflow_parser = subparsers.add_parser("workflow", help="print a reviewable archive workflow script")
    workflow_parser.add_argument("--catalog", type=Path, default=default_catalog_path())
    workflow_parser.add_argument("--source", type=Path, required=True)
    workflow_parser.add_argument("--kind", choices=KINDS, default="all")
    workflow_parser.add_argument("--output", type=Path, required=True)
    workflow_parser.add_argument("--openai", action="store_true", help="include opt-in OpenAI analysis")
    workflow_parser.add_argument("--batches", type=int, default=1, help="maximum analysis batches per run")
    workflow_parser.add_argument("--mountpoint", type=Path, help="require this external mount when the script runs")
    return parser


def _print_scan(summary: dict[str, int | str], as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, sort_keys=True))
        return
    print(f"Status: {summary['status']}")
    print(f"Sources: {summary['source_count']}")
    print(f"Discovered files: {summary['discovered_files']}")
    print(f"Logical bytes: {summary['total_bytes']}")
    print(
        "New / changed / unchanged: "
        f"{summary['new_files']} / {summary['changed_files']} / "
        f"{summary['unchanged_files']}"
    )
    print(f"Missing: {summary['missing_files']}")
    print(f"Skipped entries: {summary['skipped_entries']}")
    print(f"Errors: {summary['error_count']}")
    print("Source files modified: 0")


def _print_inventory(rows: list[dict[str, int | str]], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, sort_keys=True))
        return
    if not rows:
        print("Catalog contains no active files.")
        return
    print("Kind\tFiles\tLogical bytes")
    for row in rows:
        print(f"{row['media_kind']}\t{row['file_count']}\t{row['total_bytes']}")


def _terminal_safe(value: str) -> str:
    return "".join(
        character
        if character.isprintable()
        else character.encode("unicode_escape").decode("ascii")
        for character in value
    )


def _print_exact_duplicates(report: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, sort_keys=True))
        return
    print(f"Status: {report['status']}")
    print(f"Candidate files: {report['candidate_files']}")
    print(f"Hashed / reused: {report['hashed_files']} / {report['reused_hashes']}")
    print(f"Stale / errors: {report['stale_files']} / {report['error_count']}")
    print(
        f"Exact groups: {report['total_group_count']} "
        f"(showing {report['returned_group_count']})"
    )
    for group in report["groups"]:
        print(
            f"\n{group['sha256']}  {group['size_bytes']} bytes  "
            f"{group['file_count']} paths  "
            f"{group['reclaimable_bytes']} potentially reclaimable bytes"
        )
        for item in group["files"]:
            print(f"  {_terminal_safe(str(item['path']))}")
        if group["files_truncated"]:
            print(f"  ... {group['files_truncated']} additional paths not shown")
    print("Source files modified: 0")


def _print_advice(arguments: argparse.Namespace) -> int:
    with Catalog(arguments.catalog) as catalog:
        rows = catalog.inventory_summary()
    _print_inventory(rows, as_json=False)
    step = next_step(rows)
    step += f" --catalog {shlex.quote(str(arguments.catalog))}"
    print(_terminal_safe(step), flush=True)
    if not arguments.openai or not rows:
        return 0
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("WARN: Set OPENAI_API_KEY to get OpenAI advice.", file=sys.stderr)
        return 1
    try:
        advice = request_advice(rows, api_key=api_key, model=arguments.model)
    except AdviceUnavailable as error:
        print(f"WARN: {error}", file=sys.stderr)
        return 1
    print(f"OpenAI suggestion: {_terminal_safe(advice)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "agent":
            return _print_advice(arguments)
        if arguments.command == "workflow":
            from .archive import workflow_script

            workflow_script(arguments)
            return 0
        with Catalog(arguments.catalog) as catalog:
            if arguments.command in ("analyze", "archive", "compare"):
                from .archive import analyze_batch, archive_report, compare_photos

                return {
                    "analyze": analyze_batch,
                    "archive": archive_report,
                    "compare": compare_photos,
                }[arguments.command](catalog, arguments)
            if arguments.command == "report":
                from .review_report import write_report

                return write_report(catalog, arguments)
            if arguments.command == "scan":
                summary = scan_sources(catalog, arguments.roots)
                _print_scan(summary.as_dict(), arguments.as_json)
                return 0 if summary.status == "complete" else 1
            if arguments.command == "inventory":
                _print_inventory(catalog.inventory_summary(), arguments.as_json)
                return 0
            if arguments.command == "duplicates" and arguments.duplicate_kind == "exact":
                report = find_exact_duplicates(
                    catalog, group_limit=arguments.group_limit
                )
                _print_exact_duplicates(report.as_dict(), arguments.as_json)
                return 0 if report.status == "complete" else 1
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"photo-custodian: {error}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {arguments.command}")
    return 2
