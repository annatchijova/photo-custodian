# ADR 0014: Private storage and resumable triage failures

Create a private catalog file before opening it with SQLite. A normal catalog
open restricts permissions on existing catalog files and sidecars owned by
the user. Read-only commands do not alter existing file permissions. Publish
HTML using the same private, no-replace output mechanism as intake metadata.
The caller still controls access to the parent directory.

Run bounded extractors with an explicit process environment. Local evidence
retains bounded output, and handoff reports the status of text, visual sample
and model truncation without emitting sensitive prose. Older records have
unknown extraction coverage. A rejected model response is recorded per item
and the queue advances. Provider failures pause the batch. A deliberate retry
can revisit rejected outputs.

Verify bytes against receipts when reusing Gmail and attachment files.
A separate offline Gmail audit covers previously downloaded messages,
including a completed download. These comparisons use local receipts and
carry no claim of independent acquisition authenticity.

Applied skills include secure-by-construction, surgical-patcher,
claim-provenance-discipline and honest-degradation.
