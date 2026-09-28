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
