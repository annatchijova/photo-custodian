# ADR 0013: Registered removable sources

Register a local block filesystem by Linux UUID and a relative directory.
Treat its current mountpoint as a locator. Neither an empty mount directory
nor a different attached device establishes that files are missing.

The registered scan pins the root directory descriptor, rejects an
unverifiable or nested mount layout, and writes catalog changes in a
single transaction. Recheck the attachment before publishing. On an
observed change, roll back inventory writes and record source unavailability
independently of item status. The handoff reports the last recorded source
state rather than probing any device.

UUID identifies a filesystem for inventory routing. It does not authenticate
a physical device or distinguish cloned filesystem UUIDs. Separate
acquisition and working-copy verification remain necessary for evidentiary
use. Unsupported mount layouts and phone or PC exports can be handled as
ordinary local working-copy sources.

Applied skills include destination-driven-construction, secure-by-construction,
claim-provenance-discipline and recovery-integrity.
