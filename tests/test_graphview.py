# tests/test_graphview.py
import json
import re
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


def test_a_node_answers_the_cursor_and_stops_under_reduced_motion():
    """The dashboard's own hover motion is executed in the browser tier; this
    page's is not, and deliberately: `/api/graph` renders every node of the real
    store into one document — 21 MB against this ledger — so driving it under
    Playwright would cost more than the rule is worth. Source shape is what is
    affordable here, so it is what is claimed: the pairing is asserted, not the
    cascade.

    Both halves matter. The animation is what the operator asked for; the fill
    is what survives when they have asked the operating system for less motion,
    and a node that answered the cursor with nothing at all in that mode would
    be a regression dressed as accessibility. The guard needs `!important` for
    the reason the cat sheet's does: a media query adds no specificity, and the
    rule it has to beat is the same `:hover` rule."""
    css = graphview.render_html(_graph())
    assert "animation:gv-pop" in css, "a graph node has no hover motion at all"
    assert "@keyframes gv-pop" in css, "the hover rule names keyframes that do not ship"
    assert "transform:scale(" in css, (
        "gv-pop animates something other than transform — the earlier "
        "stroke-width version measured +5.3ms task and +0.7ms layout against a "
        "no-animation control, where transform measured as no cost at all")
    assert "transform-box: fill-box" in css, (
        "without fill-box an SVG child scales about the viewBox origin, so a "
        "hovered node does not grow in place — it flies across the canvas")
    assert "fill: #566070" in css, (
        "the hover fill is gone, so a reduced-motion viewer gets no hover "
        "feedback from a node at all")
    guard = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "animation:none !important" in guard, (
        "gv-pop keeps running under prefers-reduced-motion, and this page "
        "inherits no guard from the dashboard — it is served on its own")


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


def _graph_blob(html):
    """The graph's JSON payload, found by MATCHING the tag rather than by
    splitting on an exact literal. Splitting on the literal raises IndexError
    inside the test body, which reads as a failure of whatever was under test;
    the `assert` below names the helper as the thing that went stale instead.
    The pattern tolerates attribute order, extra attributes, either quote style
    and whitespace inside the tag -- it is anchored on the id, not on the exact
    spelling of the opening tag."""
    m = re.search(r"""<script[^>]*\bid=["']graph-data["'][^>]*>(.*?)</script>""",
                  html, re.S)
    assert m, "no graph-data block in the rendered page"
    return m.group(1)


def test_a_semantic_failure_reaches_the_page_that_asked_for_it():
    """`/api/graph?semantic=1` renders whatever `build` returned. The error
    rode in the dict and was dropped here, so a view that asked for semantic
    links and got none said nothing about why."""
    msg = "semantic query failed on the 'ledger' index: <b>hnsw</b>"
    html = graphview.render_html(dict(_graph(), semantic_error=msg))
    assert json.loads(_graph_blob(html))["semantic_error"] == msg
    assert "<b>hnsw" not in html, "the error reached the page unescaped"
    plain = graphview.render_html(_graph())
    assert "semantic_error" not in json.loads(_graph_blob(plain))


def _banner_block():
    """The renderer's `if (data.semantic_error) {...}`, by brace matching.
    Its CSS and its text are in every page, error or not, so only the block
    itself says whether a banner is drawn."""
    js = graphview._RENDERER_JS
    at = js.index("if (data.semantic_error) {")
    depth = 0
    for j in range(js.index("{", at), len(js)):
        depth += {"{": 1, "}": -1}.get(js[j], 0)
        if depth == 0:
            return js[at:j + 1]
    raise AssertionError("the banner block has unbalanced braces")


def test_the_banner_is_drawn_into_the_stage():
    block = _banner_block()
    assert 'semerr.id = "semerr"' in block
    assert "Semantic links unavailable" in block
    assert 'document.getElementById("stage").appendChild(semerr)' in block


def test_the_banner_is_written_as_text_never_as_markup():
    """The message carries ledger-adjacent text (paths, exception strings);
    the renderer puts it in with textContent, so no string in it is ever
    parsed as HTML."""
    block = _banner_block()
    assert "textContent" in block and "innerHTML" not in block
    assert 'setAttribute("role", "alert")' in block


def test_render_html_escapes_a_comment_opener():
    """`</script>` is the obvious way out of a script element; `<!--<script>` is
    the one that hid, and the exact trigger matters. A bare `<!--` opens
    script-data-ESCAPED state, where `</script>` still closes normally -- so a
    fixture carrying only `<!--` proves nothing. It is `<script` seen WHILE in
    that state that opens script-data-DOUBLE-escaped state, where `</script>`
    stops closing: the renderer after the blob is swallowed and the Graph view
    goes blank with no error.

    Both halves are browser-verified, and the fixture below is the SPLIT form on
    purpose -- `<!--` in one memo and `<script>` in a later one -- because that
    is the reachable shape: two ordinary memos about HTML, neither odd on its
    own. Served over http, a page with this pair renders an EMPTY body; the same
    page with a bare `<!--` reports `renderer=RENDERER RAN`."""
    html = graphview.render_html({
        "nodes": [{"id": "a", "type": "event", "label": "e", "text": "look <!-- here"},
                  {"id": "b", "type": "event", "label": "e", "text": "and <script> back"}],
        "edges": [],
    })
    assert "<!--" not in _graph_blob(html)
    assert html.count("</script>") == 2        # the two real closers, no more
    assert "<script" not in _graph_blob(html)  # the half that actually arms it
    assert [n["text"] for n in json.loads(_graph_blob(html))["nodes"]] == [
        "look <!-- here", "and <script> back"]  # unchanged once parsed


def test_render_html_escapes_the_javascript_line_terminators():
    """U+2028 and U+2029 are line terminators in JavaScript source but ordinary
    characters in JSON, and the graph blob is dumped with `ensure_ascii=False`.
    They are inert in a `type="application/json"` data block read back with
    `JSON.parse` -- but that is a property of the template, not of the data, and
    ledger memo prose reaches this blob. Before the reader fix of 2026-09-10 a
    row carrying one could not get here at all; `read_all` tore it and dropped
    it. Now it can."""
    html = graphview.render_html({
        "nodes": [{"id": "x", "type": "event", "label": "e",
                   "text": "before\u2028after\u2029end"}],
        "edges": [],
    })
    assert "\u2028" not in html
    assert "\u2029" not in html
    assert "\\u2028" in html and "\\u2029" in html
    # still valid JSON, and still the same string once parsed
    assert json.loads(_graph_blob(html))["nodes"][0]["text"] == (
        "before\u2028after\u2029end")


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
