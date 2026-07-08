# tests/test_cli_skills.py — `hive skills {list,invoke,status}` CLI surface.
# This is the seam the /mindhive and /circle skills call to drive ANY installed
# skill through MindHive and have the outcome recorded (green/red + learning).
import json
from mindhive import __main__ as cli_mod, events


def test_cli_skills_invoke_success(capsys, monkeypatch):
    from mindhive import invoker
    monkeypatch.setattr(invoker, "invoke_skill",
        lambda name, plugin=None, project=None: {"ok": True, "exit": 0, "excerpt": "did it"})
    ret = cli_mod.main(["skills", "invoke", "brainstorming"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "brainstorming" in out and "ok" in out.lower()


def test_cli_skills_invoke_failure_returns_nonzero(capsys, monkeypatch):
    from mindhive import invoker
    monkeypatch.setattr(invoker, "invoke_skill",
        lambda name, plugin=None, project=None: {"ok": False, "exit": 1, "excerpt": "boom"})
    ret = cli_mod.main(["skills", "invoke", "flaky", "--plugin", "some-plugin"])
    assert ret == 1                       # nonzero exit lets callers gate on it


def test_cli_skills_invoke_passes_plugin(capsys, monkeypatch):
    from mindhive import invoker
    seen = {}
    monkeypatch.setattr(invoker, "invoke_skill",
        lambda name, plugin=None, project=None: seen.update(name=name, plugin=plugin)
        or {"ok": True, "exit": 0, "excerpt": ""})
    cli_mod.main(["skills", "invoke", "ce-plan", "--plugin", "compound-engineering"])
    assert seen == {"name": "ce-plan", "plugin": "compound-engineering"}


def test_cli_skills_invoke_passes_project(capsys, monkeypatch):
    from mindhive import invoker
    seen = {}
    monkeypatch.setattr(invoker, "invoke_skill",
        lambda name, plugin=None, project=None: seen.update(name=name, project=project)
        or {"ok": True, "exit": 0, "excerpt": ""})
    cli_mod.main(["skills", "invoke", "ce-plan", "--project", "other-ns"])
    assert seen == {"name": "ce-plan", "project": "other-ns"}


def test_cli_skills_status_prints_map(capsys, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "call_status", lambda project=None: {"a": "ok", "b": "fail"})
    ret = cli_mod.main(["skills", "status"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "a" in out and "ok" in out and "b" in out and "fail" in out


def test_cli_skills_list_as_json(capsys, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "live_inventory",
        lambda: {"changed": True, "signature": "x",
                 "inventory": {"plugins": [{"name": "alpha", "skills": []}],
                               "local_skills": [], "counts": {"skills_total": 0}}})
    ret = cli_mod.main(["skills", "list", "--json"])
    assert ret == 0
    data = json.loads(capsys.readouterr().out)
    assert data["inventory"]["plugins"][0]["name"] == "alpha"


# --------------------------------------------------------------------------
# skills record / stats — the agent-assessed write-through + telemetry (U5)
# --------------------------------------------------------------------------

def test_cli_skills_record_ok_appends_agent_assessed_invoke_result():
    ret = cli_mod.main(["skills", "record", "ce-plan", "--ok",
                        "--note", "went fine", "--plugin", "compound-engineering"])
    assert ret == 0
    ev = [e for e in events.read_all() if e.type == "invoke_result"][0]
    assert ev.payload["name"] == "ce-plan"
    assert ev.payload["ok"] is True
    assert ev.payload["plugin"] == "compound-engineering"
    assert ev.payload["provenance"] == "agent-assessed"
    assert "agent-assessed" in ev.tags


def test_cli_skills_record_fail_appends_tool_failed_with_skill_signature():
    ret = cli_mod.main(["skills", "record", "ce-plan", "--fail",
                        "--note", "picked the wrong plan"])
    assert ret == 0
    ev = [e for e in events.read_all() if e.type == "tool_failed"][0]
    assert ev.payload["signature"] == "skill:ce-plan"
    assert ev.payload["text"] == "picked the wrong plan"
    assert ev.payload["provenance"] == "agent-assessed"
    assert "agent-assessed" in ev.tags


def test_cli_skills_record_requires_an_outcome():
    import pytest
    with pytest.raises(SystemExit):
        cli_mod.main(["skills", "record", "ce-plan"])


def test_cli_skills_stats_json_keyed_by_skill(capsys):
    events.append_event("invoke_result", {"name": "a", "ok": True, "text": ""},
                        session_id="s", ts="2026-07-01T10:00:00Z", project="test")
    ret = cli_mod.main(["skills", "stats", "--json"])
    assert ret == 0
    data = json.loads(capsys.readouterr().out)
    assert data["a"]["calls"] == 1
    assert data["a"]["completion_rate"] == 1.0


def test_cli_skills_stats_unknown_project_is_empty_not_error(capsys):
    ret = cli_mod.main(["skills", "stats", "--json", "--project", "ghost-ns"])
    assert ret == 0
    assert json.loads(capsys.readouterr().out) == {}


# --------------------------------------------------------------------------
# skills recommend — deterministic ranking CLI (U6)
# --------------------------------------------------------------------------

_FAKE_INVENTORY = {"changed": True, "signature": "x", "inventory": {
    "plugins": [{"name": "p", "skills": [
        {"name": "tester", "trigger": "Use when tests fail in the suite."},
        {"name": "deployer", "trigger": "Use when deploy pipelines break."},
    ]}],
    "local_skills": [],
    "counts": {"plugins": 1, "plugin_skills": 2, "local_skills": 0,
               "skills_total": 2}}}


def test_cli_skills_recommend_json_has_coverage_and_provenance(capsys, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "live_inventory", lambda: _FAKE_INVENTORY)
    ret = cli_mod.main(["skills", "recommend", "--json"])
    assert ret == 0
    data = json.loads(capsys.readouterr().out)
    assert data["coverage"] == {"measured": 0, "total": 2}
    assert all(e["provenance"] in ("measured", "estimated")
               for e in data["recommendations"])


def test_cli_skills_recommend_brief_is_fast_and_three_lines_max(capsys, monkeypatch):
    import time as _time
    from mindhive import plugins
    monkeypatch.setattr(plugins, "live_inventory", lambda: _FAKE_INVENTORY)
    # a small but real store: a few telemetry events to read
    for i in range(5):
        events.append_event("invoke_result", {"name": "tester", "ok": True, "text": ""},
                            session_id="s", ts=f"2026-07-01T1{i}:00:00Z", project="test")
    t0 = _time.monotonic()
    ret = cli_mod.main(["skills", "recommend", "--brief"])
    elapsed = _time.monotonic() - t0
    assert ret == 0
    assert elapsed < 2.0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert 1 <= len(lines) <= 3
    assert all("(measured)" in ln or "(estimated)" in ln for ln in lines)


def test_cli_skills_recommend_brief_honors_top(capsys, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "live_inventory", lambda: _FAKE_INVENTORY)
    ret = cli_mod.main(["skills", "recommend", "--brief", "--top", "2"])
    assert ret == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 2                 # --top overrides the default 3-cap


def test_cli_skills_recommend_brief_surfaces_intended_use(capsys, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "live_inventory", lambda: _FAKE_INVENTORY)
    ret = cli_mod.main(["skills", "recommend", "--brief"])
    assert ret == 0
    out = capsys.readouterr().out
    # each brief line still fits ONE line and carries the skill's intended use
    assert "intended use: Use when tests fail in the suite." in out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert 1 <= len(lines) <= 3


def test_cli_skills_recommend_full_surfaces_intended_use(capsys, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "live_inventory", lambda: _FAKE_INVENTORY)
    ret = cli_mod.main(["skills", "recommend"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "intended use: Use when tests fail in the suite." in out


_BLANK_TRIGGER_INVENTORY = {"changed": True, "signature": "x", "inventory": {
    "plugins": [{"name": "p", "skills": [
        {"name": "mystery", "trigger": "   "},   # whitespace-only description
    ]}],
    "local_skills": [],
    "counts": {"plugins": 1, "plugin_skills": 1, "local_skills": 0,
               "skills_total": 1}}}


def test_cli_skills_recommend_full_omits_empty_intended_use(capsys, monkeypatch):
    # a whitespace-only description must not leave a dangling 'intended use:'
    # line — full mode shares brief_line's empty-handling via _truncate
    from mindhive import plugins
    monkeypatch.setattr(plugins, "live_inventory", lambda: _BLANK_TRIGGER_INVENTORY)
    ret = cli_mod.main(["skills", "recommend"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "mystery" in out
    assert "intended use:" not in out
