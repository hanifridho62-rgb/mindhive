# Plugin Catalog & Evaluation — Design

**Date:** 2026-06-22
**Status:** Approved (Approach A)

## Goal

A reusable MIND HIVE feature that scans the installed Claude Code skills/plugins,
keeps only the ones usable **without API keys or paying**, ingests them into the
knowledge graph as nodes with their triggers, scores them by breadth, and records
a top-5 recommendation (plus curated "Claude's recommendations" notes) into the
event store as evaluation material.

Everything stays consistent with MIND HIVE's rules: deterministic, event-sourced,
zero API tokens at runtime, graph is a projection of events (nothing hand-edited).

## Decisions (from brainstorming)

1. **Deliverable:** reusable tool feature (new `hive plugins` command), not a one-off report.
2. **Discovery source:** scan the plugin cache on disk (`~/.claude/plugins/cache/`).
3. **Scoring basis:** static **breadth** (trigger count + topic spread), independent of user history.
4. **"Claude's recommendations":** curated notes I author at build time, stored as data, printed at runtime (no live LLM call).
5. **DB integration:** **one `plugin_catalog` snapshot event per scan**; graph projector expands the latest snapshot into nodes.

## Architecture (Approach A)

New module `src/mindhive/plugins.py` owns scan + filter + score + recommend.
`graph.py` becomes a thin consumer that expands the latest `plugin_catalog` event.
This mirrors the existing module-per-concern layout (`recall.py`, `export.py`, `harvest.py`).

### `plugins.py`

- `scan(cache_dir=None) -> list[dict]`
  Walks `<cache>/<marketplace>/<plugin>/<version>/`. Per plugin reads
  `.claude-plugin/plugin.json` (name, version, description, `configuration.required`,
  `mcpServers`/`mcp`, `dependencies`) and globs `skills/*/SKILL.md`, parsing YAML
  frontmatter into `{name, trigger}` (the `description:` line is the trigger).
  - Keeps the highest semver per `<marketplace>/<plugin>`.
  - Skips `temp_local_*` scratch dirs and `.codex-plugin` / `.cursor-plugin` mirrors.
  - Tolerant: a plugin with no `skills/` dir yields an empty skill list; a malformed
    `plugin.json` or `SKILL.md` is skipped, not fatal.

- `is_free(plugin) -> (bool, reason)`
  **Not free** if any of:
  - a `configuration.required` key matches `*_API_KEY|*_TOKEN|*_SECRET|*_KEY`;
  - the plugin declares a non-empty `mcpServers` block (account/auth-gated MCP);
  - `dependencies` names a known paid-API SDK (`openai`, `anthropic`, `cohere`, `mistralai`).
  Otherwise **free**. `reason` explains exclusions for the output.

- `score(plugin) -> dict`  — deterministic static breadth:
  - `trigger_count` = number of skills.
  - `topic_spread` = distinct topic keywords matched across skill descriptions
    (curated `TOPIC_KEYWORDS` set: testing, security, frontend, backend, data, deploy,
    debug, docs, perf, infra, api, mobile, ml, review, …).
  - `breadth_score = trigger_count + topic_spread * BREADTH_WEIGHT` (named const, default 1.0).
  - Ties broken alphabetically by name → stable, replayable ranking.

- `recommend(plugins, top_n=5) -> dict`
  Filters to free, sorts by `(breadth_score desc, name asc)`, takes top N, attaches
  `CURATED_NOTES[name]` (default `""`). Returns the full evaluation payload (below).

- `CURATED_NOTES: dict[str, str]` — module-level, authored at build time.

### Snapshot event payload (`plugin_catalog`)

```
{
  "scanned_at": "<iso>",
  "cache_dir": "<path>",
  "counts": {"plugins_total": N, "free": F, "paid": P, "skills_total": S},
  "free_plugins":   [ <plugin dict incl. score + skills> ],
  "paid_plugins":   [ {name, version, free_reason} ],
  "recommendations":[ {name, breadth_score, note} ]   # top 5
}
```

`plugin` dict shape:
```
{ marketplace, name, version, description, free, free_reason,
  skills: [{name, trigger}],
  score: {trigger_count, topic_spread, breadth_score} }
```

### Event type

Add `plugin_catalog` to `events.DETERMINISTIC_TYPES` so `append_event` accepts it.
It is **not** a `LEDGER_TYPE` and not handled by `build_state`, so STATE.md and the
LEDGER are unaffected (no pollution of the human-facing memory).

### Graph projection (`graph.py`)

- Generic per-event node creation **skips** `plugin_catalog` events (they would be noise).
- After the existing build, find the **latest** `plugin_catalog` event and expand it:
  - `plugin:<marketplace>/<name>` nodes (type `plugin`), label = name, with `score`,
    `version`, `free` extra fields. Free plugins only.
  - `skill:<plugin>/<skill>` nodes (type `skill`), label = skill name, `text` = trigger.
  - Edge `plugin -> skill` rel `provides`.
  - One `recommendation:<scanned_at>` node (type `recommendation`) summarizing the top 5;
    edge `recommendation -> plugin` rel `recommends` for each of the 5.
  - Edges between a plugin and a `topic:<kw>` node (type `topic`) rel `covers`, so the
    graph visibly clusters plugins by topic.
- Expansion is pure and deterministic given the snapshot; replays identically.

### Rendering (`graphview.py`)

Add `TYPE_COLORS` / `KIND_LABELS` entries for `plugin`, `skill`, `topic`,
`recommendation`. The legend auto-populates from node types present, so no other
view changes are needed.

### CLI (`__main__.py`)

New `hive plugins` group:
- `hive plugins scan [--cache DIR]` → scan, score, recommend, append one
  `plugin_catalog` event, print a summary (counts + top 5 with notes).
- `hive plugins recommend` → read the latest `plugin_catalog` event and print the
  top-5 recommendations + curated notes (no rescan; zero work if none exists → hint
  to run scan).

`hive graph build` automatically picks up the latest snapshot (no flag needed).

## Testing

`tests/test_plugins.py` builds a **fake cache** under `tmp_path`:
- a free plugin with 3 skills across 2 topics,
- a paid plugin with `configuration.required: {OPENAI_API_KEY: ...}`,
- an MCP plugin with an `mcpServers` block,
- highest-semver dedupe (two versions of one plugin),
- a malformed `plugin.json` (must be skipped, not crash).

Assertions: `scan` finds plugins + skills/triggers; `is_free` classifies correctly
with reasons; `score` is deterministic and orders as expected; `recommend` returns ≤5,
sorted, free-only, with notes attached.

`tests/test_graph.py` (extend): a `plugin_catalog` event expands into `plugin`/`skill`/
`recommendation`/`topic` nodes and `provides`/`recommends`/`covers` edges; a second
snapshot supersedes the first (only the latest expands); `plugin_catalog` produces no
generic `event:` node.

`tests/test_cli_new.py` (extend): `hive plugins scan` on a fake cache appends exactly
one event and prints the top 5; `hive plugins recommend` reads it back.

All wired into the existing `hive selftest` (pytest) run.

## Out of scope (YAGNI)

- Live LLM commentary / `--explain` flag.
- Usage instrumentation (tracking real skill firings over time).
- Relevance-to-history scoring.
- Editing or installing plugins — this feature only reads and evaluates.
