# feat: per-session roadmap auto-sync (TodoWrite → roadmap events)

**Date:** 2026-07-18 · **Branch:** `claude/roadmap-not-working-a13dfa`

## Problem

The Oversee roadmap panel (2026-07-17-001) works, but nothing feeds it for
interactive sessions: the only writers are the manual `hive roadmap` CLI
(nobody runs it) and the loop engine's out-of-usage snapshot (loop runs only).
Verified live: `hive roadmap list` → "(no roadmaps)". Every chat-session card
falls back to the synthesized "live view — no declared roadmap", which is
nearly empty (0/1 steps) because sessions rarely record `decision` /
`next_step` / `file_touched` memos.

User ask: declare a roadmap **automatically per session** and keep it in sync
with the actual progress of the work.

## Design

The session's own TodoWrite telemetry IS its plan + live progress. Pipe it
through the existing PostToolUse capture into auto-managed `roadmap` events
(deterministic hook, not prompt prose — SOP agentic-loops §2), and close the
plan out at session end.

- **plan_id = session_id** — the dashboard already attaches a plan to its
  session card by that equality; zero dashboard changes.
- **Status mapping:** completed→done, in_progress→active, pending→pending.
- **Upsert idiom:** every material change appends one `roadmap set` (fold
  replaces steps wholesale, bumps version, revives interrupted plans). No
  diffing into `step` events — simpler, and the history stays honest.
- **`auto` tag as ownership marker:** auto-sync only ever touches plans whose
  latest event carries the `auto` tag. A human-declared plan for the same id
  is never clobbered.

## Requirements

- R1 `roadmap.sync_from_todos(session_id, todos, ...)`: no-op unless
  session_id matches `ID_RE` and todos map to ≥1 valid step; prompt = headline
  of the session's first `user_prompt` event (fallback `session <sid[:12]>`);
  skip append when steps AND prompt are unchanged; steps/prompt/title
  truncated to the module caps so an oversized todo can never invalidate the
  sync. Tags `["roadmap", "auto"]`.
- R2 `hookcapture.record_post_tool_use`: a `TodoWrite` branch calls R1
  best-effort — a sync failure logs to `hooks_errors.log`, never breaks
  capture. `extract_events` stays untouched (pure contract; harvest mirrors
  it and must NOT replay TodoWrite — transcript replay can only reorder the
  registry, never improve it).
- R3 `roadmap.autoclose_session(session_id, ...)` + `hive roadmap autoclose
  --session <sid>` run by `hooks/session_end.ps1` before projections rebuild:
  auto-owned, non-completed plan → `complete` when every step is
  done/skipped, else `interrupt` (cause `session_end`) so the existing
  STATE.md marker preserves the unfinished plan for the next session.
- R4 Out-of-repo, single key: extend the PostToolUse matcher in
  `~/.claude/settings.json` with `|TodoWrite` (same discipline as the
  `|Skill` extension in 2026-07-05-001). Safe pre-merge: current master's
  extract_events ignores unknown tools.
- R5 Tests: sync declare/update/no-op/manual-guard/bad-sid/truncation/revive;
  autoclose complete/interrupt/no-op paths; hookcapture TodoWrite branch
  (append, count, failure-logged, extract_events still returns []).

## Out of scope

Harvest back-fill of TodoWrite; cross-session plan re-attachment; loop-run
todos (the engine has its own snapshot); staleness aging of orphan plans.
