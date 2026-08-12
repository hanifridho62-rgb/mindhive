# Fix the feedback loop across five MIND HIVE surfaces

**Date:** 2026-08-10 · **Base:** `master` @ `efa3751` · **Live store:** 31,291 events
**Review:** `/plan-devex-review` — mode DX POLISH · persona: Hanif + Claude
**Decisions:** 12 review + 7 cross-model = 19, all resolved. Outside voice: Claude subagent (Codex not installed).

## The one problem

Five separately-reported complaints share one root cause: **MIND HIVE does not tell you
what it is doing, or whether it worked.**

| # | Reported as | Actually |
|---|---|---|
| P1 | dashboard slow | seconds with zero feedback, in a window where the standard mandates a skeleton |
| P2 | roadmap card never leaves | no checkable link from a plan to the code it produced |
| P3 | rotation never fires | feature never armed in a plain `claude` session, silently |
| P4 | three buttons feel dead | honest-zero indistinguishable from failure; 4-minute job with no progress |
| P5 | SOP injection was off | 64 commits stale, silently; then degraded procedure→pointer, silently |

Every one is a feedback-loop failure, not a feature failure.

## Corrections to the source prompts and to this review's first draft

Recorded because acting on an unverified premise is how three of these bugs were built.

1. **`/api/projects` citation.** The prompt cited `dashboard.py:398`; line 398 is inside
   `_get_overview`. The real path is
   [`_get_projects` at dashboard.py:361](../../src/mindhive/dashboard.py:361) →
   `_project_stats` → `events.read_all`, once per namespace.
2. **Namespace count — this review's own error.** The first draft claimed "80 directories,
   not ~70". `~/.mindhive` has 80 *entries*, but `_all_projects()`
   ([dashboard.py:100](../../src/mindhive/dashboard.py:100)) filters to directories containing
   an `events/` dir: **56**. The prompt's "~70" was closer than the correction. Caught by the
   outside voice.
3. **The race repro — twice wrong.** The prompt's recipe (click section A, then B) cannot fail
   on `efa3751`, because `setBusy` disables every nav button
   ([dashboard_page.py:263](../../src/mindhive/dashboard_page.py:263), `:220`, `:342`). This
   review then concluded the reachable path was the project picker — also too narrow.
   `refreshLive` calls `liveFn()` **directly** ([:253](../../src/mindhive/dashboard_page.py:253)),
   bypassing `run()` and `setBusy`. The real, every-2.5-seconds path is: poller starts
   `renderOversee`; nav is still clickable; user clicks Overview; `run()` fetches; if
   `/api/overview` returns first, the stale poller's `showHTML()` lands last and paints Oversee
   under a "ready" status. **A generation guard stamped in `run()` would not catch this** —
   poller renders never pass through `run()`.
4. **The 4-second premise is unprofiled.** Measured server-side component costs on this store:
   `_all_projects()` 0.004s · `default_project()` 0.590s · `read_all("mindhive")` 0.264s ·
   `chain.verify` 0.280s · all `_project_stats` 0.585s. `/api/projects` ≈ 1.2s CPU,
   `/api/overview` ≈ 1.1s — not 4.0–4.5s. **~3 seconds are unexplained by anything the original
   W1 proposed to fix.**
5. **The MEMORY.md 200-line limit is a Claude Code harness constant, not MIND HIVE's.**
   Grepping `src/`, `tests/`, `docs/` finds it nowhere. The first draft called it "its own
   limit" — wrong, and hardcoding `200` into `sync.py` would rot silently.

---

## Developer Persona Card

```
WHO         Two consumers of one surface:
            (H) Hanif — dashboard 127.0.0.1:54461, hive CLI, 56 store namespaces
            (A) Claude — MEMORY.md auto-load, hook stdin, hive skills invoke
CONTEXT     H: opens the dashboard mid-work to see what Claude is doing
            A: new session, zero context, must orient before the first edit
TOLERANCE   H: ~1s before doubting the button works. 4s reads as "hung"
            A: zero. Context that does not arrive cannot be asked for again
EXPECTS     H: click → change. Action → know the outcome
            A: SOP + STATE + prevention rules arrive whole, or it says they did not
```

## Developer Empathy Narrative (H — dashboard)

> I open `http://127.0.0.1:54461`. Overview paints. I click **Oversee**. The entire nav
> greys out — 21 buttons dead at once. I wait. Four seconds. No spinner in the panel, no
> skeleton, just small `loading oversee` text in the header and nothing clickable. I think
> I misclicked. The card finally appears: `smart-zone-interactive-rotation v14 — 11/12 done
> (active)`. Same card as last week; the work merged. I click **Reflect** to see whether MIND
> HIVE learned anything. A raw browser `confirm()`: "this can take a while. Continue?" OK.
> The header says `Reflect running…`. Nav dies again. Then one line:
> `Reflect: 0 reflected · 0 failed · 0 pending`. The list below is byte-identical. I cannot
> tell whether that succeeded, failed, or the button is broken.

## Developer Empathy Narrative (A — Claude, observed live during this review)

> Session starts. `MEMORY.md` loads. My context receives:
> **"WARNING: MEMORY.md is 203 lines (limit: 200). Only part of it was loaded."**
> Part of the briefing that is my mandatory orientation was cut, silently. Then every prompt
> carries a `MANDATORY SOP` block whose body is not the procedure but
> `(body over the per-prompt budget — read it now: hive sop show execution-discipline)`.
> I do know the SOP — but from `MEMORY.md`, loaded once at session start: precisely the
> delivery mode sopgate's own docstring says decays by turn 80.

**Premise correction to P5.** The SOP text *does* reach the model, by the wrong path, once per
session. And the truncation warning reaches only the party who cannot act on it: by the time
anyone reads it, Claude's context is already cut.

## Competitive DX Benchmark

Classic TTHW does not fit a one-machine tool. The metric this plan moves is **TTF — Time To
Feedback**: action → knowing what happened.

| Benchmark | Number | DX choice | Source |
|---|---|---|---|
| 2026 loading ladder | 0–300ms nothing · 300ms–1s spinner · **1–10s skeleton** · **10s+ progress bar + status text** | time-based ladder, not one spinner | onething.design / thehangline |
| Stale-while-revalidate | instant paint from cache, silent revalidate | the standard answer to slow reads | wearepresta |
| Generic spinner | **2026 anti-pattern** | skeleton mirroring final layout | onething.design |
| Push vs poll | "always prefer pushing over polling" | **calibrated for internet-scale fan-out — does not apply to one user on localhost with two timers already running** | rednafi / marius-schroeder |
| claude-mem | v12.6.4, 259 releases / 7 months | direct comparable in Claude Code memory | augmentcode |
| **MIND HIVE today** | 4.0–4.5s end-to-end (measured by Hanif) vs ~1.2s server CPU (measured in review) — **gap unexplained** | — | requires a profile before optimising |

**Rank: Red Flag** — not for the seconds, but for seconds *with no signal at all*, in the
window where the standard mandates a skeleton.

Human-newcomer TTHW: **unmeasurable** — `README.md` never states the base install command.
Only `pip install -e .[build]` at README.md:254 (builds the .exe) and `/plugin marketplace add`
at README.md:59.

## Magical Moment Specification

**Already built, verified present:** the `online — Claude is working` strip in Oversee, with
the Claude-style checklist — green ✓ done, pulsing ◐ now, grey ○ pending. Delivery vehicle:
the dashboard live view.

**What breaks it:** W1 and W4 both do. The moment costs seconds of frozen nav before it is
visible, then polls every 2.5s — faster than its own response time. The magical moment exists
and is buried. **W1 and W4 are the magical-moment work**, not incidental chores.

## Developer Journey Map

| Stage | Developer does | Friction point | Resolution |
|---|---|---|---|
| 1 Discover | reads README | 50 flat bullets, no Quick Start | W7 |
| 2 Install | `pip install -e .` | **command absent from README** | W7 |
| 3 Hello World (H) | `hive dashboard` | stable port 54461 + persisted token — **good, keep** | ok |
| 3 Hello World (A) | MEMORY.md auto-load | 203-line file truncated at load, silently | W5 |
| 4 Real Usage | clicks a section | seconds, 21 nav buttons frozen, zero skeleton | W1 |
| 5 Debug | clicks Reflect / Self-test | 4 minutes of silence; honest-zero reads as broken | W4 |
| 6 Upgrade | `git pull` on master | fails silently for 64 commits; editable install serves stale code | W5 |

## First-Time Developer Confusion Report

```
T+0:00  Click Oversee. Whole nav greys out. Panel blank.        → W1
T+0:04  Card appears. Same card as last week. 11/12.            → W2
T+0:20  Open its roadmap. Last step: "Live interactive TUI
        session driven to rotation by a human." Never checkable. → W2 + W3
T+0:45  Click Reflect. Raw browser confirm() dialog. OK.        → W4
T+0:50  "Reflect: 0 reflected · 0 failed · 0 pending". List
        unchanged. Success? Failure? Broken? Indistinguishable. → honest-zero invariant
T+1:30  Click Self-test. Nav dies. Four minutes. Not one byte
        of output reaches the browser — all to server stdout.    → W4
T+5:30  Give up guessing, open the server terminal.              → W4
```

---

## What already exists (reuse, do not reinvent)

- **`_SESSIONS_MEMO` / `_sessions_cached`** —
  [dashboard.py:266–305](../../src/mindhive/dashboard.py:266). A fingerprint memo keyed on
  `(len(evs), evs[-1].ts)` that **self-invalidates on any append from any process** — CLI,
  hooks, `hive loop`, other sessions. This is the caching pattern W1 extends. It is strictly
  better than invalidating on dashboard mutations only, because this store is written
  constantly from outside the dashboard process.
- **Generation-guard precedent** — `renderPlugins()` stamps `PLUG_GEN` before fetching and
  discards stale cycles ([dashboard_page.py:1462](../../src/mindhive/dashboard_page.py:1462)).
- **`viewPicked` boot-race fix** — already live (commit `8c40704`, PR #102). Keep it.
- **Single-read optimisation in `_get_overview`** — [dashboard.py:396](../../src/mindhive/dashboard.py:396)
  already reads each project's log once for both stats and the chain check.
- **`hive context --check`** — [__main__.py:2941](../../src/mindhive/__main__.py:2941) already
  has `--budget`, `--check` (exit 1 over budget), `--json`, `--top`, `--origin`. This is the
  context-delivery health check, already built. Extend it; do not build a rival.
- **`hive verify`**, **`hive packs check`**, and the **STATE.md blocking-notice mechanism** —
  the existing health surfaces and the existing delivery vehicle.
- **Append-only tombstone precedent** — `loop_run_dismissed` removes an Oversee card without
  deleting events. W2 and W4 follow it.
- **`verify.run_and_record`** already appends a `verification` event per selftest run. W4 must
  not double-record it.
- **`fail()` client error surface** — [dashboard_page.py:271](../../src/mindhive/dashboard_page.py:271)
  already has a good server-unreachable message. Extend, do not replace.
- **Fail-open + opt-in rotation contract** — `hooks/stop.ps1` + `mindhive.rotation.hook`.
  CLAUDE.md forbids weakening either.

---

## The honest-zero invariant

> Every path that can produce **zero work**, a **degraded result**, or an **abandoned job**
> MUST report which of the three it is, and why. Enforced by test, not convention.

Applies to: Reflect/Resolve/Self-test (W4), rotation hook `return None` (W3), sopgate
procedure→pointer (W5), MEMORY.md truncation (W5), roadmap retirement reason (W2).

**Abandoned state (T-F).** On dashboard start, any job with `job_started` and no
`job_finished` is sealed with an append-only `job_abandoned` carrying the reason. The UI reads
it as "interrupted by a server restart", never as "still running" — otherwise W4 reproduces
P2's never-leaving card on a new surface.

---

## Corrections from the engineering review

Five more claims this review made and then had to withdraw. Recorded for the same reason as
the first set: every one of them would have shipped as written.

6. **The guard does not need new machinery.** This review specified a generation guard stamped
   in `showHTML()`. `showHTML(html)` receives only a string — it cannot know which render sent
   it, so implementing that literally means changing the signature and threading a token through
   a dozen call sites. The file already solves this exact cross-section race **seven times**:
   `if (liveFn === renderPlugins) paintPlugins();`
   ([:1480](../../src/mindhive/dashboard_page.py:1480), `:1705`, `:1710`, `:1749`, `:1765`,
   `:1775`, `:1786`). `showOversee`/`showOverview` assign `liveFn` synchronously *before*
   `run()` ([:1151-1152](../../src/mindhive/dashboard_page.py:1151)), so a stale render can
   discard itself with one comparison. `PLUG_GEN` solves *within-section* poll ordering, which
   is a different problem.
7. **HTTP/1.1's hazard was not Content-Length.** This review verified both `send_response` sites
   set it and declared T2 safe. The real keep-alive hazards are elsewhere: `_body()` raises
   **before draining** ([dashboard.py:350](../../src/mindhive/dashboard.py:350)), so leftover
   bytes become the next request line; and `do_GET`/`do_POST` wrap dispatch in a blanket
   `except` that emits a **second full response** with no already-responded flag
   ([:856-862](../../src/mindhive/dashboard.py:856), `:1170-1174`). Both are silent on HTTP/1.0
   and become stream corruption on HTTP/1.1.
8. **`hook_decision` does not only write an artifact.** This review's W3 said it does.
   [rotation/hook.py:104-108](../../src/mindhive/rotation/hook.py:104) returns
   `additionalContext` carrying `_ADVICE` ([:31-36](../../src/mindhive/rotation/hook.py:31)) —
   the advisory ceiling W3 proposed to spend three hours deciding is already built. **And the
   text is wrong:** `_ADVICE` states "A successor session is queued", which is false in a plain
   `claude` session with no supervisor — the one mode P3 asks to support.
9. **`default_project()` is not on the per-request hot path.** `withProj()`
   ([dashboard_page.py:298](../../src/mindhive/dashboard_page.py:298)) omits `project=` only
   while `PROJECT === ''`, and `loadProjects()` sets it during boot
   ([:328](../../src/mindhive/dashboard_page.py:328)). After the first request the `:854` call
   site is skipped. `default_project()` is also nothing but a loop over `_project_stats`
   ([dashboard.py:194-202](../../src/mindhive/dashboard.py:194)) — fixing the `_project_stats`
   memo fixes it for free, with correct invalidation.
10. **Counts used as test parameters were wrong.** `add_parser` appears **120** times, not 113.
    Distinct top-level subcommands: **39**, not 38. A parity test seeded with 38 is the vacuous
    fixture this plan's own test strategy warns about. Also `sync.py:110` is `sections =
    [USAGE_NOTE]`; the `compose_state` call is at `:116`.

## Diagrams

### Job lifecycle (state machine)

```
                 POST /api/jobs/<kind>          worker thread
                        |                            |
   [none] ----------> [started] -------------> [running] --+--> [finished]  ok | failed | nothing_to_do
                        |  job_started            (in-mem) |        job_finished(outcome, reason)
                        |  (ledger)                        |
                        |                                  +--> (process dies)
                        |                                            |
                        +--------------- dashboard boot -------------+
                                    seals: job_abandoned(reason)

   The boot edge is the ONLY transition not driven by the worker. It exists because the
   registry lives in process memory and a restart takes it with it. Single-writer
   assumption: only the dashboard starts jobs, and only one dashboard binds 54461.
```

### Cache invalidation (why a stat, not a read — and where it lies)

```
  CLI / hook / hive loop appends              dashboard request
            |                                       |
            v                                       v
   events/sNN.jsonl  size+mtime change    segment_stats(ns)  <- os.scandir, no read
            |                                       |
            +-------------------------------> fingerprint differs?
                                                    |
                                        no ---------+--------- yes
                                        |                       |
                                 serve memo             events.read_all(ns)
                                 (microseconds)          -> recompute -> store memo

  TWO STATES WHERE THE STAT LIES, both guarded explicitly:
   * MINDHIVE_EVENT_BACKEND != "file" -> read_all short-circuits to pg (events.py:356);
     local jsonl never changes; the fingerprint freezes forever. Bypass the cache entirely.
   * segment_stats returns {} on ANY OSError (sidecar.py:182). Fail-safe for the sidecar
     ({} != a real stamp -> stale), INVERTED as a cache key ({} == {} -> hit). Treat {}
     as never-equal.

  WRONG precedent: _SESSIONS_MEMO fingerprints (len(evs), evs[-1].ts) — it needs evs, so
  the read already happened. It memoizes the FOLD. We need to skip the READ.
```

### The race window (misread three times — read this before touching any render)

```
  t=0.0   pollCats/refreshLive -> liveFn() ------ renderOversee fetch --------------+
          (bypasses run(), never sets busy)                                         |
  t=0.3   user clicks Overview -> showOverview() sets liveFn = renderOverview       |
          SYNCHRONOUSLY, then run() -> renderOverview fetch ---------------------+  |
  t=0.9                                        /api/overview resolves -----------+  |
          paints Overview   OK                                                      |
  t=1.4                                        /api/oversee resolves ---------------+
          renderOversee checks: liveFn === renderOversee ?  NO -> discard.  FIXED

  The fix is the existing one-line idiom, not new machinery. The losing render never
  entered run(), so a guard stamped there tags nothing.
```

## Sequence — five PRs

```
PR-1  W1  dashboard perf + race        dashboard.py, dashboard_page.py, tests/
PR-2  W4  long operations become jobs  dashboard.py, dashboard_page.py, events.py,
                                       selftest.py, verify.py, tests/e2e/
PR-3  W2  roadmap work link            roadmap.py, tests/
PR-4  W3  rotation advisory truth      rotation/hook.py, tests/
PR-5  W5+W6+W7  health + docs          sopgate.py, context.py, sync.py, __main__.py, README.md

PR-1 first: it is what Hanif feels every time he touches the dashboard.
PR-3, PR-4, PR-5 independent — any time. Within PR-1, locks land BEFORE the nav disable
is dropped (see W1c).
```

## The honest-zero invariant (registry, not a big-bang test)

> Every path that can produce **zero work**, a **degraded result**, or an **abandoned job**
> MUST report which of the three it is, and why.

```
outcome: "did_work" | "nothing_to_do" | "degraded" | "abandoned"
reason:  str   # required unless outcome == "did_work"
```

**Enforcement that does not chain the PRs together.** The surfaces live in an explicit
registry. The parametrized test runs whatever is registered, and each PR registers its own
surface as it lands. A separate test in the LAST PR asserts all five are registered. A
big-bang test that fails on any unregistered surface would block PR-2 until PR-3, PR-4 and
PR-5 merged — which contradicts the independent lanes on the same page.

Surfaces: W4 job routes (PR-2) · roadmap retirement (PR-3) · rotation hook `return None`
(PR-4) · sopgate procedure-to-pointer and MEMORY.md size (PR-5).

## W1a — Profile before optimising (mandatory, with an exit criterion)

W1 does not start until one profile explains the gap between ~1.2s server CPU and the 4.0–4.5s
measured end-to-end. Per-request timing **under a real browser with both pollers live**.

**Exit criterion, stated up front because the answer changes what ships.** The profile
attributes the missing ~3s to one of:
- **connection setup** — HTTP/1.0 opens a fresh TCP connection per fetch, and six parallel
  fetches per paint is plausible. Then T2 is the whole fix and T4/T5 are speculative.
- **server fold cost** — then T4 and T5 are justified.
- **the client** — 72KB page, large `innerHTML` repaints, six concurrent fetches. Then none of
  T2–T9 are the fix, and W1 needs a different task list. This branch is not currently planned
  for; if the profile lands here, stop and re-plan rather than shipping server work anyway.

## W1b — Server (no new events, no new endpoints)

1. **HTTP/1.1 in three ordered steps, not one.** Order is load-bearing: step (c) alone turns
   two silent bugs into stream corruption.
   - (a) `_body()` drains the request body before raising
     ([dashboard.py:350](../../src/mindhive/dashboard.py:350)). Under keep-alive the leftover
     bytes are otherwise parsed as the next request line.
   - (b) a `_responded` flag so the blanket `except` in `do_GET`/`do_POST`
     ([:856](../../src/mindhive/dashboard.py:856), `:1170`) can never write a second response.
     On a persistent connection the client reads that 500 as the answer to its *next* request.
   - (c) `protocol_version = "HTTP/1.1"` **and** `timeout = 30` on the handler, same commit.
     `BaseHTTPRequestHandler.timeout` defaults to `None`; with keep-alive, `rfile.readline()`
     on an idle connection blocks forever, pinning a thread. Content-Length is already correct
     at both `send_response` sites ([:330](../../src/mindhive/dashboard.py:330), `:830`).
2. **Fingerprint-memo `_project_stats` on `sidecar.segment_stats`, NOT `_SESSIONS_MEMO`.**
   `_sessions_cached(proj, am, evs, stamp)` takes `evs` as a parameter — the read already
   happened; it memoizes the fold. `segment_stats` is `os.scandir` over `[size, mtime_ns]` and
   "never opens, reads, or parses a byte". **With both guards from the diagram**: `{}` is
   never-equal, and the cache is bypassed when `MINDHIVE_EVENT_BACKEND != "file"`.
   Tests must cover both failure modes, not just the happy path — the naive
   "read_all not called on a hit" test passes in both broken states.
   This also fixes `default_project()` for free; it is only a loop over `_project_stats`.
3. **Gate `/api/mindnet/cats` with the same fingerprint.**
   [dashboard.py:665](../../src/mindhive/dashboard.py:665) does a full `read_all(proj)` — or
   `all_event_pairs()` across all 56 namespaces in HIVE MIND — on **every 2.5s poll, with no
   busy gate at all**. `tel_mod.fold(evs)` there is not memoized either.
4. **Serialise both pollers** — `refreshLive` ([:255](../../src/mindhive/dashboard_page.py:255))
   and `pollCats` ([:1847](../../src/mindhive/dashboard_page.py:1847)) are independent timers;
   neither skips a tick while its own previous request is in flight.

Then **re-measure**, same conditions as W1a.

## W1c — Client: never paint a section the user did not pick

**Locks first, then the nav disable comes off. Order is not negotiable.** `setBusy(true)`
disabling 21 buttons is today's only mutual exclusion for six mutating endpoints that have no
lock at all: `_post_selftest` ([:1133](../../src/mindhive/dashboard.py:1133)), `_post_embed`
(`:907`), `_post_plugins_scan` (`:918`), `_post_harvest` (`:868`), `_post_sync` (`:902`),
`_post_project` (`:899`). Only `_REFLECT_LOCK` and `_INVOKE_LOCK` exist. Dropping the disable
first ships a window where two clicks start two full pytest runs over the same repo.

- **Add a per-operation server-side lock to all six**, following the existing `_REFLECT_LOCK`
  pattern. A second call while one runs gets an honest refusal, not a second run.
- **Then drop the nav disable**, using the existing `if (liveFn === renderX)` idiom to discard
  stale paints. No new machinery, no signature changes.
- **Skeleton past 300ms, status text past 10s.**
- **`refreshLive` counts consecutive failures.** Keep swallowing transient blips
  ([:253](../../src/mindhive/dashboard_page.py:253)), but after 3 in a row (7.5s) the live chip
  becomes visible state; one success resets it.
- **REGRESSION (mandatory):** dropping the nav disable changes behaviour that works today.
  Regression test required — nav clickable during a fetch, second click not overpainted, and
  a concurrent mutating call refused rather than run twice.

## W2 — Roadmap: retire a card because something checkable said so

- A durable link from roadmap to the work it produced. `roadmap.fold` carries no
  branch/PR/commit field today — verified, zero references in `roadmap.py`.
- Merge detection that **degrades honestly**, three distinct failure modes, not one: no GitHub
  remote · `gh` not installed · network or rate-limit failure. Each yields `unknown` with its
  own reason. Never a false `merged`.
- An honest lifecycle for a human-only step — not pinned open forever, not silently marked done.
- Retirement is an append-only event carrying the **reason**, following the
  `loop_run_dismissed` tombstone precedent. Never edit `events/*.jsonl`.
- **Extend `test_fold_is_total_on_junk_history`**
  ([tests/test_roadmap.py:138](../../tests/test_roadmap.py:138)) with a valid-but-old record
  lacking the new link fields. Do not write a second test for the same invariant.

## W3 — The advisory already ships, and it lies

The honest ceiling P3 asked this plan to decide is **already built**:
[rotation/hook.py:104-108](../../src/mindhive/rotation/hook.py:104) returns `additionalContext`
carrying `_ADVICE`. What is missing is only the supervisor that consumes `ROTATION_PENDING`.
There is nothing to re-decide.

**The real defect:** `_ADVICE` ([:31-36](../../src/mindhive/rotation/hook.py:31)) asserts
*"A successor session is queued."* In a plain `claude` session there is no supervisor, so
nothing is queued. Turning `MINDHIVE_ROTATION` on without a supervisor — exactly the mode P3
wants — makes the hook tell Claude something untrue.

- Make `_ADVICE` context-aware: claim a queued successor only when a supervisor is actually
  present; otherwise say what is true ("no successor is queued — write what the next session
  needs into the ledger and start a fresh one").
- One test per mode. Both must keep the hook fail-open and must never emit
  `{"decision": "block"}`.
- **State the platform limit:** `hooks/` is nine files, all `.ps1`, zero `.sh`.
- **Non-negotiable (CLAUDE.md):** fires in every session on this machine · stays fail-open ·
  never emits `block` · registration and activation stay separable.

## W4 — Every long operation becomes a job

**All six, not three.** The "Heavy" group is five buttons
([dashboard_page.py:199-203](../../src/mindhive/dashboard_page.py:199)) plus `Catch up`
([`_post_harvest` at :868](../../src/mindhive/dashboard.py:868)). Converting three leaves the
dashboard speaking two languages for the same thing.

- Click returns **202 accepted** immediately; the UI says so immediately.
- **No SSE.** `_send` computes `Content-Length` and gzips every body; an SSE route bypasses all
  of it, pins a thread per open tab, and the module already documents Windows connection aborts
  (`WinError 10053`, [:342](../../src/mindhive/dashboard.py:342)). One user on localhost with
  two 2.5s timers does not need push. Progress rides the existing poll.
- **`verify.py` is where the output actually lives, not `selftest.py`.** `selftest.py` is 21
  lines of delegation; the stream is produced in `verify._stream`
  ([verify.py:132-178](../../src/mindhive/verify.py:132)), which hardcodes `sys.stdout.write`,
  and `run_and_record(..., echo=True)` exposes a boolean, not a sink. Routing job output
  elsewhere means changing `verify.py`'s signature — and `verify.py` is on `premise.check`'s
  read path, so it is a shared-contract change. PR-2 must include it.
- **Ledger records `job_started` / `job_finished` / `job_abandoned` only.** No `job_progress`:
  `events.read_all` sits on the per-prompt hook path — [sopgate.py:34](../../src/mindhive/sopgate.py:34)
  refuses a ledger fallback precisely because that read costs ~0.36s on every prompt.
- **`job_abandoned` sealed at boot for the selected namespace only**, not all 56. The
  single-writer assumption is written as a code comment and a DATA_MODEL invariant, not assumed.
- **Bounded output.** Byte-capped ring buffer (~256 KB, oldest dropped); the status endpoint
  takes an offset and returns only what is new. `_body` already caps inbound at
  `MAX_BODY_BYTES` ([:346](../../src/mindhive/dashboard.py:346)); outbound had no cap.
- **DATA_MODEL section 6 per new type**, stated in the PR body.
- Do not double-record: `verify.run_and_record` already appends a `verification` event.
- **Honest-zero:** "no incident has recurred yet — reflection only runs on repeats" is a loud,
  distinct state.
- **Self-test:** running the full pytest suite inside the web server is questionable even when
  async. Keep it and justify it, or move it and say what replaces it.
- Replace the raw browser `confirm()` at
  [dashboard_page.py:1816](../../src/mindhive/dashboard_page.py:1816) with in-page affordance.
- Token check on every job route.

## W5 — Make the silence impossible (no new module)

There is no `health.py`. `src/mindhive/` already has `context.py`, `premise.py`, `verify.py`,
`packs.py`, `guard.py`, `chain.py`, `hygiene.py`; "health" is an adjective, not a boundary.
Each piece goes to its natural owner, and `__main__.py` (**3790 lines**, ~4× the thermo-nuclear
threshold) gets argparse wiring only.

- **SOP-injection probe → `sopgate.py`** (192 lines; already owns the hook path and
  `PROMPT_BUDGET`, which is what the probe measures). Runs the real hook path and reports which
  blocks fired and which stayed silent.
- **Checkout-staleness check → repo plumbing** (its own small module or `hygiene.py`; it has no
  store dependency). Follow the existing `--max-age-hours` stale-gate idiom
  ([__main__.py:3516](../../src/mindhive/__main__.py:3516)), do not invent a new one.
- **Context-size reporting → `context.py`**, extending `hive context --check`.
- **Tracking branch.** `master` genuinely has no upstream — verified. Fix it so `git pull`
  works, and make repo state self-evident.
- **The budget decision.** `PROMPT_BUDGET = 3000`
  ([sopgate.py:68](../../src/mindhive/sopgate.py:68)) with a comment stating it was sized for a
  2722-char SOP; `execution-discipline v2` is 3414. Raise, trim, or deliver differently — and
  justify it. The procedure-to-pointer degradation must be visible.
- **Context delivery.** The 200-line limit belongs to the Claude Code harness, not MIND HIVE —
  do not hardcode it. The real lever is what `compose_state` writes into MEMORY.md
  ([sync.py:116](../../src/mindhive/sync.py:116)); the SOP block dominates the file and is
  **the same text `PROMPT_BUDGET` pulls on**. Decide the SOP's size once, for both surfaces.
  Report MEMORY.md size against a *configurable* budget.
- **Constraints:** sopgate stays fail-open and sidecar-only · only ever writes
  `additionalContext` · never blocks a prompt · never folds the ledger on the per-prompt path.

## W6 — Permanent checks, on existing surfaces

No new subcommand. `hive context --check` already has `--budget --check --json --top --origin`
([__main__.py:2941](../../src/mindhive/__main__.py:2941)); `hive verify` covers the chain;
`hive packs check` is another gate; STATE.md's blocking notice is already the delivery vehicle.

- Extend `hive context --check` to cover context delivery.
- The SOP probe and staleness check surface through their owning modules' commands.
- A latency-budget test that fails when an endpoint exceeds its budget on a synthetic store.

## W7 — Documentation

- Any workstream adding or changing a command or flag updates its README bullet in the same
  branch.
- **A parity test, honestly named a test — not a gate.** It compares the **39** distinct
  top-level subcommands from `--help` against those named in README and fails when one is
  missing. Not the 120 total `add_parser` calls. No blocking STATE.md marker: this is CI
  enforcement, and the plan says so rather than claiming a gate it does not have.
- **Quick Start** above the command list: install, run the dashboard, first command producing
  meaningful output. `pip install -e .` appears nowhere in README today.

## Test strategy — two tiers, both fenced

Existing client "race tests" are **source-string assertions**, not behaviour:
[tests/test_dashboard.py:144](../../tests/test_dashboard.py:144) asserts a literal substring and
`:155-159` compares character indices. No JavaScript is executed. A guard test built on that
precedent would fail on `efa3751` (string absent) and pass after (string present) while proving
nothing — the recorded caveat verbatim: *"A regression test whose FIXTURE does not reproduce the
defect proves nothing, and reads exactly like one that does."*

`node v24.14.0` and `bun 1.3.14` are installed. `dependencies = []` stays empty.

- **Tier 1 — node unit test** of the discard decision. Much smaller now that the mechanism is
  `liveFn === renderX`: the pure function is the comparison plus its ordering guarantee.
- **Tier 2 — Playwright** for the six browser-observable flows: nav clickable during a fetch ·
  skeleton past 300ms · status text past 10s · job progress survives the 2.5s repaint ·
  zero-work reads differently from failure · stale chip after 3 consecutive failures.

**Both are fenced out of the default pytest run.** `hive selftest` shells
`[sys.executable, -m, pytest, -q, tests/]` **from inside the dashboard's POST handler**
([verify.py:64](../../src/mindhive/verify.py:64)). Without a marker that deselects `tests/e2e/`
by default, pressing Self-test would spawn node — and browsers — inside the web server.

**CI:** `ci.yml` is a 4-leg matrix whose whole install is `pip install -e .[dev]`; there is no
`actions/setup-node`, and GHA runners ship node but not Playwright browsers (~400MB+ per leg).
Add **one** separate leg with `setup-node` and a browser cache, not four. **T10 belongs to
PR-2**, not PR-1 — three of its six flows are W4 deliverables that do not exist yet.

### Coverage today

```
COVERAGE: 3/24 paths tested (13%)  |  Code: 2/13 (15%)  |  User flows: 1/11 (9%)
QUALITY:  3-star:2  2-star:0  1-star:2 (both assert source strings, not behaviour)
GAPS:     21  (6 reachable only via Playwright)
```

Reuse, do not duplicate: `test_fold_is_total_on_junk_history` (test_roadmap.py:138) ·
`test_over_budget_sop_degrades_to_a_pointer_not_a_truncation` (test_sopgate.py:72) · the whole
`test_rotation_hook.py` fail-open suite, which injects failures at the module seam.

## Failure modes per new codepath

| Codepath | Realistic production failure | Test? | Error handling? | Silent? |
|---|---|---|---|---|
| `segment_stats` fingerprint | pg backend or repeated OSError freezes the cache forever | **both modes** | `{}` never-equal; non-file backend bypasses cache | **would be silent without both guards** |
| `_body` raise before drain | leftover bytes parsed as the next request under keep-alive | required | drain before raise | **stream corruption, hard to diagnose** |
| blanket `except` double response | client reads a 500 as the answer to its next request | required | `_responded` flag | **same** |
| HTTP/1.1 keep-alive | idle connections pin threads until restart | required | `timeout = 30` | would be silent |
| Unlocked mutating endpoints | two Self-tests run two pytest suites over one repo | required | per-operation server locks | no — refusal is explicit |
| `liveFn ===` discard | idiom applied to some renders but not others | tier 1 + tier 2 | n/a | **silent — why both tiers exist** |
| Job registry | server restart mid-job orphans `job_started` | required | `job_abandoned` at boot | no |
| Ring buffer | job floods output, oldest lines dropped | required | cap + offset reads | no — truncation reported |
| Merge detection | `gh` rate-limited, mistaken for "not merged" | required | three distinct `unknown` reasons | no |
| `_ADVICE` text | claims a queued successor that does not exist | required | context-aware text | **is silent today** |
| `refreshLive` swallow | endpoint fails every 2.5s forever | required | 3-strike stale chip | **is silent today** |

## What already exists (reuse, do not rebuild)

- **`if (liveFn === renderX)`** — seven uses in `dashboard_page.py`. The cross-section discard idiom.
- **`sidecar.segment_stats`** — stat-only ledger fingerprint. The right cache primitive, with two guards.
- **`_SESSIONS_MEMO`** ([dashboard.py:266](../../src/mindhive/dashboard.py:266)) — memoizes the fold, needs `evs`. Right family, wrong member.
- **`_REFLECT_LOCK` / `_INVOKE_LOCK`** — the server-side lock pattern the other six endpoints need.
- **`_auto_harvest_loop`** ([dashboard.py:1453](../../src/mindhive/dashboard.py:1453)) — background daemon thread with an env gate and stop event. W4's threading precedent.
- **`_ADVICE` + `additionalContext`** ([rotation/hook.py:104](../../src/mindhive/rotation/hook.py:104)) — the advisory ceiling, already shipped. Fix the text, do not rebuild.
- **`--max-age-hours` stale gate** (`plugins autoscan`) — W5's staleness idiom.
- **`loop_run_dismissed`** — append-only tombstone; W2 retirement and W4 abandonment both follow it.
- **`hive context --check`** ([__main__.py:2941](../../src/mindhive/__main__.py:2941)) — the health-check surface.
- **`MAX_BODY_BYTES`** ([dashboard.py:346](../../src/mindhive/dashboard.py:346)) — bounded-payload precedent, inbound.
- **`verify.run_and_record`** — already records selftest runs. Do not double-record.
- **`viewPicked`** (PR #102) — boot race, already fixed. Keep.

## NOT in scope (deferred, with reason)

- **`LICENSE` / `CONTRIBUTING` / `CHANGELOG`** — absent while README publishes a public install command. TODOS.md.
- **Splitting `__main__.py`** (3790 lines) — real, but mixing structural and behavioural change is what to avoid. TODOS.md, blocked on these five PRs.
- **Nav restructure** — 21 buttons mixing navigation and mutation. TODOS.md.
- **CLI surface** — 39 ungrouped top-level subcommands. TODOS.md.
- **Full README restructure** — Quick Start only for now. TODOS.md.
- **Cross-platform hooks** — POSIX alongside `.ps1`. Stated as a limit, not built.
- **SSE / push progress** — rejected on feasibility, not deferred.
- **A supervisor for plain `claude`** — the only genuinely missing half of rotation. Out of scope here; W3 fixes the lie, not the gap.

## Worktree parallelization

| Step | Modules touched | Depends on |
|---|---|---|
| W1 (PR-1) | `dashboard.py`, `dashboard_page.py`, `tests/` | — |
| W4 (PR-2) | `dashboard.py`, `dashboard_page.py`, `events.py`, `selftest.py`, `verify.py`, `tests/e2e/` | W1 (same modules) |
| W2 (PR-3) | `roadmap.py`, `tests/` | — |
| W3 (PR-4) | `rotation/`, `tests/` | — |
| W5+W6+W7 (PR-5) | `sopgate.py`, `context.py`, `sync.py`, `__main__.py`, `README.md` | — |

```
Lane A: W1 -> W4     (sequential — both own dashboard.py and dashboard_page.py)
Lane B: W2           (independent)
Lane C: W3           (independent)
Lane D: W5+W6+W7     (independent)

Launch A, B, C, D in parallel worktrees. Within A, W4 waits for W1.
Conflict flag: none — B, C, D share no module directory with A or each other.
Note: PR-5 touches __main__.py for argparse wiring only; keep those hunks small so a
      future __main__.py split rebases cleanly.
```

## Implementation Tasks

- [ ] **T1 (P1, human: ~3h / CC: ~30min)** — dashboard — profile the 1.2s-vs-4.3s gap
  - Surfaced by: Corrections 4 — no optimisation ships on a hypothesis
  - Files: profiling harness only
  - Verify: breakdown under a real browser, both pollers live; state which of the three exit branches it lands in
- [ ] **T2 (P1, human: ~2h / CC: ~15min)** — dashboard — drain `_body` before raising
  - Surfaced by: Corrections 7 — leftover bytes become the next request line under keep-alive
  - Files: `src/mindhive/dashboard.py`, `tests/`
  - Verify: oversized body followed by a valid request on the same connection is answered correctly
- [ ] **T3 (P1, human: ~2h / CC: ~15min)** — dashboard — `_responded` flag on both blanket excepts
  - Surfaced by: Corrections 7 — a second full response corrupts the keep-alive stream
  - Files: `src/mindhive/dashboard.py`, `tests/`
  - Verify: a handler that raises after writing produces exactly one response
- [ ] **T4 (P1, human: ~1h / CC: ~10min)** — dashboard — `protocol_version = "HTTP/1.1"` + `timeout = 30`
  - Surfaced by: Architecture — keep-alive pins a thread per idle connection; ships only after T2 and T3
  - Files: `src/mindhive/dashboard.py`
  - Verify: HTTP/1.1 in headers; thread count stable after N requests plus idle
- [ ] **T5 (P1, human: ~4h / CC: ~35min)** — dashboard — `segment_stats` memo with both guards
  - Surfaced by: Architecture issue 1 + cross-model X5 — `_SESSIONS_MEMO` cannot skip the read; `{}` and pg invert the key
  - Files: `src/mindhive/dashboard.py`, `tests/`
  - Verify: tests for cache hit, CLI-append invalidation, `{}`-on-OSError, and non-file backend bypass
- [ ] **T6 (P1, human: ~4h / CC: ~30min)** — dashboard — gate `/api/mindnet/cats` with the same fingerprint
  - Surfaced by: Performance issue 12 — full `read_all` every 2.5s, no busy gate, all namespaces in HIVE MIND
  - Files: `src/mindhive/dashboard.py`
  - Verify: unchanged ledger yields no `read_all` and no `tel_mod.fold`; payload identical
- [ ] **T7 (P1, human: ~2h / CC: ~15min)** — dashboard_page — serialise both pollers
  - Surfaced by: Outside voice 2 — `pollCats` has no busy gate; neither skips in-flight ticks
  - Files: `src/mindhive/dashboard_page.py`
  - Verify: no overlapping requests from either timer under a 1s response
- [ ] **T8 (P1, human: ~3h / CC: ~25min)** — dashboard — server-side locks on the six unlocked mutating endpoints
  - Surfaced by: Cross-model X3 — `setBusy` is today's only mutual exclusion; PR-1 would remove it
  - Files: `src/mindhive/dashboard.py`, `tests/`
  - Verify: a second call while one runs is refused, not run twice. Ships BEFORE T9.
- [ ] **T9 (P2, human: ~2h / CC: ~15min)** — dashboard_page — `liveFn ===` discard, drop nav disable, skeleton ladder, REGRESSION test
  - Surfaced by: Cross-model X1 + the REGRESSION RULE — the idiom already exists seven times in this file
  - Files: `src/mindhive/dashboard_page.py`, `tests/`
  - Verify: nav clickable during fetch; stale cross-section paint discarded; concurrent mutation refused
- [ ] **T10 (P2, human: ~2h / CC: ~15min)** — dashboard_page — 3-strike stale chip on `refreshLive`
  - Surfaced by: Code Quality issue 8 — `catch(_) {}` makes a permanently-failing endpoint look healthy
  - Files: `src/mindhive/dashboard_page.py`
  - Verify: 3 consecutive failures flip the chip; one success resets it
- [ ] **T11 (P1, human: ~4h / CC: ~30min)** — tests — node unit test of the discard decision, marker-fenced
  - Surfaced by: Test issue 9 — source-string assertions prove nothing
  - Files: `tests/`, `pytest.ini` or `pyproject.toml` marker config
  - Verify: fails on `efa3751`, passes after; deselected from the default `pytest tests/` run
- [ ] **T12 (P1, human: ~1d / CC: ~1h)** — tests — Playwright suite, fenced, one CI leg
  - Surfaced by: Test issue 11 + cross-model X6 — six flows unreachable otherwise; would otherwise spawn browsers inside the web server
  - Files: `tests/e2e/`, `.github/workflows/ci.yml`
  - Verify: `hive selftest` never selects `tests/e2e/`; one CI leg with `setup-node` runs it. Belongs to PR-2.
- [ ] **T13 (P1, human: ~1d / CC: ~45min)** — dashboard — all six long operations become 202-accepted jobs
  - Surfaced by: P4 + Code Quality issue 6 — three of five Heavy buttons plus Catch up left synchronous
  - Files: `src/mindhive/dashboard.py`, `src/mindhive/dashboard_page.py`, `src/mindhive/selftest.py`, `src/mindhive/verify.py`
  - Verify: exercise all six against the real dashboard, including the empty-work case
- [ ] **T14 (P1, human: ~4h / CC: ~30min)** — events — `job_*` types + `job_abandoned` boot seal
  - Surfaced by: DX T-F + Architecture issue 3 — orphaned jobs read as still-running
  - Files: `src/mindhive/events.py`, `src/mindhive/dashboard.py`, `docs/DATA_MODEL.md`, `tests/`
  - Verify: DATA_MODEL section 6 in the PR body; selected namespace only; single-writer assumption written down
- [ ] **T15 (P1, human: ~3h / CC: ~25min)** — jobs — shared outcome type + opt-in surface registry
  - Surfaced by: Code Quality issue 7 + cross-model X7 — a big-bang test would chain all five PRs together
  - Files: `src/mindhive/`, `tests/`
  - Verify: the parametrized test runs registered surfaces; a final test in PR-5 asserts all five are registered
- [ ] **T16 (P2, human: ~2h / CC: ~15min)** — jobs — ring buffer + offset reads
  - Surfaced by: Performance issue 13 — unbounded output in memory, re-sent whole every 2.5s
  - Files: `src/mindhive/dashboard.py`, `src/mindhive/dashboard_page.py`
  - Verify: buffer capped; status returns only new bytes since offset; truncation reported
- [ ] **T17 (P1, human: ~1d / CC: ~40min)** — roadmap — durable work link + three distinct `unknown` reasons
  - Surfaced by: P2 — `roadmap.fold` has no branch/PR/commit field
  - Files: `src/mindhive/roadmap.py`, `tests/`
  - Verify: no remote, no `gh`, and rate-limited each yield `unknown` with its own reason
- [ ] **T18 (P1, human: ~30min / CC: ~10min)** — roadmap — extend the existing totality test
  - Surfaced by: Test issue 10 — `test_fold_is_total_on_junk_history` already guards this
  - Files: `tests/test_roadmap.py`
  - Verify: a valid-but-old record without link fields does not raise
- [ ] **T19 (P1, human: ~2h / CC: ~20min)** — rotation — make `_ADVICE` stop claiming a successor that does not exist
  - Surfaced by: Cross-model X4 — the advisory ceiling already ships; its text is false without a supervisor
  - Files: `src/mindhive/rotation/hook.py`, `tests/test_rotation_hook.py`
  - Verify: one test per mode; hook stays fail-open; never emits `block`
- [ ] **T20 (P1, human: ~1d / CC: ~45min)** — sopgate — SOP-injection probe in its owning module
  - Surfaced by: P5 + cross-model X8 — `sopgate.py` owns the hook path and `PROMPT_BUDGET`
  - Files: `src/mindhive/sopgate.py`, `src/mindhive/__main__.py` (argparse wiring only)
  - Verify: probe output before and after; induce the broken state deliberately and show it caught
- [ ] **T21 (P2, human: ~3h / CC: ~25min)** — plumbing — checkout-staleness check via the `--max-age-hours` idiom
  - Surfaced by: P5 — 64 commits stale, silently; `master` has no upstream (verified)
  - Files: repo-plumbing module or `src/mindhive/hygiene.py`, `src/mindhive/__main__.py`
  - Verify: reports N commits behind; degrades honestly with no network
- [ ] **T22 (P2, human: ~3h / CC: ~25min)** — sync/context — one SOP-size decision for both surfaces
  - Surfaced by: DX D10 + outside voice 10 — `PROMPT_BUDGET` and MEMORY.md pull the same text
  - Files: `src/mindhive/sync.py`, `src/mindhive/context.py`, `tests/`
  - Verify: MEMORY.md size reported against a configurable budget, never a hardcoded 200
- [ ] **T23 (P2, human: ~3h / CC: ~25min)** — context — extend `hive context --check` + latency budget test
  - Surfaced by: DX T-C — no new subcommand; the health surfaces already exist
  - Files: `src/mindhive/context.py`, `src/mindhive/__main__.py`, `tests/`
  - Verify: red states surface through the STATE.md notice; budget test fails when exceeded
- [ ] **T24 (P2, human: ~2h / CC: ~15min)** — docs — README updates, 39-subcommand parity test, Quick Start
  - Surfaced by: DX D7/D12 + Corrections 10 — five behaviour changes, zero planned doc updates; the count was wrong
  - Files: `README.md`, `tests/`
  - Verify: the parity test enumerates `--help` at runtime rather than hardcoding a count
- [ ] **T25 (P3, human: ~15min / CC: ~5min)** — repo — state the Windows-only limit in the W3 PR body
  - Surfaced by: DX D8 — `hooks/` is nine `.ps1` files, zero `.sh`
  - Files: PR body only
  - Verify: reviewer confirms the limit is stated, not implied

## House rules (every PR)

- Full pytest suite green before done; CI green before a PR is called done.
- `hive log <category> "<what changed and why>"` before the work is called done.
- Never repoint the live dashboard via `PYTHONPATH` or `sys.path`; preview an unmerged branch on
  a different port from its own worktree.
- Append-only: corrections are new events, never edits to `events/*.jsonl`.

## Design decisions

Classifier: **APP UI** (dashboard, data-dense, task-focused). App UI rules apply, not
landing-page rules. Reference board (real palette, real glyphs, hand-built — the mockup
generator has no API key configured):
`~/.gstack/projects/hanifridho62-rgb-Lintester-project/designs/status-vocabulary-20260810/board.html`

### D1 — Status vocabulary: reuse, do not invent a third dialect

The dashboard already has two status vocabularies and the plan was adding a third.
`TRIAGE_LEGEND` ([dashboard_page.py:444-447](../../src/mindhive/dashboard_page.py:444)) already
means what four of the five job outcomes mean — `dismissed` ("no resolution needed") **is**
`nothing_to_do`.

```
outcome         glyph  hex        borrowed from        hover sentence
did_work          ✓    #46c46a    resolved             "ran and did work"
nothing_to_do     ○    #6b7488    dismissed            "ran, found nothing to do — <reason>"
degraded          ●    #e0b341    watching             "partial result — <reason>"
failed            ✗    #ff8a8a    unresolved           "failed — <reason>"
abandoned         ⊘    #6b7488    (new — no analogue)  "interrupted by a server restart"
```

`abandoned` is the only genuinely new glyph, because "the process is gone" has no existing
counterpart. Every row carries a hover sentence in the `TRIAGE_LEGEND` style.

### D2 — Five states, and `failed` is one of them

The plan's enum and its own job-lifecycle diagram contradicted each other: the enum listed
`did_work | nothing_to_do | degraded | abandoned`, the diagram listed `ok | failed |
nothing_to_do`. A job that genuinely errors — pytest fails, the `claude` CLI exits non-zero,
`gh` throws — had nowhere to land, and would have been rendered as `degraded`, which means
"partial result". A failing self-test would have read as a partial result.

```
outcome: "did_work" | "nothing_to_do" | "degraded" | "failed" | "abandoned"
reason:  str   # required unless outcome == "did_work"
```

The lifecycle diagram is corrected to match. The parametrized invariant test includes `failed`
as a mandatory case: every registered surface must be able to produce it and render it
distinctly.

### D3 — Skeletons are per-section, or they lie

"Skeleton past 300ms" is not one component. Three shapes, matched to what actually renders:

```
SECTION                                   SKELETON SHAPE
Overview                                  card grid, 6 placeholders (matches the 6 stat cards)
Memory / Sessions / Log / Improvements    list rows, ~6 placeholders
  / SOP / Oversee
State / Briefing / Recall                 one monospace block
Graph / MindParallel / MindSwarm          NONE — the iframe has its own loading
```

Placeholder counts match typical real counts. The 2026 guidance this plan's ladder comes from
is explicit: a placeholder showing three items that resolves into seven costs more trust than
no skeleton at all.

### D4 — Job progress lives as an Oversee card

The plan required job state to survive the 2.5s repaint but never said where a job is *seen*.
Oversee already renders live Claude work as titled cards with the ✓/◐/○ checklist and the
`online — Claude is working` strip. A job is live work; it is a new member of an existing
category, not a new category.

- No new band under the header. The comment at
  [dashboard_page.py:150-153](../../src/mindhive/dashboard_page.py:150) explains that the
  session strip sits *beside* the faculty row precisely to avoid a second band pushing the page
  down every time something starts.
- Jobs inherit the `loop_run_dismissed` tombstone and the cancel affordance for free.
- Side effect worth naming: this makes the magical moment richer instead of competing with it.

### D5 — Header: two tiers, one accent

The header row already carries six signals at equal weight: title, busy dot, status text,
project picker, live chip, sync time. Removing the nav disable promotes 12px muted status text
to the primary "working" signal, in the busiest place on the page.

```
TIER 1 (prominent, coloured, left)   busy dot + status text
                                     the only things allowed to move or carry colour at rest
TIER 2 (dim #6b7488, right, still)   project · live chip · sync time
```

The stale chip is promoted to tier 1 while red and drops back afterwards. The plan states the
rule; it does not just add a third chip state.

### D6 — Skeleton is the primary working signal, at 150ms

Today the nav greying out is the largest visual change on the page — annoying, but impossible
to miss. Removing it and leaving a 9px dot plus 12px text in the opposite corner from where the
eye is would trade "feels hung" for "feels like nothing happened", which is worse.

- Skeleton appears at **150ms**, not 300ms — the dead window must be shorter than the distance
  to doubt.
- The panel dims slightly while old content is replaced.
- The header dot stays as secondary confirmation.
- `aria-busy` on the panel, plus one `aria-live="polite"` region announcing section changes and
  job start/finish/outcome. Today the only screen-reader-visible change is `disabled` on the nav
  buttons; removing it without a replacement leaves zero announcements, not weak ones.

### D7 — Drop the confirmation dialog

`confirm()` ([dashboard_page.py:1816](../../src/mindhive/dashboard_page.py:1816)) currently
warns "this can take a while. Continue?" — a warning whose reason disappears the moment a click
returns 202 immediately and progress is visible in Oversee. Keeping a dialog for an operation
that no longer blocks anything preserves friction whose cause was deleted.

- Click starts the job. The button flips to a running state instantly; the Oversee card carries
  a cancel button. **Cancellable beats confirmable.**
- One exception: **Embed forcing past `LIVE_LOCK`** keeps an explicit confirmation. It is the
  only genuinely dangerous action on the page.

### D8 — Motion and announcements

- Every animation — the existing pulse and the new skeleton shimmer — is wrapped in
  `@media (prefers-reduced-motion: reduce)` that disables it. Repeating animation is not an
  annoyance for some people, it is a trigger.
- The `aria-live` region and `aria-busy` from D6.
- **Desktop-only is stated deliberately.** Fixed sidebar nav, card grids, a 72vh graph iframe.
  For a one-machine tool that is a reasonable position, and naming it beats pretending mobile
  is an open gap.

### D9 — Extract colour tokens in PR-1

`dashboard_page.py` carries **123 hardcoded hex literals and zero CSS custom properties**.
`#2a2f3d` (borders) is repeated 17 times, `#46c46a` (ok green) 11 times. The palette has already
started drifting: `#9aa3b8` (7×) beside `#8a93a6` (5×); `#e6e8ef` (3×) beside `#dfe6f3` (2×).
Nothing knows which is correct because nothing is named. This plan adds five coloured states
plus a chip state on top of that.

```
:root { --bg --panel --nav --head --line --fg --mut --dim --ok --bad --amb --blu --pill }
```

All 123 literals become references; the two near-duplicate pairs resolve to one each.
Mechanical, and verifiable by eye — the page must look identical.

## Design scorecard

```
+====================================================================+
|            DESIGN PLAN REVIEW — SCORECARD                          |
+====================================================================+
| Pass                        | Before | After |
|-----------------------------|--------|-------|
| 1  Information Architecture |  5/10  | 9/10  |
| 2  Interaction States       |  3/10  | 9/10  |
| 3  User Journey             |  5/10  | 8/10  |
| 4  AI Slop Risk             |  8/10  | 8/10  |
| 5  Design System Alignment  |  2/10  | 8/10  |
| 6  Responsive & A11y        |  1/10  | 8/10  |
| 7  Unresolved Decisions     |  10 raised, 10 resolved, 0 deferred  |
+--------------------------------------------------------------------+
| Classifier                  | APP UI                                |
| Hard rejections             | 0 triggered by this plan              |
| Overall design              |  4/10  → 8/10                         |
+====================================================================+
```

Pass 4 note, not a plan finding: `body { font: 14px/1.5 system-ui }` puts `system-ui` as the
primary typeface, which is on the AI-slop blacklist ("the I-gave-up-on-typography signal").
Pre-existing, not this plan's doing, and not proposed for change here.

Pass 5 reaches 8 rather than 10 because `DESIGN.md` is deferred to TODOS.md; Pass 6 reaches 8
because the `#6b7488` contrast failure (3.87:1 on `#171a23`, below the 4.5:1 AA threshold) is
legacy debt deferred to TODOS.md.

## Design implementation tasks

- [ ] **T26 (P1, human: ~2h / CC: ~20min)** — dashboard_page — extract colour tokens to `:root`
  - Surfaced by: Design D9 — 123 hex literals, 0 custom properties, palette already drifting
  - Files: `src/mindhive/dashboard_page.py`
  - Verify: page renders identically; `#9aa3b8`/`#8a93a6` and `#e6e8ef`/`#dfe6f3` resolved to one each
- [ ] **T27 (P1, human: ~2h / CC: ~15min)** — dashboard_page — job outcome glyphs from the existing vocabulary
  - Surfaced by: Design D1/D2 — a third status dialect, and `failed` missing from the enum
  - Files: `src/mindhive/dashboard_page.py`, `src/mindhive/events.py`
  - Verify: five states render with distinct glyph + colour + hover sentence; `failed` is in the enum
- [ ] **T28 (P1, human: ~3h / CC: ~25min)** — dashboard_page — three skeleton shapes at 150ms
  - Surfaced by: Design D3/D6 — one grey box lies; the dead window must be shorter than doubt
  - Files: `src/mindhive/dashboard_page.py`
  - Verify: card grid / list rows / monospace block per section; Graph and Workspace exempt
- [ ] **T29 (P1, human: ~3h / CC: ~25min)** — dashboard_page — job progress as an Oversee card
  - Surfaced by: Design D4 — the plan required persistence but never said where a job is seen
  - Files: `src/mindhive/dashboard_page.py`, `src/mindhive/dashboard.py`
  - Verify: a running job is visible from any section; switching sections does not hide it
- [ ] **T30 (P2, human: ~2h / CC: ~15min)** — dashboard_page — two-tier header + drop the confirm dialog
  - Surfaced by: Design D5/D7 — six equal signals; a warning whose reason was deleted
  - Files: `src/mindhive/dashboard_page.py`
  - Verify: tier 2 is dim and still; only Embed-past-LIVE_LOCK still confirms
- [ ] **T31 (P1, human: ~2h / CC: ~15min)** — dashboard_page — reduced-motion + aria-live + aria-busy
  - Surfaced by: Design D6/D8 — removing `disabled` removes the only screen-reader signal
  - Files: `src/mindhive/dashboard_page.py`
  - Verify: animations stop under `prefers-reduced-motion`; section change and job outcome are announced

## T1 RESULT — the profile (measured 2026-08-10, `efa3751`)

W1a's exit criterion named three branches. **The answer is branch 2 — server fold cost — with a
mechanism no review named: GIL-serialized queueing behind two unserialized pollers.**

Method: `curl -w` against the live dashboard on 54461 with a browser attached and both pollers
running, versus an isolated dashboard started from this worktree on 54471 with no browser.
Same store, same commit. The live process was never touched; the 54471 process was stopped
after measuring.

```
                    54471 alone        54461 real load      amplification
                    (no browser)       (browser + 2 pollers)
/api/projects       1.76 - 1.86s       11.7 - 12.4s              6.7x
/api/overview       1.15 - 1.20s        6.4 -  8.3s              6.1x
/api/oversee        1.26 - 1.44s        8.6 -  9.5s              6.6x
/api/mindnet/cats   0.87 - 0.92s        6.0s                     6.7x

time_connect        0.0009s on every request, both servers
```

GIL serialization test on 54471 — four concurrent requests:

```
sum of the four measured serially   ~5.3s
wall clock for all four concurrent   4.99s     -> concurrency saves 6%
```

`ThreadingHTTPServer` gets its threads, but the folds are pure Python and the GIL serializes
them. Connection churn corroborates the HTTP/1.0 finding independently: 65 TIME_WAIT vs 2
ESTABLISHED on the live server (a normal keep-alive origin on this machine shows 24
ESTABLISHED vs 2 TIME_WAIT), and 9 TIME_WAIT from 8 requests on the isolated one.

### What this changes

1. **HTTP/1.1 contributes 0.0009s of 12.4s.** T2/T3/T4 are **correctness** work — the idle-
   connection thread leak, the keep-alive desync from `_body` raising before draining, the
   double-response from the blanket excepts. They are not latency work and must stop being
   justified as such.
2. **T6 (gate `/api/mindnet/cats`) and T7 (serialize both pollers) are the lever**, not T5.
   They remove the queue everything else waits behind. `/api/mindnet/cats` returns **503 bytes
   in 6 seconds** under load — pure queueing plus fold, with no payload to blame.
3. **T5 still earns its place**, but for a narrower reason: `/api/projects` is the worst single
   endpoint at 1.86s even with no contention.
4. The original "4.0–4.5s, cold server, no browser" figure matches neither the controlled
   no-browser baseline (0.9–1.9s) nor real load (6–12.4s). **Real usage is 2–3x worse than
   reported.** Re-measure against these numbers, under these conditions.

### Verification conditions for the "after" measurement

Same two-server method, same four endpoints, browser attached with both pollers live. A
"cold server, no browser" re-measurement would report the 0.9–1.9s column and prove nothing
about what Hanif experiences.

## T7 RESULT — pollers serialized (landed 2026-08-10)

`refreshLive` and `pollCats` each raise an in-flight flag before their first `await` and clear
it in a `finally`. A tick that arrives while the previous request is still running now returns
instead of stacking a second request onto the queue.

**Behavioural proof, in a real browser against this branch's code** (preview on 54471 with
`PYTHONPATH` pointed at this worktree — the live 54461 process was never touched):

```
flag_while_in_flight   true    guard raised before the await
first_tick_ms          715     the real request
second_tick_ms         0       the overlapping tick bailed instantly
flag_after_settle      false   released by finally, not wedged
verdict                GUARD WORKS: second tick skipped, flag released
```

The pane tab reports `document.hidden = true`, so both pollers short-circuit at their
pre-existing early return on `dashboard_page.py:251` before ever reaching the new guard. The
probe overrode `document.hidden` for its duration and restored the real getter afterwards.
This is worth knowing for T12: **a backgrounded Playwright page will not exercise either
poller** unless visibility is forced.

Test: `test_both_pollers_skip_a_tick_instead_of_queueing`. Canary-checked — it **fails on
`efa3751`** ("refreshLive must skip while one of its own requests is in flight") and passes
here, so it discriminates rather than merely describing. It asserts three real failure modes:
guard absent, guard raised after the await, guard never released. Its limit is stated in the
test itself: it is a source-shape assertion, and the behavioural cover above was taken by hand,
not by CI. T11 and T12 are what make it repeatable.

Full suite green: **4511 passed**. Change-logged with explicit `--files` (the ledger caveat
about `hive log` over-claiming when `MINDHIVE_SESSION` is unset applies here).

**Not yet measured:** the end-to-end latency win. T7 alone only reduces request count when a
response exceeds the 2500 ms interval, which does not happen on an idle preview server
(0.9–1.9s). The before/after number belongs after T6 lands, taken on the live setup under the
verification conditions above.

## Design reference board

| Screen/Section | Path | Direction | Notes |
|---|---|---|---|
| Status vocabulary + skeleton shapes | `~/.gstack/projects/hanifridho62-rgb-Lintester-project/designs/status-vocabulary-20260810/board.html` | Existing dark palette, hand-built from the real hex values | Not an AI mockup — the gstack designer has no API key configured on this machine, so this was written directly from `dashboard_page.py`. It is therefore exact rather than approximate. Shows the two existing status vocabularies beside the proposed job outcomes, the six-signal header row, and the three skeleton shapes against their real content. |

## Unresolved Decisions

None. 19 DX-review + 15 eng-review + 8 cross-model + 10 design-review + 3 CEO-review = 55
decisions, all answered.

## PR-1 resequencing — CEO review, 2026-08-10

Mode: HOLD SCOPE. Coverage unchanged — all 31 tasks stand, none added, none removed. Only
the order changed, and it changed because one session produced 2 of 31 tasks while four of
the five reported complaints have PRs that are independent and unstarted.

Lane A stays serial inside itself (`dashboard.py` 1460 lines and `dashboard_page.py` 1859
lines are the shared surface — three live slices on them would conflict three ways). Lanes
B/C/D share no module with A or with each other and run in parallel, exactly as the
Worktree parallelization table above already says.

```
PR-1a  T8 -> T6 -> T9 + T11 + T31     closes P1 end to end (slow AND wrong-section)
PR-1b  T5, T2, T3, T4                 worst single endpoint + keep-alive correctness
PR-1c  T26, T28, T10                  polish
PR-2   T13..T16, T12                  closes P4 (three dead buttons)

PR-3   T17, T18      closes P2 (roadmap card never retires)  | parallel, can start now
PR-4   T19, T25      closes P3 (rotation advisory lies)      | parallel, can start now
PR-5   T20..T24      closes P5 (SOP injection proof)         | parallel, can start now
```

Three decisions this review made:

1. **Order inside PR-1a is `T8 -> T6 -> T9`, not `T8 -> T9 -> T6`.** T9 drops the nav
   disable, which raises concurrent requests; T1 measured the folds as GIL-serialized
   (4 concurrent 4.99s vs 5.3s serial). T6 removes the queue first, then nav opens. T8
   stays first — W1c already states that order is not negotiable.
2. **T11 moves from PR-2 into PR-1a.** The failure-modes table classifies the `liveFn ===`
   discard as **silent** and requires tier 1 + tier 2. Tier 2 (T12) legitimately belongs to
   PR-2, but shipping T9 with neither leaves its mandatory regression test defaulting to the
   source-string style this plan already calls vacuous. T11 is the tier-1 node test and is
   marker-fenced, so `hive selftest` still never spawns node from inside the POST handler
   ([verify.py:64](../../src/mindhive/verify.py:64)).
3. **T31 moves from PR-1c into PR-1a.** T9 removes `disabled`, today's only screen-reader
   busy signal; T31's `aria-busy`/`aria-live` is its replacement. Split across merged PRs
   they leave an a11y regression window; landed together they are one change.

No decision from the DX, eng, design, or outside-voice reviews was reopened.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | CLEAR (HOLD) | mode: HOLD_SCOPE, 0 critical gaps, 3 decisions |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 21 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR (FULL) | score: 4/10 → 8/10, 10 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 1 | ISSUES_OPEN | score: 3/10 → 7/10, TTF: unprofiled → <300ms cached paint |
| Outside Voice | Claude subagent | Independent plan challenge | 2 | ISSUES_FOUND | 26 findings across two passes |

**CODEX:** Codex CLI is not installed on this machine; both outside-voice passes ran as Claude subagents with fresh context. The gstack mockup generator is installed but unconfigured (no OpenAI key), so the design reference board was hand-built from the real palette instead — exact rather than generated.

**CROSS-MODEL:** The CEO review's central finding is one no prior pass reached: the failure-modes table marks the `liveFn ===` discard **silent** and requires tier 1 + tier 2, while both tiers were assigned to PR-2 and T9 to PR-1. Eng, design, DX, and both outside-voice passes each reviewed one dimension and none crossed the test-strategy table against the PR assignment. No prior finding was contradicted.

**VERDICT:** CEO + ENG + DESIGN CLEARED — ready to implement. DX review remains ISSUES_OPEN (score 7/10, informational, never a ship gate). PR-1 resequenced into three slices with lanes B/C/D unblocked in parallel; scope held at 31 tasks.

NO UNRESOLVED DECISIONS

**CROSS-MODEL:** The outside voice ran twice and was right both times. Pass 1 refuted three DX-review conclusions and one fact (namespace count 80 → 56), reversing the execution order, cutting an SSE endpoint and a new subcommand, and adding a mandatory profiling step. Pass 2 refuted five engineering-review conclusions: the generation guard was over-engineered (a one-line `liveFn ===` idiom already exists seven times in the same file), HTTP/1.1's real hazards were `_body` raising before draining and a blanket-except double response rather than Content-Length, `hook_decision` already ships the advisory ceiling W3 proposed to design, `default_project()` is not on the per-request hot path, and the subcommand count seeding a parity test was wrong (39, not 38). It also found that dropping the nav disable removes the only mutual exclusion on six unlocked mutating endpoints. All 8 tensions resolved toward the outside voice. The design review then found that the plan was inventing a third status vocabulary where two already existed, and that its own enum contradicted its own diagram by omitting `failed`.

**VERDICT:** ENG + DESIGN CLEARED — 31 findings folded across three reviews, 0 critical gaps, 0 unresolved. Ready to implement as five PRs.

NO UNRESOLVED DECISIONS
