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

### 4.4.1 The opt-in gate (MN-6)

**Blast radius.** The `PreToolUse` hook is registered at user level and
`paths.detect_project` collapses every linked worktree onto the main repo's
namespace, so one sidecar governs every checkout of a repo: a blocked task in one
worktree would deny in all of them. Both mindnet entries therefore default
**dormant**, restoring `enforce.py`'s own stated contract ("enforcement is always
opt-in"). Every other member of `POLICY_CHECKS` decides from the tool call alone
and is the same answer in every repository; these two decide from mutable
projection state, which is what makes ON-by-default wrong for them specifically.

Two mechanisms, and the environment one out-ranks the file:

| Mechanism | Scope | `status()["source"]` |
|---|---|---|
| `MINDNETWORK_ENFORCE=1` / `=0` | this process | `env` |
| `<project>/mindnet/enforce.json` | one checkout root | `file` |
| neither | — | `default` (dormant) |

```json
{"version": 1,
 "roots": {"C:/Users/hanif/mindhive": {"enabled": true,
                                       "at": "2026-07-29T12:00:00Z",
                                       "session": "s1"}}}
```

`MINDNETWORK_ENFORCE` decides in **both** directions so it beats the record —
that is the kill switch MN-4 lacked, and it needs no writable disk. Only the
documented spellings (`1/true/yes/on/enable[d]`, `0/false/no/off/disable[d]`)
decide; anything else, **including the empty string a shell leaves when a
variable is cleared**, falls through to the record rather than being guessed at.

Roots are keyed and matched **segment-aligned**, and the **deepest** recorded
root that covers `Path.cwd()` wins. This repo's linked worktrees live *under* the
main checkout (`<repo>/.claude/worktrees/<name>`), so plain containment would
mean arming the main checkout silently re-arms every worktree in it — the same
repo-wide reach, one level down. Deepest-wins is what makes `--disable` from
inside one worktree a real local escape from an armed ancestor, which is also why
a disable is **recorded rather than deleted**: a deleted entry out-ranks nothing.

`hive mindnet enforce` reads the state (and is the only place `[mindnet]
enforcement dormant: opt-in required` is surfaced); `--enable` / `--disable`
toggle it. The toggle **appends no ledger event** — `sidecar._staleness` counts
every event, so a row written without also rewriting the sidecar reads STALE and
silently degrades U9's phase-aware injection, and disarming a frozen repo must
not depend on the ledger being writable.

The opt-in is checked **before** the sidecar read, so a project that never opted
in pays no mindnet file I/O on any tool call; the tool-name test still comes
first, so `Read` pays neither. `optin.py` imports neither `events` nor
`subprocess`: resolving the current checkout is pure path arithmetic, and the one
`paths.worktree_root` git call happens in the CLI when a root is *recorded*.
Fail-open in the direction that matters — any fault in the gate is **dormant**,
never armed.

The gate composes with, rather than replaces, the manifest kill switch: an armed
opt-in still respects `policy:gate:mindnet-*` → `{"enabled": false}`, and
dormancy never reaches `gate:no-verify`, `gate:config-protection` or
`gate:destructive-command`, which have their own adoption decision.

### 4.5 The MN-5 cat board payload

`GET /api/mindnet/cats` →

```json
{"cats": [{"faculty": "task-graph", "display": "active", "status": "active",
           "task_id": "mn5-u9", "phase": "execution", "age_secs": 4,
           "skill": ""}],
 "dim_after_secs": 180, "hide_after_secs": 21600}
```

`ui_cats.board(state, now)` is pure with `now` injected, which is what makes the
thresholds testable at the boundary rather than approximately.

**Two questions, two axes.** Classifying by age alone (the original rule) answered
"is it stuck?" and got "is it running?" wrong — a faculty that finished 30 seconds
ago stayed on screen, green, for three minutes and dim for six hours, against the
stated ask that a cat for work that is not running should disappear.

| Last reported | Age | Renders |
|---|---|---|
| `idle` | past `IDLE_VANISH_SECS` (15 s) | **gone** — it finished |
| `active` | under `DIM_AFTER_SECS` (180 s) | `active` |
| `active` | 180 s to 6 h | `dim` — **this band is the hang signal** |
| anything | at or past 6 h | gone |

The 15-second grace is a flicker guard, not a feature: `telemetry.busy` brackets
every mutation with an `active` row and an `idle` row, so a burst of commands on a
1.2 s poll would blink the cat in and out. Set it to 0 for a cat that vanishes the
instant work stops. `classify(age)` without a status keeps the old age-only
meaning, so no caller is silently changed.

The dim band **is** the feature: a faculty reporting `active` with an age of
hours is a stuck parallel run, which is the reason the board was asked for. The
thresholds ship in the payload so the page never respells them (invariant 10).
An unparseable `ts` is treated as maximally old and hidden — treating it as
fresh would pin a corrupt row on screen as a permanently active cat, and a false
"something is running" is the one thing this board must never say.

Each faculty's cat is its **index in `telemetry.FACULTIES`**, injected into the
page as `FACULTY_ORDER` rather than restated as a faculty→colour table in
JavaScript (invariant 10): a faculty added to the tuple gets its own palette with
no second list to update, and none can silently collide. `CAT_PALETTES` therefore
carries at least `len(FACULTIES)` entries and every modulo reads the table's
length. Dim cats get `filter:grayscale(100%)` — the pre-MN-7 sprite was a
`currentColor` silhouette, so the dim text colour greyed it for free; a coloured
cat needs the filter or the 180-second band stops being visible at a glance.

**Where the board renders (MN-13).** The ask was a cat on every running section, so
an operator can see "apa saja yang menyala dan apa saja yang tidak menyala". It first
shipped on the MindSwarm page alone — a page nobody keeps open — so the signal was
absent from the dashboard the operator actually watches. It now renders on both, from
**one** implementation in `cat_board.py`: `CSS`, `HTML` and `script()` are
interpolated into each page's template. Pasting the strip into the second page would
have created a second copy of the palettes, sprites and labels — invariant 10's exact
failure mode, and an invisible one, since a drifted page shows a plausible board that
merely disagrees with the other.

Two things stay out of `cat_board` on purpose:

- **The container's box.** The swarm page wants a full-width band under its header;
  the dashboard wants an inline cluster at its header's trailing edge. Only the cat
  and panel rules are page-independent, so a page declaring `#mn-cats` itself is
  correct — a shared rule plus two per-page overrides is the same duplication.
- **When a cat renders.** `ui_cats.board` decides every band server-side and ships
  the thresholds in the payload, so the fragment cannot disagree with the tests that
  pin them.

The dashboard drives the board from its **own** interval, not from `refreshLive`:
that timer returns early when no section has registered a live renderer, when the
visible section is not refreshable, and while a manual operation runs — precisely the
moments the board exists to report on. It calls `apiGet`, so the picker's project
scopes it like every other read. The caption sits **above the head** (the original
ask: "kucing yang diatas kepala nya bertulisan Skill and plugin") via
`flex-direction:column-reverse`, which puts the sprite below the text while leaving
the caption first in the DOM for a screen reader.

The metadata panel clamps into the viewport, measured after it is shown — a
`display:none` element reads zero wide, so clamping a hidden panel does nothing. This
was a live regression, not a hypothetical: left-aligning to the cat is harmless on
the swarm page, but the dashboard's strip sits at the right edge, so the last cats in
the legend opened their panel off screen and their task id — the thing you click for
— could not be read.

### 4.6 The MN-7 orchestrator: waves over the graph

`hive mindnet orchestrate` walks the MN-2 graph in **waves**. One wave = the
current frontier, run concurrently, then persisted.

```json
{"outcome": "settled",
 "waves": [{"index": 0, "tasks": ["a"], "completed": ["a"], "blocked": [],
            "skipped": [], "reasons": {}},
           {"index": 1, "tasks": ["b", "c"], "completed": ["b"],
            "blocked": ["c"], "skipped": [],
            "reasons": {"c": "the validation run exited 3 · log: <abs path>"}}],
 "completed": ["a", "b"], "blocked": ["c"], "skipped": [],
 "reasons": {"c": "the validation run exited 3 · log: <abs path>"}}
```

**`blocked` vs `skipped` is the load-bearing distinction.** A status row is written
only for a task whose child **actually ran** — keyed on `RunResult.executed`, which
`executor.py` calls "the SECURITY property ... deliberately not derivable from
`result`". `refused` (the gate said no — including the default case of a task with
no `validation_script`) and `spawn-failed` (the OS said no) mean nothing happened,
so the task is reported `skipped`, keeps its PENDING status, and stays recoverable.

Mapping those to `blocked` was a P0: `tasks.ready` excludes every task with a
BLOCKED ancestor and every task whose dependency is not COMPLETED, so one
script-less task killed its whole subtree **and** its dependents — permanently,
because `orchestrator._persist` is the only writer of `mindnet_task` `status` rows
and a re-declare is forbidden from resetting status. There was no CLI remedy at
all. A skipped task instead terminates the loop through the no-progress guard,
reporting `stalled` with the reason attached.

**The frontier is not a new rule.** `orchestrator.frontier(graph, limit)` calls
`tasks.ready()` — PENDING, every dependency COMPLETED, no BLOCKED ancestor — and
adds exactly two things: the concurrency bound, and **withholding container
tasks**. `ready` answers "may this start", not "may this be run as a unit", so it
offers a parent alongside its still-open children; running both and writing the
parent COMPLETED produces a status `can_complete` refuses ("has N open
descendant(s)"), and `can_complete` has no production caller to catch it.
Re-deriving readiness itself would let a second rule disagree with `can_complete`
about the same graph; `orchestrator.py` therefore contains no status literals at
all and a test over its own source enforces that.

**`MAX_WAVES` is a floor, not the cap.** The effective cap is
`max(MAX_WAVES, len(graph) + 1)`. `MAX_DEPTH` bounds the *parent* chain, while
dependency-chain length is bounded only by `MAX_NODES` — so a legal 65-task
dependency chain hit the "runaway backstop" and exited 1. An explicit
`--max-waves` is still honoured verbatim.

**The worktree root is resolved once**, before any child, and a failure refuses the
whole run. Letting each child fall back to `executor.execute`'s own
`paths.worktree_root()` meant one `git rev-parse` per task fired concurrently, and
a missing git or one call past its 2s timeout made every task in the wave report
itself failed.

The failure cascade is a consequence, not extra code: a blocked task is not
COMPLETED, so every dependent stays off the frontier forever. `tasks.ready`
caches its ancestor answer by `parent_id` — exact, since siblings share an
ancestor set by definition. Measured on `MAX_NODES` all-ready nodes at
`MAX_DEPTH`: ~0.8 ms branching, ~4.6 ms with every task on its own parent chain.

| Outcome | Meaning | Exit |
|---|---|---|
| `dry-run` | planned only; no child ever started | 0 |
| `settled` | nothing left to do | 0 |
| `stalled` | work remains, nothing can advance it | 1 |
| `capped` | hit `MAX_WAVES` with work outstanding | 1 |

`RESULT_STATUS` and `NON_EXECUTING_RESULTS` together **partition**
`executor.RUN_RESULTS`: `ok` → `completed`, `fail`/`timeout` → `blocked`, and
`refused`/`spawn-failed` → no row at all. A value in neither set **raises** rather
than defaulting, because a new failure mode reading as success would mark a task
COMPLETED on evidence nobody checked. A runner that *raises* blocks only its own
task; the two directions differ on purpose (a task failure versus a bug in the
mapping).

Four properties that are contract rather than implementation detail:

- **`--execute` is opt-in.** Bare `orchestrate` is a dry run. This is the one
  command here that starts real child processes, so the safe form is the one an
  operator gets by accident.
- **One `sidecar.rebuild` per wave, AFTER the status appends.** `_staleness`
  counts every event, so a row written without rewriting the sidecar reads STALE
  — silently disabling MN-1's phase-aware injection and MN-4's teeth for the next
  wave. `_persist` then returns the caller's graph advanced by exactly those rows
  rather than re-folding: `rebuild` already folds the ledger, and a re-fold would
  discard any graph that did not come from it.
- **Termination compares statuses across a wave**, not completion counts. A wave
  reporting success while nothing moved would otherwise re-run identically to the
  cap — a spin dressed as progress. `MAX_WAVES` is a runaway backstop, set at or
  above `MAX_DEPTH` so no legal graph can hit it.
- **The task id travels in the run key** (`mn7-w<wave>-<task_id>`), never as a
  runner keyword: `executor.execute` has no task parameter, so a keyword would
  `TypeError` on every real run while a test double accepted it. The key also
  names the RunLog artifact, so identity and attribution are one string, and it
  is wave-scoped so a retry cannot overwrite an earlier attempt's log.

Children inherit the parent's MN-6 arming decision through `executor.execute`'s
`env=` overlay (merged over `os.environ`, never substituted — a bare replacement
drops `PATH`/`SYSTEMROOT` and the child fails to start at all).

### 4.7 What a spawned agent runs (MN-8)

The question that made §4.6's orchestrator decoration. `Task.validation_script` is
a **validator** — the command `hive mindnet validate` runs to prove a phase
cleared — so handing it to `executor.execute` could only ever RE-CHECK a task.
Nothing could take a task and do its work.

`hive mindnet orchestrate --execute` now spawns a headless `claude` per task via
`mindnet/agent.py`, built on `claude_cli.run_headless` (the repo's one spawn home:
it already handles the Windows `.CMD` shim, carries the prompt on **stdin** past
cmd.exe's ~8k argv limit, and sets the `MINDHIVE_HEADLESS` recursion guard so a
spawned session cannot re-fire this project's hooks). `--via-script` selects the
old validator behaviour.

Two runners, two payloads, paired explicitly at the call site because they take
different content in the same argument position:

| Runner | Payload builder | What it does |
|---|---|---|
| `agent.run` (default) | `agent.prompt_for` | spawns an agent that does the work |
| `executor.execute` (`--via-script`) | `orchestrator.validation_script_of` | re-runs the task's validator |

`prompt_for` builds the instruction from the graph, not from prose an operator
maintains: the id/title/description are the task's own, the phase's exit criterion
comes from `phases.descriptor` (invariant 10), and `language_context` is restated
because MN-4 will enforce it on the agent whether or not the agent was told.

**The envelope is checked before the exit code, and that ordering is the whole
correctness of the mapping.** `is_error` means the CLI ran and the *model* never
worked — auth, a usage limit, a refused request. That is not a task failure, and
recording one would `blocked` the task and remove its whole subtree from every
future frontier. Checking the exit code first was the first implementation, and a
real spawn produced `is_error: true` with `"Not logged in"` at **exit 1**, so an
unauthenticated or rate-limited machine blocked every task it touched.

| Outcome | `executed` | Status written |
|---|---|---|
| clean envelope, exit 0 | yes | **none** — `ran-unverified` |
| `is_error` envelope, **any** exit code | no | none — task stays PENDING |
| no readable envelope (any exit) | no | none |
| exit 127 (`claude` absent) | no | none |
| exit 124 (timeout) | yes | **none** — wall-clock, not a task failure |
| clean envelope, non-zero exit | yes | `blocked` |

`fail` is therefore narrow by design: the agent reported a non-error result and
still exited non-zero. Everything unprovable fails toward recoverable.

**An agent's exit 0 is not evidence.** `RESULT_STATUS["ok"] -> completed` was
written for `executor.execute`, where exit 0 means a validation command passed.
An implementer's exit 0 only means the model replied — and `prompt_for` explicitly
invites the failing reply ("if you cannot complete the task, say so plainly"). So
a clean agent run returns `ran-unverified` and writes **no status row**: the task
stays PENDING, the transcript and any branch are reported, and
`hive mindnet validate` is what turns the work into evidence. Before this, a task
nobody worked on was written COMPLETED, its dependents unblocked, and the run
reported `settled`; nothing downstream re-checks, because `can_complete` has no
production caller and the fold is last-write-wins.

Three sets therefore partition the outcomes, and a value in none of them raises:
`RESULT_STATUS` (it ran and the outcome IS evidence), `NON_EXECUTING_RESULTS` (no
child was created), and `UNVERIFIED_RESULTS` (it ran, the outcome is not
evidence — `ran-unverified` and `timeout`).

**Concurrency depends on isolation.** Every agent runs with `cwd=root`, so without
isolation two agents editing one working tree corrupt each other's edits and the
git index — `agent.MAX_SAFE_WORKERS` is 1 and `--max-workers` above it is ignored
with a printed note (`--allow-shared-tree` overrides). `--isolation worktree`
removes the reason for the cap; see §4.8.

An orchestrated wave also emits the `task-graph` faculty through
`telemetry.busy`, so the §4.5 cat board lights up for exactly as long as the wave
runs. Because that appends activity rows and `_staleness` counts every event,
`_persist` rebuilds the sidecar even on a wave that settles nothing.

**Still open:** authority. No `permission_mode` is granted by default, so a
spawned agent may be unable to edit; granting it is an operator decision this
surface deliberately does not make silently.

### 4.9 The pr-green completion gate (MN-10)

MindNetwork's founding requirement: a task *"tidak akan selesai hingga pr berwarna
hijau"* — not finished until the PR is green. `tasks.can_complete` implemented it
from the start: no open descendants, evidence recorded, and the terminal phase
satisfied (`pr-green`, or `TRIVIAL_EXIT_PHASE` for an ADR-0005 bypass).

**Nothing called it.** Every reference to `can_complete` in `src/` was a comment or
a docstring, so `_persist` — the only writer of a `completed` row — marked tasks
finished without ever asking. A task could be COMPLETED at `brainstorm`, unblocking
its dependents on a promise the machine never checked.

`orchestrator.completion_gate_default` is the wire. A run succeeding is not the
same as a task being finished: on refusal the task moves out of `completed` into
`skipped`, keeps its PENDING status, its dependents stay blocked, and the reason
says what is missing. The satisfaction predicate comes from the **sidecar** (which
`_persist` rebuilds every wave, so it is current by construction) rather than a
fresh ~264 ms fold; a missing sidecar yields no records and the gate refuses, which
is the direction a gate must fail in.

**A second wire was required, and its absence had been invisible.**
`can_complete`'s own message says *"a validation run must record its artifact"* —
but `Task.evidence` is written only by a `mindnet_task` `action: "evidence"` row,
and **nothing in the codebase ever wrote one**. The field was permanently empty, so
gate 3 was unsatisfiable for every non-bypassed task. That went unnoticed only
because nothing called the gate either; wiring the gate alone would have made normal
tasks permanently incompletable. `executor.validate` now mirrors a successful run's
artifact onto the graph axis (best-effort — the phase-axis satisfy is the
load-bearing write and has already landed).

### 4.8 Per-task worktree isolation (MN-9)

`hive mindnet orchestrate --execute --isolation worktree` gives each agent its own
git worktree and its own ledger segment. That is what lifts §4.7's cap of one, so
it is the difference between sequential and genuinely parallel agent execution.
Default is `none` — N worktrees is real disk and real git state, so the safe form
is the one an operator gets by accident.

| | path | branch | ledger segment |
|---|---|---|---|
| per task | `.claude/worktrees/mn9-<task_id>` | `mn9/<task_id>` | `mn9-<task_id>.jsonl` |

**Two preconditions, both load-bearing.**

`.claude/worktrees/` is in the **committed** `.gitignore`. It previously lived only
in `.git/info/exclude`, which is local and does not travel, so on any other clone
an agent's worktree appeared as untracked changes in the parent — and this
project's own change-log detection reads `git status`.

Each agent gets `MINDHIVE_SESSION=mn9-<task_id>`, because `events.append_event`
appends to `<session_id>.jsonl` through an **unlocked** `open(..., "a")`.
Concurrent agents sharing a session interleave mid-line into JSONL the fold cannot
read, and the ledger is the source of truth for everything. The per-session
segmentation was already the design; nothing was passing a distinct session down.
`claude_cli.run_headless` gained an `env` overlay for this, and it applies
`MINDHIVE_HEADLESS` **after** the overlay so no caller can defeat the recursion
guard (the failure mode of that is an unbounded spawn loop).

**Cleanup never discards work.** `worktree.retire` removes a checkout only when it
is clean **and** not ahead of the base commit `add` captured. Anything else is left
on disk and reported under `kept`, which is the one line of the run report an
operator must act on. The branch is never deleted, so committed work survives
either way — the only thing a removal could destroy is uncommitted changes, and
that is precisely the case that is refused. `remove` always passes `--force`: on
Windows it fails with "Permission denied" whenever a handle is still open in the
tree, which right after an agent finishes writing is the normal case.

A failed `git worktree add` becomes that task's `skipped` (`refused` /
`executed=False`, reusing §4.7's mapping rather than adding a branch to it), so one
task's environmental failure never writes a status row and never stops the wave.

**Merging is not automated.** Each agent's work lands on its own branch and the
orchestrator reports the names. What to integrate, in what order, and how to
resolve conflicts is the operator's call — an orchestrator that auto-merged N
branches would be making that call silently, and a bad merge costs far more than a
manual one.

### 4.10 The always-on catalog pass (MN-14)

The founding requirement asks for a system that *"secara paksa mengoptimalisasi dan
improvisasi untuk prompt ... pada pembangunan kode sekecil apapun ... dengan membuka
katalog skill and plugin"* — forcibly, for code work however small, open the catalog
and add the context the model needs.

`promptgate` implemented only half of that. Its quality gate fires when a prompt
looks *underspecified*; a well-phrased code request got nothing at all, which is
precisely the case "sekecil apapun" names. Being asked clearly is not a reason to
skip the catalog.

So `hook_context` runs **two independent passes** and joins them:

| pass | fires when | block |
|---|---|---|
| quality gate | `assess` finds the prompt underspecified | "looks underspecified — …" |
| catalog (MN-14) | `is_code_work` matches, however clear the prompt | "Code work — skill & plugin catalog consulted" |

`is_code_work` is pure, cheap, and covers English **and** Indonesian vocabulary plus
a bare source filename — an English-only detector would answer "not code work" for
most of this user's prompts. It skips machine-injected text for the same reason the
gate does (a task notification is nobody asking for anything) and returns False past
`PROMPT_SCAN_CAP`, so a pasted log cannot become a regex scan on the hook's path.

The catalog's skill is trained through the **same** `_pick_skill` ranker as every
dimension, stored as `manifest["always"]`. A second selection path would be a second
place to get measured-vs-unmeasured provenance wrong, and the first disagreement
would have this line calling a skill proven while the gate's own ranking says
otherwise. No inventory match means no rule and no line — invent nothing.

When both passes fire, a skill already named by the gate is **not** repeated under
the catalog heading: the same name under two headings reads as either one demand or
two and the reader cannot tell which. Suppression is per-skill, so a catalog pick the
gate did not name still surfaces.

**What this is not.** "Force" here means forcing *context* in — the output stays
`additionalContext`, additive, never a decision, and tests assert no `deny`, `block`
or `permissionDecision` can appear. Denying the user's own prompt would be a
different and hostile mechanism, and is deliberately not built.

A manifest trained before this unit has no `always` key. That degrades to the
previous behaviour rather than raising — the hook's fail-open promise covers a stale
sidecar too, and a retrain fills it in.

### 4.11 The per-prompt playbook (MN-15)

MN-14 named one skill. The follow-up ask was for three things:

> setiap prompt akan di tambahkan konteks dengan memberitahu **ledger mana saja yang
> perlu dibaca** dan **menggunakan teknik apa** … ketika membangun sesuatu maka akan
> menampilkan skill yang berhubungan dengan plan kemudian **di sintesakan skill yang
> dapat di andalkan** sehingga lebih efisien

`playbook.py` renders exactly that, in the order technique → skills → records, so a
reader who stops after one line still has the most useful part:

```
Technique for this prompt: plan — write the plan before the first edit.
Reliable skills for it, in order:
  1. MUST USE: ce-plan (compound-engineering) via the Skill tool — measured 37/37 completion.
  2. MUST USE: writing-plans (superpowers) via the Skill tool — measured 8/8 completion.
Read these ledger records before starting:
  - caveat @ 2026-07-18T04:44:19Z — dashboard_page.py: 1,715 lines — past the …
  - open them with: hive recall "dashboard buatkan export fitur"
```

**Where the cost lands, measured.** The live store is 373 segments / 12.54 MB /
22 835 events; `events.read_all()` takes **1.09 s**; the whole hook runs in **~0.28 s**
including interpreter startup. A hook that read the ledger would several-fold the
latency of every prompt typed. So nothing on the hook path reads it:
`promptgate.train_and_store` already calls `read_all` behind a staleness gate and
`train()` already receives `evs`, so the index is built there and stored in the
manifest the hook was already loading. Manifest grows to ~36 KB; the injected block is
~1.2 KB against the previous 335 B, and latency did not regress.
`test_nothing_here_reads_the_ledger_on_the_hook_path` monkeypatches `read_all` to
raise, so a later edit that reaches for it fails a test rather than slowing every
prompt.

**Two levels, because the two halves are decided at different times.** Which *skills*
suit a technique is a property of the technique, so it is settled at train time under
`playbook["phases"][<phase>]`. Which *records* a prompt should read is a property of
the prompt, so only the pool is stored (`playbook["records"]`, newest-first, deduped,
capped at `RECORD_POOL`) and `pick_records` ranks it against the prompt's own words at
hook time — a few milliseconds for ~200 short gists.

That split was not the first design. Freezing records per phase at train time meant
the only thing available to match against was the phase's generic need text, and the
result was visibly wrong: a prompt about a CSV export in the dashboard was told to
read a decision about phase-deny rules. Prompt relevance is the primary sort key, the
phase theme the secondary, recency the tie-break — the ledger never rewrites anything,
so an older memo is the more likely to have been superseded.

**Three things deliberately reused rather than rebuilt.** Phase names come from
`mindnet.tasks.PHASES` (the cue table maps onto that legend and tests assert every key
is in it — a near-miss spelling would also trip the invariant-10 guard, which forbids
phase-name synonyms as string literals anywhere in `src/`). Skill ranking is
`recommend`'s own provenance chain, so this cannot call a skill proven that the rest of
the system calls unproven, and an open incident denies MUST wording here as it does in
`train`. Pointers are `type @ ts` + gist + the exact `hive recall` command, **not** the
projector's `.md` filename: that name mixes a digest of the rendered body
(`projector.py:187`) and reproducing it would mean a second copy of the scheme.

`POINTER_TYPES` is `caveat`, `decision`, `experiment_result` — the memos a person wrote
on purpose. `command_run` (8 501 rows) and `assistant_response` (7 152) are raw history
and would bury the signal.

**Cues cover Indonesian as well as English**, including symptom reports that never name
a defect. "kucing di dashboard tidak muncul" is a bug report with no keyword any
English table would catch; before those cues it inferred no technique and the debugging
skill stayed shut on exactly the prompt that needed it. A false positive costs a few
injected lines; a miss costs the feature entirely on the phrasing this user actually
uses.

**Still additive.** `infer` returning None means the block says nothing about technique
rather than guessing — a confidently wrong technique teaches the reader to skip the
marker. Values read off disk are collapsed to one line *and* have the directive words
lowercased, so a stored skill name or ledger gist can neither forge a line nor pose as
an order the system is issuing. A manifest with no playbook falls back to MN-14's
single-skill line.

### 4.12 Repairing the playbook, and closing the loop (MN-16)

An audit of the *live* injected output falsified two of the defects I had written down
and surfaced two worse ones. The corrections are recorded here because the credibility
of the rest depends on which claims were measured.

| Claimed | Measured | Verdict |
|---|---|---|
| `RECORD_POOL` truncates the pool silently | Cap never reached — 130 kept of 284. The 154 missing rows are dropped by **dedupe** | **False as stated** |
| `render_always` is dead code the playbook replaced | 33 of 191 distinct real code prompts infer no technique, so the fallback is live | **False** |
| Relevance needs IDF re-ranking | IDF re-ranking left the top-3 ordering unchanged on every prompt tried | **Rejected, not built** |

**Inference is by weight of evidence.** `infer` counts how many *distinct* cues each
phase matches and takes the strongest; ties resolve in `_CUES` order, so balanced
evidence behaves exactly as it did before. First-match-wins was deciding for **64 of 275
(23%)** real code prompts that matched more than one phase. `cue_scores` is public so a
surface can explain an inference rather than assert it.

Two missing cues mattered more than the ranking. The `plan` phase carried the Indonesian
word but not the English one, so a prompt literally asking for a plan inferred nothing.
And `fix` was absent everywhere — which is why "fix the code … improve the file
architecture" was classified a system audit off a single passing mention of architecture.
No cue was deleted to fix any of this: demoting incidental signals keeps the true
positives.

Distribution over the **distinct** prompts afterwards — the honest view, since the raw
corpus repeats scheduled prompts and the old "audit-system 60" was largely one prompt
repeated ~54 times:

```
plan 45 · audit-bug 44 · pr-green 43 · audit-code 10
brainstorm 6 · execution 5 · audit-system 4 · prd 1
```

**Gists are cut at a word boundary**, close whatever delimiter the cut left open, and
carry an ellipsis so a shortened quote cannot pose as a complete one. 100 of 130 live
gists previously sat at the limit, most ending mid-token. The single quote is
deliberately *not* balanced — in prose it is an apostrophe far more often than a quote,
and enforcing an even count would corrupt "don't".

**Weak records are suppressed, not padded over.** A document-frequency table (pruned to
terms in two or more records) is precomputed at train time and used as a *threshold
only* — never a sort key, because re-ranking by it demonstrably changed nothing. Measured
limit, stated rather than smoothed over: suppression is **inert** when every candidate
shares exactly one common term with the prompt, which is what happens when the prompt's
distinguishing words are Indonesian and the ledger is English. The sample went from 14
record lines to 12, not the 10 first predicted.

**The pool discloses what it dropped** — seen / kept / collapsed / capped, separately, so
the mechanism is distinguishable rather than guessed at.

**The loop closes through machinery that already existed.** `record_direction` used to
return early on a clear verdict, so a well-phrased code prompt directed skills through
the catalog and traced nothing. It now records both paths on the existing
`skill_directed` type — no new event type, no projector renderer, no dashboard change.
`catalog_block` owns the playbook-versus-fallback decision so the recorded trail cannot
disagree with the printed block; `playbook.selected_skills` does the same one level down
with `render` and `directed_skills` as its two projections.

Training then counts directed versus consumed per skill through
**`oversee.consumed_directions`** — which owns the consume-once pairing rule, reused
rather than reimplemented. A skill directed at least `MIN_DIRECTIONS` times and never
used sorts behind its peers.

The first version counted from `oversee.skill_trails` instead, and that was wrong in a
way worth recording. That fold answers the **display** question — "what is the most
informative why for this use?" — and invocation args win there, so a `skill_used`
carrying `args` returns the args and never reaches the direction branch. **78 of 216
real `skill_used` rows (36%) carry args**, so a followed suggestion looked ignored, and
since the signal demotes on "never taken" it would have demoted skills that *are* being
taken while presenting that as evidence. Systematic, not noise. The pairing rule now
lives in its own function and only the display path keeps the args ordering; on the live
store the fix moved `ce-plan` from `taken: 1` to `taken: 2` immediately.

**Demotion only.** Being taken never promotes: the signal is confounded, because the
agent may follow directions mechanically. Provenance wording stays `recommend`'s to
decide — an ignored skill above the auto gate is still a MUST. Demotion reorders; it
never re-labels.

**Cost.** Everything expensive is train-time (0.82 s). The hook moved from 0.27-0.29 s to
**0.38-0.46 s**, still below the MN-15-era 0.39-0.65 s and far below the 1.09 s a ledger
read would cost; the test that forbids a ledger read on the hook path is the hard guard.
A memo that would have recovered ~0.08 s of duplicated work between `hook_context` and
`record_direction` was written and then reverted: the two load the manifest separately,
so any key cheap enough to be worth it is either a guaranteed miss or a staleness hazard.

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
