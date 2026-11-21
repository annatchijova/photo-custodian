# ADR 0011: Mail attachment derivatives

Stage decoded MIME parts under generated filenames in a private, source-bound
working directory. Each completed message receipt records the raw EML hash,
the observed file fingerprint, MIME traversal indices and decoded part hashes.
Declared MIME types and filenames remain source assertions. Embedded messages,
parse defects and limit exits are recorded as partial coverage where applicable.

Publish a message directory after its receipt is written. The temporary
directory is a sibling of the scan root so interrupted output stays outside
the analysis queue. Reuse requires the same observed EML fingerprint.
A changed source requires a separate staging revision. Resume does not
attest that stored derivatives remained unchanged. The manifest command can
independently hash staged bytes for a later handoff.

Applied skills include claim-provenance-discipline, recovery-integrity and
secure-by-construction.
