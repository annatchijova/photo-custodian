# Photo Custodian Product Requirements

## Problem

Photo and video libraries spread across Linux computers, mounted disks, and
phone imports accumulate exact copies, visual variants, large files, and related
assets. Existing cleanup workflows often mix detection with deletion and do not
preserve enough evidence to reverse a bad decision.

## Product objective

Build a local-first Linux application that inventories media collections once,
supports multiple analysis categories over a shared catalog, and keeps every
filesystem mutation behind an explicit review and reversible quarantine step.

## Initial users

The first release targets a single technical user managing personal collections
on multiple Linux computers. Multi-user authorization and hosted operation are
outside the first release.

## Capability categories

1. Inventory files incrementally without modifying source libraries.
2. Detect exact duplicates using complete cryptographic content digests.
3. Report large files by type, source, path, and age.
4. Identify visually similar photos as review candidates.
5. Inventory videos and later compare representative frames.
6. Propose organization plans without applying them during analysis.
7. Review decisions and apply them through reversible quarantine manifests.

## Milestone M0 acceptance criteria

- A CLI accepts one or more local directory roots.
- Source roots are validated before traversal.
- Symlinks and non-regular files are never followed or indexed as regular media.
- A versioned SQLite catalog stores file identity, path, stat fingerprint, type,
  and scan state.
- Repeating a scan distinguishes unchanged files from new or changed files.
- Files absent from a completed scan are marked missing rather than deleted from
  the catalog.
- A summary reports counts and logical byte totals by media category.
- The scanner performs no writes in the source roots.
- Errors produce explicit WARN/FAIL information rather than a false complete
  result.

## Non-functional requirements

- Linux is the primary platform.
- The scanner must be resumable and bounded in memory.
- Runtime network access is not required.
- The initial runtime uses only the Python standard library.
- Catalog schema changes are explicit, versioned migrations.
- Filesystem mutations are absent from M0 and M1.

## Evidence required for M0

- Boundary tests for invalid roots and symlinks.
- Behavioral tests for new, unchanged, changed, and missing files.
- Tests that assert the schema version and inventory totals.
- A negative control demonstrating that weakening change detection makes a test
  fail.
- A manual smoke scan against a disposable fixture directory.

## Delivery status and later milestone gates

M0 inventory and M1 exact duplicate detection are implemented. M1 uses complete
SHA-256 over size-collision candidates and has traversal-order, hardlink, cache,
stale-file, and schema-migration tests.

Quarantine cannot ship until restore is exercised on same-filesystem and
cross-filesystem fixtures. Similarity cannot ship with a default threshold until
evaluated against a labeled local corpus.
