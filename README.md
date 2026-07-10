# MIND HIVE

[![CI](https://github.com/hanifridho62-rgb/Lintester-project/actions/workflows/ci.yml/badge.svg)](https://github.com/hanifridho62-rgb/Lintester-project/actions/workflows/ci.yml)

Deterministic, event-sourced memory for long-running Claude sessions.
Append events; deterministic projections rebuild everything. Nothing is hand-edited.

## Commands
- `hive append <type> <payload> [--json] [--stdin] [--tags a,b]` — add an event
- `hive project` — rebuild STATE.md + LEDGER/ + index.json + chain.json
- `hive state` — print the live STATE.md
- `hive recall "<query>" [--semantic]` — grep (default) or vector search
- `hive rules summaries [--project P] [--json]` — structured lesson records (root cause, lesson, prevention rule, category, active rule) for every incident — the same data the dashboard **Improvements** view shows, for agents working headless. `--json` emits the raw list
- `hive graph export [--out path] [--semantic] [--open]` — write a standalone, offline graph.html
- `hive dashboard [--port N] [--no-browser]` — open the web control panel for every feature
- `hive plugins scan [--cache dir] [--top N]` — catalog free (no API key / no pay) skills & plugins, score by breadth, store a `plugin_catalog` snapshot, print the top N
- `hive plugins autoscan [--max-age-hours H] [--top N] [--force]` — scan **only when the catalog is missing or stale** (default 24h); a near-instant no-op otherwise, so it's safe to run every session
- `hive plugins recommend` — print the top recommendations from the latest snapshot
- `hive skills list [--json]` — list every installed plugin + skill from the live registry (updates the instant one is installed/removed)
- `hive skills invoke <name> [--plugin p]` — call any installed skill through MindHive via a headless `claude`; records the outcome and exits non-zero on failure so callers can gate on it
- `hive skills status` — green/red last-call outcome per skill from the ledger
- `hive review queue [--session id]` — queue the session's touched source files for a thermo-nuclear review (run by `session_end`; writes the pending-review marker)
- `hive review show` — print the pending-review marker, if any
- `hive review clear` — clear the pending-review marker once the review has run
- `hive export [--out path]` — write a portable handoff.md (zero API tokens)
- `hive verify` — check the hash chain
- `hive embed [--force]` — build the on-demand vector index
- `hive selftest` — run the test suite
- `hive loop init` — scaffold `.hive-loop/` (PROMPT.md, fix_plan.md, AGENT.md, loop.toml) and check the `claude` CLI
- `hive loop` — run an autonomous Claude Code loop in the current project until the task is done; memory-seeded each iteration, with real token/cost budget gating, a context smart-zone reset at half the window, a dual-condition exit gate, and a circuit breaker. A real run also **auto-opens the web dashboard** so you can watch it live — opt out with `--no-dashboard` or `MINDHIVE_LOOP_DASHBOARD=0`
- `hive loop status` — summarize the last loop run
- `hive loop --dry-run` — show prompts + projected budget/context math without calling the CLI (no dashboard is opened)
- **`/circle`** — the in-session front door to `hive loop`: type `/circle` and Claude preflights the `claude` CLI, scaffolds/confirms `.hive-loop/`, dry-runs, then drives the autonomous loop and reports the result. Headless agent runs with `permission_mode = "acceptEdits"` by default. Shipped as a **Claude Code plugin** (`commands/circle.md` slash command + `skills/circle/` skill, declared in `.claude-plugin/`) so it works in any project once installed — see below.
- **`/mindhive <idea>`** — the MindHive Orchestrator: turns an idea into finished, quality-checked work through a four-phase pipeline — recall-informed interview (one question at a time, recalling from the ledger before it asks), a scored skill **Recommendation Matrix** (suitability, compatibility, success probability, risk, speed, cost, verdict), execution through the chosen skill(s) with proactive follow-up chaining (debug / security / quality), and learnings appended back to the ledger. Offers a context **Handoff** at ~50%. Shipped in the same plugin (`commands/mindhive.md` + `skills/mindhive-orchestrator/`).
- **`/engineering-loop`** — the primary autonomous engineering foundation: fuses `/circle`'s bounded headless loop with the compound-engineering `lfg` pipeline into a memory-driven **plan → work → review → fix → verify → learn** loop. It's more detailed than `lfg` because MindHive memory sits between every stage (recall before, append after), it drives the chosen skills through the invoke channel with green/red status, and any skill failure feeds the improvement loop as a `skill:<name>` prevention rule. Shipped in the same plugin (`skills/engineering-loop/`).

## Install the `/circle` plugin (global)
This repo is a self-contained Claude Code plugin marketplace. To get `/circle` in **every** project:

```
/plugin marketplace add hanifridho62-rgb/Lintester-project
/plugin install mindhive@mindhive-dev
```

(or `/plugin marketplace add C:\Users\hanif\mindhive` to install from the local checkout). Then start a new session and type `/circle`. `hive loop` itself still needs the `mindhive` package importable (the plugin shells out to `python -m mindhive loop`) and the `claude` CLI on PATH.

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

## PowerShell 5.1 note
Inline JSON args are mangled by PowerShell 5.1. To set state from PS, pipe via stdin:

```powershell
'{"task":"OFI verdict","next_step":"recapture 24h"}' | hive append state_set --json --stdin
```

(Claude in-session uses the Bash tool, where normal quoting works.)

## V17 safety
`embed` and `recall --semantic` refuse to run while `~/.mindhive/LIVE_LOCK`
exists (V17 is trading). Override with `--force` only when the box is idle.

## Usage-cap fallback
When the Opus window is nearly spent, run `hive export` and paste handoff.md into
any model/client. It is deterministic and costs no API tokens.

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
`MINDHIVE_AUTOHARVEST_SECS` (default 180). Trigger it by hand with the **Catch
up** button, the `POST /api/harvest` endpoint, or `hive harvest --catch-up`.

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
- **Sessions** — each append stream (`session_id`); click one to scope Memory to it.
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
