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
  still-limited delivery re-schedules itself with a short backoff until
  MAX_ATTEMPTS.
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

from . import events as ev_mod
from . import claude_cli
from . import paths
from . import roadmap
from .sop import ID_RE

ACTIONS = ("scheduled", "fired", "cancelled")
OUTCOMES = ("delivered", "still_limited", "error", "skipped")

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
            "timer": "none", "outcome": "", "detail": "",
            "created": ts, "updated": ts}


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
                outcome="", detail="")
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
    # What the run was doing when it stopped, when the interrupting writer
    # recorded it. Placed before the ask because it is the situation the ask is
    # being resumed INTO — a reader who stops after the first paragraph should
    # still know they are picking up a loop, not starting one.
    relay = str(rec.get("relay_context") or "").strip()
    if relay:
        lines += [f"**Where it stopped:** {relay}", ""]
    if any(s.get("new") for s in rec["steps"]):
        lines += [
            "Steps marked `(new)` below were added by the replenishment "
            "injector because the record proved them outstanding — they were "
            "not in the plan this work started from. They are real work, not "
            "filler, and each one was admitted only against recorded evidence.",
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


def _register_timer(plan_id, due_iso, cwd, project) -> str:
    """Register a per-user Windows scheduled task that runs
    ``hive autoresume fire <plan_id>`` at the due time (converted to local
    time — Task Scheduler triggers are local). Returns the timer label for
    the scheduled payload; "none" on any failure or off-platform, because
    the scheduled event must still be recorded — `hive autoresume wait`
    then delivers it."""
    if not _timer_enabled() or not sys.platform.startswith("win"):
        return "none"
    epoch = _parse_ts(due_iso)
    if epoch is None:
        return "none"
    local = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))
    arg = f"-m mindhive autoresume fire {plan_id}"
    if project:
        arg += f" --project {project}"
    action = (f"$a=New-ScheduledTaskAction -Execute "
              f"{_ps_quote(sys.executable)} -Argument {_ps_quote(arg)}")
    if cwd:
        action += f" -WorkingDirectory {_ps_quote(cwd)}"
    script = (
        f"{action}; "
        f"$t=New-ScheduledTaskTrigger -Once -At ([datetime]::ParseExact("
        f"{_ps_quote(local)},'yyyy-MM-ddTHH:mm:ss',$null)); "
        f"Register-ScheduledTask -TaskName {_ps_quote(_task_name(plan_id))} "
        f"-Action $a -Trigger $t -Force | Out-Null")
    return "windows_task" if _run_powershell(script) else "none"


def _unregister_timer(plan_id) -> None:
    if not _timer_enabled() or not sys.platform.startswith("win"):
        return
    _run_powershell(
        f"Unregister-ScheduledTask -TaskName "
        f"{_ps_quote(_task_name(plan_id))} -Confirm:$false "
        f"-ErrorAction SilentlyContinue")


# --- lifecycle: schedule / cancel / fire / wait -------------------------------

def schedule(plan_id, *, project=None, evs=None, cooldown_hours=None, at=None,
             attempt=1, model=None, cwd=None, session_id=None, register=None,
             now=None):
    """Schedule the plan's marker to re-enter Claude. Guards mirror
    roadmap.record: the plan must exist and be interrupted — auto-resume
    delivers interruption markers, nothing else. Re-scheduling replaces the
    pending delivery (and the -Force'd OS task). Returns the appended
    event."""
    if evs is None:
        evs = ev_mod.read_all(project)
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
               "model": str(model or "")[:MAX_MODEL_CHARS], "timer": timer}
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


def fire(plan_id, *, project=None, session_id=None, runner=None,
         timeout_s=None, now=None, register=None, unregister=None) -> dict:
    """Deliver the marker now (normally invoked by the OS timer at the due
    time). Re-checks the plan first: one resumed or completed by a human is
    skipped, never re-delivered. A still-limited delivery re-schedules
    itself (RETRY_HOURS backoff) until MAX_ATTEMPTS. Returns the fired
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
    prompt = resume_prompt(rec, attempt=attempt)
    cwd = (sched or {}).get("cwd") or None
    if cwd and not os.path.isdir(cwd):
        cwd = None            # the worktree moved; run from the default cwd
    res = (runner or _run_claude)(
        prompt, model=(sched or {}).get("model") or None, cwd=cwd,
        timeout_s=FIRE_TIMEOUT_S if timeout_s is None else timeout_s)
    outcome, detail = _classify(res)
    payload = {"action": "fired", "plan_id": plan_id, "attempt": attempt,
               "outcome": outcome, "detail": str(detail)[:MAX_DETAIL_CHARS]}
    validate_payload(payload)
    ev_mod.append_event("autoresume", payload, session_id=session_id,
                        tags=["autoresume"], project=project)
    if outcome == "still_limited" and attempt < MAX_ATTEMPTS:
        now = time.time() if now is None else now
        schedule(plan_id, project=project, session_id=session_id,
                 at=_iso(now + RETRY_HOURS * 3600), attempt=attempt + 1,
                 model=(sched or {}).get("model") or None, cwd=cwd,
                 register=register, now=now)
    else:
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
