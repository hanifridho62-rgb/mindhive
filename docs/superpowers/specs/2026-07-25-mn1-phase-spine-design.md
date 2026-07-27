# MN-1 — the phase spine (governor core)

> Status: **implemented (2026-07-27), with five recorded amendments.** Slice 2
> of 6 in `2026-07-25-mindnetwork-architecture-decomposition.md`.
> Where the shipped code and this document disagreed, the disagreement is
> written down in §0 rather than left for a reader to trip over — that is this
> document's own §10 rule ("amended in the same change-set, not silently
> contradicted") applied to itself.
> Every "already exists" claim is file:line-grounded against this checkout on
> 2026-07-25. Every latency figure is measured on this machine, not estimated.
> Standing constraints (CLAUDE.md + SOPs): stdlib-only; test-first; `hive log`
> every `src/`+`tests/` change-set; land green; append-only ledger.

---

## 0. Amendment log (2026-07-27, MN-1 U8)

Five places where the design as written and the design as built disagreed. Each
is amended IN PLACE below and recorded here, so the contradiction has one
resolution instead of two coexisting statements.

| # | Where | Was | Is, and why |
|---|---|---|---|
| A1 | §2 row 4 + §2.1 | `audit-bug` "agent-reported"; §2.1 stated flatly "Python never spawns" | **Python executes it, behind a fail-closed gate.** `hive mindnet validate` runs the task's `validation_script` as argv (`shell=False`, `posix=False`, timeout, `runlog` capture) and stamps `verified_by="python"` on the result. The two statements could not both survive: the operator authorised free-form validation execution, and a `command` phase that Python may not run has no python-verifiable evidence at all. What is preserved is the *reason* the old rule existed — see A1b. |
| A1b | §2.1 | — | The `gh` / `git push` denylist and the fail-closed classifier are what carry KTD6's intent forward. `tests/test_research.py` still asserts Python never shells out to `gh pr`, and `executor.py` refuses that argv explicitly rather than relying on nobody writing it. |
| A2 | §2 row 5 | `audit-code` verified by **python** | **agent-reported** (KTD17). No review event type is registered in `events.py` and `hive review clear` (`review.py:121-126`) unlinks a sidecar and appends nothing — there is no artifact Python could read, so a `python` claim here would be exactly the lie §2.1's honesty rule exists to prevent. A `review_cleared` producer is its own slice. `audit-code` also takes `event` evidence, not `command`. |
| A3 | §2.2, §3.1, §7 | `trivial` | **`triviality_bypass`**. The shipped field name, in `tasks.Task`, the `mindnet_task` payload, and the CLI flag. `trivial` was never written to a ledger row. |
| A4 | §2 rows 0–2 | `brainstorm` had no directory constraint | **`brainstorm` requires `docs/superpowers/specs/`.** With no directory, *any* non-empty file satisfied it — including `mindnet/state.json`, the machine's own sidecar. Related widening in the same table: `plan` accepts **two** directories, `docs/plans/` and `docs/superpowers/plans/`, because this repo genuinely has both and a single-directory rule would have left this machine unable to validate its own plan file. Containment (`Path.is_relative_to` on resolved paths), never a string prefix. |
| A5 | §7, and the decomposition's build order | MN-2 was "fifth in the build order and depends on MN-3"; §7 was forward-declaration only | **MN-2 shipped BEFORE MN-1.** The phase spine needs a task to hang a phase on, so `mindnet/tasks.py` is a real module with its own suite, not a sketch. §7's dataclass is superseded by the shipped `tasks.Task`; its three cycle checks all shipped. |

Two smaller corrections, folded into the sections themselves rather than given
rows: `verified_by` is a **top-level** field of the `mindnet_phase` payload, not
a member of `evidence` (§3.1 showed it nested — a self-attesting shape KTD2
forbids); and §6's module table omitted `sidecar.py`, `executor.py` and
`tasks.py`, while §6.2's command list omitted `validate` and `restat`.

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

| # | Phase | Objective | Evidence kind | Exit evidence | Verified by |
|---|-------|-----------|---------------|---------------|-------------|
| 0 | `brainstorm` | Explore intent, constraints, rejected alternatives | `file` | a file **contained in** `docs/superpowers/specs/` (A4) | **python** |
| 1 | `plan` | Data structures, contracts, dependencies, language lock | `file` | a file contained in `docs/plans/` **or** `docs/superpowers/plans/` (A4) | **python** |
| 2 | `prd` | Source-of-truth requirements doc | `file` | a file contained in `docs/prds/` | **python** |
| 3 | `execution` | Write code strictly against the PRD | `event` | a `code_change` event carrying this `task_id` (KTD16) | **python** |
| 4 | `audit-bug` | Unit/integration tests green | `command` | `validation_script` exit 0, captured to a runlog artifact (A1) | **python** |
| 5 | `audit-code` | Correctness + maintainability review | `event` | a review record — no such event type exists yet (A2) | agent-reported |
| 6 | `audit-system` | Thermo-nuclear / system-level audit cleared | `command` | review cleared | agent-reported |
| 7 | `pr-green` | CI green on the PR | `command` | `gh pr checks` green — run by the agent; the executor **refuses** this argv (A1b) | agent-reported |

### 2.1 Evidence kinds, and the honesty rule

`EVIDENCE_KINDS = ("file", "event", "command", "human")`

- `file` — Python resolves the path against the worktree root and requires it
  to be **contained** in one of the phase's required directories (never a
  string prefix: `docs/prds/../../../etc/passwd` passes a `startswith` test).
  `verified_by="python"`.
- `event` — a ledger event of the required type exists for this task.
  `verified_by="python"`.
- `command` — **Python runs it, behind a fail-closed gate** (amended, A1). See
  below.
- `human` — operator confirmation at an interactive prompt.
  `verified_by="human"`.

**Why `command` is Python-executed, and what that cost.** *(Amended A1 — the
original text here read "the agent ran it and reports the exit code… Python
never spawns", which the shipped executor contradicts. The operator authorised
free-form validation execution, and the reason is structural: a `command` phase
Python may not run has no python-verifiable evidence at all, so `audit-bug`
would have been agent-reported forever and the honesty rule would have had
nothing left to distinguish.)*

`hive mindnet validate` splits the stored `validation_script` with
`shlex.split(cmd, posix=False)` and runs the argv with `shell=False`, an
explicit timeout, `paths.NO_WINDOW`, and output captured through MN-0's
`runlog` — the `research.run_experiment` idiom. `posix=False` is load-bearing on
Windows: POSIX-mode `shlex` eats backslashes as escapes, silently turning
`pytest C:\Users\…` into `pytest C:Users…` and recording a false `fail`.

What survives from KTD6 ("Python plans; the agent executes") is its actual
subject — **`gh`**. `executor.py` refuses, by explicit denylist, any argv whose
program resolves to `gh` or to `git push`, so the `GITHUB_DENY` posture and
`tests/test_research.py`'s standing assertion that Python never shells out to
`gh pr` are enforced rather than merely restated. `pr-green` therefore stays
agent-reported (table row 7).

Both refusals — the destructive-command classifier and the denylist — **fail
closed**: any reason string refuses, and any exception raised *inside* the
classifier also refuses. This deliberately inverts `enforce.evaluate_policies`
(`except Exception: return None` → allow), which is fail-open because it is
advisory. Fail-open is right for a hint and dangerous for an execution gate.

A refusal must not wedge a task, so `audit-bug` remains satisfiable by
`hive mindnet satisfy --agent-reported`, whose `detail` MUST carry the refusal
reason verbatim. A classifier false positive therefore costs an honest downgrade
rather than a deadlock — and a satisfaction stamped `verified_by="agent"` is
**never** displayed or folded as if Python had checked it. That distinction is
the whole point of R11 — see §5.

### 2.2 Legal transitions

`controller.can_advance(rec, to_phase) -> (ok: bool, reason: str)`

1. **Forward one step** — `phase_index + 1`. Legal only when the current
   phase's evidence is satisfied.
2. **Backward any distance** — always legal, no evidence required. A failed
   `audit-bug` falls back to `execution`; a design flaw found in `audit-code`
   may fall all the way to `plan`. Falling back is how the machine handles
   failure, so it must never be gated.
3. **Forward skip > 1** — illegal, with one exception:
4. **Triviality bypass** — a task declared `triviality_bypass: true` *(amended
   A3: the field was called `trivial` here and shipped as
   `triviality_bypass`)* enters at `execution`, treating
   `brainstorm`/`plan`/`prd` as not-required, and its terminal gate is
   `audit-bug` rather than `pr-green`. Scope is ADR-0005's verbatim list:
   one-line fixes, typos, renames, config tweaks, doc edits. Recorded on the
   task, so the bypass is auditable rather than implicit. Non-trivial work
   cannot use it, and three further rules make that stick: the flag is honoured
   on the **first declare only** (a later re-declare setting it was a
   completion skeleton key), clearing it on a task already at or past
   `execution` is refused, and the trivial entry itself is refused when any
   bypassed phase already carries a satisfaction.

---

## 3. Ledger contract (DATA_MODEL.md §6)

Two new event types, because they have genuinely different lifetimes.

### 3.1 `mindnet_phase` → `SEMANTIC_TYPES`

Registry/audit lifecycle, exactly like `roadmap` and `sop`: low volume,
recallable, chain-verified, and it must survive consolidation.

```json
{"action": "declare|enter|satisfy|fall_back",
 "task_id": "kebab-case, <=64 chars",
 "phase": "one of PHASES (omitted only on declare)",
 "epoch": 0,
 "verified_by": "python|agent|human",
 "detail": "<=2000 chars (REQUIRED on fall_back)",
 "evidence": {"kind": "file|event|command|human",
              "ref": "worktree-root-relative path | runlog artifact | command",
              "result": "ok|fail",
              "detail": "<=2000 chars"}}
```

Three corrections against the shape first drafted here. `verified_by` is
**top-level, not inside `evidence`** — nesting it inside the object the caller
supplies is the self-attestation KTD2 exists to stop. `epoch` is required on
`satisfy`: a satisfaction not bound to the task's current `fall_back`
generation is re-usable forever. And `trivial` is not a field of this payload
at all (A3) — the bypass lives on the `mindnet_task` axis, and the entry phase
is *derived* by the fold rather than named by a caller, because a caller-named
entry phase is precisely the teleport MN-2 removed.

Contract enforced by `mindnet/phases.validate_payload` (the append surface,
which refuses `verified_by`) and `mindnet/phases.validate_stored` (the writer,
which requires it), dispatched from `events._PAYLOAD_CONTRACTS` — the dispatch
table now lives beside the type registry so the CLI and the dashboard's
`POST /api/append` cannot enforce different contracts, which is how they
drifted before. Merge exemptions: `consolidate.NEVER_CONSOLIDATE` **and**
`dedupe.NEVER_COLLAPSE` — both, because those two already drifted apart, on
`learning` and `state_set` as well as here.

`mindnet_phase` also needs a `ledger_body(payload)` (it has, in `phases.py`).
Its payload carries no `text`, so without one every phase event would project
an empty LEDGER body and two events in the same second would hash to the same
filename.

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

As built (the table originally listed four modules and omitted three):

| Module | Purpose | Test |
|---|---|---|
| `mindnet/tasks.py` | MN-2's graph: the `PHASES` legend, `Task`, the fold, triple-threat cycle detection, `ledger_body` | `test_mindnet_tasks.py` |
| `mindnet/phases.py` | Evidence descriptors, ref containment, the two-surface contract, `ledger_body` | `test_mindnet_phases.py` |
| `mindnet/controller.py` | Fold, transition table, evidence evaluation, `restat` | `test_mindnet_controller.py` |
| `mindnet/sidecar.py` | `mindnet/state.json`: the freshness stamp, the pid-scoped write, `rebuild` | `test_mindnet_sidecar.py` |
| `mindnet/inject.py` | Unconditional phase-aware `extend_hook_context` | `test_mindnet_inject.py` |
| `mindnet/executor.py` | The validation runner: argv, fail-closed gate, `gh`/`git push` denylist | `test_mindnet_executor.py` |
| `mindnet/telemetry.py` | `mindnet_activity` emission (MN-5 feed) | `test_mindnet_telemetry.py` |

`PHASES` lives in `tasks.py` and is **imported** by `phases.py`, never
redefined — invariant 10, one legend one home. Sidecar read/write was split out
of `controller.py` rather than left inside it, because the hook path must reach
the sidecar without importing the fold it exists to avoid running.

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

`hive mindnet` subparser, mirroring `hive roadmap` (`__main__.py:2225-2288`):
`declare`, `enter`, `satisfy`, `fall-back`, `validate`, `restat`, `show`,
`list`, `rebuild`. As built this is nine subcommands, not seven — `validate`
(run the script behind the gate) and `restat` (re-check file evidence) were
missing from the original list, and both are impure writers that must be
explicit commands rather than side effects of `show`.

Two deliberate omissions in the parser: no `--verified-by` flag anywhere (KTD2
— that field is derived by whatever check actually ran), and no argparse
`choices=PHASES` on the phase arguments (a second copy of the legend is a place
for it to drift, and it would swap a reason the operator can act on for
argparse's bare "invalid choice").

`declare` writes **both** axes — a `mindnet_task` event and a `mindnet_phase`
one. See DATA_MODEL.md §4.1 for why a task on only one axis is broken.

---

## 7. MN-2 — ~~forward-declaration~~ **shipped first** (amended A5)

*Amended A5.* This section was written as forward-declaration: "MN-2 is fifth
in the build order and depends on MN-3." It shipped **before** MN-1 instead,
because the phase spine needs a task to hang a phase on and the alternative was
a state machine with no state to govern. The build order in
`2026-07-25-mindnetwork-architecture-decomposition.md` is amended to match.

`mindnet/tasks.py` is therefore a real module with its own suite, not a sketch,
and the sketch below is superseded by the shipped `Task`. The `task_id` on
every `mindnet_phase` event **is** the graph node id — still the only coupling
between the two axes.

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
    triviality_bypass: bool       # ADR-0005 bypass (A3: was `trivial` here)
    evidence: tuple[dict, ...]
```

The shipped record differs in three ways worth knowing before reading the code:
it is `@dataclass(frozen=True)` and rebuilt with `dataclasses.replace` (so two
folds can never alias each other's state); `evidence` is a tuple of runlog
artifact **paths**, not dicts; and it carries two derived fields the sketch has
no room for — `depth` (recomputed in one memoized pass at the end of every
fold) and `violations` (the refusals the fold made against this record, which
is how the projection can explain why it disagrees with an immutable ledger).

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
