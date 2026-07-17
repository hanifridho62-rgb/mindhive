---
name: engineering-loop
description: >-
  Run a detailed, memory-driven engineering loop that fuses /circle (the MindHive
  autonomous headless loop) with the compound-engineering lfg pipeline. Use when
  the user types /engineering-loop, asks to "run the full loop", "build this end
  to end and keep going until it's done", or wants a hands-off plan→work→review→
  fix→verify→learn cycle that is more rigorous than lfg alone. It is a
  subordinate execution engine the /mindhive-orchestrator pipeline may drive at
  its execution stage — not the centralized pipeline itself. Seeds every stage
  from the MindHive ledger, drives skills through the invoke channel with green/
  red status, and turns every failure into a prevention rule.
---

# Engineering Loop — circle × lfg, with a memory spine

This is a **subordinate execution engine**, not the centralized pipeline. The one
centralized pipeline lives in **`/mindhive-orchestrator`**; this loop is one of the
execution engines that pipeline may drive at its execution stage (and it remains
directly runnable via `/engineering-loop` for a focused hands-off cycle). It takes
`/circle`'s bounded headless loop and `compound-engineering:lfg`'s staged pipeline
and fuses them into one loop that is **more detailed than lfg** because MindHive
memory sits between every stage: each stage recalls what was learned before it
runs and appends what it learned after, and any skill/plugin failure is wired
straight into the improvement loop.

> **What this does:** it drives a full plan → work → review → fix → verify →
> learn cycle, invoking the right installed skills through MindHive (green on
> success, red on failure) and re-planning from recalled lessons until the task
> is genuinely done. Like `/circle`, it can hand the keyboard to an unattended
> headless agent — confirm the goal before launching.

All commands run via the project venv:
`C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive …` (or just
`python -m mindhive …` if that interpreter is active).

## Why this is more than lfg

`lfg` runs plan → work → review → fix → verify once, top to bottom. This loop
adds three things lfg lacks:

1. **A memory spine.** Before each stage: `python -m mindhive recall "<stage +
   task terms>"` to seed it with prior decisions, caveats, and failures. After
   each stage: `python -m mindhive append <type> "<one-line lesson>"` so the
   next iteration (and the next project) is smarter.
2. **Skill-level green/red + self-healing.** Stages are executed by invoking
   installed skills **through MindHive** (`python -m mindhive skills invoke
   <name>`), which records each outcome. A `fail` becomes a `skill:<name>`
   incident that the Improvements loop learns a prevention rule from — so the
   loop repairs its own tooling instead of repeating a bad call.
3. **A real convergence gate.** Like `/circle`, it loops until BOTH the agent
   signals done AND zero required `fix_plan` items remain — not a single pass.

## The loop (one iteration)

Each iteration walks the six stages. Recall before, append after, every stage.

### 1. PLAN
- `python -m mindhive recall "<task terms> plan"` — seed with prior plans/caveats.
- Invoke a planning skill through MindHive:
  `python -m mindhive skills invoke ce-plan --plugin compound-engineering`
  (or drive `ce-plan` with the Skill tool when running interactively).
- Record the plan path. `append decision "plan: <path> — <one line>"`.

### 2. WORK
- `recall "<task terms> implementation caveats"` first.
- Execute via `skills invoke ce-work --plugin compound-engineering` (or Skill
  tool). Prefer skills whose `skills status` shows **green**; if a needed skill
  shows **red**, read its lesson (`rules show "skill:<name>"`) and apply the
  prevention rule before invoking it.
- **Learning-gated tool selection:** consult `skills recommend --json` —
  `execution_mode: auto` skills (proven track record) may be applied without
  asking; `suggest` skills (unproven / never used) are proposed first; skills
  in the `discontinued` list are never invoked — use their listed alternatives
  and surface the prevention rule that stopped them.
- **Parallel whenever independent:** when the plan's units touch disjoint
  files, fan them out (subagents or concurrent `hive loop` runs, each visible
  in the dashboard's Oversee section); keep genuinely dependent stages
  sequential with an evaluation checkpoint at each boundary.
- `append experiment_result "work: <what changed>"` when something notable held.

### 3. REVIEW
- `recall "<area> review findings"`.
- `skills invoke ce-code-review --plugin compound-engineering` in agent mode.
- Capture actionable findings. `append caveat "<finding>"` for anything that
  should never recur.

### 4. FIX
- Apply the review's verified fixes. Do **not** weaken tests to pass.
- If a skill call itself failed this iteration, its `skill:<name>` incident is
  already queued for reflection — run `python -m mindhive rules reflect` to turn
  it into a prevention rule now, so the next iteration avoids the same mistake.

### 5. VERIFY
- Run the project's real checks (tests/build). `skills invoke ce-test-browser`
  for UI work.
- On failure, loop back to FIX (bounded by the circuit breaker below), not
  forward. A red verify is the loop's signal to re-plan from what just broke.

### 6. LEARN
- Evaluate the iteration and write durable lessons:
  - dead-end → `append caveat "<one line>" --tags engineering-loop`
  - better path found → `append experiment_result "<one line>" --tags engineering-loop`
  - new preference → `append decision "<one line>" --tags engineering-loop,preference`
- `python -m mindhive sync` so the next iteration's briefing sees them.

## Driving it autonomously (the /circle engine)

For hands-off runs, this loop rides `/circle`'s headless engine rather than
re-implementing it:

1. `python -m mindhive loop init` — scaffold `.hive-loop/` and confirm the
   `claude` CLI is on PATH.
2. Put the six-stage checklist into `.hive-loop/fix_plan.md` as required `- [ ]`
   items, and the goal into `PROMPT.md`.
3. `python -m mindhive loop --dry-run` to preview, then `python -m mindhive loop`
   to run. It honors the `~/.mindhive/LIVE_LOCK` guard (V17 trading) — only pass
   `--force` when the box is confirmed idle.
4. The loop auto-opens the dashboard; watch the **Skills & plugins** view to see
   each invoked skill flip green/red live, and the **Improvements** view accrue
   prevention rules from any failures.

## Convergence & safety

- Completion requires BOTH `EXIT_SIGNAL: true` from the agent AND zero remaining
  required `fix_plan` items — a single green pass is not "done".
- The circuit breaker (`no_progress_threshold`, `same_error_threshold` in
  `loop.toml`) stops a loop that stops making progress or repeats an error.
- Never disturb the V17 live-trading setup or the `LIVE_LOCK` guard.

## Notes

- This skill **selects and chains** installed skills; it does not author new
  ones. Candidate skill names must resolve verbatim from the session's
  available-skills list (or `python -m mindhive skills list`).
- The green/red status and Improvements lessons are the same data the
  dashboard renders — CLI and dashboard are two views of one ledger.
