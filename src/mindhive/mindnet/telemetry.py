# src/mindhive/mindnet/telemetry.py
"""MN-1 U10 — the faculty activity feed: which MindNetwork faculty is awake.

`mindnet_activity` has been registered in `events.py` since MN-1 opened and has
had **no producer**. MN-5 renders one cat per awake faculty from this feed, so
until something emits, the event type is dead and MN-5's defining claim —
**a missing cat means a real bug** (R16) — is unfalsifiable.

Three properties, and each is a direct answer to a way that claim can be
falsified by something other than a real bug:

## 1. Ledger-derived, never in-memory

The decomposition's first hard constraint for MN-5. MindSwarm's mission state is
a plain instance attribute (`workspace.py:472`) that dies with the server; a cat
board built the same way blinks out on every restart, and "the cat is missing"
stops meaning anything. So this module holds NO state: `emit` appends, `fold`
replays, and `current` is the two composed. There is no cache to go stale and
nothing to rebuild after a restart.

## 2. Best-effort — the observer never disturbs the observed

A phase transition that succeeded must not be reported as failed because a cat
animation could not be logged. `emit` therefore wraps its append and returns
``None`` on any fault, mirroring `promptgate.record_direction`
(`promptgate.py:445-470`) — the committed idiom for exactly this. `busy` carries
the rule in the other direction too: it emits the closing `idle` from a
``finally`` and never swallows the caller's exception, because telemetry that
ate a real error would disturb the observed far more than a missing cat could.

A rejected faculty name is a *refusal*, not an error: `emit` returns ``None``
and appends nothing. Raising would make a mistyped constant take down the
transition it was only supposed to describe.

## 3. One canonical name set, verbatim (invariant 10)

`FACULTIES` is defined here, once, and rejected-if-not-a-member. The set is
fixed by §MN-5 of the architecture decomposition and repeated in `events.py`'s
own registration comment; `tests/test_mindnet_telemetry.py` pins this tuple to
both of those files so a synonym fails a test rather than silently rendering no
cat. Two plausible-sounding inventions — `superpower_brain` and `code_auditor` —
reached this unit's brief before being caught, which is precisely why the guard
is machine-checked rather than reviewed.

## Deliberate asymmetry between required and optional fields

``faculty`` and ``status`` gate the event: outside their canonical sets, nothing
is appended. ``task_id`` and ``phase`` are decoration on the cat's detail panel
and are DROPPED when malformed, with the event still landing. Refusing a whole
event over a mistyped task_id would make a cat vanish and send someone hunting a
faculty bug that does not exist — the exact false positive R16 exists to rule
out.

## Purity and bounds

`fold` and `awake` are pure, deterministic and total: malformed rows are
skipped, never raised (the ledger holds history written by every past version of
the emitter, and MN-5 polls this feed — one bad row must not take out the
board). No cap constant is needed: the payload's four fields are drawn from
fixed sets or `sop.ID_RE`, and the projection is bounded by the seven faculties
however many events the feed carries.

This module is deliberately NOT wired into `controller.py` or `executor.py` —
where the emit calls belong is a seam to decide explicitly, not a side effect of
adding the producer.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import NamedTuple

from .. import events as ev_mod
from ..sop import ID_RE
from .tasks import PHASES

EVENT_TYPE = "mindnet_activity"

# The seven faculties, verbatim from §MN-5 of the architecture decomposition and
# from `events.py`'s registration comment. ONE home: anything comparing faculty
# names imports this tuple instead of respelling it, and anything outside it is
# refused rather than rendered as a cat nobody defined.
FACULTIES = ("skill-plugin", "sop", "improvements", "history", "task-graph",
             "compatibility", "pr-green")

# The one faculty with a producer outside this package: MN-4's language lock
# emits it from `enforce.hook_decision`, which is the seam where that rule
# finishes deciding (`enforcer.py` itself may not import `events`). Named here
# so the cross-module caller shares this legend rather than respelling the
# string, and asserted a member so a rename cannot leave a dangling emitter that
# `_payload` would silently refuse.
FACULTY_COMPATIBILITY = "compatibility"
assert FACULTY_COMPATIBILITY in FACULTIES

STATUS_ACTIVE = "active"
STATUS_IDLE = "idle"
STATUSES = (STATUS_ACTIVE, STATUS_IDLE)

# `mindnet` groups this with the phase events; `activity` distinguishes the
# high-volume UI feed from the semantic phase axis at a glance in recall and the
# dashboard; `auto` is the taxonomy's existing mark for machine-written rows.
TAGS = ("mindnet", "activity", "auto")


class Activity(NamedTuple):
    """One faculty's CURRENT state — the fold's per-faculty survivor, not a
    single event. Every field is a plain ``str`` so the record serializes into
    a sidecar or an API response without a custom encoder, and absent optional
    fields read as ``""`` rather than ``None`` (one shape for a template to
    render)."""
    faculty: str
    status: str
    task_id: str = ""
    phase: str = ""
    ts: str = ""


# --- emitting ------------------------------------------------------------------------

def _task_id(value) -> str:
    """*value* as a task id, or ``""``. Same shape the phase axis enforces
    (`phases._check_shape`), so a cat's detail link can join to a real task."""
    text = value if isinstance(value, str) else ""
    return text if ID_RE.match(text) else ""


def _payload(faculty, status, task_id=None, phase=None):
    """The ``{faculty, status, task_id?, phase?}`` payload, or ``None`` when a
    REQUIRED field is outside its canonical set (see the module docstring on the
    required/optional asymmetry). Pure — the caller decides what a ``None``
    means."""
    if faculty not in FACULTIES or status not in STATUSES:
        return None
    payload = {"faculty": faculty, "status": status}
    tid = _task_id(task_id)
    if tid:
        payload["task_id"] = tid
    # Membership in the ONE legend, never a second spelling of it (invariant 10).
    if phase in PHASES:
        payload["phase"] = phase
    return payload


def emit(faculty, status, task_id=None, phase=None, project=None,
         session_id=None, ts=None):
    """Record that *faculty* woke or went idle. Returns the event, or ``None``
    when the call was refused or the write failed.

    NEVER raises. A phase transition that succeeded must not be reported as
    failed because a cat animation could not be logged, so every fault — a
    non-canonical name, an unwritable segment, a backend that is down — degrades
    to ``None`` and leaves the caller untouched (`promptgate.record_direction`
    is the same contract).

    Unconditional by design: it does not read the ledger to check whether
    *faculty* is already in *status*. That read would cost a full replay on a
    path that has to stay cheap, and the fold is idempotent under repeats, so a
    duplicate `active` is harmless."""
    try:
        payload = _payload(faculty, status, task_id, phase)
        if payload is None:
            return None
        return ev_mod.append_event(EVENT_TYPE, payload, session_id=session_id,
                                   tags=list(TAGS), ts=ts, project=project)
    except Exception:
        return None


@contextmanager
def busy(faculty, task_id=None, phase=None, project=None, session_id=None):
    """Mark *faculty* awake for the duration of a block, idle on the way out —
    however the block ends.

    The `finally` is the point. A cat stuck awake forever is the same false
    signal as a missing one, and a hand-written try/finally at every future call
    site is the kind of thing exactly one call site forgets. The caller's
    exception propagates unchanged: swallowing it here would be telemetry
    disturbing the observed far more than a lost event ever could."""
    emit(faculty, STATUS_ACTIVE, task_id, phase, project=project,
         session_id=session_id)
    try:
        yield
    finally:
        emit(faculty, STATUS_IDLE, task_id, phase, project=project,
             session_id=session_id)


# --- reading -------------------------------------------------------------------------

def fold(evs) -> dict:
    """Deterministic, total fold of `mindnet_activity` into
    ``faculty -> Activity``.

    Last write wins, and that is the whole rule — unlike the phase fold, which
    resolves contradictions by authority (`controller.py` KTD3), there is no
    contradiction to adjudicate here: a faculty's most recent state IS its
    state. Replay-order manipulability (`events.py`'s second-resolution ``ts``)
    therefore costs nothing on this axis, and no event is a claim about
    anything that has to be trusted.

    Optional fields are re-validated on the way out, not merely on the way in:
    this feed will be written by every future version of the emitter and read
    straight into MN-5's DOM, so a `phase` that is not a phase is dropped
    here."""
    state: dict = {}
    for ev in evs or []:
        if getattr(ev, "type", None) != EVENT_TYPE:
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        faculty, status = p.get("faculty"), p.get("status")
        if faculty not in FACULTIES or status not in STATUSES:
            continue
        phase = p.get("phase")
        state[faculty] = Activity(faculty=faculty, status=status,
                                  task_id=_task_id(p.get("task_id")),
                                  phase=phase if phase in PHASES else "",
                                  ts=str(getattr(ev, "ts", "") or ""))
    return state


def current(evs=None, project=None) -> dict:
    """The live feed: `fold` over the ledger. *evs* is injectable so a caller
    that already read the events (MN-5's poll, the sidecar path) folds once
    rather than re-reading per consumer."""
    return fold(ev_mod.read_all(project) if evs is None else evs)


def awake(state) -> tuple:
    """The faculties currently `active`, in CANONICAL order — the order MN-5
    lays cats out in, so the board does not reshuffle itself as events arrive.
    Total: a missing or empty state is simply nobody awake."""
    rows = state or {}
    return tuple(f for f in FACULTIES
                 if getattr(rows.get(f), "status", None) == STATUS_ACTIVE)
