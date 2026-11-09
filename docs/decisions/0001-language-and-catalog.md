# ADR 0001: Python orchestration with a SQLite catalog

- Status: Accepted for M0
- Date: 2026-11-09

## Context

The first milestone needs safe filesystem traversal, incremental metadata
inventory, a local catalog, and a CLI. No representative benchmark currently
shows that native hashing or traversal performance is the limiting constraint.

## Decision

Use Python 3.11 or newer for orchestration and SQLite for the local catalog.
Keep the runtime dependency-free during M0. Use explicit schema migrations and
standard-library tests.

## Consequences

The first implementation is easy to inspect and can evaluate the data model
before introducing image or model dependencies. Python does not itself provide
determinism, path safety, or bounded memory; the implementation and tests must
establish those properties.

SQLite serializes catalog writes. That is acceptable for the initial single-user
scanner and makes scan completion and missing-file marking transactional.

## Rejected alternatives

- Rust offers native performance, memory safety, and stronger static modeling.
  It remains the preferred candidate for an isolated scanner if representative
  measurements show Python cannot meet the agreed scan budget.
- Go offers simple distribution and concurrency, but provides less leverage for
  later image and ML experiments.
- A Python/Rust split is rejected until a measured bottleneck justifies the
  additional process or FFI boundary.

## Reopen conditions

Reconsider this decision if a representative one-terabyte corpus exceeds an
agreed scan-time or memory budget, if image parsing requires a safer isolated
runtime, or if multi-host concurrency invalidates the single-writer catalog
assumption.
