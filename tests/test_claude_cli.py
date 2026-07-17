# tests/test_claude_cli.py
"""Shared headless-`claude` helper — one home for executable resolution, the
one-shot spawn, and envelope parsing (previously copy-pasted across invoker.py
and reflect.py)."""
from mindhive import claude_cli


class _Proc:
    returncode = 0
    stdout = '{"is_error": false, "result": "hi"}'
    stderr = ""


def test_resolve_exe_uses_which(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which",
                        lambda name: "C:/npm/claude.CMD" if name == "claude" else None)
    assert claude_cli.resolve_exe() == "C:/npm/claude.CMD"


def test_resolve_exe_falls_back_to_bare_name(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert claude_cli.resolve_exe() == "claude"


def test_run_headless_builds_argv_with_resolved_exe(monkeypatch):
    import shutil
    captured = {}
    monkeypatch.setattr(shutil, "which",
                        lambda name: "C:/npm/claude.CMD" if name == "claude" else None)
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda argv, **kw: captured.update(argv=argv) or _Proc())
    out = claude_cli.run_headless("do x")
    assert out == {"exit": 0, "stdout": '{"is_error": false, "result": "hi"}'}
    argv = captured["argv"]
    assert argv[0] == "C:/npm/claude.CMD"           # resolved, not bare "claude"
    assert "-p" in argv and "do x" in argv
    assert "--output-format" in argv and "json" in argv


def test_run_headless_maps_missing_cli_to_127(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no claude")
    monkeypatch.setattr(claude_cli.subprocess, "run", boom)
    assert claude_cli.run_headless("x") == {"exit": 127, "stdout": ""}


def test_run_headless_maps_timeout_to_124(monkeypatch):
    import subprocess
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(claude_cli.subprocess, "run", boom)
    assert claude_cli.run_headless("x") == {"exit": 124, "stdout": ""}


def test_envelope_ok_true_on_success():
    assert claude_cli.envelope_ok('{"is_error": false, "result": "ok"}') is True


def test_envelope_ok_false_on_is_error_or_garbage():
    assert claude_cli.envelope_ok('{"is_error": true, "result": "401"}') is False
    assert claude_cli.envelope_ok("not json") is False
    assert claude_cli.envelope_ok("") is False


def test_result_text_extracts_result_but_empty_on_error():
    assert claude_cli.result_text('{"is_error": false, "result": "the text"}') == "the text"
    assert claude_cli.result_text('{"is_error": true, "result": "boom"}') == ""
    assert claude_cli.result_text("not json") == ""


def test_error_text_carries_status_and_message():
    out = ('{"is_error": true, "api_error_status": 429, '
           '"result": "You\'ve hit your weekly limit"}')
    assert claude_cli.error_text(out) == "429: You've hit your weekly limit"


def test_error_text_message_only_and_status_only():
    assert claude_cli.error_text('{"is_error": true, "result": "boom"}') == "boom"
    assert claude_cli.error_text(
        '{"is_error": true, "api_error_status": 500, "result": ""}') == "api error 500"
    assert claude_cli.error_text('{"is_error": true}') == "is_error envelope"


def test_error_text_empty_on_success_or_garbage():
    # a success envelope's result must never be reported as an error cause
    assert claude_cli.error_text('{"is_error": false, "result": "fine"}') == ""
    assert claude_cli.error_text("not json") == ""
