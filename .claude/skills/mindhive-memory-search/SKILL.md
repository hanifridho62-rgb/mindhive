---
name: mindhive-memory-search
description: Use when working on MindHive's memory search, recall, briefing, or context delivery — the pipeline that turns the append-only event ledger into the MEMORY.md briefing Claude auto-loads. Covers recall ranking, semantic vectors, dedup, and context-budget control.
---

# Working on MindHive memory search

MindHive ("hive") captures Claude Code session activity as an **append-only event
ledger** and projects it into a `MEMORY.md` briefing that the next session
auto-loads. The job of "memory search" is to deliver the *most relevant* context
to Claude using the *fewest* tokens. Every change here trades off **recall
quality** against **context budget**.

## The pipeline (read these in order)

1. `events.py` — append-only JSONL ledger. `read_all()` reads **every** event
   each call and sorts by `(ts, file, line)`. Source of truth; never rewrite it.
2. `harvest.py` — turns Claude transcripts into events (`file_touched`,
   `command_run`, memos, `user_prompt`, `assistant_response`). Cursor in
   `harvest_state.json`; cross-process `_harvest_lock`.
3. `projector.py` → `STATE.md`; `briefing.py` → `BRIEFING.md`.
4. `sync.py` — composes native `MEMORY.md` (USAGE + STATE + full BRIEFING) at the
   path the desktop app auto-loads (`native_memory_path`).
5. `recall.py` / `vectors.py` — on-demand search. Keyword (AND-match over LEDGER
   `*.md`) or `--semantic` (chroma + all-MiniLM-L6-v2).
6. `dedupe.py` — display-time, deterministic Jaccard collapse of near-duplicate
   memos. **Built for de-bloating but only wired into some surfaces.**

## Invariants — do not break

- **Ledger is append-only and verbatim.** Dedup/truncation are *display-time*
  only (`dedupe.py` docstring). Never mutate or collapse events on disk.
- **Determinism.** `briefing.py`, `dedupe.py`, `projector.py` must be pure: same
  events in → same bytes out. No clocks, no RNG, no model calls in these paths.
  Tests assert this — keep it.
- **Semantic layer is optional.** `vectors.py` guards imports via `_require_deps`;
  base install must work without `chromadb`/`sentence_transformers`.
- **`guard.ensure_safe(force=)`** gates the embed/semantic paths — preserve it.

## Known hot spots (verify before "fixing", these are the levers)

- **Briefing bloat = the #1 context waste.** `build_briefing` emits the timeline
  ("every entry") and the full caveats/decisions lists with **no dedup and no
  cap**, even though `dedupe.collapse` exists for exactly this. A repeated caveat
  shows up N times verbatim. Wiring `dedupe` + a cap into `briefing.py` is the
  highest-leverage context-budget win. Measure tokens before/after.
- **`recall.py` keyword search has no ranking.** `all(t in low for t in terms)`
  is brittle AND-match; results come back in **filename order**, not relevance;
  no score is returned so callers can't threshold. Snippet hard-capped at 200.
- **`vectors.semantic_recall` discards `distances`.** Without the score, callers
  can't drop low-relevance hits — every query returns `limit` rows regardless of
  quality. `embed()` re-encodes **all** files each run (no incremental embed).
- **`read_all()` is O(events) on every briefing/recall.** 700+ events already;
  this grows unbounded.

## Workflow

1. **Measure first.** Context delivery is the metric. Before changing recall or
   briefing, capture the current `MEMORY.md` size (chars / approx tokens) and a
   few representative `hive recall` outputs. Re-measure after.
2. **Add a failing test** (`tests/test_briefing.py`, `test_recall.py`,
   `test_vectors.py`, `test_dedupe.py`) — determinism and dedup are test-guarded.
3. Keep semantic extras optional; gate any new model use behind `_require_deps`.
4. Run `python -m pytest -q` from the repo root (`conftest.py` sets paths).

## Quick commands

```
python -m pytest -q                 # full suite
python -m pytest tests/test_briefing.py tests/test_recall.py -q
hive recall "<query>"               # keyword
hive recall "<query>" --semantic    # vector (needs .[vectors])
hive sync                           # rebuild MEMORY.md briefing
```
