# tests/test_taskview.py
import json
import time
from mindhive import taskview
from mindhive.events import Event


def _ev(etype, payload, ts="2026-07-09T10:00:00Z", sid="s1"):
    return Event(ts=ts, session_id=sid, type=etype, payload=payload)


def test_status_of_boundary():
    now = time.time()
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60)) + "Z"
    stale = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 3600)) + "Z"
    assert taskview.status_of(fresh, now=now) == "ongoing"
    assert taskview.status_of(stale, now=now) == "completed"
    assert taskview.status_of(None, now=now) == "completed"
    assert taskview.status_of("garbage", now=now) == "completed"


def test_tally_counts_and_line_sums():
    evs = [
        _ev("file_touched", {"text": "a.py", "plus": 10, "minus": 4}),
        _ev("file_touched", {"text": "a.py", "plus": 2, "minus": 1}),  # same file
        _ev("file_touched", {"text": "b.py"}),                          # no stats
        _ev("command_run", {"text": "pytest -q"}),
        _ev("caveat", {"text": "plan FIRST"}),
        _ev("tool_failed", {"signature": "skill:x", "text": "boom"}),
        _ev("loop_iteration", {"iteration": 1, "summary": "did a thing"}),
        _ev("user_prompt", {"text": "ignored"}),
    ]
    t = taskview.tally(evs)
    assert t == {"files": 2, "plus": 12, "minus": 5, "commands": 1,
                 "notes": 1, "failures": 1, "loops": 1}


def test_items_maps_kinds_and_caps_to_last_100():
    evs = [_ev("command_run", {"text": f"cmd {i}"}) for i in range(150)]
    evs.append(_ev("file_touched", {"text": "z.py", "plus": 1, "minus": 0,
                                    "tool": "Edit"}))
    out = taskview.items(evs)
    assert out["total"] == 151
    assert out["truncated"] == 51
    assert len(out["items"]) == 100
    assert out["items"][-1] == {"kind": "touched", "text": "z.py",
                                "ts": "2026-07-09T10:00:00Z",
                                "plus": 1, "minus": 0, "tool": "Edit"}
    assert out["items"][0]["text"] == "cmd 51"     # oldest 51 dropped


def test_items_loop_row_carries_engine_fields():
    evs = [_ev("loop_iteration", {
        "iteration": 3, "summary": "fixed breaker", "progress": True,
        "cost_usd": 0.12, "total_tokens": 900,
        "context_fill_ratio": 0.4, "breaker_state": "CLOSED"})]
    row = taskview.items(evs)["items"][0]
    assert row["kind"] == "loop" and row["text"] == "fixed breaker"
    assert row["iteration"] == 3 and row["progress"] is True
    assert row["breaker_state"] == "CLOSED"
    assert row["cost_usd"] == 0.12 and row["total_tokens"] == 900


def test_find_transcript_rejects_traversal_ids(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "good-1.jsonl").write_text("{}", encoding="utf-8")
    assert taskview.find_transcript("good-1", root=tmp_path) is not None
    assert taskview.find_transcript("../evil", root=tmp_path) is None
    assert taskview.find_transcript("..\\evil", root=tmp_path) is None
    assert taskview.find_transcript("", root=tmp_path) is None
    assert taskview.find_transcript(None, root=tmp_path) is None


def _transcript_lines():
    use = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Edit",
         "input": {"file_path": "src/a.py", "old_string": "x = 1",
                   "new_string": "x = 2"}},
        {"type": "tool_use", "id": "t2", "name": "Bash",
         "input": {"command": "pytest -q", "description": "Run tests"}},
    ]}}
    res = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2",
         "content": [{"type": "text", "text": "3 passed"}]},
    ]}}
    return [json.dumps(use), "not json {", json.dumps(res)]


def test_detail_parses_transcript_and_pairs_results(tmp_path):
    d = tmp_path / "enc-proj"
    d.mkdir()
    (d / "sess-9.jsonl").write_text("\n".join(_transcript_lines()),
                                    encoding="utf-8")
    out = taskview.detail("sess-9", root=tmp_path)
    assert "error" not in out
    kinds = [(i["kind"], i["text"]) for i in out["items"]]
    assert kinds == [("touched", "src/a.py"), ("ran", "pytest -q")]
    assert "-x = 1" in out["items"][0]["diff"]
    assert "+x = 2" in out["items"][0]["diff"]
    assert out["items"][1]["output"] == "3 passed"
    assert out["items"][1]["desc"] == "Run tests"


def test_detail_missing_transcript_degrades(tmp_path):
    out = taskview.detail("nope", root=tmp_path)
    assert out == {"error": "transcript_unavailable", "items": []}
