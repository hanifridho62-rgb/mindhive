# tests/test_faculty_beat.py
"""MN-19 U4 — a faculty cat lasts as long as the turn, not fifteen seconds.

`announce_faculties` runs AFTER the injected block is printed, and it used to write an
`active` row and an `idle` row in the same second. `ui_cats` hides an idle faculty once
the 15-second flicker grace passes, so a faculty that genuinely engaged was visible for
at most fifteen seconds — and only if the operator happened to be looking. The board
reported the turn in the past tense.

The repair mirrors the swarm producer: light it when the work starts, re-assert while
the work continues, and let SILENCE put it out. No closing `idle` is written at all.
That is deliberate. There is no turn-end hook registered on this machine, so a settle
would need a signal that does not exist — and a settle that fails to run is exactly how
a cat gets stranded lit, which `ui_cats` calls worse than a missing one. Absence of a
beat dims the cat on `ui_cats`' own schedule and means precisely what the dim band has
always meant: said active, then went quiet.

The heartbeat's throttle cannot live in memory. Every hook invocation is a FRESH
PROCESS (`mindhive hook post_tool_use`), so a module-level timestamp would reset on
every tool call and beat every time. The marker file's mtime is the clock instead —
one `stat` on a path that fires on every tool call.
"""
import json
import os
import time

import pytest

from mindhive import events as ev_mod
from mindhive import promptgate as pg
from mindhive.mindnet import telemetry as tel
from mindhive.mindnet import ui_cats


def _rows():
    return [e for e in ev_mod.read_all() if e.type == tel.EVENT_TYPE]


def _statuses():
    return [e.payload.get("status") for e in _rows()]


def _age_marker(seconds):
    """Backdate the beat marker, which is how the throttle reads the clock."""
    path = pg._beat_path(None)
    old = time.time() - seconds
    os.utime(path, (old, old))


# --- lighting ---------------------------------------------------------------

def test_announcing_a_faculty_writes_active_and_no_idle():
    """The defect, stated directly. The paired `idle` is what made the cat a
    15-second afterglow instead of a report on work in progress."""
    pg.announce_faculties({"sop"})
    assert _statuses() == [tel.STATUS_ACTIVE]


def test_announcing_nothing_writes_nothing():
    """The honesty property the board rests on: a cat lights only when that faculty
    really engaged. An empty set is the common case on a non-code prompt."""
    pg.announce_faculties(set())
    assert _rows() == []


def test_announcing_several_lights_each_of_them():
    pg.announce_faculties({"sop", "history"})
    assert sorted(e.payload["faculty"] for e in _rows()) == ["history", "sop"]


def test_a_faculty_outside_the_legend_is_refused_not_raised():
    """`telemetry.emit` refuses a non-canonical name by returning None. A mistyped
    constant must cost a cat, never the prompt it was describing."""
    pg.announce_faculties({"not-a-faculty"})
    assert _rows() == []


# --- the heartbeat ----------------------------------------------------------

def test_the_beat_interval_stays_under_the_dim_threshold():
    """A beat at or above the dim band would let the cat grey out between beats,
    which is the defect this exists to prevent. Pinned as a RELATIONSHIP so moving
    either number keeps the guarantee or fails loudly (invariant 10)."""
    assert pg.FACULTY_BEAT_SECS < ui_cats.DIM_AFTER_SECS


def test_a_beat_inside_the_interval_writes_nothing():
    """`PostToolUse` fires on every tool call. Unthrottled, a busy turn would append
    a row per call to a feed whose own read path is a full ledger replay."""
    pg.announce_faculties({"sop"})
    before = len(_rows())
    for _ in range(5):
        pg.beat_faculties()
    assert len(_rows()) == before


def test_a_beat_past_the_interval_re_asserts_what_the_turn_lit():
    """The repair itself: a long turn keeps its cat awake. `telemetry.emit`'s own
    docstring blesses the repeat — the fold is idempotent under duplicates."""
    pg.announce_faculties({"sop"})
    _age_marker(pg.FACULTY_BEAT_SECS + 5)
    pg.beat_faculties()
    assert _statuses() == [tel.STATUS_ACTIVE, tel.STATUS_ACTIVE]
    assert {e.payload["faculty"] for e in _rows()} == {"sop"}


def test_the_beat_re_arms_so_it_does_not_fire_twice_running():
    pg.announce_faculties({"sop"})
    _age_marker(pg.FACULTY_BEAT_SECS + 5)
    pg.beat_faculties()
    n = len(_rows())
    pg.beat_faculties()
    assert len(_rows()) == n, "the beat did not re-arm its own throttle"


def test_a_beat_with_no_turn_in_flight_writes_nothing():
    """Nothing was lit, so there is nothing to sustain. A beat that emitted anyway
    would light a faculty that never engaged — the exact dishonesty the board
    exists to avoid."""
    pg.beat_faculties()
    assert _rows() == []


def test_the_beat_survives_a_corrupt_marker():
    """The marker is a derived sidecar, not ledger truth. A hand-edited or
    half-written file must cost a beat, never the tool call it rode in on."""
    pg.announce_faculties({"sop"})
    pg._beat_path(None).write_text("{ not json", encoding="utf-8")
    _age_marker(pg.FACULTY_BEAT_SECS + 5)
    pg.beat_faculties()          # must not raise


# --- the observer never disturbs the observed -------------------------------

def test_announcing_never_raises_when_the_ledger_is_down(monkeypatch):
    """The hook's contract: a prompt that succeeded must not fail because a cat
    could not be logged."""
    def boom(*_a, **_kw):
        raise RuntimeError("ledger is down")

    monkeypatch.setattr(tel, "emit", boom)
    pg.announce_faculties({"sop"})       # must not raise


def test_beating_never_raises_when_the_ledger_is_down(monkeypatch):
    pg.announce_faculties({"sop"})
    _age_marker(pg.FACULTY_BEAT_SECS + 5)

    def boom(*_a, **_kw):
        raise RuntimeError("ledger is down")

    monkeypatch.setattr(tel, "emit", boom)
    pg.beat_faculties()                  # must not raise


def test_an_unwritable_marker_does_not_break_the_announce(monkeypatch):
    """The rows are the product; the marker is only what sustains them. Losing the
    marker costs the heartbeat, not the cat."""
    monkeypatch.setattr(pg, "_beat_path", lambda project=None: (_ for _ in ()).throw(
        OSError("no such directory")))
    pg.announce_faculties({"sop"})
    assert _statuses() == [tel.STATUS_ACTIVE]


# --- the wiring -------------------------------------------------------------

def test_the_post_tool_use_hook_beats():
    """The canary. Every test above drives `beat_faculties` directly, so all of them
    would still pass if no hook ever called it — and the feature would be dead."""
    import inspect

    from mindhive import __main__ as main_mod
    src = inspect.getsource(main_mod.cmd_hook)
    branch = src[src.index('if args.stage == "post_tool_use"'):]
    branch = branch[:branch.index('elif args.stage ==')]
    assert "beat_faculties" in branch, (
        "post_tool_use does not sustain the faculty cats; the heartbeat is dead code")


def test_the_marker_records_what_was_lit():
    pg.announce_faculties({"sop", "history"})
    data = json.loads(pg._beat_path(None).read_text(encoding="utf-8"))
    assert sorted(data["faculties"]) == ["history", "sop"]
