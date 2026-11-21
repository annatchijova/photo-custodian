# ADR 0008: Private intake and advisory evidence triage

Gmail intake uses only fixed-origin GET requests with a separately supplied
Gmail read-only OAuth token. It keeps raw MIME messages, including embedded
attachments, in a private directory with per-message metadata and SHA-256.
A checkpoint binds the paging cursor to an account and query. Gmail pages
represent an API listing, not a forensic device image or a stable mailbox
snapshot. Authentication and consent are handled outside the model.

WhatsApp intake accepts supplied exports. ZIP members get generated names
and an index linking them to the source archive and member names. Text
chunks retain source-relative byte ranges. A completed index records the
source digest. An interrupted run can leave staged files without a completed
index. Never infer source completeness from a partial directory.

The local Whisper adapter accepts installed weights and a bounded sample.
Its timestamps are media offsets. OCR, MIME decoding, transcriptions and
model interpretations are derivatives. Preserve the originals for forensic
review. Vision text estimates are stored separately from local OCR.
Independent defense and complaint leads are advisory and may coexist.
The default archive view omits excerpts and model notes.

Applied skills include destination-driven-construction,
claim-provenance-discipline, secure-by-construction, agent-trust-boundaries
and honest-degradation.
