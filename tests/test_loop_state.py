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
    # One contract for the whole group: iteration summaries and checklist items
    # arrive in the same {text, is_new} shape, so the renderer never has to ask
    # which kind of entry it is holding.
    assert {"text": "iter 1: did scaffold", "is_new": False} in r["done"]
    assert {"text": "scaffold", "is_new": False} in r["done"]
    assert r["planned"] == [{"text": "wire API", "is_new": False}]
    assert r["not_yet"] == [{"text": "polish", "is_new": False}]


def test_list_runs_marks_injected_checklist_items(tmp_path):
    goal = tmp_path / ".hive-loop"
    goal.mkdir()
    (goal / "fix_plan.md").write_text(
        "- [ ] wire API\n- [ ] (new) Bootstrap the suite\n", encoding="utf-8")
    _seed_run("bbb", 10, goal_dir=str(goal))
    r = state.list_runs("test",
                        now_fn=lambda: _epoch("2026-07-07T10:03:00Z"))["runs"][0]
    assert r["planned"] == [{"text": "wire API", "is_new": False},
                            {"text": "Bootstrap the suite", "is_new": True}]


def test_dismissed_live_run_resurfaces_on_new_events():
    _seed_run("aaa", 10)                                   # running
    events.append_event("loop_run_dismissed", {"run_id": "aaa", "cause": "manual"},
                        ts="2026-07-07T10:05:00Z")
    hidden = state.list_runs("test", now_fn=lambda: _epoch("2026-07-07T10:06:00Z"))
    assert hidden["runs"] == [] and hidden["dismissed"] == 1
    # the run was actually still alive: a later iteration resurfaces it
    events.append_event("loop_iteration", {"run_id": "aaa", "iteration": 2,
                        "summary": "still going", "cost_usd": 0.1, "total_tokens": 50},
                        ts="2026-07-07T10:07:00Z")
    res = state.list_runs("test", now_fn=lambda: _epoch("2026-07-07T10:08:00Z"))
    assert [r["run_id"] for r in res["runs"]] == ["aaa"]
    assert res["dismissed"] == 0


def test_online_window_uses_per_run_heartbeat():
    events.append_event("loop_run_start",
                        {"run_id": "aaa", "title": "T", "goal_dir": "d",
                         "heartbeat_seconds": 3600},        # 60-min iterations
                        ts="2026-07-07T10:00:00Z")
    events.append_event("loop_iteration", {"run_id": "aaa", "iteration": 1,
                        "summary": "s", "cost_usd": 0.1, "total_tokens": 10},
                        ts="2026-07-07T10:01:00Z")
    # 40 min after the last event: stale under the 16-min default, alive
    # under the run's own 60-min window
    res = state.list_runs("test", now_fn=lambda: _epoch("2026-07-07T10:41:00Z"))
    assert res["runs"][0]["online"] is True


def test_fix_plan_read_requires_hive_loop_dir(tmp_path):
    stray = tmp_path / "not-a-loop-dir"
    stray.mkdir()
    (stray / "fix_plan.md").write_text("- [ ] should not be read\n", encoding="utf-8")
    _seed_run("aaa", 10, goal_dir=str(stray))
    res = state.list_runs("test", now_fn=lambda: _epoch("2026-07-07T10:03:00Z"))
    assert res["runs"][0]["planned"] == []


# --- the injected count on the loop card -------------------------------------

def test_a_run_reports_how_many_items_it_injected():
    _seed_run("ccc", 10)
    for i in range(3):
        events.append_event(
            "work_injected",
            {"outcome": "landed", "round": 1, "surface": "loop", "lens": "l",
             "title": f"item {i}", "fingerprint": f"fp-{i}",
             "evidence": ["e"], "run_id": "ccc"},
            ts="2026-07-07T10:02:00Z")
    r = state.list_runs("test",
                        now_fn=lambda: _epoch("2026-07-07T10:03:00Z"))["runs"][0]
    assert r["injected_count"] == 3


def test_rows_for_another_run_do_not_count():
    _seed_run("ccc", 10)
    events.append_event(
        "work_injected",
        {"outcome": "landed", "round": 1, "surface": "loop", "lens": "l",
         "title": "elsewhere", "fingerprint": "fp-x", "evidence": ["e"],
         "run_id": "another-run"}, ts="2026-07-07T10:02:00Z")
    r = state.list_runs("test",
                        now_fn=lambda: _epoch("2026-07-07T10:03:00Z"))["runs"][0]
    assert r["injected_count"] == 0


def test_only_landed_rows_count():
    _seed_run("ccc", 10)
    events.append_event(
        "work_injected",
        {"outcome": "refused", "round": 1, "surface": "loop", "lens": "l",
         "title": "declined", "fingerprint": "fp-r", "reason": "no evidence",
         "run_id": "ccc"}, ts="2026-07-07T10:02:00Z")
    r = state.list_runs("test",
                        now_fn=lambda: _epoch("2026-07-07T10:03:00Z"))["runs"][0]
    assert r["injected_count"] == 0


def test_a_malformed_injection_row_is_skipped_and_the_fold_returns():
    # The ledger holds rows written by every past version of the injector; a
    # 500 on the Oversee projection is not an acceptable way to learn that.
    _seed_run("ccc", 10)
    events.append_event("work_injected", ["not", "a", "dict"],
                        ts="2026-07-07T10:02:00Z")
    events.append_event("work_injected",
                        {"outcome": "landed", "round": 1, "surface": "loop",
                         "run_id": None}, ts="2026-07-07T10:02:01Z")
    r = state.list_runs("test",
                        now_fn=lambda: _epoch("2026-07-07T10:03:00Z"))["runs"][0]
    assert r["injected_count"] == 0


def test_a_run_with_no_injections_reports_zero():
    _seed_run("ccc", 10)
    r = state.list_runs("test",
                        now_fn=lambda: _epoch("2026-07-07T10:03:00Z"))["runs"][0]
    assert r["injected_count"] == 0
