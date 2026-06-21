# tests/test_cli_new.py — tests for briefing, graph, sync CLI subcommands
import json
from mindhive import __main__ as cli_mod, events


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def test_cli_briefing(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "cli test decision"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    ret = cli_mod.main(["briefing"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "PROJECT BRIEFING" in out
    assert "BRIEFING.md" in out


def test_cli_graph_build(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "graph build test"},
                        session_id="s", ts="2026-06-20T09:00:00Z",
                        tags=["arch"])
    ret = cli_mod.main(["graph", "build"])
    assert ret == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "nodes" in data
    assert "edges" in data


def test_cli_graph_related(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    events.append_event("file_touched", {"path": "src/myfile.py"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    # Build first
    cli_mod.main(["graph", "build"])
    capsys.readouterr()  # clear output
    ret = cli_mod.main(["graph", "related", "myfile"])
    assert ret == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)


def test_cli_sync(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    # Use tmp_path as fake home via monkeypatching Path.home
    from pathlib import Path
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # Patch Path.home in sync module
    import mindhive.sync as sync_mod
    original_home = Path.home
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    events.append_event("decision", {"text": "sync test"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    import os
    ret = cli_mod.main(["sync", "--folder", str(tmp_path / "project")])
    assert ret == 0
    out = capsys.readouterr().out
    assert "MEMORY.md" in out
