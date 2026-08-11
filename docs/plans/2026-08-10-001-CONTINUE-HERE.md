# CONTINUE HERE — PR-1, fresh session brief

**Written:** 2026-08-10, at the end of a session that hit 700k context and then a hard session
limit (`resets 8:20pm Asia/Jakarta`).
**Branch:** `claude/mindhive-dashboard-perf-eab7dd` · **Base:** `master` @ `efa3751`
**Worktree:** `C:\Users\hanif\mindhive\.claude\worktrees\mindhive-dashboard-perf-eab7dd`
**The plan:** [2026-08-10-001-fix-feedback-loop-five-surfaces-plan.md](2026-08-10-001-fix-feedback-loop-five-surfaces-plan.md)
(1041 lines, 31 tasks, 52 decisions, three plan reviews + two outside-voice passes)

Read the plan. Do not re-litigate its decisions. This file is only what a fresh session needs
that the plan does not already say.

---

## 1. Exact state of the tree

```
a3d7606  docs: PR-1 plan, three review reports, and the T1 profile
99dd244  feat(dashboard): serialise both live pollers so a slow tick is skipped, not queued
```

**Both commits landed.** Working tree clean, 2 commits ahead of `efa3751`. §7 below is
therefore already done — do not redo it.

**This work lives ONLY in this worktree**, on `claude/mindhive-dashboard-perf-eab7dd`.
Neither commit exists on `master` or in any other worktree. A "fresh session in this
worktree" means literally this path — opening one elsewhere gets `efa3751` and none of it.

## 2. Done and verified

**T1 — the profile.** Recorded in the plan under `## T1 RESULT`. This is the single most
valuable artifact of the session; it is what reordered the whole PR.

**T7 — pollers serialized.** `refreshLive` and `pollCats` each raise an in-flight flag before
their first `await` and clear it in `finally`.
- Full suite green: **4511 passed**.
- Test `test_both_pollers_skip_a_tick_instead_of_queueing` — **canary-checked**: it fails on
  `efa3751` and passes here, so it discriminates.
- Behavioural proof taken by hand in a real browser: `flag_while_in_flight=true`,
  `first_tick_ms=715`, `second_tick_ms=0`, `flag_after_settle=false`.
- Change-logged: `hive log update` with explicit `--files` (2 files, no over-claim).

## 3. NOT done

24 tasks remain. **Resequenced by `/plan-ceo-review` on 2026-08-10 (HOLD SCOPE — nothing
added, nothing removed, order only).** Full reasoning: `## PR-1 resequencing` in the plan.

```
PR-1a  T8   server locks on six mutating endpoints   MUST land before T9
       T6   gate /api/mindnet/cats                   biggest lever after T7
       T9   liveFn === discard + drop nav disable    AFTER T6, not before
       T11  node unit test of the discard decision   moved up from PR-2
       T31  reduced-motion + aria-live + aria-busy   moved up from PR-1c
       -> closes complaint P1 end to end, and yields the before/after number

PR-1b  T5   segment_stats memo                       two guards mandatory
       T2   drain _body before raising
       T3   _responded flag on both blanket excepts
       T4   protocol_version HTTP/1.1 + timeout=30   only after T2 and T3

PR-1c  T26  extract 123 hex literals to CSS tokens
       T28  three skeleton shapes at 150ms
       T10  3-strike stale chip
```

Why T6 before T9: T9 makes the nav clickable during a fetch, which raises concurrent
requests, and T1 measured the folds as GIL-serialized (4 concurrent 4.99s vs 5.3s serial).
Relieve the queue first, then open the nav.

Why T11 rides with T9: the plan's failure-modes table marks the `liveFn ===` discard
**silent** and requires tier 1 + tier 2. Without T11, T9's mandatory regression test falls
back to the source-string style the plan itself calls vacuous (trap 7 below).

**Lane A is serial** — PR-1a, PR-1b, PR-1c and PR-2 all own `dashboard.py` and
`dashboard_page.py`. Do not run them concurrently.

**PR-3, PR-4, PR-5 are unblocked and can start NOW, in parallel worktrees** — they share no
module with lane A or with each other. Each is small and each closes a whole complaint:
PR-3 = T17,T18 (P2 roadmap card) · PR-4 = T19,T25 (P3 rotation) · PR-5 = T20..T24 (P5 SOP
proof). PR-2 (T13-T16, T12) closes P4 but must wait for lane A.

## 4. Measured facts — do NOT re-derive these

Taken 2026-08-10 on the live 31k-event store. Re-deriving them costs ~20 minutes and they will
not change.

```
time_connect                     0.0009s on every request, both servers
section endpoints, idle          1.2 - 1.9s   (/api/projects 1.76-1.86s)
/api/mindnet/cats, idle          0.87 - 0.92s, returns 503 bytes
same endpoints, real browser     6.0 - 12.4s  -> amplification 6.1-6.7x
4 concurrent vs 4 serial         4.99s vs 5.3s  -> the GIL serialises the folds
connection churn, live server    65 TIME_WAIT vs 2 ESTABLISHED (a keep-alive
                                 origin on this machine shows 24 vs 2)
store namespaces                 56 (dirs containing events/), NOT 80
contrast #9aa3b8 on #171a23      7.17:1  passes AA
contrast #6b7488 on #171a23      3.87:1  FAILS AA - legacy debt, TODOS.md item 7
contrast #e0b341 on #171a23      9.24:1  passes AA
hex literals in dashboard_page   123, zero CSS custom properties
top-level hive subcommands       39 (add_parser total: 120)
```

**The conclusion those numbers force:** HTTP/1.1 is **correctness** work (idle-connection thread
leak, keep-alive desync, double responses). It is **not** a latency fix — 0.9ms of 12,400ms.
T6 and T7 are the latency levers because they remove the queue everything else waits behind.

## 5. Traps — each of these cost real time this session

1. **The venv serves master, not this worktree.** `mindhive` is installed editable against
   `C:\Users\hanif\mindhive`. A default import resolves to
   `C:\Users\hanif\mindhive\src\mindhive\__init__.py` even when cwd is the worktree. Any preview
   server started without `PYTHONPATH=<worktree>/src` **serves master and measures the wrong
   branch**. My first profile did exactly this and was silently measuring master.
   CLAUDE.md forbids this override on the LIVE 54461 process; a separate port from the worktree
   is the sanctioned path and does need it.

2. **The in-app Browser pane reports `document.hidden = true`.** Both pollers early-return on it
   (`dashboard_page.py:251` and the same check in `pollCats`), so **neither poller runs in the
   pane**. It looks exactly like a wedged poller when nothing is wrong. Override with
   `Object.defineProperty(document, 'hidden', {configurable:true, get:()=>false})` and `delete
   document.hidden` to restore. **T12 (Playwright) must handle this or it will test nothing.**

3. **`hive log` over-claims files** when `MINDHIVE_SESSION` is unset — it falls back to a stale
   touched-files marker. Always pass `--files` explicitly.

4. **pytest node-ids need Windows or relative paths.** `/c/Users/...::test_name` from Git Bash
   gives "file or directory not found". Use `cd <repo> && ... pytest tests/x.py::test_y`.

5. **`python` is not on PATH in Git Bash** — it hits the WindowsApps stub and dies. Use
   `/c/Users/hanif/mindhive/.venv/Scripts/python.exe`.

6. **Bash heredocs with long markdown fail** in this harness ("unexpected EOF"). Write the
   content with the Write tool to a scratchpad file and `cat` it into place.

7. **The existing "race tests" prove nothing.** `tests/test_dashboard.py:137` and `:149` assert
   literal substrings of the served page; no JavaScript ever executes. Any new test in this file
   must be canary-checked against `efa3751` or it is the vacuous fixture the ledger already
   warns about.

## 6. What to run first in the fresh session

```bash
cd C:\Users\hanif\mindhive\.claude\worktrees\mindhive-dashboard-perf-eab7dd
```

**Do not trust MEMORY.md auto-load to carry this handoff.** Measured at the end of the session:

```
C--Users-hanif-mindhive\memory\MEMORY.md                      225 lines, 16:02, STALE
  -> zero mentions of this session's work (grep for "poller|PR-1|12.4s|efa3751" = 0)
C--...-worktrees-mindhive-dashboard-perf-eab7dd\...\MEMORY.md  207 lines, 20:57, fresh
```

The harness keys its memory directory off the **cwd**, so the worktree has its own, and
`hive sync` from the worktree writes only to that one. Whichever you open, **both are over the
harness's 200-line limit and will be silently truncated at load** — the exact defect T22/D10
records, biting the handoff itself. So:

- Open the fresh session **in this worktree** if you want the synced memory, and
- **explicitly point it at this file and the plan** either way. They are ordinary repo files and
  are not subject to the auto-load truncation.
- A portable, token-free copy is at `C:\Users\hanif\.mindhive\mindhive\handoff.md` (719 lines,
  written by `hive export` at the end of the session).

1. `hive state` and `hive briefing` — standing SOP, orient from the ledger first.
2. Read the plan, then this file.
3. ~~Commit what exists first~~ — **already done**, see §7. Do not redo it.
4. Then PR-1a: **T8 → T6 → T9, plus T11 and T31**, in that order (see §3). T8 first because
   T9 cannot ship safely without it; T6 before T9 because T9 raises concurrency.
5. Consider opening PR-3, PR-4 or PR-5 in a separate worktree in parallel — each is 2-5
   tasks, each closes a whole complaint, and none shares a module with lane A.

**Do not** re-run `/plan-devex-review`, `/plan-eng-review`, `/plan-design-review`, or
`/plan-ceo-review`. All four are logged CLEAR/complete within the 7-day window and their
reports are in the plan.

**Subagents and workflows were unavailable** at the end of this session (hard session limit).
If they are available again, the failed workflow script is at
`~/.claude/projects/.../workflows/scripts/mindhive-pr1-complete-wf_df6391e7-fc3.js` and can be
re-invoked with `Workflow({scriptPath: ..., resumeFromRunId: 'wf_df6391e7-fc3'})` — zero agents
completed, so nothing replays from cache; it starts fresh. Its two lanes have strict file
ownership (`dashboard.py` vs `dashboard_page.py`) and separate new test files, which is what
makes it safe to parallelise.

## 7. The first commit — DONE 2026-08-10

Both change-sets are committed. Kept here for history; there is nothing to do in this
section. **Verified 2026-08-10: no upstream, no remote ref, no PR — this branch has no
copy anywhere but this disk.**

```
1. src/mindhive/dashboard_page.py + tests/test_dashboard.py
   feat(dashboard): serialise both live pollers so a slow tick is skipped, not queued
   Already change-logged in the ledger. Suite green at 4511.

2. docs/plans/2026-08-10-001-*.md + TODOS.md
   docs: PR-1 plan, three review reports, and the T1 profile
   No src/ or tests/ touched -> no hive log entry required for this one.
```

Landed as two commits — `99dd244` (behaviour) and `a3d7606` (planning).

## 8. Honest gaps

- **No end-to-end latency number yet.** T7 alone only reduces request count when a response
  exceeds the 2500ms interval, which does not happen on an idle preview server. The before/after
  belongs after T6 lands, measured under the conditions in the plan's
  `### Verification conditions for the "after" measurement`.
- **T7's behavioural proof was taken by hand, not by CI.** T11 (node unit test) and T12
  (Playwright) are what make it repeatable. Neither exists.
- **The design review's mockups were hand-built**, not generated — the gstack designer is
  installed but has no OpenAI key. The reference board is at
  `~/.gstack/projects/hanifridho62-rgb-Lintester-project/designs/status-vocabulary-20260810/board.html`.
