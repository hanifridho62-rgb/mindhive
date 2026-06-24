# 1. LLM-first graph structuring (Claude Opus 4.8 as the structuring engine)

Date: 2026-06-24
Status: Accepted

## Context

We are merging a **code graph** (functions/classes/modules + structural edges)
into MIND HIVE's existing **memory graph** (events/decisions/files/tags), so the
hive mind retrieves intent *attached to the specific code it concerns* (see
`CONTEXT.md` → "Merge").

MIND HIVE's documented ethos is "zero API tokens, offline, deterministic;
append events, deterministic projections rebuild everything." Graphify — the
inspiration — structures graphs with two phases: deterministic tree-sitter AST
extraction, and LLM semantic extraction.

The owner's position: to produce a genuinely *structured* graph (not just
literal AST adjacency), the **LLM must be the main brain** — specifically Claude
Opus 4.8 — and it should run as a first-class part of the build, not an opt-in
afterthought. Two prior framings (AST-default/LLM-opt-in; bounded-but-gated)
were considered and rejected because they relegate the LLM to enrichment rather
than making it the structuring engine.

## Decision

The graph's **structure is produced by an LLM (Claude Opus 4.8) as the primary
engine**, run as part of the graph build. LLM-derived nodes and edges are
first-class. This supersedes the "deterministic projection" guarantee **for the
`graph.json` artifact specifically** — and only for it.

## Consequences

**Verified to be safe:**
- `hive verify` hashes only the **event ledger** (`chain.py:compute_chain` over
  `events.read_all`), never `graph.json`. The append-only source of truth and
  its hash chain are **untouched** by this decision. Tamper-evidence is intact.
- `graph.json` was always a rebuildable, lossy view (`graph.build()` writes it
  atomically and any caller rebuilds on demand). Losing bit-reproducibility of a
  cache costs nothing structural.

**Real costs accepted:**
- `graph.json` is no longer bit-for-bit reproducible: rebuild yields different
  `INFERRED` edges. Any test asserting exact graph contents must move to
  asserting invariants (node presence, edge classes), not exact equality.
- Each full build spends tokens. Wiring it into automatic hooks would spend
  tokens every session — see mitigation.
- **LIVE_LOCK / trading-box risk — RESOLVED.** The heavy Opus layer is built
  **only on explicit human command** (`hive graph build --llm`), never in
  `session_start`/autosync hooks and never stale-auto-refreshed. It refuses under
  `LIVE_LOCK` by default (with a `--force` override, mirroring `embed`). The
  cheap deterministic EXTRACTED layer (AST + file links) is the only thing that
  refreshes automatically, and it costs no tokens. So an LLM build can only load
  the box when the owner deliberately starts one off the trading window.

## Alternatives considered

- **AST default, LLM opt-in** (mirrors existing `--semantic`/`semantic_near`):
  rejected — relegates the LLM to enrichment.
- **AST only, no LLM:** rejected — not "structured" enough for the goal.
