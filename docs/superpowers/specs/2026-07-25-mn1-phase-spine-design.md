# MN-1 — the phase spine (governor core)

> Status: **design, awaiting approval.** Slice 2 of 6 in
> `2026-07-25-mindnetwork-architecture-decomposition.md`.
> Every "already exists" claim is file:line-grounded against this checkout on
> 2026-07-25. Every latency figure is measured on this machine, not estimated.
> Standing constraints (CLAUDE.md + SOPs): stdlib-only; test-first; `hive log`
> every `src/`+`tests/` change-set; land green; append-only ledger.

---

## 1. What MN-1 is, and what it is not

MN-1 converts ADR-0005's methodology from injected prose into a deterministic,
ledger-backed state machine. `directives.py:72-101` already mandates the exact
pipeline; prose drifts, which is the actual complaint. MindNetwork does not
invent a methodology — it replaces that methodology's delivery mechanism.

**MN-1 is additive.** It makes injection *unconditional and phase-aware* — it
always fires and cannot be skipped, which is materially stronger than today's
conditional quality gate (`promptgate.hook_context` returns `{}` on a clear
prompt, `promptgate.py:483-484`). But it cannot refuse a tool call. The ability
to *deny* non-compliant work is MN-4.

Stated plainly so expectations are calibrated: after MN-1 you get
**compulsion-by-always-present-context**. You get **compulsion-by-refusal**
only after MN-4.

### 1.1 Operator decisions this design implements (2026-07-25)

| Decision | Effect here |
|---|---|
| Follow the accepted decomposition, not a blocking controller now | MN-1 additive; no PreToolUse matcher in this slice |
| 8 canonical phase names | §2 legend is normative; no synonyms anywhere |
| Keep ADR-0005's triviality bypass | §2.2 — trivial tasks enter at `execution` |
| MN-0 lands first, via PR | MN-1 rebases onto MN-0; uses its `runlog` for durable evidence |

---

## 2. The legend (normative — `mindnet/phases.py`)

Invariant 10 requires one canonical name set used verbatim in code, UI, and
docs. This table is that source of truth.

```python
PHASES = ("brainstorm", "plan", "prd", "execution",
          "audit-bug", "audit-code", "audit-system", "pr-green")
```

| # | Phase | Objective | Exit evidence | Verified by |
|---|-------|-----------|---------------|-------------|
| 0 | `brainstorm` | Explore intent, constraints, rejected alternatives | design/spec file exists, non-empty | **python** |
| 1 | `plan` | Data structures, contracts, dependencies, language lock | plan file under `docs/superpowers/plans/` | **python** |
| 2 | `prd` | Source-of-truth requirements doc | file under `docs/prds/` | **python** |
| 3 | `execution` | Write code strictly against the PRD | `code_change` event present for this task | **python** |
| 4 | `audit-bug` | Unit/integration tests green | test command exit 0 | agent-reported |
| 5 | `audit-code` | Correctness + maintainability review | review record event | **python** |
| 6 | `audit-system` | Thermo-nuclear / system-level audit cleared | review cleared | agent-reported |
| 7 | `pr-green` | CI green on the PR | `gh pr checks` green | agent-reported |

### 2.1 Evidence kinds, and the honesty rule

`EVIDENCE_KINDS = ("file", "event", "command", "human")`

- `file` — Python stats the path: exists and non-empty. `verified_by="python"`.
- `event` — a ledger event of the required type exists for this task.
  `verified_by="python"`.
- `command` — **the agent ran it and reports the exit code.** Python records
  the claim and stamps `verified_by="agent"`. Python never spawns.
- `human` — operator confirmation. `verified_by="human"`.

**Why `command` is agent-reported and not Python-executed.** KTD6 is locked:
"Python plans; the agent executes — Python never spawns agents"
(`skills/mindhive-orchestrator/references/parallel-orchestration.md:10-12`).
For PR-green specifically the operator already decided agent-executes-`gh`,
respecting the `GITHUB_DENY` posture — `tests/test_research.py:1139-1141`
actively asserts Python never shells out to `gh pr`. So the directive's
"`validation_script` that MUST return exit code 0" becomes: the agent runs it,
Python holds the gate state, and the record says which of the two vouched for
it. A satisfaction stamped `verified_by="agent"` is **never** displayed or
folded as if Python had checked it. That distinction is the whole point of R11
— see §5.

### 2.2 Legal transitions

`controller.can_advance(rec, to_phase) -> (ok: bool, reason: str)`

1. **Forward one step** — `phase_index + 1`. Legal only when the current
   phase's evidence is satisfied.
2. **Backward any distance** — always legal, no evidence required. A failed
   `audit-bug` falls back to `execution`; a design flaw found in `audit-code`
   may fall all the way to `plan`. Falling back is how the machine handles
   failure, so it must never be gated.
3. **Forward skip > 1** — illegal, with one exception:
4. **Triviality bypass** — a task declared `trivial: true` enters at
   `execution`, treating `brainstorm`/`plan`/`prd` as not-required. Scope is
   ADR-0005's verbatim list: one-line fixes, typos, renames, config tweaks,
   doc edits. Recorded on the task, so the bypass is auditable rather than
   implicit. Non-trivial work cannot use it.

---

## 3. Ledger contract (DATA_MODEL.md §6)

Two new event types, because they have genuinely different lifetimes.

### 3.1 `mindnet_phase` → `SEMANTIC_TYPES`

Registry/audit lifecycle, exactly like `roadmap` and `sop`: low volume,
recallable, chain-verified, and it must survive consolidation.

```json
{"action": "enter|satisfy|fall_back|declare",
 "task_id": "kebab-case, <=64 chars",
 "phase": "one of PHASES",
 "trivial": false,
 "evidence": {"kind": "file|event|command|human",
              "ref": "path | event type | command",
              "result": "ok|fail",
              "verified_by": "python|agent|human",
              "detail": "<=500 chars"}}
```

Contract enforced by `mindnet/phases.validate_payload`, dispatched from
`__main__.cmd_append` alongside the existing `roadmap`/`sop`/`code_change`
cases (`__main__.py:65-73` is the pattern). Merge exemptions: add to
`consolidate.CONSOLIDATE_TYPES` **and** `dedupe.collapse` — both, because
those two already drifted apart (§7 known defect 2).

### 3.2 `mindnet_activity` → `DETERMINISTIC_TYPES`

The MN-5 cat feed. High volume, telemetry, off the semantic layer.

```json
{"faculty": "skill-plugin|sop|improvements|history|task-graph|compatibility|pr-green",
 "status": "active|idle",
 "task_id": "…", "phase": "…"}
```

Faculty names are verbatim from the decomposition §3/MN-5 — kebab-case per the
SOP 7.4 tag taxonomy. MN-5 renders these in `swarm_page.py`; MN-1 only emits
them. Per the decomposition's hard constraint the feed is **ledger-derived,
never in-memory** — R16's "a missing cat means a real bug" collapses otherwise.

---

## 4. The hot-path problem, and the sidecar

**Measured on this store (20,128 events): `events.read_all()` = 264 ms.**

MN-1's injection is unconditional — it runs on *every* prompt. A `read_all()`
per prompt would add 264 ms to every prompt and grow without bound. The
existing code already knows this: `enrich.extend_hook_context` documents
"Reads only the settings sidecar (never the ledger — this runs on every gated
prompt)" (`enrich.py:459-460`).

**Design:** the fold's output is cached in a `mindnet/state.json` sidecar
(`rules/triggers.json` precedent — derived, rebuildable data stays out of the
append-only chain). The hook reads only the sidecar. Every `mindnet_phase`
append refreshes it. `hive mindnet rebuild` reconstructs it from the ledger,
so the ledger stays the source of truth and the sidecar is a pure projection
that can be deleted at any time.

This keeps invariant 1 (ledger-first) and invariant 7 (latency honesty)
simultaneously. Target: **< 5 ms added per prompt**, asserted by a test, not
assumed.

---

## 5. R10 and R11 — the two behavioural inversions

### R10: check history before asking the human

`enrich.py:6-16` gathers context in the order **ask-user → check-code →
inject-skills**. MN-1 inverts it:

> **recall history → read code → consult catalog → ask the human last**, and
> only for what none of those could answer.

This is a reordering of existing mechanisms, not new machinery. Asking the
operator something the ledger already answers is the waste this removes.

### R11: the verification duty

Any operator claim checkable against code, ledger, or tests must be checked
before it is acted on. A human claim is never evidence. Unverifiable claims
are *named as unverified* rather than quietly accepted.

MN-1 lands the duty and its audit trail; MN-4 lands the teeth. Concretely
here: the injected block carries a standing verification directive, and every
`satisfy` event records `verified_by`, so the ledger shows exactly which
claims were machine-checked and which were taken on an agent's or a human's
word. **This slice's own history is the worked example** — three claims in the
originating directive ("MN-0 is complete", "contracts.py exists", "promptgate
is additive-only so a blocking controller must be built") were each false or
misleading, and each was caught by a mechanical check rather than a
conversation.

---

## 6. Module layout

New package `src/mindhive/mindnet/` — mirrors the `loop/` precedent, avoids
colliding with the existing `graph.py` (knowledge graph), and keeps everything
out of `dashboard_page.py` (1780 lines, past the thermo-nuclear threshold;
invariant 8 forbids new code there).

| Module | Purpose | Test |
|---|---|---|
| `mindnet/phases.py` | The legend: names, order, evidence descriptors, `validate_payload` | `test_mindnet_phases.py` |
| `mindnet/controller.py` | Fold, transition table, evidence evaluation, sidecar read/write | `test_mindnet_controller.py` |
| `mindnet/inject.py` | Unconditional phase-aware `extend_hook_context` | `test_mindnet_inject.py` |
| `mindnet/telemetry.py` | `mindnet_activity` emission (MN-5 feed) | `test_mindnet_telemetry.py` |

Reused as-is, not reimplemented: the `UserPromptSubmit` hook path, `sop.block()`
(`projector.py:145-147`), `incidents.inject()`, `recommend`/`plugins` for the
catalog, `promptgate`'s `_safe_text` sanitiser and manifest-write idiom.

### 6.1 Wiring — one composition point

`__main__.cmd_hook`'s `user_prompt_submit` branch (`__main__.py:110-125`).
Today everything is inside `if context:`, so a clear prompt prints nothing.
MN-1 lifts the print out so its block fires unconditionally:

```python
from . import enrich, promptgate
from .mindnet import inject as mn_inject
context = promptgate.hook_context(raw)          # conditional (unchanged)
if context:
    context = enrich.extend_hook_context(context, raw)
context = mn_inject.extend_hook_context(context, raw)   # UNCONDITIONAL, fail-open
if context:
    print(json.dumps(context))
promptgate.record_direction(raw)
```

`mn_inject.extend_hook_context` mirrors `enrich.extend_hook_context`'s exact
signature and fail-open contract (`enrich.py:456-484`), with one difference: it
builds a context dict when handed `None`, because it must fire on clear
prompts.

**No hook-registration change.** Registration lives at
`C:/Users/hanif/.claude/settings.json` pointing at the *main* checkout, so
`hooks/*.ps1` edits in a worktree are inert until merged. Every behaviour here
is therefore exercisable via `python -m mindhive hook user_prompt_submit` with
a stdin payload, and that is what the tests assert. The live chain is
integration, not the unit under test.

### 6.2 CLI

`hive mindnet` subparser, mirroring `hive roadmap` (`__main__.py:1622-1696`):
`declare`, `enter`, `satisfy`, `fall-back`, `show`, `list`, `rebuild`.

---

## 7. MN-2 forward-declaration (design only — not built in this slice)

MN-2 is fifth in the build order and depends on MN-3. Recorded here only so
MN-1's event shape does not have to change when MN-2 lands. The `task_id` on
every `mindnet_phase` event **is** the future graph node id — that is the only
coupling MN-1 introduces.

```python
@dataclass
class Task:
    task_id: str                  # kebab-case, ID_RE
    parent_id: str | None         # infinite nesting
    title: str
    description: str
    phase: str                    # one of PHASES
    status: str                   # pending|in_progress|blocked|completed
    dependencies: tuple[str, ...] # all must be completed before in_progress
    language_context: tuple[str, ...]   # permitted languages (R13)
    trivial: bool                 # ADR-0005 bypass
    evidence: tuple[dict, ...]
```

Cycle safety needs **two** independent checks, not one: the `parent_id` chain
must stay a tree (no ancestor loops), and the `dependencies` edges must stay a
DAG. A task must also not depend on its own ancestor or descendant — that
deadlocks without forming a simple cycle in either graph alone, so it is a
third distinct check.

Termination reuses the loop engine's proven machinery rather than a new one:
`safeguards.should_exit`'s dual-condition gate (already refuses to finish while
`required_remaining > 0`, `loop/safeguards.py:6-26`) and its circuit breaker
(`:37-88`). Parallel lanes reuse `clusters.py`'s no-file-in-two-lanes invariant
(`clusters.py:187-194`).

**`roadmap.py` is not replaced.** It has five live consumers — `notice()` →
STATE.md interrupted-plan marker, `/api/oversee`, `autoresume.fold` (joined on
`plan_id`), `hookcapture` → `sync_from_todos` (the TodoWrite mirror), and the
CLI. Replacing it is the exact regression class CLAUDE.md warns about (the
Oversee-section-vanished incident). MN-2 lands as a new additive registry
beside it and may project a roadmap-shaped view for the existing UI.

---

## 8. Test plan (test-first)

1. **Legend** — `PHASES` order stable; every phase has an evidence descriptor;
   no name appears in code/docs that is not in `PHASES` (a real grep assertion,
   so invariant 10 is enforced rather than hoped for).
2. **Contract** — `validate_payload` accepts each valid action and rejects:
   unknown action, unknown phase, bad `task_id`, unknown evidence kind,
   oversized `detail`.
3. **Fold totality** — malformed rows, unknown actions, events for unknown
   tasks, and a `satisfy` with no prior `enter` are all skipped, never raise.
   Standing canary: a historical event lacking every new key still folds.
4. **Determinism** — same events in, same bytes out; no clock/RNG in the fold.
5. **Transitions** — forward+1 with satisfied evidence passes; forward+1 with
   unsatisfied evidence refuses; backward from any phase always passes;
   forward+2 refuses; trivial task enters at `execution`; non-trivial task
   cannot bypass.
6. **Cycle checks** (MN-2 schema, since the checks ship with the dataclass):
   parent loop, dependency cycle, and depend-on-ancestor all rejected.
7. **Injection** — fires on a *clear* prompt (the unconditional property, and
   the thing that distinguishes MN-1 from promptgate); block is size-capped;
   `verified_by="agent"` never renders as machine-verified; fail-open returns
   the input context unchanged on every injected fault.
8. **Latency** — sidecar read path asserted under the budget; a test proves the
   hook path never calls `read_all()`.
9. **Regression** — full suite green (1712 tests on the MN-0 base).

---

## 9. Acceptance criteria (R12 cross-cutting invariants)

A slice is not done until all ten hold. Specific to MN-1:

- [ ] Ledger-first; sidecar is a deletable, rebuildable projection
- [ ] Reads total — folds skip malformed rows, never raise
- [ ] Fail-open — no MindNetwork component can break a prompt, hook, or dashboard
- [ ] Deterministic projections — no clocks/RNG/model calls in any fold
- [ ] Budgeted delivery — injected block bounded by **size**, not just item count
- [ ] stdlib-only; `dependencies` stays `[]`
- [ ] Latency stated as measured, per hook path
- [ ] One module one purpose, each with `tests/test_<module>.py`
- [ ] Every event type follows DATA_MODEL.md §6, including **both** merge exemptions
- [ ] Clear legend — §2 names used verbatim in code, UI, and docs

Plus: `hive log <category> "…"` before the work is called done, and the full
suite green.

---

## 10. Explicitly out of scope for MN-1

Named so nobody expects them from this slice:

- **Any PreToolUse deny.** MN-4. The measured cost is ~395 ms/hook event and
  ~0.8 s per Bash call; a broad policy taxes every tool call and a false
  positive halts real work mid-flight.
- **Amending `directives.py:39-40` and `promptgate.py:477`.** Those two
  additive-only statements get amended in the same change-set that adds the
  teeth — MN-4 — not silently contradicted now.
- **The cat UI.** MN-5 renders; MN-1 only emits the feed.
- **Method memory / "Improvements" keyed on task type.** MN-3.
- **The task graph itself.** MN-2 — §7 is forward-declaration only.
- **Language compatibility enforcement.** MN-4. MN-1 carries
  `language_context` in the schema so MN-4 has somewhere to read it from.
