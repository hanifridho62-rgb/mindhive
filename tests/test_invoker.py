# tests/test_invoker.py
"""Skill/plugin invocation channel — the headless-CLI mechanism that makes an
installed skill callable from the MindHive dashboard, recording each outcome so
green/red status and the failure→improvement loop have something to render."""
from mindhive import invoker, events


def _ok_runner(prompt):
    return {"exit": 0, "stdout": '{"is_error": false, "result": "did the thing"}'}


def _fail_runner(prompt):
    return {"exit": 1, "stdout": '{"is_error": true, "result": "boom"}'}


def test_invoke_success_returns_ok_and_records_result():
    res = invoker.invoke_skill("brainstorming", runner=_ok_runner, project="test")
    assert res["ok"] is True
    assert res["exit"] == 0
    results = [e for e in events.read_all("test") if e.type == "invoke_result"]
    assert results and results[-1].payload["name"] == "brainstorming"
    assert results[-1].payload["ok"] is True


def test_invoke_failure_records_tool_failed_with_skill_signature():
    res = invoker.invoke_skill("flaky-skill", runner=_fail_runner, project="test")
    assert res["ok"] is False
    fails = [e for e in events.read_all("test") if e.type == "tool_failed"]
    assert fails and fails[-1].payload["signature"] == "skill:flaky-skill"
    assert "flaky-skill" in fails[-1].payload["action"]


def test_invoke_is_error_envelope_counts_as_failure_even_on_exit_zero():
    runner = lambda p: {"exit": 0, "stdout": '{"is_error": true, "result": "auth 401"}'}
    res = invoker.invoke_skill("s", runner=runner, project="test")
    assert res["ok"] is False
    assert [e for e in events.read_all("test") if e.type == "tool_failed"]


def test_invoke_records_result_event_for_status_derivation():
    invoker.invoke_skill("a", runner=_ok_runner, project="test")
    invoker.invoke_skill("b", runner=_fail_runner, project="test")
    kinds = {e.type for e in events.read_all("test")}
    assert {"invoke_result", "tool_failed"} <= kinds


def test_prompt_names_the_skill_and_plugin():
    seen = {}
    def spy(prompt):
        seen["prompt"] = prompt
        return {"exit": 0, "stdout": '{"is_error": false, "result": "ok"}'}
    invoker.invoke_skill("ce-plan", plugin="compound-engineering",
                         runner=spy, project="test")
    assert "ce-plan" in seen["prompt"]
    assert "compound-engineering" in seen["prompt"]


class _Proc:
    returncode = 0
    stdout = '{"is_error": false, "result": "ok"}'
    stderr = ""


def test_default_runner_builds_headless_claude_argv(monkeypatch):
    import shutil
    captured = {}
    monkeypatch.setattr(shutil, "which", lambda name: None)   # not resolvable -> bare name
    monkeypatch.setattr(invoker.subprocess, "run",
                        lambda argv, **kw: captured.update(argv=argv) or _Proc())
    invoker.invoke_skill("brainstorming", project="test")
    argv = captured["argv"]
    assert argv[0] == "claude"
    assert "-p" in argv
    assert any("brainstorming" in a for a in argv)


def test_default_runner_resolves_executable_via_which(monkeypatch):
    """On Windows `subprocess.run(['claude', ...])` fails (WinError 2) because
    the npm shim is claude.CMD. Resolve the real path via shutil.which so the
    headless call works cross-platform."""
    import shutil
    captured = {}
    monkeypatch.setattr(shutil, "which",
                        lambda name: "C:/npm/claude.CMD" if name == "claude" else None)
    monkeypatch.setattr(invoker.subprocess, "run",
                        lambda argv, **kw: captured.update(argv=argv) or _Proc())
    invoker.invoke_skill("s", project="test")
    assert captured["argv"][0] == "C:/npm/claude.CMD"   # resolved, not bare "claude"
