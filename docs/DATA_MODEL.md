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

**Deterministic** (`DETERMINISTIC_TYPES`) — machine-harvested telemetry:
`file_touched`, `command_run`, `git_diff`, `session_start`, `session_end`,
`compaction`, `plugin_catalog`, `user_prompt`, `assistant_response`,
`loop_*` (autonomous loop lifecycle), `tool_failed`, `rule_*` (prevention-rule
lifecycle), `trigger_trained`, `invoke_requested`/`invoke_result`,
`skill_used` (`{name, plugin?, ok, text, args?}` — optional `args` is the
capped invocation ask, the recorded "why" behind Oversee's per-session skill
trail; contract owned by `hookcapture.py`), `memory_consolidated`,
`prompt_enriched`/`enrich_toggled` (Oversee prompt enrichment, `enrich.py`:
recorded enrichment acts and the default-on toggle's audit trail — the
settings sidecar `enrich.json` is the source of truth).

Registry/audit types are **exempt from merging everywhere**: `code_change`,
`sop`, `learning` are never absorbed by consolidation (`consolidate.py`) and
never collapsed by display dedupe (`dedupe.py`) — similar wording does not
mean same fact for audit rows, and the SOP fold replays every lifecycle event.

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
- `memory_consolidated.absorbed[]` holds `(ts, type, fingerprint)` keys of
  memos hidden (not deleted) from briefings by the dreaming pass.

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
3. Decide merge semantics: audit/registry types must be exempted in
   `consolidate.CONSOLIDATE_TYPES` and `dedupe.collapse`.
4. Decide projections: LEDGER body (`projector.build_ledger_entries`),
   STATE/MEMORY block, briefing section, dashboard API + view.
5. Add tests mirroring `tests/test_sop.py` (contract, fold totality,
   delivery, CLI) and run the full suite.
6. Record the change: `hive log <category> "…"` — the change-log gate is
   mandatory for `src/mindhive/` and `tests/`.
