# Session prompt — build adaptive work injection

Paste the block below as the first message of the next session.

---

Build the adaptive work-injection mechanism for MindHive: a loop that
replenishes its own plan from evidence instead of stopping the moment its
checklist empties.

**Read before the first edit, in this order:** `hive state`, `hive briefing`,
`hive sop list`, then the approved spec
`docs/superpowers/specs/2026-08-05-adaptive-work-injection-design.md`, then
`docs/session-legend.md` (the address map) and `docs/DATA_MODEL.md` §6 (how a
new event type is added). The spec is approved — implement it, do not re-open
the design. If you find the spec contradicts the code, the code wins: say so and
adjust.

**Plan before you build.** Use the `ce-plan` skill, then `writing-plans`. Do not
touch code before the plan exists.

**Scope this session: Phase 1 only** (the headless loop). Phase 2 (interactive
sessions, the three model lenses, `adaptive_ambition`) is a separate PR and
must not leak into this one.

Phase 1 build order — each step lands before the next starts:

1. `src/mindhive/replenish.py` with the six deterministic lenses (`repair`,
   `evidence-gap`, `debt`, `audit`, `caveat`, `coverage`) and the value gate.
   Evidence is mandatory: a candidate with no pointer to a real ledger row,
   file, or signature is refused.
2. Register `work_injected` in `events.py` `DETERMINISTIC_TYPES` following the
   DATA_MODEL.md §6 procedure, with the why-comment the other entries carry.
   Document it in `docs/DATA_MODEL.md` in the same change-set.
3. Both landing axes: the `- [ ] (new) <title>` line written to `fix_plan.md`
   above the `# Future` heading, and the `mindnet_task` + `mindnet_phase`
   `declare` pair (follow `decompose.declare` — a graph-only declaration orphans
   every later phase event).
4. Wire the trigger into `loop/engine.py` immediately before `should_exit`, only
   when `required_remaining == 0` and the parsed status claims completion.
5. Provenance through to Oversee: `task.plan_items` carries the `new` flag, and
   **every** caller is updated in the same change-set — `loop/state._fix_plan_items`,
   `rmFromCard` in `dashboard_page.py`, and `roadmap.steps_from_plan_items` via
   `engine._snapshot_interrupted_plan`. This is the riskiest change in the spec;
   treat a missed caller as a defect, not a follow-up.
6. The `new` pill in `ovGroup`/`ovRow` and the `new` status in the roadmap panel.
   The badge is permanent — it means "not in the original plan", not "recent".
7. The `[replenish]` config section in `loop/config.py`, and the caps
   (`max_injections_per_run`, `max_injection_rounds`, `max_injection_depth`,
   `dry_rounds_to_exit`).
8. The relay: injected items keep their `new` flag through the interruption
   snapshot, and the injector's own state (used fingerprints, refused candidates
   with reasons, round counter, dry-round count) is recoverable from a fold over
   `work_injected` — no new sidecar.

**Reuse, do not reimplement.** `decompose.validate` for proposal validation
(`ID_RE`, char caps, id collision, `MAX_DEPTH`, `MAX_NODES`, the `check_graph`
probe) — `tasks.fold` drops an unsound edge silently, so a hand-rolled check
would half-land and report success. `signature.py` for fingerprints.
`invoker.invoke_skill` for skill invocation. `engine._snapshot_interrupted_plan`
and `autoresume` for the relay.

**Skill trail honesty.** When the injector directs a skill, append
`skill_directed` before the invocation with a new `source` field, and give
`oversee._skill_why` a branch for it. The existing string says "directed by
prompt-gate", which would be a false statement for a loop-injected direction.
`why` is recorded fact or a labelled purpose — never a synthesized motive.

**Parallel agents:** allowed for reviewing the finished change across
dimensions, and for the Phase 2 model lenses. Not for the six deterministic
lenses — those are plain folds and a subagent would be pure overhead.

**Tests must discriminate.** Include the canary that fails if the injector never
injects, and assert the core claim against the real gate: after an injection,
`safeguards.should_exit` refuses to exit. Keep every new fold total — a
malformed row is skipped, never raised.

**Definition of done — none of these may be skipped or inferred:**

- Full pytest suite green in this worktree, and the real count reported from a
  run you actually read.
- `hive log <category> "<what changed and why>"` recorded.
- A real `hive loop` run that injects at least one task, with its `work_injected`
  event and its `fix_plan.md` line shown.
- The dashboard previewed on a side port from this worktree — never port 54461,
  never via `PYTHONPATH` on the live process — and the served page grepped for
  the new render hooks alongside the pre-existing `showOversee`/`renderOversee`.
  HTTP 200 is not evidence.
- `hive skills trail` showing the injected task's skill row with its honest `why`.
- Anything skipped, untested, or assumed named explicitly. Do not report a
  partial as complete.

**If usage runs out before Phase 1 is done:** leave the relay yourself — record
the roadmap marker with the remaining steps and what was verified, so the next
session resumes from the next step instead of starting cold. Then say plainly
what is left.
