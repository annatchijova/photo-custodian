# ADR 0016: Independent checks for declared parent bytes

Require an explicit working-copy root and an independently retained SHA-256
of the manifest payload before opening manifest entries. Use the existing
descriptor-root verifier and record a bounded batch in one transaction.
Each observation retains the manifest identity, entry identifiers, expected
digest, outcome and software observation time.

The case handoff may show the latest recorded observation for the hash
declared by a derivative receipt. A match corroborates byte identity for
that working copy and time. Identical content can exist in several copies.
The verifier does not identify an original physical source or establish
custody, event time, authorship, or accuracy of the derived content.

Applied skills include claim-provenance-discipline, deterministic-core,
recovery-integrity and honest-degradation.
