# SPDX-License-Identifier: Apache-2.0
# src/mindhive/autoresume.py
"""Auto-resume: deliver the interruption marker straight back into Claude.

The roadmap registry (roadmap.py) already preserves the creation context of
work that was cut off mid-plan — the original ask, the plan, and how far it
got — as the STATE.md "⚠ Interrupted plan" marker. That marker is passive: it
waits for the *next* session to auto-load it. This module adds the delivery
leg the marker was missing: when the interruption cause is a Claude usage
limit, the marker is **scheduled** to re-enter Claude by itself once the
usage window reopens (the ~5-hour cooldown), instead of waiting for a human
to come back.

The lifecycle is three ``autoresume`` events (deterministic telemetry — the
roadmap events stay the semantic record):

- ``scheduled`` — when the delivery is due, which attempt this is, and where
  the resumed work should run (cwd). Best-effort registers a per-user Windows
  scheduled task that runs ``hive autoresume fire <plan_id>`` at the due
  time; when no OS timer can be registered, ``hive autoresume wait`` is the
  portable blocking fallback.
- ``fired`` — the delivery happened: the marker was composed into a resume
  prompt (the ask, per-step progress, the NEXT pointer, and directives to
  keep the roadmap updated) and fed to a headless ``claude -p``. Outcome is
  ``delivered`` / ``still_limited`` / ``error`` / ``skipped``; a
  still-limited delivery re-schedules itself until MAX_ATTEMPTS -- a
  one-shot after a short backoff, a relaunched loop one cooldown after it
  returned, where only a relaunch that checked off nothing new on its
  fix_plan checklist spends an attempt. Whatever a human did while the
  delivery ran -- cancelled it, re-scheduled it by hand, retired the plan
  -- is honoured over that re-arm.
- ``cancelled`` — a human picked the plan back up (``hive roadmap resume`` /
  ``complete``), so the pending delivery and its OS timer are withdrawn.

Like every registry here, current state is a deterministic, total fold over
the events; reads never raise on historical rows.
"""
import os
import subprocess
import sys
import time
import calendar
import contextvars

from . import events as ev_mod
from . import claude_cli
from . import paths
from . import roadmap
from .sop import ID_RE

ACTIONS = ("scheduled", "fired", "cancelled")
OUTCOMES = ("delivered", "still_limited", "error", "skipped")

# How a delivery re-enters Claude. "prompt" (default) composes the marker
# into a one-shot headless `claude -p` — right for resuming interactive
# work. "loop" relaunches the autonomous loop engine on the recorded
# goal_dir — right for loop/fabrication runs, which get back the breaker,
# budget gates, and exit contract a bare prompt lacks.
MODES = ("prompt", "loop")

# Claude usage windows reopen on a ~5-hour cycle; a still-limited delivery
# retries hourly (clock skew, weekly caps) up to MAX_ATTEMPTS.
COOLDOWN_HOURS = 5.0
RETRY_HOURS = 1.0
MAX_ATTEMPTS = 6
MIN_LEAD_S = 60           # never register a timer in the past

# The delivered session does real work (edits files, runs tests), so it gets
# a working permission mode and a work-sized timeout — unlike the reflection
# engine's read-only 180s calls.
PERMISSION_MODE = "acceptEdits"
FIRE_TIMEOUT_S = 3600

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Size caps bound every event the same way roadmap.py's do.
MAX_DETAIL_CHARS = 500
MAX_PATH_CHARS = 500
MAX_MODEL_CHARS = 100

# The checklist marks a loop chain carries from one wake-up to the next
# (`checked_high`, the most fix_plan items it has seen checked; `checked_left`,
# how many the last relaunch left checked) are bounded too: no checklist has
# this many items, and a hand-written mark past every reachable count would
# freeze progress detection for good.
MAX_CHECKED_ITEMS = 100_000
MARKS = ("checked_high", "checked_left")

# Interrupt causes that mean "Claude usage ran out" — the ones worth an
# automatic delivery. session_end (a normal close) deliberately matches
# neither the set nor the substrings.
USAGE_CAUSES = frozenset(
    {"usage_limit", "api_cap", "budget_cap", "rate_limit", "out_of_usage"})

# Error-text markers that mean the usage window has NOT reopened yet (429 /
# 529 / the CLI's own limit wording) — retry later instead of giving up.
_LIMIT_MARKERS = ("usage limit", "rate limit", "out of usage", "weekly limit",
                  "hit your limit", "429", "529", "overloaded", "resets at")


def is_usage_cause(cause) -> bool:
    """True when an interrupt cause is usage-shaped (worth auto-delivery)."""
    c = str(cause or "").strip().lower()
    return c in USAGE_CAUSES or "usage" in c or "limit" in c


# --- timestamps ---------------------------------------------------------------

def _parse_ts(ts) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(ts), TS_FMT)))
    except (ValueError, TypeError):
        return None


def _iso(epoch: float) -> str:
    return time.strftime(TS_FMT, time.gmtime(epoch))


def due_at(interrupted_at, cooldown_hours=COOLDOWN_HOURS, now=None) -> str:
    """The delivery time: interruption + cooldown, but never in the past —
    a marker scheduled after the cooldown already elapsed fires almost
    immediately (MIN_LEAD_S) instead of registering a dead timer."""
    now = time.time() if now is None else now
    base = _parse_ts(interrupted_at)
    due = (now if base is None else base) + float(cooldown_hours) * 3600
    return _iso(max(due, now + MIN_LEAD_S))


# --- payload contract ---------------------------------------------------------

def _mark(value):
    """A checklist mark as the contract reads it -- an int within the bound --
    or None for anything else (absent, a bool, past the bound)."""
    if isinstance(value, int) and not isinstance(value, bool) \
            and 0 <= value <= MAX_CHECKED_ITEMS:
        return value
    return None


def validate_payload(payload) -> None:
    """Enforce the autoresume event contract at every append surface (the
    module's own writers and the raw ``hive append autoresume`` path). The
    event store stays type-agnostic — the contract lives here, mirroring
    changelog.py / sop.py / roadmap.py."""
    if not isinstance(payload, dict):
        raise ValueError("autoresume payload must be an object")
    action = payload.get("action")
    if action not in ACTIONS:
        raise ValueError(
            "autoresume requires an action — one of: " + ", ".join(ACTIONS))
    if not ID_RE.match(payload.get("plan_id") or ""):
        raise ValueError(
            "autoresume requires the kebab-case plan_id of the roadmap "
            "whose marker it delivers")
    if action in ("scheduled", "fired"):
        attempt = payload.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) \
                or attempt < 1:
            raise ValueError("autoresume requires an integer attempt >= 1")
    if action == "scheduled":
        if _parse_ts(payload.get("due")) is None:
            raise ValueError(
                "autoresume scheduled requires a due timestamp "
                "(UTC, e.g. 2026-07-18T15:00:00Z)")
        cooldown = payload.get("cooldown_hours")
        if cooldown is not None and (
                not isinstance(cooldown, (int, float))
                or isinstance(cooldown, bool) or cooldown <= 0):
            raise ValueError("autoresume cooldown_hours must be > 0")
        if len(str(payload.get("cwd") or "")) > MAX_PATH_CHARS:
            raise ValueError(f"autoresume cwd exceeds {MAX_PATH_CHARS} chars")
        if len(str(payload.get("model") or "")) > MAX_MODEL_CHARS:
            raise ValueError(
                f"autoresume model exceeds {MAX_MODEL_CHARS} chars")
        timer = payload.get("timer")
        if timer is not None and timer not in ("windows_task", "none"):
            raise ValueError(
                "autoresume timer must be 'windows_task' or 'none'")
        mode = payload.get("mode")
        if mode is not None and mode not in MODES:
            raise ValueError(
                "autoresume mode must be one of: " + ", ".join(MODES))
        if len(str(payload.get("goal_dir") or "")) > MAX_PATH_CHARS:
            raise ValueError(
                f"autoresume goal_dir exceeds {MAX_PATH_CHARS} chars")
        if mode == "loop" and not str(payload.get("goal_dir") or "").strip():
            raise ValueError(
                "autoresume mode 'loop' requires the goal_dir to relaunch")
        for key in MARKS:
            if payload.get(key) is not None and _mark(payload[key]) is None:
                raise ValueError(
                    f"autoresume {key} must be an integer from 0 to "
                    f"{MAX_CHECKED_ITEMS}")
    elif action == "fired":
        if payload.get("outcome") not in OUTCOMES:
            raise ValueError(
                "autoresume fired requires an outcome — one of: "
                + ", ".join(OUTCOMES))
        if len(str(payload.get("detail") or "")) > MAX_DETAIL_CHARS:
            raise ValueError(
                f"autoresume detail exceeds {MAX_DETAIL_CHARS} chars")
    # cancelled: plan_id is enough.


# --- fold ---------------------------------------------------------------------

def _blank(plan_id, ts) -> dict:
    return {"plan_id": plan_id, "status": "", "due": "", "attempt": 1,
            "cooldown_hours": COOLDOWN_HOURS, "cwd": "", "model": "",
            "timer": "none", "mode": "prompt", "goal_dir": "",
            "outcome": "", "detail": "", "created": ts, "updated": ts}


def fold(evs) -> dict:
    """Deterministic fold of autoresume events into plan_id -> delivery
    record. Total — malformed rows (bad due, bad attempt, unknown outcome or
    action) are skipped, never raised on; a fired/cancelled row without a
    prior schedule still folds (history written by other code versions)."""
    recs: dict = {}
    for ev in evs:
        if ev.type != "autoresume" or not isinstance(ev.payload, dict):
            continue
        p = ev.payload
        action, plan_id = p.get("action"), p.get("plan_id")
        if action not in ACTIONS or not plan_id:
            continue
        rec = recs.get(plan_id) or _blank(plan_id, ev.ts)
        rec = dict(rec)
        if action == "scheduled":
            attempt = p.get("attempt")
            if _parse_ts(p.get("due")) is None or not isinstance(attempt, int) \
                    or isinstance(attempt, bool) or attempt < 1:
                continue
            cooldown = p.get("cooldown_hours")
            rec.update(
                status="scheduled", due=str(p["due"]), attempt=attempt,
                cooldown_hours=float(cooldown) if isinstance(
                    cooldown, (int, float)) and not isinstance(cooldown, bool)
                and cooldown > 0 else COOLDOWN_HOURS,
                cwd=str(p.get("cwd") or "")[:MAX_PATH_CHARS],
                model=str(p.get("model") or "")[:MAX_MODEL_CHARS],
                timer=p.get("timer") if p.get("timer") in
                ("windows_task", "none") else "none",
                mode=p.get("mode") if p.get("mode") in MODES else "prompt",
                goal_dir=str(p.get("goal_dir") or "")[:MAX_PATH_CHARS],
                outcome="", detail="")
            # A loop chain's marks ride only the schedule that carried them
            # forward: one without them starts the count over.
            for key in MARKS:
                mark = _mark(p.get(key))
                if mark is None:
                    rec.pop(key, None)
                else:
                    rec[key] = mark
        elif action == "fired":
            outcome = p.get("outcome")
            if outcome not in OUTCOMES:
                continue
            attempt = p.get("attempt")
            if isinstance(attempt, int) and not isinstance(attempt, bool) \
                    and attempt >= 1:
                rec["attempt"] = attempt
            rec.update(status="fired", outcome=outcome,
                       detail=str(p.get("detail") or "")[:MAX_DETAIL_CHARS])
        elif action == "cancelled":
            rec.update(status="cancelled")
        if action != "scheduled":
            # The marks belong to a pending chain; a delivered, given-up or
            # cancelled record carries none.
            for key in MARKS:
                rec.pop(key, None)
        rec["updated"] = ev.ts
        recs[plan_id] = rec
    return recs


def pending(evs) -> list:
    """Deliveries still waiting to fire, earliest due first."""
    return sorted((r for r in fold(evs).values()
                   if r["status"] == "scheduled"),
                  key=lambda r: (r["due"], r["plan_id"]))


# --- the resume prompt (the marker, composed for Claude) ----------------------

_GLYPHS = {"done": "[x]", "active": "[>]", "pending": "[ ]", "skipped": "[-]"}


def rehydrate_spent(rec, evs) -> frozenset:
    """The spent-fingerprint set a resumed run should start from.

    The UNION of the fresh fold and the interrupt's cache, and the fold is the
    authority. `replenish` has no sidecar precisely because a second source of
    truth drifts invisibly, so this cache is allowed to ADD to the spent set and
    never to remove from it: the worst a stale or truncated cache can do is skip a
    candidate the ledger would also have skipped, while a cache that could
    un-spend a fingerprint would silently re-propose finished work.

    Total on a malformed record: a corrupt or non-list cache is ignored and the
    fold alone is used."""
    from . import replenish as replenish_mod

    folded = replenish_mod.history(evs).fingerprints
    cached = rec.get("spent_fingerprints") if isinstance(rec, dict) else None
    if not isinstance(cached, list):
        return folded
    return folded | frozenset(f for f in cached
                              if isinstance(f, str) and f.strip())


def resume_prompt(rec, attempt=1) -> str:
    """Compose the interrupted plan into the self-contained prompt that goes
    straight to Claude: the original ask verbatim, every step with its index
    and status (so `hive roadmap step` commands are copy-pastable), the NEXT
    pointer, and spec-shaped directives. Size is bounded by the roadmap
    contract caps (prompt <= 2000 chars, <= 50 steps of <= 300 chars)."""
    p = rec["progress"]
    pid = rec["plan_id"]
    nxt = roadmap.next_step(rec)
    lines = [
        "# AUTO-RESUME — continue interrupted work (delivered by MindHive)",
        "",
        f"The previous session was cut off before this plan finished "
        f"(cause: {rec['cause'] or 'unknown'}, "
        f"at {rec['interrupted_at'] or 'unknown'}). This marker was "
        f"scheduled to re-enter Claude after the usage cooldown; this is "
        f"delivery attempt {attempt}.",
        "",
    ]
    # A loop-origin plan says what stopped it and what the marker means. Only when
    # the interrupt actually carried a relay — a session-origin plan, or any
    # interrupt written before the relay existed, reads exactly as it did before.
    relay = str(rec.get("relay_context") or "").strip()
    if relay:
        lines += [
            "## You are resuming an autonomous loop run",
            relay,
            "",
            "Steps below carrying the `(new)` marker were mined from the record "
            "by the replenishment injector rather than planned at the start. "
            "They are work the ledger proved outstanding; treat them as real "
            "requirements, not suggestions.",
            "",
        ]
    lines += [
        "## The original ask (what the user wants — verbatim)",
        rec["prompt"],
        "",
        f"## The plan and its progress ({p['done']}/{p['total']} steps done)",
    ]
    for i, s in enumerate(rec["steps"]):
        note = f" — {s['note']}" if s.get("note") else ""
        mark = "  <- NEXT" if s is nxt else ""
        # This prompt IS the relay: it re-enters Claude carrying the plan. An
        # agent picking the work back up has to know which steps were not in the
        # plan the session started from.
        lines.append(f"{i}. {_GLYPHS[s['status']]} {roadmap.new_prefix(s)}"
                     f"{s['text']}{note}{mark}")
    lines += [
        "",
        "## Directives",
        "1. Continue from the NEXT step above — do not start over, and do "
        "not re-plan the finished steps.",
        f"2. First mark the plan live again: `hive roadmap resume {pid}`.",
        f"3. As each step lands, record it: `hive roadmap step {pid} "
        f"<index> --status done --note \"what landed\"` "
        "(indexes as listed above).",
        f"4. When every step is done: `hive roadmap complete {pid}`. If "
        f"you must stop early again: `hive roadmap interrupt {pid} "
        "--cause <why>`.",
        "5. Follow the repository's own instructions (CLAUDE.md): tests "
        "must be green before the work is called done, and any change to "
        "src/mindhive/ or tests/ needs a `hive log` change-log entry.",
    ]
    return "\n".join(lines)


# --- OS timer (best-effort; the scheduled event is the source of truth) -------

def _timer_enabled() -> bool:
    # MINDHIVE_AUTORESUME_TIMER=off turns real Task Scheduler registration
    # off (the test suite sets it; useful for air-gapped runs too).
    return os.environ.get("MINDHIVE_AUTORESUME_TIMER", "").lower() \
        not in ("off", "0", "none")


def _task_name(plan_id) -> str:
    return f"MindHive AutoResume {plan_id}"


def _ps_quote(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _run_powershell(script: str) -> bool:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             script],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode == 0
    except Exception:
        return False


def register_windows_task(task_name, due_iso, cwd, argument) -> str:
    """Register a per-user Windows scheduled task running
    ``<this python> <argument>`` at the due time (converted to local time —
    Task Scheduler triggers are local). The one OS-timer primitive, shared
    with fabricate.py's dispatcher. Returns "windows_task" on success,
    "none" on any failure or off-platform — callers record their event
    either way, because the ledger event is the source of truth and
    `hive autoresume wait` is the delivery fallback."""
    if not _timer_enabled() or not sys.platform.startswith("win"):
        return "none"
    epoch = _parse_ts(due_iso)
    if epoch is None:
        return "none"
    local = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))
    action = (f"$a=New-ScheduledTaskAction -Execute "
              f"{_ps_quote(sys.executable)} -Argument {_ps_quote(argument)}")
    if cwd:
        action += f" -WorkingDirectory {_ps_quote(cwd)}"
    script = (
        f"{action}; "
        f"$t=New-ScheduledTaskTrigger -Once -At ([datetime]::ParseExact("
        f"{_ps_quote(local)},'yyyy-MM-ddTHH:mm:ss',$null)); "
        f"Register-ScheduledTask -TaskName {_ps_quote(task_name)} "
        f"-Action $a -Trigger $t -Force | Out-Null")
    return "windows_task" if _run_powershell(script) else "none"


def unregister_windows_task(task_name) -> None:
    if not _timer_enabled() or not sys.platform.startswith("win"):
        return
    _run_powershell(
        f"Unregister-ScheduledTask -TaskName {_ps_quote(task_name)} "
        f"-Confirm:$false -ErrorAction SilentlyContinue")


def _register_timer(plan_id, due_iso, cwd, project) -> str:
    arg = f"-m mindhive autoresume fire {plan_id}"
    if project:
        arg += f" --project {project}"
    return register_windows_task(_task_name(plan_id), due_iso, cwd, arg)


def _unregister_timer(plan_id) -> None:
    unregister_windows_task(_task_name(plan_id))


# --- lifecycle: schedule / cancel / fire / wait -------------------------------

def schedule(plan_id, *, project=None, evs=None, cooldown_hours=None, at=None,
             attempt=1, model=None, cwd=None, mode=None, goal_dir=None,
             checked_high=None, checked_left=None, session_id=None,
             register=None, now=None):
    """Schedule the plan's marker to re-enter Claude. Guards mirror
    roadmap.record: the plan must exist and be interrupted — auto-resume
    delivers interruption markers, nothing else. Re-scheduling replaces the
    pending delivery (and the -Force'd OS task); ``mode`` None keeps the mode
    and goal dir of the plan's last delivery record -- a re-schedule moves
    the delivery, it does not change how the plan resumes -- and a plan
    never scheduled resumes in prompt mode. The two marks carry a loop
    chain's progress state from one wake-up to the next (see `_progress`).
    Returns the appended event."""
    if evs is None:
        evs = ev_mod.read_all(project)
    if mode is None:
        last = fold(evs).get(plan_id) or {}
        mode = last.get("mode") or "prompt"
        goal_dir = goal_dir or last.get("goal_dir")
    rec = roadmap.get(evs, plan_id)
    if rec is None:
        raise ValueError(
            f"no roadmap '{plan_id}' — the marker auto-resume delivers is a "
            "roadmap record; declare and interrupt it first "
            "(hive roadmap set … / hive roadmap interrupt …)")
    if rec["status"] != "interrupted":
        raise ValueError(
            f"roadmap '{plan_id}' is not interrupted ({rec['status']}) — "
            "auto-resume delivers interruption markers only")
    cooldown = COOLDOWN_HOURS if cooldown_hours is None else cooldown_hours
    now = time.time() if now is None else now
    if at is not None:
        e = _parse_ts(at)
        if e is None:
            raise ValueError(
                "autoresume --at must be a UTC timestamp like "
                "2026-07-18T15:00:00Z")
        due = _iso(max(e, now + MIN_LEAD_S))
    else:
        due = due_at(rec["interrupted_at"], cooldown, now=now)
    cwd = str(cwd or os.getcwd())
    # The OS task fires in a fresh process where neither today's env vars nor
    # this cwd's namespace detection are guaranteed — bake the resolved
    # project into the task's arguments so it reads THIS store.
    try:
        effective_project = project or paths.detect_project()
    except Exception:
        effective_project = project
    try:
        timer = (register or _register_timer)(plan_id, due, cwd,
                                              effective_project) or "none"
    except Exception:
        timer = "none"        # the event is the source of truth; wait() covers
    payload = {"action": "scheduled", "plan_id": plan_id, "due": due,
               "attempt": attempt, "cooldown_hours": float(cooldown),
               "cwd": cwd[:MAX_PATH_CHARS],
               "model": str(model or "")[:MAX_MODEL_CHARS], "timer": timer,
               "mode": mode,
               "goal_dir": str(goal_dir or "")[:MAX_PATH_CHARS]}
    for key, mark in zip(MARKS, (checked_high, checked_left)):
        if mark is not None:
            payload[key] = mark
    validate_payload(payload)
    return ev_mod.append_event("autoresume", payload, session_id=session_id,
                               tags=["autoresume"], project=project)


def cancel(plan_id, *, project=None, evs=None, session_id=None,
           unregister=None):
    """Withdraw a pending delivery (and its OS timer). Raises when nothing
    is scheduled — cancelling a fired or unknown delivery is a typo."""
    if evs is None:
        evs = ev_mod.read_all(project)
    rec = fold(evs).get(plan_id)
    if rec is None or rec["status"] != "scheduled":
        raise ValueError(
            f"no pending auto-resume for '{plan_id}' — nothing to cancel")
    payload = {"action": "cancelled", "plan_id": plan_id}
    validate_payload(payload)
    ev = ev_mod.append_event("autoresume", payload, session_id=session_id,
                             tags=["autoresume"], project=project)
    try:
        (unregister or _unregister_timer)(plan_id)
    except Exception:
        pass
    return ev


def _run_claude(prompt, model=None, cwd=None, timeout_s=FIRE_TIMEOUT_S):
    return claude_cli.run_headless(prompt, model=model, timeout_s=timeout_s,
                                   permission_mode=PERMISSION_MODE, cwd=cwd)


def _classify(res) -> tuple:
    """Map a headless-run envelope to (outcome, detail). A parseable
    non-error envelope is a delivery; limit-shaped errors mean the window
    has not reopened (retry later); everything else is a terminal error."""
    stdout = (res or {}).get("stdout", "")
    if claude_cli.envelope_ok(stdout):
        from .briefing import headline
        return "delivered", headline(claude_cli.result_text(stdout)) or "ok"
    code = (res or {}).get("exit")
    err = claude_cli.error_text(stdout) or (
        "claude CLI not found" if code == 127
        else "timed out" if code == 124 else f"exit {code}")
    if any(m in err.lower() for m in _LIMIT_MARKERS):
        return "still_limited", err
    return "error", err


# The attempt of the loop-mode wake-up running in this context, None outside
# one: `fire` sets it around the relaunch, and the engine's interruption
# snapshot reads it through `waking_attempt` before re-arming anything.
_WAKING = contextvars.ContextVar("mindhive_autoresume_waking", default=None)


def waking_attempt():
    """The attempt number of the loop-mode wake-up this code runs inside, or
    None outside one. A relaunched run that is capped again must not re-arm
    itself: its fresh schedule would restart the count at attempt 1 on every
    relaunch -- a capped loop waking every cooldown with no end. Inside a
    wake-up `fire` re-arms instead, and spends an attempt unless the
    relaunch checked off something new on its checklist, until
    MAX_ATTEMPTS."""
    return _WAKING.get()


def _checked_items(goal_dir):
    """How many items the goal dir's fix_plan.md has checked off -- the
    loop's own record of how far it got -- or None when it cannot be read.
    Not the roadmap's done count: a session that adopts the interrupted plan
    while the loop sleeps re-writes its steps with its own todos, and the
    roadmap keeps at most roadmap.MAX_STEPS of them. Total: whatever the
    read or the parse raises, the checklist counts as unreadable; the count
    is capped at the bound the contract puts on a carried mark."""
    from pathlib import Path
    from .loop import task as task_mod
    try:
        plan = Path(goal_dir) / "fix_plan.md"
        text = plan.read_text(encoding="utf-8", errors="replace") \
            if plan.exists() else ""
        return min(len(task_mod.plan_items(text)["done"]), MAX_CHECKED_ITEMS)
    except Exception:                                       # noqa: BLE001
        return None


def _progress(sched, before, after) -> tuple:
    """Whether a relaunch got somewhere, and the marks its re-arm carries.

    Progress is a checked count past the most this chain has ever seen
    (`checked_high`), so a step checked and un-checked by turns never counts
    twice, nor does work checked off between wake-ups (`before` raises the
    mark first). One reading lowers it: fewer items checked at this wake-up
    than the last relaunch left (`checked_left`) were un-checked while the
    loop slept -- a human reopening work -- and re-doing those is progress,
    so the mark drops by as many. A count that cannot be read is no
    progress, and sets no mark."""
    high, left = sched.get("checked_high"), sched.get("checked_left")
    if None not in (high, left, before) and before < left:
        high -= left - before
    seen = [n for n in (high, before) if n is not None]
    progressed = after is not None and bool(seen) and after > max(seen)
    return progressed, {
        "checked_high": max(seen + ([] if after is None else [after]),
                            default=None),
        "checked_left": after}


def _run_loop_delivery(plan_id, goal_dir, project) -> tuple:
    """Loop-mode delivery: relaunch the autonomous loop engine on its goal
    dir — for loop-originated interruptions and fabrication runs, which get
    back the breaker, budget gates, and exit contract a one-shot prompt
    lacks. A plan that is a fabrication's fab_id wakes through
    `fabricate.run` instead, so the launch records its `run` event and a
    `completed` one closes the roadmap and drops the dispatch timer. Lazy
    imports avoid a cycle (the engine and fabricate import this module)."""
    from pathlib import Path
    from . import fabricate as fabricate_mod
    from .loop import config as loop_config
    from .loop import driver as loop_driver
    from .loop import engine as loop_engine
    if (fabricate_mod.fold(ev_mod.read_all(project)).get(plan_id)
            or {}).get("goal_dir"):
        try:
            p = fabricate_mod.run(plan_id, project=project)
        except ValueError as e:              # PROMPT.md gone: not runnable
            return "error", str(e)
        reason = p["reason"]
        detail = f"loop {reason} after {p['iterations']} iteration(s)"
        if reason in ("spawn_failed", "guard_refused"):
            return "error", detail
        if reason in loop_engine.USAGE_INTERRUPT_REASONS:
            return "still_limited", detail
        return "delivered", detail
    if not loop_driver.preflight("claude"):
        return "error", "claude CLI not found"
    cfg = loop_config.load_config(Path(goal_dir) / "loop.toml")
    try:
        result = loop_engine.run_loop(
            project=project, loop_dir=Path(goal_dir), cfg=cfg,
            driver=loop_driver.CLIDriver("claude"), plan_id=plan_id)
    except RuntimeError as e:            # LIVE_LOCK guard refusal
        return "error", str(e)
    reason = result.get("reason", "")
    detail = f"loop {reason} after {result.get('iterations', 0)} iteration(s)"
    if reason in loop_engine.USAGE_INTERRUPT_REASONS:
        return "still_limited", detail
    return "delivered", detail


def fire(plan_id, *, project=None, session_id=None, runner=None,
         loop_runner=None, timeout_s=None, now=None, register=None,
         unregister=None) -> dict:
    """Deliver the marker now (normally invoked by the OS timer at the due
    time). Re-checks the plan first: one resumed or completed by a human is
    skipped, never re-delivered. A still-limited delivery re-schedules
    itself until MAX_ATTEMPTS: a one-shot RETRY_HOURS later, a relaunched
    loop one cooldown after the relaunch returned. The relaunched run's own
    snapshot leaves that re-arming to this call (`waking_attempt`), which
    counts the attempts: a relaunch that left more items checked on its
    fix_plan checklist than the chain had ever seen restarts the count, one
    that did not spends one (`_progress`). What a human did while the
    delivery ran decides over all of that (`_settle`). Returns the fired
    payload."""
    evs = ev_mod.read_all(project)
    sched = fold(evs).get(plan_id)
    attempt = sched["attempt"] if sched else 1
    rec = roadmap.get(evs, plan_id)
    if rec is None or rec["status"] != "interrupted":
        payload = {"action": "fired", "plan_id": plan_id, "attempt": attempt,
                   "outcome": "skipped",
                   "detail": "roadmap missing or no longer interrupted — "
                             "nothing to deliver"}
        validate_payload(payload)
        ev_mod.append_event("autoresume", payload, session_id=session_id,
                            tags=["autoresume"], project=project)
        try:
            (unregister or _unregister_timer)(plan_id)
        except Exception:
            pass
        return payload
    goal_dir = (sched or {}).get("goal_dir") or ""
    if (sched or {}).get("mode") == "loop" and goal_dir \
            and os.path.isdir(goal_dir):
        before = _checked_items(goal_dir)
        waking = _WAKING.set(attempt)
        try:
            outcome, detail = (loop_runner or _run_loop_delivery)(
                plan_id, goal_dir, project)
        except Exception as e:                          # noqa: BLE001
            # The OS timer ran this once and will not again: whatever the
            # delivery raised -- a malformed loop.toml, one it cannot read --
            # is recorded as its outcome, so the plan never reads as
            # scheduled while nothing is.
            outcome, detail = "error", f"{type(e).__name__}: {e}"
        finally:
            _WAKING.reset(waking)
        # Only a wake-up that checked off nothing new spends an attempt: the
        # bound is for a loop capped without getting anywhere, not for one
        # capped while it works through a plan longer than MAX_ATTEMPTS.
        progressed, marks = _progress(sched, before, _checked_items(goal_dir))
        # Due one cooldown after the relaunch returned: the window has just
        # closed on it. Not after the recorded interruption -- a relaunch
        # whose best-effort snapshot recorded none would re-arm off the old
        # one, long past its cooldown, and wake a minute later into a window
        # that is still closed.
        nxt = dict(attempt=1 if progressed else attempt + 1,
                   cooldown_hours=sched["cooldown_hours"], cwd=sched["cwd"],
                   mode="loop", goal_dir=goal_dir, **marks)
        wait_s = sched["cooldown_hours"] * 3600
        gave_up = (f"{MAX_ATTEMPTS} wake-ups in a row checked off nothing "
                   "new")
    else:
        # Prompt-mode delivery (also the degrade path when a loop schedule's
        # goal dir vanished: the composed marker still works as a one-shot).
        prompt = resume_prompt(rec, attempt=attempt)
        cwd = (sched or {}).get("cwd") or None
        if cwd and not os.path.isdir(cwd):
            cwd = None        # the worktree moved; run from the default cwd
        model = (sched or {}).get("model") or None
        res = (runner or _run_claude)(
            prompt, model=model, cwd=cwd,
            timeout_s=FIRE_TIMEOUT_S if timeout_s is None else timeout_s)
        outcome, detail = _classify(res)
        nxt = dict(attempt=attempt + 1, model=model, cwd=cwd, mode="prompt")
        wait_s = RETRY_HOURS * 3600
        gave_up = f"{MAX_ATTEMPTS} deliveries in a row still limited"
    return _settle(plan_id, sched, attempt, outcome, detail, nxt, wait_s,
                   gave_up, project=project, session_id=session_id, now=now,
                   register=register, unregister=unregister)


def _settle(plan_id, sched, attempt, outcome, detail, nxt, wait_s, gave_up,
            *, project, session_id, now, register, unregister) -> dict:
    """Record the delivery and arm the next one, from what the ledger says
    NOW. The `fired` row lands only here, hours after the delivery began, so
    anything a human did meanwhile -- `hive autoresume cancel`, a fresh
    `hive autoresume schedule --at`, `hive roadmap complete` -- found the
    delivery still scheduled and succeeded, and is honoured over the re-arm
    the delivery earned (`nxt`, due `wait_s` after the delivery RETURNED --
    the clock is read here, not when it began): a hand-issued schedule is
    re-armed as written, whatever the outcome; a cancelled delivery or a plan
    no longer interrupted is not re-armed at all; and a still-limited
    delivery that is not re-armed says why in its detail (`gave_up` names the
    reason once the attempts run out). The spent OS timer is dropped unless
    something was armed in its place."""
    after = ev_mod.read_all(project)
    live = fold(after).get(plan_id)
    plan = roadmap.get(after, plan_id)
    rearm = None
    if plan is None or plan["status"] != "interrupted":
        if outcome == "still_limited":
            detail = (f"{detail} -- not re-armed: roadmap no longer "
                      f"interrupted ({plan['status'] if plan else 'gone'})")
    elif live is not None and live["status"] == "scheduled" and live != sched:
        rearm = dict(at=live["due"], attempt=live["attempt"],
                     cooldown_hours=live["cooldown_hours"],
                     cwd=live["cwd"] or None, model=live["model"] or None,
                     mode=live["mode"], goal_dir=live["goal_dir"] or None,
                     checked_high=live.get("checked_high"),
                     checked_left=live.get("checked_left"))
        detail = (f"{detail} -- re-armed as scheduled by hand while it ran "
                  f"(due {live['due']})")
    elif outcome == "still_limited":
        if (live or {}).get("status") == "cancelled":
            detail = f"{detail} -- not re-armed: " + (
                "fired by hand after a cancel"
                if (sched or {}).get("status") == "cancelled"
                else "cancelled while it ran")
        elif nxt["attempt"] > MAX_ATTEMPTS:
            detail = f"{detail} -- gave up: {gave_up}"
        else:
            back = time.time() if now is None else now
            rearm = dict(nxt, at=_iso(back + wait_s))
    payload = {"action": "fired", "plan_id": plan_id, "attempt": attempt,
               "outcome": outcome, "detail": str(detail)[:MAX_DETAIL_CHARS]}
    validate_payload(payload)
    ev_mod.append_event("autoresume", payload, session_id=session_id,
                        tags=["autoresume"], project=project)
    if rearm is not None:
        try:
            schedule(plan_id, project=project, session_id=session_id,
                     register=register, now=now, **rearm)
            return payload
        except ValueError:
            pass          # retired between the two reads: nothing to arm
    try:
        (unregister or _unregister_timer)(plan_id)
    except Exception:
        pass
    return payload


def wait(project=None, *, poll_s=60.0, now_fn=time.time, sleep_fn=time.sleep,
         fire_fn=None) -> int:
    """Portable delivery fallback (no OS timer needed): block until each
    pending delivery comes due, fire it, and return how many fired once
    nothing is left scheduled. A defensive stuck-guard exits if a fire pass
    leaves the same due deliveries scheduled (a no-op fire_fn must not spin
    forever)."""
    fired = 0
    prev_key = None
    while True:
        evs = ev_mod.read_all(project)
        sched = pending(evs)
        if not sched:
            return fired
        now = now_fn()
        due_now = [r for r in sched
                   if (_parse_ts(r["due"]) or 0) <= now]
        if due_now:
            key = tuple((r["plan_id"], r["due"]) for r in due_now)
            if key == prev_key:
                return fired
            prev_key = key
            for r in due_now:
                (fire_fn or fire)(r["plan_id"], project=project)
                fired += 1
            continue
        earliest = min((_parse_ts(r["due"]) or now) for r in sched)
        sleep_fn(max(1.0, min(float(poll_s), earliest - now)))


def ledger_body(payload) -> str:
    """Self-describing markdown body should an autoresume event ever be
    projected — kept for symmetry with the other registries."""
    action = payload.get("action", "")
    head = f"[{action}] autoresume {payload.get('plan_id', '')}"
    parts = [head]
    if payload.get("due"):
        parts.append(f"due: {payload['due']} (attempt "
                     f"{payload.get('attempt', 1)})")
    if payload.get("outcome"):
        parts.append(f"outcome: {payload['outcome']}")
    if payload.get("detail"):
        parts.append(str(payload["detail"]))
    return "\n\n".join(parts)
