# tests/test_oversee.py — interactive-session Oversee projection.
import types
from datetime import datetime

import pytest

from mindhive import events, oversee
from mindhive import __main__ as cli


def _epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _ev(etype, payload, minute, sid="sess-abc", second=0):
    events.append_event(etype, payload, session_id=sid,
                        ts=f"2026-07-08T10:{minute:02d}:{second:02d}Z",
                        project="test")


def test_active_session_appears_with_prompt_title_and_online():
    _ev("session_start", {"cwd": "x"}, 0)
    _ev("user_prompt", {"text": "build the parser feature"}, 1)
    _ev("file_touched", {"path": "src/parser.py"}, 2)
    _ev("command_run", {"text": "pytest -q"}, 3)
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T10:04:00Z"))
    assert len(res) == 1
    s = res[0]
    assert s["kind"] == "chat"
    assert s["title"] == "build the parser feature"
    assert s["online"] is True
    assert s["iterations"] == 1                       # one command_run
    assert any("src/parser.py" in d for d in s["done"])
    stale = oversee.interactive_sessions("test",
                                         now_fn=lambda: _epoch("2026-07-08T10:30:00Z"))
    assert stale[0]["online"] is False                # idle > online window


def _ws_step(phase, action, minute, sid="sess-abc", status="pending"):
    from mindhive import worksessions as ws
    ts = f"2026-07-08T10:{minute:02d}:00Z"
    tag = ws.make_work_session_tag(phase, action, ts=ts)
    _ev("work_session", {"action": "step_append", "session_id": sid,
                         "step_id": f"st-{minute}", "phase": phase, "ws_tag": tag,
                         "initiative_tag": "[USER_SPEC: x]",
                         "usage_tag": "[USAGE: 0/100000 | 0.0%]", "status": status,
                         "timestamp": ts}, minute, sid=sid)
    return tag


def test_card_carries_the_work_session_phase_and_its_recent_tags():
    _ev("user_prompt", {"text": "build it"}, 0)
    tags = [_ws_step(p, f"step {i}", i + 1)
            for i, p in enumerate(("brainstorm", "plan", "PRD", "kanban", "execution"))]
    _ev("work_session", {"action": "step_status", "session_id": "sess-abc",
                         "step_id": "st-5", "status": "completed",
                         "timestamp": "2026-07-08T10:07:00Z"}, 7)
    (card,) = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:08:00Z"))
    assert card["phase"] == "execution"            # nothing active: the last step
    assert card["ws_tags"] == tags[-4:]            # the head tag + three before it
    assert card["initiatives"] == []
    # Bookkeeping rows are not activity: the card's clock is the prompt's.
    assert card["last_event_ts"] == "2026-07-08T10:00:00Z"


def test_the_card_head_is_the_active_step_not_the_last_appended():
    _ev("user_prompt", {"text": "build it"}, 0)
    tags = [_ws_step("plan", "one", 1), _ws_step("execution", "two", 2),
            _ws_step("quality assessment", "three", 3)]
    _ev("work_session", {"action": "step_status", "session_id": "sess-abc",
                         "step_id": "st-1", "status": "completed",
                         "timestamp": "2026-07-08T10:04:00Z"}, 4)
    _ev("work_session", {"action": "step_status", "session_id": "sess-abc",
                         "step_id": "st-2", "status": "active",
                         "timestamp": "2026-07-08T10:05:00Z"}, 5)
    (card,) = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:06:00Z"))
    assert card["phase"] == "execution"
    assert card["ws_tags"] == tags[:2]             # the trail ends at the active step


def test_off_axis_or_malformed_work_session_rows_leave_the_card_quiet():
    _ev("user_prompt", {"text": "x"}, 0)
    _ev("work_session", {"action": "step_append", "phase": "implementation",
                         "ws_tag": "[WS: implementation | x | t]"}, 1)
    _ev("work_session", {"action": "step_append", "phase": "plan", "ws_tag": 3}, 2)
    _ev("work_session", "free text", 3)
    _ev("initiative_declared", "free text", 4)
    (card,) = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))
    assert (card["phase"], card["ws_tags"], card["initiatives"]) == ("", [], [])


def test_a_session_without_work_session_rows_still_carries_the_keys():
    _ev("user_prompt", {"text": "x"}, 0)
    (card,) = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:01:00Z"))
    assert (card["phase"], card["ws_tags"], card["initiatives"]) == ("", [], [])


def test_work_session_rows_alone_open_no_card():
    _ws_step("plan", "copied from a roadmap", 1, sid="ws-demo")
    _ev("initiative_declared", {"reason": "r", "ref": "", "tag": "[INITIATIVE: r]"}, 2,
        sid="ws-demo")
    assert oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z")) == []


def test_work_session_rows_never_resurrect_an_aged_out_or_dismissed_session():
    events.append_event("user_prompt", {"text": "old task"}, session_id="old-sess",
                        ts="2026-07-08T01:00:00Z", project="test")
    _ev("user_prompt", {"text": "some work"}, 1)
    events.append_event("loop_run_dismissed", {"run_id": "sess-abc", "cause": "manual"},
                        ts="2026-07-08T10:05:00Z", project="test")
    _ws_step("plan", "migrated", 20, sid="old-sess")      # `hive worksession migrate`
    _ws_step("plan", "migrated", 21)
    _ev("initiative_declared", {"reason": "r", "ref": "", "tag": "[INITIATIVE: r]"}, 22)
    assert oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:23:00Z")) == []


def test_initiatives_ride_the_card_oldest_first_and_capped():
    _ev("user_prompt", {"text": "x"}, 0)
    for i in range(10):
        _ev("initiative_declared", {"reason": f"r{i}", "ref": "i:1" if i == 9 else "",
                                    "tag": f"[INITIATIVE: r{i}]"}, 1, second=i)
    (card,) = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:02:00Z"))
    assert [i["reason"] for i in card["initiatives"]] == [f"r{i}" for i in range(2, 10)]
    assert card["initiatives"][-1] == {"reason": "r9", "ref": "i:1",
                                       "tag": "[INITIATIVE: r9]",
                                       "ts": "2026-07-08T10:01:09Z", "step_id": ""}
    assert card["last_event_ts"] == "2026-07-08T10:00:00Z"


def test_an_initiative_step_written_through_the_api_gets_its_badge():
    from mindhive import worksessions as ws
    _ev("user_prompt", {"text": "x"}, 0)
    step = ws.create_work_session_step("quality assessment", "TDD_GREEN", True,
                                       initiative_reason="autonomous extra")
    ws.record_work_session_step("sess-abc", step, project="test")
    now = events.read_all("test")[-1].ts
    (card,) = oversee.interactive_sessions("test", now_fn=lambda: _epoch(now) + 60,
                                           active_seconds=10 ** 9)
    (init,) = card["initiatives"]
    assert (init["reason"], init["step_id"], init["tag"]) == \
        ("autonomous extra", step.step_id, "[INITIATIVE: autonomous extra]")
    assert card["phase"] == "quality assessment"


def test_ended_session_is_removed_automatically():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "quick fix"}, 1)
    _ev("session_end", {}, 2)
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))
    assert res == []


def test_stale_session_ages_out():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "old work"}, 1)
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T17:30:00Z"))
    assert res == []                                   # > 6h idle


def test_manual_dismissal_hides_until_new_activity():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "some work"}, 1)
    events.append_event("loop_run_dismissed", {"run_id": "sess-abc", "cause": "manual"},
                        ts="2026-07-08T10:05:00Z", project="test")
    hidden = oversee.interactive_sessions("test",
                                          now_fn=lambda: _epoch("2026-07-08T10:06:00Z"))
    assert hidden == []
    _ev("file_touched", {"path": "y.py"}, 7)          # session was still alive
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T10:08:00Z"))
    assert [s["run_id"] for s in res] == ["sess-abc"]


def test_loop_and_manual_events_are_not_chat_sessions():
    events.append_event("loop_run_start", {"run_id": "r1", "goal_dir": "d"},
                        session_id="sess-abc", ts="2026-07-08T10:00:00Z", project="test")
    events.append_event("decision", {"text": "cli append"}, session_id="manual",
                        ts="2026-07-08T10:01:00Z", project="test")
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T10:02:00Z"))
    assert res == []


def test_semantic_events_and_next_steps_fill_the_checklist():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "refactor the store"}, 1)
    _ev("decision", {"text": "keep the file backend"}, 2)
    _ev("next_step", {"text": "migrate the reader"}, 3)
    _ev("assistant_response", {"text": "migrating the reader now"}, 4)
    s = oversee.interactive_sessions("test",
                                     now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))[0]
    assert any("keep the file backend" in d for d in s["done"])
    assert s["planned"] == ["migrate the reader"]
    assert s["doing"] == "migrating the reader now"


def test_skill_trail_records_name_time_ok_and_why():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "ship the parser"}, 1)
    _ev("skill_used", {"name": "ce-plan", "plugin": "compound-engineering",
                       "ok": True, "text": "planned",
                       "args": "plan the parser feature"}, 2)
    _ev("user_prompt", {"text": "now fix the bug"}, 3)
    _ev("skill_used", {"name": "ce-debug", "ok": False, "text": "boom"}, 4)
    s = oversee.interactive_sessions("test",
                                     now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))[0]
    assert s["skills_total"] == 2
    a, b = s["skills"]
    assert a["name"] == "ce-plan" and a["plugin"] == "compound-engineering"
    assert a["ts"] == "2026-07-08T10:02:00Z"
    assert a["ok"] is True
    assert a["why"] == "plan the parser feature"      # the args = the ask
    assert a["during"] == "ship the parser"           # the prompt it served
    assert b["ok"] is False and b["why"] == ""
    assert b["during"] == "now fix the bug"           # later prompt shifts it


def test_skill_before_any_prompt_and_nameless_payloads():
    _ev("session_start", {}, 0)
    _ev("skill_used", {"name": "verify"}, 1)
    _ev("skill_used", {"text": "no name"}, 2)         # skipped, never raises
    s = oversee.interactive_sessions("test",
                                     now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["skills_total"] == 1
    assert s["skills"][0]["during"] == ""             # nothing preceded it


def test_skill_trail_caps_display_but_counts_all():
    _ev("session_start", {}, 0)
    for i in range(12):
        _ev("skill_used", {"name": f"s{i}"}, 1, second=i)
    s = oversee.interactive_sessions("test",
                                     now_fn=lambda: _epoch("2026-07-08T10:02:00Z"))[0]
    assert s["skills_total"] == 12
    assert [k["name"] for k in s["skills"]] == [f"s{i}" for i in range(4, 12)]


def test_skill_used_keeps_the_session_fresh():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "long think"}, 1)
    _ev("skill_used", {"name": "verify"}, 30)         # only recent activity
    s = oversee.interactive_sessions("test",
                                     now_fn=lambda: _epoch("2026-07-08T10:31:00Z"))[0]
    assert s["online"] is True


def test_self_reliant_run_is_labeled_and_titled():
    # a Self-Reliant run's research-<id> stream carries the provenance label
    # and a human title (it has no user_prompt to title from)
    _ev("experiment_result", {"text": "Self-Reliant experiment 1: ok"}, 0,
        sid="research-ab12cd34")
    _ev("user_prompt", {"text": "ordinary chat"}, 1, sid="sess-plain")
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T10:02:00Z"))
    by_id = {r["run_id"]: r for r in res}
    sr = by_id["research-ab12cd34"]
    assert sr["origin"] == "self-reliant"
    assert sr["title"] == "Self-Reliant run ab12cd34"
    assert by_id["sess-plain"]["origin"] is None


# --- richer why: causal direction + intended-use fallback ----------------------

def test_skill_directed_stamps_causal_why():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "make it better"}, 1)
    _ev("skill_directed", {"skills": [{"name": "ce-plan",
                                       "plugin": "compound-engineering",
                                       "measured": {"ok": 12, "calls": 12}}]}, 2)
    _ev("skill_used", {"name": "ce-plan", "plugin": "compound-engineering"}, 3)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))[0]
    k = s["skills"][0]
    assert k["why"] == "directed by prompt-gate (measured 12/12)"
    assert k["during"] == "make it better"


def test_skill_directed_without_measured_stat():
    _ev("session_start", {}, 0)
    _ev("skill_directed", {"skills": [{"name": "verify"}]}, 1)   # unproven -> no stat
    _ev("skill_used", {"name": "verify"}, 2)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["skills"][0]["why"] == "directed by prompt-gate"


def test_args_why_beats_direction():
    _ev("session_start", {}, 0)
    _ev("skill_directed", {"skills": [{"name": "ce-plan"}]}, 1)
    _ev("skill_used", {"name": "ce-plan", "args": "plan the parser"}, 2)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["skills"][0]["why"] == "plan the parser"   # args (the ask) wins


def test_intended_use_fills_bare_why_labelled_as_purpose():
    _ev("session_start", {}, 0)
    _ev("skill_used", {"name": "verify"}, 1)            # no args, no direction, no prompt
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"),
        intended_use={"verify": "exercise the real flow end to end"})[0]
    # labelled as the skill's PURPOSE, never presented as this call's motive
    assert s["skills"][0]["why"] == "purpose: exercise the real flow end to end"


def test_direction_is_consumed_once_not_over_attributed():
    # one direction must not stamp every later same-skill call — only the next
    _ev("session_start", {}, 0)
    _ev("skill_directed", {"skills": [{"name": "ce-plan"}]}, 1)
    _ev("skill_used", {"name": "ce-plan"}, 2)           # consumes the direction
    _ev("skill_used", {"name": "ce-plan"}, 3)           # independent later call
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))[0]
    whys = [k["why"] for k in s["skills"]]
    assert whys == ["directed by prompt-gate", ""]      # not both stamped


def test_non_list_skills_directed_stays_total():
    # a present-but-non-list `skills` value must not raise (total-read invariant)
    for bad in (None, 7, "ce-plan"):
        events.append_event("skill_directed", {"skills": bad}, session_id="s-bad",
                            ts="2026-07-08T10:01:00Z", project="test")
        events.append_event("skill_used", {"name": "ce-plan"}, session_id="s-bad",
                            ts="2026-07-08T10:02:00Z", project="test")
        trails = oversee.skill_trails(project="test")   # must not raise
        assert trails["s-bad"][-1]["why"] == ""         # nothing stamped from junk


def test_direction_is_scoped_to_its_session():
    _ev("session_start", {}, 0, sid="A")
    _ev("skill_directed", {"skills": [{"name": "ce-plan"}]}, 1, sid="A")
    _ev("session_start", {}, 0, sid="B")
    _ev("skill_used", {"name": "ce-plan"}, 2, sid="B")
    res = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))
    b = {r["run_id"]: r for r in res}["B"]
    assert b["skills"][0]["why"] == ""                  # A's direction must not leak


def test_malformed_skill_directed_never_raises():
    _ev("session_start", {}, 0)
    _ev("skill_directed", {"skills": ["not-a-dict", {"noname": 1}]}, 1)
    _ev("skill_used", {"name": "ce-plan"}, 2)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["skills"][0]["why"] == ""                  # junk skipped, total read


# --- skill_trails: the durable fold behind the CLI / API -----------------------

def test_skill_trails_includes_ended_sessions():
    _ev("session_start", {}, 0)
    _ev("skill_used", {"name": "verify"}, 1)
    _ev("session_end", {}, 2)
    # the live board drops the ended session...
    assert oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z")) == []
    # ...but the durable fold keeps its trail
    trails = oversee.skill_trails(project="test")
    assert [k["name"] for k in trails["sess-abc"]] == ["verify"]


def test_cli_skills_trail_prints_trail(capsys, monkeypatch):
    monkeypatch.setattr(oversee, "intended_use_map", lambda *a, **k: {})
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "ship it"}, 1)
    _ev("skill_used", {"name": "ce-plan", "args": "plan the thing"}, 2)
    args = types.SimpleNamespace(session=None, all=False, json=False, project="test")
    cli.cmd_skills_trail(args)
    out = capsys.readouterr().out
    assert "ce-plan" in out and "plan the thing" in out and "sess-abc" in out


# --- injector-directed skills and the honest trail ---------------------------
# The trail must never attribute a call to something that did not cause it. That
# rule is what makes the field necessary: before it, every direction rendered as
# "directed by prompt-gate" no matter who issued it.

from mindhive import replenish as _replenish                       # noqa: E402

TSID = "sess-trail-01"


def _direct(source="replenish_injector", name="ce-code-review"):
    payload = {"skills": [{"name": name}]}
    if source is not None:
        payload["source"] = source
    events.append_event("skill_directed", payload, session_id=TSID,
                        ts="2026-08-02T00:00:00Z", tags=["skill"])


def _used(name="ce-code-review", ts="2026-08-02T00:01:00Z", **over):
    payload = {"name": name}
    payload.update(over)
    events.append_event("skill_used", payload, session_id=TSID, ts=ts,
                        tags=["skill"])


def _why():
    return oversee.skill_trails(evs=events.read_all())[TSID][0]["why"]


def test_an_injector_direction_names_the_injector():
    _direct()
    _used()
    assert _why() == "directed by autonomous replenish loop"


def test_a_row_with_no_source_still_renders_as_the_prompt_gate():
    # Every historical row predates the field; the fold must not change for one.
    _direct(source=None)
    _used()
    assert _why() == "directed by prompt-gate"


def test_the_measured_suffix_still_rides_a_gate_direction():
    events.append_event(
        "skill_directed",
        {"skills": [{"name": "ce-code-review",
                     "measured": {"calls": 4, "ok": 3}}]},
        session_id=TSID, ts="2026-08-02T00:00:00Z", tags=["skill"])
    _used()
    assert _why() == "directed by prompt-gate (measured 3/4)"


def test_an_unknown_source_is_not_attributed_to_the_prompt_gate():
    _direct(source="some-future-writer")
    _used()
    why = _why()
    assert why == "directed"
    assert "prompt-gate" not in why
    assert "some-future-writer" not in why      # never echo the raw value


def test_invocation_args_still_outrank_any_direction():
    _direct()
    _used(args="review the parser change")
    assert "review the parser change" in _why()


def test_consumed_directions_counts_an_injector_direction_the_same():
    _direct()
    _used()
    assert oversee.consumed_directions(evs=events.read_all()) == {
        "ce-code-review": 1}


def _stats(fail=(), ok=()):
    for n in fail:
        events.append_event("tool_failed",
                            {"text": "boom", "signature": f"skill:{n}",
                             "action": "x"}, ts="2026-08-01T00:00:00Z")
    for n in ok:
        events.append_event("invoke_result", {"name": n, "text": "fine"},
                            ts="2026-08-01T00:00:00Z")


def test_a_skill_whose_last_call_failed_is_not_picked():
    _stats(fail=["ce-code-review"], ok=["ce-plan"])
    assert _replenish.pick_skill(["ce-code-review", "ce-plan"]) == "ce-plan"


def test_the_more_followed_skill_wins():
    _stats(ok=["ce-plan", "ce-work"])
    events.append_event("skill_directed", {"skills": [{"name": "ce-work"}]},
                        session_id=TSID, ts="2026-08-01T00:00:00Z")
    events.append_event("skill_used", {"name": "ce-work"}, session_id=TSID,
                        ts="2026-08-01T00:01:00Z")
    assert _replenish.pick_skill(["ce-plan", "ce-work"]) == "ce-work"


def test_an_exact_tie_breaks_on_the_name():
    _stats(ok=["b-skill", "a-skill"])
    assert _replenish.pick_skill(["b-skill", "a-skill"]) == "a-skill"


def test_no_viable_skill_directs_nothing_and_records_nothing():
    # A direction with no basis is a fabricated trail.
    _stats(fail=["only-skill"])
    assert _replenish.direct_skill(["only-skill"], session_id=TSID) == ""
    assert [e for e in events.read_all() if e.type == "skill_directed"] == []


def test_an_empty_name_list_directs_nothing():
    assert _replenish.pick_skill([]) == ""
    assert _replenish.pick_skill(["  "]) == ""


def test_the_direction_is_recorded_before_the_invocation():
    _stats(ok=["ce-plan"])
    assert _replenish.direct_skill(["ce-plan"], session_id=TSID) == "ce-plan"
    row = [e for e in events.read_all() if e.type == "skill_directed"][0]
    assert row.payload["source"] == _replenish.SOURCE_REPLENISH
    assert row.payload["skills"] == [{"name": "ce-plan"}]
    # Recorded first is what lets the consume-once pairing join it to the
    # skill_used that follows; `ts=None` stamps now, after the direction.
    _used(name="ce-plan", ts=None)
    assert _why() == "directed by autonomous replenish loop"


def test_append_false_picks_without_recording():
    _stats(ok=["ce-plan"])
    assert _replenish.direct_skill(["ce-plan"], session_id=TSID,
                                   append=False) == "ce-plan"
    assert [e for e in events.read_all() if e.type == "skill_directed"] == []


# --- what the card says it is working on, and what is in the way -------------
#
# WHY. The card folded `task` out of every `state_set` and then deleted it on
# the way out (`s.pop("task", None)`), so the one field that names what the
# session is actually working on survived only as a title fallback -- and the
# user reading the Oversee card saw a prompt headline where they had asked for
# the task. The problems half had no producer at all: `open_question` events
# were folded by nothing, so "ada masalah apa?" had no answer on the surface
# built to answer it.

def test_the_card_carries_the_task_it_was_told_it_is_working_on():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "look at the mirror"}, 1)
    _ev("state_set", {"task": "Port the Postgres read mirror",
                      "next_step": "measure the numeric round trip"}, 2)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["task"] == "Port the Postgres read mirror"
    # The title stays the prompt headline: the two answer different questions
    # and collapsing them is what hid the task in the first place.
    assert s["title"] == "look at the mirror"
    assert "measure the numeric round trip" in s["planned"]


def test_the_newest_state_set_wins_the_task():
    """`state_set` is last-one-wins state, and the fold has to agree with the
    projections that already treat it that way."""
    _ev("session_start", {}, 0)
    _ev("state_set", {"task": "first framing"}, 1)
    _ev("state_set", {"task": "what it actually turned into"}, 2)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["task"] == "what it actually turned into"


def test_an_empty_task_does_not_erase_the_one_already_folded():
    """A later `state_set` that omits the task is not a retraction. Without the
    guard the card blanks out mid-session for a payload that said nothing."""
    _ev("session_start", {}, 0)
    _ev("state_set", {"task": "Port the mirror"}, 1)
    _ev("state_set", {"next_step": "keep going"}, 2)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["task"] == "Port the mirror"


def test_open_questions_become_the_cards_blockers():
    _ev("session_start", {}, 0)
    _ev("open_question", {"text": "does numeric keep the sign of zero?"}, 1)
    _ev("open_question", {"text": "is the pg tier reachable from here?"}, 2)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["blockers"] == ["does numeric keep the sign of zero?",
                             "is the pg tier reachable from here?"]


def test_a_session_with_no_open_questions_has_an_empty_blockers_list():
    """Shape stability: the renderer reads `blockers` on every card, and a key
    that appears only sometimes is how a surface starts throwing on the quiet
    case instead of the loud one."""
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "nothing is wrong"}, 1)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["blockers"] == [] and s["task"] == ""


# --- a red CI run turns the session's card red (blueprint P1g) --------------------------


def _ci(session, code, pr=7, gate="gate2_suite", scope="ci", **extra):
    from mindhive import verify
    events.append_event("verification", {
        "text": f"hive ci: PR #{pr}", "argv": ["python", "-m", "pytest"],
        "exit_code": code, "counts": {"passed": 1}, "excerpt": "",
        "verified_by": verify.VERIFIED_BY, "scope": scope, "gate": gate,
        "pr_id": pr, "branch": "b", "commit_sha": "", "mode": "offline",
        "merge_state": "skipped", **extra}, tags=["verify"], session_id=session)


def test_a_red_ci_run_marks_the_session_failed():
    events.append_event("user_prompt", {"text": "land the branch"},
                        session_id="s-ci")
    _ci("s-ci", 1)
    card = oversee.interactive_sessions()[0]
    assert card["status"] == "failed"
    assert card["ci"]["exit_code"] == 1
    assert any("PR #7" in b for b in card["blockers"]), card["blockers"]


def test_a_green_ci_run_leaves_the_card_running():
    events.append_event("user_prompt", {"text": "land the branch"},
                        session_id="s-ci")
    _ci("s-ci", 0)
    card = oversee.interactive_sessions()[0]
    assert card["status"] == "running"
    assert card["ci"]["exit_code"] == 0
    assert card["blockers"] == []


def test_a_later_green_run_clears_the_red():
    """The LAST CI run is the answer. A card stuck red after the fix landed
    is a card the operator learns to ignore."""
    events.append_event("user_prompt", {"text": "land it"}, session_id="s-ci")
    _ci("s-ci", 1)
    _ci("s-ci", 0)
    assert oversee.interactive_sessions()[0]["status"] == "running"


def test_a_verification_that_is_not_a_ci_run_never_reds_the_card():
    """A failing `hive selftest` is a red suite, not a red CI gate, and the
    two are different claims about different runs."""
    events.append_event("user_prompt", {"text": "work"}, session_id="s-ci")
    _ci("s-ci", 1, scope="full")
    card = oversee.interactive_sessions()[0]
    assert card["status"] == "running"
    assert card["ci"] is None


@pytest.mark.parametrize("code,verdict,status", [(3, "green", "running"),
                                                  (1, "red", "failed"),
                                                  (2, "error", "failed")])
def test_a_premerge_run_is_judged_by_its_verdict_not_its_exit_code(code, verdict, status):
    """`hive ci premerge --merge` exits 3 for a green run whose merge did not
    happen. `hive ci status` reads that as green, and so must the card."""
    events.append_event("user_prompt", {"text": "land it"}, session_id="s-ci")
    _ci("s-ci", code, gate="premerge", verdict=verdict)
    card = oversee.interactive_sessions()[0]
    assert card["status"] == status
    assert card["ci"]["verdict"] == verdict
    assert bool(card["blockers"]) == (status == "failed")


def test_every_card_carries_the_ci_key_even_with_no_ci_run():
    events.append_event("user_prompt", {"text": "work"}, session_id="s-ci")
    card = oversee.interactive_sessions()[0]
    assert "ci" in card and card["ci"] is None


def test_the_status_glyph_reads_failed_before_it_reads_chat():
    """`ovStatus` answers on `kind === 'chat'` first, so a `failed` branch
    placed after it would never be reached by an interactive card -- which is
    the only kind a CI run ever attaches to."""
    from mindhive import dashboard_js_oversee as js
    body = js.JS
    fail_at = body.index("r.status === 'failed'")
    chat_at = body.index("r.kind === 'chat'")
    assert fail_at < chat_at, "the failed branch is unreachable behind chat"
    assert "sbad" in body[fail_at:fail_at + 200]
