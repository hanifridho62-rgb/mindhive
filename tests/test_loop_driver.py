# tests/test_loop_driver.py
import json
from mindhive.loop import driver


def test_build_argv_uses_resume_not_continue():
    argv = driver.build_argv(
        cmd="claude", prompt="do x", loop_context="ctx",
        allowed_tools="Read,Edit", output_format="json",
        session_id="sess-1", model=None, effort=None)
    assert argv[0] == "claude"
    assert "--resume" in argv and "sess-1" in argv
    assert "--continue" not in argv
    assert "--output-format" in argv and "json" in argv
    assert "--allowedTools" in argv
    assert "--append-system-prompt" in argv
    assert argv[-2] == "-p" and argv[-1] == "do x"


def test_build_argv_omits_resume_when_no_session():
    argv = driver.build_argv(
        cmd="claude", prompt="p", loop_context="c",
        allowed_tools="Read", output_format="json",
        session_id=None, model="claude-opus-4-8", effort="high")
    assert "--resume" not in argv
    assert "--model" in argv and "claude-opus-4-8" in argv
    assert "--effort" in argv and "high" in argv


def test_parse_result_extracts_usage_and_session():
    blob = json.dumps({
        "type": "result", "subtype": "success",
        "result": "all good\nLOOP_STATUS:\n  EXIT_SIGNAL: true\n",
        "total_cost_usd": 0.42, "num_turns": 3, "session_id": "sess-9",
        "usage": {"input_tokens": 8000, "output_tokens": 1200,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 42000},
    })
    r = driver.parse_result(blob)
    assert "LOOP_STATUS" in r.text
    assert r.usage.input_tokens == 8000
    assert r.usage.cache_read_input_tokens == 42000
    assert r.usage.cost_usd == 0.42
    assert r.session_id == "sess-9"
    assert r.usage.context_occupancy == 50000
    assert r.rate_limited is False


def test_parse_result_detects_rate_limit():
    blob = '{"rate_limit_event": {"status": "rejected"}}'
    r = driver.parse_result(blob)
    assert r.rate_limited is True


def test_parse_result_non_json_falls_back_to_text():
    r = driver.parse_result("not json at all")
    assert r.text == "not json at all"
    assert r.usage.total_tokens == 0
    assert r.session_id is None
