---
date: 2026-08-05
topic: adaptive-work-injection
---

# Adaptive work injection

## Summary

One replenishment core mines the ledger for work the record proves is still
outstanding, gates each candidate on real evidence, and lands the survivors
through a surface-specific sink. Steps it creates are marked `new` because they
were not in the original plan, and they live on a roadmap card that outlives the
session or run that created them. Both execution surfaces — the interactive
Claude Code session and the headless `hive loop` — feed the same core and are
covered by this one design.

## Problem Frame

Work in this system ends quietly. A session stops when the human stops typing; a
loop run stops when its checklist empties. Neither moment asks whether the
record still holds proof of unfinished work, so debt accumulates in the ledger
while every individual ending looks correct.

The ledger measures the cost. In the `mindhive` namespace: 132 `code_change`
events against 6 `verification` events — 126 code changes with no mechanical
test evidence. 726 `tool_failed`. 134 caveats. None of it produces a task, and
none of it survives into the next session as anything a person or an agent must
act on.

The same fold exposes a premise worth stating plainly, because it reorders the
work: across all 54 namespaces in the store there are **zero** `loop_run_start`,
`loop_iteration`, and `loop_run_end` events, and exactly one `mindnet_task` row
(in a namespace named `mn18-demo`). `hive loop` and the MindNetwork task graph
are built and tested but unexercised. The surface carrying 27,075 events is the
interactive session.

## Key Decisions

**One core, several sinks.** Deciding *what* work is outstanding is
surface-independent; *forcing* it is not. The lenses, the value gate, and the
dedup live once. Each surface supplies a sink that lands survivors in whatever
shape forces continuation there.

**Interactive first, loop still in the design.** The loop sink is specified here
and built in a later phase, because building the forcing mechanism first on a
surface with zero recorded runs would test the design against nothing. The core
is written surface-agnostic from day one so the loop sink is wiring, not a
rewrite.

**Forcing is structural on both surfaces.** In the loop, an unchecked required
item makes `required_remaining > 0`, which the exit gate already refuses to pass.
In an interactive session, an open roadmap plan rides the auto-loaded state the
next session reads before anything else — the same channel that already carries
the blocking "unlogged code changes" notice. No prompt asks an agent to keep
going.

**The work's home and the relay are the same object.** A roadmap plan already
survives the session that owned it and already carries an interruption marker.
Using it for both avoids a second mechanism that would have to be kept in sync.

**Evidence is mandatory.** A candidate must point at a real ledger row, file, or
signature. This is the rule that separates replenishment from inventing work to
fill a quota.

**The threshold tunes itself.** An injector whose steps are never completed gets
quieter; one whose steps get done gets bolder. Completion is already recorded per
roadmap step, so this is measured, not configured.

**`new` is permanent.** It marks provenance — outside the original plan — not
recency. It does not clear when the step completes.

## Actors

A1. **The operator** — reads Oversee and STATE.md, completes or dismisses
injected work, sets the caps.

A2. **The interactive session** — the surface whose ending triggers a
replenishment round.

A3. **The loop run** — the second surface; same trigger role, different sink.

A4. **The replenishment core** — mines, gates, dedups, and hands survivors to a
sink. Has no opinion about which surface called it.

A5. **The lenses** — each answers one question about the record. Deterministic
lenses are folds; model lenses are agents whose output is untrusted input.

## Key Flows

F1. **Interactive round.** A session ends → the core runs every enabled lens over
the ledger → candidates without evidence are refused → survivors are scored,
deduped against every prior injection, and capped → the roadmap sink writes a
plan whose new steps carry `new` → the plan appears on Oversee and in the next
session's auto-loaded state → the operator completes or dismisses each step →
completion rates feed the threshold for the next round.

F2. **Loop round.** A run's checklist empties and the reply claims completion →
the core runs before the exit gate is evaluated → survivors are appended to the
run's required checklist → the exit gate refuses to exit → the run continues on
the injected work.

F3. **Relay.** Usage or budget runs out mid-flight → the open plan is marked
interrupted with its remaining steps and their `new` flags → delivery is
scheduled for when the window reopens → the next session inherits the plan and
resumes at the next step. Injector state — used fingerprints, refused candidates
and their reasons, round and dry-round counters — is recoverable by folding the
injection events, so no state is carried in a file that could drift.

F4. **Learning.** An injected step whose work fails leaves a failure signature →
the signature becomes a prevention rule → the repair lens sees it on the next
round. A step that stays incomplete across rounds raises the threshold instead
of being re-proposed.

## Requirements

**The core**

R1. The replenishment core decides what work is outstanding without knowing
which surface invoked it.

R2. Every candidate carries evidence: a ledger row, a file path, or a failure
signature that a reader can check. A candidate without evidence is refused, and
the refusal is recorded with its reason.

R3. Candidates are deduplicated by a stable fingerprint across rounds and across
sessions, so a refused or already-injected item is never re-proposed.

R4. Each injection is recorded as an event carrying the lens, the evidence, the
score, the round, and the fingerprint. This record is the injector's only
persistent state.

R5. Reads over injection history stay total: a malformed row is skipped, never
raised.

**Lenses**

R6. The `evidence-gap` lens proposes work when code changed without mechanical
test evidence recorded since.

R7. The `repair` lens proposes work for a recurring failure signature that has no
resolution.

R8. The `caveat`, `debt`, `audit`, and `coverage` lenses propose work from
unresolved pinned caveats, files past the project's size threshold, source files
with no change-log entry, and modules with no test counterpart.

R9. Model-driven lenses (innovation, system improvement, user-need drift) run
concurrently and their output is validated before anything is appended, using the
same validation the existing subtask proposer applies.

**Sinks and forcing**

R10. The roadmap sink lands survivors as steps on a plan that outlives the
session or run that created it.

R11. The loop sink appends survivors to the run's required checklist so the exit
gate refuses to exit while any remains.

R12. The graph sink declares survivors as subtasks of the work that produced
them, on both the task and phase axes — a task declared on one axis only is a
defect.

R13. An injected step is marked `new` wherever it is rendered, permanently.

**Bounds**

R14. Injection is capped per round, per run, and by depth; injected work may not
inject beyond the configured depth.

R15. When a cap truncates a round, the truncation is recorded — a silent cut
reads as "nothing more was found".

R16. Two consecutive rounds producing no qualifying candidate end replenishment
for that surface.

R17. The score threshold moves with the completion rate of previously injected
steps, within bounded steps, and every move is recorded.

**Relay**

R18. When usage or budget runs out, the open plan is marked interrupted with its
remaining steps, their `new` flags preserved, and delivery is scheduled for the
next window.

R19. Injector state needed to resume — fingerprints, refusals, counters — is
derivable from the injection events alone.

**Skill attribution**

R20. When the injector directs a skill, the direction is recorded before the
invocation and identifies the injector as its source. The rendered reason is
recorded fact or a labelled purpose, never a synthesized motive, and never
attributes an injector-driven call to the prompt gate.

## Acceptance Examples

AE1. **Covers R6, R10, R13.** A session changes source files and records no test
run. At session end a roadmap plan appears on Oversee with a step marked `new`
naming those files. The plan is still there in the next session.

AE2. **Covers R2.** A lens produces a candidate whose evidence pointer resolves
to nothing. No step is created, and the refusal is recorded with the reason.

AE3. **Covers R3, R19.** The same outstanding condition holds across two
sessions. The second round proposes nothing, because the fingerprint from the
first round is recovered from the injection events.

AE4. **Covers R11.** A loop run empties its checklist and its reply claims
completion. The core injects one item. The exit gate returns "required items
remain" and the run continues.

AE5. **Covers R15.** A round finds more qualifying candidates than the cap
allows. The capped count is recorded, and the round does not report itself as
exhausted.

AE6. **Covers R17.** Injected steps go three rounds without completion. The
threshold rises and the next round proposes fewer items.

AE7. **Covers R18.** A run stops on a usage limit with injected steps open. The
interruption marker carries those steps with their `new` flags, and delivery is
scheduled.

## Scope Boundaries

**Phased, all inside this design**

- Phase 1 — the core, the `evidence-gap` lens, the roadmap sink, the `new`
  marker, the injection event, the caps, and the interactive trigger.
- Phase 2 — the `repair` and `caveat` lenses, the threshold backpressure, and the
  relay for interactive sessions.
- Phase 3 — the loop sink and the loop trigger, gated on one proven end-to-end
  `hive loop` run.
- Phase 4 — the graph sink, the remaining deterministic lenses, injector-directed
  skills, and the model lenses with their parallel agents.

**Deferred beyond this design**

- Injecting into a project namespace other than the one the surface ran in.
- Any change to the Stop hook or the rotation machinery.
- Replacing the existing subtask proposer; it is called, not rewritten.
- A standalone Oversee section for injected work; the roadmap card is the home.

## Dependencies / Assumptions

- The roadmap plan card renders on Oversee independently of the session that
  created it. Verified in the dashboard's plan-card path; the interactive session
  card itself is removed at session end, which is why it cannot be the home.
- Auto-resume delivery has never fired in this store (zero recorded deliveries).
  Phase 2 must prove it fires rather than assume it.
- `hive loop` has never completed a recorded run. Phase 3 must produce one before
  building on the loop sink.
- The MindNetwork task graph holds one row across the whole store. Phase 4 should
  expect to be its first real consumer.
- The interactive trigger runs at session end, alongside the existing review and
  change-log gates.

## Outstanding Questions

**Resolve before planning**

- None.

**Deferred to planning**

- Whether the checklist provenance marker is parsed from the line text or carried
  beside it, and how the existing consumers of that parse are updated together.
- Whether the threshold's bounded steps are three named levels or a continuous
  value.
- Which caps ship with defaults versus requiring an explicit setting.

## Sources / Research

- Loop exit gate and the required-items rule: `src/mindhive/loop/safeguards.py`,
  `src/mindhive/loop/engine.py`, `src/mindhive/loop/task.py`.
- Interactive session card lifecycle and the skill trail: `src/mindhive/oversee.py`.
- Persistent plan card and the Oversee render path: `src/mindhive/dashboard_page.py`.
- Interruption marker and scheduled delivery: `src/mindhive/roadmap.py`,
  `src/mindhive/autoresume.py`.
- Untrusted-model-output validation to reuse: `src/mindhive/mindnet/decompose.py`.
- Failure-to-rule pipeline: `src/mindhive/failures.py`, `src/mindhive/signature.py`,
  `src/mindhive/incidents.py`, `src/mindhive/reflect.py`.
- Event registry and the procedure for adding a type: `src/mindhive/events.py`,
  `docs/DATA_MODEL.md`.
- Module ownership map: `docs/session-legend.md`.
- Ledger counts quoted in Problem Frame: folded from the live store on
  2026-08-05 across all 54 project namespaces.
