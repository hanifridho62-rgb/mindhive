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
candidate skill covering: suitability, compatibility, probability of success,
risk level, execution speed, resource cost, and a final verdict.

- Resolve candidate skill names **verbatim from the session's available-skills
  list** — never recommend a skill name that is not present there.
- For each candidate, run `python -m mindhive recall "<skill name>"` to surface
  prior outcomes with it, and let that inform the scores.
- Label every probability/percentage as a **model estimate** — there is no
  per-skill telemetry backend.
- Present the matrix, state your recommended selection, and get the user's
  approval (or revision) before executing.

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

### Phase 4 — Log the learnings back into MindHive

After the workflow concludes, evaluate the run and write durable lessons to the
ledger so the next `/mindhive` is smarter:

- Mistake or dead-end hit → `python -m mindhive append caveat "<one line>" --tags orchestrator`
- Inefficiency / better path discovered → `python -m mindhive append experiment_result "<one line>" --tags orchestrator`
- New stated preference → `python -m mindhive append decision "<one line>" --tags orchestrator,preference`

Only append when something **memorable** actually happened. A smooth,
unremarkable run writes nothing — do not create noise events. End by running
`python -m mindhive sync` so next session's briefing reflects the new lessons.

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
- Percentages in the matrix are honest model estimates, not measured rates.
  Building a real per-skill success-rate backend is deferred follow-up work.
