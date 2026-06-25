# MIND HIVE

[![CI](https://github.com/hanifridho62-rgb/Lintester-project/actions/workflows/ci.yml/badge.svg)](https://github.com/hanifridho62-rgb/Lintester-project/actions/workflows/ci.yml)

Deterministic, event-sourced memory for long-running Claude sessions.
Append events; deterministic projections rebuild everything. Nothing is hand-edited.

## Commands
- `hive append <type> <payload> [--json] [--stdin] [--tags a,b]` — add an event
- `hive project` — rebuild STATE.md + LEDGER/ + index.json + chain.json
- `hive state` — print the live STATE.md
- `hive recall "<query>" [--semantic]` — grep (default) or vector search
- `hive graph export [--out path] [--semantic] [--open]` — write a standalone, offline graph.html
- `hive dashboard [--port N] [--no-browser]` — open the web control panel for every feature
- `hive plugins scan [--cache dir] [--top N]` — catalog free (no API key / no pay) skills & plugins, score by breadth, store a `plugin_catalog` snapshot, print the top N
- `hive plugins autoscan [--max-age-hours H] [--top N] [--force]` — scan **only when the catalog is missing or stale** (default 24h); a near-instant no-op otherwise, so it's safe to run every session
- `hive plugins recommend` — print the top recommendations from the latest snapshot
- `hive review queue [--session id]` — queue the session's touched source files for a thermo-nuclear review (run by `session_end`; writes the pending-review marker)
- `hive review show` — print the pending-review marker, if any
- `hive review clear` — clear the pending-review marker once the review has run
- `hive export [--out path]` — write a portable handoff.md (zero API tokens)
- `hive verify` — check the hash chain
- `hive embed [--force]` — build the on-demand vector index
- `hive selftest` — run the test suite
- `hive loop init` — scaffold `.hive-loop/` (PROMPT.md, fix_plan.md, AGENT.md, loop.toml) and check the `claude` CLI
- `hive loop` — run an autonomous Claude Code loop in the current project until the task is done; memory-seeded each iteration, with real token/cost budget gating, a context smart-zone reset at half the window, a dual-condition exit gate, and a circuit breaker
- `hive loop status` — summarize the last loop run
- `hive loop --dry-run` — show prompts + projected budget/context math without calling the CLI
- **`/circle`** — the in-session front door to `hive loop`: type `/circle` and Claude preflights the `claude` CLI, scaffolds/confirms `.hive-loop/`, dry-runs, then drives the autonomous loop and reports the result. Headless agent runs with `permission_mode = "acceptEdits"` by default. Shipped as **both** a slash command (`.claude/commands/circle.md`, what you type) and an auto-invoked skill (`.claude/skills/circle/`). Start a **new** Claude session in this folder to load it.

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
- **Auto (panel):** opening the dashboard **Plugins** view calls the same
  stale-gated autoscan, so just looking at it keeps it fresh.
- **Manual (force):** `hive plugins scan` or the dashboard **Plugins scan**
  (Heavy) button force a full rescan anytime, in any session.

Because every session shares one event store, a refresh by one session is
immediately visible to all the others.

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
recall, append memories, project, sync, verify, export, embed, and plugins
scan. The graph is one of its views.

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
