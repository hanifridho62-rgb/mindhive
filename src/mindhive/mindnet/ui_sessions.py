# src/mindhive/mindnet/ui_sessions.py
"""MN-19 — the session cat board's data provider: which Claude is working.

`ui_cats` answers "which FACULTY engaged". This answers a different question, and
conflating them was never an option: the faculty board's whole value is that a lit cat
means that faculty really ran (R16). "A Claude is working" is a session claim, true on
essentially every prompt, and pushing it through the faculty legend would light all
seven identically and delete the only thing they are good at.

## Nothing new is emitted

`oversee.interactive_sessions` already folds the events the hooks write —
`session_start`, `user_prompt`, `command_run`, `file_touched`, `skill_used`,
`assistant_response` — and already decides `online` from `ONLINE_WINDOW_SECONDS`.
This module renders that answer.

It does NOT recompute liveness, and that restraint is the design. A second
implementation of "is it live" is a second thing that can disagree with the Oversee
section already on screen, and two surfaces telling different stories about the same
session is exactly the confusion this feature exists to end. It also means the prompt
hook pays nothing: no new event type, no ledger write on a path with a ~0.4 s budget.

The threshold alignment is the tell that this is the right seam — `oversee`'s 180 s
"working now" window is the same number as `ui_cats.DIM_AFTER_SECS`. Both already mean
"activity this fresh counts as alive".

## Pure, with `now` injected

Nothing here reads the clock. That is what makes the age testable exactly rather than
approximately, and it keeps the dashboard handler a renderer with no logic of its own
to drift — the same split `ui_cats` is built on.

## Total, because it feeds a DOM

The input is a fold over every event any past version of the emitter wrote. One
unreadable row must cost one cat, never the board: a strip that blanks on a single bad
timestamp would read as "nothing is running", which is the false negative the whole
feature exists to remove.

## The cap is a UI bound, not a data one

A busy afternoon can hold more live sessions than a strip can show. The list is capped
so the faculty cats are not pushed off screen, but the TRUE total rides along — the
page says "+N more" rather than quietly under-reporting how much is running.
"""
from __future__ import annotations

from .ui_cats import age_secs

# How many session cats render before the strip starts counting instead.
# Six keeps the strip readable beside the seven faculty cats on a laptop width;
# the overflow is reported, never dropped silently.
SESSION_CAP = 6

DISPLAY_WORKING = "working"
DISPLAY_RESTING = "resting"


# How long a quiet session keeps a cat. `oversee`'s own 6-hour window is the
# lifetime of an Oversee CARD — a list you scroll to see what happened today. A
# strip answering "who is working NOW" cannot inherit it: the live board showed
# six sessions, all resting, the freshest four hours old, and the one working
# Claude would have been pushed into "+N more" by its own corpses.
REST_KEEP_SECS = 15 * 60

# A session id whose prefix says it is not an interactive Claude. Workspace agents
# already have their own cats on the swarm canvas, and Self-Reliant research runs
# are not a Claude anybody is watching for — showing either here double-counts
# work and dilutes what a session cat means.
_NOT_A_CHAT = ("ws-", "research-")

# Clock skew between whatever wrote an event and whatever renders it is normal;
# minutes into the future is not, and is unfixable once on screen.
_FUTURE_SKEW_SECS = 120


def _kind(session_id, declared) -> str:
    """The session's kind. `oversee` hardcodes ``"chat"`` for every record it
    builds, so the id prefix is the only real evidence of what a session is."""
    if session_id.startswith("ws-"):
        return "agent"
    if session_id.startswith("research-"):
        return "research"
    return str(declared) if declared else "chat"


def _clip(text, limit=72) -> str:
    """*text* bounded, on a word edge where one is near the cut."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    space = cut.rfind(" ")
    return (cut[:space] if space > limit - 20 else cut) + "…"


def _text(value) -> str:
    """*value* as a plain string, or ``""``.

    Absent optional fields read as ``""`` rather than ``None`` so a template has one
    shape to render — the rule `telemetry.Activity` already keeps. `oversee` leaves
    `doing` and `origin` as ``None`` until an event fills them, and a literal "None"
    reaching the DOM is a bug the operator would see."""
    return str(value) if isinstance(value, (str, int, float)) and value != "" else ""


def board(sessions, now, cap=SESSION_CAP) -> dict:
    """The render-ready session board for *sessions* at *now*.

    *sessions* is `oversee.interactive_sessions`'s output — already filtered to live,
    non-dismissed runs, already carrying its own `online` verdict. Returns
    ``{"cats": (...), "total": n, "hidden": m}``: a mapping rather than `ui_cats`'
    bare tuple because the count of what did NOT fit is part of the answer, and a
    caller reconstructing it from the tuple length would have lost the overflow.

    Working sessions sort ahead of resting ones, then freshest first, then by id.
    The operator reads this strip left to right looking for "what is running now",
    so a quiet session ahead of a busy one buries the answer; the id tiebreak keeps
    two same-second sessions from swapping places between 1.2-second polls."""
    rows: list = []
    for s in sessions or ():
        if not isinstance(s, dict):
            continue                      # a shape this module does not know
        age = age_secs(s.get("last_event_ts"), now)
        if age is None:
            # Unreadable timestamp. Dropped rather than shown as fresh: a corrupt
            # row pinned on screen as a permanently working cat is a false "someone
            # is here", which is worse than losing it — the same call `ui_cats`
            # makes for the same reason.
            continue
        # A future-dated row would be permanently "working": `oversee`'s window
        # test and `age_secs`' skew clamp both read a negative elapsed as maximally
        # fresh, so it sorts first and no rule can ever retire it. `ui_cats` hides
        # an UNPARSEABLE timestamp for this reason; a parseable-but-impossible one
        # deserves the same answer, not the opposite. Args reversed to measure how
        # far AHEAD it is, reusing the one parse rather than a second one.
        if (age_secs(now, s.get("last_event_ts")) or 0) > _FUTURE_SKEW_SECS:
            continue
        sid_raw = str(s.get("run_id") or "")
        if sid_raw.startswith(_NOT_A_CHAT):
            continue
        working = bool(s.get("online"))
        if not working and age > REST_KEEP_SECS:
            # Quiet for longer than this strip's memory. `oversee` keeps it for
            # six hours because its list is a history; this strip is a "now", and
            # a row of four-hour-old corpses buries the one Claude actually
            # working — and eats the cap doing it.
            continue
        sid = _text(s.get("run_id"))
        if not sid:
            continue
        rows.append({
            "session_id": sid,
            "display": DISPLAY_WORKING if working else DISPLAY_RESTING,
            "kind": _kind(sid, s.get("kind")),
            # Truncated HERE, not in CSS. `title` is whatever the turn's first
            # prompt said — routinely a full paragraph with absolute paths — and
            # shipping the whole thing every 1.2 s poll to render 150px of pill
            # is payload nobody reads.
            "title": _clip(_text(s.get("title"))),
            "doing": _clip(_text(s.get("doing"))),
            "age_secs": int(age),
        })
    rows.sort(key=lambda r: (r["display"] != DISPLAY_WORKING, r["age_secs"],
                             r["session_id"]))
    limit = max(0, int(cap))
    return {"cats": tuple(rows[:limit]), "total": len(rows),
            "hidden": max(0, len(rows) - limit)}
