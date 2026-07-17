# tests/test_loop_events.py
from mindhive import events


def test_loop_types_registered():
    for t in ("loop_run_start", "loop_iteration", "loop_run_end"):
        assert t in events.VALID_TYPES
        assert t in events.DETERMINISTIC_TYPES
        assert t not in events.SEMANTIC_TYPES


def test_append_loop_iteration():
    ev = events.append_event("loop_iteration", {"iteration": 1, "summary": "did x"},
                             session_id="s", ts="2026-06-24T10:00:00Z")
    assert ev.type == "loop_iteration"
    assert events.read_all()[0].payload["iteration"] == 1
