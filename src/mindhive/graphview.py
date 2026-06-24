# src/mindhive/graphview.py
from __future__ import annotations
import json
from pathlib import Path
from . import paths

# Self-contained page: graph data is inlined as JSON; the renderer is a small
# vanilla-JS SVG engine that arranges the nodes into a seeded "meteor" — a dense
# glowing head of recent, well-connected memories, a radial spray of filaments,
# and a comet tail of older nodes streaming down-left. No external (http/https)
# references, so the file works fully offline in any browser or webview.
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
  .edge { stroke: #3a4759; stroke-width: .7; fill: none; opacity: .5; }
  .node circle { stroke: #0e1116; stroke-width: .8; cursor: pointer; }
  .node text { fill: #c9d1d9; font-size: 11px; pointer-events: none;
    opacity: 0; transition: opacity .12s; }
  svg.show-labels .node text { opacity: .9; }
  .node:hover text, .node.kin text { opacity: 1; }
  .node.dim circle, .edge.dim { opacity: 0.12; }
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
  #artbtn { position: absolute; top: 46px; right: 10px; background: #21262d;
    color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px;
    padding: 4px 12px; cursor: pointer; font: inherit; }
  #artbtn:hover { background: #30363d; }
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
    <button id="artbtn" title="Toggle pinwheel view">Pinwheel</button>
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
  var VIBE_AMP = 2.5, VIBE_WAVES = 5, VIBE_SPEED = 5.2, VIBE_PHASE = 0.9,
      VIBE_SAMPLES = 18;

  // ---- Meteor layout ("Sidereal Decay") -----------------------------------
  // A deterministic, seeded arrangement (no physics): the freshest / most-
  // connected memories pack into a glowing spherical HEAD (upper-right), older
  // and leaf nodes spray outward as fine radial FILAMENTS, and the whole cloud
  // streams DOWN-LEFT into a tapering comet TAIL. Same seed => same meteor.
  var META = {
    seed: 7,
    headR: 150,          // radius of the dense head, in virtual units
    tailLen: 1000,       // how far the comet body streams down-tail
    hairLen: 300,        // length of the radial filament spray
    tailDir: [-0.70711, 0.70711],  // down-left in screen space (y points down)
    tailSpread: 1.35,    // angular half-width (rad) of the hairy tail
    pTailHair: 0.6,      // share of filaments that lean into the tail
    wRecency: 0.6, wDegree: 0.4    // how "core" a node is: time + connectivity
  };
  function hashStr(s) {
    var h = 2166136261 >>> 0;
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i); h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }
  function mulberry32(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      var t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  function parseTime(n) {
    // Event labels look like "type @ 2026-06-23T...": pull the ISO timestamp.
    var s = String(n.label || ""), i = s.lastIndexOf(" @ ");
    var v = i >= 0 ? Date.parse(s.slice(i + 3)) : NaN;
    return isNaN(v) ? null : v;
  }

  // Force-layout stability constants. Large memory graphs contain mega-hub tags
  // (e.g. "harvest"/"auto" tag nearly every event), so one node can be wired to
  // ~all others. With explicit-Euler integration that concentration of spring
  // pull, combined with the 1/d^2 repulsion spiking when two hub-siblings briefly
  // coincide, sends velocities to infinity and the coordinates explode (1e90+),
  // after which fitView zooms into garbage and the whole graph renders as a
  // single dot. REPULSE_MINDIST floors the repulsion denominator, SPRING_CAP
  // bounds the per-edge pull, and MAX_VEL clamps per-axis speed — together they
  // keep the simulation bounded no matter how dense the hub.
  var REPULSE_MINDIST2 = 12 * 12, SPRING_CAP = 8, MAX_VEL = 30, COOL_RATE = 0.004;

  // Clustered ("grouped clumps") layout. Spring strength per edge relation:
  // structural edges (a node and its session/file/next) pull their clump tight;
  // tag edges pull NOTHING, because the mega-hub tags wire to ~every node and
  // any pull would drag all clumps back into one blob. Nodes are also held to a
  // per-group anchor (see below) so each session settles into its own clump.
  var SPRING_K = { in_session: 0.03, touched: 0.02, next: 0.04,
                   tagged: 0, shares_tag: 0 };
  var SPRING_REST = 40, ANCHOR_GRAVITY = 0.06, REPULSE = 900;

  // The per-frame edge "buzz" rebuilds every edge as a 20-point polyline each
  // frame, forever. On a big graph (thousands of edges) that animation never
  // stops and steals the main thread from zoom/pan, making them feel laggy.
  // So buzz only on small graphs; large graphs draw static straight edges and
  // the render loop HALTS once the layout settles (see kick()/tick) — after
  // which zoom and pan are just a CSS transform on the root group, zero JS.
  var BUZZ_MAX_EDGES = 600;

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
  // Two layers under the shared pan/zoom transform: the force graph and the
  // pinwheel art view. Only one is visible at a time (toggled by #artbtn).
  var graphLayer = document.createElementNS(NS, "g");
  var artLayer = document.createElementNS(NS, "g");
  artLayer.style.display = "none";
  root.appendChild(graphLayer); root.appendChild(artLayer);

  // Soft halo behind the head (origin in virtual space) so the core reads as a
  // glowing meteor. Appended before edges/nodes, so it always sits behind them.
  (function addGlow() {
    var defs = document.createElementNS(NS, "defs");
    var grad = document.createElementNS(NS, "radialGradient");
    grad.setAttribute("id", "meteor-glow");
    [["0%", "#e6edff", "0.55"], ["38%", "#9db6ff", "0.16"],
     ["100%", "#9db6ff", "0"]].forEach(function (st) {
      var s = document.createElementNS(NS, "stop");
      s.setAttribute("offset", st[0]);
      s.setAttribute("stop-color", st[1]);
      s.setAttribute("stop-opacity", st[2]);
      grad.appendChild(s);
    });
    defs.appendChild(grad); root.appendChild(defs);
    var halo = document.createElementNS(NS, "circle");
    halo.setAttribute("cx", "0"); halo.setAttribute("cy", "0");
    halo.setAttribute("r", "340"); halo.setAttribute("fill", "url(#meteor-glow)");
    root.appendChild(halo);
  })();

  var view = { x: 0, y: 0, k: 1 };
  function applyView() {
    root.setAttribute("transform",
      "translate(" + view.x + "," + view.y + ") scale(" + view.k + ")");
    // Labels would be unreadable mush across the whole meteor; reveal them only
    // when the user zooms in (hover and selection still reveal them any time).
    if (view.k >= 0.9) svg.classList.add("show-labels");
    else svg.classList.remove("show-labels");
  }

  // --- Assign each node to a clump ----------------------------------------
  // event -> its session; file -> the session of a file it touched; session ->
  // itself; tags (and anything left over) -> their own group. Group anchors are
  // spread on a ring so the clumps land in separate regions of the canvas.
  var nodeType = {};
  data.nodes.forEach(function (n) { nodeType[n.id] = n.type; });
  var group = {};
  data.edges.forEach(function (e) {
    if (e.rel !== "in_session") return;
    var ev = nodeType[e.src] === "event" ? e.src : e.dst;
    group[ev] = ev === e.src ? e.dst : e.src;
  });
  data.nodes.forEach(function (n) { if (n.type === "session") group[n.id] = n.id; });
  data.edges.forEach(function (e) {
    if (e.rel !== "touched") return;
    var fl = nodeType[e.src] === "file" ? e.src : e.dst;
    var ot = fl === e.src ? e.dst : e.src;
    if (group[ot] != null) group[fl] = group[ot];
    else if (nodeType[ot] === "session") group[fl] = ot;
  });
  data.nodes.forEach(function (n) {
    if (group[n.id] == null) group[n.id] = n.type === "tag" ? "__tags__" : "__misc__";
  });
  var groupIds = [], seenG = {};
  data.nodes.forEach(function (n) {
    var g = group[n.id];
    if (!seenG[g]) { seenG[g] = true; groupIds.push(g); }
  });
  var G = Math.max(1, groupIds.length), groupIdx = {};
  groupIds.forEach(function (g, i) { groupIdx[g] = i; });
  var RGROUP = 140 + G * 16;
  var anchors = groupIds.map(function (g, i) {
    var a = (i / G) * 2 * Math.PI;
    return { x: W / 2 + Math.cos(a) * RGROUP, y: H / 2 + Math.sin(a) * RGROUP };
  });

  var nodes = data.nodes.map(function (n, i) {
    var an = anchors[groupIdx[group[n.id]]];
    var jx = ((i * 71) % 60) - 30, jy = ((i * 37) % 60) - 30;  // break symmetry
    return { id: n.id, type: n.type, label: n.label, kind: n.kind, text: n.text,
             ax: an.x, ay: an.y, x: an.x + jx, y: an.y + jy, vx: 0, vy: 0 };
  });
  var byId = {}; nodes.forEach(function (n) { byId[n.id] = n; });
  var edges = data.edges
    .filter(function (e) { return byId[e.src] && byId[e.dst]; })
    .map(function (e) {
      var k = SPRING_K[e.rel]; if (k == null) k = 0.01;
      return { s: byId[e.src], t: byId[e.dst], rel: e.rel, k: k };
    });

  var adj = {};
  data.edges.forEach(function (e) {
    (adj[e.src] = adj[e.src] || []).push({ id: e.dst, rel: e.rel });
    (adj[e.dst] = adj[e.dst] || []).push({ id: e.src, rel: e.rel });
  });

  // Compute coreness (recency + degree) and place every node into the meteor.
  function layoutMeteor() {
    var D = META.tailDir, P = [-D[1], D[0]], tailAng = Math.atan2(D[1], D[0]);
    var tmin = Infinity, tmax = -Infinity, degMax = 1;
    nodes.forEach(function (n) {
      n.deg = (adj[n.id] || []).length; if (n.deg > degMax) degMax = n.deg;
      n.time = parseTime(n);
      if (n.time != null) {
        if (n.time < tmin) tmin = n.time;
        if (n.time > tmax) tmax = n.time;
      }
    });
    var haveTime = tmax >= tmin, span = (tmax - tmin) || 1;
    // Below headT the node belongs in the round head; above it, the comet body.
    var headT = 2 * META.headR / (META.tailLen + META.headR);
    nodes.forEach(function (n) {
      var recN = (haveTime && n.time != null) ? (n.time - tmin) / span : 0.35;
      n.core = META.wRecency * recN + META.wDegree * (n.deg / degMax);  // 1 = head
      // Per-node seeded RNG: stable id hash mixed with the layout seed.
      var rnd = mulberry32(hashStr(String(n.id)) ^ (Math.imul(META.seed, 2654435761) >>> 0));
      var t = 1 - n.core, x, y;
      if (n.deg <= 1 && rnd() < 0.85) {
        // Radial filament — the meteor's hairy spray, biased into the tail.
        var ang = rnd() < META.pTailHair
          ? tailAng + (rnd() + rnd() - 1) * META.tailSpread
          : rnd() * 2 * Math.PI;
        var L = META.headR * 0.55 + (0.35 + 0.65 * t) * META.hairLen * Math.sqrt(rnd());
        x = Math.cos(ang) * L + D[0] * t * META.tailLen * 0.12;
        y = Math.sin(ang) * L + D[1] * t * META.tailLen * 0.12;
      } else if (t < headT) {
        // Dense round head — the glowing core of fresh, well-connected memories.
        var rr = META.headR * Math.sqrt(rnd()), aa = rnd() * 2 * Math.PI;
        x = Math.cos(aa) * rr; y = Math.sin(aa) * rr;
      } else {
        // Comet body — older memories tapering down the tail (ρ ∝ (1-frac)^0.75).
        var frac = (t - headT) / (1 - headT);
        var ax = META.headR + frac * META.tailLen;
        var rho = META.headR * Math.pow(1 - frac, 0.75);
        var off = rho * (rnd() + rnd() - 1);   // triangular: denser toward the spine
        x = D[0] * ax + P[0] * off; y = D[1] * ax + P[1] * off;
      }
      n.x = x; n.y = y; n.vx = 0; n.vy = 0;
    });
  }
  layoutMeteor();

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
    graphLayer.appendChild(l);
    return l;
  });
  var nodeEls = nodes.map(function (n, i) {
    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "node");
    var c = document.createElementNS(NS, "circle");
    // Core nodes are larger and brighter (the incandescent head); tail nodes
    // shrink and fade — the comet's glow falling off behind it.
    var baseR = n.type === "session" ? 7 : 4.5;
    c.setAttribute("r", (baseR * (0.45 + 0.9 * n.core)).toFixed(2));
    c.setAttribute("fill", nodeColor(n));
    c.setAttribute("fill-opacity", (0.3 + 0.7 * n.core).toFixed(2));
    var t = document.createElementNS(NS, "text");
    t.setAttribute("x", 7); t.setAttribute("y", 3);
    t.textContent = canvasLabel(n);
    g.appendChild(c); g.appendChild(t);
    g.setAttribute("transform", "translate(" + n.x + "," + n.y + ")");
    n._i = i;
    g.addEventListener("mousedown", function (ev) { startDrag(ev, n); });
    g.addEventListener("click", function (ev) { ev.stopPropagation(); select(n); });
    graphLayer.appendChild(g);
    return g;
  });

  var fitted = false, running = false;
  var BUZZ = edges.length <= BUZZ_MAX_EDGES;   // animate edges only when cheap
  function fitView() {
    if (!nodes.length || artMode) return;
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

  // --- Pinwheel art view ---------------------------------------------------
  // Each session becomes a "petal": a spiral of nested squares that scale and
  // rotate toward the centre, depth scaled by how many events the session holds.
  // Petals are clickable — clicking opens that session in the side panel. It's a
  // second layer under the same pan/zoom, toggled by #artbtn.
  var artMode = false, artBuilt = false, savedView = null;
  function buildArt() {
    var groupSize = {};
    data.nodes.forEach(function (n) { var g = group[n.id]; groupSize[g] = (groupSize[g] || 0) + 1; });
    var petals = groupIds
      .filter(function (g) { return nodeType[g] === "session"; })
      .map(function (g) { return { id: g, size: groupSize[g] }; })
      .sort(function (a, b) { return b.size - a.size; });
    if (!petals.length) return;
    var P = petals.length, maxSize = petals[0].size;
    var minDim = Math.min(W, H), CX = W / 2, CY = H / 2;
    var Rc = minDim * 0.30, h = minDim * 0.165, sk = 0.5, f = 0.84,
        dTheta = -20 * Math.PI / 180;
    var base = [[-h, -h], [h, -h], [h, h], [-h, h]].map(function (c) {
      return [Rc + (c[0] * Math.cos(sk) - c[1] * Math.sin(sk)),
              c[0] * Math.sin(sk) + c[1] * Math.cos(sk)];
    });
    var R = Rc + h * 1.7;
    var paper = document.createElementNS(NS, "rect");
    paper.setAttribute("x", CX - R - 24); paper.setAttribute("y", CY - R - 24);
    paper.setAttribute("width", 2 * (R + 24)); paper.setAttribute("height", 2 * (R + 24));
    paper.setAttribute("rx", 16); paper.setAttribute("fill", "#eef2fb");
    artLayer.appendChild(paper);
    petals.forEach(function (pt, i) {
      var A = i * 2 * Math.PI / P;
      var L = Math.max(5, Math.min(18, Math.round(pt.size / maxSize * 18)));
      var pg = document.createElementNS(NS, "g");
      pg.setAttribute("cursor", "pointer");
      var fillsAdded = false;
      for (var k = 0; k < L; k++) {
        var s = Math.pow(f, k), ang = A + k * dTheta,
            ca = Math.cos(ang), sa = Math.sin(ang);
        var pts = base.map(function (v) {
          var x = s * (ca * v[0] - sa * v[1]), y = s * (sa * v[0] + ca * v[1]);
          return (CX + x).toFixed(1) + "," + (CY + y).toFixed(1);
        }).join(" ");
        if (!fillsAdded) {
          var poly = document.createElementNS(NS, "polygon");
          poly.setAttribute("points", pts);
          poly.setAttribute("fill", "#e7d391"); poly.setAttribute("fill-opacity", "0.55");
          poly.setAttribute("stroke", "none");
          pg.appendChild(poly); fillsAdded = true;
        }
        var line = document.createElementNS(NS, "polygon");
        line.setAttribute("points", pts); line.setAttribute("fill", "none");
        line.setAttribute("stroke", "#2c3c66"); line.setAttribute("stroke-width", "0.8");
        line.setAttribute("stroke-opacity", "0.85");
        pg.appendChild(line);
      }
      pg.addEventListener("click", function (ev) {
        ev.stopPropagation(); if (byId[pt.id]) select(byId[pt.id]);
      });
      artLayer.appendChild(pg);
    });
    artBuilt = true;
  }
  function setArt(on) {
    artMode = on;
    if (on) {
      if (!artBuilt) buildArt();
      savedView = { x: view.x, y: view.y, k: view.k };
      view.x = 0; view.y = 0; view.k = 1; applyView();
      graphLayer.style.display = "none"; artLayer.style.display = "";
      if (artbtn) artbtn.textContent = "Graph";
      panel.innerHTML = "<div id=empty>Click a petal to inspect its session.</div>";
    } else {
      if (savedView) { view.x = savedView.x; view.y = savedView.y; view.k = savedView.k; applyView(); }
      artLayer.style.display = "none"; graphLayer.style.display = "";
      if (artbtn) artbtn.textContent = "Pinwheel";
    }
  }
  var artbtn = document.getElementById("artbtn");
  if (artbtn) artbtn.addEventListener("click", function () { setArt(!artMode); });

  // No physics: node positions ARE the fixed meteor (computed once in
  // layoutMeteor). Each frame we only keep the edge "buzz" shimmering along the
  // filaments — the incandescent-hair effect — and fit-to-window once. Dragging
  // a node updates its own transform directly (see mousemove); its incident
  // edges follow here. When the buzz is off (large graphs) the loop halts once
  // fitted, so zoom/pan then cost nothing until a drag kicks it (see startDrag).
  function tick(now) {
    now = now || 0;
    if (BUZZ) {
      var t = now / 1000;
      edges.forEach(function (e, i) {
        var sx = e.s.x, sy = e.s.y, tx = e.t.x, ty = e.t.y;
        var dx = tx - sx, dy = ty - sy, len = Math.sqrt(dx * dx + dy * dy) || 1;
        var nx = -dy / len, ny = dx / len;       // unit perpendicular
        var phase = i * VIBE_PHASE;
        var d = "M" + sx + " " + sy;
        for (var k = 1; k <= VIBE_SAMPLES; k++) {
          var s = k / VIBE_SAMPLES;
          var env = Math.sin(s * Math.PI);       // pins displacement to 0 at ends
          var disp = VIBE_AMP * env *
            Math.sin(s * Math.PI * VIBE_WAVES + phase + t * VIBE_SPEED);
          var bx = sx + dx * s, by = sy + dy * s;
          d += " L" + (bx + nx * disp).toFixed(1) + " " + (by + ny * disp).toFixed(1);
        }
        edgeEls[i].setAttribute("d", d);
      });
    } else if (dragging || !fitted) {
      // No buzz: draw straight edges, only while a drag moves them or before the
      // first fit. Otherwise the loop halts and edges stay put (positions fixed).
      edges.forEach(function (e, i) {
        edgeEls[i].setAttribute("d", "M" + e.s.x.toFixed(1) + " " + e.s.y.toFixed(1) +
          "L" + e.t.x.toFixed(1) + " " + e.t.y.toFixed(1));
      });
    }
    if (!fitted) { fitted = true; fitView(); }
    // Keep animating only while there's work: buzz is on, or a drag is moving a
    // node. Otherwise halt — zoom/pan then cost nothing until a drag kicks us.
    if (BUZZ || dragging) requestAnimationFrame(tick);
    else running = false;
  }
  function kick() { if (!running) { running = true; requestAnimationFrame(tick); } }
  if (nodes.length) kick();

  var dragging = null, dragOff = null;
  function startDrag(ev, n) {
    ev.preventDefault(); ev.stopPropagation();
    dragging = n;
    kick();   // restart the render loop if it had halted, so edges follow the drag
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
      nodeEls[dragging._i].setAttribute("transform",
        "translate(" + dragging.x + "," + dragging.y + ")");
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
      el.classList.toggle("kin", keep);   // reveal labels for the focused cluster
    });
    edgeEls.forEach(function (el, i) {
      var e = edges[i];
      el.classList.toggle("dim", e.s !== n && e.t !== n);
    });
  }
  svg.addEventListener("click", function () {
    panel.innerHTML = "<div id=empty>Click a node to inspect it.</div>";
    nodeEls.forEach(function (el) { el.classList.remove("dim"); el.classList.remove("kin"); });
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
