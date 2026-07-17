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


def test_render_html_has_color_legend():
    html = graphview.render_html(_graph())
    assert 'id="legend"' in html


def test_render_html_has_subtype_color_map():
    # The renderer bakes a per-kind color map so caveats/decisions look distinct.
    html = graphview.render_html(_graph())
    assert "caveat" in html
    assert "experiment_result" in html


def test_render_html_has_zoom_to_fit():
    html = graphview.render_html(_graph())
    assert "fitView" in html


def test_render_html_uses_node_kind_and_text():
    # On-canvas labels/colors must be driven by the enriched fields.
    html = graphview.render_html(_graph())
    assert ".kind" in html
    assert ".text" in html


def test_render_html_uses_seeded_meteor_layout():
    # Nodes are arranged into a deterministic, seeded "meteor" (head / filaments /
    # comet tail) computed once — not an iterative force layout.
    html = graphview.render_html(_graph())
    assert "function layoutMeteor()" in html
    assert "mulberry32" in html          # seeded RNG => reproducible per seed
    assert "META" in html and "tailDir" in html


def test_render_html_has_no_iterative_physics():
    # The O(n^2) repulsion / spring / cooling simulation is gone: positions are
    # the fixed meteor, so none of the old physics machinery should remain.
    html = graphview.render_html(_graph())
    assert "1200 / d2" not in html       # the old inverse-square repulsion
    assert "alpha" not in html           # no cooling variable anymore


def test_render_html_fits_to_window_once():
    # A single fit-to-window still happens (the meteor is static, so one fit holds
    # instead of going stale behind a drifting simulation).
    html = graphview.render_html(_graph())
    assert "!fitted" in html
    assert "fitView()" in html


def test_render_html_panel_has_prewrap_body():
    # Long multi-line payloads must render as a readable, wrapped body block,
    # not be crammed into the headline.
    html = graphview.render_html(_graph())
    assert "pre-wrap" in html
    assert "class=body" in html


def test_render_html_edges_are_vibrating_paths():
    # Edges render as <path> (so they can bend), not straight <line> elements,
    # and the open path must be stroked rather than filled.
    html = graphview.render_html(_graph())
    assert 'createElementNS(NS, "path")' in html
    assert 'createElementNS(NS, "line")' not in html
    assert "fill: none" in html


def test_render_html_edges_vibrate_in_realtime():
    # The buzz is driven by the frame timestamp, so the filaments keep shimmering
    # even though the meteor layout itself is static.
    html = graphview.render_html(_graph())
    assert "VIBE_AMP" in html
    assert "VIBE_SPEED" in html
    assert "VIBE_WAVES" in html
    assert "now / 1000" in html


def test_render_html_edges_reflect_real_connections():
    # Every vibrating line maps 1:1 to a real data.edges relationship: the
    # renderer only draws edges whose both endpoints exist — no synthetic lines.
    html = graphview.render_html(_graph())
    assert "byId[e.src] && byId[e.dst]" in html


from mindhive import events, paths


def _seed_events():
    return [
        events.Event(ts="2026-06-20T09:00:00Z", session_id="sess1",
                     type="session_start", payload={}, tags=[]),
        events.Event(ts="2026-06-20T09:02:00Z", session_id="sess1",
                     type="decision", payload={"text": "Use TDD"}, tags=["arch"]),
    ]


def _append_seed_events():
    """Append the seed events to disk so export()'s rebuild picks them up."""
    for ev in _seed_events():
        events.append_event(ev.type, ev.payload, session_id=ev.session_id,
                            tags=ev.tags, ts=ev.ts)


def test_export_writes_graph_html():
    _append_seed_events()
    out = graphview.export(project=None)
    assert out.exists()
    assert out.name == "graph.html"
    html = out.read_text(encoding="utf-8")
    assert "session:sess1" in html


def test_export_builds_graph_if_missing():
    d = paths.events_dir()
    d.mkdir(parents=True, exist_ok=True)
    for ev in _seed_events():
        events.append_event(ev.type, ev.payload, session_id=ev.session_id,
                            tags=ev.tags, ts=ev.ts)
    out = graphview.export(project=None)
    assert out.exists()
    assert (paths.project_dir() / "graph.json").exists()


def test_export_honors_out_path(tmp_path):
    from mindhive.graph import build
    build(project=None, _evs=_seed_events())
    target = tmp_path / "custom.html"
    out = graphview.export(project=None, out=str(target))
    assert out == target
    assert target.exists()


def test_export_reflects_newly_appended_events():
    """A second export must show events appended after the first export."""
    events.append_event("decision", {"text": "first decision"},
                        session_id="sess1", tags=["a"], ts="2026-06-20T09:00:00Z")
    graphview.export(project=None)  # first export (builds + persists graph.json)
    events.append_event("decision", {"text": "second decision"},
                        session_id="sess1", tags=["b"], ts="2026-06-20T09:05:00Z")
    out = graphview.export(project=None)  # must rebuild, not serve stale cache
    html = out.read_text(encoding="utf-8")
    assert "ev:2026-06-20T09:05:00Z:decision" in html
