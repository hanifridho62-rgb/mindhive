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
| `roadmap` | `{action, plan_id, title?, prompt?, steps[]?, index?, status?, cause?, note?, reason?, work_branch?, work_pr?, work_commits[]?}` | Flat plan registry (`roadmap.py`); action ∈ set, step, interrupt, resume, complete, archive, link; `set` replaces the step list wholesale (≤ `MAX_STEPS` 50, no `parent_id`); `interrupt` projects the STATE.md out-of-usage marker; `archive` retires a plan whose last step nothing can verify and **requires `reason`**; `link` records the work link and touches nothing else. The three `work_*` keys may ride ANY action and MERGE into the record (an absent key keeps what is on file — `sync_from_todos` re-`set`s without one), so a plan stays pointed at its code across a re-plan. `roadmap.py` owns the contract; `repo.py` owns the probe that reads it |
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
`skill_directed` (`{skills: [{name, plugin?, measured?}], source?}` — these
skills were directed at a session, and `oversee.skill_trails` joins the row to a
later same-session `skill_used` for the honest causal why. `source` names the
writer: **absent or empty is the prompt gate** (`promptgate.py`, its only writer
before the replenishment injector, so every historical row folds unchanged),
`"replenish_injector"` is the injector (`replenish.direct_skill`).
`oversee.DIRECTION_SOURCES` maps each to its rendered reason. An **unrecognised**
source renders a bare `"directed"`: a trail must never attribute a call to
something that did not cause it, and that cannot hold if every unknown writer
falls back to the gate's wording — while echoing the raw value would put an
unvalidated ledger string into the rendered trail), `memory_consolidated`,
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
than losing the event),
`policy_denied` (`{signature, tool_name, decision, reason}` — one row per
`PreToolUse` refusal, written by `enforce.hook_decision`: `signature` is the
`POLICY_CHECKS` id an operator would disable in the manifest, and `""` for the
learned-matcher tier which has none; `decision` ∈ deny, ask; `reason` is stored
**without** `ENFORCED_MARK` and truncated at `DENIAL_REASON_LIMIT` (600) — as is
`tool_name` at `TOOL_NAME_LIMIT` (120), since it comes off the same untrusted
stdin payload — while the hook payload still carries the full text. **Appended on deny only** —
`PreToolUse` fires on every tool call, so an append on the allow path would put
a file write on the hottest path in the system; `tests/test_enforce.py` keeps an
allow-path canary because every other test in that section would pass just as
well against an enforcer that appended every time. Fail-open and
result-discarding: a ledger that cannot be written must not turn a deny into an
allow),
`verification` (`{text, argv[], exit_code, counts{}, excerpt, verified_by,
artifact?, error?}` — one row per mechanical test run, written by
`verify.run_and_record`: the argv actually executed, its **real** exit code, and
a best-effort parse of the summary line. **Two validators, exactly as
`mindnet_phase` has:** `validate_payload` (the raw-append surfaces) REFUSES a
payload carrying `verified_by`, so a hand-written append can never forge a
mechanical verification; `validate_stored` (the writer) REQUIRES it. The stamp
attaches to the **exit code**, which was observed — `counts` is a parse, and an
unparseable summary records counts **absent rather than zero**, because "we
could not tell" and "nothing passed" are different facts. Full output goes to
`runlog`, not the ledger; the event keeps a bounded excerpt plus the artifact
path),
`work_injected` (`{outcome, round, surface, run_id?, lens?, title?, evidence[]?,
fingerprint?, kind?, score?, terms?, reason?, dropped?, plan_id?, step_index?}` —
one row per replenishment decision, written by `replenish.py`: `outcome` ∈ landed,
refused, truncated, dry. `landed` and `refused` are per-candidate and carry
`lens`, `title` and `fingerprint`. `fingerprint` is a SHA-256 digest minted by
`replenish.generate_fingerprint`, never a readable key: the field is capped, and a
readable key long enough to be clamped to that cap would let two different
conditions share one identity — which reads as "already proposed" and silences the
lens that found the second one. The digest is built from a **length-prefixed**
encoding of the lens name and the lens's chosen identity parts, so no component's
content can forge a separator and collide with another (`repair` derives its parts
from arbitrary captured error text). Parts are sorted, which is a deliberate SET
contract for keys made of unordered collections such as `audit`'s uncovered file
set. `kind` is optional and carries the readable classification the digest gives
up, for a lens that observes more than one kind of condition (`tech-debt` proposes
both oversized modules and uncleared review entries); a FUSED systemic candidate
carries `lens: "resonance"`, `kind: "systemic"`. `terms` is optional and appears on
`landed` rows only: the named score terms (`base`, `recurrence`, `severity`,
`magnitude`, `innovation`, `resonance`, `decay`) with their integer values plus
`total`, zero terms omitted. It exists because `score` alone says a candidate
cleared the bar and only the terms say *how* — on magnitude, on a recurrence the
record kept adding to, or on several lenses resonating about one file. Rows
written before the field existed simply lack it, and the fold reads them
unchanged. Not every refusal is recorded: a candidate refused only for scoring
below the round's threshold writes **no row**, because a recorded refusal spends
the fingerprint and "below the bar" is *not yet* rather than *no*. `landed`
additionally requires a **non-empty**
`evidence` list, because a candidate with nothing to point at is exactly what
the value gate refuses and the contract must not let one in through the raw
surface. `truncated` carries `dropped` — a cap that silently cut a round would
read as "nothing more was found". `dry` carries nothing extra and exists as its
own outcome because "this round found nothing" has to be a recorded fact: the
two-consecutive-empty-rounds exit cannot be derived from silence. These rows are
the injector's **only** persistent state — spent fingerprints, the round counter
and the dry-round count are folded back out of them, so there is no sidecar that
can drift. The work itself is not here: a landed candidate becomes a `roadmap`
step, which is semantic and rides the LEDGER, the chain and recall),
`job_started` / `job_finished` / `job_abandoned` (`{job_id, kind}` on all three,
plus `pid` on started, `{outcome, summary, seconds}` on finished and `reason` on
abandoned — one row per lifecycle edge of a long dashboard operation, written by
`jobs.py`. Six operations (harvest, embed, plugin scan, reflect, resolve,
selftest) return **202 Accepted** with a job id and are polled, so these rows are
what makes the answer to "what happened to that click?" outlive the process that
ran it. `outcome` is drawn from `outcomes.OUTCOMES`, and the distinction it
carries is the reason the events exist: `did_work` and `nothing_to_do` are both
successes and only one of them changed anything.
**Three types rather than one `job` with an action field**, unlike `roadmap` or
`autoresume`, because the boot seal's entire job is to pair a start against a
close, and "what never finished" should be answerable by asking for exactly that.
The payload contract is nonetheless SHARED across all three
(`jobs.validate_payload`) since the raw-append surfaces see only a payload, and
what the fold depends on is the two fields it pairs on. **Two validators, exactly
as `verification` has:** `validate_stored` (the writer) additionally requires what
each type must carry — a `job_finished` with no outcome is the row that would make
a finished job read as running forever — and it REFUSES `outcome: abandoned`,
because a run cannot witness its own death.
`job_abandoned` is written **only** by `jobs.seal_orphans` at dashboard boot, and
it rests on a single-writer assumption spelled out in `jobs.py`'s module
docstring: "started, never finished, and not by this pid" is abandoned, which is
right for a restart and wrong for two dashboards sharing one namespace. The seal
is scoped to the selected namespace for the same reason — sealing every store on
the machine would extend that claim to namespaces the process has no business
speaking for. Job output is capped in memory and read by byte offset
(`jobs.OutputRing`), never carried whole in these rows)

**"Deterministic" means off the SEMANTIC layer, not off the hash chain.** A
type registered in `DETERMINISTIC_TYPES` gets no LEDGER file, no Memory-view
row, no recall corpus entry and no consolidation pass — but
`chain.compute_chain` folds `events.read_all()` with **no type filter**, so
every telemetry event is hashed and re-hashed in full by every `hive verify`.
Volume is therefore a real cost of a high-frequency type (`mindnet_activity`
being the one to watch); bound it at the emitter.

**The checkpoint seals whole SECONDS, not event counts.** `read_all` orders rows
by `(ts, segment_name, line_index)` — total and deterministic for a given set of
files, but **not append-monotonic**: ledger timestamps are second-resolution and
the tie-break is the segment's filename, so an event appended later with a `ts`
equal to already-sealed rows can sort *before* them. A checkpoint whose boundary
sat inside such a group was invalidated by the next arrival in that same second,
which kept `hive verify` red permanently — and an alarm that is always red
attests to nothing, because a real tamper cannot be told apart from the standing
failure. `chain.sealable` therefore ends the stamped prefix at a second boundary.

Two consequences follow, and both are intended. Events in the newest second are
**unattested** until a later projection seals them, so `ahead` is the normal
steady state and a tamper confined to that window is not caught until the next
projection. A store written entirely within one second seals nothing (count 0,
the zero digest) — the correct answer rather than a case to special-case around.
Removing the window entirely needs an append-monotonic order (a sequence number
or sub-second timestamps), which would re-base every chain head ever computed.

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
  (`roadmap.fold`), joins the `autoresume` delivery telemetry
  (`autoresume.fold`) to the interrupted plan whose marker it re-delivers, and
  — on a `landed` `work_injected` row, together with `step_index` — points at
  the roadmap step the injector actually produced. That is the only link from an
  audit row to its work: the row explains WHY a step exists, the step IS the
  work. The key is optional on the row because a future sink need not land in a
  roadmap at all (the loop's checklist sink has no `plan_id`).
- `work_branch` / `work_pr` / `work_commits[]` are the roadmap's join *outward*,
  to git and GitHub — the plan's only checkable link to the code it produced.
  All three are optional and independent (§4.15).
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
  the `GITHUB_DENY` claim is enforced rather than merely asserted.

This bullet once carried a second clause — that `pr-green` therefore stays
agent-reported. MN-8b severed the two (§4.9). The boundary is unchanged and is
still the reason nothing here reads GitHub's CI matrix; the conclusion about
who may verify the terminal gate did not follow from it.

A refusal must never wedge a task: the phase can still be satisfied by
`hive mindnet satisfy --agent-reported`, whose `detail` carries the refusal
reason verbatim, so the ledger shows plainly that Python did not verify it.
**Read `clears` on the result** — for the two phases whose descriptor names
`python` (`audit-bug`, and `pr-green` since MN-8b) that satisfy is recorded as a
violation and does not clear the phase.

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
| `gate:mindnet-language` | a write, **or a shell command that creates a file**, whose language falls outside a populated `language_context` | `ACCESS DENIED: Language Compatibility Lock violation. This task is strictly restricted to {languages}.` |
| `gate:mindnet-language` (MN-8a) | a **`Write`** of a `.py` path, permitted by the rule above, whose `content` does not parse as Python | `ACCESS DENIED: Language Compatibility Lock Violation. The provided code contains alien syntax or spaghetti code that is not valid Python.` |

**The two language denials are separate messages on purpose.** The first is
about a file NAMED in the wrong language and its remedy is to widen
`language_context`; the second is about content that will not compile, where
that advice is useless. Collapsing them would hand the operator the wrong
instruction for one of the two failures.

**What MN-8a checks, and what it deliberately does not.** The rules above all
resolve a language from the DESTINATION — the extension, the redirect target,
the scaffold generator. None looked at the payload, so `src/thing.py` containing
`function add(a, b) { … }` satisfied every one of them. `Write` content for a
`.py` path under a python-permitting context now goes through `ast.parse` **in
memory**; nothing is written to disk in order to check it. The check runs last,
so reaching it means the extension rule already allowed the write and a denial
from there can only be about content.

- **`Write` only.** `Edit`, `MultiEdit` and `NotebookEdit` carry *fragments*,
  and a correct fragment — an indented method body, a lone `else:` branch —
  raises `IndentationError` parsed standalone. Checking them would deny ordinary
  edits, and since the documented ways out of an enforcement state are
  themselves edits, a session could not reliably edit its way out. This is the
  same class of guard as the five below, written in advance of the outage
  rather than after it.
- **Heredoc bodies are out of scope.** The redirect TARGET is still checked;
  extracting the body is a shell-parsing problem whose false positives are paid
  on every call — the same trade MN-4 already declined for command language
  inference.
- **`SyntaxError` is the only deny signal.** A `TypeError` on a non-string
  payload or a `RecursionError` on a pathological literal is a fault in the
  checker, not evidence about the code, and allows. It is caught inside the
  helper because `SyntaxError` subclasses `Exception` and `language_violation`'s
  outer fail-open would otherwise convert every intended denial into an allow.
- **It answers "is this valid Python", not "was this meant as Python".** A JSON
  object literal is a valid Python dict expression and parses clean. That limit
  is pinned by a test rather than left to be discovered.

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

**The language lock reaches shell commands, but only the two shapes that create
files.** It originally inspected write tools alone, which left the most obvious
way an agent puts bytes on disk — `cat > src/app.ts <<EOF` — outside the rule
entirely. MN-4 deferred the surface for a reason that still stands: classifying
an arbitrary command's language is a parsing problem whose false positives get
paid on every call. So the question asked is not "what language does this
command imply" but the narrower one the lock was always asking — does this
command put a file on disk in a forbidden language:

| Shape | Example | Resolved via |
|---|---|---|
| Redirect / heredoc / `tee` target | `echo x > src/app.ts`, `cat > a.ts <<EOF`, `>>`, `>\|`, `tee --append` | `EXTENSION_LANGUAGE`, same as a write |
| Scaffolding generator | `npx create-react-app`, `cargo new`, `go mod init` | `SCAFFOLD_LANGUAGE` |

Everything else allows. `node app.js` under a Python-only task is **not** a
violation — running a program produces no source. `SCAFFOLD_LANGUAGE` is keyed
on a token **prefix**, not a program name, because `go mod init` scaffolds and
`go mod tidy` does not.

**A command is lexed before it is scanned, and each step is a false-refusal this
rule shipped without.** `>` is only an operator in the right context:

- **Heredoc bodies are stripped.** A body is a document, not a command. Scanning
  it refused `cat > notes.md <<EOF` whose body merely quoted `echo x > a.ts` —
  and where the real target was this phase's evidence document, the body hit
  overruled the exemption and re-created the deadlock §4.4 promises cannot
  reopen. This project's conventions prescribe heredocs for multi-line text, so
  the bodies most likely to be written are the ones most likely to quote a shell
  command.
- **Quoted runs are neutralised, not deleted.** `echo "renamed a.py -> b.js"`
  and `grep -n "a -> b.md" f` created nothing and were both refused. Masking is
  length-preserving so a quoted TARGET — `> "src/app ts.ts"`, the only way to
  write a path with a space — is still captured.
- **`->` and `=>` are not redirects,** and `[ \t]*` (not `\s*`) stops a
  line-final `>` from claiming the next line's first token as its target.
- **`>&` is excluded**: `2>&1` and `>&2` are the two most common redirects in
  this repo's own commands, and reading `&1` as a filename denies a stderr
  redirect.
- **`EXEMPT_PROGRAMS` is applied per `;`/`&&`/`||`/`|` segment**, not to the
  first token of the whole string. Reading it off the whole command meant
  `git status;` in front of anything switched the rule off. The exemption exists
  to keep the documented remedies reachable, which covers the remedy's
  invocation, not whatever is chained after it.

**Coverage is partial by design, and the boundary is wide.** Uncovered and
verified to allow: `cp`, `mv`, `install`, `touch`, `dd of=`, `curl -o`,
`sed -i`, `patch`, `unzip`, `python -c "open(...)"`, `$VAR`/`$(...)`
indirection, `sh -c '…'` (quote-masking makes the inner redirect data), and
PowerShell cmdlets (`Set-Content`, `Out-File`). The rule is an anti-mistake rail
for a cooperating agent, **not a sandbox** — a `policy_denied` row proves the
shapes the rule recognises were refused, never that no violation occurred.

**Every refusal appends one `policy_denied` event** (§3), so enforcement is
auditable per SOP 1.4 — it previously appended nothing on deny or allow, which
made MindNetwork's own forcing unreconstructable. A **language** denial
additionally emits the `compatibility` faculty's `mindnet_activity` pair, which
is that cat's only emit site (MN-4 left it with none, so it never woke) — an
`active` row AND its `idle`, like `telemetry.busy` writes for every other
faculty, because an unpaired `active` parks the cat in the 180 s–6 h band that
§4.5 defines as the *hang* signal. All three writes live in
`enforce.hook_decision`, never in `mindnet/enforcer.py` — a test asserts over
that module's source that it imports no `events`, and both `append_event` and
`telemetry.emit` do. A phase denial does not wake the compatibility cat; the
phase spine is `task-graph`'s faculty.

**An audit append must not age the projection it audits.** `_staleness`
compares each segment's `[st_size, st_mtime_ns]`, so writing to the events
directory ages the sidecar; the next read answers `fresh=False`, and
`locked_task`/`blocking_task`/`active_task` all open with `if not sc.fresh:
return None, None` — **allow**. So the first refusal of any kind switched off
*both* gates until the next `hive mindnet rebuild`, and re-running the refused
command succeeded. `hook_decision` therefore snapshots freshness **before** its
appends and re-stamps **after** the last one. Re-stamping rather than rebuilding
is sound because neither written type feeds any fold the sidecar projects, so
the phase state still corresponds to the ledger; a rebuild is unavailable here
because it costs a full `read_all` on the hook that fires for every tool call.
Two guards: a sidecar that was **already** stale is never certified fresh, and
if any segment other than the one appended to also moved, the ledger genuinely
has passed the projection and stale is the correct answer.

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

**Who may satisfy it (MN-8b).** `PHASE_EVIDENCE["pr-green"]["verified_by"]` is
`python`. It shipped as `agent`, which meant the founding requirement could be
met by the agent asserting its own PR was green — the one gate in the pipeline
that self-attestation could close.

The rationale it inherited was borrowed from `audit-code` / `audit-system`:
Python cannot see GitHub CI, because the executor denylists `gh` (§4.2). That
premise still holds and the boundary is untouched. The conclusion does not
follow, because those two phases have **no** artifact Python can read and this
one does — the repository's own suite exits with a code, `executor.validate`
runs it as a real subprocess, and `python` stamped off a real exit code is the
honest label rather than the KTD2 lie.

So the claim is **bounded, not dropped**: satisfying `pr-green` asserts the
LOCAL suite passed, which is what `CLAUDE.md` accepts as standing in for CI. It
asserts nothing about GitHub's matrix, and nothing may.

Two consequences, both deliberate:

- `controller._apply_satisfy` refuses a claim weaker than the descriptor, so an
  `agent`-stamped satisfy at `pr-green` lands on the ledger (append-only:
  what was claimed is recorded) and folds to a **violation**. The phase stays
  open. `executor.agent_satisfy` reports this as `clears=False`.
- The task is **not** wedged. Two exits stay live: re-run mechanically through
  `validate`, or `fall_back` to an earlier phase, which opens a new epoch.
  Both are pinned in `tests/test_mindnet_executor.py`; a gate nothing could
  satisfy would be a bricked pipeline dressed up as a hardened one.

Note the two gates compose: clearing the phase is necessary and not sufficient,
because `can_complete` independently requires the task-axis `evidence` tuple
that only a validation run records.

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

### 4.13 Waking the spine: auto-declare and the staleness gate (MN-18)

The phase spine, the SOP section and the Improvements section were **built, tested, and
reached by no prompt**. `advice.resolve_sops` and `advice.resolve_constraints` worked;
`inject.block` returned `""` on its first line because the sidecar held no phase records,
and nothing created the first task — `hive mindnet declare` is hand-run and `decompose`
needs a parent.

**Two gates were shut, not one.** Auto-declaring a task is the obvious fix and it is
insufficient. Measured: one appended event of *any* type marks the sidecar stale, the hook
itself appends on every prompt (`skill_directed`, faculty activity rows), and advice rode
only the fresh branch. A declared task would still have got the generic block forever.

**Gate 1 — a task exists.** `autodeclare.decide` is pure: everything is passed in, because
this runs where `events.read_all` costs 1.09 s against a ~0.4 s budget and where a raise
breaks the user's prompt. Refusal is the default, with three independent bounds on an
append-only store:

| Bound | Effect |
|---|---|
| off until armed, through MN-6's `optin` | no writes at all in a dormant project |
| no declare while a record is active | one auto-declared root at a time — writes are one-per-work-session |
| id derived deterministically from the prompt | a repeated prompt re-declares the same id, which the graph fold upserts |

**No phase is ever named.** `cmd_mindnet_declare` is explicit that the entry point is
derived by `tasks.fold` and that "a caller-named entry phase is the teleport MN-2 stopped
accepting". `playbook.infer` therefore decides only *whether* a prompt is recognizable code
work worth declaring for — never where the task starts. A test asserts the absence of a
phase field structurally.

**Gate 2 — advice rides the stale branch.** `inject.block` now carries resolved advice on
both branches while per-task phase direction stays fresh-only. The old rule withheld
everything on staleness, argued from phase correctness — right about the *phase*, which a
later event can change; wrong about SOPs and prior-failure constraints, which resolve
against the task's *text* and cannot be invalidated by a later event. Since stale is the
normal state, the broader rule cost the entire feature and bought no safety.

Rejected: rebuilding on the prompt path (0.32 s against ~0.4 s, for a result the next
prompt's own write invalidates), and narrowing staleness to the event types the projection
folds — the better long-term answer, but segment size and mtime cannot reveal an event's
type without the read the budget forbids. Deferred, recorded in the plan.

**The refresh lives in `hive state`.** There is no `session_start` stage in this CLI; the
session-start hook runs `hive state`, which is the only command in that hook belonging to
this repo. Stale-gated, so a session opening against a fresh projection pays nothing.

**The canary matters more than any single unit.** Every unit here can pass while the user
sees nothing — that is precisely what happened, with the resolvers' own unit tests green
throughout. `test_the_sop_and_improvements_sections_reach_the_prompt` walks arm → prompt →
refresh → prompt and asserts on the text the user receives, and its own canary disables
`advice_block` to prove it is not passing by finding nothing.

### 4.14 The premise gate (§0's mechanical half)

`directives.OPERATING_SPEC` §0 makes absolute honesty the core operating rule.
Its third clause — never accept a premise you did not verify — is the one a
machine can enforce, and `premise.py` is that enforcement.

**Why it exists.** The directive that commissioned this module asserted an
interrupted roadmap (none existed), a file to patch (`src/mindhive/history.py`,
present on no branch), two unbuilt features (both shipped and merged), and a
3518-test baseline (the real number was 3580). Every checkable claim was false.
`docs/plans/2026-08-02-003-feat-mindnet-mn8-hardening-plan.md` records the same
pattern in its own frontmatter: *supersedes a directive whose premises were
verified false*.

**Why `promptgate` did not catch it.** That gate stops scanning at
`PROMPT_SCAN_CAP` (2000 chars) because its target regexes backtrack
catastrophically — a pasted 100KB run of dots measured ~100s unscanned vs ~ms
capped. Past the cap `assess` returns `clear` and `is_code_work` returns False,
so a 20KB handoff gets **no assessment and no catalog injection at all**. The
incentive is inverted: the longer and more assertion-dense a prompt, the less
scrutiny it receives — and handoffs, pasted specs, and generated continuations
are all past the cap by construction. That cap is correct and is unchanged;
`premise` carries its own larger budget (`PREMISE_SCAN_CAP`) and pays for it
structurally, with bounded repetition and a start-anchoring lookbehind in every
pattern so a hostile input is a linear scan rather than an exponential one. A
test times this rather than trusting it.

**Three stages, three claim classes.**

| stage | contract |
|---|---|
| `extract(text)` | pure, bounded, no I/O → `{claims[], truncated{}, scanned}` |
| `check(claims, evs, root)` | filesystem + ledger → one verdict per claim |
| `render` / `report` | pure; `render` is the hook block, `report` the CLI listing |

| claim class | evidence source |
|---|---|
| asserted repo-relative path | the filesystem, rooted at the checkout |
| asserted roadmap id / interrupted roadmap | `roadmap.get` / `roadmap.interrupted` |
| asserted test count | the last `verification` event (`verify.last_run`) |

**Three-way verdicts, and `UNKNOWN` never collapses.** Folding `UNKNOWN` into
`CONFIRMED` re-creates the failure being fixed — silence reading as assent.
Folding it into `CONTRADICTED` is worse: it manufactures accusations against
true statements, and the first false one teaches the operator to ignore the
block permanently. A test-count claim on a store with no recorded run is
`UNKNOWN`, never `CONTRADICTED`. The one asymmetry is deliberate: an empty
roadmap store *is* authoritative about its own contents, so it contradicts
rather than abstains.

**Additive, never a deny.** The gate emits `additionalContext` only —
`promptgate`'s MN-14 note settles that precedent for this surface, and it binds
harder here, because a premise checker will have false positives (a path claim
about a file the user is about to create) and a false positive that adds a line
costs a line while one that blocks a prompt wedges the session. It fires only
on a contradiction; a gate that speaks every prompt is one the reader learns to
skip.

**No subprocess, ever.** `repo_root` walks up for a `.git` entry instead of
calling `paths.worktree_root`, which shells out to `git rev-parse`. This runs on
every prompt, and the `UserPromptSubmit` path spawns nothing today. Equally, the
gate reads the *last recorded* run and never produces one: a gate that could
spawn a multi-minute suite on a keystroke is a denial of service on the user's
own session. `.git` is tested with `exists`, not `is_dir`, because in a linked
worktree it is a file.

**What is deliberately NOT checked** — stated with equal prominence, because a
gate that implied broader coverage would itself violate §0:

- Claims about what a previous session did. No queryable evidence source.
- Intent, urgency, authority, framing. Not factual claims.
- Claims about code behavior. Would require executing the claim; nothing on the
  prompt path may do that.
- Git refs (asserted SHAs, branch names). Cheap in principle, but it would put a
  process spawn on the prompt path. Deferred, not dismissed.
- Counts of anything other than tests. One evidence source, one claim class.
- Quotation vs assertion. The extractor reads text, not intent, so a path or
  count *mentioned* as an example or a quote is extracted the same as one
  asserted — running the CLI over a document that discusses a false baseline
  will flag that baseline. Inherent to regex extraction, stated rather than
  papered over; on the hook's real surface (a typed prompt) a stated count
  almost always is an assertion.

Both the injected block and the CLI listing end with that limit stated out loud,
so a report of three contradictions can never read as a completed audit.

`hive premise check [--file PATH]` exposes the same engine, exits non-zero on
any contradiction, and exits zero on `UNKNOWN` — "could not check" is not a
finding, and failing on it would train the operator to suppress the command.

### 4.16 The always-on surface: two budgets, and what happens past them

Not event types — two operator-config sidecars and the degradation rule each one
governs. Recorded here because both decide what a session actually receives, and
both used to degrade with nothing said.

**`context_budget.json`** — `paths.project_dir()/context_budget.json`:

```json
{"budget": 51000, "memory_lines": 200, "set_at": "2026-08-13T00:00:00Z"}
```

Config, not a projection: it is not rebuildable from the ledger, exactly like
`rules/enforce.json`. `budget` is the namespace's estimated-token ceiling for the
auto-loaded surface; `memory_lines` is the line budget for the native `MEMORY.md`.
Writes **merge**: setting the token budget never discards a line budget set
earlier. A missing, corrupt or non-positive value reads as unset (token budget) or
as `context.MEMORY_LINE_BUDGET` (line budget) — a bad value must not silence a
real measurement.

Those two numbers bound different failures, which is why they are separate:

| | Bounds | Past it |
|---|---|---|
| `budget` | **cost** — tokens spent before work starts | reported; `hive context --check` exits 1 |
| `memory_lines` | **loss** — the loader reads only the first N lines | the digest's *tail sections* never arrive; `--check` exits 1 even with no token budget set |

The loss case is the subtler one. `sync` caps ITEM COUNTS (`LESSONS_CAP`,
`DECISIONS_CAP`) and no item count bounds lines, so the digest grew past the
loader's limit and lost the two sections written last — the lessons and decisions
the caps existed to protect. `context.memory_pressure` names which sections fall
past the cut, `hive sync` warns the moment it writes such a file, and the
`memory_truncated` finding is **high** severity because every other finding
suggests trimming while this one reports something already gone.

**The per-prompt SOP budget** (`sopgate.PROMPT_BUDGET`, a constant rather than a
sidecar — raising it is a code decision to spend more on every prompt, not a knob).
Past it a body degrades to a `hive sop show <id>` **pointer, never a truncation**:
half a procedure reads like a whole one. `sopgate.plan` is the single place that
decision is made, and `hive sop injection` reports it — `verbatim`, `partial`,
`pointers_only`, or `silent`. `pointers_only` is the state that matters: the block
still arrives, so nothing looks broken, and the procedure it claims to re-assert
is not in it. Measured on this repo at the time of writing: one mandatory SOP of
3,414 chars against a 3,000-char budget, i.e. `pointers_only` on every prompt.

**Checkout staleness** (`repo.staleness`, `hive repo stale`) is the same honesty
rule applied to git: the count of commits behind is reported **with** the age of
the fetch it was measured against, because "0 behind" against a remote last read
in July is not up to date. It never fetches — a probe that reached the network
could not run on the prompt path, in a hook, or offline — and `unknown` carries
its reason (`no_checkout`, `no_integration_ref`, `probe_failed`).

### 4.x Smart-zone session rotation (`mindhive.rotation`)

Not an event type — a sidecar file plus a measurement rule. Recorded here because
both are contracts other code reads.

**Context occupancy.** `input_tokens + cache_creation_input_tokens +
cache_read_input_tokens`. All three are prompt. With prompt caching on,
`input_tokens` is 1–2 (the uncached remainder) while the conversation itself
sits in the two cache fields: `cache_read` is served from cache, `cache_creation`
is written to cache *on this turn*. Both were in the prompt the model read; the
split is a billing distinction, not a size one. `output_tokens` is excluded — it
is what the model wrote, and it reaches the context on the *next* turn, arriving
inside these same input fields. One definition, in `loop.context.occupancy_from`,
used by both `hive loop` and the Stop hook.

**Rotation threshold.** `min(cap, ratio × window_for(model))`, default cap
500,000, default ratio `LoopConfig.smart_zone_ratio` (0.5). The ratio is
proportional, so one config is correct on a 200K Haiku and a 1M Opus; the cap is
absolute, because a 1M window does not make a 900K session a good one. Windows
live in `rotation.window.WINDOWS` as a static longest-prefix table — the hook
runs at the end of every turn and must not make a network call. Refresh from
`GET /v1/models/{id}` → `max_input_tokens`. Unknown ids resolve to the *smallest*
current window (200K), so staleness costs a wasted rotation rather than a session
that never rotates.

**`ROTATION_PENDING`** — `paths.project_dir()/ROTATION_PENDING`, JSON:

```json
{"session_id": "...", "occupancy": 203820, "threshold": 500000,
 "model": "claude-opus-5", "ts": "2026-08-03T00:00:00Z"}
```

Written by the Stop hook, read by `hive session run` after the child exits,
cleared before every spawn. It exists because a Stop hook **cannot end a
session**: its only outcomes are allow-the-stop and `{"decision":"block"}`, and
block means *continue working* — the opposite of rotating. A file is the only
signal that survives the child's exit, however the child died. Reading a corrupt
one returns `None` (no rotation pending), because a truncated artifact is the
normal consequence of an abrupt exit, not an exceptional event.

**The Stop hook is inert by default.** It fires in every session on the machine,
so registration (`hive session install-hook`) and activation are separate steps:
it does nothing unless `MINDHIVE_ROTATION` is set, and `hive session run` sets
that only in the environment of children it spawns. Rotation is therefore a
property of running under the supervisor, not of having installed the hook. It
also never emits `decision`, honours `stop_hook_active`, ignores subagent stops
(`agent_id` present — a subagent's usage is the subagent's context), and returns
silence on any exception. Fail-open is the contract: the failure mode of a
measuring hook must be "we did not measure", never "your work stopped".

**A second variable, `MINDHIVE_ROTATION_SUPERVISED`, decides only what the hook
SAYS.** `MINDHIVE_ROTATION` is set both by `hive session run` and by a human
switching the feature on, so it cannot answer whether anything is waiting to read
the artifact — and the advice used to promise a queued successor to every session
that crossed the boundary, which is false in the second case: the file is written
and nothing ever reads it. `supervisor._child_env` is the only place the marker
is set, because it is the only place "a supervisor is waiting on you" is a fact.
Supervised, the advice names the supervisor and what it will do. Unsupervised, it
says plainly that nothing is queued, names the artifact path it wrote, and names
`hive session run` as the command that would queue one. The marker enables
nothing: with `MINDHIVE_ROTATION` unset the hook is silent whatever the marker
says (`tests/test_rotation_hook.py`).

**Rotation is Windows-only today.** Every shim in `hooks/` is PowerShell — nine
`.ps1` files, no POSIX equivalent — so `hive session install-hook` on
macOS/Linux writes a perfectly valid settings.json entry for a command that
cannot run, and the session then never rotates with nothing said. `install`
returns a `platform_warning` on any successful result from a non-`nt`
`os.name`, so the limit is reported rather than discovered as an absence.

### 4.15 The work link, and retiring a card on evidence

A roadmap card used to have no way out except "every step done". So a plan whose
last step was *"merge the PR"* or *"watch it run for a week"* stayed in Oversee
forever: nothing in the store could see the outside world, and ticking the step
anyway would have put a claim in the audit trail that no code had checked. Two
additions close that, and they are deliberately separate.

**The link (`roadmap.py`).** Three optional, independent keys — `work_branch`,
`work_pr`, `work_commits[]` — may ride **any** action, and the fold MERGES them
into the plan record. Independent because they become knowable at different
moments (branch when work starts, commits as it lands, the PR only once one is
opened); merged rather than replaced because `sync_from_todos` appends a fresh
`set` on every material TodoWrite change and never supplies a link — without
carry-forward, one more todo edit would silently drop the PR number, the same
erasure the `new` step flag already had to be protected from. A record written
before these keys existed simply has none of them: `roadmap.work_link` answers
`{branch: "", pr: 0, commits: []}` and the fold stays total.

**The probe (`repo.py`).** `repo.merge_state(**roadmap.work_link(rec))` returns
`{state, reason, detail, evidence}` and never raises. `state` ∈ `merged`,
`closed` (PR closed without merging), `open`, `unknown`. **`merged` is returned
only on proof** — a merged PR record, or a commit git confirms is an ancestor of
the integration ref (`origin/HEAD`, falling back to `origin/master` /
`origin/main`). Where it cannot tell, it says which of four things stopped it:

| reason | meaning |
| --- | --- |
| `no_work_link` | the plan records no branch, PR, or commit |
| `no_github_remote` | not a git checkout, or no github.com remote |
| `no_gh_cli` | `gh` is not on PATH, so a PR could not be read |
| `probe_failed` | git or `gh` ran and failed (offline, rate limit, auth, bad sha) |

Probes run PR → commits → branch and the first definite answer wins, which is
what makes the module degrade instead of fail: with no `gh`, recorded commits
still answer. Two boundaries are load-bearing. `git merge-base --is-ancestor`
exits 1 for "not an ancestor" and 128 for "no such object" — reading both as
"not merged" would turn a typo'd sha into a claim about the code. And the branch
probe is **prove-only**: a squash merge deletes the branch and rewrites its
commits, so "not an ancestor" is not evidence of anything, and without `gh` the
answer is `unknown`/`no_gh_cli` rather than `open`.

**The exit.** `archive` retires a plan and **requires a `reason`** (status
`archived`, hidden from `plans()` alongside `completed`). It is append-only, on
the `loop_run_dismissed` tombstone precedent: the retirement carries its own
justification and never edits the history that pinned the card open. The plan's
real progress stays on the record, so `archived` never reads as "finished".
`hive roadmap retire` is the automatic path — it completes what the probe proves
merged, archives what it proves closed, and **reports every plan it examined**,
including the untouched ones with their reasons, so "nothing was provably
landed" cannot be mistaken for "the sweep worked".

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
