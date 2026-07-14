# Session Legend — autonomous close-out of PR #49 (Self-Reliant)

This is the handoff map for scheduled, no-human-input sessions. Read it first;
it tells you what this project is, where the work stands, exactly what to do,
and how to budget the session. **The mission ends when PR #49 is green and
merged into master** — after that, disable/delete the recurring schedule and
stop.

## Final goal

- **PR:** https://github.com/hanifridho62-rgb/Lintester-project/pull/49
  (`claude/phase-2-roadmap-1cfa15` → `master`)
- **Done means:** all 4 CI checks green (ubuntu/windows × py3.11/3.12), no
  unresolved review comments, no merge conflicts, PR merged (this repo uses
  plain merge commits — see `git log` on master), branch deleted, and the
  recurring schedule removed.

## What this project is (60-second map)

**MindHive** is a deterministic, event-sourced memory system for Claude Code.
Append-only events (`src/mindhive/events.py`) flow into projections: recall,
graph, briefing, incidents → reflection → prevention rules ("Improvements"),
and a local dashboard (`src/mindhive/dashboard.py`, fixed port 54461).

Key surfaces:
- **Dashboard** — `dashboard.py` (HTTP routes) + `dashboard_page.py` (shell UI);
  full-page iframe views: `workspace_page.py` (MindParallel), `swarm_page.py`
  (MindSwarm), `research_page.py` (Self-Reliant, NEW in this PR).
- **Workspace** — `workspace.py`: fleet of headless `claude` subprocess agents
  (Manager/Agent), account profiles via `CLAUDE_CONFIG_DIR`, recipes, kanban.
- **Self-Reliant** (this PR) — `research.py`: autonomous autoresearch loop.
  Researcher agent edits `train.py`; Python enforces a 5-min training watchdog,
  leaderboard, terminators, and memory write-back. WSL bring-up (U1) is
  manual-gated: `scripts/self_reliant/bring_up.sh` — **never run it yourself**.
- **Plan executed:** `docs/plans/2026-07-12-001-feat-self-reliant-autoresearch-plan.md`
  (U2–U11 shipped; read it for design rationale KTD1–KTD11).

## Session playbook (run this every session, in order)

1. `gh pr view 49 --json state,mergeable,reviewDecision,statusCheckRollup`
   - **MERGED** → delete the recurring schedule, append a `decision` to the
     hive ledger, stop. Mission complete.
   - **CLOSED (not merged)** → stop and leave a note for the human; do not
     reopen or force anything.
2. **CI failing?** Check out `claude/phase-2-roadmap-1cfa15` in a worktree,
   reproduce with `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests -q`
   (from the worktree; bare `python` is not on PATH). Fix root-cause (use
   systematic-debugging), commit, push. Likely weak spots: timing-sensitive
   controller tests on slow CI runners (`tests/test_research.py` — widen
   waits/timeouts rather than deleting assertions), POSIX vs Windows process
   semantics in `run_experiment`/`_kill_tree`.
3. **CI pending?** Don't burn the session waiting. If nothing else is
   actionable, end the session early — the next scheduled run will see the
   result.
4. **Review comments?** Address each root-cause (ce-resolve-pr-feedback shape:
   assess validity, fix, reply), commit, push.
5. **Merge conflicts with master?** Merge master into the branch, resolve,
   run the full suite, push.
6. **All green + no comments + mergeable?** Merge:
   `gh pr merge 49 --merge --delete-branch`. Then do step 1's MERGED actions.
7. Whatever you did, record it: `python -m mindhive append decision|caveat "<one line>" --tags self-reliant, pr-49`
   (use the repo venv python).

## Usage budget (estimate + rule)

The building session that produced this PR (ported Phase 1, built the
controller + terminator matrix + UI + routes, 62 tests, browser verification)
consumed roughly one full 5-hour usage window on Fable 5 — treat that as the
ceiling of what one session can build. Scheduled close-out sessions are much
cheaper; budget rule:

- **CI-fix iteration:** ~5–15% of a window each — you can afford several.
- **Review-response round:** ~10–20%.
- **Full-suite local run:** cheap in tokens, ~2 min wall-clock.
- **Decision rule:** at session start, pick the single smallest action that
  advances PR #49 (steps above are already ordered). Do not start new feature
  work in these sessions; if the PR is blocked on something only the human can
  do (e.g. a review), say so in the session output and end early. Ending early
  is success, not failure — the 5-hour cadence exists because usage refreshes
  every 5 hours.

## Hazards (learned the hard way — do not repeat)

- The **live dashboard** at http://127.0.0.1:54461 belongs to the human. Never
  kill or restart it. If you must serve a dashboard, use another port from a
  worktree and kill only that PID.
- **Never run `scripts/self_reliant/bring_up.sh`** — it downloads gigabytes
  and is gated on explicit human intent (plan KTD9).
- Work in a **git worktree**, never directly on master.
- Read-gate: **Read every file before you Edit it** in the same session.
- `python` is not on PATH on this machine — use
  `C:/Users/hanif/mindhive/.venv/Scripts/python.exe`.
