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


def test_graph_export_writes_html(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "Use TDD"},
                        session_id="sess1", tags=["arch"], ts="2026-06-20T09:02:00Z")
    from mindhive import paths
    rc = cli_mod.main(["graph", "export"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "graph.html" in out
    assert (paths.project_dir() / "graph.html").exists()


def test_graph_export_open_invokes_browser(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "Use TDD"},
                        session_id="sess1", tags=["arch"], ts="2026-06-20T09:02:00Z")
    import webbrowser
    opened = {}
    monkeypatch.setattr(webbrowser, "open",
                        lambda uri: opened.setdefault("uri", uri))
    rc = cli_mod.main(["graph", "export", "--open"])
    assert rc == 0
    assert "graph.html" in opened.get("uri", "")


def test_cli_plugins_scan_stores_sidecar(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    from tests.test_plugins import make_cache
    from mindhive import plugins
    cache = make_cache(tmp_path)
    ret = cli_mod.main(["plugins", "scan", "--cache", str(cache)])
    assert ret == 0
    out = capsys.readouterr().out
    assert "free-plugin" in out
    assert "stored plugin catalog" in out
    # the catalog goes to the latest-only sidecar, NOT the append-only ledger
    assert plugins.latest_catalog() is not None
    assert not [e for e in events.read_all() if e.type == "plugin_catalog"]


def test_cli_plugins_recommend_reads_latest(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    from tests.test_plugins import make_cache
    cache = make_cache(tmp_path)
    cli_mod.main(["plugins", "scan", "--cache", str(cache)])
    capsys.readouterr()
    ret = cli_mod.main(["plugins", "recommend"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "free-plugin" in out


def test_cli_plugins_recommend_without_scan(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    ret = cli_mod.main(["plugins", "recommend"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "run: hive plugins scan" in out


def test_dashboard_subcommand_parses(monkeypatch):
    called = {}
    import mindhive.dashboard as dash
    monkeypatch.setattr(dash, "serve", lambda **kw: called.update(kw))
    rc = cli_mod.main(["dashboard", "--no-browser", "--port", "8123"])
    assert rc == 0
    assert called == {"port": 8123, "open_browser": False}
