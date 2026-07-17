---
title: "feat: Mandatory code change-log with dashboard Log section"
date: 2026-07-15
type: feat
depth: standard
origin: (none — solo invocation; scope confirmed interactively)
---

# feat: Mandatory code change-log with dashboard Log section

## Summary

Add an event-sourced change-log that records every change to mindhive's own code — categorized as update, minor change, deletion, reduction, major change, or replacement — with a brief, required, human-written explanation. Entries are `code_change` events in the existing ledger, surfaced in a new **Log** section of the dashboard. Logging is mandatory via the same enforcement pattern as the thermo-nuclear review gate: touched source files are detected at session end, and an "unlogged code changes" warning blocks the briefing/STATE.md until `hive log <category> "<explanation>"` is run.

**User decisions (confirmed):** enforcement = briefing gate (not a git hook); granularity = one entry per change-set (per logical piece of work), files auto-detected.

---

## Problem Frame

Mindhive's code evolves through many sessions and PRs, but there is no single, queryable record of *what changed and why* at the code level. Git history exists but is not surfaced in the hive, carries no category taxonomy, and nothing forces a human-readable explanation at change time. The user wants a mandatory procedure: touching mindhive code must produce a brief categorized explanation, visible in the dashboard.

## Requirements

- R1. A change-log entry has: category (one of `update`, `minor_change`, `deletion`, `reduction`, `major_change`, `replacement`), a brief non-empty explanation, the affected files, timestamp, and session id.
- R2. Entries are events in the existing event store (new type `code_change`), so they inherit ledger, chain, export, and Memory-view machinery.
- R3. Recording is a CLI action: `hive log <category> "<explanation>"` with files auto-detected (explicit `--files` override allowed).
- R4. Mandatory enforcement: when in-scope source files were touched but no covering `code_change` entry exists, a blocking notice appears in STATE.md/briefing (same mechanism as `pending_review.json`), instructing how to log. Recording clears it.
- R5. The dashboard gains a **Log** nav section listing entries newest-first with category badge, explanation, files, and pending-unlogged banner.
- R6. The procedure is documented as standing policy in CLAUDE.md and README so agents and the user always follow it.

---

## Key Technical Decisions

- **KTD1 — `code_change` is a SEMANTIC type.** It is human-authored intent (like `decision`/`caveat`), so it joins `SEMANTIC_TYPES` and therefore the LEDGER, hash chain, and Memory view for free. Rationale: the change-log *is* an audit trail; excluding it from the chain would undercut that.
- **KTD2 — Mirror `review.py` wholesale for the gate.** New module `changelog.py` reuses the proven shape: `in_scope()` filter (delegate to `review.in_scope`), `touched_files()` from `file_touched` events, a sidecar `pending_changelog.json` marker under the project dir, `queue()/load_pending()/clear()/notice()`. The projector renders the notice into STATE.md right after the review notice. Rationale: identical lifecycle, identical failure modes already solved (atomic tmp-replace write, stale-marker cleanup).
- **KTD3 — Per-change-set coverage = per-session detection.** The gate is computed from the session's `file_touched` events at `session_end` (like review). `hive log` records one entry for the whole change-set and clears the marker. Files default to the pending marker's file list when present, else the current session's touched files, else `git diff --name-only` against the merge-base with master as a fallback for manual use.
- **KTD4 — Explanation is validated, not just required.** Empty/whitespace or < 10 characters is rejected with a clear error; category must be one of the six. Mandatory means the cheap path is a real sentence, not `"x"`. (Length floor is a tripwire, not a quality guarantee.)

## Scope Boundaries

- **In scope:** event type, changelog module + gate, CLI, projector notice, session_end wiring, dashboard Log section, docs.
- **Not in scope (true non-goals):** git pre-commit hook enforcement (user chose briefing gate); logging changes to *other* projects' code (in-scope filter stays `src/mindhive/` + `tests/`); retroactive backfill of historical changes.
- **Deferred to follow-up work:** auto-drafting explanations from git diffs; linking entries to PR numbers automatically.

---

## Implementation Units

### U1. `changelog.py` — entry recording and pending gate

**Goal:** Core module: categories, record, gate lifecycle, notice rendering.
**Requirements:** R1, R2 (record side), R4.
**Dependencies:** none.
**Files:** `src/mindhive/changelog.py`, `tests/test_changelog.py`, `src/mindhive/events.py` (add `code_change` to `SEMANTIC_TYPES`).
**Approach:**
- `CATEGORIES = {"update", "minor_change", "deletion", "reduction", "major_change", "replacement"}`.
- `record(category, text, files=None, session_id=None, project=None)` → validates category + explanation (KTD4), resolves files (KTD3 fallback order), appends a `code_change` event with payload `{"text", "category", "files"}`, clears the pending marker, returns the event.
- `queue(evs, session_id, project)` / `load_pending()` / `clear()` / `notice(marker)` mirroring `review.py`; marker file `pending_changelog.json`. `queue()` skips files already covered by a `code_change` event from the same session.
- Reuse `review.in_scope` and the `touched_files` shape rather than duplicating the filter.
**Patterns to follow:** `src/mindhive/review.py` (whole lifecycle), `src/mindhive/events.py::append_event`.
**Test scenarios:**
- record with valid category + explanation appends a `code_change` event with normalized payload and clears a pre-existing marker.
- record rejects: unknown category; empty explanation; whitespace-only; 9-char explanation (boundary) — no event appended.
- record with explicit `--files` list stores them verbatim; with no files and a pending marker, uses marker files; with neither, falls back to git detection (mock/stub the git call).
- queue with in-scope `file_touched` events writes the marker; queue with none removes a stale marker; queue excludes files covered by a same-session `code_change` event.
- notice(marker) renders category menu + `hive log` instruction; notice(None) is "".
- `code_change` is accepted by `append_event` and appears in `SEMANTIC_TYPES` (ledger/chain inclusion covered in U3 projector test).

### U2. CLI — `hive log`

**Goal:** User-facing recording and inspection commands.
**Requirements:** R3.
**Dependencies:** U1.
**Files:** `src/mindhive/__main__.py`, `tests/test_changelog.py` (CLI cases; follow `tests/test_cli.py` conventions if separate).
**Approach:** `hive log <category> "<explanation>" [--files ...] [--session id]` records; `hive log show` prints entries newest-first; `hive log pending` prints the marker; `hive log queue --session <sid>` refreshes the gate (for the hook). Validation errors exit non-zero with the category list in the message. Note: subparser name `log` must not collide with existing commands (verify against current parser table during implementation).
**Patterns to follow:** `cmd_review_queue`/`cmd_review_clear` wiring in `src/mindhive/__main__.py`.
**Test scenarios:**
- `hive log update "…"` exit 0, event visible via `read_all`; unknown category exit non-zero, stderr names the six categories; short explanation exit non-zero.
- `hive log queue --session sid` writes marker for that session's touched files.
- `hive log show` output contains the recorded category and explanation.

### U3. Projector + briefing notice

**Goal:** Blocking visibility: unlogged changes ride STATE.md like the review gate.
**Requirements:** R4.
**Dependencies:** U1.
**Files:** `src/mindhive/projector.py`, `tests/test_projector.py`.
**Approach:** `build_state` gains the changelog notice directly after the review notice; `compose_state` passes `changelog.load_pending(project)`. LEDGER: `code_change` is already included via `SEMANTIC_TYPES` (verify `LEDGER_TYPES` math still excludes only `state_set`).
**Test scenarios:**
- STATE.md contains "⚠ Unlogged code changes" block when marker present; absent when not.
- Both review and changelog notices render together without clobbering each other.
- A `code_change` event produces a LEDGER entry file and rides the chain (existing chain test pattern).

### U4. session_end hook wiring

**Goal:** The gate refreshes automatically when a session that touched code ends.
**Requirements:** R4.
**Dependencies:** U2.
**Files:** `hooks/session_end.ps1`.
**Approach:** Add `& $py -m mindhive log queue --session $sid | Out-Null` next to the existing `review queue` line, before `project`/`briefing` rebuild so the notice lands in the same pass.
**Test expectation:** none — one-line hook script edit; covered indirectly by U2's `log queue` CLI test. Manual verification: run the hook body's commands by hand once.

### U5. Dashboard Log section

**Goal:** New **Log** nav view rendering the change-log.
**Requirements:** R5.
**Dependencies:** U1.
**Files:** `src/mindhive/dashboard.py`, `src/mindhive/dashboard_page.py`, `tests/test_dashboard.py`.
**Approach:** GET `/api/changelog` → `{"entries": [...newest first...], "pending": <marker or null>}` built from `code_change` events (ts, session, category, text, files). Register in `_GET_ROUTES`. In `dashboard_page.py`: nav button `Log` beside Memory/Oversee, `showLog()`/`renderLog()` following the `showOversee` pattern — pending banner on top (amber, instructs `hive log …`), then entries with a colored category badge, explanation, file list, timestamp.
**Patterns to follow:** `_get_oversee` route + `showOversee`/`renderOversee` in `src/mindhive/dashboard_page.py`.
**Test scenarios:**
- `/api/changelog` returns recorded entries newest-first with all fields; empty store → empty list, `pending: null`.
- With a pending marker, `pending.files` is present in the payload.
- Auth: endpoint honors the token guard like sibling routes (mirror existing dashboard auth test).
- Served page contains `showLog` hook string (the CLAUDE.md "grep the served page" check).

### U6. Documentation — standing mandatory procedure

**Goal:** Make the procedure durable policy.
**Requirements:** R6.
**Dependencies:** U1–U5 (describe what actually shipped).
**Files:** `CLAUDE.md`, `README.md`.
**Approach:** CLAUDE.md gets a short section: any change to `src/mindhive/` or `tests/` must be logged with `hive log <category> "<explanation>"` before the work is called done; category definitions in one line each. README documents the `hive log` commands beside the `hive review` entries.
**Test expectation:** none — documentation.

---

## Verification

- Full suite green: `pytest` with `PYTHONPATH=<worktree>/src` on the subprocess (worktree caveat from the ledger).
- Manual: record an entry for this very feature (`hive log major_change "Added mandatory code change-log …"`) — dogfooding is the acceptance test: the feature's own change must be its first entry.
- Dashboard preview on a side port (never touch 54461, per CLAUDE.md) showing the Log section rendering that entry.

## Risks

- **Nag fatigue:** a session that touches code but ends mid-work triggers the gate; that is intended (the next session sees it), and `queue()` auto-clears when a covering entry lands.
- **`log` subcommand name collision:** checked during U2; rename to `changelog` if `log` is taken.
- **Dashboard page size:** `dashboard_page.py` is large; keep the new view additive and grep-verifiable (`showLog`).
