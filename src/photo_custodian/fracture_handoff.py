"""Plan a Fracture handoff from an independently anchored byte manifest."""

from __future__ import annotations

import base64
import os
import re

from .intake import _write_json
from .manifest_verify import read_manifest

# Fracture ef192c86: vigia_agent._build_orchestrator_kwargs, single-file path.
# A suffix suggests routing only; it says nothing about validity or provenance.
_SINGLE_FILE_ROUTES = {
    b".evtx": "event_logs",
    b".e01": "disk_path",
    b".pcap": "pcap_path",
    b".pcapng": "pcap_path",
    b".vmem": "memory_path",
    b".mem": "memory_path",
    b".dmp": "memory_path",
}
_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")


def plan(args) -> int:
    if not _CASE_ID.fullmatch(args.case_id):
        raise ValueError("Use an opaque case ID of up to 80 ASCII letters, digits, hyphens or underscores.")
    output = args.output.expanduser()
    if output.suffix.lower() != ".json" or os.path.lexists(output):
        raise ValueError("Use a new JSON output outside the working copy.")
    manifest, digest = read_manifest(args.manifest)
    if digest != args.expected_sha256:
        raise ValueError("Manifest does not match the independently supplied digest.")
    files = []
    for item in manifest["files"]:
        relative = base64.b64decode(item["relative_path_b64"], validate=True)
        route = _SINGLE_FILE_ROUTES.get(os.path.splitext(relative)[1].lower())
        files.append({
            "file_id": item["file_id"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "suggested_single_file_route": route,
            "disposition": "parser_candidate" if route else "requires_examiner_mapping",
        })
    _write_json(output, {
        "schema_version": 1,
        "purpose": "fracture-intake-plan",
        "case_id": args.case_id,
        "source_id": manifest["source_id"],
        "working_copy_manifest_payload_sha256": digest,
        "fracture_contract_revision": "ef192c86",
        "working_copy_bytes_checked_in_this_step": False,
        "files": files,
    })
    print(f"Fracture intake plan contains {len(files)} references.")
    return 0
