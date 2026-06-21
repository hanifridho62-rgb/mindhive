# src/mindhive/graphview.py
from __future__ import annotations
import json
from pathlib import Path
from . import paths

# Self-contained page: graph data is inlined as JSON; the renderer is a small
# vanilla-JS SVG force layout. No external (http/https) references, so the file
# works fully offline in any browser or webview.
_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MIND HIVE Graph</title>
<style>
  :root { color-scheme: dark; }
  html, body { margin: 0; height: 100%; background: #0e1116; color: #e6edf3;
    font: 14px/1.4 system-ui, sans-serif; overflow: hidden; }
  #app { display: flex; height: 100%; }
  #stage { flex: 1; position: relative; }
  svg { width: 100%; height: 100%; display: block; cursor: grab; }
  svg.panning { cursor: grabbing; }
  .edge { stroke: #30363d; stroke-width: 1; }
  .node circle { stroke: #0e1116; stroke-width: 1.5; cursor: pointer; }
  .node text { fill: #c9d1d9; font-size: 11px; pointer-events: none; }
  .node.dim circle, .edge.dim { opacity: 0.15; }
  #panel { width: 320px; background: #161b22; border-left: 1px solid #30363d;
    padding: 16px; overflow-y: auto; }
  #panel h2 { font-size: 15px; margin: 0 0 4px; word-break: break-word; }
  #panel .type { display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; margin-bottom: 12px; }
  #panel h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .05em;
    color: #8b949e; margin: 16px 0 6px; }
  #panel ul { margin: 0; padding-left: 16px; }
  #panel li { margin: 3px 0; word-break: break-word; }
  #panel .rel { color: #8b949e; font-size: 11px; }
  #empty { padding: 40px; color: #8b949e; text-align: center; }
  #hint { position: absolute; bottom: 8px; left: 12px; color: #8b949e;
    font-size: 12px; }
</style>
</head>
<body>
<div id="app">
  <div id="stage">
    <svg id="svg"></svg>
    <div id="hint">drag nodes &middot; scroll to zoom &middot; drag background to pan &middot; click a node</div>
  </div>
  <div id="panel"><div id="empty">Click a node to inspect it.</div></div>
</div>
<script id="graph-data" type="application/json">__GRAPH_JSON__</script>
<script>
__RENDERER_JS__
</script>
</body>
</html>
"""

_RENDERER_JS = r"""
(function () {
  var COLORS = { event: "#58a6ff", session: "#3fb950", file: "#d29922", tag: "#bc8cff" };
  var data = JSON.parse(document.getElementById("graph-data").textContent);
  var svg = document.getElementById("svg");
  var panel = document.getElementById("panel");
  var W = svg.clientWidth || 800, H = svg.clientHeight || 600;

  var NS = "http:" + "//www.w3.org/2000/svg";
  var root = document.createElementNS(NS, "g");
  svg.appendChild(root);

  var view = { x: 0, y: 0, k: 1 };
  function applyView() {
    root.setAttribute("transform",
      "translate(" + view.x + "," + view.y + ") scale(" + view.k + ")");
  }

  var nodes = data.nodes.map(function (n, i) {
    var a = (i / Math.max(1, data.nodes.length)) * 2 * Math.PI;
    return { id: n.id, type: n.type, label: n.label,
             x: W / 2 + Math.cos(a) * 180, y: H / 2 + Math.sin(a) * 180,
             vx: 0, vy: 0 };
  });
  var byId = {}; nodes.forEach(function (n) { byId[n.id] = n; });
  var edges = data.edges
    .filter(function (e) { return byId[e.src] && byId[e.dst]; })
    .map(function (e) { return { s: byId[e.src], t: byId[e.dst], rel: e.rel }; });

  var adj = {};
  data.edges.forEach(function (e) {
    (adj[e.src] = adj[e.src] || []).push({ id: e.dst, rel: e.rel });
    (adj[e.dst] = adj[e.dst] || []).push({ id: e.src, rel: e.rel });
  });

  var edgeEls = edges.map(function (e) {
    var l = document.createElementNS(NS, "line");
    l.setAttribute("class", "edge");
    root.appendChild(l);
    return l;
  });
  var nodeEls = nodes.map(function (n) {
    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "node");
    var c = document.createElementNS(NS, "circle");
    c.setAttribute("r", n.type === "session" ? 9 : 6);
    c.setAttribute("fill", COLORS[n.type] || "#888");
    var t = document.createElementNS(NS, "text");
    t.setAttribute("x", 9); t.setAttribute("y", 4);
    t.textContent = n.label.length > 28 ? n.label.slice(0, 27) + "…" : n.label;
    g.appendChild(c); g.appendChild(t);
    g.addEventListener("mousedown", function (ev) { startDrag(ev, n); });
    g.addEventListener("click", function (ev) { ev.stopPropagation(); select(n); });
    root.appendChild(g);
    return g;
  });

  function tick() {
    for (var i = 0; i < nodes.length; i++) {
      var a = nodes[i];
      for (var j = i + 1; j < nodes.length; j++) {
        var b = nodes[j];
        var dx = a.x - b.x, dy = a.y - b.y;
        var d2 = dx * dx + dy * dy || 0.01;
        var f = 1200 / d2;
        var d = Math.sqrt(d2);
        a.vx += (dx / d) * f; a.vy += (dy / d) * f;
        b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
      }
    }
    edges.forEach(function (e) {
      var dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
      var d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      var f = (d - 90) * 0.02;
      e.s.vx += (dx / d) * f; e.s.vy += (dy / d) * f;
      e.t.vx -= (dx / d) * f; e.t.vy -= (dy / d) * f;
    });
    nodes.forEach(function (n) {
      n.vx += (W / 2 - n.x) * 0.002; n.vy += (H / 2 - n.y) * 0.002;
      if (n !== dragging) { n.x += n.vx; n.y += n.vy; }
      n.vx *= 0.85; n.vy *= 0.85;
    });
    edges.forEach(function (e, i) {
      edgeEls[i].setAttribute("x1", e.s.x); edgeEls[i].setAttribute("y1", e.s.y);
      edgeEls[i].setAttribute("x2", e.t.x); edgeEls[i].setAttribute("y2", e.t.y);
    });
    nodes.forEach(function (n, i) {
      nodeEls[i].setAttribute("transform", "translate(" + n.x + "," + n.y + ")");
    });
    requestAnimationFrame(tick);
  }
  if (nodes.length) requestAnimationFrame(tick);

  var dragging = null, dragOff = null;
  function startDrag(ev, n) {
    ev.preventDefault(); ev.stopPropagation();
    dragging = n;
    var p = toWorld(ev);
    dragOff = { x: p.x - n.x, y: p.y - n.y };
  }
  var panning = null;
  svg.addEventListener("mousedown", function (ev) {
    panning = { x: ev.clientX - view.x, y: ev.clientY - view.y };
    svg.classList.add("panning");
  });
  window.addEventListener("mousemove", function (ev) {
    if (dragging) {
      var p = toWorld(ev);
      dragging.x = p.x - dragOff.x; dragging.y = p.y - dragOff.y;
      dragging.vx = 0; dragging.vy = 0;
    } else if (panning) {
      view.x = ev.clientX - panning.x; view.y = ev.clientY - panning.y;
      applyView();
    }
  });
  window.addEventListener("mouseup", function () {
    dragging = null; panning = null; svg.classList.remove("panning");
  });
  svg.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var factor = ev.deltaY < 0 ? 1.1 : 0.9;
    var mx = ev.clientX, my = ev.clientY;
    view.x = mx - (mx - view.x) * factor;
    view.y = my - (my - view.y) * factor;
    view.k *= factor;
    applyView();
  }, { passive: false });

  function toWorld(ev) {
    return { x: (ev.clientX - view.x) / view.k, y: (ev.clientY - view.y) / view.k };
  }

  function select(n) {
    var conns = (adj[n.id] || []);
    var connHtml = conns.length
      ? "<ul>" + conns.map(function (c) {
          var label = (byId[c.id] && byId[c.id].label) || c.id;
          return "<li>" + esc(label) + " <span class=rel>(" + esc(c.rel) + ")</span></li>";
        }).join("") + "</ul>"
      : "<p class=rel>No connections.</p>";
    panel.innerHTML =
      "<h2>" + esc(n.label) + "</h2>" +
      "<span class=type style=\"background:" + (COLORS[n.type] || "#888") + "33;color:" +
        (COLORS[n.type] || "#888") + "\">" + esc(n.type) + "</span>" +
      "<div class=rel style=\"word-break:break-all\">" + esc(n.id) + "</div>" +
      "<h3>Connected (" + conns.length + ")</h3>" + connHtml;
    nodeEls.forEach(function (el, i) {
      var keep = nodes[i] === n || conns.some(function (c) { return c.id === nodes[i].id; });
      el.classList.toggle("dim", !keep);
    });
    edgeEls.forEach(function (el, i) {
      var e = edges[i];
      el.classList.toggle("dim", e.s !== n && e.t !== n);
    });
  }
  svg.addEventListener("click", function () {
    panel.innerHTML = "<div id=empty>Click a node to inspect it.</div>";
    nodeEls.forEach(function (el) { el.classList.remove("dim"); });
    edgeEls.forEach(function (el) { el.classList.remove("dim"); });
  });

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  if (!nodes.length) {
    panel.innerHTML = "<div id=empty>This graph has no nodes yet.<br>" +
      "Append some events and run <code>hive graph build</code>.</div>";
  }
})();
"""


def render_html(graph: dict) -> str:
    """Return a single self-contained HTML document for the given graph dict.

    Pure function: no I/O, no network. The graph is inlined as JSON and rendered
    by an embedded vanilla-JS SVG force layout.
    """
    graph_json = json.dumps(
        {"nodes": graph.get("nodes", []), "edges": graph.get("edges", [])},
        ensure_ascii=False, separators=(",", ":"),
    )
    # Prevent a literal "</script>" in data from closing the inline script tag.
    graph_json = graph_json.replace("</", "<\\/")
    return (
        _TEMPLATE
        .replace("__RENDERER_JS__", _RENDERER_JS)   # constant first
        .replace("__GRAPH_JSON__", graph_json)       # user data second
    )


def export(project=None, out: str | None = None, semantic: bool = False) -> Path:
    """Render the project's graph to a standalone graph.html and return its path.

    Loads graph.json (builds it first if missing or if semantic is requested).
    Writes atomically (tmp + replace), matching graph.json's persistence.
    """
    from . import graph as graph_mod

    pdir = paths.project_dir(project)
    gfile = pdir / "graph.json"
    if semantic or not gfile.exists():
        graph_data = graph_mod.build(project=project, semantic=semantic)
    else:
        graph_data = json.loads(gfile.read_text(encoding="utf-8"))

    html = render_html(graph_data)
    dest = Path(out) if out else (pdir / "graph.html")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(dest)
    return dest
