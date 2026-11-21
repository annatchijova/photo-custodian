# ADR 0009: Metadata-only review handoff

Export a read-only SQLite snapshot to private HTML or JSON. The HTML exposes
coverage and a bounded list of proposed IDs. JSON streams the full selection
and records inventory status, extraction status, and advisory triage status
independently. A current extraction is joined by file ID, analysis version
and catalog fingerprint. The latest recorded scan state is distinct from
whether a source is currently accessible.

Keep filenames, media, extracted text, and model prose out of these exports.
Defense and complaint labels can coexist. Missing lane fields indicate
unassessed records. An empty model suggestion does not establish relevance.

The index uses catalog-local IDs and stat observations. A working-copy byte
manifest can be produced separately. External forensic tools may consume the
JSON through a future adapter. This export has no custody or delivery
authority and performs no network requests.

Applied skills include claim-provenance-discipline, non-overwhelming-ux,
secure-by-construction and honest-degradation.
