# MindNetwork — architecture & decomposition

> Status: **decomposition accepted; MN-0 specced separately and ready for a plan.**
> Scope: this document is the system-level design for MindNetwork and the
> authoritative decomposition into six independently shippable slices. It is
> **not** an implementation plan. Each slice gets its own spec → plan →
> implementation cycle; MN-0's spec is
> `2026-07-25-mn0-parallel-output-durability-design.md`.
> Every "already exists" claim below is file:line-grounded against the
> checkout at `C:/Users/hanif/mindhive` on 2026-07-25.
> Standing constraints (CLAUDE.md + SOPs): Python package stays **stdlib-only**;
> test-first; every `src/`+`tests/` change-set gets `hive log`; land green;
> never repoint the live dashboard via PYTHONPATH; append-only ledger — never
> edit `events/*.jsonl`.

---

## 1. What MindNetwork is

MindNetwork is the governing brain over every phase of building code in this
repo — brainstorm → plan → PRD → execution → bug audit → code audit → system
audit → PR green — enforced by the system rather than requested by prose, and
observable in MindSwarm so the operator can see which faculty is awake.

### 1.1 The founding insight: the spine already exists, as prose

`docs/adr/0005-superpowers-as-default-methodology-spine.md` (Accepted
2026-06-24) **already mandates the exact pipeline** MindNetwork is being asked
to enforce:

> brainstorming (gate) → writing-plans → subagent-driven-development → TDD →
> verification-before-completion → requesting-code-review; with
> using-git-worktrees and dispatching-parallel-agents.

plus a triviality bypass, systematic-debugging as the default debug path, an
"approved plan in an isolated worktree is batched consent" execution envelope,
and a close-out chain ending at branch-finish/PR.

It ships as **injected directive prose** — `directives.py:72-101`
(`OPERATING_SPEC` §8), printed verbatim at SessionStart by `hive directives`.

Prose drifts. That drift is the actual complaint MindNetwork answers, and the
project's own SOP 1.2 already states the remedy:

> "a rule that must always hold goes into a hook, prompts drift, hooks don't."

**Therefore MindNetwork is not a new methodology. It is the conversion of an
already-decided methodology from prose into a deterministic, ledger-backed,
observable state machine.** Everything below follows from that framing: reuse
the spine, replace its delivery mechanism.

### 1.2 The policy conflict, resolved

Two current statements directly contradict a forcing controller:

- `directives.py:39-40` — "mindhive recommends skills and plugins; it does not
  auto-execute them."
- `promptgate.hook_context` docstring (`promptgate.py:477`) — "Additive only —
  never a block decision."

**Decision (operator, 2026-07-25): hybrid enforcement.** Mandatory injection at
`UserPromptSubmit` (once per prompt, effectively free) plus **narrow** hard
denies at `PreToolUse` for tightly-defined, mechanically-checkable violations
only. Both statements above must be amended by MN-4, in the same change-set
that adds the teeth — not silently contradicted.

**What this means for expectations, stated plainly:** MN-1 makes injection
*unconditional and phase-aware* — it always fires and cannot be skipped, which
is materially stronger than today's conditional quality gate. But it remains
**additive**: it cannot block a tool call, so it stays inside the current
policy and needs no amendment. The ability to actually *refuse* non-compliant
work arrives only with MN-4. Anyone reading R6/R8's "forcibly" should expect
compulsion-by-always-present-context after MN-1, and compulsion-by-refusal only
after MN-4.

Rationale for "narrow": measured hook latency is ~395 ms per event / ~0.8 s per
Bash tool call (`docs/prds/2026-07-21-002-rust-tool-interception-prd.md` §0.2,
live-measured 2026-07-21). A broad PreToolUse policy taxes every tool call and
a false positive halts real work mid-flight. Prompt-time forcing costs one
event per prompt and cannot deadlock the operator.

---

## 2. Requirement → slice map

Every requirement from the originating request, so none is lost. R12 is
cross-cutting and becomes acceptance criteria on all slices.

| # | Requirement | Slice |
|---|---|---|
| R1 | Orchestrate all 8 phases as one brain | MN-1 |
| R2 | Task→subtask decomposition, exhaustive, dynamically growable | MN-2 |
| R3 | Nothing is done until the PR is green | MN-2 + MN-4 |
| R4 | Lives in the MindSwarm section | MN-5 |
| R5 | Auto-injects on any of the 8 activities | MN-1 |
| R6 | Forcibly sharpens the prompt (goal, design, roadmap, direction, execution, function) | MN-1 |
| R7 | Opens the Mind Hive skill/plugin catalog against the prompt's needs | MN-1 |
| R8 | Forces compliance with the SOP section | MN-1 |
| R9 | Subject to Improvements: a method that failed before must be replaced | MN-3 |
| R10 | Check history **before** asking the user | MN-1 |
| R11 | Validate human claims — never trust | MN-1 (duty) + MN-4 (teeth) |
| R12 | Production-grade: clear architecture, function, purpose, human-readable, matches intent, flexible, non-spaghetti, clear roadmap, clear legend | cross-cutting |
| R13 | Language compatibility gate; never emit foreign syntax; check first | MN-4 |
| R14 | Forced parallelism when usage allows — **but fix the parallel bugs first** | MN-0 (fix) → MN-2 (use) |
| R15 | Cat per running faculty: label, light animation, click→detail, idle→vanish | MN-5 |
| R16 | Visual on/off tracking so missing info reveals bugs | MN-5 |

---

## 3. The six slices

Build order is **MN-0 → MN-1 → MN-5 → MN-3 → MN-2 → MN-4**. MN-0 first by
operator decision (R14's own precondition). MN-5 is pulled early — right after
the governor emits its first stage events — so the system becomes *visible*
before it becomes large; a governor you cannot see is a governor you cannot
debug (R16).

### MN-0 — Parallel output durability *(first; specced separately)*

Fixes the pre-existing defect that makes parallel work untrustworthy: agent
output is discarded on the paths that matter. Verified defects and the full
design are in `2026-07-25-mn0-parallel-output-durability-design.md`.

Independently valuable: it stops silently burning tokens on work whose output
is then thrown away, whether or not the rest of MindNetwork ever ships.

### MN-1 — The phase spine (governor core)

Converts ADR-0005's prose into a deterministic state machine.

New:
- **A phase axis.** `promptgate`'s five `DIMENSIONS` (`promptgate.py:94-126`:
  `too-short`, `vague-verb`, `dangling-reference`, `ambiguity-markers`,
  `method-skip`) are *quality* axes. Phase is an orthogonal, new axis:
  `brainstorm | plan | prd | execution | audit-bug | audit-code |
  audit-system | pr-green`.
- **A phase fold** over new ledger events, so "which phase is this work in, and
  what has it satisfied" survives restarts and compaction.
- **Precedence inversion (R10).** `enrich.py` gathers context in the order
  ask-user → check-code → inject-skills (`enrich.py:6-16`). MindNetwork
  inverts it: **recall history → read code → consult catalog → ask the human
  last, and only for what none of those can answer.** This is a reordering of
  an existing mechanism, not a new one.
- **A verification duty (R11).** Any operator claim checkable against code,
  ledger, or tests must be checked before it is acted on; a human claim is
  never evidence. Unverifiable claims are named as unverified rather than
  quietly accepted. Teeth land in MN-4; the duty and its audit trail land here.

Reused as-is: the `UserPromptSubmit` hook path, `promptgate`'s skip predicate
and trained-manifest machinery (`rules/triggers.json`), `sop.block()` injection
(`projector.py:145-147`), `incidents.inject()` prevention rules, and
`recommend`/`plugins` for R7's catalog.

Latency: rides one already-registered hook. No new per-tool-call cost.

### MN-5 — The cat status board *(pulled early)*

MindSwarm gains one cat per **awake MindNetwork faculty** — Skill & Plugin,
SOP, Improvements, History, Task Graph, Compatibility, PR-Green — each with its
name above its head, a light idle animation, click→detail, and vanishing when
idle.

Nearly all of it already exists and must be reused, not reinvented:
- Sprites: `CAT_GRID` 12×12, `CAT_PALETTES` (6), `catFor(id)`, `catSVG(idx,size)`
  — `swarm_page.py:129-160`, no image files.
- Click→detail: the `#insp` inspector (`swarm_page.py:97-106`) filled by
  `pollInspector()` (`:307-328`) — the same affordance the request compares to
  the graph section.
- Idle→vanish: Oversee's two server-side windows —
  `ONLINE_WINDOW_SECONDS = 180.0` (badge off, card stays) and
  `ACTIVE_WINDOW_SECONDS = 6*3600.0` (card disappears) — `oversee.py:30-31`.
- Animation: the single house keyframe `@keyframes pulse`
  (`dashboard_page.py:35`), and for anything richer, `graphview.py`'s
  **self-halting** rAF pattern (`tick`/`kick`, `:636-681`) so an idle board
  costs no CPU.

Two hard constraints:
1. **The feed must be ledger-derived, not in-memory.** MindSwarm's mission
   state is a plain instance attribute (`workspace.py:472`) that dies with the
   server. R16's entire purpose — a missing cat means a real bug — collapses if
   the feed can vanish for unrelated reasons.
2. **Build it in `swarm_page.py`, not `dashboard_page.py`.** MindSwarm is a
   separate iframe document with its own 1200 ms poll (`swarm_page.py:352-361`).
   `dashboard_page.py` is 1780 lines — already past the 1000-line
   thermo-nuclear threshold — has no section registry, and exposes only one
   CSS/JS injection slot pair (`/*__TASKS_CSS__*/`, `//__TASKS_JS__`), already
   taken by `taskview`.

New: a per-faculty activity signal. Today none exists — only per-session
(`taskview.ACTIVE_WINDOW_SECS = 600`), per-Oversee-session, and per-loop-run
heartbeats. MN-1's phase events supply it, which is why MN-5 follows MN-1.

### MN-3 — Method memory (the "Improvements" the request means)

**"Improvements" is not a store today.** There is no module, file, or event
type by that name; `/api/improvements` (`dashboard.py:403-442`) is a live read
over `incidents.summaries()` + `signal_stats()` + `caveat` events.

The closest existing machinery is `resolve.py` + the `rule_rerouted` event —
but it is keyed on **error signature**, not on task type, capped at
`MAX_REROUTES = 3`, and never applied to human-authored rules
(`resolve.py:201-203`). Its `old_text`/`routes` payload is the only persisted
"that approach failed, use this one" record in the entire system.

R9 needs the same idea keyed on **task type**: given the kind of work at hand,
what has failed before and what replaced it. That store does not exist —
`reflect`'s `task_category` (`reflect.py:26-27`) is written into an incident's
Auto Evaluation and read back only as a display string; nothing indexes or
queries by it.

MN-3 builds it by generalising `resolve.py`'s proven reroute shape from
signature-keyed to task-type-keyed, reusing its "never repeat a failed route"
selection logic rather than inventing new ranking.

### MN-2 — The task graph

The largest build, because the current model cannot express the requirement at
all:

- No hierarchy exists anywhere. No `parent_id`, `subtask`, or depth field in
  `roadmap.py`, `workspace.py`, `taskview.py`, `loop/*.py`, or `oversee.py`.
- `roadmap.py` steps are a **flat list capped at `MAX_STEPS = 50`**
  (`roadmap.py:36`), and the only way to change the set is **wholesale
  replacement** — `set` replaces steps entirely (`roadmap.py:166-206`). The
  `step` action can only flip an existing index's status; there is no
  insert/append/remove (`ACTIONS`, `roadmap.py:29`).

So R2's "subtasks can be added as the need appears" is not a tweak to roadmap —
it needs a new hierarchical, append-friendly model with per-node exit criteria,
whose terminal condition is R3 (PR green).

Reused: the loop engine's proven termination machinery rather than a new one —
`safeguards.should_exit`'s dual-condition gate (`loop/safeguards.py:6-26`, which
already refuses to finish while `required_remaining > 0`) and its circuit
breaker (`:37-88`, CLOSED/OPEN/HALF_OPEN, 3 no-progress / 5 same-error, 30 min
cooldown). Parallel fan-out uses `clusters.py`'s no-file-in-two-lanes invariant
(`clusters.py:187-194`).

**Locked constraint (KTD6):** "Python plans; the agent executes — Python never
spawns agents"
(`skills/mindhive-orchestrator/references/parallel-orchestration.md:10-12`,
locked in `docs/plans/2026-07-09-001-feat-mindhive-orchestrator-sequential-parallel-plan.md:77`).
MN-2 plans lanes; dispatch stays with `superpowers:dispatching-parallel-agents`.

### MN-4 — Teeth: compatibility gate, enforcement, PR-green

Where the hybrid decision becomes code.

- **R13, language compatibility.** This repo has a narrow, checkable surface:
  149 `.py`, 94 `.md`, 9 `.ps1`, 6 `.txt`, 3 `.json`, 1 `.yml`, 1 `.toml`,
  1 `.sh`; `pyproject.toml` declares `requires-python = ">=3.11"` and
  **`dependencies = []`**. So the rule is concrete and mechanically
  enforceable: Python 3.11+ stdlib-only under `src/`, PowerShell 5.1 under
  `hooks/`, Markdown for docs. No TypeScript, React, or Node. The dashboard is
  hand-written HTML/CSS/JS inside Python template strings — no build step, no
  framework. A hard PreToolUse deny on foreign-syntax writes is exactly the
  "tightly-defined, mechanically-checkable" case the hybrid decision licenses.
- **Enforcement breadth.** `enforce.MATCHERS` covers exactly two signatures
  today (`tool:ripgrep-timeout`, `git:worktree-locked`, `enforce.py:69-72`) plus
  three static policies. MN-4 widens it deliberately, within the latency budget.
- **PreToolUse auditability.** PreToolUse currently appends **no event** on
  deny or allow, so enforcement leaves no ledger evidence — contrary to
  SOP 1.4. MN-4 fixes this; without it, MindNetwork's own forcing would be
  unauditable.
- **PR-green (R3).** **Decision (operator): the agent executes, Python only
  tracks state.** This respects KTD6 and the existing `GITHUB_DENY` posture
  (`research.py:1143`, with `tests/test_research.py:1139-1141` asserting Python
  never shells out to `gh pr`). Python holds the gate state in the ledger; the
  agent runs `gh`/CI and reports back. No CI integration exists in `src/`
  today — the only `gh` references are prohibitions.
- **Amend the two policy statements** in `directives.py` in this same
  change-set (§1.2).

Also here, because it is a one-line registration with outsized value:
**register `PostToolUseFailure`.** It is fully built —
`hookcapture.record_post_tool_use_failure` (`hookcapture.py:228`), the CLI stage
(`__main__.py:97-101`), and `hooks/post_tool_use_failure.ps1` all exist — but it
appears nowhere in `settings.json`, so structured failure capture never fires
live. It is the documented fix for the 260-unparseable-`tool_failed`-events
defect, and MN-3's method memory is only as good as the failure data feeding it.

---

## 4. Cross-cutting invariants (R12 as acceptance criteria)

Applies to every slice; a slice is not done until it satisfies these.

1. **Ledger-first.** State lives in the append-only ledger; every projection is
   rebuildable. Corrections are new events, never edits (SOP 7.1).
2. **Reads stay total.** Folds and projections skip malformed rows and never
   raise — the ledger holds history written by every past version
   (`events._row_to_event` exists because one corrupt line once took down
   `main()` and all 14 dashboard endpoints).
3. **Fail-open.** No MindNetwork component may break a prompt, a hook, or the
   dashboard. `promptgate` and `enrich` are the shape to copy: pure functions,
   fail-open everywhere.
4. **Deterministic projections.** No clocks, RNG, or model calls inside
   `projector`, `briefing`, `dedupe`, or any new fold.
5. **Budgeted context delivery.** Every injected block is capped. The existing
   caps bound *item counts*, not *sizes* (`context.py` names this gap
   explicitly) — new blocks must bound size too.
6. **No new dependencies.** stdlib-only.
7. **Latency honesty.** Any component on a hook path states its measured cost.
   Prompt-time is nearly free; per-tool-call is not.
8. **One module, one purpose, testable alone.** New code goes in new modules
   with `tests/test_<module>.py` (78 test files follow this convention). Nothing
   new lands in `dashboard_page.py` (already 1780 lines).
9. **Every event type follows DATA_MODEL.md §6** — register with a why-comment,
   define `validate_payload` at the append surface, decide merge exemptions,
   decide projections, test, `hive log`.
10. **Clear legend.** Every new phase name, task state, and cat faculty label is
    defined in one place and used verbatim in code, UI, and docs — no synonyms
    drifting between layers.

---

## 5. Known-defect ledger (found during exploration, tracked separately)

Not MindNetwork work, but recorded so they are not rediscovered. Two are already
spawned as separate tasks:

1. **`dashboard._post_append` bypasses payload validation** (`dashboard.py:765`)
   — the per-type `validate_payload` dispatch that `cmd_append`
   (`__main__.py:34`) enforces is skipped, so a malformed `code_change`/`sop`/
   `roadmap`/`autoresume` can enter the chain through the web UI. *(spawned)*
2. **Three-way merge-exemption drift** — `consolidate.CONSOLIDATE_TYPES` exempts
   `{state_set, code_change, learning, sop, roadmap}`; `dedupe.collapse`
   (`dedupe.py:89`) hard-codes only `(code_change, sop, roadmap)`, omitting
   `learning`, while claiming to mirror it; `DATA_MODEL.md` §3 omits `roadmap`
   entirely and has no `roadmap` payload row. *(spawned)*
3. **`policy:gate:config-protection` is unreachable dead code.** The registered
   `PreToolUse` matcher is `Grep|Bash|PowerShell`, but
   `gateguard.protected_config_file` returns `None` unless the tool is
   `Edit|Write|NotebookEdit` (`gateguard.py:91,501`). MN-4 should either widen
   the matcher (paying the measured latency) or remove the dead check.
4. **`invoke_requested` is registered but never appended.** `events.py:41`
   declares it; `invoker.py` writes only `invoke_result` and `tool_failed`.

---

## 6. Environment constraint that shapes every hook-touching slice

**Hook registration lives outside this repository, and outside every worktree.**

Registration is user-level at `C:/Users/hanif/.claude/settings.json` (lines
3-66). Every command points at `C:/Users/hanif/mindhive/hooks/*.ps1` — the
**main checkout**. There is no `.claude/settings.json` in this worktree or in
`C:/Users/hanif/mindhive/.claude/`.

Consequences:
- **`hooks/*.ps1` edits made inside a worktree are inert until merged.** No
  hook-behaviour change can be validated through the live chain from a
  worktree.
- Therefore every hook-borne MindNetwork behaviour **must be exercisable
  directly** via `python -m mindhive hook <stage>` with a payload on stdin, and
  that path is what tests assert. The live chain is integration, not the unit
  under test.
- Each shim short-circuits on `MINDHIVE_HEADLESS -eq "1"` (recursion guard) and
  hardcodes `C:\Users\hanif\mindhive\.venv\Scripts\python.exe`.

Six stages are live: `SessionStart`, `PreToolUse` (`Grep|Bash|PowerShell`),
`UserPromptSubmit`, `PostToolUse`
(`Edit|Write|NotebookEdit|Bash|PowerShell|Skill|TodoWrite`), `PreCompact`,
`SessionEnd`. `PostToolUseFailure` is built but unregistered (§3, MN-4).
