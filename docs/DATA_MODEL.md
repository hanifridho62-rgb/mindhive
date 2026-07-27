# MindHive data model — the ledger database, its tables, and their relationships

This is the inheritance document for the MindHive store: everything a new
developer (or an AI reading the store cold) needs to interpret the data
correctly. The store's design contract in one line: **an append-only event
ledger is the single source of truth; every other file is a deterministic
projection that can be rebuilt from it at any time.**

Authoritative code: `src/mindhive/events.py` (schema + type registry),
`src/mindhive/paths.py` (locations), `src/mindhive/projector.py` /
`briefing.py` / `sync.py` (projections). When this document and the code
disagree, the code wins — fix the document.

---

## 1. Store layout

Store home: `~/.mindhive/` (override: `MINDHIVE_HOME`). One directory per
project namespace (`~/.mindhive/<project>/`). A project name is detected from
the git main-repo directory name, normalized to a kebab-case slug
(`paths.detect_project`); all git worktrees of a repo share one namespace.

| Path (per namespace)      | Role       | Written by | Rebuildable? |
|---------------------------|------------|------------|--------------|
| `events/<session_id>.jsonl` | **Source of truth.** One segment file per session; one canonical-JSON event per line, append-only. | `events.append_event` | NO — never edit, never rewrite |
| `STATE.md`                | Projection: current state + notices + prevention rules + **SOP block** + suggested skills. | `projector.project` | yes |
| `LEDGER/*.md`             | Projection: one markdown file per semantic event (recall's search corpus). | `projector.project` | yes |
| `index.json`              | Projection: LEDGER filenames indexed by tag and type. | `projector.project` | yes |
| `chain.json`              | Projection: tamper-evident hash chain over semantic events (`hive verify`). | `chain.write_chain` | yes |
| `BRIEFING.md`             | Projection: deduped, capped narrative briefing. | `briefing.briefing` | yes |
| `SOUL.md`                 | Promoted (proven-permanent) prevention rules. | `incidents` | partially (human-curated) |
| `rules/`                  | Prevention-rule records minted from recurring failures. | `incidents`/`reflect` | no (stateful lifecycle) |
| `graph.json`, `graph.html`| Projection: knowledge graph over entities/tags. | `graph`, `graphview` | yes |
| `handoff.md`              | Projection: portable export. | `export` | yes |
| `harvest_state.json`      | Cursor: which transcript lines were already harvested. | `harvest` | no (cursor) |
| `pending_review.json`, `pending_changelog.json` | Sidecar gates: files awaiting thermo-review / a mandatory change-log entry. | `review`, `changelog` | self-healing |
| `chroma/`                 | Optional vector index for `--semantic` recall. | `vectors.embed` | yes |

The auto-loaded memory file lives OUTSIDE the store:
`~/.claude/projects/<encoded-folder>/memory/MEMORY.md`, composed by
`sync.sync()` = usage note + embedded STATE body + prevention rules + capped
lessons + top decisions. This is the context Claude reads at session start.

## 2. The event record (the one true schema)

Every row in every `events/*.jsonl` segment:

```json
{"ts": "2026-07-16T17:10:21Z",        // UTC, second resolution, ISO-8601 Z
 "session_id": "2c3835ba-…",          // segment name == session_id
 "type": "decision",                  // MUST be in events.VALID_TYPES
 "payload": {"text": "…"},            // object; contract varies by type (§3)
 "tags": ["memo"]}                    // lowercase kebab-case strings
```

- Serialization is `events.canonical_json` (sorted keys, tight separators,
  UTF-8) — byte-stable so the hash chain is meaningful.
- Global order = sort by `(ts, segment_name, line_number)`; two events may
  share a second, so precise identity is `(ts, type, text-fingerprint)`
  (`dedupe.text_fingerprint`).
- `append_event` rejects unknown types. Reads never validate payload shape —
  **folds and projections must stay total** (skip junk, never raise).

## 3. Event taxonomy and payload contracts

Two classes (registered in `events.py`):

**Semantic** (`SEMANTIC_TYPES`) — curated, human-meaningful. These ride the
LEDGER, the hash chain, the Memory view, and keyword recall:

| type | payload contract | notes |
|------|------------------|-------|
| `decision` | `{text}` | a choice made and why; one decision per event |
| `caveat` | `{text}` | a lesson/warning; tag `pinned` = never rotates out of MEMORY.md, never consolidated |
| `experiment_result` | `{text}` | measured outcome |
| `next_step` | `{text}` | forward pointer |
| `open_question` | `{text}` | unresolved question |
| `state_set` | `{task, verified, next_step, open_questions[], deferred[]}` | last one wins; excluded from LEDGER |
| `code_change` | `{text, category, files[]}` | mandatory change-log (`changelog.py`); category ∈ update, minor_change, deletion, reduction, major_change, replacement |
| `learning` | `{text, …}` | Self-Reliant PRD report |
| `sop` | `{action, sop_id, title?, text?}` | SOP lifecycle (`sop.py`); action ∈ add, update, delete; current registry = deterministic fold, delivered via the STATE.md SOP block |
| `roadmap` | `{action, plan_id, title?, prompt?, steps[]?, index?, status?, cause?, note?}` | Flat plan registry (`roadmap.py`); action ∈ set, step, interrupt, resume, complete; `set` replaces the step list wholesale (≤ `MAX_STEPS` 50, no `parent_id`); `interrupt` projects the STATE.md out-of-usage marker |
| `mindnet_task` | `{action, task_id, title?, parent_id?, description?, status?, on?, ref?, dependencies[]?, language_context[]?, validation_script?, faculty?, triviality_bypass?}` | MN-2 hierarchical task graph (`mindnet/tasks.py`); action ∈ declare, status, depend, reparent, evidence. **No `phase` action** — the phase axis has exactly one writer (see `mindnet_phase`). `triviality_bypass` is honoured on the FIRST declare only. The fold *refuses* cycle-closing / over-deep / settled-work edges and records the refusal in the record's `violations` rather than entering the bad state |
| `mindnet_phase` | `{action, task_id, phase?, epoch?, evidence?{kind, ref?, result?, detail?}, detail?, verified_by}` | MN-1 phase spine (`mindnet/phases.py` contract, `mindnet/controller.py` fold); action ∈ declare, enter, satisfy, fall_back; phase ∈ brainstorm, plan, prd, execution, audit-bug, audit-code, audit-system, pr-green; evidence kind ∈ file, event, command, human; `verified_by` ∈ python, agent, human. **Two validators, deliberately:** `validate_payload` (the raw-append surface) REFUSES a payload carrying `verified_by`, and `validate_stored` (the writer, called just before `append_event`) REQUIRES it |

**Deterministic** (`DETERMINISTIC_TYPES`) — machine-harvested telemetry:
`file_touched`, `command_run`, `git_diff`, `session_start`, `session_end`,
`compaction`, `plugin_catalog`, `user_prompt`, `assistant_response`,
`loop_*` (autonomous loop lifecycle), `tool_failed`, `rule_*` (prevention-rule
lifecycle), `trigger_trained`, `invoke_requested`/`invoke_result`,
`skill_used` (`{name, plugin?, ok, text, args?}` — optional `args` is the
capped invocation ask, the recorded "why" behind Oversee's per-session skill
trail; contract owned by `hookcapture.py`),
`skill_directed` (`{skills: [{name, plugin?, measured?}]}` — the prompt-quality
gate directed these skills on an underspecified prompt; `promptgate.py` writes
it, `oversee.skill_trails` joins it to a later same-session `skill_used` for the
honest causal "directed by prompt-gate" why), `memory_consolidated`,
`prompt_enriched`/`enrich_toggled` (Oversee prompt enrichment, `enrich.py`:
recorded enrichment acts and the default-on toggle's audit trail — the
settings sidecar `enrich.json` is the source of truth),
`autoresume` (`{action, plan_id, due?, attempt?, cooldown_hours?, cwd?,
model?, timer?, outcome?, detail?}` — auto-resume delivery lifecycle,
`autoresume.py`: action ∈ scheduled, fired, cancelled; after a usage-limit
interruption the roadmap marker is scheduled to re-enter Claude once the
~5h window reopens, and these events are the delivery audit trail; the
roadmap events remain the semantic record),
`mindnet_activity` (`{faculty, status, task_id?, phase?}` — MN-1's faculty feed
for MN-5's cat board, `mindnet/telemetry.py`: faculty ∈ skill-plugin, sop,
improvements, history, task-graph, compatibility, pr-green; status ∈ active,
idle; `task_id`/`phase` are decoration and are dropped when malformed rather
than losing the event).

**"Deterministic" means off the SEMANTIC layer, not off the hash chain.** A
type registered in `DETERMINISTIC_TYPES` gets no LEDGER file, no Memory-view
row, no recall corpus entry and no consolidation pass — but
`chain.compute_chain` folds `events.read_all()` with **no type filter**, so
every telemetry event is hashed and re-hashed in full by every `hive verify`.
Volume is therefore a real cost of a high-frequency type (`mindnet_activity`
being the one to watch); bound it at the emitter.

### 3.1 Merge exemptions (both lists, or it is not exempt)

Registry/audit types are **exempt from merging everywhere**, and "everywhere"
is two independent lists that must agree:

| List | Governs | Constant |
|---|---|---|
| consolidation (the dreaming pass) | a ledger-visible merge — `memory_consolidated` hides the absorbed originals from every future briefing | `consolidate.NEVER_CONSOLIDATE` (subtracted from `CONSOLIDATE_TYPES`) |
| display dedupe | a display-time merge in the briefing, the dashboard Memory view, and Improvements | `dedupe.NEVER_COLLAPSE` |

Exempt in both: `code_change`, `learning`, `state_set`, `sop`, `roadmap`,
`mindnet_task`, `mindnet_phase`. Two reasons, and both matter — **audit rows**
(`code_change`, `learning`) where similar wording does not mean the same fact,
and **replayed registries** (`sop`, `roadmap`, `mindnet_task`,
`mindnet_phase`) whose folds replay every lifecycle event, so an add and its
later update are two steps of one record's history rather than a duplicate
pair. `state_set` is last-one-wins state with no `text` at all.

They are two lists on purpose, so they are checked rather than derived:
`tests/test_architecture_drift.py` asserts the agreement. It exists because
they drifted twice — `learning` and `state_set` were exempt from consolidation
while still collapsible in the display path.

## 4. Relationships (how the tables connect)

```
Claude Code transcripts ──harvest──▶ events/<session_id>.jsonl   (append-only)
hive append / hive log / hive sop ──▶          │
                                               ▼  read_all() — ordered fold
        ┌───────────────┬───────────────┬──────┴────────┬──────────────┐
        ▼               ▼               ▼               ▼              ▼
    STATE.md        LEDGER/*.md     chain.json      BRIEFING.md    graph.json
 (state_set fold,  (1 file per     (hash chain     (dedup + caps)  (tag/entity
  notices, rules,   semantic ev)    over semantic)                  edges)
  SOP block)            │
        │               └──────────▶ hive recall (keyword) / chroma (semantic)
        ▼
    MEMORY.md  (sync.py — auto-loads into every session)
```

Key joins an AI reader should know:

- `session_id` links every event to its segment file, to `session_start` /
  `session_end` bookends, and to Sessions views. Prefix `research-` marks
  Self-Reliant runs (`events.origin_of`).
- `tags` is the graph's edge source and recall's filter; `index.json` maps
  tag → LEDGER files. Keep tags lowercase-kebab; `skill:<name>` prefixes tie
  effectiveness telemetry to a skill.
- `sop_id` threads an SOP's lifecycle events into one registry entry
  (`sop.fold`); `files[]` in `code_change` joins audit entries to the
  `file_touched` telemetry that queued the gate.
- `plan_id` threads a roadmap's lifecycle events into one plan record
  (`roadmap.fold`) and joins the `autoresume` delivery telemetry
  (`autoresume.fold`) to the interrupted plan whose marker it re-delivers.
- `memory_consolidated.absorbed[]` holds `(ts, type, fingerprint)` keys of
  memos hidden (not deleted) from briefings by the dreaming pass.
- `task_id` is MindNetwork's join key and threads **three** streams:
  `mindnet_task` (the graph node — `tasks.fold`), `mindnet_phase` (that node's
  phase and evidence — `controller.fold`), and `mindnet_activity` (the faculty
  feed). It also joins *outward*: `code_change.task_id` is an optional key
  (written by `changelog.record(task_id=…)`) and is what makes the `execution`
  phase machine-checkable — the check is "a `code_change` event on this
  ledger carries this `task_id`". Historical `code_change` rows have no such
  key, which is exactly why it is optional and reads stay total.

### 4.1 MindNetwork: two axes, one declare

A MindNetwork task exists on **two** axes with two event types, and a task
present on only one of them is broken in a way that is easy to create and hard
to see:

| Axis | Event type | Fold | What it owns |
|---|---|---|---|
| graph | `mindnet_task` | `mindnet/tasks.py` | parent/child, dependencies, status, `triviality_bypass`, `validation_script` |
| phase | `mindnet_phase` | `mindnet/controller.py` | current phase, per-phase evidence, epoch, `verified_by` |

`Task.phase` records only the **declared entry phase** and has no writer inside
`tasks.py`; the authoritative current phase is the controller's. That is the
one-writer rule, and it is why `mindnet_task` has no `phase` action.

Consequence for any producer: **`hive mindnet declare` appends BOTH events.** A
graph node with no phase record cannot advance (there is nothing to advance),
and a phase record with no graph node folds into the controller's `orphans`
bucket — rendered, never silently dropped, because the events are already
immutable on the ledger.

### 4.2 The validation executor's denylist

`hive mindnet validate` is the one place Python runs an operator-authored
string (`mindnet/executor.py`). Contract, so nothing downstream has to assume
it: `shlex.split(cmd, posix=False)` → argv with `shell=False`, an explicit
timeout, and output captured through MN-0's `runlog`. Two refusals, both
**fail-closed** — the opposite of `enforce.evaluate_policies`, which is
fail-open by design because it is advisory:

- the destructive-command classifier refuses on any reason string **and on any
  exception it raises**;
- an explicit denylist refuses any argv resolving to `gh` or to `git push`, so
  the "`pr-green` stays agent-reported, `GITHUB_DENY` is preserved" claim is
  enforced rather than merely asserted.

A refusal must never wedge a task: the phase can still be satisfied by
`hive mindnet satisfy --agent-reported`, whose `detail` carries the refusal
reason verbatim, so the ledger shows plainly that Python did not verify it.

### 4.3 The sidecar's `advice` section (MN-3)

`mindnet/state.json` is a **projection**, rebuildable from the ledger and never
a source of truth. Schema version 3 adds an `advice` block beside `records`,
`orphans` and `tasks`:

```json
"advice": {"sops": ["…"], "constraints": ["…"], "skills": ["…"]}
```

Each value is a list of finished, sanitized, per-item-capped LINES — not
records. It exists because MN-3's injector must staple the actual text of the
matched SOPs, prior-failure constraints and skill descriptions into every
prompt, and all three sources are expensive:

| Source | Entry point | Cost |
|---|---|---|
| skills | `plugins.recommend_for` | `events.read_all` (~264 ms) + a `live_inventory` filesystem scan |
| SOPs | `sop.current` | `events.read_all` |
| prior failures | `incidents.summaries` | ledger + per-incident file reads |

So resolution runs at **write** time (`sidecar.resolve_advice`, called from
`rebuild` with the events the caller already read) and the **read** path costs
one `sidecar.read` it was already paying. `inject.py` renders the block and
never imports `advice` — that module imports `inject` for the sanitizer, and the
two ends of the boundary agree on the key names through a contract test rather
than a shared import.

Compatibility runs both ways: a v2 document decodes to no advice (true — it was
written before anything resolved any), and a v2 reader ignores a key it does not
know. Empty lists are not persisted, keeping the round trip an exact equality.

### 4.4 `PreToolUse` denial payloads (MN-4)

The phase spine's teeth. Two entries appended to `enforce.POLICY_CHECKS`, both
returning the standard hook shape:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "[mindhive-rule] …"}}
```

| Signature | Fires when | Message opens with |
|---|---|---|
| `gate:mindnet-phase` | a write/execute tool while **any** `in_progress` task sits at `brainstorm`, `plan` or `prd` | `ACCESS DENIED: MindNetwork State Machine violation. You cannot write or execute code while in Phase: {phase}.` |
| `gate:mindnet-language` | a file write whose extension falls outside a populated `language_context` | `ACCESS DENIED: Language Compatibility Lock violation. This task is strictly restricted to {languages}.` |

Both are `deny` rather than `ask`: a destructive command is sometimes
legitimate, a state-machine violation is not. Both inherit the manifest kill
switch every policy has (`rules/enforce.json`, `policy:<signature>` →
`{"enabled": false}`).

Each message continues past the sentence above with the **task id** and the way
out. That is load-bearing rather than cosmetic: with several tasks in the graph
a message naming only the phase does not say which task froze the repository,
and a refusal an operator cannot act on is an outage.

**Five guards keep this from bricking a repository.** Three were in the first
implementation; two were added after review, and each of those two was a
reachable total deadlock that shipped green:

- **`in_progress` only** for the phase block. `pending` is every declared task's
  default status (`tasks.py`), and `inject.active` deliberately steers it.
  Denying on it would mean one forgotten task refuses every write forever.
- **The current phase's evidence directory is exempt**, read from
  `phases.PHASE_EVIDENCE` (invariant 10). Without it, `brainstorm` forbids
  writing the very document under `docs/superpowers/specs/` that clears it.
  Matched on a **segment-aligned suffix**, because `Write`/`Edit` require an
  *absolute* `file_path` — an anchored comparison against
  `docs/superpowers/specs/` fails on every path the tool can actually emit,
  which is how the first implementation shipped a guard that never once fired.
  The suffix match errs open by design: a guard that exists to prevent a
  deadlock must fail permissive.
- **Unmapped extensions never deny.** The language table is finite; the file
  types a repo contains are not. Trailing dots, trailing spaces and `::$DATA`
  are stripped before the extension is parsed, since Windows strips them when
  creating the file.
- **The evidence exemption spans BOTH rules.** Every planning phase is cleared
  by a markdown document and `.md` maps to `markdown`, so a `language_context`
  of `["python"]` had the phase rule wave the document through and the language
  rule deny it one check later — two individually-sound guards composing into
  the deadlock both prevent.
- **`hive`, `git` and `gh` are never denied as execute tools.** Every documented
  remedy (`hive mindnet satisfy`, `hive mindnet enter`,
  `hive rules enforce --disable`) and the mandatory `hive log` entry run through
  Bash; denying them made the block unrecoverable without leaving the session.
  Running code (`python`, `pytest`, `node`) stays denied — that is the point of
  the rule.

The phase block scans for **any** blocked `in_progress` task rather than
consulting the single furthest-along one. Routing it through `inject.active`
meant advancing any unrelated task to `execution` silently switched the gate off
repo-wide, and finishing that task switched it back on just as invisibly.

The language lock is scoped to `WORKABLE_STATUSES`. It previously consulted
status not at all, so a task completed months ago still enforced its
`language_context` on every write — and because the shared ordering prefers the
task furthest along, the settled task out-competed the live one.

Fail-open throughout: missing, stale, corrupt or empty sidecar allows.
`mindnet/enforcer.py` does not import `events` at all — `PreToolUse` fires on
every tool call, so a ~264 ms ledger read there is not a slow feature but an
unusable session. It also does not resolve the worktree root, because
`paths.worktree_root` shells out to git with a 2-second timeout.

**Blast radius.** The `PreToolUse` hook is registered at user level and
`paths.detect_project` collapses every linked worktree onto the main repo's
namespace, so one sidecar governs every checkout of a repo: a blocked task in
one worktree denies in all of them.

Three guards keep this from bricking a repository, and each has a test whose
deletion is the outage:

- **`in_progress` only.** `pending` is every declared task's default status
  (`tasks.py`), and `inject.active` deliberately steers it. Denying on it would
  mean one forgotten task refuses every write in the repo forever.
- **The current phase's evidence directory is exempt**, read from
  `phases.PHASE_EVIDENCE` (invariant 10) and scoped to the current phase.
  Without it, `brainstorm` forbids writing the very document under
  `docs/superpowers/specs/` that clears it. Containment, not prefix — the same
  call `phases.resolve_ref` makes.
- **Unmapped extensions never deny.** The language table is finite; the file
  types a repo contains are not.

Fail-open throughout: missing, stale, corrupt or empty sidecar allows.
`mindnet/enforcer.py` does not import `events` at all — `PreToolUse` fires on
every tool call, so a ~264 ms ledger read there is not a slow feature but an
unusable session.

### 4.5 The MN-5 cat board payload

`GET /api/mindnet/cats` →

```json
{"cats": [{"faculty": "task-graph", "display": "active", "status": "active",
           "task_id": "mn5-u9", "phase": "execution", "age_secs": 4,
           "skill": ""}],
 "dim_after_secs": 180, "hide_after_secs": 21600}
```

`ui_cats.board(state, now)` is pure with `now` injected, which is what makes the
two thresholds testable at the boundary rather than approximately. Classification
is by **age**, not status: under 180 s is `active`, 180 s to 6 h is `dim`, and at
6 h the faculty leaves the payload entirely. An `idle` faculty inside the window
still renders — "finished a minute ago" and "hung a minute ago" are both worth
seeing, and the raw `status` rides along so the UI can distinguish them.

The dim band **is** the feature: a faculty reporting `active` with an age of
hours is a stuck parallel run, which is the reason the board was asked for. The
thresholds ship in the payload so the page never respells them (invariant 10).
An unparseable `ts` is treated as maximally old and hidden — treating it as
fresh would pin a corrupt row on screen as a permanently active cat, and a false
"something is running" is the one thing this board must never say.

## 5. Invariants — do not break these

1. **Append-only, verbatim.** Never mutate, reorder, or delete ledger lines.
   Corrections are new events (that's what `sop update`, `rule_revised`,
   `memory_consolidated` are for).
2. **Projections are deterministic.** Same events in → same bytes out. No
   clocks, RNG, or model calls inside `projector`, `briefing`, `dedupe`,
   `sop.fold`. Tests assert this.
3. **Reads are total.** New code must skip malformed/unknown payloads, not
   raise — the ledger contains history written by every past version.
4. **Context delivery is budgeted.** MEMORY.md caps: lessons 10 (pinned
   first), decisions 5, timeline 40, SOP block `sop.BLOCK_FULL_BUDGET`.
   Anything unbounded that rides MEMORY.md is a regression.
5. **The semantic layer is optional.** Base install works without
   chromadb/sentence-transformers (`vectors._require_deps`).

## 6. Adding a new event type (the extension procedure)

1. Register it in `events.SEMANTIC_TYPES` (curated, recallable) or
   `DETERMINISTIC_TYPES` (telemetry), with a comment saying why it exists.
2. Define its payload contract in a dedicated module (pattern:
   `changelog.py`, `sop.py`) with a `validate_payload` enforced in
   `__main__.cmd_append` — the store stays type-agnostic; the contract lives
   at the append surfaces.
3. Decide merge semantics: an audit/registry type must be named in **both**
   `consolidate.NEVER_CONSOLIDATE` and `dedupe.NEVER_COLLAPSE` (§3.1).
   `tests/test_architecture_drift.py` fails if you do only one.
4. Decide projections: LEDGER body (`projector.build_ledger_entries`),
   STATE/MEMORY block, briefing section, dashboard API + view. **A semantic
   type whose payload has no `text` key needs a `ledger_body(payload)` in its
   owning module** (pattern: `sop`, `roadmap`, `mindnet/phases`,
   `mindnet/tasks`) wired into `build_ledger_entries`. Without one it projects
   an empty body — invisible to keyword recall, which searches the projected
   markdown — and, because the LEDGER filename hashes `ts + type + body`, two
   events in the same second collide and one silently overwrites the other.
5. Add tests mirroring `tests/test_sop.py` (contract, fold totality,
   delivery, CLI) and run the full suite.
6. Record the change: `hive log <category> "…"` — the change-log gate is
   mandatory for `src/mindhive/` and `tests/`.
