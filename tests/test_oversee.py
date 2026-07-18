# tests/test_oversee.py — interactive-session Oversee projection.
import types
from datetime import datetime

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
