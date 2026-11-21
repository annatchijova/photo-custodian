# ADR 0004: Local media review and optional image categorization

Photo Custodian adds an offline HTML review surface over its SQLite catalog.
Python keeps the existing CLI, migrations and exact matcher in one runtime.
Pillow is an optional dependency for bounded local image decoding. Browser
JavaScript exports and restores group-scoped review notes. A hosted frontend
would add a service boundary for private paths and previews. A native matcher
remains a candidate if measured catalog runs miss the operating budget.

Visual matching uses versioned difference hashes, coarse color and aspect
ratio, and compares each candidate with a fixed group anchor. Five bands
cover a maximum Hamming distance of four. Integer thresholds and stable file
IDs make grouping reproducible for a fixed catalog and decoder. Bounded
comparisons and decode exclusions appear in coverage. These groups are
suggestions and do not measure the probability that two files share an origin.

The report opens without network access. Explicit photo IDs authorize up to ten
separate OpenAI requests containing resized JPEGs and no source metadata or
paths. The adapter accepts a fixed taxonomy and stores suggestions with the
catalog fingerprint and model. The model has no filesystem tools and does not
influence duplicate groups or review decisions.

Applied skills include software-archaeology, language-selection,
non-overwhelming-ux, frontend-accessibility-by-construction,
agent-trust-boundaries, llm-out-of-the-loop, honest-degradation,
surgical-patcher, git-discipline, and abductive-engineering.
