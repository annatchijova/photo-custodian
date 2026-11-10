# ADR 0002: Exact duplicate identity uses complete SHA-256 digests

- Status: Accepted for M1
- Date: 2026-11-09

## Context

Equal names, timestamps, dimensions, sizes, perceptual hashes, and embeddings do
not prove that two files contain identical bytes. Exact duplicate groups may
later inform consequential filesystem operations, so their identity evidence
must be independently recomputable.

The initial real catalog contains 4,042 active files and approximately 1.29 GB
of logical data. Only 45 files, totaling approximately 15 MB, share a file size
with another active file.

## Decision

Use file size only as a candidate prefilter, then stream the complete contents of
every candidate through SHA-256. Cache the digest with the file's size, mtime,
ctime, device, and inode fingerprint. Reuse a cached digest only while that full
fingerprint remains unchanged.

Open files with `O_NOFOLLOW`, verify that the opened descriptor is a regular
file, compare the catalog fingerprint before hashing, and compare the descriptor
fingerprint again after hashing. A mismatch produces a warning and no digest.

Model hardlinks as multiple paths to one physical storage instance. They may
appear in an exact group, but they do not count as independently reclaimable
copies.

M1 reports review candidates only. It has no move, quarantine, Trash, or delete
operation.

## Consequences

Exact groups have a clear claim: every member produced the same complete SHA-256
digest at a fingerprint-stable read. This does not prove that any path is safe to
remove, because redundant copies may be intentional.

The catalog moves from schema v1 to v2 through an atomic migration that adds a
rebuildable `file_hashes` table. Existing sources, scans, and file rows remain
unchanged.

Hashing cost is bounded in memory by an 8 MiB streaming buffer. Work remains
linear in candidate bytes. Group output is capped at 10,000 groups and 1,000
displayed paths per group.

## Rejected alternatives

- Filename and timestamp matching are metadata heuristics, not content identity.
- A quick prefix or suffix hash reduces I/O but cannot support an exactness
  claim.
- Perceptual hashes and embeddings answer visual-similarity questions and remain
  outside exact identity.
- Hashing every unique-size file would establish content addressing for the full
  catalog, but reads more data than M1 needs. This remains a future `verify` or
  archival mode.

## Reopen conditions

Revisit the prefilter when full-catalog content addressing becomes a product
requirement, when a measured corpus shows size-candidate hashing is insufficient
for the desired workflow, or when the storage backend can supply a trusted
content digest that is independently verified.
