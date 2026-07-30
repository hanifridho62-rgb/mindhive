# Residual Review Findings — MN-6 / MN-7

**Branch:** `claude/mindnetwork-system-init-756283`
**Reviewed range:** `ab1ddd4..b20ea0d`
**Review run:** `20260730-mn7-50abc0ba`
**Suite at time of writing:** 2759 passed, 0 failed

Six findings from this review were **fixed** in `b20ea0d` (discarded
`sidecar.rebuild()` flag, mid-wave append abort, unbounded worker pool, missing
blocked-reasons, CLI traceback, stale MN-4 denial text). What follows is what was
**not** fixed, and why.

---

## ⚠ Coverage gap: 8 of 11 reviewers did not complete

This is the most important entry here, because it bounds how much confidence the
rest of the review earns.

The review dispatched 11 reviewers. An API session limit terminated 8 of them
mid-flight. **Completed:** `reliability`, `ce-learnings-researcher`,
`ce-agent-native-reviewer`. **Terminated without returning findings:**
`correctness`, `security`, `adversarial`, `testing`, `maintainability`,
`performance`, `api-contract`, `project-standards`.

The findings that *were* returned surfaced six real defects — two of them
regressions of lessons this same change documents — so the incomplete personas
should not be assumed to have found nothing. In particular there is currently **no
independent correctness, security, or adversarial verdict** on `optin.py`'s
deepest-root matching or on `orchestrator.py`'s termination logic. Both were
verified by the author directly, which catches wrong facts but not author bias.

**Recommended:** re-run `/ce-code-review base:ab1ddd4` on this branch before merge.

---

## Not fixed — deferred with reasoning

- **P2 · `src/mindhive/mindnet/orchestrator.py:209` · No wave-level timeout.**
  `pool.map` blocks until every task returns. `executor.execute` bounds each child
  (`timeout_s`, default 600s) but kills only the **direct** child, so a
  `validation_script` that forks its own tree can leave grandchildren running past
  the timeout and, in principle, hold a wave open. Fixing properly means either
  `cf.as_completed` with a wave-level deadline, or porting `research.py`'s
  `_kill_tree` (process groups + `taskkill /F /T` / `killpg`) into `executor`.
  That is an `executor` change with its own blast radius, not orchestrator wiring.
  *Currently low risk:* `--execute` is only useful for tasks carrying a runnable
  `validation_script` (see Open Question below), and the dry run is the default.

- **P2 · `src/mindhive/mindnet/inject.py` · Enforcement state is invisible to an
  agent until it is already denied.** `inject.py` builds the block injected into
  every turn and never consults `optin`. The only ways an agent learns the teeth
  are armed are running `hive mindnet enforce` unprompted, or being denied. Since
  `optin.status()` is pure path arithmetic (no ledger read, no subprocess), a
  one-line armed/dormant note would close this without disturbing the hot-path
  cost model MN-6 was careful about. Not done here because `inject.block()` has a
  hard 1200-char budget (`MAX_BLOCK_CHARS`) and spending part of it is a
  deliberate trade that deserves its own change.
  *Flagged by `ce-agent-native-reviewer` as its top finding.*

- **P3 · `src/mindhive/__main__.py` (missing) · No non-visual surface for the cat
  board.** `GET /api/mindnet/cats` exists and `ui_cats.board` is pure, but there
  is no `hive mindnet cats`, and `list`/`show` never touch `telemetry`/`ui_cats`.
  So "which faculties are active, stale, or hung" — the entire point of the
  180s/6h bands — is reachable only by a human looking at the dashboard, or by an
  agent hand-rolling the same fold. Every other mindnet noun got a `--json`
  surface; this one reads as an oversight rather than a cut.

- **P3 · Ledger status rows carry no reason.** `b20ea0d` put per-task reasons in
  the `Orchestration` result and `--json`, which covers the operator and the
  agent. The `mindnet_task` `status` rows themselves still carry only
  `{action, task_id, status}`, so `hive mindnet show <id>` — a *different*
  surface — still cannot explain a block after the process exits. The clean fix is
  a companion `evidence` row (the schema already supports one) carrying
  `RunResult.artifact`. Deferred as a schema-touching change.

---

## Open question carried from the plan (unchanged)

**What command should a child actually run?** `Task.validation_script` is the only
per-task command the graph stores, and it is a *validator*, not an implementer. A
task without one is refused by the executor and lands `blocked` — honest, but it
means `--execute` is today only useful for graphs whose tasks carry a runnable
script. `U2`–`U5` and the dry run do not depend on resolving this. Documented in
`docs/DATA_MODEL.md` §4.6 under "Known limitation".

---

## Accepted risks (no action proposed)

- `_child_env` swallows any exception from `optin.armed()` and falls back to
  `armed=False` without logging. Correct direction (fail toward dormant), but it
  could mask a real configuration fault indefinitely.
- An exception escaping `_run_wave`/`apply_status` skips both the loop `break` and
  its `else`, so a run that raises returns nothing about what it already did —
  even though ledger state may have partially landed. `b20ea0d` narrows this (the
  raise now names the unrecorded tasks) but does not eliminate it.
- The orchestrator is a third concurrency pattern in this repo, alongside
  `research.py` (process groups + `_kill_tree`) and `workspace.py` (daemon threads
  + a lock-protected fleet). The divergence is deliberate and documented in
  `executor.execute`'s docstring, but it means MN-7 inherits `executor`'s
  direct-child-only kill for every task it runs.
