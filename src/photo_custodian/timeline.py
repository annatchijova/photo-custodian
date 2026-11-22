"""Export a metadata-only, clock-domain-preserving catalog timeline."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from html import escape
from pathlib import Path

_FORMAT_VERSION = 1
_STATUSES = {"active", "missing"}


def _record(row: sqlite3.Row) -> dict:
    """A filesystem timestamp is recorded metadata, never an event-time claim."""
    if (type(row["id"]) is not int or row["id"] < 1
            or type(row["source_id"]) is not int or row["source_id"] < 1
            or type(row["mtime_ns"]) is not int or row["status"] not in _STATUSES):
        raise ValueError("Catalog timeline row is malformed.")
    return {
        "file_id": row["id"], "source_id": row["source_id"],
        "recorded_filesystem_mtime_ns": row["mtime_ns"],
        "timestamp_semantics": "filesystem_metadata_recorded_time",
        "epistemic_level": "recorded_metadata",
        "sequence_scope": "within_source_only",
        "inventory_status": row["status"],
    }


def _render_ns(value: int) -> str:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    try:
        base = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return f"unrenderable-recorded-ns:{value}"
    return f"{base}.{nanoseconds:09d}+00:00"


def _write_html(output, header: dict, tracks: dict[int, list[dict]]) -> None:
    output.write("""<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'">
<title>Cronología de metadatos</title><style>
body{font:1rem/1.55 system-ui;max-width:70rem;margin:auto;padding:1rem;color-scheme:light dark}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.45rem;border-bottom:1px solid GrayText}
details{margin:1rem 0}</style><main><h1>Cronología de metadatos</h1>
<p>Esta salida no afirma cuándo ocurrió un hecho. Cada fecha es un metadato del sistema de archivos registrado en el catálogo. Las fuentes son dominios de reloj separados: no se establece un orden entre fuentes sin un ancla independiente.</p>
""")
    for source_id, rows in tracks.items():
        output.write(f"<details><summary>Fuente {source_id} · {len(rows)} registros</summary><table><thead><tr><th>Archivo</th><th>Fecha registrada UTC</th><th>Base</th><th>Estado</th></tr></thead><tbody>")
        for item in rows:
            rendered = _render_ns(item["recorded_filesystem_mtime_ns"])
            output.write("<tr><td>{}</td><td>{}</td><td>Metadato del sistema de archivos</td><td>{}</td></tr>".format(
                item["file_id"], escape(rendered), escape(item["inventory_status"])))
        output.write("</tbody></table></details>")
    output.write(f"<p>Exportación UTC · {escape(header['exported_utc'])}</p></main></html>")


def export(args) -> int:
    output = Path(args.output).expanduser()
    if output.suffix.lower() not in (".json", ".html") or os.path.lexists(output):
        raise ValueError("Use a new .json or .html output file.")
    requested = sorted(set(args.source_id or ()))
    if len(requested) > 100 or any(type(item) is not int or not 1 <= item <= 2**63 - 1 for item in requested):
        raise ValueError("Use at most 100 positive source IDs.")
    catalog = Path(args.catalog).expanduser().resolve(strict=True)
    connection = sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)
    temporary = None
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        clause, parameters = "", []
        if requested:
            marks = ",".join("?" for _ in requested)
            if connection.execute(f"SELECT COUNT(*) FROM sources WHERE id IN ({marks})", requested).fetchone()[0] != len(requested):
                raise ValueError("At least one requested source ID is absent from the catalog.")
            clause, parameters = f" WHERE source_id IN ({marks})", requested
        rows = connection.execute("SELECT f.id, f.source_id, f.mtime_ns, f.status FROM files f" + clause.replace("source_id", "f.source_id") +
                                  " ORDER BY source_id, mtime_ns, id", parameters).fetchall()
        tracks: dict[int, list[dict]] = {}
        for row in rows:
            tracks.setdefault(row["source_id"], []).append(_record(row))
        header = {
            "schema_version": _FORMAT_VERSION, "purpose": "catalog-metadata-timeline",
            "timestamp_scope": "recorded filesystem metadata; not event time",
            "cross_source_ordering": "not_established_without_anchor",
            "source_ids_filter": requested, "exported_utc": datetime.now(timezone.utc).isoformat(),
        }
        profile_clause = (f" WHERE s.id IN ({','.join('?' for _ in requested)})" if requested else "")
        profiles = connection.execute("""
            SELECT s.id AS source_id, coalesce(p.timestamp_semantics, 'unknown') AS timestamp_semantics,
                   coalesce(p.declared_by, 'system') AS declared_by
            FROM sources s LEFT JOIN source_time_profiles p ON p.source_id = s.id
        """ + profile_clause + " ORDER BY s.id", requested).fetchall()
        header["source_time_profiles"] = [dict(row) for row in profiles]
        descriptor, temporary = tempfile.mkstemp(prefix=".timeline-", dir=output.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            if output.suffix.lower() == ".json":
                json.dump({"header": header, "tracks": tracks}, handle, ensure_ascii=True, sort_keys=True)
                handle.write("\n")
            else:
                _write_html(handle, header, tracks)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)
    finally:
        connection.close()
        if temporary is not None and os.path.lexists(temporary):
            os.unlink(temporary)
    print(json.dumps({"sources": len(tracks), "records": sum(map(len, tracks.values()))}))
    return 0
