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
    # Asserted key-by-key rather than as whole-dict equality: the contract is
    # that these two keys carry the exit code and the raw envelope, not that
    # the mapping has exactly two keys. `usage` and `session_id` were added
    # alongside them for the smart-zone gate without changing this contract.
    assert out["exit"] == 0
    assert out["stdout"] == '{"is_error": false, "result": "hi"}'
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
    out = claude_cli.run_headless("x")
    assert out["exit"] == 127 and out["stdout"] == ""


def test_run_headless_maps_timeout_to_124(monkeypatch):
    import subprocess
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(claude_cli.subprocess, "run", boom)
    out = claude_cli.run_headless("x")
    assert out["exit"] == 124 and out["stdout"] == ""


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


# --- the smart-zone gate needs two things this spawn used to throw away -------
#
# It discarded the envelope's `usage` block (so no caller could see how much
# context a run was carrying) and had no way to resume (so there was no session
# to rotate FROM). Both are additive: every assertion above still holds.

def _capture_argv(monkeypatch, stdout=None):
    captured = {}

    class _P:
        returncode = 0

    _P.stdout = stdout if stdout is not None else '{"is_error": false, "result": "hi"}'
    _P.stderr = ""
    monkeypatch.setattr(
        claude_cli.subprocess, "run",
        lambda argv, **kw: captured.update(argv=argv, kw=kw) or _P())
    return captured


def test_no_session_id_produces_no_resume_flag(monkeypatch):
    """Regression guard for every existing caller — invoker, reflect and
    autoresume all spawn without a session and must keep spawning fresh."""
    captured = _capture_argv(monkeypatch)
    claude_cli.run_headless("p")
    assert "--resume" not in captured["argv"]
    assert "--continue" not in captured["argv"]


def test_session_id_produces_an_explicit_resume(monkeypatch):
    captured = _capture_argv(monkeypatch)
    claude_cli.run_headless("p", session_id="sess-42")
    argv = captured["argv"]
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sess-42"
    # NEVER --continue: it can hijack the most-recent session in the directory,
    # which in a fleet of concurrent worktree agents means one task silently
    # resuming another task's conversation.
    assert "--continue" not in argv


def test_blank_session_id_is_treated_as_absent(monkeypatch):
    for blank in (None, "", "   "):
        captured = _capture_argv(monkeypatch)
        claude_cli.run_headless("p", session_id=blank)
        assert "--resume" not in captured["argv"], f"session_id={blank!r}"


def test_usage_and_session_id_are_returned_from_the_envelope(monkeypatch):
    blob = ('{"is_error": false, "result": "ok", "session_id": "sess-9", '
            '"usage": {"input_tokens": 8000, "cache_creation_input_tokens": 200000, '
            '"cache_read_input_tokens": 0, "output_tokens": 12}}')
    _capture_argv(monkeypatch, stdout=blob)
    out = claude_cli.run_headless("p")
    assert out["session_id"] == "sess-9"
    assert out["usage"]["cache_creation_input_tokens"] == 200000
    assert out["exit"] == 0 and out["stdout"] == blob


def test_error_envelope_yields_no_usage_rather_than_zeros(monkeypatch):
    """An is_error envelope means the CLI ran and the MODEL never worked. Zeros
    presented as fact would read as 'this session is carrying no context',
    which would suppress a rotation that may well be due."""
    _capture_argv(monkeypatch, stdout='{"is_error": true, "result": "429: limit"}')
    out = claude_cli.run_headless("p")
    assert out["usage"] is None
    assert out["session_id"] is None


def test_unreadable_envelope_yields_no_usage_and_does_not_raise(monkeypatch):
    _capture_argv(monkeypatch, stdout="not json at all")
    out = claude_cli.run_headless("p")
    assert out["usage"] is None
    assert out["session_id"] is None
    assert out["exit"] == 0


def test_spawn_failures_still_carry_the_new_keys(monkeypatch):
    """127/124 already had a documented shape; the added keys must be present
    there too, or callers need a branch for 'which failure was it'."""
    import subprocess as sp

    def not_found(*a, **k):
        raise FileNotFoundError("no claude")

    monkeypatch.setattr(claude_cli.subprocess, "run", not_found)
    out = claude_cli.run_headless("x")
    assert out["exit"] == 127 and out["stdout"] == ""
    assert out["usage"] is None and out["session_id"] is None

    def timed_out(*a, **k):
        raise sp.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(claude_cli.subprocess, "run", timed_out)
    out = claude_cli.run_headless("x")
    assert out["exit"] == 124 and out["usage"] is None
