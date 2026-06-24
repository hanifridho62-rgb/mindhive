# 2. Per-edge provenance tags (EXTRACTED / INFERRED / AMBIGUOUS)

Date: 2026-06-24
Status: Accepted

## Context

ADR 0001 makes Opus the graph's structuring engine, which makes the graph
non-deterministic. But the tiered-retrieval design (see
`docs/adr/0003-tiered-context-retrieval.md` once written) requires the AI to
trust the graph for **deterministic structural facts** when making refactoring
decisions — e.g. "does A actually call B?". A purely LLM-inferred graph cannot
be trusted for that: a hallucinated `calls` edge is a refactoring hazard.

Graphify's confidence-tag model resolves this without splitting the graph.

## Decision

Every edge carries a `source` (provenance) tag:

- **EXTRACTED** — deterministic. Tree-sitter AST facts (`calls`, `imports`,
  `defines`) and event-derived links (`touched`, `in_session`, `tagged`,
  `next`). Reproducible; safe to treat as ground truth.
- **INFERRED** — Opus's conceptual links (e.g. "this decision concerns that
  function," "these two modules share a concern"). Soft; useful for recall, not
  authoritative for structural decisions.
- **AMBIGUOUS** — Opus flagged the link as plausible but uncertain.

One graph, provenance-aware. `path` queries can filter to `EXTRACTED`-only for
deterministic structural answers; `subgraph` recall includes `INFERRED` for a
richer relevant neighborhood.

## Consequences

- The deterministic spine remains verifiable independent of Opus: an
  `EXTRACTED`-only rebuild *is* reproducible, even though the full graph is not.
- Retrieval tools must expose a provenance filter (at minimum EXTRACTED-only vs
  all).
- Node dedup (graphify-style) must merge an AST-derived symbol node and an
  Opus-mentioned symbol node into one, preserving the union of edges.

## Alternatives considered

- **Two physically separate graphs:** rejected — clean boundary, but 1-hop
  neighbor walks across the structural/conceptual layers get awkward.
- **Trust the LLM graph wholesale:** rejected — makes deterministic structural
  facts impossible; unsafe for refactoring.
