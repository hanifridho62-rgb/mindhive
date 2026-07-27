# src/mindhive/mindnet/ui_cats.py
"""MN-5 U8 — the cat board's data provider: telemetry plus the passage of time.

`telemetry.fold` answers "what did each faculty last SAY"; `telemetry.awake`
filters that to the ones that said `active`. Neither knows WHEN anything was
said, and when is the entire feature.

## The board is a hang detector, not decoration

The user asked for the cats to find silent hangs in parallel work: a faculty
that went active four hours ago and has said nothing since means something is
stuck. Under `awake` that faculty is indistinguishable from one that started a
second ago — the exact case the board exists to make visible. So this module
classifies by AGE:

- under `DIM_AFTER_SECS` (180 s)   -> `active`
- 180 s up to `HIDE_AFTER_SECS`    -> `dim`, still on screen. This band IS the
                                      hang: dim-but-present is the signal.
- at or past 6 h                   -> absent from the payload entirely.

Age, not status, is the axis. A faculty that reported `idle` thirty seconds ago
still renders, because "finished just now" and "hung just now" are both things
the operator wants to see; the raw ``status`` rides along so the UI can tell
them apart.

## Pure, with `now` injected

Nothing here reads the clock. That is what makes the 180 s and 6 h boundaries
testable at the boundary rather than approximately, and it keeps `swarm_page.py`
a renderer with no logic of its own to drift.

## Total, because it parses history and feeds a DOM

`ts` strings on this feed were written by every past version of the emitter and
one bad row must not take out the board — the same contract `telemetry.fold`
holds. An unparseable timestamp is treated as maximally old, so it is HIDDEN.
The alternative (treating it as fresh) would pin a corrupt row on screen as a
permanently active cat, which is worse than losing it: a false "something is
running" is exactly the signal this board is supposed to be trustworthy about.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .telemetry import FACULTIES

# The directive's two numbers. One home each: the UI reads them from here rather
# than respelling them in JavaScript, so the badge and the payload can never
# disagree about what "idle" means.
DIM_AFTER_SECS = 180
HIDE_AFTER_SECS = 6 * 3600

DISPLAY_ACTIVE = "active"
DISPLAY_DIM = "dim"


def _parse(ts):
    """*ts* as an aware datetime, or ``None``.

    Accepts the trailing ``Z`` the ledger writes, which `datetime.fromisoformat`
    rejects before 3.11 and which is the only format `events.py` produces. A
    naive result is assumed UTC — the ledger is UTC throughout, and treating it
    as local time would shift every age by the machine's offset."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    text = ts.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def shift(ts, secs) -> str:
    """*ts* moved by *secs*, in the ledger's own format.

    Exported rather than kept private because the tests need to build "N seconds
    before now" without duplicating the parse-and-format rules — a second
    implementation of those in the test file is a second thing that can be
    wrong."""
    base = _parse(ts) or datetime(1970, 1, 1, tzinfo=timezone.utc)
    moved = base + timedelta(seconds=secs)
    return moved.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_secs(ts, now):
    """Seconds between *ts* and *now*, or ``None`` when either will not parse.

    Clamped at zero. Clock skew between whatever wrote the event and whatever is
    rendering the board is normal, and a negative age would either render as a
    cat that is impossibly fresh forever or fall through a threshold comparison
    in whichever direction the arithmetic happened to land."""
    a, b = _parse(ts), _parse(now)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def classify(age):
    """``active``/``dim``, or ``None`` when the faculty should not render.

    ``None`` for an unparseable age is the deliberate branch — see the module
    docstring on why an unreadable timestamp hides rather than shows."""
    if age is None or age >= HIDE_AFTER_SECS:
        return None
    return DISPLAY_DIM if age >= DIM_AFTER_SECS else DISPLAY_ACTIVE


def board(state, now, skills=None) -> tuple:
    """The render-ready cat board for *state* at *now*.

    *state* is `telemetry.fold`'s ``faculty -> Activity`` map. *skills* is an
    optional ``faculty -> skill name`` overlay for the click-through panel; the
    field is always present and empty when unknown, so a template has one shape
    to render rather than two.

    Iterates `FACULTIES` rather than *state* for two reasons: it fixes the
    output order (a board that reshuffles as events arrive cannot be read), and
    it means a faculty name nobody defined is dropped rather than rendered as a
    cat — the one-legend rule `telemetry.py` already enforces on the way in."""
    rows: list = []
    if not isinstance(state, dict):
        return ()
    lookup = skills if isinstance(skills, dict) else {}
    for faculty in FACULTIES:
        act = state.get(faculty)
        if act is None:
            continue
        age = age_secs(getattr(act, "ts", None), now)
        display = classify(age)
        if display is None:
            continue
        rows.append({
            "faculty": faculty,
            "display": display,
            "status": str(getattr(act, "status", "") or ""),
            "task_id": str(getattr(act, "task_id", "") or ""),
            "phase": str(getattr(act, "phase", "") or ""),
            # Rounded to whole seconds: this is rendered as "idle for 4h", and a
            # float would make the payload differ on every poll for no visible
            # gain.
            "age_secs": int(age),
            "skill": str(lookup.get(faculty, "") or ""),
        })
    return tuple(rows)
