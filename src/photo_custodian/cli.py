from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        with Catalog(arguments.catalog) as catalog:
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
