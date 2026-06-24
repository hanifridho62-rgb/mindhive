# tests/test_loop_state.py
from mindhive import events
from mindhive.loop import state


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def test_last_run_status_summarizes_events(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("loop_run_start", {"goal_dir": "d"}, ts="2026-06-24T10:00:00Z")
    events.append_event("loop_iteration", {"iteration": 1, "cost_usd": 0.1,
                        "total_tokens": 500}, ts="2026-06-24T10:01:00Z")
    events.append_event("loop_iteration", {"iteration": 2, "cost_usd": 0.2,
                        "total_tokens": 700}, ts="2026-06-24T10:02:00Z")
    events.append_event("loop_run_end", {"reason": "completed", "iterations": 2},
                        ts="2026-06-24T10:03:00Z")
    s = state.last_run_status("test")
    assert s["reason"] == "completed"
    assert s["iterations"] == 2
    assert round(s["total_cost_usd"], 2) == 0.30
    assert s["total_tokens"] == 1200


def test_last_run_status_none_when_no_runs(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert state.last_run_status("test") is None
