# ADR 0003: Inventory advice through the Responses API

Use the existing Python CLI and SQLite aggregate query for an optional,
single-turn advisory command. The network call sends three kinds of counts
and logical byte totals. A native adapter in Go or Rust would introduce a
second runtime boundary without addressing network latency. The standard
library is sufficient for this one HTTPS endpoint. Revisit the transport
choice if the workflow needs streaming, multiple endpoints, or tool dispatch.

The local next step comes from the inventory before any model call. Model
output is supplementary text, with no tools or authority to modify files or
record decisions. The local result remains available if the API fails.

Applied skills include language-selection, software-archaeology,
non-overwhelming-ux, OpenAI Docs, agent-trust-boundaries, llm-out-of-the-loop,
honest-degradation, surgical-patcher, git-discipline, and abductive-engineering.
