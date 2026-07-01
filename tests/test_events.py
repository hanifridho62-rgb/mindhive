# tests/test_events.py
from mindhive import events, paths


def test_read_all_fails_loud_on_missing_required_field(tmp_path):
    import json
    import pytest
    events.append_event("decision", {"text": "ok"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    # Append a structurally valid JSON line that lacks required fields.
    with open(seg, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-06-20T11:00:00Z", "type": "decision"}) + "\n")
    with pytest.raises(json.JSONDecodeError, match="missing required field"):
        events.read_all("test")


def test_canonical_json_is_stable():
    a = events.canonical_json({"b": 1, "a": 2})
    b = events.canonical_json({"a": 2, "b": 1})
    assert a == b == '{"a":2,"b":1}'

def test_append_rejects_unknown_type():
    import pytest
    with pytest.raises(ValueError):
        events.append_event("bogus", {"text": "x"})

def test_user_prompt_is_valid_and_in_memory_view():
    ev = events.append_event("user_prompt", {"text": "hello"}, session_id="s",
                             ts="2026-06-23T10:00:00Z")
    assert ev.type == "user_prompt"
    assert "user_prompt" in events.VALID_TYPES
    # shows in the dashboard Memory view, but is NOT a curated semantic type
    assert "user_prompt" in events.MEMORY_VIEW_TYPES
    assert "user_prompt" not in events.SEMANTIC_TYPES

def test_append_and_read_orders_by_ts():
    events.append_event("decision", {"text": "second"}, session_id="s1",
                        ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "first"}, session_id="s2",
                        ts="2026-06-20T09:00:00Z")
    evs = events.read_all()
    assert [e.payload["text"] for e in evs] == ["first", "second"]

def test_segments_are_per_session():
    events.append_event("caveat", {"text": "x"}, session_id="abc",
                        ts="2026-06-20T10:00:00Z")
    assert (paths.events_dir() / "abc.jsonl").exists()


def test_assistant_response_is_valid_and_in_memory_view():
    ev = events.append_event("assistant_response", {"text": "ok"}, session_id="s",
                             ts="2026-06-23T10:00:00Z")
    assert ev.type == "assistant_response"
    assert "assistant_response" in events.MEMORY_VIEW_TYPES
    assert "assistant_response" not in events.SEMANTIC_TYPES


def test_tool_failed_is_a_valid_deterministic_type():
    assert "tool_failed" in events.VALID_TYPES
    assert "tool_failed" in events.DETERMINISTIC_TYPES
    # deterministic, not semantic: must stay out of the LEDGER/graph intent layer
    assert "tool_failed" not in events.SEMANTIC_TYPES
    ev = events.append_event("tool_failed", {"text": "boom", "signature": "pymodule:x"},
                             session_id="s1", tags=["auto", "failure"], project="test")
    assert ev.type == "tool_failed"
