# tests/test_loop_state.py
from mindhive import events
from mindhive.loop import state


def test_last_run_status_summarizes_events():
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


def test_last_run_status_none_when_no_runs():
    assert state.last_run_status("test") is None


def _seed_run(run_id, hour, reason=None, title="Build it", goal_dir="d",
              summaries=("first pass",)):
    events.append_event("loop_run_start",
                        {"run_id": run_id, "title": title, "goal_dir": goal_dir},
                        ts=f"2026-07-07T{hour:02d}:00:00Z")
    for n, s in enumerate(summaries, start=1):
        events.append_event("loop_iteration",
                            {"run_id": run_id, "iteration": n, "summary": s,
                             "cost_usd": 0.1, "total_tokens": 100},
                            ts=f"2026-07-07T{hour:02d}:0{n}:00Z")
    if reason:
        events.append_event("loop_run_end", {"run_id": run_id, "reason": reason},
                            ts=f"2026-07-07T{hour:02d}:59:00Z")


def _epoch(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def test_list_runs_separates_interleaved_runs():
    events.append_event("loop_run_start", {"run_id": "aaa", "title": "A", "goal_dir": "x"},
                        ts="2026-07-07T10:00:00Z")
    events.append_event("loop_run_start", {"run_id": "bbb", "title": "B", "goal_dir": "y"},
                        ts="2026-07-07T10:00:30Z")
    events.append_event("loop_iteration", {"run_id": "aaa", "iteration": 1,
                        "summary": "a1", "cost_usd": 0.1, "total_tokens": 100},
                        ts="2026-07-07T10:01:00Z")
    events.append_event("loop_iteration", {"run_id": "bbb", "iteration": 1,
                        "summary": "b1", "cost_usd": 0.2, "total_tokens": 200},
                        ts="2026-07-07T10:02:00Z")
    res = state.list_runs("test", now_fn=lambda: _epoch("2026-07-07T10:03:00Z"))
    runs = {r["run_id"]: r for r in res["runs"]}
    assert set(runs) == {"aaa", "bbb"}
    assert runs["aaa"]["iterations"] == 1
    assert runs["aaa"]["total_tokens"] == 100
    assert runs["bbb"]["total_tokens"] == 200


def test_list_runs_online_tracks_heartbeat():
    _seed_run("aaa", 10)  # running: no end event
    fresh = state.list_runs("test", now_fn=lambda: _epoch("2026-07-07T10:02:00Z"))
    stale = state.list_runs("test", now_fn=lambda: _epoch("2026-07-07T11:30:00Z"))
    assert fresh["runs"][0]["status"] == "running"
    assert fresh["runs"][0]["online"] is True
    assert stale["runs"][0]["online"] is False


def test_list_runs_legacy_events_without_run_id():
    events.append_event("loop_run_start", {"goal_dir": "d"}, ts="2026-07-07T09:00:00Z")
    events.append_event("loop_iteration", {"iteration": 1, "summary": "old",
                        "cost_usd": 0.1, "total_tokens": 50}, ts="2026-07-07T09:01:00Z")
    events.append_event("loop_run_end", {"reason": "max_iterations"},
                        ts="2026-07-07T09:02:00Z")
    res = state.list_runs("test", now_fn=lambda: _epoch("2026-07-07T09:03:00Z"))
    assert len(res["runs"]) == 1
    assert res["runs"][0]["status"] == "max_iterations"
    assert res["runs"][0]["iterations"] == 1


def test_list_runs_excludes_dismissed():
    _seed_run("aaa", 10, reason="completed")
    events.append_event("loop_run_dismissed", {"run_id": "aaa", "cause": "completed"},
                        ts="2026-07-07T10:59:30Z")
    _seed_run("bbb", 11)
    res = state.list_runs("test", now_fn=lambda: _epoch("2026-07-07T11:02:00Z"))
    assert [r["run_id"] for r in res["runs"]] == ["bbb"]
    assert res["dismissed"] == 1


def test_list_runs_progress_groups(tmp_path):
    goal = tmp_path / ".hive-loop"
    goal.mkdir()
    (goal / "fix_plan.md").write_text(
        "- [x] scaffold\n- [ ] wire API\n## Future / Optional\n- [ ] polish\n",
        encoding="utf-8")
    _seed_run("aaa", 10, goal_dir=str(goal), summaries=("did scaffold", "wiring now"))
    res = state.list_runs("test", now_fn=lambda: _epoch("2026-07-07T10:03:00Z"))
    r = res["runs"][0]
    assert r["title"] == "wiring now"          # latest summary supersedes PROMPT title
    assert r["doing"] == "wiring now"
    assert "iter 1: did scaffold" in r["done"]
    assert "scaffold" in r["done"]
    assert r["planned"] == ["wire API"]
    assert r["not_yet"] == ["polish"]
