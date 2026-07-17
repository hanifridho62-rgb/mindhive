---
name: mindhive-orchestrator
description: >-
  The single centralized MindHive pipeline. Use when the user types
  /mindhive-orchestrator [idea], or asks to "orchestrate this idea the MindHive
  way", "run the whole pipeline", or wants an idea taken from brainstorm →
  execution → learning. Runs a ten-stage sequential, dynamic workflow whose main
  execution engine is restricted to the superpowers and compound-engineering
  plugins (other skills complement only); recalls from the MindHive ledger before
  it asks, advances each stage automatically when intent is clear and pauses to
  converge only when it is not, fans execution out to parallel sub-agents over a
  file-complexity cluster database, and logs what it learned back into MindHive.
  This is the ONLY trigger for the centralized pipeline — /circle and
  mindhive:circle are the headless-loop primitive, not this pipeline.
---

# MindHive Orchestrator — the one centralized pipeline (idea → execution → learning)

Typing **`/mindhive-orchestrator [idea]`** runs a **ten-stage sequential, dynamic
pipeline** that turns a raw idea into finished, quality-checked work while
learning from every run. It does **not** re-implement planning, coding, or review
skills — it **selects, sequences, and chains** them, with its **main execution
engine restricted to two plugins: `superpowers` and `compound-engineering`**.
Either may run a stage, or both may be combined. Every other installed skill is a
**complement/sub** that only raises quality — never a stage backbone.

> **What this does:** it recalls before it asks, walks ten ordered stages,
> advancing automatically when it understands your intent and pausing to converge
> when it doesn't, fans execution out to parallel sub-agents, runs quality passes,
> and writes lessons back to the MindHive ledger. Confirm the idea before it
> starts building.

**This is the only front door to the centralized pipeline.** `/circle` and
`mindhive:circle` are the headless-loop **primitive**; `/engineering-loop` is a
subordinate execution engine this pipeline may drive. None of them host the
pipeline — the pipeline lives here.

All MindHive commands below run through the project venv:
`C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive …` (or just
`hive …` / `python -m mindhive …` if that interpreter is active).

## Prime directive: recall before you ask

Before **any** stage question, run the recall preflight and let its results
pre-answer or skip questions:

1. `python -m mindhive state` — current project state, active work, standing constraints.
2. `python -m mindhive recall "<key terms from the idea>"` — prior decisions, caveats, failures on this topic. Add `--semantic` when literal recall is thin.
3. `python -m mindhive graph related "<main entity>"` — connected decisions and files, when the idea touches an existing subsystem.

**Crucial rule:** if a question can be answered by exploring the codebase or the
ledger, explore instead of asking. Only genuinely user-owned decisions — scope,
priorities, taste, product intent — reach the user.

## The ten-stage sequence

Run these stages **in order**. Each stage's backbone is a `superpowers` or
`compound-engineering` skill, resolved **verbatim from the session's
available-skills list**. **Load `references/pipeline-stages.md`** for the full
stage → backbone-skill map, the permitted complement categories, and the dynamic
gate rule.

1. **Brainstorm** — `superpowers:brainstorming` or `compound-engineering:ce-brainstorm`. Establish what to build.
2. **Plan** — `compound-engineering:ce-plan` or `superpowers:writing-plans`. Turn the brief into an implementation plan.
3. **Observation** — recon the codebase and the ledger (`hive recall`/`state`/`graph`, plus the plugins' research agents). Ground the plan in what actually exists.
4. **Understand & converge** — resolve the open scope/architecture/risk forks with the user, but only the ones recall + codebase couldn't (this is the dynamic gate in action).
5. **PRD** — the requirements document the brainstorm/plan already emits (`docs/brainstorms/*-requirements.md`, `docs/plans/*-plan.md`) is the product-requirements artifact; do not re-author intent.
6. **Recommendation Matrix** — score the candidate execution skills from measured telemetry. **Load `references/recommendation-matrix.md`**, apply the learning-gated selection rules below, present the matrix, then get approval on the selection.
7. **Kanban** — turn the plan's Implementation Units into a work board (`TaskCreate`/`TaskList`), dependency-ordered.
8. **Execution** — `compound-engineering:ce-work` drives the board; for independent work, fan out to **parallel sub-agents** (see the next section).
9. **Quality assessment** — `compound-engineering:ce-code-review` + `ce-simplify-code` + `ce-test-browser` + `superpowers:verification-before-completion`. A failed verify loops back to stage 8, not forward.
10. **Learning** — write durable lessons back to MindHive (see the last section).

## Dynamic advancement: automatic vs. semi-automatic

The pipeline is **dynamic** — this is the "accuracy where the user's wishes and
the AI's context understanding meet." At **every stage boundary**, estimate your
intent-confidence for the next stage from observable proxies (recall coverage,
codebase answerability, scope/architecture/risk clarity):

- **High confidence → automatic.** Announce the inferred decision in one line and
  advance without a blocking question.
- **Low confidence → semi-automatic.** Ask **one** question at a time via the
  blocking-question tool (`AskUserQuestion` in Claude Code — load its schema via
  `ToolSearch` with `select:AskUserQuestion` first if needed), each **leading with
  your recommended answer**, until confidence is restored, then advance.

The gate **converges; it never hard-stops**. Full rule in
`references/pipeline-stages.md`.

## Learning-gated selection: telemetry, execution modes, discontinued skills

Score the stage-6 matrix from measured ground truth, never remembered
impressions:

- `python -m mindhive skills stats --json` — per-skill calls, ok/fail, completion
  rate, last outcome, and streak from the ledger. **Pulling this measured
  telemetry is mandatory before scoring anything.**
- `python -m mindhive skills recommend --json` — the deterministic ranking, with
  `measured|estimated` provenance and an `execution_mode` on every entry plus
  measured coverage. Anchor the shortlist on it.

Every recommend entry carries `execution_mode`. `auto` = proven track record
(measured ≥3 calls, completion rate ≥ 0.8, no open incident) — apply it during
the task without a blocking question, but still announce it. `suggest` =
unproven (above all a never-used skill) — propose it and wait for the user's
approval before invoking. Never auto-apply a `suggest`-mode skill, and say which
mode applied when presenting the matrix.

**Discontinued skills:** the payload's `discontinued` list names skills stopped
by the improvements linkage (an active `skill:*` prevention rule plus a trailing
failure streak). Do not invoke or recommend them; if one would otherwise be the
best fit, surface its rule text and offer the alternatives instead. They return
when the incident resolves or a successful call breaks the streak.

**Honesty guardrails.** Every figure carries its provenance — `measured`,
`estimated`, or `agent-assessed` (`skills record` self-grades). Never present an
estimate or a self-grade as a measured rate; model-estimated percentages are
allowed only for never-called skills and must be labeled `estimated`.

## Parallel multi-agent execution (the "like Fable 5" upper layer)

At the execution stage, classify the codebase and fan work out to parallel
sub-agents instead of grinding serially. **Load `references/parallel-orchestration.md`**
for the full protocol. In short:

- `python -m mindhive clusters build` — build the **cluster database** (files
  grouped by module) with **file-complexity** tiers, stored in a `cluster_db.json`
  sidecar.
- `python -m mindhive clusters plan --lanes N` — get a **shard plan** that packs
  clusters into `N` balanced, **file-ownership-safe** lanes (no file in two lanes).
- Dispatch one sub-agent per lane under `superpowers:dispatching-parallel-agents`
  (with worktree isolation when agents write concurrently), then integrate and run
  the stage-9 quality gate. Skip the fan-out and go serial when the work touches a
  single cluster or the tree is trivially small.

MindHive plans the shards (deterministic Python); the agent runtime spawns the
sub-agents. Python never spawns agents.

**Parallel dispatch rules (dynamic workflows):**

- **Fan out independent tasks.** Dispatch units that touch disjoint files
  concurrently — sub-agents via the platform's subagent primitive, or concurrent
  `hive loop` runs (one task dir per unit; every run shows up separately in the
  dashboard's **Oversee** section). Before fanning out, check the units' file
  sets for overlap — overlapping units run sequentially.
- **Dependent stages get evaluation checkpoints.** When stage 2 needs stage 1's
  output, run them in order and verify each boundary: build/tests pass, the
  artifact exists, the assumption still holds. On failure, **read the failure
  log (stack trace) yourself, re-verify your assumptions, and fix it without new
  instructions from the user** — the loop's circuit breaker and budget gates
  bound the retries, so never bounce the first hurdle back to the user.
- **Model routing applies per unit:** lightweight analysis does not need a
  flagship model — `hive loop` routes tiers automatically when no model is
  pinned; match that spirit when choosing sub-agent models.

## Stage 10 — log the learnings back into MindHive

After the pipeline concludes, evaluate the run and write durable lessons so the
next `/mindhive-orchestrator` is smarter:

- Mistake or dead-end hit → `python -m mindhive append caveat "<one line>" --tags orchestrator`
- Inefficiency / better path discovered → `python -m mindhive append experiment_result "<one line>" --tags orchestrator`
- New stated preference → `python -m mindhive append decision "<one line>" --tags orchestrator,preference`
- Reusable pattern worth compounding → invoke `compound-engineering:ce-compound`.

Only append when something **memorable** actually happened. A smooth,
unremarkable run writes nothing — do not create noise events. End by running
`python -m mindhive sync` so the next session's briefing reflects the new lessons.

## Invoking skills through MindHive (green/red + self-healing)

When execution should be driven by MindHive itself (a headless run where the
dashboard shows live status), any installed skill is callable **through MindHive**:

- **List what's callable:** `python -m mindhive skills list`.
- **Call any skill:** `python -m mindhive skills invoke <name> [--plugin <p>]` —
  runs it via a headless `claude` and records the outcome (exit `0` ok / `1` fail).
- **Check health first:** `python -m mindhive skills status`. Before invoking a
  skill showing `fail`, read its lesson (`python -m mindhive rules show
  "skill:<name>"`) **and consult its measured record** (`skills stats --json`) —
  a one-off blip on a strong completion rate argues retry; a low rate or an open
  `skill:*` incident argues substituting the next-ranked alternative.
- **Record outcomes in real time (the learning loop's heartbeat):** immediately
  after each executed skill completes, run
  `python -m mindhive skills record <name> --ok|--fail [--note "…"]`
  (agent-assessed provenance) before starting the next step — these records feed
  the measured completion rates the next matrix runs on; skipping one starves
  the learning loop.

Every failed invoke becomes a `skill:<name>` `tool_failed` event that flows into
the incidents → reflect → Improvements pipeline, so a bad call **teaches the
system a prevention rule**. Prefer green skills; treat red as "fix or avoid, with
a reason".

## Context management: monitor and hand off at ~50%

At **every stage boundary**, estimate context consumption heuristically (tool-call
volume, files read, stages completed, harness compaction notices). When the
estimate reaches roughly **50%** of the window, **halt, alert the user, and
request permission to hand off** — do not silently push on. **Load
`references/handoff-protocol.md`** for the alert wording and the approved-handoff
sequence (`hive export` → append open threads → print resume instructions). The
PreCompact hook remains the hard backstop; this heuristic is a courtesy
checkpoint.

## Notes

- This skill orchestrates skills already installed on this machine; it does not
  install or author new skills.
- It never disturbs the V17 live-trading setup or the `~/.mindhive/LIVE_LOCK`
  guard — if a chained skill would run the loop, that skill's own guards apply.
- Matrix figures carry provenance: measured completion rates come from
  `skills stats --json`; model estimates appear only for never-called skills and
  are always labeled `estimated`.
