# MIND HIVE

[![CI](https://github.com/hanifridho62-rgb/Lintester-project/actions/workflows/ci.yml/badge.svg)](https://github.com/hanifridho62-rgb/Lintester-project/actions/workflows/ci.yml)

Deterministic, event-sourced memory for long-running Claude sessions.
Append events; deterministic projections rebuild everything. Nothing is hand-edited.

## Quick start

    pip install -e .[vectors,dev,tui]
    hive state                       # what the record says right now
    hive append decision "chose X over Y because Z"
    hive sync                        # refresh the MEMORY.md a session auto-loads
    hive context --check             # is the auto-loaded surface still honest?

Four commands are worth knowing before the rest: `hive state` (the live
projection), `hive append` (the only way anything gets in), `hive sync` (rebuild
what the next session reads), and `hive context --check` (what that costs, and
whether all of it actually arrives). Everything else in this file is a
specialisation of those four.

The runtime needs none of the three extras on that install line — `hive`, the
hooks and the dashboard run on the standard library — and each one buys one
thing. `vectors` (chromadb, sentence-transformers) is what `hive embed`,
`hive recall --semantic`, the embedding watcher and `hive pg embed` load; without
it they say so and the lexical paths keep working. `dev` (pytest) is what
`hive selftest`, `hive ci run` and the test suite need. `tui` (rich) draws the
live terminal board of `hive lintester --board`; without it the board degrades
to one log line per event. Plain `pip install -e .` still installs a working
`hive`; the other extras (`pg`, `mcp`, `ui`, `browser`, `build`) are named where
the feature that needs them is described below.

On Windows use PowerShell 5.1 syntax — no `&&`, no ternaries — see the
**PowerShell 5.1 note** below.

### Prerequisites

- **Operating system:** Linux or Windows. CI runs the suite on `ubuntu-latest`
  and `windows-latest` (`.github/workflows/ci.yml`); the reference run behind
  `IMPLEMENTATION_PLAN.md` was Windows 11 with CPython 3.14.3. macOS is not
  tested.
- **Python 3.11 or newer** — `requires-python = ">=3.11"` in `pyproject.toml`;
  CI covers 3.11, 3.12 and 3.14.
- **Claude Code CLI** (`claude` on `PATH`; 2.1.198 at the time of writing) for
  the commands that spawn agents — `hive lintester`, `hive mindnet orchestrate
  --execute`, `hive mindnet ab --execute` (the spawn sites are enumerated in
  `tests/test_advice_authorship.py`). Every other command, and the whole test
  suite, runs without it.
- **Hardware:** no GPU. The `vectors` extra is the one heavy dependency:
  loading it peaks at about 500 MB of working set (14 MB for the bare
  interpreter, 74 MB after `chromadb`, 455 MB with `all-MiniLM-L6-v2` loaded,
  500 MB after the first query — measured on Windows 11, 2026-09-29), and the
  event index for this repository's own 654-event ledger is 9.76 MB on disk
  (`hive bench recall-semantic`). The full test suite ran 11,138 tests in
  24 minutes on an idle laptop. The measurements, and the blueprint
  traceability behind them, are in `IMPLEMENTATION_PLAN.md`.

## Commands
- `hive append <type> <payload> [--json] [--stdin] [--tags a,b]` — add an event
- `hive project` — rebuild STATE.md + LEDGER/ + index.json + chain.json
- `hive state` — print the live STATE.md. Also warns (stderr, non-fatal) when this checkout's branch tracks nothing: `git pull` fails there and no drift count can notice it, which is invisible to `git status` and to `hive repo stale` alike
- `hive recall "<query>" [--semantic]` — grep (default) or vector search
- `hive rules summaries [--project P] [--json]` — structured lesson records (root cause, lesson, prevention rule, category, active rule) for every incident — the same data the dashboard **Improvements** view shows, for agents working headless. `--json` emits the raw list
- `hive rules reflect [--limit N] [--cooldown-hours H] [--all-projects]` — bounded reflection pass over active un-reflected incidents via the `claude` CLI (default: 3 per batch, 6h cooldown; `--cooldown-hours 0` disables). `--all-projects` drains the backlog across every registered namespace — a manual drain, never invoked from hooks. The same pass also **reroutes** red leaking rules (below)
- `hive rules resolve [--limit N] [--list]` — active resolution over the **red (unresolved) set**: incidents with no rule are reflected; a leaking **auto** rule is replaced with the best distinct alternative route (model-ranked, deterministic-fallback aware, capped at 3 attempts per signature — then flagged `reroute_exhausted` for a human). The `rule_rerouted` event re-anchors leak counting, so a route that holds turns green (resolved) and auto-promotes to SOUL.md. Human rules are never rerouted. `--list` prints the red set without spending tokens
- `hive flag "<text>" [--category tag]` — self-flag a judgment error (wrong call, not a tool failure); active as a prevention rule from the first occurrence
- `hive graph export [--out path] [--semantic] [--open]` — write a standalone, offline graph.html
- `hive ci run [--pr N] [--branch B] [--merge] [--root R] [--project P] [--json]` — the **native CI gate**: four staged gates in a git worktree of its own — the repo's own guards, the suite, the pg tier when `MINDHIVE_PG_DSN_ADMIN` names a server, then the hash chain — stopping at the first red. Exit 0 green, 1 red, 2 it could not start. `--merge` lands a green run: through `gh pr review --approve` + `gh pr merge --squash` when `gh` is logged in, and otherwise offline, which never checks the base out from the ephemeral worktree
- `hive ci status [--pr N] [--page N] [--project P] [--json]` — the last recorded CI run: which gate decided it, its counts, the commit and the merge state. Exit 0 it was green, 1 red, 2 there has never been one — three different facts, spelled differently
- `hive ci hook-install [--root R] [--force]` — write `pre-push` (POSIX `sh`, LF-only, executable) so every push runs `hive ci run --branch "$branch"` first. Written to the repository's COMMON hooks dir, so it works from a linked worktree too; refuses to clobber a hook someone else wrote unless `--force`
- `hive ci breaker status [--project P] [--json]` — what the **integration circuit** is doing, without moving it: state, consecutive failures, trips inside the window, cooldown left, and the record behind it. Exit 1 while OPEN and 2 while QUARANTINED, so a script can gate on it. It deliberately does **not** call the breaker's own `check`, which is a transition: an OPEN circuit whose cooldown has elapsed moves itself to HALF_OPEN as it answers
- `hive ci breaker reset [--project P] [--json]` — close the circuit by hand and clear its trip history. **The only exit from QUARANTINED**, which three trips inside the window reach and no cooldown leaves; total over every other state, so a reset of an already-closed circuit is a named no-op rather than an error
- `hive ci premerge <PR> [--merge] [--allow-base-drift] [--legs L] [--jobs N] [--timeout S] [--no-status] [--keep] [--json]` — the **pre-merge CI** GitHub Actions used to be, on this machine: every `ci.yml` leg (the 3 × 2 OS × Python matrix, the node, browser and pg tiers) run against the MERGE the PR would make, each in a fresh repository with its own venv, and the verdict posted on the PR as commit statuses (`mindhive-ci`, `mindhive-ci/<leg>`). Exit 0 green, 1 red, 2 it could not be checked, 3 green but `--merge` did not merge. `--merge` re-reads the PR, re-checks the base and merges with `gh pr merge --merge --match-head-commit <tested head>`; `--branch B [--base M]` tests a local branch instead, with no statuses and no merge
- `hive ci watch [--once] [--interval S] [--skip-drafts] [...]` — keep every open PR's verdict current: a new head is tested in full, a moved base is re-checked with one `git merge-tree` and a new conflict turns the PR red at once. Never merges
- `hive ci conflicts [--base M] [--json]` — which open PRs conflict with their base, and which pairs cannot both land as they stand. Exit 1 when any does
- `hive ci legs [--legs L] [--json]` — the `ci.yml` legs and whether this machine can run each, with the reason when it cannot
- `hive dashboard [--port N] [--no-browser] [--idle-cutoff S]` — open the web control panel for every feature. Without `--idle-cutoff` it is exactly the 24/7 server it has always been and runs until you stop it; with one, it also shuts down after that many seconds with no page open
- `hive ui [--port N] [--no-browser] [--idle-cutoff S] [--asgi]` — the same panel as a **page** rather than a service: it beats every 15 s from the open tab and shuts itself down 60 s after the last beat, so closing the tab ends the process. Defaults to port 54461, the dashboard's own — one panel, one URL — and if the 24/7 dashboard is already there it simply opens that one instead of binding over it. The heartbeat's timer runs in a Worker so a background tab is not throttled into a false shutdown, and there is no `document.hidden` guard: hidden is not closed. `--idle-cutoff` moves the beat interval with it (a quarter of the cutoff, never slower than 15 s), so `--idle-cutoff 5` is a five-second run rather than a usage error. `--asgi` serves through FastAPI/uvicorn instead of the stdlib server and needs `pip install -e .[ui]`; without the extras it exits 2 naming that line. `--idle-cutoff SECONDS` defaults to 60, or to `MINDHIVE_UI_IDLE_CUTOFF_S` when that holds a positive number of seconds (`5`, `0.5`, `1e2` all count). A value that is not a positive finite number — empty, text, `nan`/`inf`, zero, negative — is ignored: `hive ui` still starts, under 60, and prints one line on stderr (`hive ui: MINDHIVE_UI_IDLE_CUTOFF_S='abc' is not a positive number of seconds; using 60`). The flag beats the variable. `hive dashboard` does not read the variable; its own `--idle-cutoff` is unchanged
- `hive plugins list [--json]` — print the **whole stored catalog**, free and paid, from the latest snapshot; never rescans (that's `scan`/`autoscan`), and says what to run when no snapshot exists yet. Under `--json` stdout is always parseable — `{}` when there is no snapshot, with the hint on stderr
- `hive plugins scan [--cache dir] [--top N]` — catalog free (no API key / no pay) skills & plugins, score by breadth, store a `plugin_catalog` snapshot, print the top N
- `hive plugins autoscan [--max-age-hours H] [--top N] [--force]` — scan **only when the catalog is missing or stale** (default 24h); a near-instant no-op otherwise, so it's safe to run every session
- `hive plugins recommend` — print the top recommendations from the latest snapshot
- `hive plugins cache-check [--json] [--sync] [--hook]` — is the plugin Claude Code loads the one this checkout holds? Compares the version **and** a content hash of the plugin's components (manifest, skills, commands, agents, `hooks/hooks.json`, `.mcp.json`) with the copy in `~/.claude/plugins/cache`, names the files that differ, and exits 1 on drift — a version compare alone misses a copy taken before a change that kept the version. `--sync` repairs through Claude Code's own `claude plugin update <plugin>@<marketplace>` (restart Claude Code to load it), and reports success only when a second comparison finds the copy in sync; `--hook` is the session-start form: one line only on drift, never fails, and syncs only when `MINDHIVE_PLUGIN_AUTOSYNC=1` (which adds the sync's outcome line and can hold session start for up to 45 s while `claude plugin update` runs)
- `hive skills list [--json]` — list every installed plugin + skill from the live registry (updates the instant one is installed/removed)
- `hive skills invoke <name> [--plugin p]` — call any installed skill through MindHive via a headless `claude`; records the outcome and exits non-zero on failure so callers can gate on it
- `hive skills status` — green/red last-call outcome per skill from the ledger
- `hive skills stats [--json]` — per-skill completion-rate telemetry from the ledger (calls, ok/fail, last outcome, last used)
- `hive skills record <name> --ok|--fail [--note "…"]` — record an agent-assessed skill outcome directly (write-through for skills driven in-session rather than via `invoke`)
- `hive skills recommend [--json|--top N|--brief]` — deterministic most-needed-skills ranking from measured data; `--brief` prints ≤3 one-liners (the same block STATE.md embeds at every projection rebuild)
- `hive review queue [--session id]` — queue the session's touched source files for a thermo-nuclear review (run by `session_end`; writes the pending-review marker)
- `hive review show` — print the pending-review marker, if any
- `hive review clear` — clear the pending-review marker once the review has run
- `hive log <category> "<explanation>"` — record a **mandatory** code change-log entry for the current change-set (categories: `update`, `minor_change`, `deletion`, `reduction`, `major_change`, `replacement`; files auto-detected, `--files` to override). Entries are `code_change` events shown in the dashboard's **Log** section
- `hive log queue [--session id]` — queue the session's unlogged source files (run by `session_end`; writes the pending-changelog marker whose notice blocks STATE.md until the entry is recorded)
- `hive log show` — print change-log entries, newest first
- `hive log pending` — print the pending unlogged-changes marker, if any
- `hive roadmap set <id> --prompt "<ask>" --step "…" [--step "…"|--file steps.txt]` — declare a plan as an event-sourced **roadmap**: the original ask, the ordered steps, per-step progress. Each Oversee card gets a **roadmap** button rendering it as an interactive stepper (ask, progress bar, clickable step nodes)
- `hive roadmap step <id> <index> --status done|active|pending|skipped [--note "…"]` — update one step; marking the active step done auto-advances the pointer to the next pending step
- `hive roadmap interrupt <id> --cause usage_limit` — the **out-of-usage marker**: flags the plan interrupted so STATE.md (and the auto-loaded MEMORY.md) carries the creation context — the ask, steps done, and the next step — until it is resumed. `hive loop` records this automatically when a run ends on `api_cap` (Claude usage limit) or `budget_cap`
- `hive roadmap resume <id>` / `hive roadmap complete <id>` — pick an interrupted plan back up / mark it finished (append-only, like everything else)
- `hive roadmap link <id> [--pr N] [--branch <name>] [--commit <sha>]` — the **durable work link**: point a plan at the code it produced, without changing its status. `set`, `complete` and `archive` accept the same three flags; all three are optional and independent (the branch is known when work starts, the PR only once one is opened), and they merge into the record, so a re-plan never drops the PR number
- `hive roadmap merge-state <id>` — ask git and `gh` whether that work landed. Read-only, and **honest about not knowing**: `merged` only on proof (a merged PR, or a commit git confirms is an ancestor of the integration ref), otherwise `open`, `closed`, or `unknown` with the reason — `no_work_link`, `no_github_remote`, `no_gh_cli`, or `probe_failed`. It never reports `merged` because a probe failed to run
- `hive roadmap archive <id> --reason "<what settled it>"` — retire a plan whose last step nothing can verify ("merge the PR", "watch it for a week"). The honest third exit between *finished* and *still open*: the plan leaves the live views, the reason is recorded append-only, and the progress it actually reached stays on the record, so `archived` never reads as "done"
- `hive roadmap retire` — retire every live plan whose work link **proves** the work is finished: merged → `complete`, closed unmerged → `archive`. Reports every plan it examined, untouched ones included with their reasons, so "nothing was provably landed" cannot be mistaken for a successful sweep
- `hive roadmap list [--all]` / `hive roadmap show <id>` — list plans (retired ones — completed and archived — hidden unless `--all`) / print one plan with step statuses, its work link, and history
- **Session auto-sync** — interactive sessions declare and update their roadmap automatically: the PostToolUse hook mirrors each TodoWrite into a `roadmap set` for the session's id (statuses mapped completed→done, in_progress→active), and `hive roadmap autoclose --session <id>` (run by the SessionEnd hook) completes a finished plan or interrupts an unfinished one so its context rides STATE.md into the next session. Auto-synced plans carry the `auto` tag; human-declared plans are never touched
- `hive consolidate [--limit N] [--cooldown-hours H] [--force]` — one bounded memory-consolidation ("dreaming") pass: merge near-duplicate semantic memories into survivors and drop the absorbed originals from the briefing/MEMORY.md digest. Append-only throughout — the originals stay in the hash chain as the archive; `hive verify` stays green
- `hive export [--out path]` — write a portable handoff.md (zero API tokens)
- `hive verify [--json] [--strict]` — check the hash chain (`--json`: sorted one-line JSON for scripts; `--strict`: exit 1 when the chain is broken or a corrupt row exists — the default always exits 0 and reports on stdout)
- `hive embed [--project P] [--force] [--rebuild]` — build the on-demand vector index (`--rebuild`: drop and refill the `ledger` and `events` chroma collections, which is what picks up a change to the HNSW tuning an existing collection would otherwise keep ignoring; the watcher's own collection is never touched). `--project` names the namespace verbatim — it is what a failed semantic read tells you to run, where `MINDHIVE_PROJECT` would be normalised (`my_proj` → `my-proj`) into another namespace
- `hive watcher run [--once] [--json]` — drain the pending-embed queue: the rows `append_event` marked for indexing, embedded and the marker cleared. `--once` claims the queue and returns (the shape a scheduled task wants); without it the loop runs in the foreground until Ctrl+C. The same work `hive dashboard` does on a background thread while it is open, so a machine that keeps the dashboard open never needs this
- `hive watcher status [--json]` — how much is queued and how much is claimed. Always exit 0: a backlog is not an error, it is what a week with the dashboard closed looks like
- `hive watcher recover [--stale SECONDS] [--json]` — return abandoned claims to the queue (default: older than 600 s). The same pass the dashboard runs at boot, by hand for the machine that lost power mid-embed
- `hive selftest` — run the test suite
- `hive loop init` — scaffold `.hive-loop/` (PROMPT.md, fix_plan.md, AGENT.md, loop.toml) and check the `claude` CLI
- `hive loop` — run an autonomous Claude Code loop in the current project until the task is done; memory-seeded each iteration, with real token/cost budget gating, a context smart-zone reset at half the window, a dual-condition exit gate (the explicit `EXIT_SIGNAL: true` **plus** ≥2 natural-language completion signals in the reply — `[loop] min_completion_heuristics` in loop.toml, `0` restores explicit-only), and a circuit breaker (3 no-progress / 5 same-error iterations). When no `[model].model` is pinned, iterations **route to a model tier** (light/mid/heavy, `[model.tiers]` in loop.toml): small tasks start light, a failed or no-progress iteration escalates one tier, and the starting tier is learned from what completed comparable-size runs in the ledger. A real run also **auto-opens the web dashboard** so you can watch it live — opt out with `--no-dashboard` or `MINDHIVE_LOOP_DASHBOARD=0`
- `hive loop status` — summarize the last loop run
- `hive loop --dry-run` — show prompts + projected budget/context math without calling the CLI (no dashboard is opened)
- `hive fabricate compile <prd.md> | --from-learning [--id <fab-id>] [--dir <repo-root>]` — the **PRD assembly line**, step one: parse an Executable PRD (a `TITLE:`/`SUMMARY:` header and `Goal` / `SOP` / `Acceptance` sections, in markdown-heading or `(4) SOP —` style; a PRD with no SOP steps is refused) into a loop work order — a goal dir `.hive-fab/<fab-id>/` (PROMPT.md carrying the goal and the acceptance criteria as the exit gate, fix_plan.md carrying the SOP as the checklist, plus the scaffolded AGENT.md and loop.toml) and a `fabricate`-tagged roadmap keyed by the fab id (default `fab-<title-slug>`). `--from-learning` takes the latest Self-Reliant PRD from the ledger's `learning` events instead of a file; an id whose roadmap exists and is not fabricate-owned is refused
- `hive fabricate dispatch <fab-id> [--at <UTC ts> | --in-hours <h>]` — arm the OS timer that starts the run unattended (default: a minute from now): the same per-user Windows scheduled task `hive autoresume` uses, named `MindHive Fabricate <fab-id>` and aimed at `hive fabricate run <fab-id>` from the repo root. The `dispatched` event is the source of truth — where no timer can be registered it reports `timer: none` and names the command to run yourself
- `hive fabricate run <fab-id> [--force]` — execute one loop launch on the goal dir now (what the timer runs): `hive loop` with the fabrication's id as the roadmap key, so a usage cap interrupts THE fabrication roadmap and re-arms a loop-mode auto-resume (loop → sleep → loop until the exit gate passes, or until six wake-ups in a row check off nothing new on the checklist), and a completed launch completes the roadmap and drops the timer. The auto-resume wake-up of a capped fabrication is delivered through this same run, not the bare loop, so every wake-up records its `run` event and the one that passes the exit gate closes the fabrication and drops its timer. Exit 0 on `completed`, `api_cap` or `budget_cap`, 1 on anything else (`spawn_failed`, `guard_refused`, a tripped breaker); `--force` bypasses the LIVE_LOCK guard
- `hive fabricate list [--json]` — every fabrication with its status (`compiled`, `dispatched`, `completed`, `stopped`), its last loop run and exit reason
- **`/mindhive-orchestrator <idea>`** — the **single centralized pipeline**: turns an idea into finished, quality-checked work through a **ten-stage sequential, dynamic** workflow — brainstorm → plan → observation → understand-and-converge → PRD → recommendation matrix → kanban → execution → quality assessment → learning. Its **main execution engine is restricted to the `superpowers` and `compound-engineering` plugins** (either may run, or both combined); every other installed skill is a **complement** that only raises quality. It **recalls before it asks** (from the ledger, or by reading the code), and advances **dynamically** — auto-advancing when intent is clear from recall + codebase, pausing to converge with you only when a scope/architecture/risk fork is genuinely unresolved. It scores execution skills with a **telemetry-anchored Recommendation Matrix** (measured completion rates from `skills stats`, learning-gated `auto`/`suggest` execution modes, green/red `skills status`), chains proactive quality passes, offers a context **Handoff** at ~50%, and appends learnings back to the ledger. At execution it fans work out to **parallel sub-agents** over a **file-complexity cluster database** (`hive clusters`). This is the **only** trigger for the pipeline. Shipped in the plugin (`commands/mindhive-orchestrator.md` + `skills/mindhive-orchestrator/`, with `references/` for the pipeline stages, parallel orchestration, recommendation matrix, cross-platform guide, and handoff protocol).
- **`/circle`** — the loop **primitive** (not the pipeline): type `/circle` and Claude preflights the `claude` CLI, scaffolds/confirms `.hive-loop/`, dry-runs, then drives the autonomous `hive loop` and reports the result. Headless agent runs with `permission_mode = "acceptEdits"` by default. The `/mindhive-orchestrator` pipeline may drive this loop as one of its execution engines. Shipped as a **Claude Code plugin** (`commands/circle.md` slash command + `skills/circle/` skill, declared in `.claude-plugin/`) so it works in any project once installed — see below.
- **`hive clusters build` / `hive clusters plan --lanes N`** — the parallel-orchestration layer the pipeline stands on at execution: clusters the repo by module, classifies each file's complexity, and packs whole clusters into `N` **file-ownership-safe lanes** (no file in two lanes) so parallel sub-agents never collide. Deterministic and dependency-free; the cluster DB is an overwrite-in-place `cluster_db.json` sidecar.
- **`hive lintester "<intent>"`** — one instruction in, one mission report out: the intent is named and decomposed, every unit is weighted by a real **AST cyclomatic count plus `git log --numstat` line volatility plus recorded agent turns**, `hive clusters`' bin-packer sorts them into balanced lanes, each lane takes its own `.worktrees/lane-N` checkout, and an **asyncio work-stealing scheduler** keeps an idle lane busy by taking queued work off the deepest one — never work a child has already been handed, which is the invariant that makes the stealing safe. Each sub-agent receives **only its own** brief, skill directive and AST scope. A failed unit is rolled back one phase and retried with its own **defused** stderr quoted; a target that keeps failing trips a per-target circuit breaker, which then runs `hive rules` reflection unattended. **A dry run is the default** — `--execute` is what starts agents. Add `--lanes N`, `--json`, or `--checkpoint-dir DIR` to make an interrupted mission resumable. `--max-lane-tokens N` and `--max-lane-cost USD` put a **ceiling on what ONE lane may spend** (MEB K21): the lane that passes it takes no further unit while every other lane keeps running, and the refusal is recorded where `hive rules` can see it. Both need a runner that reports usage — when none did, the report says `usage: unavailable` rather than printing a zero it never measured. The per-unit gate is **fail-closed** (blueprint P2): a unit whose changed module no test mentions is refused, and when that module is new the refusal names the exact file to write (`tests/test_<stem>.py`) so the child has an instruction it can follow rather than a complaint. `--allow-untested` passes a change no test could ever name — a README, a migration — and the report then counts those units so a green mission cannot hide them (`warning: N untested override(s)`). `--weight-model {log_linear,linear}` picks the fusion every unit is weighed by (blueprint P4, K3): `log_linear` is the calibrated default, `linear` the three-term sum. Default: none — an exported `MINDHIVE_WEIGHT_MODEL` still decides, and without either the model is `log_linear`. Given, the mission runs with `MINDHIVE_WEIGHT_MODEL` set to the flag and the previous value (or its absence) restored afterwards, even when the mission fails; the lanes a mission starts inherit it. The report names the model that really weighed: JSON `weight_model` and `weight_model_reason`, plain `  weight model: <name>` — an unknown exported name shows as `weight model: log_linear (unknown weight model 'cubic' from MINDHIVE_WEIGHT_MODEL: used log_linear)`. A unit may not edit the tests it is judged by — every file under `tests/`, any `conftest.py` pytest would load, the interpreter's `sitecustomize`/`usercustomize` hooks and plugin metadata on the import path, the root pytest config (`pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`), and the pytest section of `pyproject.toml`/`setup.cfg`/`tox.ini`, read the way pytest reads it — measured from the commit its lane stood at before its first attempt, so a child that commits its edit does not move the baseline, and inherited by the lane's next unit unless nothing of the judge changed in what the lane committed since. A new test module is always fine; a new `conftest.py`, a new module named like one the run already imports (`src/json.py`, `tests/pkg.py`), or any edit to the judge, is refused unless `--allow-test-edits` (or `--no-immutable-tests`) says otherwise, and a lane git cannot read is refused rather than passed.
- **`/engineering-loop`** — a **subordinate execution engine** (not a competing pipeline): fuses `/circle`'s bounded headless loop with the compound-engineering `lfg` pipeline into a memory-driven **plan → work → review → fix → verify → learn** loop. MindHive memory sits between every stage (recall before, append after), it drives the chosen skills through the invoke channel with green/red status, and any skill failure feeds the improvement loop as a `skill:<name>` prevention rule. The `/mindhive-orchestrator` pipeline may drive it at its execution stage. Shipped in the same plugin (`skills/engineering-loop/`).

### The always-on surface: what auto-loads, and whether it arrives

Four of these exist because the auto-loaded surface can degrade **silently** —
the block still arrives, so nothing looks broken, and part of what it promised is
missing. `tests/test_docs_parity.py` enumerates the shipped commands off the live
parser and fails when one of them is absent from this file, so this list cannot
quietly fall behind the CLI again.

- `hive sync [--folder P] [--no-briefing]` — write the native `MEMORY.md` digest the desktop app auto-loads. It **warns when the file it just wrote will be cut short by the loader**, naming the sections that will not reach a session and the unit that bit — the digest's caps bound item counts, and no item count bounds lines or bytes, so the tail (lessons, decisions) is what silently stops arriving
- `hive context [--check] [--top N] [--origin hive|claude|hook] [--json]` — measure everything that auto-loads into every session, ranked by estimated token cost, with trim suggestions. `--check` is the gate: exit 1 when over the token budget **or** when `MEMORY.md` is past either loader cut with no rollover notice ahead of the cut (that one is loss, not cost, and is reported even with no token budget configured). Two softer cases print `WARNING:` and keep the exit code: a truncation the adaptive tail MANAGED (its rollover notice arrives before the cut, so the session is told what moved to the semantic index), and a digest past the composer's safe budget of 24,500 bytes / 195 lines that the loader still reads whole
- `hive context --set-budget N [--set-memory-lines N] [--set-memory-bytes N]` — persist this namespace's token budget and, optionally, the two `MEMORY.md` cuts (defaults: the native loader's own limits). One sidecar holds all three; setting one never discards the others. **The byte budget is the one that usually bites**, and it is measured on LF-normalised content, not the size on disk — `sync` writes in text mode, so on Windows the file on disk is one byte per line larger than what the loader reads
- `hive context --set-adaptive-tail lessons=N,decisions=N,enabled=1|0` — opt in to the adaptive `MEMORY.md` tail (stored under `adaptive_tail` in the same `context_budget.json`; `hive sync` applies it). Off (the default), `sync` keeps its fixed caps of 10 lessons and 5 decisions and now closes the digest with a rollover line counting what the caps left out. On either path that line is withheld when the digest fits the loader without it and not with it: the notice is never the thing that gets a digest cut. On, the tail is sized to the composer's safe budget: SOUL rules, pinned lessons, the mandatory SOP tier and the active work session are never cut; the rest fills newest-first until the first item that does not fit (no notice is charged when every item fits without one; under a `memory_lines` / `memory_bytes` cut set below the safe budget the tail keeps the same 500-byte / 5-line margin under that cut), near-duplicates (Jaccard >= 0.88, the Dreaming Pass) are folded away, and everything else is counted in the rollover line `... N keputusan dan pelajaran dialihkan ke basis data vektor sekunder (ChromaDB).` with a pointer to `hive recall --semantic` (plus the install hint when the vector extras are absent, and `hive pg tool search_events_by_type` on a Postgres backend)
- `hive symbols [PATH] [--budget N] [--json]` — a **zero-token** symbol index of a source tree: imports, classes, methods and module functions, extracted locally with stdlib `ast` and no LLM call at all. 137 modules of this repo render as ~993 tokens, ranked by a hub+authority PageRank over the import graph so the entry point and the widely-imported core both survive the budget — and the map states how many modules it left out, because a map of 15 files and a map of 137 cut to 15 are otherwise the same bytes. Set `MINDHIVE_SYMBOLS=1` to also inject it into every prompt (it is off by default: the map is cheap to build, ~9 ms warm, but it is not free to *send*)
- `hive inject show [--turn last|N] [--project P] [--json]` — what the injection pipeline actually delivered on a turn: every registered pass with its verdict, size, milliseconds and reason. The rows come from the **registry**, not from the record, so a pass that never ran is reported as a pass that never ran rather than omitted — six rows on a seven-pass pipeline is the exact blindness this feature exists to end. `--turn N` indexes the recent ring (0 = newest); ring entries are the *index* and carry no injected text, and the report says so rather than showing an empty block that reads like a silent turn. `--json` stays parseable on stdout even when nothing has ever been recorded
- `hive inject table [--markers]` — the composition order as markdown, generated from the registry. It is what this file and `docs/session-legend.md` carry between generated markers, and `tests/test_cli_inject.py` regenerates it and compares — which is what stops the documented pipeline naming five passes while seven of them run
- `hive sop add|update|delete|list|show <id>` — the event-sourced SOP registry. A **mandatory** SOP is re-asserted on every human prompt, verbatim
- **Who may write an SOP** (`advice.py`, blueprint P1d): `hive sop add|update|delete` and `hive append sop` ask one gate before anything is read or written. With `MINDHIVE_ORCHESTRATION_SECRET` set on the host, only a process whose `MINDHIVE_PROCESS_TOKEN` equals it may write — `--session` is not consulted in a process that sees the secret, and the token is withheld from every agent, and from agent-written code, this package spawns (`claude_cli.py`, `mindnet/executor.py`, the Workspace board in `workspace.py`, `loop/driver.py`, the sandbox in `orchestrator/resilience.py`, the suite runner in `verify.py`, the trainer in `research.py`; what keeps the token is the operator's own: `rotation/supervisor.py`, whose child is the operator's successor session, and the two `claude auth` calls in `workspace.py`, the CLI's login and status flow, which start no session), so a spawned agent is refused by default. The dashboard's `POST /api/append` refuses type `sop` outright while a secret is configured: it cannot verify its caller, and an unstamped row would read as the operator's. With no secret configured the session rule decides: an orchestration session id (`mn9-`, `lm-`, …) is refused. A refusal exits 2 with `penulisan SOP (<action>) ditolak: …` on stderr and appends nothing. A token-validated write is tagged `sop-validated`, and a procedure stays SYSTEM once ANY applied write to it was (the fold accumulates it, like the agent flag, so an unstamped title-only edit cannot relabel a system-composed body as the operator's); the per-prompt fence labels every delivered body `[MANDATORY SOP - HUMAN VERIFIED]` or `[SYSTEM SOP - SYSTEM VALIDATED]` and admits the agent-written rows it withheld with one `[ADVISORY TIP - UNVERIFIED AGENT]` count line (`hive sop list` marks those rows `[agent-written]` and counts them apart from the mandatory ones, and `hive sop show <id>` prints an `authorship:` line above the stored body). A label-shaped bracket inside a title or a body is turned into parentheses on every surface that composes one — the fence, STATE.md, MEMORY.md, `hive sop preflight|list|show` titles, the briefing and a sub-agent's brief: the three keywords as whole words, in ASCII, fullwidth or CJK brackets, on one line with at most 60 characters between the keyword and the closing bracket, and in a title also an opener that is never closed; not homoglyphs, zero-width characters, a label padded past those 60 characters, or one split across lines in a body. The labels attribute what the record says; they are not a proof against a process that controls its own environment: one that reads the inherited secret and exports the token is labelled SYSTEM VALIDATED, one that blanks `MINDHIVE_ORCHESTRATION_SECRET` for its own command is back on the session rule, where `--session` decides and the row reads as the operator's, and the ledger is files, so a same-user process can append around the CLI altogether
- `hive sop injection [--json]` — what that per-prompt injection **actually delivers right now**: every mandatory body verbatim, or degraded to `hive sop show <id>` pointers because it outgrew the per-prompt budget. Read-only, and not a gate
- `hive sop preflight [--json]` — pre-execution report: is this session new or a continuation (with the ledger evidence), which SOPs are mandatory, what the improvements record already warns about
- `hive briefing` — build and print `BRIEFING.md` (the full context; `MEMORY.md` is the compact digest of it)
- `hive directives` — print the session operating directives
- `hive repo stale [--max-age-hours H] [--json]` — how far this checkout has drifted from its integration ref. **Never fetches**: it compares against the ref as this checkout last saw it and reports the age of that snapshot alongside the count, because "0 behind" and "0 behind, last fetched in July" are different facts. It also reports whether the branch **tracks anything at all**, because the counts are measured against the ref a merge lands on — which resolves on any clone — while `git pull` needs a ref this branch tracks, and a checkout answering "0 behind" with a broken pull has no way to notice drift in the first place. `unknown` carries its reason (`no_checkout`, `no_integration_ref`, `probe_failed`)
- `hive premise check [--file F]` — check a prompt or handoff document's factual claims against the record: asserted file paths, roadmap ids, and test counts that contradict what is on disk, plus the claims it could not check
- `hive bench dashboard --url <origin> --label "<condition>" [--token T] [--project P] [--reps N] [--timeout S] [--endpoint PATH ...] [--json]` — time the dashboard's own API endpoints. Read-only: it starts nothing, restarts nothing, and writes no events — not even the token, which it reads but will never create. **Cold/miss and warm/hit are separate rows and nothing is discarded**, because the perf work being measured *is* a fingerprint-keyed memo layer: the first request per endpoint pays the fold and the rest are hits, so averaging them yields a figure that moves with the hit/miss ratio. `--label` is required — the same code answers 6.7x apart between two load conditions, so a number without its condition is not evidence. A non-200 is reported with its status and excluded from every mean (a missing token would otherwise time four fast 403s and read as a healthy server). Also reports serial-total against concurrent-wall for the same set, which is how GIL-serialized folds show up
- `hive bench cli-startup [--reps N] [--timeout S] [--json]` — median wall-clock of `python -c "import mindhive.__main__"` and `python -m mindhive --help`, each run as a child process of the interpreter that runs `hive`, N children per scenario (default 10, at least 1), each killed after S seconds (default 60) and reported as a timeout. Reports median, min and max ms per scenario, ok/total, the interpreter and Python version, `PYTHONPATH` and `PYTHONDONTWRITEBYTECODE` (children get `1` unless you exported your own value). The MEB's 45 ms claim is printed beside the measured median for comparison; it is **not a gate**. Exit 0 when every child ran, however slow; 1 when any child failed or timed out (a failure is never timed as a success); 2 for `--reps` below 1. Read-only
- `hive bench recall-semantic [--query Q ...] [--reps N] [--limit K] [--project P] [--json]` — median query time of the lexical ledger scan behind `hive recall` against the chroma path behind `hive recall --semantic`, in-process, on the same queries. `--query` is repeatable and **replaces** the default set of one query per memo kind (`decision`, `caveat`, `next_step`); `--reps` (default 5, at least 1) is rounds over the query set per path; `--limit` (default 10) is hits per query, as `hive recall --limit`; `--project` defaults to the detected project. Per path it reports the cold first sample, then the warm median, min and max over the rest, and hits per query; the semantic open (dependency import, index copy, model load) is timed on its own row, never inside the query times. The ratio is lexical warm median over semantic warm median, printed beside spec §4.7's 14.7x as a claim, not a gate. Without the `vectors` extra, on a namespace never embedded, under `LIVE_LOCK`, or on an index that cannot be read (the reason names the failing stage and the repair), the semantic row reads `unavailable: <reason>` and the exit code stays 0; exit 1 when a search raised; 2 for `--reps` or `--limit` below 1. **Never writes**: it reads through the path `hive recall --semantic` reads through, which answers from a private copy of the index because chromadb writes to any index it opens; one copy serves the whole run, and its size, copy time and removal are reported

#### What composes a prompt's injected context

Seven passes, in this order. Generated from `injectors.PIPELINE_ORDER` — regenerate with `hive inject table --markers`, and see the turn you are actually getting with `hive inject show`.

<!-- BEGIN GENERATED injection-pipeline -- hive inject table -->
| # | pass | entry point | runs | writes | what it delivers |
|---|------|-------------|------|--------|------------------|
| 1 | `promptgate` | `promptgate.hook_context` | only on an underspecified prompt | creates | skill-trigger directives when the typed prompt is thin; a clear prompt yields no context at all, which is why every later pass tolerates a falsy one |
| 2 | `enrich` | `enrich.extend_hook_context` | only if promptgate fired | appends | clarifying questions attached to a gate that already fired; it extends a context and never creates one |
| 3 | `mindnet` | `mindnet.inject.extend_hook_context` | every turn | appends | the phase spine's per-task direction; the sidecar is read ONCE here and threaded on to the callers that need it afterwards |
| 4 | `premise` | `premise.extend_hook_context` | every turn | appends | the checkable claims the prompt makes -- paths, roadmap ids, test counts -- checked against the record |
| 5 | `sopgate` | `sopgate.extend_hook_context` | every turn | prepends | the mandatory SOPs verbatim; over budget a body degrades to a POINTER, never a truncation, because half a procedure reads like a whole one |
| 6 | `symbols` | `symbols.extend_hook_context` | only when MINDHIVE_SYMBOLS is set | appends | the zero-token structural map; second-to-last so a map appended after the style block cannot sit ahead of it and take the truncation |
| 7 | `style` | `style.extend_hook_context` | every turn | prepends | the standing reply style; LAST, and that placement is the contract -- it prepends, so running last is what makes it survive truncation of everything before it |
<!-- END GENERATED injection-pipeline -->

The **order is the contract**. `style` prepends and runs last, and those two facts together are the only reason its block survives truncation of everything before it; `symbols` sits second-to-last for the same reason in reverse. A new pass goes before `style`, and anything that writes to the ledger runs strictly after the hook has printed.

### The Postgres read mirror (`hive pg`) — optional, and off by default

A **one-way copy** of the event ledger in PostgreSQL, so an AI can query it instead of grepping projections or reading 20 MB of JSONL. The **file ledger stays canonical and stays the sole writer**: `MINDHIVE_EVENT_BACKEND` remains `file`, and flipping it to `pg`/`dual` also disables the dashboard's overview memo (a measured 726 ms → 6.6 ms on `/api/overview`), so that is a separate decision with a known price. `psycopg` and the MCP SDK are optional extras (`pip install -e .[pg]`, `.[mcp]`) — with neither installed every command below still answers, structurally, saying what is missing.

**Nothing on the append path touches Postgres, under any backend value.** There is no per-event write seam and there deliberately isn't one: the module the AI path connects with holds `mindhive_reader`, which is `default_transaction_read_only` **at the role level**, so a single-row insert from there could never have succeeded. The mirror follows the ledger by being *refreshed* instead — per segment, from the admin credential, on the `autosync` schedule (or by hand). One loader, one credential, and no second code path to drift.

**The invariants this mirror must not lose** — why `payload` is one `JSONB` column and not two, why `ts` is `TEXT` and never `TIMESTAMPTZ`, why `global_seq` is identity and never the sort key, and what `numeric` does to a number's spelling — are in `docs/DATA_MODEL.md` §4.18, under **The table, and the columns that are not obvious**. Read that before "simplifying" a column: every entry there names the failure it prevents, and four of them have already been proposed as simplifications by readers who could not find this paragraph.

- `hive pg status [--project P] [--json]` — is the mirror reachable, is it fresh, how many read failures has this process swallowed, and which migrations exist on disk. Exits non-zero when the mirror is unavailable, so a script can gate on it. This is where a degraded mirror becomes **visible on demand** — the read seam is deliberately quiet on the hot path, because it sits under every fold
- `hive pg migrate [--dsn D]` — apply the pending SQL migrations. Needs `MINDHIVE_PG_DSN_ADMIN`, the **owner** credential, which nothing on the AI read path can even name. Refuses loudly when an already-applied migration's bytes have changed; a schema change is a new file, never an edit to an applied one
- `hive pg rebuild [--project P]` — reload **every** segment of one namespace from the file ledger (`DELETE WHERE project` then a bulk load, in one transaction, so a concurrent reader sees the whole old snapshot or the whole new one). Never `TRUNCATE`, which cannot be scoped to a namespace. Admin DSN
- `hive pg refresh [--project P]` — reload **only the segments whose bytes moved** since the recorded fingerprint, then re-run parity. This is the command a stale mirror's own notice names, and the one `hive autosync` runs on a schedule, so an operator typing it by hand does exactly what the machine does. Per-segment reload is safe because row order is not a stored property of the mirror: `ORDER BY COALESCE(ts,''), segment, seg_line` re-establishes it at read time, and `global_seq` is row identity only. Admin DSN
- `hive pg embed [--project P] [--model M] [--batch N] [--rebuild] [--force]` — encode this namespace's **semantic** events (decisions, caveats, experiment results, code changes, next steps, open questions, learnings, SOPs, roadmap rows) into `mindhive.event_embeddings`, which is what `search_events_by_meaning` ranks against. **Incremental and re-runnable**: the row key is the ledger's own `(segment, seg_line, model)` and the stored content hash is compared too, so an already-indexed event is skipped and a *rewritten* one is re-encoded rather than left with a stale vector. Measured on this ledger: first run 664 vectors in 47.9 s, second run 0.98 s with no encoder loaded at all — the probe happens on a short connection that closes before the model would be needed. Vectors are float32 little-endian in `BYTEA`, L2-normalised so cosine similarity is a plain dot product; there is no pgvector on a stock server and `BYTEA` measured the same on disk as `real[]`, 5.5x faster to load and 10x faster to rank via NumPy. Admin DSN, plus `pip install -e .[vectors]`
- `hive pg parity [--project P] [--json]` — compare the file ledger's hash-chain head against the mirror's. **Exits non-zero when it cannot prove parity**, including when it could not look at all: an unchecked gate reported as green is worse than a red one
- `hive db migrate [--dsn D] [--project P] [--dry-run] [--json]` — replay the file ledger's **events** into `mindhive.events`, one row at a time, with the hash chain verified as it goes. This is the DATA half of what `hive pg migrate` does for SCHEMA, and the two are separate verbs so that "migrate failed" is never ambiguous. **Idempotent**: a session the mirror already holds is skipped by arithmetic (`max(seg_line)`) rather than by a per-row probe, so a second run costs one `max(seg_line)` SELECT per session and issues no INSERT. The namespace's `chain.json` is checked first and a checkpoint that disagrees with the disk stops the run before a row is offered; a namespace that has never been checkpointed is replayed and the report says so. `--dry-run` verifies and forecasts without writing, and works with no admin DSN at all. Exit 0 verified, 1 the run happened and the chain did not verify, 2 the environment stopped it (no DSN, no driver, destination unreachable) — only the first of those is worth paging for
- `hive db status [--project P] [--json]` — what is on disk, what the destination already holds, and whether a replay could run. It reports the **same checkpoint verdict `hive db migrate` gates on**, from the same `chain.verify`, so "status says ok" and "migrate will start" cannot come apart — which costs the one scan `hive verify` pays. `chain_ok` is **tri-state**: `null` means the namespace has never been checkpointed, which is not the same fact as a chain that verified. Everything else is cheap: segment names off the directory, the migration inventory (no server needed), and — only when a DSN is configured — how many segments the destination holds, with an unreachable one reported as a field rather than raised
- `hive pg tool <name> [--arg k=v ...] [--list] [--json]` — run one of the eight AI tools — four that show events (`query_database`, `get_recent_events`, `search_events_by_payload`, `get_session_summary`), three that answer "what IS the state" (`get_latest_event`, `search_events_by_type`, `get_current_state`), and one that searches by **meaning** rather than by string (`search_events_by_meaning`, nearest-neighbour over the vectors `hive pg embed` wrote) — and print the exact envelope a model would receive. `--arg` values are parsed as JSON when they parse and kept as strings when they do not, so `limit=50` is an integer and `session_id=manual` is a string. This is the same dispatch table the MCP server reads, which is the point: the human debug surface cannot drift from the AI one
- `hive pg mcp` — serve those eight tools to an MCP host over **stdio** (no port, no token, reachable only by the process that spawned it). `python -m mindhive.mcp_server` is the same `main()` and is the recommended spelling on Windows, where a console-script shim can go stale against an editable install

### Participatory consensus (`hive consensus`)

Many framed inputs, one number, and a route from the number back to every input
that produced it. Contributions are ordinary ledger events, so the consensus a
frame reaches is **derived** — re-runnable from the same events and covered by
the same hash chain. Nothing here writes a table or a sidecar.

- `hive consensus contribute --frame F --kind K --subject S --value V [--confidence C] [--session S] [--project P]` — append one framed contribution. `--kind` is the instrument (`biometric.resting_hr`, `strategic.direction`) and it is the **only** thing that separates one population's inputs from another's — one event type serves a youth anxiety frame and a board strategy frame alike. `--value` is parsed as a number when it parses and kept as a string when it does not. A contribution **may not state its own weight**; the fold assigns it and records why. Prints the content-addressed `ref` a later `trace` resolves
- `hive consensus fold --frame F [--start T] [--end T] [--json] [--project P]` — fold the frame's window into a `consensus_record`. Window bounds are ISO timestamps, **inclusive at both ends**, defaulting to the earliest and latest input. Two rules, and neither is an average of the population: every **subject** carries total weight 1 however many times they contributed (so fifty samples from one participant do not outvote fifty participants), times any confidence they stated; the answer is then the **weighted median** for a numeric frame and the heaviest distinct value for a categorical one. The frame decides which from its own data, so an operator never has to know. An empty window **exits 0** — "nobody answered this week" is a success, and a non-zero there would train whoever scheduled it to ignore the code
- `hive consensus trace --frame F [--json] [--project P]` — walk the frame's newest record back to the events it folded, showing the weight and the stated reason for each. **Exits non-zero when any input resolves to no event in the ledger**, the same gate semantics as `hive pg parity`: non-zero when the claim cannot be *proven*, not only when it is known false. A frame that was never folded exits 0 — nothing to audit yet is not a failed audit

The `consensus_record` contract is enforced at the append surface, not by
convention: a record whose inputs do not each carry a weight, a written reason,
and an `occurrence` is **refused**. An untraceable conclusion in an append-only
store would be permanent and would keep looking authoritative, which is the
failure this family exists to make impossible.

`occurrence` is there because a `ref` is a *content* address: two identical
contributions — same subject, same value, same second — hash to the same ref, so
the ref alone names "one of these". It is the 1-based rank of the row among the
events under its ref, which is what makes three identical inputs resolve to three
events, and makes a record claiming three of them **unprovable** against a ledger
holding one.

### Namespaces, hygiene & automation

- `hive projects` — list namespaces (registered vs unregistered)
- `hive gc [--prune <name>|--prune --orphans|--prune --graph-blobs] [--force]` — store hygiene: report per-namespace sizes, prune junk. Opt-in and guarded — it refuses a bulk prune when the registry is unreadable, rather than treating every namespace as an orphan
- `hive autosync [--max-age-hours H]` — automated `sync` for registered projects (run from hooks; a near-instant no-op when nothing is stale)
- `hive autoresume` — after a usage cooldown, deliver an interrupted plan's marker (the ask, the progress, the next step) into the new session — mode `prompt`, a one-shot headless session — or, for a plan a `hive loop` / `hive fabricate run` launch interrupted, mode `loop`: relaunch the loop engine on the recorded goal dir, breaker, budgets and exit gate intact
- `hive replenish [explain]` — propose the work the record proves is outstanding (run by `session_end`); `explain` shows the per-lens scoring behind each proposal
- `hive failures list` — recorded tool failures and incidents, the raw material `hive rules` mints prevention rules from
- `hive triggers` — prompt-quality trigger rules (the UserPromptSubmit gate)
- `hive enrich` — prompt enrichment for Oversee: the default-on toggle and a deterministic preview of the enriched prompt
- `hive worksession record --phase <phase> --action <key or text> [--initiative --reason … --ref …]` — record one work-session step with its four tags (`[WS: …]`, `[USER_SPEC: …]` or `[INITIATIVE: …]`, `[USAGE: …]`); an `--initiative` step is its own declaration (one row, listed with the initiatives), and `--reason`/`--ref` without `--initiative` is refused. `status` moves a step, `initiative` declares an autonomous initiative that is not one step, `list [--initiatives]` prints the folded sessions, `list --session <id>` prints that session's steps and the ids `status` takes (with `--initiatives`, only that session's initiatives), `list [--page N] [--page-size M]` pages any of those three listings (see below), and `migrate [--dry-run]` copies roadmap steps into plan-phase steps without touching the roadmap, idempotent across sessions: a copy whose roadmap text changed since is reported stale, and a copy whose roadmap status moved (drifted) or whose step left the plan (orphaned) is settled by an appended status move — unless its status was moved by hand, which is reported and left alone. Concurrent writers to one namespace take a lock across the step-id check and the append, so no step is lost to a colliding id. Oversee cards show the active step's phase pill and tags, an initiative badge and the context-usage bar (drawn only when a MEMORY.md digest was measured)
- `hive worksession list [--initiatives] [--session S] --page N --page-size M` — the paged form of the three listings (sessions, `--initiatives`, `--session S`). Both flags are whole numbers; without either the whole listing prints, as before. `--page` counts from 1 (0 or a negative reads as 1; a page past the end prints no rows and names the last page). `--page-size` defaults to 20 and is capped at 100 (0 or negative → 1, above 100 → 100: clamped, never refused); alone, it prints page 1. The footer then reads `page X of Y, K of N work session(s)` (or `initiative(s)` / `step(s)`), followed by `next page: hive worksession list [--initiatives] [--session S] [--project P] --page X+1 --page-size M` when there is one, or `past the end: the last page is … --page Y --page-size M`. With `--json` the document is the pagination envelope `{page, page_size, total_items, total_pages, has_next, has_prev, items}`; for `--session S --json` the envelope sits under `steps`. An empty listing keeps its hint
- `hive packs check [--manifest P]` — stack content-pack quality gates (read-only)
- `hive mindnet` — the MindNetwork task graph and phase spine: declare tasks, walk the six-phase pipeline, record the evidence each phase requires
- `hive session run` — supervise an interactive Claude Code session and rotate it when context fills; see **Smart-zone session rotation** below
- `hive session install-hook <settings.json> [--command C] [--dry-run]` — register the Stop hook that measures occupancy. Idempotent, refuses an unparseable settings file rather than repairing it, and backs up before writing

### Smart-zone session rotation (interactive sessions)

`hive session run` wraps an interactive session: when the Stop hook measures
context past the smart-zone boundary it writes a `ROTATION_PENDING` artifact, and
the supervisor starts a successor seeded from the ledger once the child exits. A
Stop hook cannot end a session, which is why the signal is a file.

Two properties are deliberate, not incidental. The hook is **inert until
switched on** (`MINDHIVE_ROTATION`, which only `hive session run` sets, for its
own children), so registering it changes nothing on its own. And its advice tells
the truth about what happens next: it promises a queued successor only when a
supervisor is actually waiting, and otherwise says plainly that nothing is queued
and names the command that would queue one.

**Rotation runs on both families.** `hooks/` ships nine PowerShell shims and
nine POSIX siblings, and `hive session install-hook` registers the one matching
the platform — `powershell -NoProfile -ExecutionPolicy Bypass -File
"hooks/stop.ps1"` on Windows, `sh "hooks/stop.sh"` elsewhere. The path is
quoted because an unquoted one breaks on any checkout under a directory with a
space in its name, and for the Stop stage that break is printed at the end of
every agent turn. `-ExecutionPolicy Bypass` is there because a Windows client's
default policy is `Restricted`, under which `powershell -File` refuses every
script with a SecurityError and exits 1 — on every hook fire, before the shim's
fail-open code runs; the flag applies to that one process and changes no
machine setting. A Stop entry registered before the flag existed still names
the shim, so `install-hook` reports it as already registered: on a machine
where the policy was never set, re-spell that entry by hand to the form above.
It emits a `platform_warning` only when the shim your platform needs is
actually missing from the checkout, rather than whenever you are not on
Windows.

## Environment variables

Every environment variable MIND HIVE reads, and the four third-party ones it
reads or sets. This table is the registry: `tests/test_env_registry.py` fails
when the code reads a `MINDHIVE_*` / `MINDNETWORK_*` name this table does not
list, and when the table lists a name nothing reads any more. Two names are
deliberately absent: `MINDHIVE_LIB` and `MINDHIVE_HOOK_DIR` are set by the hook
shims for their own library in the same process — plumbing, not
configuration. There are no lock-tuning variables: every lock's timeout and
staleness rule is a code constant (blueprint decision K4), listed in
[docs/RUNBOOK_RECOVERY.md](docs/RUNBOOK_RECOVERY.md).

"Switch" below means the value is compared case-insensitively against a fixed
list of spellings; anything else takes the default.

| name | type and default | read by | effect |
|---|---|---|---|
| `MINDHIVE_AUTOHARVEST` | switch; default on. `0`, `false`, `off`, `no` turn it off | `dashboard.py::_auto_harvest_loop`, inside `hive dashboard` / `hive ui` | the background loop that harvests sessions the live hooks missed |
| `MINDHIVE_AUTOHARVEST_SECS` | integer seconds; default 3, floor 1, unparseable → 3 | `dashboard.py::_auto_harvest_loop` | that loop's poll interval |
| `MINDHIVE_AUTORESEARCH_DIR` | path inside WSL; default `~/autoresearch`; read once at import | `research.py` | where the autoresearch checkout the trainer runs lives |
| `MINDHIVE_AUTORESUME_TIMER` | switch; unset = on. `off`, `0`, `none` turn it off | `autoresume.py::_timer_enabled` | whether `hive autoresume` registers a real Task Scheduler timer (the test suite turns it off) |
| `MINDHIVE_BRIEF` | switch; default on. `0`, `off`, `false`, `no` turn it off | `mindnet/brief.py` | the per-task brief composed for a MindNetwork sub-agent |
| `MINDHIVE_CI_PG_DSN` | libpq DSN; unset | `ci/legs.py`, `ci/legrun.py` | the admin login of a THROWAWAY Postgres 17 cluster of its own (a separate instance, port or container, like ci.yml's service container; the database under test is named `mindhive`), which the `pg` leg of `hive ci premerge` migrates and tests. Unset, or `MINDHIVE_CI_PG_DSN_RO` unset, the pg leg is named as not runnable here; equal to one of the operator's own DSNs, it is refused; and the leg stops before `pg migrate` when the cluster holds any other database. Never the real mirror's cluster: a migration from an unmerged branch cannot be taken back |
| `MINDHIVE_CI_PG_DSN_RO` | libpq DSN; unset | `ci/legs.py`, `ci/legrun.py` | the same throwaway cluster's `mindhive_reader` login, its password set once by the operator (ci.yml's `ALTER ROLE` step does it on Actions). The pg tier's read-only tests need it; without it they would skip, so the pg leg is not run here |
| `MINDHIVE_DASHBOARD_TOKEN` | string; unset → the token persisted in `dashboard.token` under the store root, minted once with `secrets.token_urlsafe(16)` | `dashboard.py::load_or_create_token`; `bench.py` reads it and never creates one | overrides the dashboard's URL token |
| `MINDHIVE_EVENT_BACKEND` | `file` (default), `pg` or `dual` | `events.py::_maybe_read_from_pg`, `dashboard_memo.py`, `adaptivetail.py` | `pg`: `read_all` reads the Postgres mirror, falling back to the files. `pg` or `dual`: the dashboard's memo fingerprints are disabled and the `MEMORY.md` rollover line adds the `hive pg tool search_events_by_type` pointer. Appends never touch Postgres under any value, and `dual` performs no dual-read comparison. Nothing in `src/` sets it (docs/DATA_MODEL.md §5, invariant 6) |
| `MINDHIVE_HEADLESS` | `1` when set; set by the code, not the operator | set by `claude_cli.py` and `workspace.py` on every headless `claude` child and removed by `rotation/supervisor.py` for its interactive successor; read by every `hooks/*.sh` / `hooks/*.ps1` shim | a hook fired inside a headless child exits at once (no recursion) |
| `MINDHIVE_HOME` | path; default `~/.mindhive` | `paths.py::store_home`, on every call | the store root: namespaces, `LIVE_LOCK`, `dashboard.token` |
| `MINDHIVE_HOOK_WALK_LIMIT` | integer; default 8, `0` = no walk; a non-number or a value of 10+ digits → 8 | `hooks/lib.sh`, `hooks/lib.ps1` | how many directories a shim climbs looking for a `.venv` |
| `MINDHIVE_INTERCEPTOR` | default `1`; exactly `0` turns it off | `orchestrator/interceptor.py` | the gateway interceptor that records an `interceptor_signal` on pre/post tool use; it never blocks a call |
| `MINDHIVE_LOOP_DASHBOARD` | switch; default on. `0`, `false`, `off`, `no` turn it off | `commands/orchestration.py` (`hive loop`) | whether a real `hive loop` run opens the dashboard; `--no-dashboard` does the same per run |
| `MINDHIVE_MINDSWARM` | switch; default on. `0`, `false`, `off`, `no` turn it off | `dashboard.py`, per request | hides the Workspace's MindSwarm nav row; read from the environment the dashboard was started with |
| `MINDHIVE_ORCHESTRATION_SECRET` | string; unset | `advice.py::is_authorized_sop_author` | when set, only a process whose `MINDHIVE_PROCESS_TOKEN` equals it may write an SOP; unset, the session-prefix rule decides |
| `MINDHIVE_PG_DSN_ADMIN` | libpq DSN; unset | `pgmirror.py`, `autosync.py`, `ci/runner.py`, `storage/cli.py`, `ci/legrun.py` (set inside the pg leg to `MINDHIVE_CI_PG_DSN`'s value, never read), `ci/legs.py` (compared, so the CI DSN is never this one) | the owner credential for `hive pg migrate`/`rebuild`/`refresh`/`embed`; the default of `hive db migrate --dsn`; when set, `hive autosync` refreshes the mirror and `hive ci run` runs the pg tier |
| `MINDHIVE_PG_DSN_RO` | libpq DSN; unset | `pgstore.py`, `ci/legrun.py` (set inside the pg leg to `MINDHIVE_CI_PG_DSN_RO`'s value, never read), `ci/legs.py` (compared, so the CI DSN is never this one) | the reader credential (`mindhive_reader`, read-only at the role level) behind `hive pg tool` and the MCP server |
| `MINDHIVE_PG_MAX_BYTES` | integer bytes; default 32768, ceiling 131072; zero, negative or unparseable → default | `pgshape.py` | the response cap on one AI-tool envelope |
| `MINDHIVE_PLUGIN_AUTOSYNC` | switch; default off. `1`, `true`, `yes`, `on` turn it on | `plugin_cache.py`, through `hive plugins cache-check --hook` | on drift at session start, sync through `claude plugin update` (can hold session start up to 45 s) |
| `MINDHIVE_PROCESS_TOKEN` | string; unset | `advice.py::is_authorized_sop_author` | the write token checked against `MINDHIVE_ORCHESTRATION_SECRET` (`hmac.compare_digest`); withheld from every agent the package spawns |
| `MINDHIVE_PROJECT` | namespace name; default: the slug of the git main repository's directory (a linked worktree collapses onto it), else the working-tree root's name, else the working directory's | `paths.py::detect_project` | which namespace every command reads and writes; `--project` overrides it per command |
| `MINDHIVE_PYTHON` | path to an interpreter; unset | `hooks/lib.sh`, `hooks/lib.ps1` | first in the shims' interpreter resolution order, when it names a real file |
| `MINDHIVE_RESEARCH_CAP_S` | integer seconds; default 900; read once at import | `research.py` | the outer watchdog on one research experiment (the 300 s training budget plus startup and compile) |
| `MINDHIVE_RESEARCH_TRAIN_CMD` | shell command; default `cd <MINDHIVE_AUTORESEARCH_DIR> && uv run train.py`, run through `wsl bash -lc` | `research.py::wsl_trainer_cmd` | the trainer command a research iteration runs |
| `MINDHIVE_RESEARCH_TURN_TIMEOUT_S` | seconds (float); default 600; read once at import | `research.py` | the bound on one researcher edit-turn (the thinking time between experiments) |
| `MINDHIVE_RESEARCH_WEB` | switch; default on. `0`, `false`, `off`, `no` turn it off | `research.py` | kill switch for learn mode's trusted web domains; off returns it to the offline read-only tools |
| `MINDHIVE_ROTATION` | switch; default off. `1`, `true`, `on`, `yes` turn it on | `rotation/hook.py`; set to `1` by `rotation/supervisor.py` for the children of `hive session run` | switches the Stop hook's smart-zone measurement on; off, the hook is silent |
| `MINDHIVE_ROTATION_CAP` | integer tokens; default 500000 (`rotation.window.DEFAULT_CAP`); unparseable → default | `rotation/hook.py` | the absolute bound in the rotation threshold `min(cap, ratio × window)` |
| `MINDHIVE_ROTATION_RATIO` | float; default 0.5 (`LoopConfig.smart_zone_ratio`); unparseable → default | `rotation/hook.py` | the proportional bound in that threshold; the window is the model's, 200000 when unknown |
| `MINDHIVE_ROTATION_SUPERVISED` | marker; set only by `supervisor._child_env` | `rotation/hook.py::supervised` | chooses which advice the hook prints (a successor is queued, or nothing is); **not** an enable switch |
| `MINDHIVE_SESSION` | session id; hooks export the payload's id after validating it; unset → `manual` | `events.py::append_event` and most commands' `--session` default | the session every append is attributed to |
| `MINDHIVE_SYMBOLS` | any non-empty value turns it on — **including `0`**; unset or empty = off | `symbols.py::enabled` | adds the zero-token symbol map to every prompt's injected context |
| `MINDHIVE_SYMBOLS_ROOT` | path; default the working directory | `symbols.py` | the tree the injected symbol map is built from |
| `MINDHIVE_UI_IDLE_CUTOFF_S` | seconds (float); unset → 60 (`server.lifecycle.IDLE_CUTOFF_S`); anything but a positive finite number is ignored with one stderr line | `server/lifecycle.py::idle_cutoff_from_env`, at call time, for `hive ui` only (not `hive dashboard`) | the idle cutoff when `--idle-cutoff` is absent; the flag wins |
| `MINDHIVE_WATCHER_POLL_S` | seconds (float); default 2.0; zero, negative or unparseable → 2.0 | `watcher.py` | the embedding watcher's poll interval; an explicit argument beats it |
| `MINDHIVE_WEIGHT_MODEL` | `log_linear` (default) or `linear`; unset → `log_linear` | `orchestrator/metrics.py::_resolve_model`, at every weighing; set for one mission by `hive lintester --weight-model` and restored afterwards | the fusion a mission weighs its units by; an unknown name falls back to `log_linear` and the report says so |
| `MINDHIVE_WS_TIMEOUT` | integer seconds; default 1800; read once at import | `workspace.py` (`RUN_TIMEOUT_S`) | the kill timer on one Workspace (MindParallel / MindSwarm) run |
| `MINDNETWORK_ENFORCE` | tri-state; default dormant. `1`, `true`, `yes`, `on`, `enable`, `enabled` enforce; `0`, `false`, `no`, `off`, `disable`, `disabled` stay dormant | `mindnet/optin.py` | whether MindNetwork enforcement is armed; any other value, the empty string included, leaves it to the per-checkout record that `hive mindnet enforce --enable` / `--disable` writes, and with no record it stays dormant |
| `ANTHROPIC_API_KEY` | Anthropic's own; not set by MIND HIVE | `workspace.py`, in the auth status check | with no CLI login, a set key reports the Workspace's auth as ok |
| `MKL_NUM_THREADS` | set to `2` at `import mindhive.vectors` with `setdefault`, so an exported value wins | `vectors.py` (`THREAD_ENV`) | bounds the embedding encoder's threads; the same `setdefault` gives `VECLIB_MAXIMUM_THREADS` and `NUMEXPR_NUM_THREADS` `2` and `TOKENIZERS_PARALLELISM` `false` |
| `OMP_NUM_THREADS` | as `MKL_NUM_THREADS` | `vectors.py` (`THREAD_ENV`) | as `MKL_NUM_THREADS` |
| `OPENBLAS_NUM_THREADS` | as `MKL_NUM_THREADS` | `vectors.py` (`THREAD_ENV`) | as `MKL_NUM_THREADS` |

## Disaster recovery

The event ledger (`events/*.jsonl` under each namespace) is the only source of
truth; everything else — `STATE.md`, `LEDGER/`, `chain.json`, the chroma index,
the Postgres mirror — is a projection a command can rebuild from it. So recovery
is almost always "rebuild the projection", and the one thing never to do is
edit a ledger row: corrections are new events.

The step-by-step runbook — scenario, how you notice, the exact commands, and
how to confirm the fix — is [docs/RUNBOOK_RECOVERY.md](docs/RUNBOOK_RECOVERY.md).
The short form:

- **Semantic recall fails**: `hive recall --semantic` exits 1 and names the
  failing stage and the repair. A failed `count` or `query`, or a cut segment
  of `ledger` or `events`: `hive embed --rebuild --project <ns>`, which drops
  the collection without reading it, and the delete below if the read still
  fails. Files chromadb cannot open (any other `open`) or damage to the
  watcher's collection: stop the writers (the dashboard, `hive ui`,
  `hive watcher run`), delete the `chroma/` directory the message names, and
  run `hive embed --project <ns>`. An empty answer means nothing is indexed
  yet: `hive embed --project <ns>`. Then `hive watcher recover` and
  `hive watcher run --once`; confirm with `hive recall "<query>" --semantic`.
- **A lock left behind by a dead process**: every lock expires on its own
  (the runbook lists each one's staleness rule); `hive watcher recover`
  returns abandoned embed claims.
- **`hive verify` reports the chain broken**: stop, read `hive verify --json`,
  and do **not** run `hive project` until you have decided — it re-seals the
  chain over whatever is on disk now. There is no repair command.
- **The Postgres mirror disagrees**: `hive pg parity`, then `hive pg refresh`
  or `hive pg rebuild`.
- **Projections missing or stale**: `hive project`, then `hive sync`.

## CI

The **daily gate is `hive ci run`**, not GitHub Actions. It runs the same four
gates locally, in a `git worktree add --detach` of its own, so the tree being
judged is the branch and not whatever the working copy happens to hold:

1. the repo's own guards (file-size cap, docs parity, CLI arg wiring,
   architecture drift, pg repo gates), filtered by what the branch actually
   has — pytest answers exit 4 for a path that is not there, and a branch
   predating one of these files is not broken;
2. `python -m pytest -q`;
3. the `pg` tier plus `hive pg parity`, only when `MINDHIVE_PG_DSN_ADMIN`
   names a server — skipped, and said to be skipped, otherwise;
4. `chain.verify()`, in-process.

Every gate child runs with `PYTHONPATH` pointing at the worktree's `src`
first and `PYTHONDONTWRITEBYTECODE=1`, so it imports the tree under test
rather than the editable install and leaves no `__pycache__` behind in a
directory that is about to be deleted.

The verdict is appended once as a `verification` carrying `scope: "ci"` and
the gate that decided it; `hive ci status` reads it back and a red one turns
the session's Oversee card red. A red run also crosses into `hive incidents`
through the same bridge a failed tool call uses.

Put it where pushes pass:

```sh
hive ci hook-install
```

`.github/workflows/ci.yml` is **`workflow_dispatch` + `push: tags: ["v*"]`**
only, and while the account's Actions jobs are refused for payment it cannot
run at all. Its jobs run here instead:

### Pre-merge CI on this machine (`hive ci premerge`)

`hive ci premerge <PR>` does what the Actions checkout of `refs/pull/N/merge`
did, without a runner or a bill:

1. fetches the base and `refs/pull/N/head` into refs of its own
   (`refs/mindhive-ci/<run>/…`, deleted afterwards) — nothing checked out is
   read or moved;
2. computes the merge with `git merge-tree --write-tree` (git ≥ 2.38) — a
   conflict is red at once, and no leg runs;
3. runs each `ci.yml` leg in a fresh repository whose `HEAD` is that merge
   commit, with its whole history — read from this repository's object store
   through `.git/objects/info/alternates`, not copied — no hooks, and CRLF for
   a windows leg and LF for an ubuntu one, as the runners check out; then a
   fresh venv, `pip install -e .[extras]` and the job's own pytest command —
   windows legs through `py -0p`'s interpreters, ubuntu legs in WSL (a login
   shell, so uv's interpreters are found), a few at a time, under
   `<tempdir>/mindhive-ci` unless `--work-root` names another place;
4. posts `mindhive-ci/<leg>` and the overall `mindhive-ci` as commit statuses
   on the PR's head — a plain REST write, nothing billed;
5. records one `verification` (`scope: "ci"`, `gate: "premerge"`) with every
   leg's counts, and keeps the report and each leg's log as runlog artifacts.

The suite reads history — `tests/test_implementation_plan.py` names a commit —
so a one-commit checkout is red on master itself. Every `ci.yml` checkout is
therefore `fetch-depth: 0`, and each leg has the history the same way.

A leg this machine cannot run is **named, never counted**: the verdict says
`not run here: pg`. `hive ci legs` shows which legs can run and why the others
cannot. Two legs depart from their `ci.yml` job, and say so in `hive ci legs
--json`: the `browser` leg runs the interpreter running `hive` (which must
launch playwright's Chromium) against the leg's own `src`, so no Chromium is
downloaded per run, and the `pg` leg runs only against a throwaway Postgres
cluster of its own, named by `MINDHIVE_CI_PG_DSN` and `MINDHIVE_CI_PG_DSN_RO`
— never the operator's own DSNs. A leg whose pytest passed no test is not a
pass: a tier whose every test skipped proved nothing.

A PR from a fork is refused unless `--trust`: its legs would run its code on
this machine as you, so read its diff first. `--legs` runs some legs only:
each leg's status is posted, the overall `mindhive-ci` is not, and a partial
run never merges.

`--merge` merges a green PR, guarded: the PR must still be open, not a draft,
and at the tested head; a base that moved is previewed again and a different
merge is tested again (up to three rounds) unless `--allow-base-drift`; the
merge is `gh pr merge N --merge --match-head-commit <head>`, and only GitHub
reading back `MERGED` counts. The landed commit's tree is compared with the
tested tree. How the merge went is `merge_state`, one word — `merged`,
`refused`, `failed`, `unknown` (the run stopped inside `gh pr merge`, so the
PR's state on GitHub says whether it landed) or `skipped` (no merge was asked
for, or the verdict was not green) — and the why is `merge_reason`.

Exit codes are for scripts and agents: `0` green; `1` red; `2` the check could
not be made; `3` green, but `--merge` did not merge; `130` stopped by hand
(Ctrl+C) — every leg it started is stopped, and a run that got as far as
fetching is recorded as an error. A leg step whose command could not be
started at all records exit `127`. Under `--json` stdout is JSON whatever
happens: a refusal or a stop prints one object, `{"verdict": "error",
"exit_code": N, "reason": "..."}`, beside the line on stderr, and each verb's
`--help` ends with its exit codes. `hive ci status --gate premerge --pr N`
reads the last run back with the time it was `recorded_at`; `--branch` picks a
local branch's runs.

To keep every open PR's verdict current, run the watch — by hand, or from a
scheduled task you register yourself (MIND HIVE never registers one):

```sh
hive ci watch                  # loops; Ctrl+C to stop
hive ci watch --once           # one pass, for a scheduled task
schtasks /Create /TN "MindHive CI watch" /SC MINUTE /MO 30 /TR "<venv>\Scripts\hive.exe ci watch --once --root <repo> --project <namespace>"
```

Each pass tests a PR whose head moved; re-checks, with one `git merge-tree`, a
PR whose base moved; tests again, once per head, a red that errors alone made;
and posts again a verdict GitHub refused to take. Every action it reports has
`pr`, `action`, `verdict`, `exit_code` and `detail`: `--once --json` prints the
pass as one array, and a looping `--json` watch one JSON line per pass.

## Installing the hooks on a new machine
The hooks are portable: nothing in `hooks/` names a user, a checkout, or a venv.
Each shim resolves its interpreter at run time through `hooks/lib.sh` (POSIX) or
`hooks/lib.ps1` (PowerShell), in this order:

1. **`MINDHIVE_PYTHON`**, when it points at a real file. The operator override —
   set this if your interpreter is somewhere unusual. On Windows both libraries
   read it the same way whether it is spelled `C:\...\python.exe`, in Git Bash's
   `/c/.../python.exe` form, or without the `.exe`.
2. **The nearest `.venv` above the shim**, probing `Scripts/python.exe`, then
   `bin/python3`, then `bin/python` — one order in both libraries, so one tree
   gets one answer. In a plain checkout `mindhive` is installed editable in that
   venv, so it is the interpreter that imports the checkout the hook belongs to.
   A git worktree is the exception: a checkout under
   `<repo>/.claude/worktrees/<name>` with no `.venv` of its own walks up to
   `<repo>/.venv`, whose editable install imports `<repo>/src` — so that
   worktree's hooks run the **main checkout's** code, not the worktree's branch.
   To run a worktree's own code, give it its own `.venv` (`pip install -e .`
   inside the worktree); the walk finds the nearer one first. The walk climbs at
   most `MINDHIVE_HOOK_WALK_LIMIT` directories (default 8; `0` turns it off).
3. **`python3`, then `python` from PATH**, as a last resort — except a hit under
   Windows `WindowsApps`, which is the Microsoft Store redirector. That one is
   refused on purpose: it spawns cleanly, prints an install prompt and exits 49,
   so accepting it would give every hook an interpreter that never runs a line.
   The refusal is a SKIP, not a stop — on a default Windows profile that
   directory sits on PATH *ahead* of a per-user Python install, so the search
   goes on to the next match rather than concluding there is no interpreter.
   Both libraries walk every PATH entry themselves to make that true; a
   first-hit-only lookup answers "no interpreter" on a machine that has one,
   and `Get-Command`, which `lib.ps1` used to lean on, does not strip the
   quotes a PATH entry may legally carry, so an interpreter in
   `"C:\Program Files\Py"` was found by one family and not the other. On
   Windows only a `.exe` counts from PATH: a `python.cmd` or `python.bat`
   wrapper runs through `cmd.exe`, which re-parses its arguments, so both
   libraries pass over it. One asymmetry is deliberate: under Git Bash
   `lib.sh` also accepts an extensionless `#!` script, which that shell can
   exec and PowerShell cannot, so `lib.ps1` passes such a file over.

If none of those resolve, every shim exits 0 and prints nothing — with one
documented exception: `pre_compact` still prints its human reminder, because
that text is the whole point of the hook and needs no interpreter to compose.
The silence is deliberate — these hooks fire in every session on the machine, so
a hook that can fail loudly is worse than one that occasionally does nothing.

So the whole setup is:

```
git clone <repo> && cd mindhive
python -m venv .venv
.venv/Scripts/pip install -e .      # or .venv/bin/pip on POSIX
hive session install-hook "$HOME/.claude/settings.json"     # POSIX
```

On Windows PowerShell the last line is:

```
hive session install-hook "$env:USERPROFILE\.claude\settings.json"
```

Spelled out rather than `~`, because nothing expands it: the CLI hands the
string to `pathlib.Path`, which treats `~` as a literal directory name, so the
`~` form reports `missing` against a path that does not exist and registers
nothing. The shell is what expands `$HOME`; `pathlib` never sees the tilde.

Registering the Stop hook does **not** activate rotation: the shim stays inert
unless `MINDHIVE_ROTATION` is set, and only `hive session run` sets it, for the
children it spawns.

`hooks/` holds nine shims, but they are not nine hooks to register. `autosync`
is not a Claude Code hook at all — it is the scheduler wrapper (Task Scheduler
on Windows, cron on POSIX) — and `post_tool_use_failure` is built but not
registered today (`docs/prds/2026-07-21-002-rust-tool-interception-prd.md`
lists it as "built, unregistered"). That leaves six — `SessionStart`,
`PreToolUse`, `UserPromptSubmit`, `PostToolUse`, `PreCompact`, `SessionEnd` —
to register by pointing `~/.claude/settings.json` at the shim for your
platform, e.g.
`powershell -NoProfile -ExecutionPolicy Bypass -File "<repo>/hooks/session_start.ps1"`
or `sh "<repo>/hooks/session_start.sh"` — plus `Stop`, which `install-hook`
writes for you.

## Install the `/circle` plugin (global)
This repo is a self-contained Claude Code plugin marketplace. To get `/circle` in **every** project:

```
/plugin marketplace add hanifridho62-rgb/Lintester-project
/plugin install mindhive@mindhive-dev
```

(or `/plugin marketplace add <path-to-your-checkout>` to install from a local clone). Then start a new session and type `/circle`. `hive loop` itself still needs the `mindhive` package importable (the plugin shells out to `python -m mindhive loop`) and the `claude` CLI on PATH.

## Event types
Semantic (you/Claude emit): decision, experiment_result, caveat, next_step,
open_question, state_set.
Deterministic (machine-generated, no AI/API cost): file_touched, command_run,
git_diff, session_start, session_end, compaction, plugin_catalog. The first six
are emitted by the session hooks; `plugin_catalog` is emitted on demand by
`plugins scan`/`autoscan` (the `session_start` hook fires a stale-gated
`autoscan` so it refreshes across sessions without you lifting a finger).

## Plugin/skill catalog: auto + manual
The recommendation catalog refreshes three ways, so it's current in any session:
- **Auto (new sessions):** the `session_start` hook runs `plugins autoscan` in
  the background — stale-gated, so it only does real work when the snapshot is
  missing or >24h old.
- **Auto (panel):** opening the dashboard **Skills & plugins** view calls the same
  stale-gated autoscan, so just looking at it keeps it fresh.
- **Manual (force):** `hive plugins scan` or the dashboard **Plugins scan**
  (Heavy) button force a full rescan anytime, in any session.

Each snapshot is stamped with the plugin version and inventory signature it was
scanned under (`version_hash`). A plugin version bump, an install, an uninstall,
or a skill added or removed makes the stored snapshot stale at once, whatever its
age, and the next autoscan replaces it.

Because every session shares one event store, a refresh by one session is
immediately visible to all the others.

## Skills & plugins: a live, callable, self-healing view
The dashboard's **Skills & plugins** view is a real-time inventory driven by
Claude's own `installed_plugins.json`, so installing or uninstalling a plugin —
or deleting a local skill — updates the list within seconds (a cheap disk
signature gates the rebuild). Every entry is:
- **Click-through** — opens a detail panel with the skill's **intended use**
  first, then the date it was added, then the full `SKILL.md` body.
- **Callable** — a **▶ call this skill** button (or `hive skills invoke`) runs it
  through MindHive via a headless `claude`. The indicator turns **green** on
  success and **red** on failure.
- **Self-healing** — a failed call is recorded as a `skill:<name>` incident that
  flows into the **Improvements** loop, so a bad call becomes a prevention rule
  the next run can consult. All installed skills are callable this way, not a
  hand-picked subset.
- **Effectiveness-aware ranking.** Skill invocations are captured live (and
  back-filled from transcripts), so recommendations blend coverage with a
  measured success rate. A skill with ≥3 calls and <40 % success is demoted:
  listed with its failure reason instead of recommended. No history = neutral
  (unproven skills are neither boosted nor punished).

## PowerShell 5.1 note
Inline JSON args are mangled by PowerShell 5.1. To set state from PS, pipe via stdin:

```powershell
'{"task":"OFI verdict","next_step":"recapture 24h"}' | hive append state_set --json --stdin
```

(Claude in-session uses the Bash tool, where normal quoting works.)

## V17 safety
`embed` and `recall --semantic` refuse to run while `~/.mindhive/LIVE_LOCK`
exists (V17 is trading). Override with `--force` only when the box is idle.
For both commands the refusal — and a missing `[vectors]` extras install — is
`error: …` on stderr and exit 2, as is a `recall --limit` below 1. A semantic
index that cannot be read is exit 1, never an empty result: the message names
the failing stage, the project namespace and a remedy that depends on what
broke — wait out a busy writer, free the temp directory, or repair the
damaged index. Where a collection opened and then failed — a query that
answered nothing from a collection holding rows it could have returned, or a
hit without its text, included — or the `ledger` or `events` collection read
has a cut segment, that is `hive embed --rebuild --project <ns>`
first, which drops the collection without reading it, and the delete of the
`chroma/` directory it names if the read still fails. Where chromadb cannot
open the files — a rebuild needs them open too, and fails on them — or the
damage is the watcher's collection, which a rebuild never writes, it is the
delete alone: delete the directory and run `hive embed --project <ns>`; the
watcher's collection refills only from rows appended afterwards. The chromadb
calls of a read run in a child process of its own, so a crash or a hang in
chromadb's native core on a damaged index — measured, a cut `link_lists.bin`
segfaulted 19 reads of 23 in-process — is exit 1 for the CLI and a 500 for
the dashboard, never the death of either; the child is closed, its copy
deleted, and the same bytes are refused without a new copy for ten minutes.
Read in-process (a frozen build), a client a read failed on is never closed
(closing one corrupted the heap), so once 16 are held, an index with a
failure no read has got past is not read again until the process restarts,
and the message names the restart after the repair, before any delete. A
`hive embed` that fails writing prints one
`error:` line — what it wrote before the failure, and the repair — and exits
1. A quarantined namespace — an
`unsafe-<hash>` directory, whose name `--project` refuses — is repaired without
`--project`: run `hive embed` from the project's checkout, or with the same
`MINDHIVE_PROJECT`, and the message says so. An argument chromadb refused at
the query — a bad `n_results`, an id named twice — is reported as such, with
no repair: the index is not at fault. A panic in chromadb's Rust core is a
failed read like any other. A semantic read never writes the index: it
queries a private copy (docs/DATA_MODEL.md §4.19). A persisted segment whose
files are shorter than its header says — a cut `link_lists.bin`, which
crashed or hung the process, or a cut `data_level0.bin`, which answered
without an error and could miss the best hits — is refused before chromadb
opens it, with the rebuild first. Not contained: a wrong answer given
without an error.

## Usage-cap fallback
When the Opus window is nearly spent, run `hive export` and paste handoff.md into
any model/client. It is deterministic and costs no API tokens.

## Oversee: live work sessions

The dashboard's **Oversee** view (Work group) shows every kind of live Claude
work for the selected project as a titled card:

- **`hive loop` runs** — titled by a one-line summary of what the run is doing
  (the latest iteration summary, seeded from PROMPT.md).
- **Interactive Claude sessions** — no `/circle` or loop needed: the moment a
  session's hook telemetry flows (prompts, file touches, commands), it appears,
  titled by your latest prompt.

Click a title to expand the progress detail, rendered as a Claude-style task
checklist in the hive palette: green **✓** done lines (past iteration
summaries / checked fix_plan items / decisions and touched files), a pulsing
**◐** for what's happening right now, and gray **○** pending lines (unchecked
required items, next steps; Future/Optional items dimmed). While anything is
live, an **online — Claude is working** strip shows at the top (loop heartbeat
= one iteration timeout; chat heartbeat = tool events within ~3 minutes); it
disappears when nothing is working.

Sessions are **removed automatically** when a loop run ends through the dual-
condition exit gate (graceful exit) or the circuit breaker trips, when an
interactive session ends, or when a chat session goes idle past ~6 hours. Any
card can be deleted manually with its **delete** button. Deletion is an
append-only `loop_run_dismissed` tombstone — the events stay in the hash chain
(the archive), so `hive verify` never breaks — and a session that was actually
still alive resurfaces on its next event instead of burning tokens out of
sight.

## Desktop control panel

`hive dashboard` starts a localhost web app (bound to 127.0.0.1) and opens your
browser to a control panel for every feature — view state/briefing/graph, run
recall, append memories, project, sync, verify, export, embed, and the live
**Skills & plugins** view (click-through detail, one-click skill calls, green/red
status). The graph is one of its views.

**Improvements view.** The Memory group also has an **Improvements** view:
lessons learned from past session logs — recurring tool failures projected into
incident lessons (root cause, lesson, prevention rule, category) plus manually
recorded caveat notes, with search and status/category filters. An efficiency
strip compares the tool-failure rate (per 100 tool actions) against the
previous day / week / month / 3 months; lessons sort by newest, most critical,
or most frequent (with a direction toggle), carry first/last-seen timestamps,
and lead with a resolution glyph — green ✓ (rule holding), red ✗ (unresolved
or recurring), amber ● (watching / not yet tested), gray ○ (dismissed via the
per-card dismiss button). The **Reflect (learn from failures)** button (Heavy)
turns recurring unreflected incidents into lessons via the `claude` CLI, a
small bounded batch per click.

**Workspace: MindParallel & MindSwarm.** The **Workspace** group runs real
Claude Code agents from the dashboard, all feeding the same hive memory.
**MindParallel** is a grid of up to 16 panes — each pane is a headless `claude`
session (follow-ups resume the same session), with named **profiles** mapping
panes to different `CLAUDE_CONFIG_DIR`s so multiple accounts run side by side,
and a kanban board whose cards dispatch their prompt to a free pane and move
backlog → running → done with the run. **MindSwarm** launches a mission crew —
a coordinator plus builders, scouts, and reviewers — where every agent's system
prompt tells it to read and write the shared Mind Hive ledger (`hive recall` /
`hive append`), so what one agent learns the next one starts with; a command
bar directs any single agent or the whole swarm. Every exchange lands in the
ledger as `user_prompt` / `assistant_response` events (failures as
`tool_failed` with signature `workspace:<role>`), so workspace activity flows
into Memory, the graph, and the Improvements loop automatically.

**Self-healing capture.** The server runs a background loop that periodically
**auto-harvests** any session the live hooks missed — so the store stays current
even in environments where the hooks never fire (e.g. the Agent SDK harness).
It's safe alongside the hooks: it skips sessions already captured live (no
double-counting) and only touches projects you already track (no surprise
namespaces). Disable with `MINDHIVE_AUTOHARVEST=0`; tune cadence with
`MINDHIVE_AUTOHARVEST_SECS` (default 3 seconds, floor 1). Trigger it by hand with the **Catch
up** button, the `POST /api/harvest` endpoint, or `hive harvest --catch-up`.

**MindSwarm is soft-deprecated, and the toggle is one variable.** New work goes
through the mission pipeline (`hive lintester "<intent>"`), so the Workspace's
**MindSwarm** row is no longer a front door. Nothing was removed: the page, the
`/api/workspace/swarm` route and its renderer all still answer, and the row is
still shown by default. `MINDHIVE_MINDSWARM=0` (or `false`/`off`/`no`) hides
just that nav row. Set it in the environment the dashboard is **started** with:
a running process cannot see a variable you export into a shell afterwards, so
bringing the row back means restarting with it unset, or set to `1`. `hive
mindnet orchestrate` carries the same notice on stderr — it is kept because the
mission pipeline builds its own graph and cannot run one you declared.

**The six long operations are jobs, not requests.** Catch up, Embed, Plugins
scan, Reflect, Resolve red and Self-test answer **`202 Accepted`** with
`{"ok": true, "job": {"id": …, "state": "running"}}` and do the work on their own
thread; poll `GET /api/job?id=<id>&offset=<bytes>` for state and for output since
`offset`. They used to hold one fetch open for the whole run, so anything longer
than the browser's timeout was reported as a failure while it was still going and
still about to succeed. A second click while one runs is still refused the same
way as before — `{"ok": false, "error": "… already running"}` with a 200.

A finished job names **what it did**, not just that it ran: `did_work`,
`nothing_to_do`, `degraded`, or `failed`. "Nothing to do" is a success — a
harvest that found no missed sessions is working correctly, and it used to be
indistinguishable from one that ingested forty. Each run leaves `job_started` and
`job_finished` rows in the ledger; a job whose process died is closed out as
`job_abandoned` by the next dashboard start-up, so an orphan no longer reads as
still-running forever.

That vocabulary is not the dashboard's alone. **Five surfaces in MIND HIVE report
progress to a human, and all five now answer in the same words** — the rule being
that every path which can produce zero work, a degraded result, or an abandoned
job has to say *which* of the three it is, and *why*:

| Surface | The loop it closes |
|---|---|
| `dashboard-jobs` | a long click reports what it did, not just that it started |
| `roadmap-retirement` | a plan card retires because something checkable said so, or names which of four things stopped it |
| `rotation-advice` | the end-of-turn advisory claims a queued successor only when one is really queued |
| `sop-injection` | a prompt is told when a mandatory procedure arrived as a pointer instead of its body |
| `memory-budget` | the always-on digest reports the sections that fall past the loader's cut instead of losing them quietly |

Each surface registers itself in `outcomes.py` and carries its own read-only
probe, which the suite runs on every pass. The point of the registry is what it
makes impossible to do quietly: a surface can no longer report "fine" for a run
that did nothing, and `nothing_to_do` — rotation switched off, no live plan, no
mandatory SOP, no digest written yet — is a **success with a reason attached**,
not an absence of news.

**The URL is stable across launches.** It binds a fixed port (**54461**) and a
token persisted to `~/.mindhive/dashboard.token`, so every launch (CLI or
`MindHive.exe`) serves the same `http://127.0.0.1:54461/?token=…` — bookmark it
once. Override the port with `--port N`, or use `--port 0` for an ephemeral one;
override the token with the `MINDHIVE_DASHBOARD_TOKEN` env var. If the port is
already bound (the dashboard is already running), a second launch just reopens
that same URL instead of failing.

The panel updates in **real time**: the Overview, Memory, Sessions and State
views auto-refresh every few seconds and the moment you append a memory, so new
events — including ones written by another session or hook — appear without a
manual reload. A `● live` toggle in the header pauses/resumes it; polling also
pauses while the tab is hidden.

**Project → Session → Memory.** A **project** picker in the header switches which
store namespace the page shows (it defaults to the one with the most events, so
a fresh launch lands on real data rather than an empty cwd-derived namespace).
Within a project the panel keeps three layers distinct so intent and context
stay legible:
- **Memory** — the semantic events you/Claude author (decision, caveat,
  experiment_result, next_step, open_question, state_set): the intent layer.
- **Sessions — notification center.** Every session renders as a collapsible
  task card: status badge (`● ongoing` / `✓ completed`), a summary chip
  ("edited 8 files (+120 −44) · ran 3 commands · 1 note"), and expandable
  rows for touched files (+N −M line stats), commands (with descriptions),
  notes/caveats, failures, and loop iterations (progress · breaker · tokens ·
  cost). Expanding a file or command row fetches the full diff or command
  output lazily from the Claude Code transcript — nothing heavy is stored in
  the ledger, and sessions whose transcript is gone degrade to a notice.
  The history is append-only and deliberately has no delete button; expanded
  cards show the last 100 actions (older rows stay in the ledger). Zero-change
  file writes (+0 −0) are not logged.
- **session / auto** — deterministic telemetry (file_touched, command_run,
  session_start/end, …), counted separately on the Overview so it never drowns
  out the memory.

Every endpoint takes a `?project=` (GET) / `"project"` (POST) parameter, so the
same running server serves any namespace; the picker just drives that.

**HIVE MIND — one unified view.** The first entry in the project picker
(`⨁ HIVE MIND (all projects)`, internally `project=*`) aggregates **every**
project into a single stream: all sessions, logs, memory and context unified.
Overview sums events/memory/sessions across the whole store (plus a project
count); Memory, Sessions, Recall and the Graph span all namespaces and label
each item with its project; Verify reports per-project chain status. It's a
read-only lens — mutating actions (append, sync, embed, scans) still need a
concrete target project, so select one first.

`hive graph export` still writes a single self-contained `graph.html` (data +
renderer inlined, no network, no keys) you can open in any browser on any PC.
Nodes are labelled and coloured by meaning — decisions, caveats (amber),
experiment results, open questions, etc. — with a colour legend, click-to-inspect
side panel, and a **Fit** button that frames the whole graph.

To build a double-click binary (Windows, Python 3.12):

    pip install -e .[build]
    python build/build_app.py        # -> dist/MindHive.exe

The exe runs a small console window showing the (stable) URL; close it to stop
the server. The window/exe use `build/icon.ico`.

## License

MIND HIVE is licensed under the [Apache License, Version 2.0](LICENSE). Every
source file in `src/mindhive/`, `hooks/`, `scripts/` and `build/` opens with
`SPDX-License-Identifier: Apache-2.0`, and `tests/test_license.py` fails on one
that does not. The one deliberate exception is the Postgres migrations
(`src/mindhive/pgmigrations/*.sql`): `apply_migrations` pins every applied file
by its sha256 and refuses to run against a changed one, so they keep their
bytes.

Third-party code keeps its own license: `skills/caveman/` is vendored from
JuliusBrussee/caveman under the MIT License
([skills/caveman/LICENSE](skills/caveman/LICENSE)).

Contributions are accepted under the same terms (Apache-2.0, section 5) — see
[CONTRIBUTING.md](CONTRIBUTING.md). Notable changes are listed in
[CHANGELOG.md](CHANGELOG.md).
