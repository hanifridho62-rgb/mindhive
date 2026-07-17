---
title: "feat: Auto-launch the web dashboard when the hive-mind loop starts"
type: feat
date: 2026-07-01
status: ready
depth: standard
---

# feat: Auto-launch the web dashboard when the hive-mind loop starts

## Summary

When the user "issues the hive mind command" — i.e. starts the MindHive
autonomous loop through any of its front doors (`/circle`, `circle.ps1`, or
`hive loop`) — the local web control panel (`hive dashboard`) should come up
automatically so the run can be watched live, instead of the user having to open
a second terminal and start it by hand.

All three front doors funnel through the same engine call: `python -m mindhive
loop` → `cmd_loop` (the "run" branch). Placing the auto-launch there means every
front door inherits it for free, with a single opt-out surface. The dashboard is
spawned as a **detached background subprocess** so it (a) does not block the
loop's foreground run and (b) outlives the loop, letting the user inspect results
after the run ends. Launch is best-effort: a failure to start the dashboard must
never abort or crash the loop.

---

## Problem Frame

The dashboard is the loop's natural live-monitoring surface — the `circle` skill
already notes that "`hive project`, `state`, and the dashboard reflect [loop
events] automatically." But today the two are started independently: the user
runs the loop in one place and, if they want to watch it, must separately run
`hive dashboard` in another. The friction means the dashboard is often not open
when it would be most useful — during an unattended, file-editing autonomous run.

The fix is to couple the two: starting a real loop run should bring up the
dashboard automatically, while preserving a clean opt-out for headless/CI
contexts where no browser or panel is wanted.

---

## Assumptions

Because this plan was generated in pipeline mode (no interactive confirmation),
the following interpretation bets are recorded explicitly rather than confirmed:

- **"hive mind command" = the autonomous loop, not the dashboard's "HIVE MIND"
  unified view.** In the codebase "HIVE MIND" also names the read-only
  all-projects lens (`project="*"`) inside the dashboard. That is a *view within*
  the dashboard, not a command the user issues, so it is not the trigger here.
  The trigger is the loop the user colloquially calls "hive mind," reachable via
  `/circle`, `circle.ps1`, and `hive loop`.
- **Launch happens on a real run only** — not on `hive loop init`, `hive loop
  status`, or `hive loop --dry-run` (those do no work and cost nothing; opening a
  browser on them would be surprising).
- **Detached subprocess over in-process thread.** The dashboard is designed for
  lifecycle independence (fixed port `54461`, idempotent "already bound → just
  reopen the URL"). A subprocess persists after the loop exits; an in-process
  daemon thread would die with the loop. Persistence is the more useful default.
- **Opt-out via CLI flag + env var is sufficient**; a persistent `loop.toml`
  config toggle is deferred to follow-up (see Scope Boundaries).

If any bet is wrong, the correction is localized: the trigger point (`cmd_loop`)
and the launch helper are the only load-bearing pieces.

---

## Requirements

- **R1** — Starting a real loop run via `hive loop` (and therefore via
  `circle.ps1` and `/circle`, which both call it) launches the dashboard
  automatically.
- **R2** — The dashboard runs independently of the loop: it does not block loop
  execution and survives after the loop terminates.
- **R3** — Launch is idempotent: if the dashboard is already running on its fixed
  port, a second launch reuses the existing instance rather than erroring.
- **R4** — Auto-launch can be suppressed per-invocation (`--no-dashboard`) and
  globally (`MINDHIVE_LOOP_DASHBOARD=0`).
- **R5** — Auto-launch never fires for `init`, `status`, or `--dry-run`.
- **R6** — A dashboard-launch failure is non-fatal: the loop proceeds and the
  failure is reported, not raised.
- **R7** — User-facing docs (circle command, circle skill, README, USAGE) and the
  `circle.ps1` header describe the new behavior and its opt-out.

---

## Key Technical Decisions

- **Trigger point: `cmd_loop` run branch in `src/mindhive/__main__.py`.** It is
  the single choke point all three front doors pass through. Insert the launch
  after the `preflight` gate passes and immediately before
  `loop_engine.run_loop(...)`. This guarantees the dashboard is up before the
  first iteration streams.
- **Launch mechanism: a new `launch_background()` helper in
  `src/mindhive/dashboard.py`** that spawns `python -m mindhive dashboard` (via
  `sys.executable -m mindhive dashboard`) as a detached process. Keeping the spawn
  in the dashboard module (not inline in `__main__`) keeps `cmd_loop` thin and
  makes the behavior unit-testable by monkeypatching `subprocess.Popen`.
- **Detachment is platform-aware.** On Windows use `DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP` creation flags; on POSIX use `start_new_session=True`.
  This is the standard cross-platform detach pattern and matters because this is a
  Windows-primary project driven from `.venv\Scripts\python.exe`.
- **Idempotency is delegated to the existing server.** `serve()` already catches
  `OSError` on a bound port and reopens the stable URL instead of crashing, so the
  helper does not need its own "is it already running?" probe — a second spawn is
  safe and cheap. (Decision: rely on this existing behavior rather than adding a
  port-liveness check, keeping the helper minimal.)
- **Opt-out precedence:** `--no-dashboard` (explicit per-run) OR
  `MINDHIVE_LOOP_DASHBOARD` in `{0,false,off,no}` suppresses launch. Either alone
  is sufficient to suppress; default (neither set) launches. This mirrors the
  existing `MINDHIVE_AUTOHARVEST` env-toggle convention already in
  `dashboard.py`.
- **Non-fatal by construction.** The `cmd_loop` call site wraps the helper in
  `try/except` and prints a warning on failure; the helper itself also guards its
  spawn. A dashboard problem must degrade to "no dashboard," never "no loop."

---

## High-Level Technical Design

Control flow for a real run (front doors collapse into one path):

```
/circle skill  ─┐
circle.ps1     ─┼─►  python -m mindhive loop  ──►  cmd_loop (run branch)
hive loop      ─┘                                        │
                                                         ├─ init/status/--dry-run ──► (no launch)
                                                         │
                                                         ▼ real run
                                          opt-out? (--no-dashboard | env=0)
                                                 │yes            │no
                                                 ▼               ▼
                                             (skip)     dashboard.launch_background()
                                                                 │  spawn detached:
                                                                 │  python -m mindhive dashboard
                                                                 ▼
                                                        serve() on port 54461
                                                        (already bound → reopen URL)
                                                                 │
                                                                 ▼
                                          loop_engine.run_loop(...)  ── blocks, iterates ──►
                                          (dashboard keeps serving, survives loop exit)
```

The dashboard process and the loop process are siblings after spawn; the loop
does its blocking `run_loop`, the dashboard serves in parallel and persists.

---

## Implementation Units

### U1. Background-launch helper in the dashboard module

**Goal:** Provide a small, testable, non-fatal function that starts the dashboard
as a detached background process and reports whether it launched or was skipped.

**Requirements:** R2, R3, R6.

**Dependencies:** none.

**Files:**
- `src/mindhive/dashboard.py` (add `launch_background()`)
- `tests/test_dashboard.py` (add helper tests)

**Approach:**
- Add `launch_background(port: int | None = None) -> dict` (or a small result
  object) that:
  - Builds the command `[sys.executable, "-m", "mindhive", "dashboard"]`, appending
    `--port <port>` only when `port` is explicitly passed (default path uses the
    fixed `DEFAULT_PORT` inside `serve()`).
  - Spawns it with `subprocess.Popen` using platform-aware detach flags
    (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows via
    `subprocess.CREATE_NEW_PROCESS_GROUP`/`DETACHED_PROCESS`; `start_new_session=True`
    on POSIX), redirecting child stdio away from the parent so it does not tie up
    the loop's console.
  - Returns a small dict like `{"launched": True, "url": <stable url>}` on success
    and `{"launched": False, "reason": <str>}` on a caught failure — it swallows
    `OSError`/`Exception` from the spawn and never raises.
- Reuse `load_or_create_token()` to compute the stable URL for the return/print,
  so the reported URL matches what the spawned server will actually serve.

**Patterns to follow:** the existing `serve()` port/token handling and the
`MINDHIVE_AUTOHARVEST` env-guard idiom already in `dashboard.py`.

**Test scenarios:**
- Happy path: monkeypatch `subprocess.Popen`; assert `launch_background()` is
  called with a command ending in `-m mindhive dashboard` and returns
  `launched=True` with a `127.0.0.1:54461` URL containing a token.
- Explicit port: `launch_background(port=0)` includes `--port 0` in the spawned
  argv.
- Non-fatal: monkeypatch `Popen` to raise `OSError`; assert `launch_background()`
  returns `launched=False` and does **not** raise.
- Detach flags: on the current platform, assert the Popen kwargs include the
  expected detach argument (`creationflags` on Windows, `start_new_session=True`
  on POSIX) — guard the assertion by `os.name` so it is meaningful cross-platform.

**Verification:** `pytest tests/test_dashboard.py` passes; no real browser/process
is spawned during the test run (Popen is stubbed).

### U2. Wire auto-launch into the loop run path

**Goal:** Make a real `hive loop` run launch the dashboard first, with the flag +
env opt-outs, and leave `init`/`status`/`--dry-run` untouched.

**Requirements:** R1, R4, R5, R6.

**Dependencies:** U1.

**Files:**
- `src/mindhive/__main__.py` (`cmd_loop` and the `loop` subparser in
  `_build_parser`)
- `tests/test_loop_cli.py` (add launch/opt-out tests)

**Approach:**
- Add a `--no-dashboard` flag to the `loop` subparser (`dest="no_dashboard",
  action="store_true"`), documented as "don't auto-open the web dashboard for this
  run."
- In `cmd_loop`, in the **run** branch only (after `init`/`status`/`--dry-run`
  early-returns and after the `preflight` success gate, before
  `loop_engine.run_loop(...)`):
  - Compute `want_dashboard = not args.no_dashboard and
    os.environ.get("MINDHIVE_LOOP_DASHBOARD", "1").lower() not in
    ("0","false","off","no")`.
  - If `want_dashboard`: `try:` import `dashboard`, call `launch_background()`,
    print the returned URL (e.g. `dashboard: <url>`); `except Exception as e:`
    print a non-fatal warning and continue.
- Do not touch the `init`, `status`, or `--dry-run` code paths — the guard's
  placement after their early returns already excludes them.

**Patterns to follow:** existing local-import style inside `cmd_loop`; the
`--dry-run`/`--live`/`--force` flag declarations on the `loop` subparser.

**Test scenarios:**
- Real run launches: monkeypatch `loop_driver.preflight`→True,
  `loop_engine.run_loop`→a stub returning `{"reason":"completed"}`, and
  `dashboard.launch_background`→a spy; run `main(["loop"])`; assert the spy was
  called exactly once and the printed output contains the dashboard URL.
- `--dry-run` does not launch: with the launch spy installed, `main(["loop",
  "--dry-run"])` leaves the spy uncalled.
- `init` and `status` do not launch: spy uncalled for `main(["loop","init"])` and
  `main(["loop","status"])`.
- `--no-dashboard` suppresses: real-run stubs installed, `main(["loop",
  "--no-dashboard"])` leaves the spy uncalled but still calls `run_loop`.
- Env opt-out: `MINDHIVE_LOOP_DASHBOARD=0` leaves the spy uncalled on a real run.
- Non-fatal: `launch_background` stubbed to raise; the run still reaches
  `run_loop` and `main` returns its normal exit code (loop not aborted).

**Verification:** `pytest tests/test_loop_cli.py tests/test_dashboard.py` passes;
no real dashboard process starts under test.

### U3. Document the coupled behavior and its opt-out

**Goal:** Make the auto-launch discoverable and its opt-out obvious across the
loop's user-facing surfaces.

**Requirements:** R7.

**Dependencies:** U2 (document the actual flag/env names as implemented).

**Files:**
- `commands/circle.md` (note auto-launch in the Launch step)
- `skills/circle/SKILL.md` (note auto-launch + opt-out in Steps/Notes)
- `circle.ps1` (header comment: mention the dashboard now comes up automatically)
- `README.md` and `USAGE.md` (short note tying the loop to the dashboard)

**Approach:** Add one or two concise sentences to each surface: starting the loop
now brings up the web dashboard automatically at the stable URL; suppress with
`--no-dashboard` or `MINDHIVE_LOOP_DASHBOARD=0`. Keep wording consistent with the
existing tone of each file. No behavioral code here.

**Patterns to follow:** the existing step/notes phrasing in `commands/circle.md`
and `skills/circle/SKILL.md`; the README's existing "HIVE MIND" and dashboard
descriptions.

**Test scenarios:** `Test expectation: none -- documentation only, no behavioral
change.` (Optionally, if the repo has a docs-consistency check like
`test_circle_skill.py`, ensure edits keep it green.)

**Verification:** Docs read correctly; `pytest tests/test_circle_skill.py` (if it
asserts on skill content) still passes.

---

## Scope Boundaries

### In scope
- Auto-launching the dashboard from the loop run path, opt-outs, non-fatal
  behavior, and doc updates.

### Deferred to Follow-Up Work
- **Persistent `loop.toml` toggle** (e.g. `[loop] launch_dashboard = false`).
  Env + flag cover the opt-out need now; a config field adds precedence
  complexity best handled separately if demand appears.
- **Auto-stopping the dashboard when the loop ends.** Current design intentionally
  lets it persist for post-run inspection; a "tear down on exit" option is a
  separate preference.
- **Auto-selecting the current project in the opened dashboard.** The dashboard
  opens on its default (most-active) project; deep-linking the loop's project into
  the URL is a nice-to-have, not required.

### Out of scope
- Any change to the dashboard's HTTP routes, HIVE MIND unified view, or the
  auto-harvest loop.
- Changing how `/circle` or `circle.ps1` themselves drive the loop beyond adding
  documentation.

---

## Risks & Dependencies

- **Orphaned dashboard processes.** Because the dashboard is detached and
  persists, repeated loop runs could leave a process running. Mitigated by the
  fixed port + idempotent reuse (a second launch does not spawn a competing
  server; it reopens the URL). Acceptable given the single-user desktop context.
- **Browser spam on repeated runs.** `serve()` opens a browser tab on each
  launch. Because the port is reused, subsequent launches hit the "already bound"
  path which also opens the URL — so rapid re-runs could open multiple tabs. If
  this proves annoying, a follow-up can add a "already-running → skip browser"
  refinement; not blocking for v1.
- **Windows detach flag correctness.** `DETACHED_PROCESS` semantics must be
  verified so the child truly survives the parent and does not hold the console.
  Covered by U1's detach-flag test and manual smoke of a real run.
- **Dependency:** relies on `hive dashboard`/`serve()` existing and its
  fixed-port reuse behavior — both already present in `src/mindhive/dashboard.py`.

---

## Verification Strategy

- Unit: `pytest tests/test_dashboard.py tests/test_loop_cli.py` (Popen and
  engine stubbed — no real process/browser).
- Full suite: `python -m mindhive selftest` / `pytest` stays green.
- Manual smoke (Windows): `.\circle.ps1 --dry-run` opens **no** dashboard; a real
  `python -m mindhive loop` (with a trivial `.hive-loop` task) opens the panel at
  `http://127.0.0.1:54461/?token=…` and the loop still runs to completion;
  `python -m mindhive loop --no-dashboard` runs with no panel.
