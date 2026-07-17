# refactor: Make MIND HIVE recall, namespace resolution, and store hygiene reliably useful for Claude

**Created:** 2026-07-04
**Type:** refactor
**Depth:** Standard
**Target repo:** mindhive (this repo). Runtime store lives at `~/.mindhive/` (`MINDHIVE_HOME`), which is data, not code — this plan changes the code that reads/writes it, never bulk-deletes live data as part of execution.

---

## Summary

An empirical audit of the live store proved the memory system delivers on only one of its two promises. The always-loaded **STATE.md heartbeat works** and carries ~80% of the real value to Claude. The **recall layer — the core of the original concept — does not**: `hive recall "real fee tier maker taker"` returned top hits about "when to use type: hard" and browser-vs-question decisions, zero relevance. Two structural defects sit behind that: naive lexical scoring in `recall.py`, and silent namespace minting in `paths.py` that fragments the store into 24 namespaces (many junk) and sends `hive state` to the wrong project.

This plan makes recall trustworthy, makes namespace selection deterministic and explicit, and adds a **safe, dry-run-by-default hygiene tool** so the 173 MB of graph bloat and orphan namespaces can be pruned deliberately by the user — it does **not** delete live data during execution. The STATE.md heartbeat that already works is preserved untouched.

Guiding principle (from the request): raise real usefulness-to-Claude per unit of complexity. No new runtime dependencies, no new machinery that isn't demonstrably paying off.

---

## Problem Frame

Grounded in reading the implementation:

- **Recall scoring is naive** (`src/mindhive/recall.py:5-30`). Score = count of distinct query terms found as *substrings*, case-insensitive, OR-matched. This has four failure modes, all observed:
  1. **Substring, not word-boundary** — `"taker"` matches `caretaker`, `"real"` matches `really`. False positives.
  2. **No stopword filtering** — common words ("the", "for", "when", "real") each contribute a full point.
  3. **No rarity weighting** — a distinctive term ("recvWindow") scores the same as a generic one ("decision").
  4. **No score threshold** — the weak 1-term tail is returned and printed, indistinguishable from strong hits.
  Frontmatter `tags:` and `type:` exist in every ledger file (`src/mindhive/projector.py:58-59`) but are never used for ranking.
- **Namespace resolution silently mints junk** (`src/mindhive/paths.py:22-57`). `detect_project` falls back to the git-root or cwd basename and returns it unconditionally — so a `hive loop` run in a temp/worktree dir (random name like `brave-brattain-28ae65`) creates a brand-new namespace. Result: 24 namespaces, most junk. `hive state` run from the wrong directory returns the wrong project (mindhive's own placeholder `Active task: x`) instead of the intended one, with no signal that it happened.
- **No store hygiene surface.** 185 MB total, 173 MB in two `graph.json` blobs (`continuous-claude-v3` 116 MB, `__hivemind__` 57 MB). Nothing reports namespace sizes, flags orphans, or prunes safely. Cleanup today means hand-`rm` against live memory.

Not in this plan's frame: the three-overlapping-systems concern (Pensyve + native `.claude` files) is real but lives outside this repo (global Claude config), and the Postgres substrate track (`docs/adr/0006-...`) is a separate large effort. Both are recorded under Scope Boundaries.

---

## Requirements

- **R1** — Recall ranks by relevance a human would agree with: distinctive query terms dominate; stopwords and substring-noise do not surface unrelated docs above related ones. The `"real fee tier maker taker"` query must rank fee/cost/tier ledger entries above governance-config entries (regression fixture).
- **R2** — Recall drops the weak tail: results scoring far below the top hit are not returned, so callers (and Claude) don't treat noise as signal.
- **R3** — Recall relevance improvements require **no new runtime dependencies** and keep the existing return shape (`{"file", "snippet", "score"}`) so `cmd_recall` and `vectors.semantic_recall` stay interchangeable.
- **R4** — Namespace selection is deterministic and inspectable: a user/hook can target a specific project explicitly, and `detect_project` no longer silently creates a namespace for an unknown cwd without a signal.
- **R5** — A user can list known namespaces and see per-namespace size/health, and prune junk/oversized data through a tool that is **dry-run by default** and never deletes without explicit opt-in.
- **R6** — The STATE.md heartbeat behavior (`hive state`, `session_start.ps1`, `projector.build_state`) is unchanged in output shape and remains the always-loaded artifact. No regression.

---

## Key Technical Decisions

- **KTD1 — Improve lexical recall in place; do not default to semantic.** The semantic path (`vectors.semantic_recall`) already exists but needs extras (`chromadb`, `sentence_transformers`) and isn't loaded at session start. The cheapest high-value fix is to make the dependency-free lexical scorer good: word-boundary tokenization + stopword list + rarity (IDF-style) weighting computed over the ledger corpus + frontmatter tag/type boost + a relative score threshold. This is testable, deterministic, and adds zero deps (R3). Semantic stays the opt-in `--semantic` escalation.
- **KTD2 — Rarity weighting is computed per-recall over the LEDGER corpus, not persisted.** Ledger corpora per namespace are small (tens to low hundreds of files); computing document-frequency at query time is cheap and avoids a new index artifact to keep in sync. If profiling later shows cost, caching is a deferred follow-up, not now.
- **KTD3 — Threshold is relative, with an absolute floor.** Drop hits scoring below `max(top_score * RATIO, FLOOR)` rather than a fixed cutoff, so a genuinely-weak-but-best result still surfaces while the long noise tail is cut. Exact constants are tuned against the regression fixture during implementation (deferred detail).
- **KTD4 — Reuse `autosync.json` as the known-projects registry.** A registry of "real" projects already exists (`src/mindhive/autosync.py`, `~/.mindhive/autosync.json`). Namespace-health and the `--project` resolver read from it rather than inventing a second source of truth. `detect_project` keeps its current fallback for backward compatibility but gains a way to distinguish "known" from "freshly minted."
- **KTD5 — Hygiene tool is diagnostic-first and non-destructive by default.** `hive gc` (name TBD in impl) prints a size/health report always; deletion requires an explicit `--prune` flag plus a targeting argument. Execution of THIS plan adds the tool and its tests; it does **not** run `--prune` against the live store. Actual cleanup of the 173 MB is a user action, deferred.
- **KTD6 — `--project` override on read commands.** `state` and `recall` gain an optional `--project` so Claude/hooks can target a namespace regardless of cwd (R4), directly fixing the "wrong project from wrong dir" failure without changing default detection.

---

## Implementation Units

### U1. Relevance-ranked lexical recall

**Goal:** Replace the substring-OR scorer with word-boundary tokenization, stopword filtering, rarity weighting, frontmatter boost, and a relative threshold — so distinctive terms dominate and the noise tail is dropped.
**Requirements:** R1, R2, R3.
**Dependencies:** none.
**Files:**
- `src/mindhive/recall.py` (modify — scoring core)
- `tests/test_recall.py` (modify/extend — new relevance + threshold scenarios, including the audit regression fixture)
**Approach:**
- Tokenize query and documents on word boundaries (`\b\w+\b`), lowercased, so `taker` no longer matches `caretaker`.
- Filter a small built-in English stopword set from query terms before scoring; if the query is *all* stopwords, fall back to current behavior so a degenerate query still returns something.
- Weight each surviving term by rarity across the LEDGER corpus (document-frequency computed at query time per KTD2): a term appearing in few ledger files scores higher than one appearing in many. Sum weighted contributions per document.
- Boost documents whose frontmatter `tags:`/`type:` line contains a query term (parse the `---` header block already written by `projector.build_ledger_entries`).
- Apply the relative-threshold cut (KTD3) after ranking, before `limit`.
- Preserve the return shape `{"file", "snippet", "score"}` and the `sorted()`-stable tie-ordering contract.
**Patterns to follow:** keep the module dependency-free and pure like the current `recall.py`; mirror the frontmatter format emitted in `src/mindhive/projector.py:58-59`.
**Test scenarios:**
- Covers R1. Regression fixture: seed a temp LEDGER with one fee/tier/cost decision doc and several governance/config decision docs; assert `recall("real fee tier maker taker")` ranks the fee/tier doc first.
- Word-boundary: query term `taker` does **not** match a doc whose only hit is `caretaker`.
- Stopword: a query of `"the for when is"` (all stopwords) returns the degenerate fallback rather than ranking every doc equally at full score.
- Rarity weighting: a doc containing a rare query term outranks a doc containing only a common (high-document-frequency) query term, even when the common-term doc repeats it.
- Tag boost: two docs with equal body matches, one with the term in its `tags:` frontmatter, rank the tagged one higher.
- Threshold: given one strong hit and a long tail of single-common-term hits, the tail below `max(top*RATIO, FLOOR)` is not returned.
- Shape/compat: results remain `{"file","snippet","score"}` with bare filenames; `limit` still caps output.
**Verification:** `tests/test_recall.py` passes; running `hive recall "real fee tier maker taker"` against the fabio namespace surfaces fee/cost entries at the top instead of governance-config noise.

### U2. Deterministic, explicit namespace selection

**Goal:** Let read commands target a namespace explicitly, expose which namespaces are "known," and stop `detect_project` from silently minting a junk namespace without a signal.
**Requirements:** R4, R6.
**Dependencies:** none (independent of U1).
**Files:**
- `src/mindhive/paths.py` (modify — add a known-projects check / "was this freshly minted" signal; keep existing fallback behavior)
- `src/mindhive/__main__.py` (modify — add `--project` to the `state` and `recall` subparsers; add a `projects` subcommand listing known namespaces)
- `src/mindhive/autosync.py` (read — reuse the registry per KTD4; extend only if a read helper is missing)
- `tests/test_paths.py` (modify/extend)
- `tests/test_cli.py` (modify/extend — `--project` override, `projects` listing)
**Approach:**
- Add a helper (e.g. `known_projects()`) that reads the registry (`autosync.json`) and returns the set of registered namespace names (KTD4).
- Add an optional `--project NAME` to `cmd_state` and `cmd_recall`; when supplied, resolve directly to that namespace via `paths.project_dir(name)` (both already accept an explicit project), bypassing cwd detection. This is the direct fix for "wrong project from wrong directory" (R4).
- Add `cmd_projects` / `hive projects` that lists known namespaces (from the registry) and flags on-disk namespaces not in the registry as `unregistered` — surfacing the junk without deleting it.
- In `detect_project`, keep the current return value for backward compatibility (do not break existing callers), but make "the resolved namespace is not known and is being created fresh" observable — minimally via the `projects` report; optionally via a stderr note when a read command lands on an unknown namespace. Do not change default STATE.md behavior (R6).
**Patterns to follow:** subparser + `set_defaults(func=...)` dispatch already used throughout `_build_parser`; `paths.project_dir(project)` explicit-project pattern already used by `recall`/`vectors`.
**Test scenarios:**
- `recall("...", project="fabio-hft-v18")` reads the named namespace regardless of cwd (monkeypatch cwd to an unrelated dir; assert the named namespace's ledger is searched).
- `state --project X` prints X's STATE.md even when cwd resolves to a different project.
- `hive projects` lists registered namespaces and marks an on-disk-but-unregistered namespace as `unregistered`.
- `detect_project` still returns the git-root/cwd basename fallback unchanged for a normal repo cwd (no behavioral regression).
- Unknown-namespace read surfaces the "unknown/fresh" signal (report or stderr) without raising.
**Verification:** `tests/test_paths.py` and `tests/test_cli.py` pass; `hive state --project fabio-hft-v18` from any directory prints the fabio state, and `hive projects` shows the 24 on-disk namespaces split into registered vs unregistered.

### U3. Store-hygiene reporting and safe prune tool

**Goal:** Provide a diagnostic-first `hive gc` command that reports per-namespace size/health and flags oversized graph blobs and unregistered/orphan namespaces, with deletion strictly opt-in.
**Requirements:** R5.
**Dependencies:** U2 (reuses `known_projects()` to classify orphans).
**Files:**
- `src/mindhive/hygiene.py` (create — size/health scan + prune logic)
- `src/mindhive/__main__.py` (modify — add `gc` subcommand: report by default, `--prune` + target to delete)
- `tests/test_hygiene.py` (create)
**Approach:**
- Scan `store_home()` for namespace dirs; for each, compute total size, `graph.json` size, ledger/event counts, and registry membership (via U2's `known_projects()`).
- Default invocation prints a sorted report (largest first) and flags: `graph.json` over a size threshold, namespaces not in the registry, and namespaces with no recent events.
- `--prune` requires an explicit target (a namespace name, or a category flag like `--orphans` / `--graph-blobs`) and prints what it will remove; only then deletes. Never prune a registered namespace's event log. No wildcard "delete everything" path.
- Guard against deleting the active/registered store: refuse to prune anything in `known_projects()` unless `--force`.
**Patterns to follow:** the guard/refuse pattern in `src/mindhive/guard.py` (`ensure_safe`) for the safety gate; JSON-emitting command handlers in `__main__.py`.
**Test scenarios:**
- Report lists namespaces largest-first with size + graph-blob size + registry flag.
- Report flags a namespace whose `graph.json` exceeds the threshold.
- `--prune` with no target is refused (exit non-zero, nothing deleted).
- `--prune --orphans` in a temp store deletes only unregistered namespaces and leaves registered ones intact.
- Prune refuses to touch a registered namespace without `--force`.
- Dry-run/report path never mutates the store (assert files unchanged).
**Verification:** `tests/test_hygiene.py` passes; `hive gc` against a temp fixture store prints an accurate report and only deletes under explicit `--prune` with a target. (Running it against the real 185 MB store is a deferred, user-driven action — see Scope Boundaries.)

---

## Scope Boundaries

### In scope
- Recall relevance rewrite (U1), explicit/deterministic namespace selection (U2), and the diagnostic-first hygiene tool (U3), each with tests.

### Deferred to Follow-Up Work
- **Actually pruning the live store** (the 116 MB + 57 MB graph blobs, the ~22 junk namespaces). U3 delivers the safe tool; running `--prune` against `~/.mindhive` is a deliberate user action, not part of this autonomous change.
- **Rarity-weight caching** if query-time document-frequency proves costly (KTD2).
- **Making semantic recall the session-start default** — only worth it if lexical (U1) still underperforms after tuning.

### Out of scope (separate tracks / other repos)
- **Three-overlapping-memory-systems cutover** (disable Pensyve, archive native `.claude` fact files). Lives in global Claude config outside this repo.
- **Postgres canonical event substrate** (`docs/adr/0006-postgres-canonical-event-substrate-staged-cutover.md`) — separate large effort.

---

## Risks & Dependencies

- **Existing recall tests encode the old OR-match behavior.** U1 will likely require updating assertions in `tests/test_recall.py`. Update them to reflect the new relevance contract; do not weaken tests merely to pass — the regression fixture (R1) is the anchor.
- **Threshold tuning could over-cut.** Mitigation: relative threshold with an absolute floor (KTD3) and an explicit "always return the single best hit" guarantee, covered by tests.
- **`detect_project` is called widely.** U2 must preserve its current return contract; the new signal is additive (report/stderr), not a changed return value — regression-tested in `test_paths.py`.
- **Hygiene deletion is irreversible.** Mitigation: dry-run default, explicit target required, registered-namespace guard, `guard.ensure_safe`-style refusal (U3).

---

## Verification Strategy

- Unit tests per unit as enumerated above (`tests/test_recall.py`, `tests/test_paths.py`, `tests/test_cli.py`, `tests/test_hygiene.py`).
- Full suite green: `python -m pytest` (or `hive selftest`).
- Manual smoke against the live store (read-only): `hive recall "real fee tier maker taker" --project fabio-hft-v18` ranks fee/cost entries first; `hive state --project fabio-hft-v18` works from any cwd; `hive projects` and `hive gc` (report only) produce accurate output. STATE.md at session start is unchanged (R6).
