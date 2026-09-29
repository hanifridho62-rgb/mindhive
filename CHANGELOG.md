# Changelog

All notable changes to MIND HIVE are recorded here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).

**Which version is this?** Release numbers are the Claude Code plugin's
(`.claude-plugin/plugin.json`), because that is what users install. The Python
package in `pyproject.toml` has stayed at `0.1.0` and is not versioned
separately yet. The repository carries no git tags, so each release below is
dated by the commit that bumped the manifest, and that commit is named.

Entries older than this file (2026-09-28) were reconstructed from the git
history and grouped by what each release window introduced. They summarise;
`git log` remains the complete record.

## [Unreleased]

### Added

- **Blueprint P11 — the traceability matrix and the audit that closes the
  blueprint** (RP §3, §4.7, §10.3; MEB §3.1, §153):
  - `IMPLEMENTATION_PLAN.md` at the root: the RTM for blueprint v1.0 and v1.1 with the commit that realised each row; the audit table `| Bab / Fitur PDF | Modul / Berkas Kode Sumber | Berkas Pengujian (Tests) | Status Verifikasi |` — one row per item, every status parsed from the junit of one full pytest run rather than typed; the MEB LOC forecast beside measured sizes; an ISO/IEC 25010 matrix of measured values; the three benches; the deviation register; and the real outcome of the one `hive mindnet ab --execute --record` run. `tests/test_implementation_plan.py` holds it to that: the commit it names must exist, every path it cites must exist, every status is one of seven words, the LOC rows equal `scripts/loc_report.py`'s.
  - `scripts/loc_report.py`: the MEB §3.1 rows as baseline / target / measured / verdict (`python scripts/loc_report.py [--json] [--check]`; `--check` exits 1 only on the ceilings the MEB states as hard). `tests/test_loc_targets.py`.
  - README `### Prerequisites`: the operating systems the CI matrix runs, the Python floor `pyproject.toml` declares, the Claude Code CLI the agent-spawning commands need, and measured hardware figures. `tests/test_readme_prerequisites.py` reads the answers from those files, not from the README.
  - The five golden rules SOP is registered from `docs/SOP_FIVE_GOLDEN_RULES_COMPACT.md`: `hive sop add --file docs/SOP_FIVE_GOLDEN_RULES.md`, as the P8 doc instructed, is refused by `sop.MAX_TEXT_CHARS` (4000 — the doc is 12,423 chars). The doc now says so, and `tests/test_sop_golden_rules_doc.py` keeps the compact body under the cap and in step with the five rule headings.
  - `docs/session-legend.md` gains the `fabricate.py` row P10 left out.
- **Blueprint P8 — the operator surface, two benches, and the documentation
  that closes the foundation** (spec §1.5, §1.6, §4.7, §9.3, §9.4; MEB K15,
  §10.1):
  - `hive lintester --weight-model {log_linear,linear}`: picks the weighing fusion for one mission by carrying it as `MINDHIVE_WEIGHT_MODEL` for the mission's duration (restored afterwards, even on failure; lanes inherit it); the report gains `weight_model` / `weight_model_reason` (JSON) and a `weight model:` line (plain), naming what really weighed.
  - `hive worksession list --page N --page-size M`: pages the sessions, `--initiatives` and `--session S` listings through `pgshape.paginate_records` — the §10.8.2 envelope under `--json`, a `page X of Y` footer with the exact next-page command in plain text; out-of-range values are clamped by the helper, never refused. Without a flag the output is unchanged.
  - `MINDHIVE_UI_IDLE_CUTOFF_S`: `hive ui` takes its idle cutoff from the variable when `--idle-cutoff` is absent (`--idle-cutoff` now defaults to None); a value that is not a positive finite number is ignored with one stderr line and the UI still starts; the flag wins; `hive dashboard` does not read it. New `server.lifecycle.IDLE_CUTOFF_ENV` and `idle_cutoff_from_env(environ=None) -> (seconds, note)`.
  - `hive bench cli-startup` and `hive bench recall-semantic` (MEB K15, spec §4.7) measure CLI startup and lexical-vs-vector recall latency. The published 45 ms and 14.7x figures are printed as claims beside the measurements, never as gates; failed children and raised searches are counted, never timed as successes. Includes `tests/test_cli_startup.py`, which checks that importing the CLI loads no heavy dependency.
  - README gains an `## Environment variables` registry: one row for each of
    the 37 `MINDHIVE_*` / `MINDNETWORK_*` variables the code reads, plus the four
    third-party ones it reads or sets. `tests/test_env_registry.py` holds the
    registry and the code together in both directions. README also gains the
    spec install line `pip install -e .[vectors,dev,tui]`, with what each extra
    buys (checked by `tests/test_pyproject_extras.py`), and a `## Disaster
    recovery` section.
  - `docs/RUNBOOK_RECOVERY.md` (spec §9.4) covers chroma corruption, a stuck
    embed queue, a broken hash chain, stale locks, a diverged Postgres mirror and
    lost projections. Each has detection, exact commands and verification, plus
    a table of the mechanisms spec §9.4 names that the code does not have.
  - `docs/SOP_FIVE_GOLDEN_RULES.md` (spec §9.3) gives the five rules, each with
    the file and test that enforces it and where nothing does. It also shows how
    to load them as an optional, non-mandatory SOP with `hive sop add`.
  - `docs/DATA_MODEL.md` §4.19 covers the Chroma secondary projection: what
    feeds it, its three collections, the write paths (`hive embed`, the embedding
    watcher) and the read path (`hive recall --semantic`), rebuilding it from the
    ledger, and its invariant.
  - `tests/test_constants_registry.py` reads the MEB's appendix 10.1 — thirteen
    calibrated constants with owner module, type and value — back against the
    code in one table.
- **License and contributor documentation** (blueprint P7, spec §2.14): the
  Apache License 2.0 in `LICENSE`, `CONTRIBUTING.md`, this changelog, an
  `SPDX-License-Identifier: Apache-2.0` line at the top of every source file in
  `src/mindhive/`, `hooks/`, `scripts/` and `build/`, and PEP 639 license
  metadata in `pyproject.toml`. The applied Postgres migrations keep their
  bytes, and `skills/caveman/` keeps its MIT license.
- **Blueprint P5 — the fixes the spec's bug list and the MEB's hook and UI
  items asked for:**
  - Preflight gate (spec §10.9.2, bug 11): `orchestrator/environment` gains
    `EXIT_ENVIRONMENT_REFUSAL` (3), `FATAL_REFUSAL_FRAME`,
    `check_claude_environment()` and `enforce_preflight_gate()` over
    preflight's single probe, failing open when the probe is inconclusive;
    `hive lintester` refuses with that frame and exit code, and the mission
    dashboard no longer takes the mission down when its display task raises.
  - Hooks (MEB HOOK-01, spec §2.8 bug 8): `hooks/lib.sh` reads JSON fields
    through `extract_json_field` — payload on stdin, field name as argv, so
    neither is ever program text — and `mindhive_session_id` rejects ids that
    do not start with a letter or digit; `hooks/lib.ps1` gains
    `Resolve-MindHivePython -Windowed`; both session-start shims run
    `hive plugins cache-check --hook` after the state load.
  - Dashboard pagination and caching (spec §10.8.2, §10.5.1, §10.11):
    `pgshape.paginate_records` / `trim_field_content`, optional
    `?page=&page_size=` on `/api/memory` and `/api/sessions` (the bare list is
    unchanged when neither is given), the public memo names `BoundedMemo`,
    `compute_project_fingerprint`, `gated_fold`, `DASHBOARD_STATS_MEMO` and
    `DASHBOARD_FOLDS_MEMO`, and the HIVE MIND `/api/skills/status` and
    `/api/skills/stats` gated on one composite stamp of every namespace.
  - Theme, rail and static script (K12, spec §2.12 bug 12, §2.13 bug 13, Pillar
    4): the palette meets WCAG AA — `bg` #12151c, `text` #e2e8f0, `dim` #9aa3b8,
    `dimmest` #798297, with `border`/`success`/`warning`/`danger` beside `accent`,
    and `theme.BLUEPRINT_PALETTE`, `relative_luminance`, `contrast_ratio`; the rail
    splits into `<nav aria-label="sections">` (the sixteen section buttons) and
    `#actions` (the ten commands) inside a sticky `#rail`; `static/dashboard.js`
    (`window.MindHive.paintInto`/`pollOverview`) is inlined first in the page
    script and served at `GET /static/dashboard.js`, read through
    `importlib.resources`, shipped by `package-data` and by `--collect-data
    mindhive` in the frozen build. Also fixes the P1h carriage return in
    `dashboard_js_actions.py` that had made the dashboard's inline script
    unparseable on this branch.
  - Review fixes (an adversarial pass over P4b and P5): the mission board's
    held backlog keeps the spec's first 50 events and the newest past capacity,
    so `mission_started` survives a burst, and a board that stops says why;
    the refusal line puts `FATAL_REFUSAL_FRAME` first, as the spec prints it;
    `hooks/lib.sh` drops its last `local` and the bashism sweep now covers it;
    a paged `/api/memory` says when its 200-row selection dropped rows (a
    `limit` key) instead of reporting the selection as the store's total; the
    Bug 13 guard reads the whole rail; K12's `--border`, `--danger` and
    `--warning` reach the initiative pill, the CI-failed card and the tripped
    status pill; every contrast ratio the docs quote is pinned.
- **The PRD assembly line** (blueprint P10, spec Bug 9, TODOS #15):
  `hive fabricate compile|dispatch|run|list`, ported by hand from the branch
  `claude/prd-assembly-line-40e466` (`3a45aea`), whose merged PR never reached
  master. `compile` turns an Executable PRD into a loop goal dir
  (`.hive-fab/<fab-id>/`: PROMPT.md, fix_plan.md, AGENT.md, loop.toml) plus a
  `fabricate`-tagged roadmap keyed by the fab id; `dispatch` arms the
  per-user Windows scheduled task that starts it; `run` executes one loop
  launch keyed to the fabrication's plan, so a usage cap interrupts that plan
  and a completed launch completes it. With it: the `fabrication` event type
  (deterministic; its contract `fabricate.validate_payload` is enforced at the
  raw append surfaces through `contracts._PAYLOAD_CONTRACTS`, and the type is
  in `pgschema.EVENT_TYPE_CLASSES`), loop-mode auto-resume (`autoresume`
  rows carry `mode` and `goal_dir`; a `loop`-mode delivery relaunches the loop
  engine on its goal dir instead of composing a one-shot prompt, through the
  shared `autoresume.register_windows_task` / `unregister_windows_task`
  timer primitives) and `loop.engine.run_loop(plan_id=…)`, which keys the
  interruption marker to an existing plan instead of the per-run id. A
  loop-mode wake-up of a fabrication goes through `fabricate.run` rather than
  the bare engine (new here, not on the branch), so it records its `run`
  event and a completed wake-up closes the fabrication and drops its timer.
  The handlers live in `commands/orchestration.py`, the parser in
  `commands/parsers/orchestration.py`; `tests/test_fabricate.py` comes over
  with its 41 tests, one of them now given a prompt runner that fails rather
  than spawning `claude`, plus five wake-up cases (46).
- **Plugin cache drift check** (blueprint P4b, spec §2.8 bug 8):
  `hive plugins cache-check [--json] [--sync] [--hook]` compares the plugin
  Claude Code loads with this checkout by version and by a content hash of its
  components, and names the files that differ. Repair goes only through
  `claude plugin update`; the session-start form syncs only when
  `MINDHIVE_PLUGIN_AUTOSYNC=1`. The update runs in a bounded process tree
  (the 45 s timeout holds on Windows, where `claude` is a `.cmd` shim), and a
  sync is reported done only when a second comparison finds the copy in
  sync; the preflight probe gets the same bound. The plugin catalog snapshot now carries a
  `version_hash` and is rescanned when the plugin version or the installed set
  changes.
- **Blueprint foundation v1, P0–P4** (branch `claude/blueprint-foundation-v1`):
  - P0, P0b — `events.py` contracts carved out into `contracts.py` and
    `core/models.py`; SEC-01/SEC-02; `append_event(fsync=)`.
  - P9, P9b — `__main__.py` split into `commands/` and `commands/parsers/`;
    `hive verify --json/--strict`.
  - P1a, P1a′, P1j — the `core/` package: locks, dreaming, recommend, and the
    MEB models (Poisson rates, the integration-breaker Markov chain, CLT bounds).
  - P1b — work sessions, initiatives and roadmap tags (`worksessions.py`).
  - P1c — the adaptive tail (`adaptivetail.py`).
  - P1d — the SOP authorship gate (`advice.py`).
  - P1e — a whole-tree kill that survives its leader.
  - P1f — the gateway interceptor, integration breaker and lane quota.
  - P1g — the native CI runner: `hive ci run|status|hook-install`.
  - P1h — the background embedding watcher and HNSW tuning.
  - P1i — the storage SPI and replay migrations: `hive db migrate|status`.
  - P1k — `hive ui`, an ephemeral dashboard server with an optional ASGI adapter.
  - P2 — a fail-closed suite gate and read-only test immutability for the judge.
  - P3 — a public, fail-closed `task_lock`.
  - P4 — log-linear calibrated skill weights and Laplace-smoothed skill promotion.
- **Since 0.5.0, before the blueprint:** the lintester mission pipeline
  (`hive lintester`) with lane merge-back and a terminal board; the Postgres
  read mirror (`hive pg`) with eight AI tools and search by meaning;
  participatory consensus (`hive consensus`); smart-zone session rotation; the
  mandatory SOP tier; work replenishment (`hive replenish`); task-specific
  briefs for spawned agents; the POSIX hook family; the benchmark command
  (`hive bench`); dashboard poll-cost gates and keep-alive.

### Changed

- **The pending-embed queue is appended to, bounded, and counted before the
  lock** (merge-safety follow-ups to PR #136): `state/pending_embed.json` is
  now one JSON object per line — a semantic append is one `open(..., "a")` and
  one write instead of re-serialising the whole document through `os.replace`
  inside the append lock — and is compacted (one entry per row, the newest
  kept) only once it passes `embedqueue.QUEUE_MAX_BYTES` (256 KiB), so a
  namespace no watcher drains no longer grows it without limit. The older
  `{"entries": [...]}` document is still read, so a claim taken before the
  upgrade drains. The segment newline count that gives `seg_line` (3.24 ms on
  the largest live segment) now runs before the append lock and is reconciled
  inside it (`embedqueue.newline_cursor`), so the lock holds a `stat`, not a
  read. `tests/test_embedqueue.py`.

### Fixed

- **`hive failures capture` takes the harvest lock.** `failures.capture_dir`
  stamps each `tool_failed` row with the transcript line's own timestamp and
  dedups against a snapshot taken before the walk, so the command run beside
  the dashboard's auto-harvest sweep (which reaches the same `capture_dir`
  under `harvest.catch_up`'s lock) was a second writer over one transcript
  directory, minting byte-identical rows the append-only, hash-chained ledger
  keeps for good. While a live harvester holds the lock the command now
  answers `{"new_failures": 0, "files": 0, "locked": true}`, appends nothing
  and exits 0, the way `hive harvest --catch-up` does; rerun once that pass
  is over. The mutex is public as `harvest.harvest_lock` now, so every CLI
  door to the writer holds the one `catch_up` holds.
- **The embedding watcher no longer records syncs that never happened.**
  Without the vector extras (`pip install -e .[vectors]`) it claims nothing,
  leaves the queue for the install that can drain it, and says why once per
  watcher (`run_once` / `process_marker` return `skipped`;
  `vectors.missing_extras`). It used to claim, append `vector_sync_started`,
  fail on the import and leave a claim on disk every cycle.
- **Duplicate queue entries no longer poison a claim.** A row a claim names
  twice is embedded once (`embedqueue.dedupe_entries`,
  `VectorStoreManager.embed_batch`); recovery requeues each row once, even
  when a claim's unlink was refused and it is recovered again, and does so
  under the append lock so an append that lands mid-merge is not published
  over. chromadb refuses an `add` that repeats an id, which failed the whole
  batch and left the claim to be recovered — and refused — again.
- **A failed semantic read no longer looks like "no hits", and a read no
  longer writes the index** (docs/DATA_MODEL.md §4.19; merged from
  `claude/semantic-recall-observable`):
  - `vectors.semantic_recall_events` swallowed a failed query and returned
    `[]`. Every step that can break now raises `vectors.SemanticQueryError`,
    naming the stage, the collection, the namespace and a repair chosen by
    what broke. `hive recall --semantic` exits 1 on it (refusals stay exit
    2); `/api/recall` answers 500 and the recall view says the search
    failed; HIVE MIND recall keeps the other namespaces' hits beside a
    `{project, error}` row; a semantic graph build that fails keeps none of
    its semantic edges and shows a `semantic_error` banner.
  - One read changed `chroma.sqlite3`, `data_level0.bin` and `length.bin`:
    chromadb 1.5.9 has no read-only client. Every read now answers from a
    fingerprint-checked private copy (`vector_snapshot`), reused while the
    index is unchanged, never taken under `chroma_lock`, with `LIVE_LOCK`
    refused before anything is copied. A client whose read failed on a
    damaged segment is never torn down (measured: that corrupts the heap,
    `0xC0000374`), the failure is remembered against the bytes it happened
    on, and a cut HNSW header is refused before chromadb opens the copy.
  - `hive embed --project`, because the repairs name the namespace
    verbatim; `hive recall --limit` below 1 is a usage error; the watcher's
    id probe reads a copy, in batches, asking each id once.
  - `hive bench recall-semantic` reads through the same path, one copy per
    run reported through `vector_snapshot.held`. It used to copy the index
    itself under a redirected `MINDHIVE_HOME` and empty chromadb's client
    registry (`clear_system_cache`) to delete the copy. A long `unavailable`
    reason or search error now keeps both ends, so the repair survives the
    300-character cap.
  - Round-3 review: a panic in chromadb's Rust core (pyo3's
    `PanicException`, a `BaseException`) is a failed read like any other,
    not a raw traceback or a HIVE MIND search that lost the other
    namespaces' hits; a log chromadb cannot read (`InvalidArgumentError`
    "Failed to pull logs from the log store") is damage to repair, not a
    refused argument; a quarantined `unsafe-<hash>` namespace is told how to
    reach it without `--project`, which refuses that name; a reader that
    overlapped a failure no longer opens and fails on the same bytes again;
    a hit chromadb returns without its document is a failed query instead of
    an `AttributeError`. The error, its repair and the judge moved to
    `vector_errors`.
  - The repair follows how far the read got. `hive embed --rebuild` drops a
    collection without reading it: measured, it mended every damaged
    `ledger` or `events` collection chromadb could open, and failed where
    chromadb cannot open the files. A failed count or query, or a cut
    segment of either collection, is rebuilt first and deleted if the read
    still fails; other `open` failures and the watcher's collection are
    deleted. The header check judges only segments the index still names:
    chromadb leaves a dropped collection's segment directory behind, and the
    cut header a rebuild had just dropped refused every read after it.
  - Round-4 review: a failure is remembered per fingerprint, and only the
    way in forgets one, once it expires — one memo per collection let a
    failure on older bytes replace the one on newer bytes, which a reader
    then opened and failed on again, and a way in on newer bytes dropped
    the memo a copy still in flight asked at install; past the cap a source
    is refused only while it has a failure no read of that collection has
    got past since (`_PASSED`), and the refusal names a restart of the
    process after the repair. A copy never replaces a newer one
    (`_Snapshot.taken`). Each file is copied with `shutil.copyfile`:
    `copy2`'s CopyFile2 refused a writer opening a 32 MB segment file 225
    times during three copies. The header check is the read's, per
    collection (`reading(check=…)`, cached per copy, never judged): with
    `events` cut, `ledger` is still read from the same copy, whose client
    closes cleanly (pinned in a child process, 5 runs of 5); segment ids
    are compared in canonical UUID form. An event query that answers
    nothing from a collection holding rows it could have returned is
    `vector_errors.UnansweredQuery`, a failed query — measured on a
    `collections.dimension` of NULL, count 1,300, every query empty, and
    `hive recall` fell back to the file index in silence; a hit without its
    document (`MissingDocument`) and an unanswered query are answers, so
    the judge never poisons the copy for them. `hive embed` prints one
    `error:` line for a write that failed and exits 1
    (`vector_errors.embed_failure`), and `LocalVectorIndex` catches a panic
    in the encoder like any failure.
  - A cut segment file is contained. `check_segments` checks every
    persisted segment's files against its own `header.bin` —
    chroma-hnswlib's 100-byte header (`HNSW_HEADER`): `data_level0.bin` and
    `length.bin` shorter than its element count needs, `link_lists.bin` cut
    inside an element or claiming more levels than it allows, a header at
    odds with itself — and refuses the read as `DamagedSegment` naming the
    collection, so the repair is the rebuild first. Measured, a
    `link_lists.bin` cut in half had segfaulted the reading process in 19
    reads of 23 and hung it in 2, and a `data_level0.bin` cut in half had
    answered without an error, missing its two best hits. And the chromadb
    calls of a read run in a child process (`vector_reader`,
    `vector_snapshot.ISOLATE_READS`): a segfault or a hang there is
    `ReaderDied` / `ReaderHung` here — a failed count or query with the same
    repair — and the child is closed and its copy deleted, where an
    in-process failure had to keep its client for good. A read through the
    child costs what an in-process one does (1.2 ms median either way on a
    1,300-row scratch index; `hive bench recall-semantic` on a copy of the
    real index: warm query median 9.8 ms in-process, 10.9 ms through the
    child, two isolated runs 10.9 and 14.0 ms); starting the child costs
    1.2–1.9 s, once per copy, inside the model load the bench times apart
    (8.4–10.5 s). It ends with the
    parent, waiting on the parent's process handle — never by reading its
    stdin: a read left pending on that pipe stalled `import numpy` in the
    child for good.
- README said the dashboard's auto-harvest polls every 180 seconds by default
  (`MINDHIVE_AUTOHARVEST_SECS`); the code's default is 3 seconds, floor 1
  (blueprint P8, found while building the environment-variable registry).

## [0.5.0] — 2026-08-02 (`0214668`)

### Added

- `/caveman`: ultra-compressed replies at lite/full/ultra/wenyan intensity,
  vendored from JuliusBrussee/caveman under the MIT License.
- MindNet: the hierarchical task graph, the phase spine with its sidecar and
  injector, PreToolUse enforcement (phase block, language lock) opt-in per
  checkout, the faculty cat board, `hive mindnet orchestrate` (dry-run by
  default), per-task worktree isolation, and "not finished until the PR is
  green".
- The prompt gate and playbook directions for code work.
- Content packs: `hive packs check|report` and three stack packs.
- The GateGuard policy tier, structured failure capture and a context budget
  gate.
- `/mindhive` fused with the ULT-IDEA rigor on the memory spine.

## [0.4.0] — 2026-07-09 (`d326444`)

### Added

- `/mindhive-orchestrator`: the single centralized ten-stage pipeline, with
  `/circle` and `/engineering-loop` decoupled from it.
- The parallel-orchestration layer: a file-complexity cluster database and
  shard planner (`hive clusters`).
- Namespaces: a `--project` override and `hive projects`; store hygiene with an
  opt-in prune (`hive gc`).
- Relevance-ranked lexical recall.
- Dashboard: skills and plugins in one view, with per-card calls, per-skill
  call history and on-demand reflection of a single failure.
- A cross-process write lock for incident files.

## [0.3.0] — 2026-07-04 (`34aabfe`)

### Added

- `/engineering-loop` (circle × lfg) and the skills invoke channel
  (`hive skills invoke`).
- Dashboard: the live skills-and-plugins list with green/red status, the
  Improvements view, the Reflect action and the efficiency strip.

## [0.2.0] — 2026-07-03 (`625e80e`)

### Added

- `/mindhive`: the idea-to-execution pipeline.
- The learning loop: failure capture (`hive failures`), incident records with a
  recurrence gate, reflection, and prevention rules (`hive rules`) promoted into
  `SOUL.md` and demoted when they leak.
- A real `hive loop` run launches the web dashboard.

## [0.1.0] — 2026-06-25 (`ce1258f`)

### Added

- MIND HIVE packaged as an installable Claude Code plugin, with `/circle` as a
  skill and a slash command.
- `hive loop`: a headless Claude Code driver with rolling token and cost
  budgets, a dual-condition exit gate, a circuit breaker and a smart-zone
  context reset (`hive loop init`, `hive loop status`, and the run itself with
  `--dry-run`, `--live` or `--force`).
- The core it runs on, built from 2026-06-20: the event-sourced, hash-chained
  ledger with STATE.md and briefing projections, the Claude Code hooks, recall,
  the dashboard graph, the operating directives, and the end-of-session code
  review queue.
