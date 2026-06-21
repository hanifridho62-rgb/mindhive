# tests/test_graphview.py
import json
from mindhive import graphview


def _graph():
    return {
        "nodes": [
            {"id": "session:sess1", "type": "session", "label": "sess1"},
            {"id": "ev:2026-06-20T09:02:00Z:decision", "type": "event",
             "label": "decision @ 2026-06-20T09:02:00Z"},
            {"id": "tag:arch", "type": "tag", "label": "arch"},
        ],
        "edges": [
            {"src": "ev:2026-06-20T09:02:00Z:decision", "dst": "session:sess1",
             "rel": "in_session"},
            {"src": "ev:2026-06-20T09:02:00Z:decision", "dst": "tag:arch",
             "rel": "tagged"},
        ],
    }


def test_render_html_contains_every_node_id():
    html = graphview.render_html(_graph())
    for node in _graph()["nodes"]:
        assert node["id"] in html


def test_render_html_is_offline_no_external_refs():
    html = graphview.render_html(_graph())
    assert "http://" not in html
    assert "https://" not in html


def test_render_html_embeds_graph_as_json():
    html = graphview.render_html(_graph())
    assert "in_session" in html
    assert "tag:arch" in html


def test_render_html_empty_graph_is_valid():
    html = graphview.render_html({"nodes": [], "edges": []})
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html


def test_render_html_escapes_script_closing_tag():
    html = graphview.render_html({
        "nodes": [{"id": "x", "type": "tag", "label": "</script><script>alert(1)"}],
        "edges": [],
    })
    assert "</script><script>" not in html


from mindhive import events, paths


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def _seed_events():
    return [
        events.Event(ts="2026-06-20T09:00:00Z", session_id="sess1",
                     type="session_start", payload={}, tags=[]),
        events.Event(ts="2026-06-20T09:02:00Z", session_id="sess1",
                     type="decision", payload={"text": "Use TDD"}, tags=["arch"]),
    ]


def test_export_writes_graph_html(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from mindhive.graph import build
    build(project=None, _evs=_seed_events())
    out = graphview.export(project=None)
    assert out.exists()
    assert out.name == "graph.html"
    html = out.read_text(encoding="utf-8")
    assert "session:sess1" in html


def test_export_builds_graph_if_missing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    d = paths.events_dir()
    d.mkdir(parents=True, exist_ok=True)
    for ev in _seed_events():
        events.append_event(ev.type, ev.payload, session_id=ev.session_id,
                            tags=ev.tags, ts=ev.ts)
    out = graphview.export(project=None)
    assert out.exists()
    assert (paths.project_dir() / "graph.json").exists()


def test_export_honors_out_path(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from mindhive.graph import build
    build(project=None, _evs=_seed_events())
    target = tmp_path / "custom.html"
    out = graphview.export(project=None, out=str(target))
    assert out == target
    assert target.exists()


import builtins
import pytest


def test_launch_app_without_pywebview_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")
    from mindhive import desktop

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "webview":
            raise ImportError("no webview")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match=r"pip install -e \.\[app\]"):
        desktop.launch_app(project=None)
