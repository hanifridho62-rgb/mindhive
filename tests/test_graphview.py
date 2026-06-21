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
