# SPDX-License-Identifier: Apache-2.0
# src/mindhive/mindnet/ui_cats.py
"""MN-5 U8 — the cat board's data provider: telemetry plus the passage of time.

`telemetry.fold` answers "what did each faculty last SAY"; `telemetry.awake`
filters that to the ones that said `active`. Neither knows WHEN anything was
said, and when is the entire feature.

## Two questions, two axes

The board answers two different things, and an earlier version of this module
collapsed them into one:

**"Is it running?"** is the STATUS axis. The user's ask was explicit — *"ketika
tidak sedang berjalan maka kucing akan menghilang"* — a cat for work that is not
running should go away. A faculty that reported `idle` has told us it finished.

**"Is it stuck?"** is the AGE axis. A faculty that went `active` four hours ago
and has said nothing since is hung, and hiding it would delete the only signal
that says so.

Classifying by age ALONE (the previous rule) satisfied the second and broke the
first: a faculty that finished thirty seconds ago stayed on screen, green, for
three minutes and then dim for six hours. So:

- `idle`, older than `IDLE_VANISH_SECS` (15 s) -> gone. It finished.
- `active`, under `DIM_AFTER_SECS` (180 s)     -> `active`.
- `active`, 180 s up to `HIDE_AFTER_SECS`      -> `dim`, still on screen. This
                                                  band IS the hang signal.
- anything at or past 6 h                      -> gone.

The 15-second grace exists because `telemetry.busy` writes an `active` row and
an `idle` row around EVERY mutation, so a burst of commands would otherwise
flicker the cat in and out on a 1.2 s poll. It is the one number here chosen by
this module rather than specified: raise it for a longer afterglow, set it to 0
for a cat that vanishes the instant its work stops.

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

from .telemetry import FACULTIES, STATUS_IDLE

# The directive's two numbers. One home each: the UI reads them from here rather
# than respelling them in JavaScript, so the badge and the payload can never
# disagree about what "idle" means.
DIM_AFTER_SECS = 180
HIDE_AFTER_SECS = 6 * 3600

# How long a FINISHED faculty lingers before its cat disappears. See the module
# docstring: this is a flicker guard, not a feature, because `telemetry.busy`
# brackets every mutation with an active row and an idle row.
IDLE_VANISH_SECS = 15

DISPLAY_ACTIVE = "active"
DISPLAY_DIM = "dim"

# The one faculty whose engagement IS a skill, and therefore the only one the
# panel's `skill` row can answer without guessing. `playbook.engaged` lights it
# from `selected_skills` and `promptgate.record_direction` writes those same
# names to a `skill_directed` event, so the two ends of the join are the same
# selection. `history` lights from RECORDS and `sop` from a procedure; labelling
# either of them with a skill name would be a confident wrong answer where an
# empty row is the correct one.
SKILL_FACULTY = "skill-plugin"
DIRECTION_EVENT = "skill_directed"


def directions_by_session(evs) -> dict:
    """``session -> the newest directed skill names``, comma-joined.

    Newest wins, which is not a choice made here so much as the rule the faculty
    fold already runs on: a faculty's most recent state IS its state, so its most
    recent direction is the matching partner. Total for the same reason
    `telemetry.fold` is — these rows were written by every past version of the
    gate and one malformed payload must not take out the board."""
    out: dict = {}
    for ev in evs or []:
        if getattr(ev, "type", None) != DIRECTION_EVENT:
            continue
        sid = str(getattr(ev, "session_id", "") or "")
        if not sid:
            continue
        p = getattr(ev, "payload", None)
        rows = p.get("skills") if isinstance(p, dict) else None
        if not isinstance(rows, list):
            continue
        names = [str(r.get("name") or "").strip() for r in rows
                 if isinstance(r, dict)]
        names = [n for n in names if n]
        # An empty list is a real answer and must overwrite an older one: the
        # gate directing nothing this turn is not the same as it never having
        # spoken, and keeping the stale names would label the cat with skills
        # that are no longer running.
        out[sid] = ", ".join(names)
    return out


def skill_overlay(state, directions) -> dict:
    """The ``faculty -> skill`` overlay `board` takes, built from *state*'s own
    sessions.

    Per SESSION, never per project: `paths.detect_project` collapses every
    worktree of a repo onto one namespace, so a project-wide join would routinely
    label one session's cat with another session's skills — the same trap
    `promptgate._beat_path` is keyed by session to avoid."""
    if not isinstance(state, dict) or not isinstance(directions, dict):
        return {}
    act = state.get(SKILL_FACULTY)
    session = str(getattr(act, "session", "") or "") if act is not None else ""
    named = directions.get(session, "") if session else ""
    return {SKILL_FACULTY: named} if named else {}


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


def classify(age, status=None):
    """``active``/``dim``, or ``None`` when the faculty should not render.

    *status* is the faculty's last reported state. A faculty that said `idle` has
    finished, so its cat disappears once the flicker grace has passed — that is
    the user's ask, and it is why this takes status at all. `status=None` (the
    default) keeps the pure age behaviour, which is what an unknown or malformed
    row gets.

    ``None`` for an unparseable age is the deliberate branch — see the module
    docstring on why an unreadable timestamp hides rather than shows."""
    if age is None or age >= HIDE_AFTER_SECS:
        return None
    if status == STATUS_IDLE and age >= IDLE_VANISH_SECS:
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
        status = str(getattr(act, "status", "") or "")
        display = classify(age, status)
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
