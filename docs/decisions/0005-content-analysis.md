# ADR 0005: Searchable archive evidence and advisory decisions

Use mounted or exported directories as intake sources. Keep extraction,
fingerprints, review status, search and batch scheduling in Python and SQLite.
Pillow, Tesseract, Poppler and FFmpeg do bounded media processing outside the
model. This keeps the existing catalog as the source of file identities and
avoids a second application runtime or a hosted copy of the library.

A file from any intake source may contain misleading text, a malformed
payload or a misleading name. Open verified regular files, pass only a bounded
extract or metadata-free preview to an explicitly enabled model, and keep
model output as typed advice. The model receives no shell tools, paths or
database authority. Saved text is evidence from extraction, while alleged
reasons for saving an item are model hypotheses. A keep recommendation can
name multiple photos or abstain. It has no authority over the source files.

The coherent destination is a local archive with searchable content, human
review and resumable jobs, followed by an independently authorized reversible
action layer. Broader format and device integration must preserve the same
identity and disclosure boundaries.

Applied skills include destination-driven-construction, language-selection,
secure-by-construction, agent-trust-boundaries, llm-out-of-the-loop,
honest-degradation, and non-overwhelming-ux.
