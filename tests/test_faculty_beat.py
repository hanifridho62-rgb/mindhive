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
from mindhive.mindnet import controller as c_mod
from mindhive.mindnet import inject as i_mod
from mindhive.mindnet import sidecar as s_mod
from mindhive.mindnet import tasks as t_mod
from mindhive.mindnet import telemetry as tel
from mindhive.mindnet import ui_cats


def _rows():
    return [e for e in ev_mod.read_all() if e.type == tel.EVENT_TYPE]


def _statuses():
    return [e.payload.get("status") for e in _rows()]


@pytest.fixture(autouse=True)
def _fresh_turn():
    """One turn is one hook PROCESS, and `_ANNOUNCED_THIS_TURN` encodes that. The
    test suite is a single process running many "turns", so without this reset the
    second test onward would take the union branch and every overwrite assertion
    would pass for the wrong reason."""
    pg._ANNOUNCED_THIS_TURN = False
    yield
    pg._ANNOUNCED_THIS_TURN = False


def _age_marker(seconds, session_id=None):
    """Backdate the beat marker, which is how the throttle reads the clock."""
    path = pg._beat_path(None, session_id)
    old = time.time() - seconds
    os.utime(path, (old, old))


TASK, PHASE = "mn1-u9", "plan"


def _spine(task_id=TASK, phase=PHASE, status="in_progress"):
    """A `Sidecar` as `read` would have returned it, carrying one workable task.

    Built from the real dataclasses rather than a stub with the right attribute
    names: what `announce_faculties` is handed at runtime IS this shape, and a
    stub would keep passing after the shape changed — a failure mode this project
    has already hit (`_spine_with_advice`'s docstring records it)."""
    rec = c_mod.PhaseRecord(task_id=task_id, phase=phase)
    task = t_mod.Task(task_id=task_id, title="Wire the panel", status=status)
    return s_mod.Sidecar(c_mod.PhaseState({task_id: rec}, {}), True, "",
                         {"events_seen": 0, "last_ts": "", "segments": {}},
                         {task_id: task})


def _marker(session_id=None):
    return json.loads(pg._beat_path(None, session_id).read_text(encoding="utf-8"))


def _pairs():
    """``(task_id, phase)`` for every activity row, as the payload carries it."""
    return [(e.payload.get("task_id"), e.payload.get("phase")) for e in _rows()]


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


# --- the marker must not become a perpetual motion machine -------------------

def test_a_marker_older_than_the_dim_threshold_refuses_to_beat():
    """The failure the beat itself would otherwise manufacture. Every beat re-arms
    the clock, so with only a lower bound one prompt that lit a faculty would keep
    re-asserting it for as long as ANY tool call happened in that project — days
    later, in another session, on prompts that engaged nothing. `HIDE_AFTER_SECS`
    becomes unreachable and the cat means "a Claude is using tools here", which is
    exactly the stranded-lit state dropping the `idle` row was supposed to avoid."""
    pg.announce_faculties({"sop"})
    before = len(_rows())
    _age_marker(pg.FACULTY_BEAT_MAX_AGE_SECS + 60)
    assert pg.beat_faculties() == 0
    assert len(_rows()) == before, "a long-dead turn was resurrected"


def test_a_dead_marker_is_retired_so_it_cannot_be_reconsidered():
    pg.announce_faculties({"sop"})
    _age_marker(pg.FACULTY_BEAT_MAX_AGE_SECS + 60)
    pg.beat_faculties()
    assert not pg._beat_path(None, None).exists()


def test_the_absolute_bound_is_not_below_the_beat_interval():
    """A ceiling under the floor would mean the beat could never fire at all."""
    assert pg.FACULTY_BEAT_MAX_AGE_SECS > pg.FACULTY_BEAT_SECS


def test_the_bound_does_not_exceed_the_dim_threshold():
    """Past the dim band the cat is already grey, so beating would contradict what
    the operator is looking at."""
    assert pg.FACULTY_BEAT_MAX_AGE_SECS <= ui_cats.DIM_AFTER_SECS


# --- two announces in one turn, two sessions in one project ------------------

def test_a_second_announce_in_the_same_turn_unions_rather_than_clobbers():
    """`cmd_hook` announces twice per prompt — the catalog from `record_direction`,
    then the consulted spine. An overwrite left the first set with no heartbeat, so
    the catalog cats dimmed mid-turn: the exact defect the beat exists to prevent."""
    pg.announce_faculties({"skill-plugin", "history"}, session_id="s1")
    pg.announce_faculties({"sop", "improvements"}, session_id="s1")
    data = json.loads(pg._beat_path(None, "s1").read_text(encoding="utf-8"))
    assert sorted(data["faculties"]) == ["history", "improvements", "skill-plugin",
                                         "sop"]


def test_a_new_turn_starts_from_nothing_lit():
    """Union is scoped to ONE turn. Accumulating across turns would re-light
    faculties that a later prompt never engaged."""
    pg.announce_faculties({"history"}, session_id="s1")
    pg._ANNOUNCED_THIS_TURN = False           # a fresh hook process = a new turn
    pg.announce_faculties({"sop"}, session_id="s1")
    data = json.loads(pg._beat_path(None, "s1").read_text(encoding="utf-8"))
    assert data["faculties"] == ["sop"]


def test_two_sessions_in_one_project_do_not_share_a_marker():
    """`paths.detect_project` collapses every worktree of a repo onto one namespace,
    so a project-only marker is shared by every interactive session on that repo —
    and one session's tool calls would re-assert faculties another session lit and
    it never touched, stamped with the wrong session id."""
    pg.announce_faculties({"sop"}, session_id="sess-A")
    pg._ANNOUNCED_THIS_TURN = False
    pg.announce_faculties({"history"}, session_id="sess-B")
    a = json.loads(pg._beat_path(None, "sess-A").read_text(encoding="utf-8"))
    b = json.loads(pg._beat_path(None, "sess-B").read_text(encoding="utf-8"))
    assert a["faculties"] == ["sop"] and b["faculties"] == ["history"]


def test_one_sessions_beat_does_not_sustain_anothers_faculty():
    pg.announce_faculties({"sop"}, session_id="sess-A")
    pg._ANNOUNCED_THIS_TURN = False
    pg.announce_faculties({"history"}, session_id="sess-B")
    _age_marker(pg.FACULTY_BEAT_SECS + 5, "sess-B")
    pg.beat_faculties(session_id="sess-B")
    beaten = [e.payload["faculty"] for e in _rows()][2:]
    assert beaten == ["history"], f"session B beat the wrong faculties: {beaten}"


def test_the_marker_write_is_atomic():
    """Readers are other PROCESSES on every tool call. A truncate-then-write is a
    torn read for whichever one lands mid-write — and this file's own
    `_write_manifest` already uses the atomic helper."""
    import inspect
    src = inspect.getsource(pg.announce_faculties)
    assert "write_text_atomic" in src
    assert ".write_text(" not in src


# --- the task and phase the cat is ABOUT ------------------------------------------
# `ui_cats.board` has always published `task_id` and `phase` on every cat, and
# `cat_board.catPanel` has always rendered them. Nothing on the prompt-gate path
# ever supplied either: `announce_faculties` called `telemetry.emit(faculty,
# ACTIVE)` and stopped. So the panel showed `task —  phase —` for every faculty the
# gate lights, on every turn, while the spine in the same hook invocation knew both
# — a field reading "nothing to report" where the truth was "nobody passed it".


def test_an_announced_cat_names_the_task_the_spine_is_steering():
    """The row the operator actually reads."""
    pg.announce_faculties({"sop"}, sc=_spine())
    assert _pairs() == [(TASK, PHASE)]


def test_a_turn_with_no_task_leaves_both_rows_empty_rather_than_guessing():
    """An em-dash is the correct answer when nothing is declared, and it must stay
    the answer — inventing a task here would put a name on the board that the
    ledger cannot back. Canary inline: the same call WITH a spine fills, so this
    negative cannot pass on an announce that never fills anything."""
    pg.announce_faculties({"sop"})
    assert _pairs() == [(None, None)]

    pg.announce_faculties({"history"}, sc=_spine())
    assert (TASK, PHASE) in _pairs()


def test_the_beat_re_asserts_the_task_and_not_only_the_faculty():
    """THE load-bearing one. `telemetry.fold` is last-write-wins and the beat fires
    from `PostToolUse`, so a beat that re-emitted the faculty WITHOUT the task
    would blank both rows on the first tool call of the turn — the fields correct
    for one poll of the board and empty for every poll after it. Asserted through
    the fold, because the fold is what the endpoint serves."""
    pg.announce_faculties({"sop"}, sc=_spine())
    _age_marker(pg.FACULTY_BEAT_SECS + 5)
    assert pg.beat_faculties() == 1
    state = tel.fold(ev_mod.read_all())
    assert (state["sop"].task_id, state["sop"].phase) == (TASK, PHASE)


def test_the_second_announce_of_a_turn_does_not_drop_the_task():
    """One hook invocation announces TWICE — the catalog's cats from
    `record_direction`, the spine's from the hook body — and the second takes the
    union branch that REWRITES the marker. Losing the task there is the same
    last-write-wins blanking as above, one step further along."""
    pg.announce_faculties({"history"}, sc=_spine())
    pg.announce_faculties({"sop"})
    data = _marker()
    assert (data.get("task_id"), data.get("phase")) == (TASK, PHASE)
    assert data["faculties"] == ["history", "sop"], "the union itself regressed"


def test_a_marker_written_before_this_change_still_beats():
    """Totality in the direction that actually happens: a marker left by the
    version before this one carries no `task_id` key at all. A beat that raised on
    it would strand the cat mid-turn — and a stranded lit cat is what `ui_cats`
    calls worse than a missing one."""
    pg.announce_faculties({"sop"}, sc=_spine())
    pg._beat_path(None, None).write_text(
        json.dumps({"faculties": ["sop"], "session_id": ""}), encoding="utf-8")
    _age_marker(pg.FACULTY_BEAT_SECS + 5)
    assert pg.beat_faculties() == 1
    assert _pairs()[-1] == (None, None)


def test_the_beat_reads_the_marker_and_never_the_sidecar():
    """`beat_faculties` runs on EVERY tool call, and its whole no-op path is one
    `stat`. Re-deriving the task from a fresh `sidecar.read` there would put a JSON
    read on that path for an answer the turn already computed, so the marker
    carries it. Pinned by making the sidecar unreadable and requiring the beat to
    still name the task."""
    pg.announce_faculties({"sop"}, sc=_spine())

    def _explode(*a, **kw):
        raise AssertionError("the beat must not read the sidecar per tool call")

    original = s_mod.read
    s_mod.read = _explode
    try:
        _age_marker(pg.FACULTY_BEAT_SECS + 5)
        assert pg.beat_faculties() == 1
    finally:
        s_mod.read = original
    assert _pairs()[-1] == (TASK, PHASE)


def test_the_announce_and_the_injector_cannot_disagree_about_the_task():
    """Two reports on one turn, one source. `steering` is asserted here as the
    source rather than the literal so that a change to the selection rule moves
    both together or fails loudly (invariant 10)."""
    sc = _spine()
    pg.announce_faculties({"sop"}, sc=sc)
    assert _pairs() == [i_mod.steering(sc)]
