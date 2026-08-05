# Adaptive work injection — a loop that replenishes its own plan

Design spec. Status: approved in brainstorm 2026-08-05, not yet planned or built.

## The ask

The operator wants a loop that does not stop at "the prompt was executed". When
a run finishes its checklist, the system should decide — from evidence, not from
mood — whether more work is warranted: a repair, a missing verification, a debt
payment, or a genuine innovation. That work becomes new tasks or subtasks, marked
`new` in Oversee because they were not in the original plan. When usage runs out
mid-flight, the whole thing hands off to a later session instead of dying.

Two words in the ask carry weight and are honoured literally:

- **Forced.** The continuation must be structural. A prompt that begs the agent
  to keep going is not this feature.
- **Adaptive.** How much gets injected, and of what kind, must follow what the
  ledger actually contains that round — never a fixed schedule or a fixed count.

## What already exists (verified in this checkout)

The structural hook the feature needs is already load-bearing:

- [`loop/engine.py:199`](../../../src/mindhive/loop/engine.py) computes
  `required_remaining = task_mod.required_remaining_count(tasks.fix_plan)` and
  passes it to `safeguards.should_exit`.
- [`loop/safeguards.py:24`](../../../src/mindhive/loop/safeguards.py) —
  `if required_remaining > 0: return False, "required_items_remain"`. An
  `EXIT_SIGNAL: true` plus every completion heuristic in the reply cannot
  override it.
- [`loop/task.py:53`](../../../src/mindhive/loop/task.py) —
  `required_remaining_count` counts unchecked `- [ ]` lines appearing **before**
  any `# Future` / `# Optional` heading.

So injection is: append a checklist item to `.hive-loop/fix_plan.md` above the
Future heading, before the exit gate is evaluated. The loop then cannot exit.
This satisfies SOP `agentic-loops` §1 (termination is structural, never
linguistic) rather than working around it.

Other parts already built and reused as-is:

| Piece | Where | Reused for |
|---|---|---|
| Subtask proposal by an agent, with hard validation | `mindnet/decompose.py` (MN-11) | the innovation lens; `validate()` rules are reused verbatim |
| Parallel agent waves in isolated worktrees | `mindnet/orchestrator.py`, `agent.py`, `worktree.py` (MN-7/8/9) | running model-driven lenses concurrently |
| Failure → signature → incident → prevention rule | `failures.py`, `signature.py`, `incidents.py`, `reflect.py`, `resolve.py` | the repair lens, and learning from injected work that failed |
| Skill trail with an honest causal "why" | `oversee.skill_trails`, `skill_directed`, `hive skills trail`, `/api/skills/trail` | recording which skill served each injected task and why |
| Headless skill invocation with ledger outcome | `invoker.invoke_skill` | driving a skill for an injected task |
| Out-of-usage relay marker | `engine._snapshot_interrupted_plan` → roadmap `set` + `interrupt` → `autoresume.schedule` | the estafet; extended, not replaced |

## What does not exist

1. Nothing ever **adds** to `fix_plan.md`. The list only shrinks.
2. `decompose.py` is operator-triggered and dry-run by default, and it only
   breaks a task down — it never proposes repair, debt, or innovation work.
3. Checklist items carry no provenance, so "from the original plan" and "injected
   later" are indistinguishable — there is nothing for an Oversee `new` badge to
   read.
4. Interactive sessions have no relay marker at all. Only `hive loop` gets one.
5. The failure loop is not wired to the loop: a recorded `tool_failed` never
   becomes a task.

## Architecture

New module `src/mindhive/replenish.py` — top-level, because two surfaces call it
(the headless loop and `session_end`). New event type `work_injected`.

```
                    round trigger
                          |
        +-----------------+------------------+
        | deterministic lenses (inline)      | model lenses (parallel agents)
        | repair / evidence-gap / debt /     | innovation / system-improvement /
        | audit / caveat / coverage          | user-need drift
        +-----------------+------------------+
                          |
                    candidates
                          |
              value gate: evidence required,
              scored, fingerprint-deduped
                          |
        +-----------------+------------------+
        | fix_plan.md: "- [ ] (new) <title>" | mindnet_task declare (subtask)
        | -> required_remaining > 0          | -> hierarchy in MN-2
        +-----------------+------------------+
                          |
                  work_injected event
                    (audit trail)
```

### 1. Trigger

**Loop (phase 1).** In `engine.run_loop`, immediately before `should_exit`, and
only when `required_remaining == 0` and the parsed status claims completion.
That is exactly "the prompt's execution finished → mint innovation/repair work".

**Interactive session (phase 2).** `hooks/session_end.ps1` already runs `review
queue`, `log queue` and `roadmap autoclose`; add a `hive replenish queue` leg
there. Deliberately **not** the Stop hook: `../../../CLAUDE.md` requires that hook
to stay fail-open and opt-in, and it fires in every Claude Code session on this
machine.

The trigger is not a schedule. A round that mines no qualifying candidate injects
nothing and says so.

### 2. Lenses

Six deterministic lenses, plain folds over the ledger and the tree — cheap, no
model, run inline:

| Lens | Evidence source | Produces |
|---|---|---|
| `repair` | `tool_failed` → `signature` → `incidents` rows with no resolution | a fix task |
| `evidence-gap` | latest `verification` event red, or absent since the last `code_change` | a verification task |
| `debt` | thermo-nuclear review queue; source files past the 1000-line threshold (`__main__.py` is 3396) | a reduction task |
| `audit` | source files touched with no `code_change` entry | a change-log task |
| `caveat` | unresolved caveats tagged `pinned` | a risk task |
| `coverage` | `src/mindhive/` modules with no counterpart under `tests/` | a test task |

Three model lenses, run **concurrently** through the `mindnet/agent.py` runner
seam (the same seam `decompose.run` uses, so they inherit its durable transcript
and outcome mapping): `innovation`, `system-improvement`, `user-need-drift`.
These are the only place a model is spawned, and the only place parallelism is
worth its cost.

### 3. Value gate

- A candidate without `evidence` (a pointer to a real ledger row, file, or
  signature) is refused. This is the rule that keeps the loop from inventing
  work to fill a quota.
- Score from: recurrence count, blocking severity, freshness, and whether a
  prevention rule already covers it.
- Fingerprint dedup across rounds **and across sessions**, using the canonical
  signature idiom in `signature.py`.
- Model output is untrusted input. Validation reuses `decompose.validate`
  unchanged: `tasks.ID_RE`, char caps, id collision, `MAX_DEPTH`, `MAX_NODES`,
  and a `check_graph` probe holding all proposals at once. Do not reimplement
  these; `tasks.fold` drops an unsound edge **silently**, so a half-landed
  decomposition would report success and leave gaps.

### 4. Landing

Two axes, both required:

- **Checklist axis.** Append `- [ ] (new) <title>` to `fix_plan.md` above the
  `# Future` heading. This is what forces continuation.
- **Graph axis.** Append `mindnet_task` `{action: "declare", task_id, title,
  parent_id}` plus the matching `mindnet_phase` `declare` — a task declared on
  the graph alone orphans every phase event it later receives (`controller.fold`
  is create-only). `decompose.declare` already does both; follow it.

### 5. Event contract — `work_injected`

Registered in `events.py` `DETERMINISTIC_TYPES`, per DATA_MODEL.md §6.

```json
{"run_id": "…", "session_id": "…", "lens": "repair",
 "title": "…", "evidence": ["…"], "score": 0.0,
 "round": 1, "task_id": "…", "fingerprint": "…", "ambition": "balanced"}
```

Deterministic, not semantic, for the same reason `loop_iteration` and
`skill_directed` are: this is the injector's audit trail, machine-generated, and
potentially many rows per run. Semantic membership would put a LEDGER markdown
file, a Memory-view row and a recall-corpus entry on disk per injection. The
*task itself* is already semantic — it lands as `mindnet_task`, which rides the
LEDGER, the chain and recall. Audit/registry types never merge (SOP
`ledger-schema` §3), so `work_injected` is exempt from consolidation.

### 6. The `new` badge in Oversee

Oversee renders two card kinds and both need the marker.

**Loop cards.** `loop/state._fix_plan_items` calls `task.plan_items`, which
today returns `{"done": [...], "planned": [...], "not_yet": [...]}` as lists of
**strings**. Carrying provenance changes that return shape, and two other callers
consume it: `rmFromCard` in the dashboard, and `roadmap.steps_from_plan_items`
via `engine._snapshot_interrupted_plan`. This is the highest-risk change in the
spec and must be made deliberately, with every caller updated in the same
change-set.

**Chat cards.** `oversee.interactive_sessions` builds `planned` from `state_set`
and `next_step`. Injected items arrive by folding `work_injected`.

**Rendering.** A `new` pill ahead of the title in `ovGroup`/`ovRow`, and a `new`
status in the roadmap panel (`rmGlyph`/`rmPanel`).

**Lifetime.** The badge is **permanent** — it does not clear when the item is
checked. It means "not in the original plan", not "recently added". That is what
the ask asked for.

### 7. Skill selection, and keeping the trail honest

Per injected task:

1. Pick a skill deterministically — `recommend` scoring, `plugins.call_status`
   green/red history, and `oversee.consumed_directions` (was a past suggestion
   actually followed?).
2. Append `skill_directed` **before** the invocation, carrying `measured` N/M so
   the trail can read "(measured N/M)". The payload needs a new `source` field:
   the existing `why` string says "directed by prompt-gate", which would be a
   **false statement** for a loop-injected direction. `oversee._skill_why` grows
   one branch keyed on `source`.
3. Invoke through `invoker.invoke_skill` → `invoke_result` on success,
   `tool_failed` with signature `skill:<name>` on failure.
4. A failure flows into `reflect` and becomes a prevention rule — and is visible
   to the `repair` lens on the next round. That is the learning loop closing on
   itself.

The honesty rule from the skill-trail work holds without exception: `why` is
recorded fact or a labelled purpose. Never a synthesized motive.

### 8. The relay (estafet)

`engine._snapshot_interrupted_plan` already fires on `api_cap` and `budget_cap`,
writes roadmap `set` + `interrupt`, and calls `autoresume.schedule`. Four gaps:

1. Injected items already reach the snapshot (steps come from
   `plan_items(fix_plan)`), but the `new` flag is dropped by
   `roadmap.steps_from_plan_items`. Carry it.
2. **Injector state must ride the baton**: used fingerprints, refused candidates
   and their reasons, the round counter, the dry-round count. Without it the next
   session re-mines and re-proposes the same refused work. The source is a fold
   over `work_injected` — no new sidecar; the ledger stays the source of truth.
3. Interactive sessions get the same marker: `session_end` writes a roadmap
   `interrupt` when injected work is still outstanding.
4. `autoresume` delivery must be **proved to fire**, not assumed. `hive
   autoresume list` / `fire` / `wait` exist; exercise them.

### 9. Termination

Normal exit: `dry_rounds >= 2` — two consecutive replenish rounds producing zero
candidates above the gate. Unchanged hard ceilings: `max_iterations`,
`max_hours`, circuit breaker, budget gate, `api_cap`. New caps, because a bug in
the scorer must not mean unbounded work:

- `max_injections_per_run`
- `max_injection_rounds`
- `max_injection_depth` — an injected task may not inject past depth K.

### 10. Configuration — the flexibility surface

A `[replenish]` section in `.hive-loop/loop.toml`, loaded by `loop/config.py`
with the same "fall back to defaults for any missing key" contract:

```toml
[replenish]
enabled = true
ambition = "balanced"      # conservative | balanced | ambitious
min_score = 0.5
max_injections_per_run = 12
max_injection_rounds = 4
max_injection_depth = 2
dry_rounds_to_exit = 2
adaptive_ambition = false   # phase 2
```

`ambition` moves three things at once: the score threshold, which lenses run (the
model lenses are off at `conservative`), and the caps. In phase 1 only the first
and third are live, because the model lenses do not exist yet — the lens gating
switch turns on with them, in phase 2.

`adaptive_ambition` is the non-static part, and belongs to phase 2: ambition
rises one step when the last injected tasks landed green, and falls one step
after repeated failure. Bounded to the three named levels, and every change is
an event — never a silent drift. It ships defaulting to `false` in phase 1 so
the key exists but changes nothing.

## Phasing

**Phase 1 — the loop.** `replenish.py`, the six deterministic lenses, the value
gate, both landing axes, `work_injected`, the `new` badge on loop cards, the
`[replenish]` config, the caps, the relay extension. Ships green, on its own PR.

**Phase 2 — interactive sessions and the model lenses.** The `session_end` leg,
the chat-card badge, the chat relay marker, the three parallel model lenses, and
`adaptive_ambition`.

Phase 1 is useful alone. Phase 2 without phase 1 is not.

## Test surface

- `plan_items` provenance parsing, including a line that literally contains the
  word "new" but is not a marker.
- `should_exit` refuses to exit after an injection (the core claim — assert on
  the real gate, not on a stub).
- A candidate with no evidence is refused.
- Fingerprint dedup holds across a simulated session boundary.
- `work_injected` fold is total: a malformed row is skipped, never raised
  (SOP `ledger-schema` §5).
- Caps hold: injection stops at `max_injections_per_run` even when candidates
  remain, and the fact is logged rather than silently truncated.
- The relay: an `api_cap` run with injected items produces a roadmap `interrupt`
  whose steps keep the `new` flag.
- A canary that fails if the injector never injects — otherwise every test above
  passes against a no-op.

## Definition of done

Per `../../../CLAUDE.md` and SOP `change-audit`:

- Full pytest suite green in this checkout.
- `hive log <category> "…"` recorded for the change-set.
- Real flow exercised and its own output read: a real `hive loop` run that
  injects at least one task, the served dashboard page grepped for the new
  render hooks (not an HTTP 200), and `hive skills trail` showing the injected
  task's skill row with its honest `why`.
- New event type documented in `docs/DATA_MODEL.md` per §6.
- Anything skipped or assumed is named explicitly.

## Deliberately not in scope

- No new sidecar file; the `work_injected` fold is sufficient.
- No rewrite of `decompose.py`. It is called, not replaced.
- No change to the Stop hook or the rotation machinery.
- No auto-injection into someone else's project namespace.
