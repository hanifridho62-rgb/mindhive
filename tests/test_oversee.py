# tests/test_oversee.py — interactive-session Oversee projection.
from datetime import datetime

from mindhive import events, oversee


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
