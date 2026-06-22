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
  .edge { stroke: #30363d; stroke-width: 1; fill: none; }
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
  #legend { position: absolute; top: 10px; left: 10px;
    background: rgba(22,27,34,0.85); border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 10px; font-size: 11px; max-width: 200px; }
  #legend .row { display: flex; align-items: center; margin: 3px 0; }
  #legend .dot { width: 10px; height: 10px; border-radius: 50%; margin-right: 6px;
    flex: 0 0 auto; }
  #fitbtn { position: absolute; top: 10px; right: 10px; background: #21262d;
    color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px;
    padding: 4px 12px; cursor: pointer; font: inherit; }
  #fitbtn:hover { background: #30363d; }
  #panel .chip { display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; margin: 0 4px 4px 0; background: #21262d; color: #c9d1d9; }
  #panel .body { white-space: pre-wrap; word-break: break-word; background: #0e1116;
    border: 1px solid #30363d; border-radius: 6px; padding: 10px; margin: 10px 0;
    max-height: 280px; overflow-y: auto; font-size: 12.5px; color: #c9d1d9; }
</style>
</head>
<body>
<div id="app">
  <div id="stage">
    <svg id="svg"></svg>
    <div id="legend"></div>
    <button id="fitbtn" title="Fit graph to window">Fit</button>
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
  // Color by event subtype (kind) so meaning is visible at a glance; structural
  // node types (session/file/tag) keep their own distinct hues.
  var KIND_COLORS = {
    decision: "#58a6ff", caveat: "#f0883e", experiment_result: "#2ea043",
    open_question: "#bc8cff", next_step: "#39c5cf", state_set: "#d2a8ff",
    file_touched: "#6e7681", command_run: "#6e7681", git_diff: "#6e7681",
    session_start: "#6e7681", session_end: "#6e7681", compaction: "#6e7681"
  };
  var TYPE_COLORS = { session: "#e3b341", file: "#768390", tag: "#db61a2",
                      event: "#58a6ff", plugin: "#3fb950", skill: "#a5d6ff",
                      topic: "#d29922", recommendation: "#f778ba" };
  var KIND_LABELS = {
    decision: "decision", caveat: "caveat", experiment_result: "experiment",
    open_question: "open question", next_step: "next step", state_set: "state",
    file_touched: "file touched", command_run: "command", git_diff: "git diff",
    session_start: "session start", session_end: "session end",
    compaction: "compaction", session: "session", file: "file", tag: "tag",
    plugin: "plugin", skill: "skill", topic: "topic",
    recommendation: "recommendation"
  };

  var data = JSON.parse(document.getElementById("graph-data").textContent);
  var svg = document.getElementById("svg");
  var panel = document.getElementById("panel");
  var W = svg.clientWidth || 800, H = svg.clientHeight || 600;

  // Real-time edge "buzz": a subtle perpendicular ripple on every real edge.
  // AMP = peak px offset, WAVES = ripples per line, SPEED = angular rate in rad/s
  // (the "speed 2" chosen on the design mockup maps to ~5.2 rad/s),
  // PHASE = per-edge desync, SAMPLES = polyline resolution of each path.
  var VIBE_AMP = 3.5, VIBE_WAVES = 5, VIBE_SPEED = 5.2, VIBE_PHASE = 0.9,
      VIBE_SAMPLES = 20;

  function nodeColor(n) {
    if (n.type === "event") return KIND_COLORS[n.kind] || TYPE_COLORS.event;
    return TYPE_COLORS[n.type] || "#888";
  }
  function nodeKey(n) { return n.type === "event" ? (n.kind || "event") : n.type; }
  function snippet(s, max) {
    s = String(s).replace(/\s+/g, " ").trim();
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  }
  function canvasLabel(n) {
    if (n.type === "event") {
      var base = KIND_LABELS[n.kind] || n.kind || "event";
      return n.text ? base + ": " + snippet(n.text, 22) : base;
    }
    return snippet(n.label, 26);
  }

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
    return { id: n.id, type: n.type, label: n.label, kind: n.kind, text: n.text,
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

  // Legend: one row per node kind/type actually present in this graph.
  (function buildLegend() {
    var legend = document.getElementById("legend");
    if (!legend) return;
    var seen = {}, rows = [];
    nodes.forEach(function (n) {
      var k = nodeKey(n);
      if (seen[k]) return;
      seen[k] = true;
      rows.push({ color: nodeColor(n), label: KIND_LABELS[k] || k });
    });
    if (!rows.length) { legend.style.display = "none"; return; }
    legend.innerHTML = rows.map(function (r) {
      return "<div class=row><span class=dot style=\"background:" + r.color +
        "\"></span>" + esc(r.label) + "</div>";
    }).join("");
  })();

  var edgeEls = edges.map(function (e) {
    var l = document.createElementNS(NS, "path");
    l.setAttribute("class", "edge");
    root.appendChild(l);
    return l;
  });
  var nodeEls = nodes.map(function (n) {
    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "node");
    var c = document.createElementNS(NS, "circle");
    c.setAttribute("r", n.type === "session" ? 9 : 6);
    c.setAttribute("fill", nodeColor(n));
    var t = document.createElementNS(NS, "text");
    t.setAttribute("x", 9); t.setAttribute("y", 4);
    t.textContent = canvasLabel(n);
    g.appendChild(c); g.appendChild(t);
    g.addEventListener("mousedown", function (ev) { startDrag(ev, n); });
    g.addEventListener("click", function (ev) { ev.stopPropagation(); select(n); });
    root.appendChild(g);
    return g;
  });

  var fitted = false, alpha = 1;
  function fitView() {
    if (!nodes.length) return;
    var minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    nodes.forEach(function (n) {
      minx = Math.min(minx, n.x); maxx = Math.max(maxx, n.x);
      miny = Math.min(miny, n.y); maxy = Math.max(maxy, n.y);
    });
    var pad = 80;
    var gw = Math.max(1, maxx - minx), gh = Math.max(1, maxy - miny);
    var k = Math.min((W - pad) / gw, (H - pad) / gh);
    k = Math.max(0.2, Math.min(2.5, k));
    view.k = k;
    view.x = W / 2 - k * (minx + maxx) / 2;
    view.y = H / 2 - k * (miny + maxy) / 2;
    applyView();
  }
  var fitbtn = document.getElementById("fitbtn");
  if (fitbtn) fitbtn.addEventListener("click", fitView);

  function tick(now) {
    now = now || 0;
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
      n.vx += (W / 2 - n.x) * 0.006; n.vy += (H / 2 - n.y) * 0.006;
      // Motion is scaled by alpha so the layout cools to a stop (see below).
      if (n !== dragging) { n.x += n.vx * alpha; n.y += n.vy * alpha; }
      n.vx *= 0.85; n.vy *= 0.85;
    });
    // Edge buzz: rebuild each edge as a perpendicular sine waveform. Driven by
    // the frame timestamp (not alpha), so edges keep vibrating after the layout
    // freezes. The sin(pi*s) envelope pins displacement to 0 at both endpoints.
    var t = now / 1000;
    edges.forEach(function (e, i) {
      var sx = e.s.x, sy = e.s.y, tx = e.t.x, ty = e.t.y;
      var dx = tx - sx, dy = ty - sy, len = Math.sqrt(dx * dx + dy * dy) || 1;
      var nx = -dy / len, ny = dx / len;       // unit perpendicular
      var phase = i * VIBE_PHASE;
      var d = "M" + sx + " " + sy;
      for (var k = 1; k <= VIBE_SAMPLES; k++) {
        var s = k / VIBE_SAMPLES;
        var env = Math.sin(s * Math.PI);
        var disp = VIBE_AMP * env *
          Math.sin(s * Math.PI * VIBE_WAVES + phase + t * VIBE_SPEED);
        var bx = sx + dx * s, by = sy + dy * s;
        d += " L" + (bx + nx * disp).toFixed(1) + " " + (by + ny * disp).toFixed(1);
      }
      edgeEls[i].setAttribute("d", d);
    });
    nodes.forEach(function (n, i) {
      nodeEls[i].setAttribute("transform", "translate(" + n.x + "," + n.y + ")");
    });
    // Cool the layout each frame; once it has settled (alpha hits 0) the node
    // positions are frozen, so a single fit-to-window holds instead of going
    // stale while the simulation keeps drifting (which clipped the graph).
    alpha = alpha > 0 ? Math.max(0, alpha - 0.008) : 0;
    if (!fitted && alpha === 0) { fitted = true; fitView(); }
    requestAnimationFrame(tick);
  }
  if (nodes.length) requestAnimationFrame(tick);

  var dragging = null, dragOff = null;
  function startDrag(ev, n) {
    ev.preventDefault(); ev.stopPropagation();
    dragging = n;
    alpha = Math.max(alpha, 0.3);  // gently reheat so neighbours respond
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

  function tsFromLabel(n) {
    // Event labels are "type @ timestamp"; pull the timestamp for display.
    var parts = String(n.label || "").split(" @ ");
    return parts.length > 1 ? parts[parts.length - 1] : "";
  }
  function select(n) {
    var conns = (adj[n.id] || []);
    var tags = conns.filter(function (c) {
      return byId[c.id] && byId[c.id].type === "tag";
    });
    var others = conns.filter(function (c) {
      return !(byId[c.id] && byId[c.id].type === "tag");
    });
    var color = nodeColor(n);
    var kindLabel = KIND_LABELS[nodeKey(n)] || nodeKey(n);
    var ts = n.type === "event" ? tsFromLabel(n) : "";

    // Title = a short first-line summary; the full (possibly multi-line) text
    // lives in a scrollable body block below so big payloads stay readable.
    var firstLine = n.text ? String(n.text).split("\n")[0] : "";
    var title = firstLine ? snippet(firstLine, 70) : (n.text ? snippet(n.text, 70) : n.label);
    var html = "<h2>" + esc(title) + "</h2>";
    html += "<span class=type style=\"background:" + color + "33;color:" + color +
      "\">" + esc(kindLabel) + "</span>";
    if (ts) html += "<div class=rel>" + esc(ts) + "</div>";
    if (n.text) html += "<div class=body>" + esc(n.text) + "</div>";
    if (tags.length) {
      html += "<h3>Tags</h3>" + tags.map(function (c) {
        return "<span class=chip>" + esc((byId[c.id] && byId[c.id].label) || c.id) +
          "</span>";
      }).join("");
    }
    html += "<h3>Connected (" + others.length + ")</h3>";
    html += others.length
      ? "<ul>" + others.map(function (c) {
          var node = byId[c.id];
          var label = node ? canvasLabel(node) : c.id;
          return "<li>" + esc(label) + " <span class=rel>(" + esc(c.rel) +
            ")</span></li>";
        }).join("") + "</ul>"
      : "<p class=rel>No other connections.</p>";
    panel.innerHTML = html;

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
      "Append some memory events, then reopen.</div>";
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

    Always rebuilds the graph from the current events so the exported view is
    never stale (build() also persists graph.json). Writes atomically
    (tmp + replace), matching graph.json's persistence.
    """
    from . import graph as graph_mod

    pdir = paths.project_dir(project)
    graph_data = graph_mod.build(project=project, semantic=semantic)

    html = render_html(graph_data)
    dest = Path(out) if out else (pdir / "graph.html")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(dest)
    return dest
