# tests/test_loop_driver.py
import json
import subprocess
from mindhive.loop import driver


def test_build_argv_uses_resume_not_continue():
    argv = driver.build_argv(
        cmd="claude", prompt="do x", loop_context="ctx",
        allowed_tools="Read,Edit", output_format="json",
        session_id="sess-1", model=None, permission_mode=None)
    assert argv[0] == "claude"
    assert "--resume" in argv and "sess-1" in argv
    assert "--continue" not in argv
    assert "--output-format" in argv and "json" in argv
    assert "--allowedTools" in argv
    assert "--append-system-prompt" in argv
    assert argv[-2] == "-p" and argv[-1] == "do x"


def test_build_argv_emits_permission_mode_not_effort():
    argv = driver.build_argv(
        cmd="claude", prompt="p", loop_context="c",
        allowed_tools="Read", output_format="json",
        session_id=None, model="claude-opus-4-8", permission_mode="acceptEdits")
    assert "--resume" not in argv
    assert "--model" in argv and "claude-opus-4-8" in argv
    # --effort is NOT a real Claude Code CLI flag; it must never be emitted.
    assert "--effort" not in argv
    assert "--permission-mode" in argv and "acceptEdits" in argv


def test_cli_driver_resolves_cmd_via_which(monkeypatch):
    """On Windows `claude` is a .CMD shim that subprocess.run(["claude", ...])
    can't launch (WinError 2 -> spawn_failed). Resolve to the real path so the
    loop iteration can actually start the CLI."""
    monkeypatch.setattr(driver.shutil, "which",
                        lambda name: "C:/npm/claude.CMD" if name == "claude" else None)
    assert driver.CLIDriver().cmd == "C:/npm/claude.CMD"


def test_cli_driver_falls_back_to_bare_cmd_when_unresolved(monkeypatch):
    monkeypatch.setattr(driver.shutil, "which", lambda name: None)
    assert driver.CLIDriver("claude").cmd == "claude"


def test_run_timeout_returns_error_not_raises(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(driver.subprocess, "run", boom)
    r = driver.CLIDriver("claude").run(
        "p", "ctx", allowed_tools="Read", session_id="s",
        model=None, permission_mode="acceptEdits", timeout_s=1)
    assert r.error == "timeout"
    assert r.rate_limited is False
    assert r.session_id is None
    assert r.text == ""


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
