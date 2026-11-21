"""Export metadata-only review leads and catalog coverage for an authorized reviewer."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from .archive import ANALYSIS_VERSION, KINDS, _FIELDS
from .derivative_lineage import public_lineage

_LANES = ("defense", "complaint")
_FORMAT_VERSION = 1
_HTML_LEAD_LIMIT = 200


def _parent_check(row: sqlite3.Row) -> dict:
    status = row["parent_status"]
    digest = row["parent_manifest_sha256"]
    checked = row["parent_checked_utc"]
    file_id = row["parent_manifest_file_id"]
    source_id = row["parent_manifest_source_id"]
    if (
        status in ("match", "mismatch", "unavailable")
        and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)
        and isinstance(checked, str) and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?\+00:00", checked
        )
        and type(file_id) is int and file_id > 0
        and type(source_id) is int and source_id > 0
    ):
        return {
            "status": status, "manifest_sha256": digest, "manifest_file_id": file_id,
            "manifest_source_id": source_id, "checked_utc": checked,
            "scope": "working_copy_bytes_for_declared_hash",
        }
    return {"status": "not_checked"}


def _record(row: sqlite3.Row) -> dict:
    extraction = row["analysis_status"] or ("pending" if row["analyzable"] else "unsupported")
    if row["status"] == "missing":
        extraction = "not_current"
    raw = row["advice"] or "{}"
    try:
        advice = json.loads(raw)
    except (ValueError, RecursionError):
        advice = {}
    if not isinstance(advice, dict):
        advice = {}
    assessed = extraction == "advised" and all(
        isinstance(advice.get(f"{lane}_lead"), str) for lane in _LANES
    )
    leads = [lane for lane in _LANES if assessed and advice[f"{lane}_lead"].strip()]
    coverage = advice.get("_coverage") if extraction in ("extracted", "advised", "unavailable") else None
    if not isinstance(coverage, dict):
        coverage = {}
    text = coverage.get("text")
    text = text if text in ("bounded", "truncated", "empty", "unavailable", "not_requested") else "unknown"
    visual = coverage.get("visual")
    truncated_for_model = coverage.get("model_text_truncated")
    rejected = extraction == "extracted" and advice.get("_triage_state") == "rejected"
    lineage = public_lineage(
        row["lineage_status"] if row["status"] == "active" else None, row["lineage_record"]
    )
    if lineage["status"] == "derivative_matched_receipt":
        lineage["parent_byte_check"] = _parent_check(row)
    return {
        "file_id": row["file_id"],
        "source_id": row["source_id"],
        "bytes": row["size_bytes"],
        "catalog_fingerprint": json.loads(row["identity"]),
        "catalog_modified_ns": row["mtime_ns"],
        "inventory_status": row["status"],
        "extraction_status": extraction,
        "triage_status": ("proposed_lead" if leads else "no_lead_proposed") if assessed
                         else "model_rejected" if rejected else "not_assessed",
        "text_coverage": text,
        "visual_sample_available": visual if type(visual) is bool else None,
        "model_text_truncated": truncated_for_model if type(truncated_for_model) is bool else None,
        "proposed_lanes": leads,
        "lineage": lineage,
    }


def _count(summary: Counter, item: dict) -> None:
    summary[item["extraction_status"]] += 1
    summary[item["triage_status"]] += 1
    summary[f"text_{item['text_coverage']}"] += 1
    summary[f"lineage_{item['lineage']['status']}"] += 1
    if "parent_byte_check" in item["lineage"]:
        summary[f"parent_{item['lineage']['parent_byte_check']['status']}"] += 1
    if item["visual_sample_available"] is True:
        summary["visual_sample_available"] += 1
    if item["model_text_truncated"] is True:
        summary["model_text_truncated"] += 1


def _write_json(output, rows, header: dict) -> dict:
    summary = Counter()
    output.write('{"header":')
    json.dump(header, output)
    output.write(',"items":[\n')
    for index, row in enumerate(rows):
        item = _record(row)
        _count(summary, item)
        if index:
            output.write(",\n")
        json.dump(item, output, ensure_ascii=True)
    output.write('\n],"coverage":')
    json.dump(dict(sorted(summary.items())), output)
    output.write("}\n")
    return summary


def _write_html(output, rows, header: dict) -> dict:
    summary = Counter()
    shown = 0
    output.write("""<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'">
<title>Referencias para revisión</title>
<style>
body{font:1rem/1.55 system-ui;max-width:58rem;margin:auto;padding:1rem;color-scheme:light dark}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.5rem;border-bottom:1px solid GrayText}
summary{cursor:pointer;padding:.5rem} a:focus-visible,summary:focus-visible{outline:2px solid Highlight}
</style>
<main><h1>Referencias para revisión</h1>
<p>Mostramos identificadores del catálogo y las líneas que propuso el agente.
La ausencia de una propuesta permite saber cómo respondió el agente y deja pendiente la evaluación humana.</p>
<p><a href="#coverage">Ir a cobertura del análisis</a></p>
<details><summary>Ver identificadores propuestos</summary>
<table><thead><tr><th scope="col">Archivo</th><th scope="col">Fuente</th><th scope="col">Línea propuesta</th><th scope="col">Vínculo registrado</th></tr></thead><tbody>
""")
    labels = {"defense": "Defensa", "complaint": "Denuncia"}
    for row in rows:
        item = _record(row)
        _count(summary, item)
        if item["proposed_lanes"] and shown < _HTML_LEAD_LIMIT:
            shown += 1
            lines = " y ".join(labels[lane] for lane in item["proposed_lanes"])
            lineage = item["lineage"]
            if lineage.get("kind") == "mime_part":
                reference = f"Parte MIME {lineage['part_index']}"
            elif lineage.get("kind") == "transcript_window":
                reference = f"Ventana solicitada desde {lineage['requested_start_seconds']} s"
            elif lineage.get("kind") == "whatsapp_text_fragment":
                basis = ("export" if lineage["range_basis"] == "export_bytes"
                         else "miembro ZIP descomprimido")
                reference = (f"Fragmento WhatsApp {lineage['entry_index']} · {basis} · "
                             f"bytes {lineage['byte_start']}-{lineage['byte_end']}")
            elif lineage["status"] == "unverified":
                reference = "Sin verificar"
            else:
                reference = "Sin vínculo registrado"
            output.write(f"<tr><td>{item['file_id']}</td><td>{item['source_id']}</td>"
                         f"<td>{lines}</td><td>{reference}</td></tr>\n")
    output.write("</tbody></table>")
    if not shown:
        output.write("<p>Esta selección no tiene propuestas registradas.</p>")
    elif summary["proposed_lead"] > shown:
        omitted = summary["proposed_lead"] - shown
        output.write(f"<p>{omitted} referencias adicionales. El índice JSON incluye la selección completa.</p>")
    output.write("</details><section id='coverage'><h2>Cobertura del análisis</h2><ul>")
    for key, label in (
        ("not_current", "Archivo marcado como faltante en el catálogo"),
        ("pending", "Extracción pendiente"), ("unsupported", "Formato fuera del flujo de extracción"),
        ("unavailable", "Extracción no disponible"), ("extracted", "Extracción guardada"),
        ("advised", "Extracción con respuesta del modelo"), ("not_assessed", "Sin evaluación de las dos líneas"),
        ("proposed_lead", "Con al menos una línea propuesta"), ("no_lead_proposed", "Evaluados sin propuesta"),
        ("model_rejected", "Sin evaluación por respuesta inválida del modelo"),
        ("text_unavailable", "Extracción de texto no disponible"),
        ("text_empty", "Extracción de texto vacía"),
        ("text_truncated", "Texto limitado por tamaño"),
        ("text_bounded", "Muestra de texto procesada"),
        ("text_unknown", "Cobertura de texto sin registrar"),
        ("text_not_requested", "Extracción de texto no solicitada"),
        ("visual_sample_available", "Muestra visual disponible"),
        ("model_text_truncated", "Texto enviado al modelo recortado"),
        ("lineage_derivative_matched_receipt", "Derivados cotejados con su registro de origen"),
        ("lineage_unverified", "Derivados con vínculo sin verificar"),
        ("parent_match", "Derivados cuyo hash padre tiene un cotejo de bytes coincidente"),
        ("parent_mismatch", "Derivados cuyo último cotejo padre presentó diferencias"),
        ("parent_unavailable", "Derivados cuyo padre no pudo leerse durante el último cotejo"),
    ):
        output.write(f"<li>{label} · {summary[key]}</li>")
    incomplete = sum(item["latest_scan_status"] != "complete" for item in header["sources"])
    output.write(f"</ul><p>Fuentes registradas: {len(header['sources'])}. "
                 f"Sin último escaneo completo: {incomplete}.</p>")
    unavailable = sum(item["last_attachment_status"] == "unavailable" for item in header["sources"])
    output.write(f"<p>Fuentes registradas como desconectadas o inaccesibles: {unavailable}.</p>")
    output.write(f"<p>Exportación UTC · {escape(header['exported_utc'])}</p></section></main></html>")
    return summary


def handoff(args) -> int:
    source_ids = sorted(set(args.source_id or ()))
    if len(source_ids) > 100 or any(not 1 <= item <= 2**63 - 1 for item in source_ids):
        raise ValueError("Use at most 100 positive source IDs.")
    output = args.output.expanduser()
    if output.suffix.lower() not in (".html", ".json") or os.path.lexists(output):
        raise ValueError("Use a new .html or .json output file.")
    catalog_path = Path(args.catalog).expanduser().resolve(strict=True)
    connection = sqlite3.connect(catalog_path.as_uri() + "?mode=ro", uri=True)
    temporary = None
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        parameters = [ANALYSIS_VERSION]
        has_lineage = connection.execute("""
            SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'file_lineage'
        """).fetchone() is not None
        fields = ("l.status AS lineage_status, l.record AS lineage_record" if has_lineage
                  else "NULL AS lineage_status, NULL AS lineage_record")
        has_parent_checks = has_lineage and connection.execute("""
            SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'parent_byte_checks'
        """).fetchone() is not None
        for column in ("status", "manifest_sha256", "manifest_file_id", "manifest_source_id", "checked_utc"):
            value = f"p.{column}" if has_parent_checks else "NULL"
            fields += f", {value} AS parent_{column}"
        query = _FIELDS.replace("SELECT f.*,", f"SELECT ({KINDS['all']}) AS analyzable, {fields}, f.*,", 1)
        where = "WHERE f.status IN ('active', 'missing')"
        if has_parent_checks:
            where = """LEFT JOIN parent_byte_checks p ON p.rowid = (
                SELECT rowid FROM parent_byte_checks c
                WHERE c.parent_sha256 = CASE WHEN json_valid(l.record)
                    THEN json_extract(l.record, '$.parent_sha256') END
                ORDER BY c.checked_utc DESC, c.id DESC LIMIT 1
            ) """ + where
        if has_lineage:
            where = """LEFT JOIN file_lineage l ON l.file_id = f.id
                AND l.fingerprint = json_array(f.size_bytes, f.mtime_ns, f.ctime_ns, f.device, f.inode)
                """ + where
        query = query.replace("WHERE f.status = 'active'", where)
        source_filter = ""
        if source_ids:
            marks = ",".join("?" for _ in source_ids)
            found = connection.execute(f"SELECT COUNT(*) FROM sources WHERE id IN ({marks})",
                                       source_ids).fetchone()[0]
            if found != len(source_ids):
                raise ValueError("At least one requested source ID is absent from the catalog.")
            query += f" AND f.source_id IN ({marks})"
            parameters.extend(source_ids)
            source_filter = f"WHERE s.id IN ({marks})"
        sources = connection.execute(f"""
            SELECT s.id, s.status AS last_attachment_status,
                   coalesce(r.status, 'not_scanned') AS latest_scan_status
            FROM sources s LEFT JOIN scan_runs r ON r.id = (
                SELECT max(id) FROM scan_runs WHERE source_id = s.id
            ) {source_filter} ORDER BY s.id
        """, source_ids).fetchall()
        rows = connection.execute(query + " ORDER BY f.source_id, f.id", parameters)
        header = {
            "schema_version": _FORMAT_VERSION,
            "purpose": "catalog-review-leads",
            "reference_scope": "IDs are local to this catalog; last recorded inventory state",
            "source_ids_filter": source_ids, "sources": [dict(row) for row in sources],
            "exported_utc": datetime.now(timezone.utc).isoformat(),
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".case-handoff-", dir=output.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            writer = _write_html if output.suffix.lower() == ".html" else _write_json
            summary = writer(handle, rows, header)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)
    finally:
        connection.close()
        if temporary is not None:
            os.unlink(temporary)
    print(json.dumps({
        "proposed_lead": summary["proposed_lead"],
        "no_lead_proposed": summary["no_lead_proposed"],
        "not_assessed": summary["not_assessed"],
        "model_rejected": summary["model_rejected"],
    }))
    return 0
