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
| `orchestrator_memory.json` | Projection: the resolution set a lintester mission's breaker triggered, folded from `incidents.summaries`. The ledger stays the source of truth; delete this and `orchestrator.resilience.remember` rebuilds it byte-identically. | `orchestrator.resilience.remember` | yes |
| `state/pending_embed.json` | Sidecar queue of semantic rows awaiting vector embedding: `{"entries": [{session_id, segment, seg_line, queued_at}]}`. `segment` is the basename actually written (`events.segment_name`, so a quarantined id names its `unsafe-*` slot); `seg_line` is the **1-based** physical line the row starts on. | `events._emit_pending_embed_marker` (`embedqueue`), inside the append lock after every `SEMANTIC_TYPES` append, fail-open; consumed and truncated by the background watcher only | yes — disposable: deleting it loses nothing, `hive embed` re-indexes from the ledger |

`state/` (`paths.state_dir`, made on demand) is the namespace's directory for
sidecars of that kind: neither the ledger nor a projection of it, written by
the append path or a daemon, and never something a reader must consult.

The auto-loaded memory file lives OUTSIDE the store:
`~/.claude/projects/<encoded-folder>/memory/MEMORY.md`, composed by
`sync.sync()` = usage note + embedded STATE body + prevention rules + capped
lessons + top decisions. This is the context Claude reads at session start.

There is one further place the same events can appear, and it is **not** in this
table because it is not part of the store: an optional **Postgres read mirror**
(§4.18). It holds a copy of `events/*.jsonl`, rebuilt from those files, and is
rebuildable in the strongest sense — deleting the whole database loses nothing.
Nothing in MindHive reads it unless an operator opts in, and nothing writes
history to it ever.

### 1.1 Names the store does not take at face value

A project names a DIRECTORY under the store and a session id names a FILE inside
`events/`, so both are path components. Each passes through one sink that cannot
throw, because every hook and projection read reaches it: `paths.namespace_name`
for a project, `events.segment_name` for a session.

| Value | On disk | Why |
|---|---|---|
| a `PROJECT_RE` / `SESSION_ID_RE` slug | itself | the normal case |
| anything else: a separator, `..`, a drive letter, a space, non-ASCII | `unsafe-<sha256[:16]>` | a traversal, or a name the filesystem would store as something else |
| a name starting with `unsafe-`, in any case | quarantined as well | otherwise a caller could address another value's slot |
| on Windows, a device name before the first dot (`CON`, `NUL.txt`, `COM1`, ...), or a component ending in a dot | quarantined | Win32 opens the device, or strips the dot so `abc.` is `abc`; a segment ends in `.jsonl`, so only a project can end in a dot |
| a project named after one of the store root's own FILES (`paths.STORE_ROOT_FILES`, `research_run_*`), in any case, on every platform | quarantined | the directory would stand where the store keeps that file: a `LIVE_LOCK` directory reads as a held lock, an `autosync.json` directory empties the registry |

Only the PATH is sanitised: the event keeps the raw value. Where a human types the
value the refusal is loud instead. `cliparse.session_id_arg` and
`cliparse.project_arg` accept only what the sink keeps on EVERY platform — the
session validator asks `events.segment_name` at its strictest (`os_name="nt"`),
so a Windows device name is refused on Linux too, because a store moves between
machines — and the dashboard's `is_safe_project` asks the sink the same question,
so an HTTP `project=` or `session_project=` the sink would rename is a 400 rather
than a 200 from another directory.

A root file is reached only through `paths.store_file`, which refuses a name not
declared in `STORE_ROOT_FILES`; `tests/test_store_root_files.py` fails on any join
onto `store_home()` outside `store_file` and `project_dir`.

**Enumerators return canonical names only.** `paths.list_namespaces` (what
`hive gc` and `hive projects` walk) and `paths.event_namespaces` (harvest, the
dashboard's project list, `hive skills trail --all`) return a directory only
when `namespace_name(name) == name`. A
quarantine slot, a hand-made `My Project`, or a Windows `abc.` twin created
through an extended-length path is UNLISTED: never listed, read or pruned by
name, and reported by `hive gc` and `hive projects` (`paths.unlisted_namespaces`).
`hygiene` counts a directory as registered by its name, its normalized name, or
its identity (`st_dev`, `st_ino`) against a registered namespace's directory, and
refuses to prune through a symlink or a junction.

The one stated limit: two conforming names that differ only in case are one file
or one directory on a case-insensitive filesystem (`Probe-1` and `probe-1` on
NTFS). Folding case would close it and would also rename existing segments and
namespaces, so the trade is refused and stated instead. The cost is bounded:
every row keeps its raw value, and appends into one namespace are serialised by
one lock held in its directory, so writers that share a segment mix their rows
without tearing or misattributing either.

## 2. The event record (the one true schema)

Every row in every `events/*.jsonl` segment:

```json
{"ts": "2026-07-16T17:10:21Z",        // UTC, second resolution, ISO-8601 Z
 "session_id": "2c3835ba-…",          // segment name == session_id (or its quarantine slot, §1.1)
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

**Two session-id surfaces, one rule.** `append_event(..., strict_session=False)`
— the default, and the hook path — quarantines a hostile id through
`events.segment_name` and never raises (§1.1): `post_tool_use` fires on every
tool call, and a hook that can throw is a hook that breaks the session.
`strict_session=True` and `cliparse.session_id_arg` refuse instead, through
`events.validate_session_id`: the anchored `SESSION_ID_REGEX` (applied with
`fullmatch`, because `$` matches before a trailing newline) AND sink identity at
the strictest platform, `segment_name(x, os_name="nt") == x + ".jsonl"`. The
refusal is a `ValueError` whose message starts `Format session_id '…' tidak
valid:`; the CLI wraps it as `invalid session id '…': …` and exits 2. A strict
append validates the RESOLVED id before `events/` is made and before the append
lock is taken, so a refusal writes nothing — not even the directory.

**The physical row is unchanged; the chain coordinates live on a wrapper.** No
row carries `prev_hash`, `chain_hash`, `seg_line` or `event_id` — every
checkpoint on disk was folded over exactly the five keys above, and one extra
key would move every head on the next append. `events.EventRecord` is a frozen
RUNTIME wrapper that readers build from a row and nothing stores: `event_id` is
`<session_id>:<seg_line>`, `prev_hash` is the head before the row and
`chain_hash` the head after it. `events.compute_chain_hash(prev, canonical)` is
`sha256((prev + canonical).encode("utf-8"))` over the str `canonical_json`
returns — the same fold `chain.py` runs, not a second scheme — and
`events.GENESIS_PREV_HASH` (64 zeros) equals `chain._ZERO`.

**Two `seg_line`s, one off by one — deliberately.** The pending-embed marker's
`seg_line` (§1, `state/pending_embed.json`) is **1-based**: the count of `\n`
bytes in the segment before the write, plus one, so it names the physical line
a human would `sed -n` for. The Postgres mirror's `seg_line` column
(`pgmirror.scan_segments`, `events._segment_lines`, `corrupt_rows`) is
**zero-based**, because it is a stored join key that must not be renumbered. A
reader that consumes the marker and then joins against the mirror subtracts one;
neither number is going to change to meet the other.

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
| `work_session` | `{action, session_id, step_id, status, timestamp, phase?, ws_tag?, initiative_tag?, usage_tag?}` | Work Sessions (`worksessions.py`, blueprint P1b, BAB 5); action ∈ step_append, step_status; status ∈ active, completed, pending, blocked; `timestamp` is `%Y-%m-%dT%H:%M:%SZ`, every field zero-padded (a stamp `strptime` alone would take, such as `2026-9-1T0:0:0Z`, is refused: stamps sort as strings); `step_id` is letters, digits and `._:-` (at most 128). A `step_append` carries the whole blueprint §8.4 step: `phase` is one of the ten `worksessions.WORK_STAGES` (the methodology axis, which is deliberately a different axis from the MindNetwork spine above), and each tag must be exactly what its builder writes — `ws_tag` parses as `[WS: phase, action, stamp]` with the payload's own phase and timestamp, `initiative_tag` round-trips through `make_initiative_tag` or `make_user_spec_tag`, `usage_tag` is `[USAGE: …]`. The keys `reason`, `ref` and `tag` belong to `initiative_declared` and are refused here. A `step_status` carries only the session, the step, the new status and its timestamp. The EVENT's session is authoritative: the fold files a row under its event's session, skips a payload naming another session, and keeps the first row per step id (the writer suffixes a colliding id, checking and appending under `worksession_lock.record_lock` so concurrent writers cannot both take one id). Both types are `events.BOOKKEEPING_TYPES`: semantic, but never a session's liveness on the Oversee board or in the Sessions view, and rendered in the Memory view through their LEDGER body. A step whose `initiative_tag` is `[INITIATIVE: …]` is itself an initiative declaration. The fold is total and replays every row, so the type is exempt from consolidation and dedupe |
| `initiative_declared` | `{reason, ref, tag}` | An autonomous initiative that is not one step, declared on its own row so it is never slipped in silently (BAB 5.3.2); owner `initiatives.py`, which is also this type's contract module. `reason` is non-empty and `reason`/`ref` are stored in the cleaned form a tag carries (no `[`, `]`, pipe or newline, whitespace folded, at most 200 characters), `tag` must equal `worksessions.make_initiative_tag(reason, ref)`, and the work-session keys (`action`, `step_id`, `phase`, `ws_tag` and the rest) are refused. Written by `hive worksession initiative`. `initiatives.initiatives` lists these rows together with every `[INITIATIVE: …]` step, oldest first; the Oversee Autonomous Initiatives group shows them newest first |

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
`mindnet_brief` (`{task_id, chars, skills, sops, constraints, symbols, gated,
empty_reason, failures, dropped, defused, authored, authored_by}` — one row per briefing
composed for a spawned agent,
`mindnet/agent.py:_record_brief` via `mindnet/brief.py`: the counts per advice
source, plus `gated` (a list) naming any source that was switched OFF rather
than empty — `symbols=0` and `gated=["symbols"]` need opposite fixes, so they
are separate fields, and `docs/runbook-empty-brief.md` turns on that
distinction. `failures` (a list) names the sources that RAISED, and `dropped`
counts the lines the fence-defuser ate — a brief can be empty for three
different reasons and `empty_reason` says which. `defused` is the strictly wider
count of lines the defuser CHANGED at all, and it exists because `dropped` alone
reported a run under attack as clean twice over: a forged banner padded past
`_BANNER_GAP` used to survive into the fenced block verbatim AND count as zero,
so both the line and its absence of a counter said nothing happened. `defused`
≥ `dropped` by construction, and a line that was merely disarmed is exactly the
gap between them. `authored` counts the RELEVANT
rows `advice` withheld because a spawned orchestration agent wrote them
(exclusion is the control; the count is what keeps it from being silent), and
`authored_by` (`{sops, constraints}`) splits that total by the source it was
aimed at, because the two are not the same size of hole — a withheld SOP would
have been rendered body-verbatim, while an incident's attacker-controlled field
is never rendered at all. Every field after `empty_reason` was appended, never
renamed: this payload lands on an append-only ledger, so the shape is additive
by construction and an older row simply lacks the newer keys. `chars` sizes the
brief without carrying it. The brief's TEXT is
deliberately NOT in the payload; it goes to the run log under
`<task_id>-brief`, because `hive verify` re-hashes the whole ledger and
kilobytes per task would be paid on every integrity check),
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

**The checkpoint seals PER-SEGMENT LINE COUNTS, and folds in append order.**
`chain.json` is v2: `{version, count, head, segments}`, where `segments` maps
each segment filename to the number of COMPLETE (newline-terminated) lines it
held at seal time. `chain.verify` re-folds exactly those lines, ordered by
`(segment, line_index)`. Two properties make that safe, and they are the whole
guarantee: appending to a segment cannot change that segment's earlier lines,
and a brand-new segment is simply absent from the map. So the checkpoint
attests to the **entire complete log** — there is no unattested trailing
window, and a tamper anywhere in it is caught.

This replaced a ts-ordered fold, and the reason is that **`ts` is not append
order and never was**. `events.append_event` takes `ts` as a CALLER parameter:
`incidents.audit` backdates `rule_expired` / `rule_activated` to the failure's
own ts on purpose, and `hookcapture` stamps `ts=now` before slow work and
before it contends for the append lock. Measured on the live store: 8,547 rows
whose `ts` runs backwards against their physical position. Every one of them
sorted INSIDE the sealed prefix, so `hive verify` flapped — `ok: true`, then
`{"ok": false, "reason": "hash_mismatch"}` twenty minutes later with nothing
tampered, then green again after the next projection. A flickering alarm is
worse than a permanently red one: it trains the reader to ignore it, and a real
tamper hides in the noise.

`chain.sealable` — which ended the stamped prefix at a second boundary — was an
earlier, partial answer to the same defect. It fixed the same-SECOND tie and
could not fix an earlier-second arrival, because no ts-based boundary can.
Nothing in `src/` calls it now — `chain._verify_v1` folds `read_all` against the
stored count directly — and it survives only in `tests/test_chain.py`, where it
is the honest way to BUILD a v1 checkpoint for the shim's own tests.

**A v1 checkpoint keeps the old behaviour, including the flapping.**
`_verify_v1` is the ts-ordered check, run so a store the old code anchored is
VERIFIED rather than falsely alarmed at; it does not repair anything. When this
landed all 67 `chain.json` files under `~/.mindhive/` were v1, so every
namespace takes that path until something re-anchors it, and a namespace nothing
writes to any more never will. One `hive project` against it is the fix.

**`verify` dispatches on the declared `version`, never on the shape of
`segments`.** Shape-dispatch was the first draft and it was a tamper-evidence
hole: replacing a v2 file's `segments` map with `[]`, `"x"`, `5` or `null` fell
through to the v1 shim — which never reads `segments` — and returned `ok: true`
off the weaker check. Anyone able to write `chain.json` could downgrade the audit
by deleting one key. So: no `version` and no `segments` → genuine v1, take the
shim; `version: 2` → the v2 check, and a missing or non-dict `segments` is
`corrupt_chain_file`; a `segments` key with no `version` is a wrecked v2 file,
not a legacy one; anything else — a newer version, a string, a bool (`True == 1`
in Python) — is `unsupported_checkpoint_version`, which is neither a green nor a
claim that the ledger is wrong.

Two costs, both accepted. Re-anchoring re-bases every chain head ever computed,
which is a one-time rewrite of `chain.json` and nothing else — no other artifact
stores a head. And `write_chain` / `verify` now scan the segments themselves,
because `read_all` discards the segment and line index the checkpoint is defined
by; measured on the live store that is *not* a regression (4.32s against the old
path's 4.76s), because canonicalising once and folding twice off a single read
is cheaper than the fold it replaced.

**A ledger row ends at `\n`, and at nothing else. Every TOTAL reader splits
through one function, `events._segment_lines`; `chain._scan` is a fifth reader
that splits inline by the same rule, because it also needs each segment's count
of COMPLETE lines — a different return shape, not a different rule.** `str.splitlines()` — what `read_all`,
`read_types`, `corrupt_rows` and `pgmirror.scan_segments` all used until
2026-09-10 — also breaks on U+0085, U+2028, U+2029, U+000B, U+000C and
U+001C–U+001E. Exactly **three** of those reach a segment: `canonical_json`
writes with `ensure_ascii=False`, so U+0085/U+2028/U+2029 land raw, while the
five below U+0020 are escaped because JSON forbids them unescaped. All three are
legal inside a JSON string and `json.loads` accepts them, so a row carrying one
is INTACT on disk and only the reader destroyed it.

What that cost, measured on three events with one U+2028 in the middle one:
`read_all` returned **two** — the event was invisible to every projection, the
briefing and the mirror — `corrupt_rows` reported **two** torn rows for the one
intact event, and every later row in that segment shifted index by one,
including `seg_line`, which Postgres stores as part of
`UNIQUE (project, segment, seg_line)` and of `event_embeddings`' primary key.
Nothing would have caught the drift: `pgmirror.parity` folds Events and discards
the index, and the staleness fingerprint is `st_size` per segment, which a
split-semantics change leaves untouched. One stale name survives this on purpose:
`pgmigrations/001_schema.sql` still describes `seg_line` as "the 0-based index
from `enumerate(splitlines())`". Everything that comment asserts about the COLUMN
is still exact — 0-based, blank and malformed lines counted — only the function
name has moved. It is left alone because `apply_migrations` records a sha256 per
migration file and refuses any already-applied file whose bytes changed, so
editing a comment in 001 would take every existing mirror down. The name is
corrected here instead; do not correct it there. The chain saw it differently again —
`chain._scan` sealed all three while recording a line count of three — and two
readers disagreeing about where a row ends is how a hash chain stops meaning
anything.

Indices are unchanged on any segment carrying none of the three: verified
**2026-09-09** across all 74 namespaces of the live store, 2495 segments and
118,875 rows at the moment of the check — identical indices and an identical
chain head under both splits — so the fix renumbered nothing already stored and
needed no `hive pg rebuild`. Those totals date the check; they are not a figure
to re-assert. The store is live and grows by the hour: a re-count two minutes
later read 119,349 and then 119,575. What the check establishes is a property of
the two splitters over rows that carry none of the three separators, and that
property does not expire when the row count moves.

That last point is worth stating as an argument rather than as a measurement,
because a measurement of a growing store only ever covers the past. An
independent re-run on 2026-09-13 replayed both splits over 2522 segments and
120,576 rows and again found zero divergence — and that zero is retroactive, not
a snapshot: `append_event` is the only writer, it opens the segment in mode
`"a"`, so no byte already on disk can change. A row that reads identically under
both splits today read identically under both splits the day it was written.
`hive pg rebuild` is therefore unnecessary as a matter of what append-only
means, not merely as a matter of what a scan happened to see. Line endings need no handling either;
`read_text`'s universal-newline decoding turns CRLF and a lone CR into `\n`
before any split, which matters because `append_event` opens the segment in text
mode and Windows therefore writes CRLF.

**`canonical_json` was deliberately NOT changed to escape them.** It is the
ledger's one serialiser and the bytes the hash chain covers, so it is the
highest-blast-radius edit available here, and escaping at the writer fixes
nothing: a row that already carries a raw separator — from a torn write, an
older writer, a hand-edit — still has to be read correctly. It would also leave
the readers wrong while making the bug unreachable through the normal writer,
which is the wrong-but-consistent state `pgmirror.scan_segments`' docstring
exists to warn about. The one genuine JavaScript hazard — U+2028/U+2029 are line
terminators in JS source — was closed where it lives instead: `graphview`
escapes them in the graph blob, which is the only place ledger prose is embedded
in an HTML document. That blob sits in a `type="application/json"` data block
read back with `JSON.parse`, so it was already inert; but the reader fix is what
first made a separator-bearing row able to reach it at all, and the escape makes
the guarantee local to the string instead of a property of a template far away.

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

> **Soft-deprecated as a front door (2026-09-09), not as a mechanism.** New work
> goes through the lintester mission pipeline; `hive mindnet orchestrate` prints
> `mindnet.orchestrator.PRIMARY_DOOR_NOTICE` on every non-JSON run and is kept,
> unchanged: the mission pipeline builds its own graph from an intent and never
> folds a declared one, so it cannot run what this runs. Everything this
> section specifies — the wave loop, the outcomes, the exit codes — still holds.
>
> **Kept is not "supported" (2026-09-13).** The agent runner's `runner_kwargs`
> carries no `permission_mode`, and a headless child with none had its Write
> declined and still exited 0. The loop above stays honest about that — a clean
> exit is `ran-unverified`, which writes no status row, so the run ends
> `awaiting-validation` with exit 1 — but an agent wave here can end with nothing
> written. The notice says so now; giving these children a posture is open in
> TODOS #35, not done.

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
| `awaiting-validation` | agents ran and replied; nothing is verified until `hive mindnet validate` says so | 1 |
| `usage-halted` | an agent reported a quota/auth condition and the run stopped spawning | 1 |

`awaiting-validation` is split out of `stalled`, not out of `settled`. The agent
runner's success result is `ran-unverified`, which writes no status row — so
`before == after` and the wave loop stops exactly as it does on a genuinely wedged
graph. The two print the same word and need opposite things from the operator
(`hive mindnet validate` here, an investigation there), which is the whole reason
the outcome exists. It stays on the exit-1 side because a wrapping CI step that
passed here would be reporting unverified work as done.

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

Each agent gets `MINDHIVE_SESSION=mn9-<task_id>`. When this shipped,
`events.append_event` appended through an unlocked `open(..., "a")`, and agents
sharing a session interleaved mid-line into JSONL the fold could not read. Appends
are serialised by `events._append_lock` now, so a distinct session is no longer
what keeps the ledger readable; it is what gives each agent its own segment and
its own id, which is what `provenance.is_agent_authored` inspects. The
per-session segmentation was already the design; nothing was passing a distinct
session down.
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
{"budget": 51000, "memory_lines": 200, "memory_bytes": 25000,
 "set_at": "2026-08-13T00:00:00Z"}
```

Config, not a projection: it is not rebuildable from the ledger, exactly like
`rules/enforce.json`. `budget` is the namespace's estimated-token ceiling for the
auto-loaded surface; `memory_lines` and `memory_bytes` are the two cuts the native
loader applies to `MEMORY.md`. Writes **merge over all three**: these are set at
three different moments, and setting one must never discard another. A missing,
corrupt or non-positive value reads as unset (token budget) or as
`context.MEMORY_LINE_BUDGET` / `context.MEMORY_BYTE_BUDGET` — a bad value must not
silence a real measurement.

Those numbers bound different failures, which is why they are separate:

| | Bounds | Past it |
|---|---|---|
| `budget` | **cost** — tokens spent before work starts | reported; `hive context --check` exits 1 |
| `memory_lines` | **loss** — the loader reads only the first N lines | the digest's *tail sections* never arrive; `--check` exits 1 even with no token budget set |
| `memory_bytes` | **loss** — and the cut that actually bit | same, and it is the one to check first: measured live at 404a7c0, the delivered digest stopped at LF byte ~24,900 |

The loss case is the subtler one, and it has bitten twice for two different
reasons. First: `sync` caps ITEM COUNTS (`LESSONS_CAP`, `DECISIONS_CAP`) and no
item count bounds lines, so the digest grew past the loader's limit and lost the
two sections written last — the lessons and decisions the caps existed to protect.
Second, and only visible once measured: the loader's cut is a **byte** cut on
LF-normalised content, not the line cut this sidecar originally modelled. At
404a7c0 `MEMORY.md` was 202 lines against a 200-line budget, so the report read
"over by 1" with `dropped == []`, while what a session actually received stopped
at line 188 and `## Key decisions` (line 195) never arrived — a 14-line,
whole-section loss reported as one line.

Two consequences are load-bearing for anyone touching `context.memory_pressure`:

* **Bytes are measured on the decoded text re-encoded as UTF-8, never
  `stat().st_size`.** `sync` writes in text mode, so on Windows the file on disk
  carries CRLF and is one byte per line larger than what the loader reads
  (measured: 26,560 on disk vs 26,359 LF — a delta of exactly 201, the line
  count). A guard reading `st_size` over-trims by that much forever.
* **Both cuts are computed and the EARLIER one is reported** (`pressure["cut"]`
  names its unit; `pressure["truncated"]` is the field callers branch on).
  Reporting the other cut understates the damage, which is precisely how a
  fourteen-line loss got reported as one.

`context.memory_pressure` names which sections fall past that first cut, `hive
sync` warns the moment it writes such a file, and the `memory_truncated` finding
is **high** severity because every other finding suggests trimming while this one
reports something already gone.

**The per-prompt SOP budget** (`sopgate.PROMPT_BUDGET`, a constant rather than a
sidecar — raising it is a code decision to spend more on every prompt, not a knob).
Past it a body degrades to a `hive sop show <id>` **pointer, never a truncation**:
half a procedure reads like a whole one. `sopgate.plan` is the single place that
decision is made, and `hive sop injection` reports it — `verbatim`, `partial`,
`pointers_only`, or `silent`. `pointers_only` is the state that matters: the block
still arrives, so nothing looks broken, and the procedure it claims to re-assert
is not in it. Measured on this repo at the time of writing: one mandatory SOP of
3,414 chars against a 3,000-char budget, i.e. `pointers_only` on every prompt.

That verdict is **also pushed onto the projections**, because `hive sop injection`
has to be typed and a degradation only a deliberate check can find is one nobody
finds. `sopgate.notice` renders a block for `pointers_only`, `partial` and
`silent`, and **the empty string for `verbatim`** — a notice present in every
healthy digest is a notice every reader learns to skip. It enters at
`projector.compose_state`, which is `build_state`'s only caller and feeds *both*
the STATE.md rebuild and the digest `sync` embeds in `MEMORY.md`; the latter is the
file a session loads without being asked, and is the reason the notice is composed
there rather than in `cmd_state`, which does not build STATE.md at all — it prints
the file (`commands/context_ops.py::cmd_state`).

Three properties of that block are stated inside it rather than left to a reader:

* **The verdict is GLOBAL.** `sopgate.probe` ignores its `project` argument and
  reads the cross-namespace `mandatory_sops.json` on purpose (the hook is
  registered machine-wide, so per-project data would make its reach a fiction),
  while STATE.md is per-namespace. A global verdict printed inside a per-namespace
  file without saying so invites exactly the wrong reading.
* **It is as of the last rebuild**, not as of read time — the same property the
  review, change-log and roadmap notices already have.
* **One vocabulary.** `sopgate.verdict_sentence` is shared by the notice and by the
  `sop-injection` outcomes surface, so the two cannot describe one measurement in
  two ways. `probe` already refuses a second *computation* of the fit rule; this
  refuses a second *phrasing* of the result.

Ordering note: `projector._project` refreshes the mandatory-SOP sidecar **before**
composing STATE.md, not after. With the write after, STATE.md carried the verdict
as of the previous rebuild while the `MEMORY.md` digest composed moments later in
the same `hive sync` carried the current one — two files disagreeing about one
measurement. The probe itself costs one small JSON read (measured 0.1 ms, worst of
5 reads 10.1 ms on the live store), and is paid at rebuild, never on the prompt path.

**Checkout staleness** (`repo.staleness`, `hive repo stale`) is the same honesty
rule applied to git: the count of commits behind is reported **with** the age of
the fetch it was measured against, because "0 behind" against a remote last read
in July is not up to date. It never fetches — a probe that reached the network
could not run on the prompt path, in a hook, or offline — and `unknown` carries
its reason (`no_checkout`, `no_integration_ref`, `probe_failed`).

The same rule has a second half, added after the probe reported `current — 0
behind / 0 ahead of origin/HEAD` on this repo's `master` while `git pull` there
was failing with *"There is no tracking information for the current branch"*.
The counts are measured against the **integration** ref — where a merge lands —
which resolves on any ordinary clone. Whether the branch tracks anything is a
separate fact, and it is the one that decides whether the checkout can ever
learn it has fallen behind. So `staleness` reports `branch`, `upstream` and
`upstream_missing` beside the counts rather than letting one be inferred from
the other, and `hive repo stale` names the condition and the command that fixes
it. `upstream_missing` is true whenever no tracking ref could be read — "we
could not ask" is not evidence that `git pull` works, the same reasoning that
makes an unreadable fetch age count as stale.

That answer is also **pushed rather than waited for**. `repo.tracking` is the
two-call subset of the probe (measured 2026-08-14, min of 5: **133 ms**, against
**249 ms** for the full `staleness` — the earlier note said ~85ms), cheap
enough that `hive state` — the one command the session-start hook runs — asks
every session and warns on stderr when the branch tracks nothing. It prints
nothing when the branch tracks something, nothing outside a checkout, nothing
on a detached HEAD, and nothing at all if the probe throws: printing STATE.md is
that command's job and a diagnostic must not be able to stop it.

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

**Rotation runs on both shim families.** `hive session install-hook` registers
`hooks/stop.ps1` through `powershell` on Windows and `hooks/stop.sh` through `sh`
everywhere else (`rotation/install.py:default_command`, chosen by `os.name`).
`install` returns a `platform_warning` on a successful result only when the shim
for this platform is MISSING from the checkout: it asks the filesystem rather
than comparing `os.name` to a constant, so a registration that cannot fire is
reported rather than discovered as an absence, and the warning stays true when a
platform gains or loses a shim.

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

### 4.17 Why the dashboard may run pytest, and what would revoke that

The **Self-test** button starts the repo's own test suite from the local web
server. The justification is recorded here and in `_job_selftest`'s docstring
because the alarming reading of that sentence is the correct instinct, and the
answer to it is a set of conditions that a later change could quietly remove.

**The true shape.** Not pytest in-process. `verify.default_argv`
(`src/mindhive/verify.py:69`) builds `[sys.executable, "-m", "pytest", "-q",
<tests>]`; `verify.stream` spawns it with `subprocess.Popen` (`verify.py:141`)
from a jobs worker thread, **argv list only** — `shell=True` appears nowhere in
that module and a test pins its absence. stdout is streamed line by line into the
job artifact, and a process that dies mid-run is sealed `job_abandoned` at the
next boot rather than left reading as "running".

**The three holds, at their real strength:**

| Hold | Where | What it is, honestly |
|---|---|---|
| Token | `make_handler(token)` `dashboard.py:527`, `_check_token` `:592-598` | **Server-wide**, not per route — one value closed over by the handler, compared constant-time. Normally delivered in the **URL query string** (`stable_url` `:1888-1894`). A URL-borne bearer token is weaker than "per-route auth" implies: it reaches shell history, browser state, and any log that records a request line. |
| Single-flight | `_SELFTEST_LOCK` `:88`, passed at `:1832-1833` | A second click during a run is refused, not queued. Prevents two pytest processes over one working tree writing the same caches and each invalidating the other's result. |
| Local-only | `ThreadingHTTPServer(("127.0.0.1", port), …)` `:2014` | Nothing off this machine can reach the route. |

**The cost, unhidden:** a browser click starts a multi-minute CPU-heavy
subprocess on the machine already serving the dashboard.

**Revocation.** Remove any ONE hold and the justification is void, not merely
weaker — bind `0.0.0.0` and local-only is gone; drop the lock and concurrent runs
corrupt each other's tree; make the token optional and an unauthenticated request
starts a process.

### 4.18 The Postgres read mirror (ADR 0009)

An optional second copy of the ledger, in Postgres, existing for exactly one
reason: **giving an AI a query surface**. Not speed — `events.read_all` was
measured at **0.46 s for 37,038 events across 921 segments** (2026-08-17), which
is not a crisis. The gap it closes is that an agent has no structured way to ask
this data a question at all: "which sessions hit `tool_failed` after a
`policy_denied`" is a fold in Python and one `SELECT` in SQL.

**The direction of authority is the whole design.** The file ledger stays
canonical and stays the **sole writer**. `MINDHIVE_EVENT_BACKEND` stays `file`;
nothing in this feature flips it, and `tests/test_pg_repo_gates.py` asserts that
nothing under `src/` writes it. Postgres is downstream of the files the way
`STATE.md` is, and it is more disposable than any projection in §1: the store
does not contain it, `hive verify` does not consult it, and a dropped database
is a `hive pg rebuild` away.

| Module | Role | Credential |
|---|---|---|
| `pgschema.py` | the shape: column list, `EVENT_TYPE_CLASSES`, row↔`Event` mapping | none |
| `pgmigrations/*.sql` | `001` table + anomaly view, `002` indexes, `003` roles + the reader's view, `004` `mirror_state` for the reader + object ownership, `005` drops the ordering index `002` created | — |
| `pgmirror.py` | migrate / rebuild / parity — the only module that writes | `MINDHIVE_PG_DSN_ADMIN` |
| `pgstore.py` | the read DAL: `READ_ALL_SQL`, `read_all_strict`, `read_all_or_none`, `mirror_one` | `MINDHIVE_PG_DSN_RO` |
| `pgtools.py` | the seven AI tools and their result shaping | `MINDHIVE_PG_DSN_RO` |
| `mcp_server.py` / `pgcli.py` | the two surfaces over one registry: MCP, and `hive pg` | — |

**Two credentials, split by module, and the split is checked.** No module on the
AI path imports the admin DSN; `pgmirror` is its only reader. A test also asserts
that `enforce.py`, `sopgate.py`, `rotation/hook.py` and `mindnet/enforcer.py`
reach none of this cluster — those four run on hot hook paths where a socket
would be an outage, not a slow feature.

#### The table, and the columns that are not obvious

`mindhive.events` mirrors §2's record, plus the bookkeeping that makes file
order derivable. Full commentary is in `pgmigrations/001_schema.sql`; the parts a
reader must not "simplify":

- **`ts` is `TEXT`, verbatim, never reparsed.** `chain.compute_chain` hashes
  canonical JSON of this string, so a `TIMESTAMPTZ` round trip — which normalises
  offset spelling, fractional seconds and the trailing `Z` — breaks chain-head
  parity, the single gate that proves the mirror faithful. It is **nullable** for
  the same reason the ledger admits `{"ts": null}`: reads check key presence only.
- **`ts_utc TIMESTAMPTZ` is filled by the loader, not generated.** Postgres
  requires a generated column's expression to be `IMMUTABLE`, and every
  text→timestamp path is `STABLE` (`DateStyle`/`TimeZone` dependent), so
  `GENERATED ALWAYS AS (ts::timestamptz) STORED` fails at `CREATE TABLE`. It is
  NULL when `ts` is not the canonical shape — a reportable anomaly
  (`mindhive.events_anomalies`), never a load failure.
- **`segment` is the source FILENAME, `seg_line` its 0-based line index.**
  Counting blank and malformed lines, because that is what the file order counts.
- **`global_seq BIGINT` is identity only.** It is the requested primary key and
  it is honoured — what changed is that no ordering rests on it.
- Every text column is `COLLATE "C"` so Postgres compares byte-wise, matching
  Python's code-point comparison. A Windows-installed cluster otherwise defaults
  to a locale collation that ignores punctuation at the primary level, which is
  precisely the distinction the ordering below turns on.
- **`payload` is `JSONB`, and it is chain-safe in a NARROWER way than "keys get
  sorted".** `canonical_json` sorts keys, so JSONB's internal key reordering is
  invisible to the chain — that much is well known and is stated again below.
  What key sorting does not cover is number **spelling**. JSONB stores every
  number as `numeric`: exact, arbitrary precision, scale preserved, and with no
  exponent notation at all — so `1e-07` is returned as `0.0000001`. The chain
  survives that because the printed form parses back to the same float64 and
  `json.dumps` re-emits `1e-07`. **One shape does not survive: `numeric` has no
  signed zero, so `-0.0` returns as `0.0` and the head moves.** It cannot
  currently occur (the live ledger holds 10,841 payload numbers and not one
  float ending in `.0`, signed or not) and it cannot occur *silently*, because
  parity compares exactly these heads. Both halves are fixtures rather than
  prose: `tests/test_pg_numeric_parity.py` models `numeric_out` and proves the
  fixture armed, and
  `tests/test_pg_live.py::test_adversarial_number_shapes_survive_the_jsonb_round_trip`
  asks a real server. That measurement is also why they had to be written: with
  zero adversarial shapes in the ledger, the 37k-event parity run below is green
  on this dimension whatever Postgres does with a number.

#### The ordering contract, and the invariant it corrects

```sql
ORDER BY COALESCE(ts, ''), segment, seg_line     -- pgstore.READ_ALL_SQL
```

ADR 0006 claimed `ORDER BY ts, session_id, id` reproduces file order, because the
segment is `<session_id>.jsonl`. **It does not, and the counter-example is in the
live store.** `events.read_all` sorts on `(ev.ts or "", seg.name, line_index)` —
the *filename*. Where one session id is a proper prefix of another, the separator
that follows decides: `-` is `0x2D`, `.` is `0x2E`, so `-` sorts below `.` and the
two orders invert. The store already holds `verify.jsonl` and
`verify-e2e-001.jsonl`:

```
by session_id : ['verify', 'verify-e2e-001']
by filename   : ['verify-e2e-001.jsonl', 'verify.jsonl']
```

Two sessions, opposite orders, therefore two different chain heads — so the
parity gate built on that invariant would have failed against its own store, and
the failure would have read as data corruption rather than a sort key.
`COALESCE` carries the second half of the correction: Python sorts a NULL `ts`
**first** (`ev.ts or ""`), Postgres sorts NULLs **last** by default.

#### The index 002 created, and why 005 drops it

`002_indexes.sql` created `events_order_idx (project, ts_key, segment, seg_line)`
to serve the ordered full read. **It never did, and no index can**, which EXPLAIN
showed and reasoning did not:

- the ordered read is `ORDER BY COALESCE(ts, '')`, and Postgres does not match
  that expression to the `ts_key` generated column, so the index is not
  considered even with `enable_seqscan=off` and `enable_sort=off`;
- `mindhive_ro.events` is `security_barrier`, which puts a Subquery Scan between
  the ORDER BY and the table — the reader could not use an index-ordered path
  even if the expression matched.

Measured on 37k rows: `Sort Method: external merge Disk: 11728kB`,
`Execution Time: 69.290 ms`. The filtered tool queries are unaffected (1.6 ms and
0.3 ms) because they are served by the *other* indexes 002 created, which are
plain column indexes. 005 therefore drops the unmatchable one and adds no
replacement — an index that is never chosen is write amplification plus a false
claim in the schema.

#### The parity gate

`hive pg parity` folds the file ledger and the mirror through
`chain.compute_chain` and compares heads. Two properties make it worth trusting:

- **`read_all_strict` has no file fallback**, deliberately. A strict reader that
  degraded to the files would make the gate compare the file ledger with itself
  and pass while proving nothing.
- **`parity_ok` in `mindhive.mirror_state` is durable, and the tools refuse when
  it is false.** Unlike staleness — which is bounded and reportable ("the mirror
  is one session behind") — a parity failure is unbounded: rows may be missing,
  duplicated or misordered and you cannot say which. `mirror_state` keeps both
  heads rather than one boolean, because "they differ" is not actionable.
- **Lag is not divergence, and a correct PREFIX passes.** `compute_chain` takes a
  `checkpoint`, so one fold yields both the ledger's head and its head at exactly
  the number of rows the mirror holds. A mirror that is behind passes by PROOF —
  every row it holds hashes to the same chain — rather than by an assumption
  drawn from append-only-ness. What still fails: a row edited in place (same
  count, different head), a mirror longer than its source, and any reordering.

  This is not a refinement, it is what makes the feature usable. Measured: all
  four tools returned `parity_failed` against a mirror whose chain head was
  correct, because the ledger had grown 3,782 bytes since the rebuild. A live
  append-only ledger is stale within seconds, so `parity_ok` and `stale` are
  answered separately and never ANDed — `pgstore.mirror_health` computes the
  second from a `stat` per segment (`ledger_fingerprint`), not a second fold.

#### The seam, and what it costs when unused

There is exactly ONE seam, and it is on the READ side: `events._maybe_read_from_pg`.
**Nothing on the append path touches Postgres, under any value of
`MINDHIVE_EVENT_BACKEND`.**

**This corrects an earlier draft of this section**, which described a write seam
as live and in-package. ADR 0006 left behind `events._maybe_mirror_to_pg`,
calling `pgstore.mirror_one`, and the draft recorded both as current. They are
gone, and their absence is the design rather than an omission:

- `pgstore` holds the READER credential, and `mindhive_reader` is
  `default_transaction_read_only` **at the role level** — a single-row `INSERT`
  from there was refused by the server, so that seam had been raising on every
  call since the day it was written;
- a row-level writer would have had to compute `seg_line` itself, while
  `pgmirror.scan_segments` numbers by PHYSICAL line, blank and torn rows
  included. The two would have agreed until the first blank line and then
  diverged with no obvious cause.

`scripts.core.event_write` (a module in the CC-v3 repository, never on this
process's path) and `pgstore.mirror_one` appear nowhere in `src/`. They are
**history**, kept named here because a reader who meets them in an old plan or
an old review needs to be able to find out they are dead — deleting the names
would leave that reader with nothing to search for. What must not happen again
is this section presenting them as live, and that is now a gate rather than a
promise: `tests/test_docs_parity.py` asserts every identifier this section
presents as CURRENT still exists in `src/`.

The mirror follows the ledger through `pgmirror.refresh_changed` instead —
reload only the segments whose bytes moved, then re-run parity — called by the
scheduled `autosync` run and by `hive pg refresh`. One loader, one credential,
no second code path to drift. `MINDHIVE_EVENT_BACKEND` keeps its meaning for
READS only, and the `file` default returns before any import, so
`import mindhive.events` never pays for `psycopg`.

#### Read-only, in layers, and only one of them is the defence

The seven tools exist so an AI issues SQL instead of shell commands. Four
answer "show me events" (`query_database`, `get_recent_events`,
`search_events_by_payload`, `get_session_summary`); three answer "what IS the
state", which is the question an agent actually opens a session with:
`get_latest_event` (one row, the tip of one session), `search_events_by_type`
(every event of one kind, newest first) and `get_current_state`
(`DISTINCT ON (event_type)` — the newest row of every kind, in one round trip).

`get_current_state` is **not** `STATE.md` and does not re-run the projector;
there is no `current_state` table. Inventing one would put two derivations of
"the current state" in the system, with the SQL one looking authoritative
because it came from a database.

**Every paged tool defaults to 20 rows** (`pgtools.DEFAULT_LIMIT`) and takes
`offset`, capped at 10,000 because OFFSET makes the server walk and discard every
skipped row. The bound is the point rather than a detail: the context window is
the resource being protected, and a tool that CAN return thousands of rows
eventually will. The enforcement that makes that safe is **L5 — the
`mindhive_reader` role** (`003_roles_and_views.sql`): it holds no privilege on
`mindhive.events` at all, sees only the `security_barrier` view
`mindhive_ro.events`, and carries `default_transaction_read_only`,
`statement_timeout`, `lock_timeout` and `log_min_duration_statement = 0` set on
the ROLE, so they survive a bug in the Python layer. The Python-side SQL shape
check is a belt for message quality; the design property under test is that
deleting it changes no outcome for any hostile input.

Namespace scope is the **view's** predicate, not a model-written one:
`WHERE project = current_setting('mindhive.project', true)`. An unset setting
yields NULL and therefore **zero rows** — the fail-closed direction — and
`project` is absent from the select list, so the model cannot write a namespace
predicate it could get wrong.

It can, however, **choose** the namespace: `project` is an ordinary tool
argument, and naming another one reads that project's ledger. That is stated
here because the code has always worked this way and two comments used to claim
the opposite. Every namespace in this mirror belongs to the same operator and
the same machine, so this is context hygiene rather than a boundary — but an
agent asked about *this* repo can read another one's memory if it names it.

**Three residuals this does not fix**, named because naming them is the
mitigation available at this layer:

1. **Queryability, not disclosure.** The file ledger already holds prompts and
   responses in plaintext. What the mirror adds is that bulk-searching them for a
   secret becomes one tool call instead of a grep over 900+ segments.
2. **A second copy, on a service.** The mirror is a full copy of the ledger
   living outside `~/.mindhive`, with its own WAL, its own backups if anyone
   configures them, and its own file permissions. `hive gc` does not reach it and
   deleting a namespace's segments does not delete its rows — `hive pg rebuild`
   is what makes the mirror forget.
3. **Every AI statement is logged in plaintext.** `log_min_duration_statement = 0`
   is set on the `mindhive_reader` role (deliberately: it is the audit trail for
   what an agent actually asked). That log is a third copy of every query, and
   queries carry the search terms — which is the part worth knowing before
   pointing this at anything shared.

A redaction control (`MINDHIVE_PG_REDACT`, an opt-in list of event types whose
text is elided in the envelope) was designed and **not built**. It would not
address any of the three above — all three are about copies of the data at rest,
not about what the envelope shows — so shipping it would have bought the
appearance of a privacy control rather than one.

#### The vector index (006), and why the bytes are BYTEA

`mindhive.event_embeddings` is the second table in this schema, keyed by
`(project, segment, seg_line, model)` — the ledger's own ordering key plus the
encoder's name, so a vector lines up row for row with `mindhive.events` and two
models can coexist instead of silently mixing two incompatible geometries under
one name.

**There is no pgvector on a stock server**, and installing an extension is not
something this project can ask of a machine it does not administer. So the
vector is a `BYTEA` of little-endian float32, L2-normalised on write (cosine
similarity is then a plain dot product), and the ranking happens in NumPy. That
was measured before it was chosen, over the same 1,000 x 384 vectors:

| shape | COPY in | rank 1,000 | on disk |
|---|---|---|---|
| `BYTEA` + NumPy | 19.6 ms | 10.4 ms | 2120 kB |
| `real[]` + SQL dot product | 107.4 ms | 101.7 ms | 2120 kB |

Same size on disk, 5.5x faster to load, 10x faster to rank. Three CHECK
constraints carry the invariants the reader cannot re-derive:
`octet_length(embedding) = dim * 4` (a short blob reshapes without error into a
matrix of the wrong length — every score after it is garbage that looks like
data), `dim > 0 AND dim <= 4096`, and `seg_line >= 0`.

Two views, both `security_barrier` and both owned by `mindhive_owner`:
`mindhive_ro.event_embeddings` is metadata only, exposing
`octet_length(embedding) AS embedding_bytes` rather than the bytes, so a
`SELECT *` from a curious agent cannot blow a context window with 384 floats per
row; `mindhive_ro.event_vectors` carries the bytes and exists for the one tool
that ranks them.

`hive pg embed` fills it, on the admin credential, and is **incremental by
content**: the stored `event_id` is the same content hash the chain covers, so
an event rewritten at the same `(segment, seg_line)` is re-encoded rather than
left with a stale vector — and a stale vector still returns confident-looking
hits, which is why presence alone is the wrong test. Three orderings are
deliberate: probe the recorded keys on a short connection **closed before the
model loads**, encode outside every transaction, then DELETE the superseded rows
and COPY the new ones in **one** transaction. Measured on this ledger: 664
vectors in 47.9 s first run, 0.98 s on a re-run with no encoder loaded at all.

#### Status: proven against a server, with the gaps named

`psycopg` and `mcp` are optional extras (`.[pg]`, `.[mcp]`), guarded the way
`vectors.py` guards chromadb, and the base install stays dependency-free.
Everything answers structurally with neither installed — which remains the state
of CI.

**Every statement here has now been executed by PostgreSQL 17.11.** What that
run established, on the real 37k-event ledger unless noted:

- all six migrations apply; a second run applies nothing; a migration whose
  bytes changed after being recorded is REFUSED rather than re-run or skipped;
- 37,435 events loaded, and the file ledger's chain head equals the mirror's —
  `exact`, `lag 0`. That single hash is what "byte-faithful" means here: verbatim
  `ts`, a JSONB round trip that preserves payload semantics (`canonical_json`
  sorts keys, so key reordering is invisible to the chain), and an ordering
  contract that matches Python's;
- the gate is non-vacuous in all four directions — proven on a constructed
  fixture, because the real ledger cannot exercise the tie-break (33,974 distinct
  timestamps, 868 shared across segments, **zero** that invert under
  `session_id` ordering);
- the reader role is not a superuser, carries `default_transaction_read_only=on`
  and `statement_timeout=5s`, has no privilege on `mindhive.events` at all, and
  returns **zero rows** when the namespace setting is unset;
- all eight tools answer; a second statement smuggled into `query_database` does
  not execute (checked by looking for the table it would have created);
- `EXPLAIN` measurements are in the two sections above.

**Three defects only a live server exposed**, all fixed here: `mirror_state` was
unreadable by the reader role (003 revoked the schema out from under it, so
`mirror_health` could only ever fail closed — that is what 004 fixes); 003 left
views and three tables owned by the migration runner, which matters because the
views are deliberately not `security_invoker`; and 002's ordering index is
unmatchable rather than merely unused (005).

**What is still NOT proven, stated plainly:**

- **The `pg` tier is deselected by default**, alongside `node` and `browser`.
  It now runs on CI in a job of its own, against a `postgres:17` service
  container, but a green *default* run still says nothing about the SQL — read
  the pg job, not the badge.
- **The semantic tier needs an encoder CI does not install.** `hive pg embed`
  and `search_events_by_meaning` need `pip install -e .[vectors]`; without it
  both answer `deps_missing` and name the extra. The pg-tier LOADER tests need
  neither import -- they replace the encoder with a deterministic stand-in, and
  packing is `struct` plus `math` -- so they run on CI; the four SEARCH tests
  skip there and say which extra is missing, and the registry gate asserts the
  shape of the refusal instead. The real `all-MiniLM-L6-v2` runs in exactly one
  test, which is the only one that can say the feature WORKS rather than that
  it runs, and it runs on a developer machine, not on CI.
- **The cluster used is a scratch one** (port 5433, loopback, trust auth,
  binaries unpacked into a temp directory). Nothing about a production
  deployment — TLS, real authentication, backups — has been exercised.

## 5. Invariants — do not break these

1. **Append-only, verbatim.** Never mutate, reorder, or delete ledger lines.
   Corrections are new events (that's what `sop update`, `rule_revised`,
   `memory_consolidated` are for).
2. **Projections are deterministic.** Same events in → same bytes out. No
   clocks, RNG, or model calls inside `projector`, `briefing`, `dedupe`,
   `sop.fold`. Tests assert this.
3. **Reads are total.** New code must skip malformed/unknown payloads, not
   raise — the ledger contains history written by every past version.
4. **Context delivery is budgeted, in the unit the loader enforces.**
   MEMORY.md caps: lessons 10 (pinned first), decisions 5, timeline 40, SOPs
   `sop.DIGEST_ITEM_CAP` (titles + pointers — `BLOCK_FULL_BUDGET` bounds the
   STATE.md rendering, a different file with no cut, and must not be cited as
   MEMORY.md's bound: at 404a7c0 it was set *above* the delivery channel and
   had never bitten). A cap on ITEM COUNT does not bound bytes, and bytes are
   what the loader cuts on — so anything unbounded that rides MEMORY.md is a
   regression, and so is a bound whose unit is not the loader's.
5. **The semantic layer is optional.** Base install works without
   chromadb/sentence-transformers (`vectors._require_deps`). The Postgres
   mirror joins it under the same rule: `psycopg` and `mcp` are extras, and
   `import mindhive.events` must not pay for either.
6. **The Postgres mirror never becomes the source, and never re-derives the
   order.** The file ledger is the sole writer; `MINDHIVE_EVENT_BACKEND` stays
   `file` and nothing in `src/` may set it. The mirror's ordering contract is
   `(COALESCE(ts,''), segment, seg_line)` — the segment **filename**, never
   `session_id` (they disagree, live, on `verify` vs `verify-e2e-001`), and
   `COALESCE` because Python sorts a NULL `ts` first where Postgres sorts it
   last. Both halves are what chain-head parity rests on, so a "simplification"
   of either reads as data corruption rather than as a sort key (§4.18).

7. **A payload number's SPELLING is not part of its identity — except at zero.**
   The mirror's `JSONB` column stores numbers as `numeric`, which has no
   exponent notation and no signed zero. Exponent forms survive the round trip
   because the printed value re-parses to the same float64; `-0.0` does not, and
   comes back `0.0`. So nothing may depend on how a number was written, and
   anything that widens what a payload can hold here is checked against
   **chain-head parity**, never against a row count (§4.18).

## 6. Adding a new event type (the extension procedure)

1. Register it in `events.SEMANTIC_TYPES` (curated, recallable) or
   `DETERMINISTIC_TYPES` (telemetry), with a comment saying why it exists.
2. Define its payload contract in a dedicated module (pattern:
   `changelog.py`, `sop.py`) with a `validate_payload` enforced in
   `commands.ledger.cmd_append` (re-exported as `__main__.cmd_append`) — the
   store stays type-agnostic; the contract lives
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
