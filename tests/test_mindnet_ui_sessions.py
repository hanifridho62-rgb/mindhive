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


# --- the strip is a "now", not a history ------------------------------------

def test_a_long_quiet_session_leaves_the_strip():
    """`oversee` keeps a session for six hours because its list is a HISTORY you
    scroll. This strip answers "who is working now". The live board showed six
    sessions, all resting, the freshest four hours old — and the one working Claude
    would have been pushed into "+N more" by its own corpses."""
    out = us.board([_sess("old", online=False, ago=us.REST_KEEP_SECS + 60)], NOW)
    assert out["cats"] == () and out["total"] == 0


def test_a_recently_quiet_session_stays():
    """Just paused is not gone — losing it the moment output stops would make the
    strip flicker on every pause between tool calls."""
    out = us.board([_sess("recent", online=False, ago=60)], NOW)
    assert [r["session_id"] for r in out["cats"]] == ["recent"]


def test_a_working_session_is_never_aged_out():
    """Working beats every age rule. `online` is oversee's verdict and it wins."""
    out = us.board([_sess("busy", online=True, ago=us.REST_KEEP_SECS + 9999)], NOW)
    assert [r["display"] for r in out["cats"]] == [us.DISPLAY_WORKING]


def test_the_rest_window_is_shorter_than_oversees_card_lifetime():
    from mindhive import oversee
    assert us.REST_KEEP_SECS < oversee.ACTIVE_WINDOW_SECONDS


# --- only actual Claudes ----------------------------------------------------

def test_a_workspace_agent_is_not_a_claude_session():
    """`workspace._log` stamps `ws-<id>` when an agent has no CLI session yet, and
    `oversee` labels every record it builds `kind: "chat"`. Those agents already
    have their own cats on the swarm canvas — showing them here double-counts the
    same work and dilutes what a session cat means. Seen on the live board:
    `ws-b474f44f` rendered as a Claude Code session."""
    out = us.board([_sess("ws-b474f44f"), _sess("real")], NOW)
    assert [r["session_id"] for r in out["cats"]] == ["real"]


def test_a_self_reliant_research_stream_is_not_a_claude_session():
    out = us.board([_sess("research-abc"), _sess("real")], NOW)
    assert [r["session_id"] for r in out["cats"]] == ["real"]


def test_a_future_dated_row_is_dropped_not_pinned_first():
    """Both `oversee`'s window test and `age_secs`' skew clamp read a negative
    elapsed as maximally fresh, so one bad row would sort FIRST, render "working —
    0s ago", survive every retirement rule, and eat a cap slot forever. `ui_cats`
    hides an unparseable timestamp for this reason; a parseable-but-impossible one
    must not get the opposite treatment."""
    ahead = _sess("ahead")
    ahead["last_event_ts"] = ui_cats.shift(NOW, us._FUTURE_SKEW_SECS + 600)
    out = us.board([ahead, _sess("real")], NOW)
    assert [r["session_id"] for r in out["cats"]] == ["real"]


def test_a_small_clock_skew_is_tolerated():
    """Skew between the writer and the renderer is normal and must not cost a cat."""
    skewed = _sess("skewed")
    skewed["last_event_ts"] = ui_cats.shift(NOW, 5)
    out = us.board([skewed], NOW)
    assert [r["session_id"] for r in out["cats"]] == ["skewed"]


# --- what the pill actually says --------------------------------------------

def test_a_long_title_is_clipped_server_side():
    """`title` is whatever the turn's first prompt said — routinely a paragraph
    with absolute paths. Shipping it whole every 1.2 s poll to render 150px of pill
    is payload nobody reads. Seen on the live board: a 50-character Windows path."""
    long_title = r"In C:\Users\hanif\mindhive, " + "diagnose the parser " * 8
    out = us.board([_sess("x", title=long_title)], NOW)
    assert len(out["cats"][0]["title"]) < len(long_title)
    assert out["cats"][0]["title"].endswith("…")


def test_a_short_title_is_left_alone():
    out = us.board([_sess("x", title="fixing the parser")], NOW)
    assert out["cats"][0]["title"] == "fixing the parser"


def test_the_kind_comes_from_the_id_when_oversee_only_ever_says_chat():
    """`oversee.interactive_sessions` hardcodes `kind: "chat"` on every record it
    builds, so the declared value carries no information and the id prefix is the
    only real evidence of what a session is."""
    assert us._kind("ws-abc", "chat") == "agent"
    assert us._kind("research-abc", "chat") == "research"
    assert us._kind("54e0d771-aedd", "chat") == "chat"


# --- what the cat answers when you open it ----------------------------------
#
# WHY. The strip drew a cat and a caption and nothing else, so the four
# questions the operator actually asks it -- what task, what phase, which
# skill, active or not -- had no answer on this surface. They were being asked
# of the FACULTY board instead, which reads `mindnet_activity`: 729 rows on the
# real ledger, zero of them carrying a task_id or a phase, and the newest 13
# days past `ui_cats.HIDE_AFTER_SECS`. That board is not where a live Claude
# session is described; this one is.
#
# Nothing new is emitted for any of it. `task` is folded by
# `oversee.interactive_sessions` from `state_set`, `skill` comes off the trail
# that fold already attaches, and `phase` is read from the roadmap plan the
# session is working. KTD1 holds: this module renders answers, it does not
# recompute them.

def _plan(plan_id="sess-plan", steps=None, status="active"):
    steps = steps if steps is not None else [
        {"text": "design the data model", "status": "done", "new": False},
        {"text": "build the fold", "status": "active", "new": False},
        {"text": "render the panel", "status": "pending", "new": False},
    ]
    done = sum(1 for s in steps if s["status"] == "done")
    return {"plan_id": plan_id, "prompt": "ship the roadmap", "title": "",
            "status": status, "steps": steps,
            "progress": {"done": done, "total": len(steps)}}


def test_the_cat_carries_the_task_the_session_declared():
    s = _sess()
    s["task"] = "Port the Postgres read mirror"
    out = us.board([s], NOW)
    assert out["cats"][0]["task"] == "Port the Postgres read mirror"


def test_the_cat_names_the_last_skill_the_session_used():
    s = _sess()
    s["skills"] = [{"name": "ce-plan", "plugin": "compound-engineering"},
                   {"name": "mindhive-python-tdd", "plugin": ""}]
    s["skills_total"] = 2
    out = us.board([s], NOW)
    assert out["cats"][0]["skill"] == "mindhive-python-tdd"


def test_a_plugin_skill_is_named_with_its_prefix():
    """`ce-plan` alone is ambiguous across plugins, and the prefix is how every
    other surface in this repo spells it."""
    s = _sess()
    s["skills"] = [{"name": "ce-plan", "plugin": "compound-engineering"}]
    out = us.board([s], NOW)
    assert out["cats"][0]["skill"] == "compound-engineering:ce-plan"


def test_the_phase_is_the_active_step_and_its_position():
    """"Sudah sampai mana" answered exactly: which step, out of how many, by
    name. The position comes from the ACTIVE step's index, never from the done
    count -- a plan with two done steps and no active one is not on step 3."""
    out = us.board([_sess()], NOW, plans={"abc123def456": _plan()})
    assert out["cats"][0]["phase"] == "step 2/3: build the fold"


def test_a_plan_with_no_active_step_reports_progress_instead_of_a_position():
    """The discriminating case. An implementation that prints `done + 1` says
    "step 2/2" for a plan that has finished one step and started nothing --
    inventing a position no step holds."""
    plan = _plan(steps=[{"text": "one", "status": "done", "new": False},
                        {"text": "two", "status": "pending", "new": False}])
    out = us.board([_sess()], NOW, plans={"abc123def456": plan})
    assert out["cats"][0]["phase"] == "1/2 steps done"


def test_an_interrupted_plan_says_so_in_the_phase():
    """The state the operator is hunting for: work paused mid-plan. Reading
    "step 2/3" for a plan nobody is advancing is the report that hid it."""
    out = us.board([_sess()], NOW,
                   plans={"abc123def456": _plan(status="interrupted")})
    assert out["cats"][0]["phase"] == "interrupted at step 2/3: build the fold"


def test_a_session_with_no_plan_has_an_empty_phase_not_a_missing_key():
    out = us.board([_sess()], NOW)
    cat = out["cats"][0]
    assert cat["phase"] == "" and cat["task"] == "" and cat["skill"] == ""


def test_a_junk_plan_is_dropped_rather_than_blanking_the_strip():
    """Totality, the contract this module already keeps for session rows: one
    malformed plan must not take the board down with it."""
    for junk in ("not a plan", None, {"steps": "nope"}, {"progress": 7}):
        out = us.board([_sess()], NOW, plans={"abc123def456": junk})
        assert out["cats"][0]["phase"] == ""


def test_the_plans_argument_is_optional_so_older_callers_still_work():
    out = us.board([_sess()], NOW, us.SESSION_CAP)
    assert out["total"] == 1
