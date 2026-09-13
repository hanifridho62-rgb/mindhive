---
name: circle
description: >-
  Activate the MindHive autonomous loop ("hive loop") for the current project.
  Use when the user types /Circle or /circle, or asks to "run the loop", "start
  the circle", "loop until done", or "autonomously finish this task". Drives the
  headless Claude Code CLI iteration-by-iteration — memory-seeded each round and
  bounded by token/cost budgets, a context smart-zone reset, a dual-condition
  exit gate, and a circuit breaker — until the task in .hive-loop/ is complete.
---

# Circle — run the MindHive autonomous loop

Typing **`/Circle`** activates `hive loop`: a native command that repeatedly
invokes the headless `claude` CLI on the current project until the task is done.
Each iteration is seeded from MindHive memory and bounded by real safeguards
(rolling token/cost budget, context reset at half the window, a dual-condition
exit gate, and a circuit breaker). This skill is the human-facing front door to
that engine — it does **not** re-implement the loop; it drives the CLI.

`/circle` is the loop **primitive**, not the centralized work pipeline. That
pipeline lives solely in **`/mindhive-orchestrator`**, which may drive this loop as
one of its execution engines. Running `/circle` on its own is just the loop — no
brainstorm→plan→review→learn stages attach to it here.

> **What this does:** it spawns a *separate, headless* Claude that edits files on
> its own (default `permission_mode = "acceptEdits"`). Treat it like handing the
> keyboard to an unattended agent. Confirm the goal before launching.

All commands below run via the MindHive checkout's own venv interpreter —
`<checkout>\.venv\Scripts\python.exe -m mindhive …` on Windows,
`<checkout>/.venv/bin/python -m mindhive …` elsewhere, where `<checkout>` is the
directory MindHive is cloned into (or just `python -m mindhive …` if that
interpreter is active).

## Steps

1. **Preflight + scaffold.** Run `python -m mindhive loop init`. It creates
   `.hive-loop/` (PROMPT.md, fix_plan.md, AGENT.md, loop.toml) if missing and
   reports whether the `claude` CLI is on PATH.
   - If it prints **"claude CLI: NOT on PATH"**, STOP and tell the user to
     install Claude Code first — the loop cannot run without it.

2. **Confirm the task.** Read `.hive-loop/PROMPT.md` and `.hive-loop/fix_plan.md`.
   - If `PROMPT.md` still holds the placeholder ("Describe what the loop should
     accomplish…"), ask the user for the goal and write it into `PROMPT.md`, and
     turn it into concrete unchecked `- [ ]` items in `fix_plan.md` (items under a
     `## Future / Optional` heading are non-blocking and never gate exit).
   - Put build/test commands the agent may need into `AGENT.md`, and make sure
     those tools are reachable via `loop.toml`'s `allowed_tools` (e.g. add
     `Bash(pytest *)`), or the headless agent can't run them.

3. **Dry run first (no API spend).** Run `python -m mindhive loop --dry-run` and
   show the user the projected prompt sizes, `required items remaining`,
   `permission_mode`, and budget/context settings. Confirm they want to proceed.

4. **Launch the loop.** Run `python -m mindhive loop`.
   - It blocks until the run ends and prints a JSON result `{"reason": …,
     "iterations": N}`. Terminal reasons: `completed`, `max_iterations`,
     `max_hours`, `budget_cap`, `api_cap`, `breaker_open`.
   - It refuses to run while `~/.mindhive/LIVE_LOCK` exists (V17 is trading).
     Only pass `--force` if the user confirms the box is idle.
   - Use `--live` to stream output, `--force` to override the LIVE_LOCK guard.
   - On a real run it **auto-opens the web dashboard** (a detached background
     process at the stable URL) so the loop can be watched live. It never fires
     on `init`, `status`, or `--dry-run`. Opt out with `--no-dashboard` for one
     run or `MINDHIVE_LOOP_DASHBOARD=0` globally; a dashboard launch failure is
     reported but never aborts the loop.

5. **Report.** Run `python -m mindhive loop status` and summarize the last run
   (reason, iteration count, total cost, total tokens). The loop also emits
   `loop_run_start` / `loop_iteration` / `loop_run_end` events into the ledger,
   so `python -m mindhive state` and the dashboard reflect it automatically.

## Tuning (`.hive-loop/loop.toml`)

- `[loop] permission_mode` — `"acceptEdits"` (default, auto-approves edits),
  `"bypassPermissions"` (any tool, max autonomy), or `""` (allowed_tools only).
- `[loop] allowed_tools` — comma list the headless agent may use unprompted.
- `[budget] window_token_budget` / `window_cost_budget_usd` — `0` = unlimited;
  `on_cap = "pause"` waits for the rolling window to free up, `"stop"` halts.
- `[loop] max_iterations` / `max_hours` / `iteration_timeout_minutes` — hard caps.
- `[breaker] no_progress_threshold` / `same_error_threshold` — when to give up.

## Notes

- The loop is resilient to a stuck iteration: an iteration that exceeds
  `iteration_timeout_minutes` is recorded as a `timeout`, drops its session, and
  feeds the circuit breaker rather than crashing the run.
- Completion requires BOTH an explicit `EXIT_SIGNAL: true` from the agent AND zero
  remaining required `fix_plan` items — so checking the boxes in `fix_plan.md`
  matters.
