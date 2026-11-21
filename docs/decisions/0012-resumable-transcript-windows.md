# ADR 0012: Resumable transcript windows

The existing inline analyzer uses a short audio sample. The explicit
transcription queue reads catalogued working copies and stages searchable
derivatives in a separate private directory. It uses installed FFmpeg,
FFprobe, Whisper, and local weights. It does not invoke hosted inference
or play audio.

Bind each output to one source, the raw media hash and observed stat
fingerprint, the weights hash, the first audio track and fixed window size.
Use the declared container duration to schedule bounded windows. Publish
each transcript and receipt together. Compare completed text bytes with
their receipts on resume. Preserve prior windows if a later operation fails.

The receipt distinguishes requested time, decoded frame count and whether
Whisper produced timed speech. The transcript uses offsets within decoded
PCM. Requested time and declared duration do not attest actual source
coverage, precise event time, speaker identity or transcript accuracy.

Applied skills include claim-provenance-discipline, recovery-integrity,
destination-driven-construction and secure-by-construction.
