# ADR 0010: Fracture intake boundary

Reviewed against local Fracture revision `ef192c86`. Fracture routes some
individual technical files by extension and routes mobile or Takeout
directories by marker names. A candidate route is a dispatch observation,
not proof that the material is intact or within the parser's scope.

Fracture's JSON scoring adapter consumes pre-scored artifacts. A Photo
Custodian handoff contains catalog observations, byte hashes and optional
advisory leads. It does not supply scoring fields. The handoff must stay
outside the directory passed to Fracture as evidence. Never pass it as the
JSON scoring input. Gmail EML, WhatsApp exports, OCR and Whisper text need
an appropriate source-specific extractor or examiner mapping before they
can be used as forensic signals.

The export binds an opaque case reference to an independently supplied
manifest payload digest. The same manifest can support distinct case
references. A proposed parser never causes automatic execution. Reverify
working-copy bytes separately before examination. Findings and analysis
outcomes are recorded downstream by Fracture.

Relevant source boundaries are `vigia_agent.py` at
`_build_orchestrator_kwargs`, the JSON adapter in `sift_orchestrator.py`,
and `validate_case_schema` in `vigia/pipeline/vigia_integration_bridge.py`.

Applied skills include claim-provenance-discipline, deterministic-core,
live-contract-compatibility and llm-out-of-the-loop.
