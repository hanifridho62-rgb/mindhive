# tests/test_mindnet_ui_sessions.py
"""MN-19 — the session cat board's data provider.

The faculty board answers "which part of MindNetwork engaged". This one answers a
different question the operator kept asking and the board could not: "is a Claude
working right now?" — for an ordinary Claude Code session, a MindParallel pane, or a
MindSwarm mission alike.

The load-bearing decision is that NOTHING new is emitted. `oversee.interactive_sessions`
already folds the events the hooks write and already decides `online` from
`ONLINE_WINDOW_SECONDS`. This module renders that answer; it does not recompute it.
A second implementation of "is it live" would be a second thing that can disagree with
the Oversee section already on screen — and the whole reason the operator could not
trust the board was two surfaces telling different stories.

Same contract as `ui_cats`: pure, `now` injected rather than read, total (one malformed
row must never blank the strip), and deterministically ordered so the board does not
reshuffle itself between 1.2-second polls.
"""
import pytest

from mindhive.mindnet import ui_cats
from mindhive.mindnet import ui_sessions as us

NOW = "2026-08-02T12:00:00Z"


def _sess(sid="abc123def456", *, online=True, ago=5, kind="chat",
          title="fixing the parser", doing=None, origin=None):
    """One `oversee.interactive_sessions` record, in its real shape."""
    return {
        "run_id": sid,
        "kind": kind,
        "title": title,
        "doing": doing if doing is not None else title,
        "online": online,
        "last_event_ts": ui_cats.shift(NOW, -ago),
        "status": "running",
        "origin": origin,
        "skills_total": 0,
        "skills": [],
    }


# --- the two display bands --------------------------------------------------

def test_a_working_session_renders_working():
    """The operator's actual ask: Claude is doing something, so a cat is moving."""
    out = us.board([_sess(online=True)], NOW)
    assert [r["display"] for r in out["cats"]] == [us.DISPLAY_WORKING]


def test_a_quiet_session_renders_resting():
    """Still on screen — the session has not ended — but not claiming to be busy.
    A cat that moves whether or not Claude is working reports nothing."""
    out = us.board([_sess(online=False, ago=600)], NOW)
    assert [r["display"] for r in out["cats"]] == [us.DISPLAY_RESTING]


def test_the_online_decision_is_taken_from_oversee_not_recomputed():
    """KTD1, pinned. `oversee` owns the liveness window; this module must not
    second-guess it. A row whose timestamp looks stale but which oversee marked
    online still renders working — because if the two ever disagree, the board and
    the Oversee section would tell the operator different stories about the same
    session, which is precisely the confusion this feature exists to end."""
    out = us.board([_sess(online=True, ago=99999)], NOW)
    assert out["cats"][0]["display"] == us.DISPLAY_WORKING


# --- purity and totality ----------------------------------------------------

def test_the_provider_never_reads_the_clock():
    """What makes the bands testable at their boundary instead of approximately —
    the same property `ui_cats` is built on."""
    rows = [_sess("aaa", ago=5), _sess("bbb", online=False, ago=400)]
    assert us.board(rows, NOW) == us.board(rows, NOW)


def test_one_malformed_row_does_not_blank_the_strip():
    """This feed is read straight into the DOM and its rows come from a fold over
    every event any past version wrote. One unreadable timestamp must cost one cat,
    never the board."""
    broken = _sess("bad")
    broken["last_event_ts"] = "not-a-timestamp"
    out = us.board([broken, _sess("good")], NOW)
    assert [r["session_id"] for r in out["cats"]] == ["good"]


def test_a_row_that_is_not_a_dict_is_skipped():
    out = us.board([None, "nonsense", _sess("good")], NOW)
    assert [r["session_id"] for r in out["cats"]] == ["good"]


def test_no_sessions_is_an_empty_board_not_an_error():
    out = us.board([], NOW)
    assert out["cats"] == () and out["total"] == 0


def test_a_non_list_input_is_survived():
    assert us.board(None, NOW)["cats"] == ()


# --- ordering and the cap ---------------------------------------------------

def test_working_sessions_sort_ahead_of_resting_ones():
    """The strip is read left to right and the operator is looking for "what is
    running now". A quiet session ahead of a busy one buries the answer."""
    rows = [_sess("quiet", online=False, ago=900), _sess("busy", online=True, ago=3)]
    out = us.board(rows, NOW)
    assert [r["session_id"] for r in out["cats"]] == ["busy", "quiet"]


def test_ordering_is_stable_for_equal_ages():
    """Two sessions whose last event landed in the same second must not swap places
    between polls — a strip that reshuffles cannot be read at a glance."""
    a, b = _sess("bbb", ago=10), _sess("aaa", ago=10)
    first = [r["session_id"] for r in us.board([a, b], NOW)["cats"]]
    second = [r["session_id"] for r in us.board([b, a], NOW)["cats"]]
    assert first == second


def test_the_cap_bounds_what_renders():
    rows = [_sess(f"s{i}", ago=i) for i in range(us.SESSION_CAP + 4)]
    out = us.board(rows, NOW)
    assert len(out["cats"]) == us.SESSION_CAP


def test_the_true_total_is_reported_even_when_capped():
    """The cap is a UI decision, not a data one. Reporting only what fits would make
    the strip lie about how much is running — so the overflow is a number the page
    can show rather than a silence."""
    rows = [_sess(f"s{i}", ago=i) for i in range(us.SESSION_CAP + 4)]
    out = us.board(rows, NOW)
    assert out["total"] == us.SESSION_CAP + 4
    assert out["hidden"] == 4


def test_nothing_is_hidden_when_everything_fits():
    out = us.board([_sess("only")], NOW)
    assert out["hidden"] == 0


# --- the row's own shape ----------------------------------------------------

def test_a_row_carries_what_the_strip_needs_to_label_it():
    """Every field is a plain string or int so the payload serializes without a
    custom encoder, and absent values read as "" rather than None — one shape for
    the template to render, matching `telemetry.Activity`'s rule."""
    out = us.board([_sess("abc123def456", kind="chat", title="fixing the parser")], NOW)
    row = out["cats"][0]
    assert row["session_id"] == "abc123def456"
    assert row["kind"] == "chat"
    assert row["title"] == "fixing the parser"
    assert isinstance(row["age_secs"], int)
    for key in ("session_id", "kind", "title", "doing", "display"):
        assert isinstance(row[key], str), f"{key} is not a plain string"


def test_a_missing_title_does_not_render_as_none():
    row = _sess("x")
    row["title"] = None
    row["doing"] = None
    out = us.board([row], NOW)
    assert out["cats"][0]["title"] == "" and out["cats"][0]["doing"] == ""


def test_the_age_is_whole_seconds():
    """Rendered as "4m ago"; a float would make the payload differ on every poll for
    no visible gain — the same reasoning `ui_cats.board` records."""
    out = us.board([_sess(ago=42)], NOW)
    assert out["cats"][0]["age_secs"] == 42


# --- the shared parse -------------------------------------------------------

def test_the_age_rule_is_borrowed_not_respelled():
    """`ui_cats.age_secs` already owns parsing the ledger's timestamps, clamping
    clock skew at zero, and answering None for junk. A second copy here is a second
    thing that can drift (invariant 10)."""
    import inspect
    src = inspect.getsource(us)
    assert "age_secs" in src
    assert "fromisoformat" not in src, (
        "ui_sessions parses timestamps itself instead of reusing ui_cats.age_secs")
