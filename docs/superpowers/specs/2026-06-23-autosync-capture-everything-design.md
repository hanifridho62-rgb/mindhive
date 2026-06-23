# Autosync capture-everything — design

Date: 2026-06-23
Status: awaiting approval

## Goal

Make MIND HIVE capture happen **automatically, with no manual trigger**, for
every project, in every environment — including the Claude Agent SDK / app
harness where user-defined Claude Code hooks never fire. The user should never
have to run `hive harvest` or register a project for their work to land in
memory.

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
3. **Native `MEMORY.md` sync — mindhive only.** Run `sync.sync(folder,
   project="mindhive")` *only* for the `mindhive` project, and only when it had
   new events this run. No other project gets an external `MEMORY.md`; they are
   fully captured in the `~/.mindhive` store and visible in the dashboard /
   HIVE MIND view, but nothing is written into their folders.

### 2. Where the mindhive `MEMORY.md` goes

The single native file is written to mindhive's desktop auto-load path via the
existing `sync.native_memory_path`:

```
~/.claude/projects/C--Users-hanif-mindhive/memory/MEMORY.md
```

derived from `folder = C:\Users\hanif\mindhive`. The mindhive folder is resolved
automatically (registry entry if present, else derived from the git-root `cwd`
of a mindhive transcript) — **no manual registration required**.

### 3. Registry's new role

`autosync.json` is no longer required for capture. It is retained only as an
optional folder-mapping/override source. The capture path ignores it; the native
sync path uses it (or the derived folder) solely to locate mindhive's folder.

> Open item for spec review: `fabio-hft-v18` currently gets a native `MEMORY.md`
> via its registry entry. Under this design, native sync is confined to
> `mindhive`, so fabio would no longer receive a `MEMORY.md` (it remains fully
> captured in the store). Confirm this is intended.

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
   sync.sync(mindhive_folder, "mindhive")          ← mindhive ONLY
                ▼
   ~/.claude/projects/C--Users-hanif-mindhive/memory/MEMORY.md
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
- `run_all` native-syncs **only** mindhive — assert other projects get no
  `MEMORY.md` write (spy on `sync.sync`).
- `run_all` resolves the mindhive folder without a manual registry entry.
- Per-project error isolation: one failing project doesn't abort the others.

Tests stub the heavy stages (project/graph/sync) and assert orchestration, plus
one end-to-end test using a fake transcript tree and `MINDHIVE_HOME`.

## Rollout

1. Land the `autosync.run_all()` change + tests (green suite).
2. Retune the scheduled task (`schtasks`) — confirmed step.
3. Verify: run `mindhive autosync` once; confirm this `mindhive` session's
   events appear in `~/.mindhive/mindhive/events/` and a fresh `MEMORY.md` is
   written for mindhive only.
