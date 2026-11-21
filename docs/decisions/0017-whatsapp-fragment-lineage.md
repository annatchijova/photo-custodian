# ADR 0017: Checked WhatsApp text-fragment lineage

WhatsApp text exports are staged as bounded working-copy fragments. A range in
an intake index alone is insufficient to establish that a catalogued fragment
is the bytes staged by that intake. The v1 index did not record a fragment
digest, so legacy staging directories remain explicitly unverified.

New intake writes a small, private receipt for each fragment only after the
source descriptor has remained stable and all staging has completed. The
receipt records the fragment digest, exact byte range, declared export digest,
and source size. ZIP receipts additionally identify the one-based ZIP member
and its decompressed digest; generated member numbers, rather than original
member names, distinguish duplicate ZIP names. A root marker is written last,
so an interrupted intake cannot be promoted to checked lineage.

Lineage validation reads receipts and data below a directory descriptor without
following symlinks. It checks the catalogued fragment digest and, for ZIP,
rehashes the staged decompressed member once per batch. Public handoffs expose
only hashes, numeric locators, and range basis. They do not expose filenames,
chat text, or ZIP entry names. The parent export digest is declared by the
receipt, not independently authenticated; use a separately anchored working
copy manifest to corroborate parent bytes.

Applied skills include abductive-engineering, claim-provenance-discipline,
forensic-persistence, llm-out-of-the-loop, secure-by-construction,
surgical-patcher and falsifiable-testing.
