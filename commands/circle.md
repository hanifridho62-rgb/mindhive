---
description: Activate the MindHive autonomous loop (hive loop) for this project
argument-hint: "[--dry-run | --live | --force]"
---

You are activating **`/Circle`** — the MindHive autonomous loop (`hive loop`) for
the current project. This repeatedly drives the headless `claude` CLI until the
task in `.hive-loop/` is complete, seeded by MindHive memory each iteration and
bounded by token/cost budgets, a context smart-zone reset, a dual-condition exit
gate, and a circuit breaker.

> It spawns a **separate, headless** Claude that edits files on its own
> (`permission_mode = "acceptEdits"` by default). Confirm the goal before launching.

Extra flags passed by the user: `$ARGUMENTS`

Run everything through the project venv:
`C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive …`

Steps:

1. **Preflight + scaffold:** run `python -m mindhive loop init`. It creates
   `.hive-loop/` (PROMPT.md, fix_plan.md, AGENT.md, loop.toml) if missing and
   reports whether the `claude` CLI is on PATH. If it is **NOT** on PATH, stop and
   tell the user to install Claude Code first.

2. **Confirm the task:** read `.hive-loop/PROMPT.md` and `.hive-loop/fix_plan.md`.
   If `PROMPT.md` is still the placeholder, ask the user for the goal, write it in,
   and turn it into concrete unchecked `- [ ]` items in `fix_plan.md` (items under
   `## Future / Optional` never gate exit). Make sure any build/test tools the
   agent needs are listed in `loop.toml`'s `allowed_tools`.

3. **Dry run (no API spend):** run `python -m mindhive loop --dry-run` and show the
   projected prompt sizes, required-items-remaining, `permission_mode`, and budget
   settings. Confirm the user wants to proceed.

4. **Launch:** run `python -m mindhive loop $ARGUMENTS`. It blocks until the run
   ends and prints `{"reason": …, "iterations": N}`. It refuses to run while
   `~/.mindhive/LIVE_LOCK` exists (V17 trading) unless `--force` is given and the
   user confirms the box is idle. On a real run it also **auto-opens the web
   dashboard** at the stable URL so you can watch the loop live; suppress it with
   `--no-dashboard` or `MINDHIVE_LOOP_DASHBOARD=0` (never fires on `init`,
   `status`, or `--dry-run`).

5. **Report:** run `python -m mindhive loop status` and summarize the last run
   (reason, iterations, total cost, total tokens).

If a `circle` skill is loaded, follow its fuller guidance; this command and that
skill describe the same workflow.
