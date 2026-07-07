---
name: mindhive-orchestrator
description: >-
  Turn an idea into executed work through a memory-informed, skill-chaining
  pipeline. Use when the user types /mindhive [idea], or asks to "orchestrate
  this idea", "pick the right skill for this", "plan and build X the MindHive
  way", or wants an idea taken from interview → skill selection → execution →
  learning. Recalls from the MindHive ledger before asking, scores candidate
  skills with an explicit Recommendation Matrix, chains follow-up quality passes
  after execution, and logs what it learned back into MindHive.
---

# MindHive Orchestrator — idea → execution, one pipeline

Typing **`/mindhive [idea]`** runs a four-phase pipeline that turns a raw idea
into finished, quality-checked work while learning from every run. It does
**not** re-implement planning, coding, or review skills — it **selects and
chains** the skills already installed on this machine, seeded by MindHive
memory each step.

> **What this does:** it interviews you, recommends the best skills for the job
> with a scored matrix, executes through them, chains quality passes, and writes
> lessons back to the MindHive ledger. Confirm the idea before it starts building.

All MindHive commands below run through the project venv:
`C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive …` (or just
`hive …` / `python -m mindhive …` if that interpreter is active).

## Prime directive: recall before you ask

Before **any** interview question, run the recall preflight and let its results
pre-answer or skip questions:

1. `python -m mindhive state` — current project state, active work, standing constraints.
2. `python -m mindhive recall "<key terms from the idea>"` — prior decisions, caveats, failures on this topic. Add `--semantic` for meaning-based matches when literal recall is thin.
3. `python -m mindhive graph related "<main entity>"` — connected decisions and files, when the idea touches an existing subsystem.

**Crucial rule:** if a question can be answered by exploring the codebase or the
ledger, explore instead of asking. Only genuinely user-owned decisions —
scope, priorities, taste, product intent — reach the user.

## The pipeline

### Phase 1 — Relentless interview (one question at a time)

Achieve absolute clarity before recommending anything. Walk down each branch of
the design tree, resolving dependencies between decisions one by one.

- Ask **one question at a time** via the platform's blocking-question tool
  (`AskUserQuestion` in Claude Code — load its schema via `ToolSearch` with
  `select:AskUserQuestion` first if needed).
- For every question, **lead with your recommended answer** based on the recall
  preflight and codebase exploration, then the alternatives.
- Skip any question the preflight already answered; say what you found instead
  of re-asking ("The ledger shows you settled on X here, so I'll assume that").
- Cap the interview at decisions that change **scope, architecture, or risk**.
  Do not interrogate for its own sake — when clarity is reached, move on.

### Phase 2 — Recommendation Matrix

Once clarity is reached, propose the most appropriate skills to execute the
idea. **Load `references/recommendation-matrix.md`** and produce a matrix per
candidate skill covering: suitability, compatibility, measured completion rate,
risk level, execution speed, resource cost, and a final verdict.

- **Pull the measured telemetry first — this step is mandatory:** run
  `python -m mindhive skills stats --json` and
  `python -m mindhive skills recommend --json`, and fill the matrix from the
  measured completion rates and provenance they return. Model-estimated
  percentages are allowed **only** for never-called skills, and every such
  figure MUST be labeled `estimated`.
- Provenance vocabulary (use it verbatim in the matrix): `measured`
  (completion rate from enough recorded calls), `estimated` (breadth prior for
  a never-called skill), `agent-assessed` (`skills record` self-grades — never
  present these as measured task effectiveness).
- Resolve candidate skill names **verbatim from the session's available-skills
  list** — never recommend a skill name that is not present there.
- For each candidate, run `python -m mindhive recall "<skill name>"` to surface
  prior outcomes with it, and let that inform the scores.
- **Overlap tie-break** (matches the engine in `src/mindhive/recommend.py`):
  when two skills overlap for one need, prefer (1) the higher **measured
  completion rate** — measured always beats unmeasured; (2) **description
  clarity** — an explicit "use when" trigger, then the shorter, less ambiguous
  description; (3) the longer **failure-free completion streak**. List the
  losers as alternatives, not as separate recommendations.
- **Execution mode (learning-gated, from `skills recommend --json`):** every
  entry carries `execution_mode`. `auto` = proven track record (measured ≥3
  calls, completion rate ≥ 0.8, no open incident) — apply it during the task
  without asking. `suggest` = unproven (above all a never-used skill) — propose
  it and wait for the user's approval before invoking. Never auto-apply a
  `suggest`-mode skill, and say which mode applied when presenting the matrix.
- **Discontinued skills:** the payload's `discontinued` list names skills
  stopped by the improvements linkage (an active `skill:*` prevention rule plus
  a trailing failure streak). Do not invoke or recommend them; if one would
  otherwise be the best fit, surface its rule text and offer the alternatives
  instead. They return when the incident resolves or a successful call breaks
  the streak.
- Present the matrix, state your recommended selection, and get the user's
  approval (or revision) before executing — `auto`-mode selections may proceed
  without a blocking question, but still announce them.

### Phase 3 — Execute, then chain

Execute through the selected skill(s) by invoking them with the Skill tool.
When execution finishes, **do not stop** — proactively recommend the follow-up
chain that guarantees quality:

| Work just done | Recommended follow-up skills (resolve against available-skills list) |
|----------------|----------------------------------------------------------------------|
| New/changed code | a code-review skill (e.g. `compound-engineering:ce-code-review`), `compound-engineering:ce-simplify-code` |
| Auth / payments / input handling / external APIs | a security skill (e.g. `security-scanning:security-sast`, `compound-engineering:ce-security-reviewer`) |
| Bug-shaped or flaky behavior | `compound-engineering:ce-debug`, `superpowers:systematic-debugging` |
| UI / frontend | `compound-engineering:ce-test-browser`, `ui-design:accessibility-audit` |
| About to ship | `compound-engineering:ce-commit-push-pr` |

Give a one-line **rationale** for each follow-up you recommend (why it refines
the architecture or de-risks the deployment). Recommend; let the user choose
which to chain.

### Phase 3b — Invoke skills through MindHive (green/red + self-healing)

Any installed skill is callable **through MindHive**, not just a hand-picked
few. When execution should be driven by MindHive itself (e.g. inside `/circle`
or a headless run where the dashboard shows live status), use the invoke
channel instead of only the in-agent Skill tool:

- **List what's callable:** `python -m mindhive skills list` — the live,
  registry-truth inventory (updates the instant a plugin is installed/removed).
- **Call any skill:** `python -m mindhive skills invoke <name> [--plugin <p>]`
  — runs it via a headless `claude` and records the outcome. Exit code is `0`
  on success, `1` on failure, so you can gate the next step on it.
- **Check health first:** `python -m mindhive skills status` shows the last
  outcome per skill (`ok`/`fail`). **Before deciding retry-vs-substitute on a
  skill that shows `fail`, read its Improvements lesson**
  (`python -m mindhive rules show "skill:<name>"`) **and consult its measured
  record** (`python -m mindhive skills stats --json`) — a one-off blip on a
  strong completion rate argues retry; a low rate or an open `skill:*` incident
  argues substituting the next-ranked alternative. This is the learning loop
  closing.

Every failed invoke is written as a `tool_failed` event with a `skill:<name>`
signature, which flows automatically into the incidents → reflect → Improvements
pipeline. So a skill that fails or returns poorly **teaches the system a
prevention rule**, and its dashboard indicator turns red until it succeeds
again. Prefer skills showing green; treat red as "fix or avoid, with a reason".

### Phase 4 — Log the learnings back into MindHive

After the workflow concludes, evaluate the run and write durable lessons to the
ledger so the next `/mindhive` is smarter:

- Mistake or dead-end hit → `python -m mindhive append caveat "<one line>" --tags orchestrator`
- Inefficiency / better path discovered → `python -m mindhive append experiment_result "<one line>" --tags orchestrator`
- New stated preference → `python -m mindhive append decision "<one line>" --tags orchestrator,preference`

Only append when something **memorable** actually happened. A smooth,
unremarkable run writes nothing — do not create noise events. End by running
`python -m mindhive sync` so next session's briefing reflects the new lessons.

## Parallel dispatch and dynamic workflows

The orchestrator is the top orchestration layer: it groups the work, classifies
each piece's complexity, and then **executes in parallel whenever the pieces
are independent** — sequential execution is reserved for genuinely dependent
stages, never chosen out of convenience.

- **Fan out independent tasks.** When selected work splits into units that
  touch disjoint files/modules (e.g. refactor one module, run integration
  tests, audit security in another), dispatch them concurrently — subagents
  via the platform's subagent primitive (`Agent`/`Task` in Claude Code), or
  concurrent `hive loop` runs (one task dir per unit; every run shows up
  separately in the dashboard's **Oversee** section). Before fanning out,
  check the units' file sets for overlap — overlapping units run sequentially.
- **Dependent stages get evaluation checkpoints.** When stage 2 needs stage
  1's output, run them in order and verify each boundary: build/tests pass,
  the artifact exists, the assumption still holds. On failure, **read the
  failure log (stack trace) yourself, re-verify your assumptions, and fix it
  without new instructions from the user** — the loop's circuit breaker and
  budget gates bound the retries, so never bounce the first hurdle back to
  the user.
- **Model routing applies per unit:** lightweight analysis does not need a
  flagship model — `hive loop` routes tiers automatically when no model is
  pinned; match that spirit when choosing subagent models.

## Non-conflict protocol (coordinator, not competitor)

The orchestrator is the session's **primary coordinator**. It never
re-implements another skill's job — planning, coding, review, and debugging
belong to the skills it selects and chains.

- **When another skill is already active or invoked in the session**, the
  orchestrator restricts itself to **selection, sequencing, and ledger
  write-back**. It does not duplicate, wrap, or second-guess the running
  skill's work.
- **Single-writer rule:** only the orchestrator appends `decision` /
  `experiment_result` events about skill choice. Executed skills own their own
  domain events; the orchestrator owns the record of *why* a skill was picked.
- **Real-time write-through (the learning loop's heartbeat):** IMMEDIATELY
  after each executed skill completes, record the outcome —
  `python -m mindhive skills record <name> --ok|--fail [--note "..."]` —
  before starting the next step. These records feed the measured completion
  rates Phase 2 runs on; skipping one starves the next run's matrix.
- After recording, re-surface what to run next from refreshed data:
  `python -m mindhive skills recommend --top 3 --brief`.

## Context management: monitor and hand off at ~50%

At **every phase boundary**, estimate context consumption heuristically (tool-call
volume, files read, phases completed, and any harness compaction notices). When
the estimate reaches roughly **50%** of the window, **halt, alert the user, and
request permission to hand off** — do not silently push on. **Load
`references/handoff-protocol.md`** for the alert wording and the approved-handoff
sequence (`hive export` → append open threads → print resume instructions). The
existing PreCompact hook remains the hard backstop; this heuristic is a courtesy
checkpoint, not a precise measurement.

## Notes

- This skill orchestrates skills already installed on this machine; it does not
  install or author new skills.
- It never disturbs the V17 live-trading setup or the `~/.mindhive/LIVE_LOCK`
  guard — if a chained skill would run the loop, that skill's own guards apply.
- Matrix percentages come from the measured telemetry backend
  (`hive skills stats --json` / `hive skills recommend --json`). A model
  estimate appears only for a never-called skill and is always labeled
  `estimated` — never presented as a measured rate.
