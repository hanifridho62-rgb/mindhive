# 3. Tiered context retrieval (briefing → MCP engine → subgraph/path tools)

Date: 2026-06-24
Status: Accepted

## Context

The stated goal is to increase the efficiency of Claude's context windows. Today
context-loading is **push/dump-based**: `session_start` injects the full
briefing/STATE.md and `hive recall` greps flat events, filling context whether or
not it's relevant. Graphify's real lever is **query-first** retrieval: pull a
small relevant subgraph instead of pushing everything.

## Decision

Adopt a three-tier model, separating macro-awareness from micro-analysis:

1. **Macro — cached distilled briefing (push, cheap).** A high-signal summary of
   project state/architecture, written by Opus **during the human-triggered
   `hive graph build --llm`** and cached to a file. `session_start` injects the
   cached text — zero tokens, offline, instant. As fresh as the last manual
   build (see ADR 0001 for why the build is human-triggered only). The same
   build also writes a `GRAPH_REPORT.md` (hub/"god" nodes, surprising INFERRED
   connections, suggested queries); its highlights feed the distilled briefing.

2. **Engine — query-first MCP server (pull).** The merged graph is exposed via a
   stdio MCP server (`hive mcp`) that Claude registers and calls as native tools,
   rather than injecting raw graph data into the prompt. A pure query core backs
   it; the dashboard reuses the same core. A directive nudges "query the graph
   before grep/recall."

3. **Micro — subgraph & path tools (pull, transient).**
   - `subgraph(query, hops=1, provenance="all")` — the matching nodes + their
     linked code entities + 1-hop neighbors. Includes INFERRED edges for
     richness. Used to load just the function-under-edit and its neighborhood.
   - `path(a, b, provenance="extracted")` — shortest connection between two
     concepts, defaulting to the deterministic EXTRACTED spine so structural
     answers are trustworthy (see ADR 0002).

## Consequences

- Claude's persistent context holds only the distilled briefing; heavy code and
  deep relationships load transiently, on request.
- Structural decisions rest on EXTRACTED graph data, not on summary inference.
- The briefing can be stale relative to live code — accepted, same trade as the
  graph itself.
- Requires the directive/AGENTS.md nudge to actually change Claude's behavior
  from "grep first" to "query first"; without it the tools go unused.

## Alternatives considered

- **Live Opus briefing at session_start:** rejected — automatic Opus-in-hook
  token spend, fires under LIVE_LOCK (contradicts ADR 0001).
- **Keep STATE.md only, no distillation:** viable fallback, but loses the
  high-signal macro map.
