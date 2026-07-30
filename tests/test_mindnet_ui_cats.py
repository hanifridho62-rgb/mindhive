# tests/test_mindnet_ui_cats.py
"""MN-5 U8 — the cat board's data provider: telemetry plus the passage of time.

Written TEST-FIRST against
``docs/plans/2026-07-27-001-feat-mindnet-mn3-mn4-mn5-mechanical-enforcement-plan.md``
U8. No line of `ui_cats.py` existed when these were written.

## Why `telemetry.awake` was not enough

`telemetry.fold` answers "what did each faculty last SAY", and `awake` filters
that to `status == "active"`. Neither has any notion of WHEN. The whole point of
the board — the user's own stated reason for asking for it — is spotting a
silent hang: a faculty that said "active" four hours ago and never said anything
again. Under `awake` that faculty looks exactly like one that started a second
ago, which is the single failure the feature exists to make visible.

So classification here is by AGE, not by status, and the two thresholds are the
feature:

- under 180 s -> `active`
- 180 s to 6 h -> `dim` (still shown; this is the hang the user is hunting)
- over 6 h     -> gone from the payload entirely

An `idle`-status faculty inside the window is still rendered dim rather than
dropped, because "finished recently" and "hung recently" are both things the
operator wants on screen.

## Boundaries are asserted on both sides

`test_exactly_at_the_dim_threshold_is_dim` and its `_just_under_` partner exist
because an off-by-one at 180 s is invisible in production and a one-character
edit in review.

## Totality over timestamps

This parses `ts` strings written by every past version of the emitter and feeds
a DOM. `telemetry.fold` is total for the same reason. An unparseable timestamp
is treated as maximally old — hidden, not raised, and not silently rendered as
brand new, which would turn a corrupt row into a permanently "active" cat.
"""
import pytest

from mindhive.mindnet import telemetry as tel
from mindhive.mindnet import ui_cats as ui

NOW = "2026-07-27T12:00:00Z"


# --- helpers -------------------------------------------------------------------

def _act(faculty, ago_secs, status=tel.STATUS_ACTIVE, task_id="t1",
         phase="execution"):
    """One `Activity` whose ts is *ago_secs* before NOW."""
    return tel.Activity(faculty=faculty, status=status, task_id=task_id,
                        phase=phase, ts=ui.shift(NOW, -ago_secs))


def _state(*acts):
    return {a.faculty: a for a in acts}


def _by_faculty(rows):
    return {r["faculty"]: r for r in rows}


# --- R13: the two thresholds --------------------------------------------------------

def test_a_recent_faculty_is_active():
    rows = ui.board(_state(_act("sop", 10)), NOW)
    assert _by_faculty(rows)["sop"]["display"] == ui.DISPLAY_ACTIVE


def test_just_under_the_dim_threshold_is_active():
    rows = ui.board(_state(_act("sop", ui.DIM_AFTER_SECS - 1)), NOW)
    assert _by_faculty(rows)["sop"]["display"] == ui.DISPLAY_ACTIVE


def test_exactly_at_the_dim_threshold_is_dim():
    """The boundary, stated. 180 s is dim, not active."""
    rows = ui.board(_state(_act("sop", ui.DIM_AFTER_SECS)), NOW)
    assert _by_faculty(rows)["sop"]["display"] == ui.DISPLAY_DIM


def test_well_past_the_dim_threshold_is_dim():
    rows = ui.board(_state(_act("sop", 4000)), NOW)
    assert _by_faculty(rows)["sop"]["display"] == ui.DISPLAY_DIM


def test_just_under_the_hide_threshold_is_still_shown():
    rows = ui.board(_state(_act("sop", ui.HIDE_AFTER_SECS - 1)), NOW)
    assert _by_faculty(rows)["sop"]["display"] == ui.DISPLAY_DIM


def test_exactly_at_the_hide_threshold_is_gone():
    """Six hours. The directive's rule, at its boundary."""
    assert ui.board(_state(_act("sop", ui.HIDE_AFTER_SECS)), NOW) == ()


def test_a_faculty_idle_for_seven_hours_is_absent_from_the_payload():
    """R13 in the directive's own terms — removed from the UI, not dimmed
    further."""
    assert ui.board(_state(_act("sop", 7 * 3600)), NOW) == ()


def test_the_thresholds_are_the_specified_values():
    """180 seconds and 6 hours, from the directive. Pinned so a refactor cannot
    quietly retune the feature."""
    assert ui.DIM_AFTER_SECS == 180
    assert ui.HIDE_AFTER_SECS == 6 * 3600


# --- the silent-hang case, which is the whole point ---------------------------------

def test_a_faculty_active_for_four_hours_is_shown_dim_not_hidden():
    """The user's stated reason for the board: a cat active for four hours
    without moving phase means a parallel bug. Hiding it would delete the
    signal; showing it green would disguise it."""
    row = _by_faculty(ui.board(_state(_act("task-graph", 4 * 3600)), NOW))
    assert row["task-graph"]["display"] == ui.DISPLAY_DIM
    assert row["task-graph"]["age_secs"] == pytest.approx(4 * 3600, abs=1)


def test_a_finished_faculty_disappears():
    """The user's ask, stated verbatim: "ketika tidak sedang berjalan maka kucing
    akan menghilang".

    This REPLACES an earlier test that asserted the opposite ("classification is
    by AGE, not by status", so a faculty idle for 30 s still rendered). That rule
    was chosen to protect hang detection and it did — but it also meant a cat for
    finished work sat on screen, green, for three minutes and dim for six hours.
    Splitting the two axes keeps the hang signal (the test below) without
    contradicting the ask."""
    rows = ui.board(_state(_act("sop", ui.IDLE_VANISH_SECS + 5,
                                status=tel.STATUS_IDLE)), NOW)
    assert rows == (), f"a finished faculty is still on the board: {rows}"


def test_a_just_finished_faculty_lingers_for_the_flicker_grace():
    """`telemetry.busy` brackets EVERY mutation with an active row and an idle
    row, so a burst of commands on a 1.2 s poll would flicker the cat in and out
    without this. The canary for the test above, too: it proves the disappearance
    is driven by the grace boundary and not by idle rows never rendering at all."""
    rows = ui.board(_state(_act("sop", 2, status=tel.STATUS_IDLE)), NOW)
    assert len(rows) == 1
    assert rows[0]["status"] == tel.STATUS_IDLE


def test_the_hang_signal_survives_the_disappearance_rule():
    """The property the old age-only rule existed to protect, asserted against
    the new one: `active` and stale must still render dim, however old, right up
    to the 6-hour cutoff. If this ever goes red the board has stopped being a
    hang detector."""
    for ago in (ui.DIM_AFTER_SECS + 1, 4 * 3600, ui.HIDE_AFTER_SECS - 60):
        rows = ui.board(_state(_act("task-graph", ago,
                                    status=tel.STATUS_ACTIVE)), NOW)
        assert len(rows) == 1, f"the hang signal vanished at {ago}s"
        assert rows[0]["display"] == ui.DISPLAY_DIM


def test_an_unknown_status_falls_back_to_the_age_rule():
    """Totality: `telemetry.fold` skips malformed rows, but a status this module
    has never heard of must not make a cat vanish — losing a cat is losing a
    signal, and the safe direction here is to keep showing it."""
    rows = ui.board(_state(_act("sop", 300, status="something-new")), NOW)
    assert len(rows) == 1
    assert rows[0]["display"] == ui.DISPLAY_DIM


def test_classify_without_a_status_keeps_the_pure_age_behaviour():
    """The signature stays backwards-compatible: `classify(age)` is still the
    age-only rule, so any caller that does not know about status is unchanged."""
    assert ui.classify(5) == ui.DISPLAY_ACTIVE
    assert ui.classify(ui.DIM_AFTER_SECS + 1) == ui.DISPLAY_DIM
    assert ui.classify(ui.HIDE_AFTER_SECS) is None
    # ...and an idle faculty past the grace is hidden only when status is given.
    assert ui.classify(ui.IDLE_VANISH_SECS + 1) == ui.DISPLAY_ACTIVE
    assert ui.classify(ui.IDLE_VANISH_SECS + 1, tel.STATUS_IDLE) is None


# --- R11/R12: shape and ordering ----------------------------------------------------

def test_faculties_render_in_canonical_order():
    """`telemetry.FACULTIES` order, so the board does not reshuffle itself as
    events arrive — a board that moves while you read it is unreadable."""
    state = _state(_act("pr-green", 5), _act("sop", 5), _act("history", 5))
    got = [r["faculty"] for r in ui.board(state, NOW)]
    assert got == [f for f in tel.FACULTIES if f in state]


def test_a_row_carries_the_metadata_the_panel_needs():
    """R12: the click-through panel shows skill/plugin, task_id and phase."""
    row = _by_faculty(ui.board(_state(_act("sop", 5)), NOW))["sop"]
    for key in ("faculty", "display", "status", "task_id", "phase",
                "age_secs", "skill"):
        assert key in row


def test_the_skill_field_is_filled_from_the_supplied_map():
    rows = ui.board(_state(_act("skill-plugin", 5)), NOW,
                    skills={"skill-plugin": "ce-migrate"})
    assert _by_faculty(rows)["skill-plugin"]["skill"] == "ce-migrate"


def test_the_skill_field_is_empty_rather_than_absent_when_unknown():
    """One shape for a template to render — the call `telemetry.Activity`
    already makes for its own optional fields."""
    assert _by_faculty(ui.board(_state(_act("sop", 5)), NOW))["sop"]["skill"] \
        == ""


def test_every_value_is_json_serializable():
    """This goes straight out of an HTTP endpoint."""
    import json
    json.dumps(ui.board(_state(_act("sop", 5)), NOW))


# --- totality ----------------------------------------------------------------------

def test_an_empty_state_is_an_empty_board():
    assert ui.board({}, NOW) == ()


@pytest.mark.parametrize("state", [None, "string", 42, []])
def test_a_junk_state_is_an_empty_board(state):
    assert ui.board(state, NOW) == ()


@pytest.mark.parametrize("ts", ["", None, "not-a-timestamp", "2026-13-45",
                                12345, [], "2026-07-27T99:99:99Z"])
def test_an_unparseable_timestamp_is_treated_as_maximally_old(ts):
    """Hidden, never raised — and specifically NOT treated as brand new, which
    would turn one corrupt row into a permanently active cat."""
    state = {"sop": tel.Activity(faculty="sop", status=tel.STATUS_ACTIVE,
                                 task_id="", phase="", ts=ts)}
    assert ui.board(state, NOW) == ()


def test_a_faculty_outside_the_canonical_set_is_ignored():
    """`telemetry.FACULTIES` is the one legend (invariant 10). A name nobody
    defined must not render as a cat."""
    state = {"superpower_brain": tel.Activity(
        faculty="superpower_brain", status=tel.STATUS_ACTIVE, task_id="",
        phase="", ts=ui.shift(NOW, -5))}
    assert ui.board(state, NOW) == ()


def test_a_row_that_is_not_an_activity_is_skipped_not_raised():
    state = {"sop": "junk", "history": _act("history", 5)}
    assert [r["faculty"] for r in ui.board(state, NOW)] == ["history"]


def test_a_future_timestamp_is_treated_as_now():
    """Clock skew between the writer and the dashboard must not produce a
    negative age or a cat that renders as impossibly fresh forever."""
    row = _by_faculty(ui.board(_state(_act("sop", -600)), NOW))["sop"]
    assert row["age_secs"] >= 0
    assert row["display"] == ui.DISPLAY_ACTIVE


def test_now_is_honoured_exactly_and_not_read_from_the_clock():
    """`now` is injected so the boundaries are testable without sleeping. An
    implementation that reached for the system clock would make every threshold
    test above meaningless."""
    old = _state(_act("sop", 10))
    assert ui.board(old, NOW)
    assert ui.board(old, ui.shift(NOW, 7 * 3600)) == ()


def test_board_does_not_mutate_the_state_it_was_given():
    state = _state(_act("sop", 5))
    before = dict(state)
    ui.board(state, NOW)
    assert state == before
