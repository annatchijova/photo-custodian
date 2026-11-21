# ADR 0006: Grounded archive queries with optional inference

Adapt the retrieval and narration boundary from Continuum at revision
0557c5f. Query existing extraction records read-only. Return catalog
identities, extraction scope, excerpt offsets and the digest of the
extracted text. These identify the stored derivative. Source acquisition,
original-byte digests and custody records belong to a separate workflow.

The query output omits source paths, pictures, excerpts and model
interpretations by default. An explicit provider and content display switch
enable optional interpretation. Only the question, bounded excerpts and
query-local reference IDs go to the provider. No provider can modify the
catalog or run tools. Citation IDs must resolve to retrieved excerpts.
The local results survive provider failure. Literal matches over stored
extractions are retrieval leads with limited coverage. File modification
time is a catalog observation, not an inferred event date.

OpenAI uses the existing Responses transport. Ollama uses the loopback
chat API without redirects or environment proxies. Case processing with
Ollama requires a server with cloud disabled and a preinstalled compatible
model. Model selection never falls back to another provider.

The duplicate report omits embedded pictures unless preview generation is
explicitly requested. OCR retrieval ideas from Gyotaku and page provenance
from Docling inform future work on indexed search and page-anchored results.
No external extraction or indexing package is required by this change.

Applied skills include claim-provenance-discipline, agent-trust-boundaries,
llm-out-of-the-loop, honest-degradation and non-overwhelming-ux.
