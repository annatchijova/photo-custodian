# ADR 0007: Working-copy byte manifests

A catalog search reference identifies an extraction. A separate manifest
records which working-copy bytes were read. It can be used when selecting
items for an authorized reviewer without revealing media in the terminal.

The producer requires catalog IDs from one source and rejects a changed
catalog fingerprint. File access descends from a root descriptor without
following symbolic links. It reads regular files incrementally, checks the
identity before and after hashing, and publishes a private manifest outside
the source using a no-replace link on the destination filesystem.

The version 1 payload has a fixed integer/string schema and ordered file IDs.
It records source ID, relative path bytes, file length and SHA-256. A
single fixed JSON encoding defines the payload digest. Tool version and
software timestamps are envelope metadata. The verifier uses only the Python
standard library, requires an explicit working-copy root and does not load
the catalog or producer. An independently retained digest is required to
compare the payload with an external reference.

This artifact makes no claim about device acquisition, custody transfers,
event dates, completeness of the source, or the correctness of an OCR or
model interpretation. A later custody log should reference the payload hash
and record its own events and authority.

Applied skills include deterministic-core, tamper-evident-audit-chain,
claim-provenance-discipline and secure-by-construction.
