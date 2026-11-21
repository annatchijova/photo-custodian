# ADR 0015: Checked derivative lineage in case handoff

Bind each lineage row to a catalog file ID and current stat fingerprint.
Read the generated staging layout under a directory descriptor without
following symlinks. Validate bounded receipts, reject duplicate fields,
check locator consistency, and hash the derivative bytes against the receipt.

Automatic analysis can record lineage for staged mail attachments and
transcripts. An independent bounded catalog pass backfills existing rows.
The parent hash remains a declaration from a local receipt. It represents
neither independent verification of the EML or recording nor authentication
of the original device. The JSON handoff reports the last catalogued check
and uses a fixed metadata-only projection. The HTML shows only locator
summaries. Earlier catalogs remain readable with an unlinked status.

Applied skills include claim-provenance-discipline, recovery-integrity,
secure-by-construction and honest-degradation.
