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
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"photo-custodian: {error}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {arguments.command}")
    return 2
