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
    monkeypatch.setattr(
        claude_cli.subprocess, "run",
        lambda argv, **kw: captured.update(argv=argv, kw=kw) or _Proc())
    out = claude_cli.run_headless("do x")
    assert out == {"exit": 0, "stdout": '{"is_error": false, "result": "hi"}'}
    argv = captured["argv"]
    assert argv[0] == "C:/npm/claude.CMD"           # resolved, not bare "claude"
    assert argv[-1] == "-p"
    assert "--output-format" in argv and "json" in argv
    # The prompt rides stdin, never argv.
    assert "do x" not in argv
    assert captured["kw"]["input"] == "do x"


def test_run_headless_feeds_long_prompt_via_stdin(monkeypatch):
    """cmd.exe (hosting the Windows .CMD shim) dies at ~8k chars of command
    line — a long prompt must never appear in argv."""
    captured = {}
    monkeypatch.setattr(
        claude_cli.subprocess, "run",
        lambda argv, **kw: captured.update(argv=argv, kw=kw) or _Proc())
    big = "x" * 20000
    claude_cli.run_headless(big)
    assert all(big not in str(part) for part in captured["argv"])
    assert captured["kw"]["input"] == big


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


# --- MN-9: the child-environment overlay -------------------------------------
#
# The orchestrator needs each concurrent agent to get its own MINDHIVE_SESSION:
# `events.append_event` writes to `<session_id>.jsonl` with an unlocked append, so
# two agents sharing one segment interleave into corrupt JSONL — and the ledger is
# the source of truth for the whole system.

def _capture_env(monkeypatch):
    """Run the spawn against a fake subprocess, returning the child's env."""
    import subprocess as sp
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw.get("env") or {})
        return _Proc()

    monkeypatch.setattr(sp, "run", fake_run)
    return seen


def test_no_env_argument_leaves_todays_behaviour_unchanged(monkeypatch):
    """The regression guard for invoker.py, reflect.py and autoresume.py, none of
    which pass env."""
    import os
    seen = _capture_env(monkeypatch)
    claude_cli.run_headless("p")
    assert seen.get("MINDHIVE_HEADLESS") == "1"
    assert seen.get("PATH") == os.environ.get("PATH")


def test_an_injected_variable_reaches_the_child(monkeypatch):
    seen = _capture_env(monkeypatch)
    claude_cli.run_headless("p", env={"MINDHIVE_SESSION": "mn9-t1"})
    assert seen.get("MINDHIVE_SESSION") == "mn9-t1"


def test_the_overlay_cannot_disable_the_recursion_guard(monkeypatch):
    """THE test of this unit. Every hooks/*.ps1 exits immediately when
    MINDHIVE_HEADLESS is set, so a child that could unset it would re-fire the
    hooks, which re-enter this spawn path — an unbounded spawn loop, not a wrong
    answer. The guard is applied AFTER the overlay for exactly this reason."""
    for attempt in ("0", "", "false", None):
        seen = _capture_env(monkeypatch)
        claude_cli.run_headless("p", env={"MINDHIVE_HEADLESS": attempt})
        assert seen.get("MINDHIVE_HEADLESS") == "1", (
            f"env={{'MINDHIVE_HEADLESS': {attempt!r}}} defeated the guard")


def test_the_overlay_is_merged_not_substituted(monkeypatch):
    """A bare replacement drops PATH and, on Windows, SYSTEMROOT — and a child with
    no SYSTEMROOT does not start at all."""
    import os
    seen = _capture_env(monkeypatch)
    claude_cli.run_headless("p", env={"MN9_NEW": "x"})
    assert seen.get("MN9_NEW") == "x"
    assert seen.get("PATH") == os.environ.get("PATH")


def test_an_overlay_value_of_none_removes_the_variable(monkeypatch):
    monkeypatch.setenv("MN9_GONE", "present")
    seen = _capture_env(monkeypatch)
    claude_cli.run_headless("p", env={"MN9_GONE": None})
    assert "MN9_GONE" not in seen


def test_a_non_mapping_overlay_is_ignored_rather_than_raising(monkeypatch):
    for junk in (["a"], "K=V", 42, object()):
        seen = _capture_env(monkeypatch)
        claude_cli.run_headless("p", env=junk)
        assert seen.get("MINDHIVE_HEADLESS") == "1"
