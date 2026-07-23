---
title: "feat: Workspace cost accounting (WS2 — pre-flight budget gate)"
type: feat
date: 2026-07-22
origin: docs/prds/2026-07-22-003-agent-process-layer-prd.md
status: ready
depth: standard
---

# feat: Workspace cost accounting (WS2 — pre-flight budget gate)

## Summary

The multi-agent (MindParallel / MindSwarm) surface spends unbounded, invisible
money: every run's cost reaches `Agent.last_cost` in memory and is then
discarded (**0 of 29 historical runs persisted a cost**), and nothing checks
spend before launching up to 16 headless `claude` subprocesses. WS2 makes spend
**durable** and **gated**: persist per-run cost to the ledger, fold it into
`loop/budget.py`'s existing pure primitives, and refuse a spawn that a
`rolling_avg` estimate says would breach a **user-set** cost ceiling — failing
**open** when there is no prior to estimate from, because there is no honest
denominator otherwise (no account "remaining quota" API exists).

This is Phase 2b of the agent process layer. It builds directly on WS1: the
journal (`agent_disconnect`) already carries cost for non-clean runs, and the
ledger-identity fix (WS0.5) means one run's cost lands under one `ws-<id>`
session.

---

## Problem Frame

`loop/engine.py` (the single-agent loop) already has full budget maturity —
rolling-window primitives, per-iteration cost events, 429 detection,
interrupt/resume. `workspace.py` (the multi-agent path) has **none of it**:

- **Cost is discarded.** `_reader` assigns `a.last_cost = total_cost_usd` from
  the result envelope, then nothing persists it. `workspace._log` writes
  `assistant_response` (done) with no cost field. *(Fixed for the failure path
  in WS1a: `agent_disconnect` carries `cost`.)*
- **No pre-flight gate.** `Manager.spawn` / `start_mission` gate only on the
  16-slot `MAX_AGENTS` count. `workspace.py` imports only `claude_cli, events,
  paths` — `loop/budget.py` is never in scope.
- **The one honest signal is post-hoc cost.** Per the origin PRD §5.2 and §2.2:
  no account remaining-quota number exists anywhere. The only real signals are
  the plan tier and each result's `total_cost_usd`. So the ceiling is a **local
  policy the user sets**, and the system must be honest that it is exactly that.

`loop/budget.py`'s five functions are **pure over a caller-supplied `entries`
list** and need **zero modification** (origin PRD §1.4). `would_exceed` is
already shaped as a pre-flight reservation check. The gap is exactly two missing
pieces: **an entry source** (costs are discarded, so `entries` can't be folded)
and **a caller** (nothing invokes the check before spawning).

---

## Requirements

Traces to origin PRD §WS2, goals **G2** (accounting) and the §5.2 budget model.

- **R1** — Every completed workspace run's real cost reaches the ledger,
  attributable to an agent and role, under the run's one `ws-<id>` identity.
- **R2** — A per-project cost ceiling is user-settable and persisted as config
  (not in the append-only chain); each change appends an audit event.
- **R3** — `Manager.spawn` / `start_mission` check a `rolling_avg`-derived cost
  estimate against the ceiling **before** launching subprocesses, and refuse
  with a reason (and `wait_seconds` when a rolling window is the binding
  constraint).
- **R4** — The gate **fails open**: with no prior to estimate from, or no
  ceiling set, a spawn is never blocked. It refuses only on evidence that the
  ceiling the user set would be breached.
- **R5** — Reads stay total (SOP 7): the entries fold skips malformed rows and
  never raises; a broken budget subsystem must never crash a pane or block a
  run.

---

## Key Technical Decisions

- **KTD1 — `loop/budget.py` is imported, never modified.** Its primitives are
  correct and pure (origin §1.4). WS2 supplies the two things it lacks: an
  `entries` source and a caller.
- **KTD2 — Persist cost by piggybacking on the terminal events, not a new
  per-run event.** The done path's `assistant_response` gains `cost` / `turns`
  fields (already available as `a.last_cost` / `a.last_turns`); the failure
  path's `agent_disconnect` already carries them (WS1a). This avoids doubling
  ledger volume on the happy path (a dedicated `agent_cost` event per run would
  add ~22 events across history for no benefit the terminal events don't
  already provide).
- **KTD3 — Ceiling lives in a `budget.json` sidecar, mirroring `enrich.json`.**
  Config/derived data stays out of the append-only hash chain (the
  `enrich.json` / `triggers.json` precedent); every change writes atomically via
  `paths.write_text_atomic` and appends a `budget_set` audit event — the
  `enrich_toggled` precedent.
- **KTD4 — Budget logic lives in a new `workspace_budget.py` module, not in
  `workspace.py`.** `workspace.py` is already the WS0/WS1/WS2 target and is over
  the 1000-line thermo-nuclear cap; the entries fold + sidecar + gate are a
  cohesive, independently-testable concern. `workspace.py` imports and calls it.
- **KTD5 — Fail-open is deliberate (R4).** `rolling_avg` over empty entries
  returns `(0, 0.0)`; `would_exceed(..., next_cost=0)` then refuses only when
  the window is *already* over an explicit non-zero ceiling. A gate that blocked
  novel work because it had no prior would be worse than today's status quo.
- **KTD6 — Tokens are not tracked; the ceiling is cost-only.** The result
  envelope gives `total_cost_usd` but no token count for workspace runs.
  `BudgetEntry.tokens` is recorded as `0` and `BudgetConfig.token_budget` stays
  `0` (unlimited). The user-set ceiling is `cost_budget_usd` over a rolling
  `window_hours`.

---

## Assumptions

- **A1** — Rehydrated cost history is sufficient signal for `rolling_avg`.
  Post-WS1, cost is persisted going forward; the estimate improves as runs
  accumulate. Early runs (thin history) fall under the fail-open path (R4),
  which is the intended behavior, not a gap.
- **A2** — The ceiling is per-session/per-project scope for this workstream. A
  per-mission override is deferred (origin §9 Q4) — not required for the gate to
  be useful.
- **A3** — Surfacing the gate/spend in the dashboard UI (a burn-rate gauge,
  a refusal toast) belongs to **WS5** (honest UI). WS2 returns the refusal in
  the `spawn` API result (`{ok: false, error, wait_seconds}`), which the
  existing UI already renders as an error; no new rendering is in scope here.

---

## Implementation Units

### U1. Persist per-run cost on the done path (the entry source)

**Goal:** Every completed run records its cost in the ledger, so `entries` can
be folded. Closes the "cost discarded on 29/29 runs" gap for successful runs
(the failure path already carries cost via WS1a's `agent_disconnect`).

**Requirements:** R1.

**Dependencies:** none (extends WS1a's `_reader` terminal branch).

**Files:**
- `src/mindhive/workspace.py` — `_reader` done branch: include `cost` / `turns`
  in the `assistant_response` payload.
- `tests/test_workspace.py` — cost persistence on the done path.

**Approach:** In `_reader`'s `code == 0 and saw_result` branch, add
`a.last_cost` / `a.last_turns` to the `assistant_response` payload passed to
`_log`. `_log` already stamps `ledger_sid` + workspace tags, so the cost lands
under the run's one identity, workspace-tagged, attributable to the role.
Leave the failure path untouched (`agent_disconnect` already carries cost).

**Patterns to follow:** WS1a's `_emit_disconnect` payload shape (`cost`,
`turns`); the existing `assistant_response` `_log` call.

**Test scenarios:**
- Happy path — a clean run's `assistant_response` payload carries `cost` equal
  to the result envelope's `total_cost_usd` and `turns` equal to `num_turns`.
- Edge — a run whose result envelope omits `total_cost_usd` persists `cost` as
  `None` (not `0`), so the fold can distinguish "no cost reported" from "$0".
- Regression — the existing `["user_prompt", "assistant_response"]` event
  sequence and text payload are unchanged (cost is additive).

**Verification:** A completed fake run's captured `assistant_response` payload
contains the cost; the done-path event sequence is unchanged.

### U2. `workspace_budget.py` — entries fold + ceiling sidecar

**Goal:** Provide the two things `loop/budget.py` lacks — an `entries` source
folded from the ledger, and a persisted, user-settable ceiling with an audit
trail.

**Requirements:** R2, R5.

**Dependencies:** U1 (cost must be persisted to fold).

**Files:**
- `src/mindhive/workspace_budget.py` — new module.
- `src/mindhive/events.py` — register `budget_set` in `DETERMINISTIC_TYPES`
  (why-comment; telemetry/audit, out of the semantic layer — the
  `enrich_toggled` precedent).
- `tests/test_workspace_budget.py` — new test file.
- `tests/test_events.py` — `budget_set` is a valid deterministic type.

**Approach:**
- `budget_config(project=None) -> BudgetConfig`: read `budget.json` from
  `paths.project_dir(project)`; default `window_hours` (see below),
  `token_budget=0`, `cost_budget_usd=0` (unlimited) when absent or unreadable.
- `set_budget(project, cost_budget_usd, window_hours) -> dict`: validate
  non-negative numbers, `paths.write_text_atomic` the sidecar, append a
  `budget_set` audit event best-effort (a failed append never fails the set —
  the sidecar is the source of truth). Mirror `enrich.set_*`.
- `budget_entries(project=None, evs=None) -> list[BudgetEntry]`: fold
  workspace-tagged `assistant_response` + `agent_disconnect` events; for each
  with a numeric `cost`, append `BudgetEntry(ts=epoch, tokens=0,
  cost_usd=cost)`. Total read — skip rows with missing/non-numeric cost or
  unparseable ts; never raise. Reuse the epoch parse helper the loop uses
  (`loop.state._ts_epoch`).

**Patterns to follow:** `enrich.py` `_settings_path` / `settings` /
`set_*` + `enrich_toggled`; `oversee._is_workspace` for the workspace-tag
guard; `loop.state` cost fold (`sum(e.payload.get("cost_usd", 0.0) ...)`).

**Test scenarios:**
- `budget_config` returns unlimited defaults when the sidecar is absent, and a
  corrupt/unreadable sidecar degrades to defaults without raising (R5).
- `set_budget` round-trips through the sidecar and appends exactly one
  `budget_set` audit event; a rejected (negative) value writes nothing.
- `budget_entries` folds cost from both `assistant_response` and
  `agent_disconnect`, ignores non-workspace sessions and ordinary chat, and
  skips rows with missing/non-numeric cost or bad ts (canary: a row with a
  valid cost DOES produce an entry; totality: a junk row does not raise).
- `budget_set` is registered deterministic, not semantic, not in the Memory
  view (mirror the WS1a `agent_disconnect` events test).

**Verification:** The fold produces one `BudgetEntry` per cost-bearing terminal
event; the sidecar persists and audits; malformed input is skipped, never
raised.

### U3. Pre-flight gate in `spawn` / `start_mission` + API surface

**Goal:** Check the estimated cost of a new run against the ceiling before
launching, refuse with a reason when it would breach, and fail open otherwise.

**Requirements:** R3, R4.

**Dependencies:** U2.

**Files:**
- `src/mindhive/workspace.py` — `Manager.spawn` / `start_mission` call the gate;
  `api_get`/`api_post` expose `budget` (read) and `set_budget` (write).
- `src/mindhive/workspace_budget.py` — `gate(project, entries=None, config=None,
  now=None) -> dict` helper returning `{ok, reason?, wait_seconds?}`.
- `tests/test_workspace.py` — gate wired into spawn; refusal shape; fail-open.
- `tests/test_workspace_budget.py` — the gate helper in isolation.

**Approach:**
- `gate(...)`: load `budget_config` + `budget_entries` (injectable for tests),
  estimate `next_cost` via `rolling_avg(entries, N)` (the recent-run mean; falls
  to `0.0` when history is thin → fail-open per R4). If
  `would_exceed(entries, now, cfg, 0, next_cost)` → `{ok: False, reason: "...",
  wait_seconds: wait_seconds(...)}`; else `{ok: True}`.
- `Manager.spawn`: after the `MAX_AGENTS` check, before constructing the Agent,
  call `gate`; on refusal return `{ok: False, error: reason, wait_seconds}`
  without spawning. Wrap the gate call so any subsystem error fails open (R5) —
  a broken budget module must not block spawning.
- `start_mission`: gate once for the whole mission (estimate ≈ N × per-run
  mean) before spawning the coordinator + crew, so a mission can't half-spawn
  and then hit the ceiling mid-fleet.
- `api_get("budget")` → current config + rolling used/remaining;
  `api_post("set_budget", {cost_budget_usd, window_hours})` → `set_budget`.

**Execution note:** Test-first — start with a failing test that a spawn is
refused when the folded entries + estimate exceed a set ceiling, and a second
that a spawn with no history is allowed.

**Test scenarios:**
- Happy path — with a ceiling set and folded history under it, `spawn` succeeds
  and launches; `gate` returns `{ok: True}`.
- Refusal — with history whose rolling estimate would breach the ceiling,
  `spawn` returns `{ok: False}` with a reason and no subprocess is launched
  (assert the fake spawn was never called).
- Fail-open (R4) — no ceiling set (unlimited) → spawn allowed; ceiling set but
  zero history → spawn allowed (estimate is 0).
- Window — when the window is the binding constraint, the refusal carries a
  positive `wait_seconds`.
- Resilience (R5) — a gate that raises internally (injected failure) fails
  open: `spawn` still launches.
- `start_mission` — refuses the whole mission pre-spawn when the aggregate
  estimate breaches, spawning nothing.
- API — `set_budget` then `get budget` round-trips; a spawn refusal surfaces in
  the `api_post("spawn")` result.

**Verification:** Exercise a refused spawn (fake spawn uncalled) and an
allowed spawn against a set ceiling; the API round-trips config and reflects a
refusal.

---

## Scope Boundaries

**In scope:** cost persistence, the ledger-folded entries source, the
user-settable ceiling with an audit trail, and the pre-flight refusal in
`spawn` / `start_mission` with a fail-open posture.

### Deferred to Follow-Up Work
- **WS5 (honest UI):** a burn-rate gauge, live spend display, and a refusal
  toast. WS2 only returns the refusal in the API result.
- **Per-mission ceiling override** (origin §9 Q4) — per-session/project is the
  WS2 scope.
- **Token budgets** — no token signal exists for workspace runs (KTD6);
  cost-only ceiling for now.
- **429 / rate-limit classification and auto-resume** for the multi-agent path
  (origin §1.3) — a separate concern from the spend gate.

---

## System-Wide Impact

- **New event type `budget_set`** — registration is step one of U2;
  `append_event` raises on unknown types, so this must land before any
  `set_budget` write. Total-read invariant preserved (audit telemetry, skipped
  by semantic projections).
- **`assistant_response` payload gains `cost`/`turns`** — additive; the Memory
  view, oversee status fold, and graph all bound or ignore extra payload fields,
  so no consumer regresses (mirror the WS1a cap-leak verification).
- **`spawn` can now return `{ok: False}` for a budget reason** — callers already
  handle `ok: False` (the `MAX_AGENTS` cap path), so the failure shape is not
  new; only the reason is.

---

## Risks & Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Gate wrongly refuses novel/legitimate work | Medium | Fail-open without a prior (R4/KTD5); refuse only against an explicit non-zero ceiling with evidence |
| Entries fold is O(all-history) per spawn | Medium | Fold lazily and cache per (project, ledger-head) if measured hot; the spawn path is human-paced, not a 2.5 s tick — measure before optimizing |
| A broken budget subsystem blocks spawning | High | Every gate/fold call is wrapped to fail open (R5); the ledger/config must never crash a pane |
| `budget.json` and the audit event diverge | Low | Sidecar is the source of truth; the `budget_set` event is best-effort audit only (the `enrich_toggled` contract) |
| `workspace.py` grows further past the 1000-line cap | Medium | Budget logic lands in the new `workspace_budget.py` (KTD4); `workspace.py` only gains the gate call sites |

---

## Sources & Research

- Origin PRD: `docs/prds/2026-07-22-003-agent-process-layer-prd.md` — §WS2,
  §1.3–1.4 (budget asymmetry, `budget.py` needs no change), §5.2 (budget model,
  the fail-open decision), §2.2 (no remaining-quota API), §9 (verification:
  "cost reached the ledger 0 of 29 runs").
- `src/mindhive/loop/budget.py` — the pure primitives WS2 calls (verified
  first-hand this session: `used`, `rolling_avg`, `would_exceed`,
  `wait_seconds`, `BudgetConfig`, `BudgetEntry`).
- `src/mindhive/enrich.py` — the `enrich.json` sidecar + `enrich_toggled` audit
  precedent WS2's ceiling mirrors (`_settings_path`, `settings`, `set_*`).
- WS1a (this session) — `agent_disconnect` already carries `cost`/`turns`; the
  done path (`assistant_response`) is the remaining gap U1 closes.
