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
