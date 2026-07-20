# tests/test_events.py
from mindhive import events, paths


def test_read_all_skips_malformed_rows_and_stays_total(tmp_path):
    # Ledger-schema SOP: reads stay TOTAL. A torn/interleaved write from a past
    # session — a line of unparseable JSON, or a JSON object missing a required
    # Event field — must be SKIPPED, never raised. Every dashboard endpoint and
    # every `hive` CLI command reads via read_all; one corrupt byte-run must not
    # take down the whole hive (it did: a torn `command_run` fragment 500'd every
    # dashboard section and crashed every CLI command).
    events.append_event("decision", {"text": "before"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    seg = next(paths.events_dir("test").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as f:
        f.write('{"ts":"2026-06-20T11:00:00Z","type":"decision"}\n')  # missing fields
        f.write(':"command_run"}\n')                                   # torn fragment
    events.append_event("decision", {"text": "after"}, session_id="s",
                        ts="2026-06-20T12:00:00Z")
    evs = events.read_all("test")
    assert [e.payload.get("text") for e in evs] == ["before", "after"]


def test_corrupt_rows_surfaces_skipped_rows(tmp_path):
    # The strict companion to the total read: read_all() drops malformed rows so
    # folds never crash; corrupt_rows() SURFACES them so `hive verify` and the
    # dashboard can report corruption instead of it passing silently.
    events.append_event("decision", {"text": "ok"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    seg = next(paths.events_dir("test").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as f:
        f.write('{"ts":"2026-06-20T11:00:00Z","type":"decision"}\n')  # missing fields
        f.write(':"command_run"}\n')                                   # torn fragment
    bad = events.corrupt_rows("test")
    assert len(bad) == 2
    joined = " ".join(r["reason"] for r in bad)
    assert "missing required field" in joined
    assert "unparseable json" in joined
    assert all(r["segment"] == seg.name for r in bad)


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


def test_rule_revised_is_a_valid_deterministic_type():
    assert "rule_revised" in events.DETERMINISTIC_TYPES
    assert "rule_revised" not in events.SEMANTIC_TYPES
    ev = events.append_event("rule_revised",
                             {"signature": "pymodule:x", "text": "do Y", "source": "human"},
                             session_id="s1", tags=["reconcile"], project="test")
    assert ev.type == "rule_revised"


def test_skill_used_is_a_valid_deterministic_type():
    # U5: without this registration append_event raises and every Skill-tool
    # capture would be silently dropped to hooks_errors.log
    assert "skill_used" in events.DETERMINISTIC_TYPES
    assert "skill_used" not in events.SEMANTIC_TYPES
    ev = events.append_event("skill_used",
                             {"name": "ce-plan", "plugin": "compound-engineering",
                              "ok": True, "text": "done"},
                             session_id="s1", tags=["auto"], project="test")
    assert ev.type == "skill_used"


def test_lifecycle_event_types_are_valid():
    for t in ("rule_activated", "rule_promoted", "rule_demoted"):
        assert t in events.DETERMINISTIC_TYPES
        assert t not in events.SEMANTIC_TYPES
    ev = events.append_event("rule_promoted", {"signature": "pymodule:x"},
                             session_id="s", tags=["promote"], project="test")
    assert ev.type == "rule_promoted"


def test_loop_run_dismissed_is_valid_deterministic_type():
    assert "loop_run_dismissed" in events.DETERMINISTIC_TYPES
    assert "loop_run_dismissed" not in events.SEMANTIC_TYPES
    ev = events.append_event("loop_run_dismissed",
                             {"run_id": "abc123", "cause": "manual"},
                             session_id="s", project="test")
    assert ev.type == "loop_run_dismissed"


def test_learning_is_a_valid_semantic_type():
    # regression guard: research._record_learning appends type "learning";
    # before it was registered, append_event rejected it and the controller's
    # crash guard silently dropped every PRD from the ledger
    assert "learning" in events.SEMANTIC_TYPES
    assert "learning" in events.MEMORY_VIEW_TYPES
    ev = events.append_event("learning", {"text": "Self-Reliant learning 1",
                                          "report": "PRD body"},
                             session_id="research-ab12cd34",
                             tags=["self-reliant", "learning"], project="test")
    assert ev.type == "learning"


def test_origin_of_marks_self_reliant_output():
    # tag mark (append-time) and session mark (research-<run_id> stream) both
    # resolve to the one provenance label; everything else stays unlabeled
    assert events.origin_of("research-ab12cd34") == "self-reliant"
    assert events.origin_of("sess-1", ["self-reliant"]) == "self-reliant"
    assert events.origin_of("sess-1", ["swarm"]) is None
    assert events.origin_of("sess-1") is None
    assert events.origin_of(None) is None
    assert events.origin_of("researchy-session") is None   # prefix, not substring
