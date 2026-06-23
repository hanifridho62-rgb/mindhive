# Autosync capture-everything — design

Date: 2026-06-23
Status: awaiting approval

## Goal

Make MIND HIVE capture happen **automatically, with no manual trigger**, for
every project, in every environment — including the Claude Agent SDK / app
harness where user-defined Claude Code hooks never fire. The user should never
have to run `hive harvest` for their work to land in memory. (Registration is
no longer needed for capture; it remains a one-time step only for a project that
also wants an external desktop-auto-load `MEMORY.md`.)

## What "memory" means here (locked)

Capture stays exactly the **existing** event types — no new ones:

- `file_touched` / `command_run` (tool actions, from transcript tool_use)
- marker memos (`decision:` / `caveat:` / `memo:` / `remember:`) from user messages

Explicit non-goals (YAGNI): no raw prompt-text capture, no assistant-response
capture, no new event types, no new hooks.

## Root cause (why it doesn't happen today)

Two independent gaps:

1. **Live hooks don't fire in the app/SDK harness.** `post_tool_use.ps1`
   (PostToolUse) is the real-time path, but this harness does not execute user
   hooks, so nothing is appended live.
2. **The background task only covers registered projects.** The installed
   `\MindHive-AutoSync` scheduled task runs `mindhive autosync`
   (`autosync.run_all`), which iterates `~/.mindhive/autosync.json`. That
   registry currently contains **only `fabio-hft-v18`**, so the `mindhive`
   project (and every other project) is never harvested by the task.

Net effect: the only universally-available trigger-free mechanism — the
background task — was scoped to one project.

## Hard constraint

True per-prompt, instant capture requires a hook, and hooks don't run in the app
harness. The achievable behavior is **automatic, trigger-free, near-real-time**
capture via the background task on a short interval (seconds-to-minutes), not
literally per-keystroke.

## Design

### 1. Rewrite `autosync.run_all()` to auto-discover everything

New pipeline (replaces the registry-iteration loop):

1. `harvest.catch_up(skip_captured=True, known_only=False)` — scan **all**
   Claude transcript directories and harvest every session into the project its
   `cwd` maps to (`paths.detect_project`, which collapses git worktrees onto
   their main repo namespace). `skip_captured=True` preserves the
   no-double-count invariant alongside live hooks; `known_only=False` lets brand
   new projects be discovered without prior registration.
   - Scope decision (locked): capture **literally every session**, regardless of
     git status. Incidental sessions (e.g. `claude` launched from the home
     folder) will create their own namespace; that is accepted.
2. For every project that had **new events** this run
   (`catch_up` returns them): `projector.project(p)` + `graph.build(p,
   semantic=False)` to keep STATE / LEDGER / index / chain / graph current.
3. **Native `MEMORY.md` sync — registered projects only.** Run `sync.sync(folder,
   project=p)` for each project that is **in the registry** (`autosync.json`)
   and had new events this run. This keeps `fabio-hft-v18`'s existing sync and
   adds `mindhive` (registered once during rollout). Auto-discovered /
   unregistered projects get **no external `MEMORY.md`** — they are fully
   captured in the `~/.mindhive` store and visible in the dashboard / HIVE MIND
   view, but nothing is written into their folders.

### 2. Where the mindhive `MEMORY.md` goes

mindhive's native file is written to its desktop auto-load path via the existing
`sync.native_memory_path`:

```
~/.claude/projects/C--Users-hanif-mindhive/memory/MEMORY.md
```

derived from `folder = C:\Users\hanif\mindhive`. mindhive is added to the
registry once during rollout (folder = the mindhive repo root) so the native
sync step has its folder mapping — a one-time setup step, not per-session.

### 3. Registry's new role

`autosync.json` is no longer required for **capture** (capture auto-discovers
every project). It is now purely the **native-sync allow-list**: only projects
listed there get an external `MEMORY.md`. After rollout it holds
`fabio-hft-v18` (unchanged) and `mindhive` (added once).

### 4. Scheduled task retune (operational, applied separately)

Reconfigure `\MindHive-AutoSync` via `schtasks`:

- Repeat every **2 minutes** (was 10).
- **Allow running on battery** (currently "No Start On Batteries" / "Stop On
  Battery Mode").

Applied as an explicit, confirmed step at rollout — not part of the Python
change.

## Data flow

```
Claude session (any harness) ──writes──▶ ~/.claude/projects/<enc>/<uuid>.jsonl
                                              │
        \MindHive-AutoSync (every 2 min) ─────┘
                │  mindhive autosync → autosync.run_all()
                ▼
   harvest.catch_up(skip_captured=True, known_only=False)
                │  (append file_touched / command_run / memo events)
                ▼
   ~/.mindhive/<project>/events/<session>.jsonl   ← every project
                │
   projector.project(p) + graph.build(p)          ← every project w/ new events
                │
   sync.sync(folder, p) for p in registry          ← registered only (fabio, mindhive)
                ▼
   ~/.claude/projects/<enc(folder)>/memory/MEMORY.md
```

## Error handling

- Per-project isolation: a failure harvesting/projecting one project must not
  abort the rest (existing `run_all` try/except per entry, preserved per
  project).
- `catch_up` already swallows unreadable transcript lines/dirs.
- Native sync failure for mindhive is logged in the result, not fatal.
- A corrupt event log surfaces as a clean error (already handled at the CLI
  boundary); autosync records it per-project and continues.

## Testing (TDD)

- `run_all` discovers and harvests an **unregistered** project end-to-end
  (creates its namespace, appends events).
- `run_all` honors `skip_captured` (a hook-captured session is not re-harvested).
- `run_all` rebuilds projections/graph for every project with new events.
- `run_all` native-syncs **only registered** projects — assert an unregistered
  auto-discovered project gets no `MEMORY.md` write (spy on `sync.sync`).
- `run_all` native-syncs each registered project that had new events (fabio +
  mindhive style), using its registry folder.
- Per-project error isolation: one failing project doesn't abort the others.

Tests stub the heavy stages (project/graph/sync) and assert orchestration, plus
one end-to-end test using a fake transcript tree and `MINDHIVE_HOME`.

## Rollout

1. Land the `autosync.run_all()` change + tests (green suite).
2. Register mindhive once: `hive autosync register --project mindhive
   --transcripts <~/.claude/projects/C--Users-hanif-mindhive> --folder
   C:\Users\hanif\mindhive` (so it joins fabio on the native-sync allow-list).
3. Retune the scheduled task (`schtasks`) — confirmed step.
4. Verify: run `mindhive autosync` once; confirm this `mindhive` session's
   events appear in `~/.mindhive/mindhive/events/`, a fresh `MEMORY.md` is
   written for the registered projects (mindhive, fabio), and an unregistered
   project is captured in the store but has no `MEMORY.md`.
