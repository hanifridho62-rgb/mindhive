# Full-text memory + deterministic meaning-based dedup

**Date:** 2026-06-23
**Project:** mindhive
**Status:** approved

## Problem

Harvested memories are truncated to 300 characters at storage time
([`harvest.py`](../../../src/mindhive/harvest.py) `val[:300]`), so longer
memos are cut off mid-sentence in the dashboard Memory view. Separately, the
same idea written across multiple sessions produces redundant, near-identical
memory entries with no deduplication.

The user wants: (1) no truncation — store the full memory text; (2) when a new
memory is similar **in meaning** to an existing one, don't show a redundant
repeat — keep the newer wording. The guiding concept is "a computing language
that focuses on function and meaning."

## Constraints

- **Sole AI = Opus 4.8.** No second AI source. The existing `semantic_recall`
  uses `sentence_transformers` + `chromadb`; it must NOT be used here. Meaning
  similarity must be **pure-Python and fully deterministic** (same input →
  same output, every run).
- **Append-only ledger.** Events are append-only JSONL, one file per session
  ([`events.py`](../../../src/mindhive/events.py) `append_event`). The design
  must not mutate or delete ledger rows.

## Decisions (from brainstorming)

| Question | Decision |
|----------|----------|
| Where does dedup happen? | **Read time** — store everything (full text), collapse near-duplicates only when rendering the Memory view. Fully reversible. |
| Merge aggressiveness | **Conservative** — collapse only when memos share ~85%+ of their meaningful words (essentially re-phrasings). |
| Match boundaries | **Same `type` AND same `project`** — a reworded caveat never absorbs a similarly-worded decision; projects stay isolated in the HIVE view. |

## Architecture

### 1. Storage — remove the memo cap
[`harvest.py`](../../../src/mindhive/harvest.py): `val[:300]` → full `val`.
Affects new harvests only; already-truncated rows cannot be recovered. The
shell-command cap (`cmd[:180]`) is intentionally left in place — commands are
not semantic memory and uncapped commands are noisy.

### 2. New module `src/mindhive/dedupe.py` (pure, deterministic)

- `normalize(text) -> frozenset[str]` — lowercase, strip punctuation, collapse
  whitespace, drop a small fixed stopword set; return the token **set**.
- `similar(a, b, threshold=0.85) -> bool` — token-set **Jaccard**
  (`|a∩b| / |a∪b|`) `>= threshold`. Order-independent, so reworded sentences
  still match. If both token sets are empty → not similar (blanks never
  collapse together); fall back to exact-string equality for that case.
- `collapse(items, threshold=0.85) -> list` — input is a list of dicts each
  carrying at least `type`, `project`, `text`, `ts`, assumed **newest-first**.
  Walk the list; each item joins an existing kept representative iff it is
  `similar` to it AND shares the same `(type, project)`; otherwise it becomes a
  new representative. The **newest** item is always the representative (first
  seen wins, because input is newest-first). Returns representatives in input
  order, each with an added `similar_count` = number of older near-duplicates
  absorbed.

### 3. Wire into the view (read-time, reversible)
[`dashboard.py`](../../../src/mindhive/dashboard.py) `/api/memory`: after
building the semantic list (already newest-first), pass it through
`dedupe.collapse(...)` before returning JSON. The raw ledger is untouched, so
raising the threshold or removing the call restores the full list.

### 4. Frontend hint
In the Memory list item, when `similar_count > 0`, render a quiet marker such
as `· +2 similar`. The client-side search/sort already added to the Memory
view operate on the collapsed set unchanged.

## Data flow

```
harvest (full text)
  → events ledger (append-only, complete history preserved)
  → /api/memory reads semantic events, newest-first
  → dedupe.collapse() by (type, project) @ 0.85
  → frontend renders representatives + "+N similar", with live search/sort
```

## Error handling / edge cases

- Empty or whitespace-only memo → empty token set → never merges with others.
- `collapse` is total and pure: never raises on well-formed item dicts; items
  missing `text` are treated as empty text.
- Determinism: no model, no randomness, no time-dependent branching inside
  `dedupe`. The representative-ordering relies solely on the caller's
  newest-first input.

## Testing

Unit tests (`tests/test_dedupe.py`):
- `normalize` lowercases, strips punctuation, drops stopwords.
- `similar` true just above 0.85, false just below; empty-vs-empty handling.
- `collapse` keeps newest as representative; isolates by `type`; isolates by
  `project`; sets `similar_count` correctly; preserves full (untruncated) text.

Dashboard test (extend `tests/test_dashboard.py`):
- `/api/memory` collapses a known near-duplicate pair into one representative
  with `similar_count == 1`, and keeps two genuinely distinct memos separate.

Harvest test (extend `tests/test_*`):
- A harvested memo longer than 300 chars is stored in full (no truncation).

## Reversibility

All dedup is display-time. The ledger keeps every memory verbatim. Disabling
the feature = remove the single `collapse()` call (or set threshold to 1.0+).
