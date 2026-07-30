# Residual Review Findings — MN-6 / MN-7

**Branch:** `claude/mindnetwork-system-init-756283`
**Reviewed range:** `ab1ddd4..f1208a2`
**Review runs:** `20260730-mn7-50abc0ba` (partial), `20260730-mn7-rerun` (completed)
**Suite:** 2773 passed, 0 failed

## Review coverage — now complete

The first pass lost 8 of 11 reviewers to an API session limit. **All eleven have
now reported.** The rerun found a **P0 the first pass missed entirely**, which is
the whole argument for having re-run it:

| Reviewer | Pass 1 | Rerun |
|---|---|---|
| correctness, security, adversarial, api-contract | terminated | ✅ completed |
| testing, performance | terminated | ✅ completed |
| maintainability, project-standards | terminated | ⚠ still not run (see below) |
| reliability, learnings, agent-native | ✅ completed | (not re-run) |

**Two reviewers remain unrun:** `maintainability` and `project-standards`. Both are
structural-quality passes rather than defect hunts, and the rerun's four
highest-stakes personas (correctness, security, adversarial, api-contract) all
completed. Worth running before merge, but no longer the gap the P0 was hiding in.

## Fixed this round (10 findings)

All in `b20ea0d` and `f1208a2`. Highlights, because two of them are worth
remembering rather than just closing:

- **P0 — a scriptless task was permanently, unrecoverably BLOCKED.** Traced
  independently by correctness, security, and adversarial. `refused` (no child ever
  created) mapped to `blocked`, and `tasks.ready`'s cascade then killed the whole
  subtree with no CLI able to revive it. Now keyed on `RunResult.executed`.
  *I had seen this behaviour in a live CLI check the previous turn and reported it
  as correct.*
- **P1 — a transient environmental condition could poison the entire graph.** Each
  child re-resolved `paths.worktree_root()` itself; a missing git or one 2s timeout
  made every task in the wave report itself failed.
- **P2 — the hot path paid a duplicate `detect_project()` git subprocess per tool
  call**, while `optin.py`'s docstring claimed "no subprocess" and the only test
  covering it passed the one argument that avoids the call. Same defect class as
  the MN-4 P0.

## Not fixed — deferred with reasoning

- **P2 · `orchestrator.py` · No wave-level timeout.** `pool.map` blocks until every
  task returns. `executor.execute` bounds each child but kills only the **direct**
  child, so a `validation_script` that forks its own tree can leave grandchildren
  running past the timeout. Fixing properly means porting `research.py`'s
  `_kill_tree` (process groups + `taskkill /F /T` / `killpg`) into `executor` — its
  own blast radius, and `executor.execute`'s docstring records the current scope as
  a deliberate bet. Revisit before `--execute` is trusted with scripts that spawn
  subprocess trees (`pytest -n auto` is the obvious case).
- **P2 · `orchestrator.py` · `KeyboardInterrupt` is not handled.** `one()` narrows
  to `except Exception`, so Ctrl-C lands on the main thread inside `pool.map`; the
  `ThreadPoolExecutor` context manager then blocks on `shutdown(wait=True)` until
  every started child finishes, and tasks that ran get no ledger row. Needs
  `BaseException`, `cancel_futures=True`, and a per-run nonce in the run key so a
  retry cannot overwrite the interrupted attempt's artifact.
- **P2 · `inject.py` · Enforcement state is invisible to an agent until it is
  already denied.** `inject.py` never consults `optin`. `optin.status()` is pure
  path arithmetic, so a one-line armed/dormant note is cheap — but it spends part
  of a hard 1200-char injection budget, which deserves its own change.
- **P2 · `optin.py` · Arming is keyed to the hook process's cwd, not to the file
  being written.** A session whose cwd is a nested worktree that was `--disable`d is
  dormant for writes into an armed parent checkout. Fail-open direction, but it
  means `--enable` protects little in a nested-worktree workflow. Either resolve
  write-tool decisions against the written path's directory, or document that
  arming is per-session-cwd.
- **P2 · `optin.py` · `set_enabled` is an unlocked read-modify-write.** Two
  concurrent `hive mindnet enforce` calls can silently discard one toggle, and the
  losing side has already printed success. Needs a lock or an O_EXCL temp name plus
  a read-back check.
- **P3 · Ledger status rows carry no reason.** `f1208a2` put per-task reasons in the
  result and `--json`, covering the operator and the agent, but the rows themselves
  still carry only `{action, task_id, status}` — so `hive mindnet show` cannot
  explain a block after the process exits. Clean fix is a companion `evidence` row
  carrying `RunResult.artifact`; deferred as schema-touching.
- **P3 · No `hive mindnet cats`.** The 180s/6h staleness bands are reachable only
  via the dashboard or by hand-rolling the fold.
- **P3 · `enforce.json`'s `version` is written but never read.** A future schema
  bump that stays duck-typed-compatible would be silently misinterpreted.
- **P3 · `--max-workers` has no upper bound**, so an operator can request more
  concurrency than `DEFAULT_MAX_WORKERS` exists to prevent. Explicit intent, but
  the backstop does not apply to the flag.
- **P3 · A dry run that exhausts `max_waves` reports a plain `dry-run`** with a
  silently truncated plan; `capped` is computed on that path and discarded.
- **P3 · `CAT_PALETTES` growing 6→7 reassigns existing swarm-agent cat colours**
  (`catFor` now mods by the table length). Cosmetic and deterministic; no test pins
  per-agent colour stability across the change.

## Open question carried from the plan (unchanged)

**What command should a child actually run?** `Task.validation_script` is a
*validator*, not an implementer. A task without one is now `skipped` rather than
blocked — recoverable, and reported — but it means `--execute` is still only useful
for graphs whose tasks carry a runnable script. Documented in DATA_MODEL §4.6.

## Accepted risks (no action proposed)

- `_child_env` swallows any exception from `optin.armed()` and falls back to
  dormant without logging. Correct direction; could mask a config fault.
- `_child_env` forces `MINDNETWORK_ENFORCE` into every child, and the env var
  out-ranks the per-checkout record in both directions — so a grandchild running in
  a different checkout loses that checkout's own opt-in decision.
- An exception escaping `_run_wave`/`apply_status` skips both the loop `break` and
  its `else`, so a run that raises returns nothing about what it already did.
  `b20ea0d` narrows this (the raise names unrecorded tasks) without eliminating it.
- `sidecar.rebuild` is ~264 ms and runs once per wave, so a deep chain pays one
  full ledger fold per dependency level. Within the system's stated bounds;
  realistic graphs are far shallower.
- Two concurrent `orchestrate --execute` runs on one namespace share a graph
  snapshot, run the same tasks twice, and collide on identical run keys. Ledger
  integrity survives (per-session segments); durable artifacts do not.
