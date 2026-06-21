# MIND HIVE Graph Desktop App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a free, offline desktop app (Windows `.exe`) that renders MIND HIVE's existing memory graph (`graph.json`) as an interactive, explorable view, plus a portable `graph.html` export.

**Architecture:** One pure renderer (`graphview.render_html`) turns `graph.json` into a single self-contained HTML string (inlined data + vanilla-JS SVG force layout, no network). Two delivery forms reuse it: `hive graph export` writes `graph.html`; `desktop.py` hosts the same HTML in a native pywebview window, frozen to a `.exe` with PyInstaller.

**Tech Stack:** Python 3.11+ stdlib (renderer/CLI, zero new core deps), vanilla JS/SVG (in-browser rendering), `pywebview` (native window, optional extra), `pyinstaller` (packaging, optional extra).

---

## Reference

- Spec: `docs/superpowers/specs/2026-06-21-mindhive-graph-desktop-app-design.md`
- Existing graph builder: `src/mindhive/graph.py` (`build()` returns `{"nodes": [...], "edges": [...]}`, persists `graph.json` via tmp+replace)
- Node shape: `{"id": str, "type": str, "label": str}` where `type` ∈ {event, session, file, tag}
- Edge shape: `{"src": str, "dst": str, "rel": str}` where `rel` ∈ {in_session, touched, tagged, next, shares_tag, semantic_near}
- Event node ids look like `ev:<ts>:<type>`; their human text lives in the originating event's `payload["text"]`, which is **not** in `graph.json`. The renderer therefore shows the node `label` (e.g. `decision @ 2026-06-20T09:02:00Z`) as the heading and uses connected nodes + tags for detail. (No payload text is added to the graph in this plan — out of scope.)
- Test convention (`tests/test_graph.py`): set `MINDHIVE_HOME` + `MINDHIVE_PROJECT` env via monkeypatch; craft `events.Event(...)` lists; pass `_evs=...` to `build()`.
- `selftest` runs pytest over `tests/`, so new test files are picked up automatically.

## File Structure

- Create: `src/mindhive/graphview.py` — pure `render_html(graph: dict) -> str` + `export(project=None, out=None, semantic=False) -> Path`.
- Create: `src/mindhive/desktop.py` — `launch_app(project=None)` opens a pywebview window with the rendered HTML.
- Create: `build/build_app.py` — PyInstaller build script producing `dist/MindHiveGraph.exe`.
- Create: `build/app_entry.py` — frozen-app entry point that calls `mindhive.desktop.launch_app()`.
- Create: `tests/test_graphview.py` — deterministic tests for `render_html` / `export`.
- Modify: `src/mindhive/__main__.py` — add `graph export` and `graph app` subcommands.
- Modify: `pyproject.toml` — add `app` and `build` optional-dependency extras.
- Modify: `README.md` — document the new commands and the build step.

---

## Task 1: Renderer core — `render_html` returns self-contained HTML

**Files:**
- Create: `src/mindhive/graphview.py`
- Test: `tests/test_graphview.py`

- [ ] **Step 1: Write the failing test**

```python
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
    # The data is inlined so the page never fetches anything.
    assert "in_session" in html
    assert "tag:arch" in html


def test_render_html_empty_graph_is_valid():
    html = graphview.render_html({"nodes": [], "edges": []})
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_graphview.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mindhive.graphview'` (or `AttributeError: render_html`).

- [ ] **Step 3: Write minimal implementation**

```python
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
    <div id="hint">drag nodes · scroll to zoom · drag background to pan · click a node</div>
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

  var NS = "http://www.w3.org/2000/svg";
  var root = document.createElementNS(NS, "g");
  svg.appendChild(root);

  // view transform (pan/zoom)
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

  // adjacency for the side panel
  var adj = {};
  data.edges.forEach(function (e) {
    (adj[e.src] = adj[e.src] || []).push({ id: e.dst, rel: e.rel });
    (adj[e.dst] = adj[e.dst] || []).push({ id: e.src, rel: e.rel });
  });

  // build SVG elements
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
    // simple force layout: repulsion + spring + centering
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

  // node drag
  var dragging = null, dragOff = null;
  function startDrag(ev, n) {
    ev.preventDefault(); ev.stopPropagation();
    dragging = n;
    var p = toWorld(ev);
    dragOff = { x: p.x - n.x, y: p.y - n.y };
  }
  // background pan
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
  // zoom
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

  // selection + side panel
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
    // highlight
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
        .replace("__GRAPH_JSON__", graph_json)
        .replace("__RENDERER_JS__", _RENDERER_JS)
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_graphview.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/graphview.py tests/test_graphview.py
git commit -m "feat(graphview): self-contained HTML renderer for the memory graph"
```

---

## Task 2: `export()` writes `graph.html` (atomic, builds graph if missing)

**Files:**
- Modify: `src/mindhive/graphview.py`
- Test: `tests/test_graphview.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_graphview.py
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
    # Seed events but never call build(); export must build graph.json itself.
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_graphview.py -k export -v`
Expected: FAIL with `AttributeError: module 'mindhive.graphview' has no attribute 'export'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/mindhive/graphview.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_graphview.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/graphview.py tests/test_graphview.py
git commit -m "feat(graphview): export() writes atomic standalone graph.html"
```

---

## Task 3: Wire `hive graph export` CLI subcommand

**Files:**
- Modify: `src/mindhive/__main__.py`
- Test: `tests/test_cli_new.py` (existing file for new CLI tests)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_cli_new.py
from mindhive.__main__ import main
from mindhive import events, paths


def _setup_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def test_graph_export_writes_html(tmp_path, monkeypatch, capsys):
    _setup_cli(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "Use TDD"},
                        session_id="sess1", tags=["arch"], ts="2026-06-20T09:02:00Z")
    rc = main(["graph", "export"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "graph.html" in out
    assert (paths.project_dir() / "graph.html").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_new.py -k graph_export -v`
Expected: FAIL — argparse exits with code 2 ("invalid choice: 'export'") or SystemExit, because the subcommand does not exist yet.

- [ ] **Step 3: Write minimal implementation**

In `src/mindhive/__main__.py`, inside the `graph` subparser block (after the `gr` / `related` parser, before `# --- sync ---`), add the export parser:

```python
    ge = gsub.add_parser("export", help="write standalone graph.html")
    ge.add_argument("--out", default=None)
    ge.add_argument("--semantic", action="store_true")
    ge.add_argument("--open", dest="open_after", action="store_true",
                    help="open the file in the default browser")
```

Then in the `elif args.cmd == "graph":` dispatch block, add an `export` branch alongside `build` / `related`:

```python
        elif args.graph_cmd == "export":
            from . import graphview
            dest = graphview.export(out=args.out, semantic=args.semantic)
            print(str(dest))
            if args.open_after:
                import webbrowser
                webbrowser.open(dest.as_uri())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_new.py -k graph_export -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite to confirm nothing broke**

Run: `python -m pytest -q`
Expected: PASS (all tests).

- [ ] **Step 6: Commit**

```bash
git add src/mindhive/__main__.py tests/test_cli_new.py
git commit -m "feat(cli): add 'hive graph export' for standalone graph.html"
```

---

## Task 4: Desktop window — `desktop.py` with `launch_app()`

**Files:**
- Create: `src/mindhive/desktop.py`
- Modify: `pyproject.toml` (add `app` extra)
- Test: `tests/test_graphview.py` (import-guard test only; GUI launch is manual)

- [ ] **Step 1: Write the failing test**

This test only verifies the missing-dependency guard message — it does NOT open a window (no display in CI).

```python
# append to tests/test_graphview.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_graphview.py -k launch_app -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mindhive.desktop'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mindhive/desktop.py
from __future__ import annotations
import json
from . import paths, graphview


def _require_webview():
    try:
        import webview  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "desktop app needs extras: pip install -e .[app]"
        ) from e
    return webview


def _load_graph(project=None) -> dict:
    pdir = paths.project_dir(project)
    gfile = pdir / "graph.json"
    if not gfile.exists():
        from . import graph as graph_mod
        return graph_mod.build(project=project)
    return json.loads(gfile.read_text(encoding="utf-8"))


def launch_app(project=None) -> None:
    """Open a native window rendering the current memory graph.

    Blocks until the window is closed. Requires the 'app' extra (pywebview).
    """
    webview = _require_webview()
    html = graphview.render_html(_load_graph(project))
    webview.create_window("MIND HIVE Graph", html=html, width=1100, height=750)
    webview.start()
```

Then add to `pyproject.toml` under `[project.optional-dependencies]` (alongside `vectors` and `dev`):

```toml
app = ["pywebview>=5"]
build = ["pyinstaller>=6"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_graphview.py -k launch_app -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/desktop.py pyproject.toml tests/test_graphview.py
git commit -m "feat(desktop): launch_app native window + app/build extras"
```

---

## Task 5: Wire `hive graph app` CLI subcommand

**Files:**
- Modify: `src/mindhive/__main__.py`
- Test: `tests/test_cli_new.py`

- [ ] **Step 1: Write the failing test**

The test monkeypatches `desktop.launch_app` so no real window opens; it only proves the CLI routes to it.

```python
# append to tests/test_cli_new.py
def test_graph_app_invokes_launch(tmp_path, monkeypatch):
    _setup_cli(tmp_path, monkeypatch)
    called = {}
    import mindhive.desktop as desktop_mod
    monkeypatch.setattr(desktop_mod, "launch_app",
                        lambda project=None: called.setdefault("hit", True))
    rc = main(["graph", "app"])
    assert rc == 0
    assert called.get("hit") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_new.py -k graph_app -v`
Expected: FAIL — argparse "invalid choice: 'app'".

- [ ] **Step 3: Write minimal implementation**

In `src/mindhive/__main__.py`, add the `app` parser inside the `graph` subparser group (after the `export` parser from Task 3):

```python
    gsub.add_parser("app", help="open the desktop graph window")
```

Then in the `elif args.cmd == "graph":` dispatch block, add the branch:

```python
        elif args.graph_cmd == "app":
            from . import desktop
            desktop.launch_app()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_new.py -k graph_app -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all tests).

- [ ] **Step 6: Commit**

```bash
git add src/mindhive/__main__.py tests/test_cli_new.py
git commit -m "feat(cli): add 'hive graph app' to open the desktop window"
```

---

## Task 6: Packaging — `build/build_app.py` + entry point

**Files:**
- Create: `build/app_entry.py`
- Create: `build/build_app.py`

No unit test (PyInstaller freezing requires a real toolchain/display; validated by the manual smoke test in Task 7).

- [ ] **Step 1: Write the frozen-app entry point**

```python
# build/app_entry.py
"""Entry point for the frozen MIND HIVE Graph desktop app."""
from mindhive.desktop import launch_app

if __name__ == "__main__":
    launch_app()
```

- [ ] **Step 2: Write the build script**

```python
# build/build_app.py
"""Freeze the MIND HIVE Graph desktop app into a single binary.

Usage:
    pip install -e .[app,build]
    python build/build_app.py

Produces dist/MindHiveGraph (.exe on Windows). PyInstaller builds for the OS it
runs on, so run this on the target OS.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "build" / "app_entry.py"
ICON = ROOT / "build" / "icon.ico"


def main() -> int:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--windowed",
        "--name", "MindHiveGraph",
        "--paths", str(ROOT / "src"),
        "--collect-all", "webview",
    ]
    if ICON.exists():
        args += ["--icon", str(ICON)]
    args.append(str(ENTRY))
    print("Running:", " ".join(args))
    return subprocess.call(args, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Verify the build script imports cleanly (no freeze yet)**

Run: `python -c "import ast; ast.parse(open('build/build_app.py').read()); ast.parse(open('build/app_entry.py').read()); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add build/app_entry.py build/build_app.py
git commit -m "build: PyInstaller script to freeze the desktop app"
```

Note: add an `icon.ico` at `build/icon.ico` when available; the build script works without it (falls back to the default icon).

---

## Task 7: Docs + manual smoke test

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the new commands in README.md**

Under the `## Commands` list in `README.md`, add these three lines after the existing `hive recall` line (keep the existing style):

```markdown
- `hive graph export [--out path] [--semantic] [--open]` — write a standalone, offline graph.html
- `hive graph app` — open the desktop graph window (needs `pip install -e .[app]`)
```

Then add a new section at the end of `README.md`:

```markdown
## Desktop graph app

`hive graph export` writes a single self-contained `graph.html` (data + renderer
inlined, no network, no keys) you can open in any browser on any PC.

`hive graph app` opens the same view in a native window (`pip install -e .[app]`).

To build a double-click binary for your machine:

```bash
pip install -e .[app,build]
python build/build_app.py        # -> dist/MindHiveGraph(.exe)
```

PyInstaller builds for the OS it runs on. Expected size ~30-50 MB.
```

- [ ] **Step 2: Commit the docs**

```bash
git add README.md
git commit -m "docs: document graph export + desktop app"
```

- [ ] **Step 3: Manual smoke test (run locally, not in CI)**

```bash
# 1. Generate a graph from real events (or use an existing project's graph.json)
hive graph export --open
#    -> opens graph.html; confirm nodes render, drag/zoom/pan work,
#       clicking a node fills the side panel with its connections.

# 2. Native window (after: pip install -e .[app])
hive graph app
#    -> confirm a window titled "MIND HIVE Graph" opens with the same view.

# 3. Freeze the binary (after: pip install -e .[app,build])
python build/build_app.py
dist/MindHiveGraph.exe
#    -> double-click dist/MindHiveGraph.exe; confirm the window opens
#       with no Python installed dependencies visible. Note the file size.
```

- [ ] **Step 4: Run the full suite one last time**

Run: `python -m pytest -q`
Expected: PASS (all tests).

---

## Self-Review

**Spec coverage:**
- Renderer (`render_html`, self-contained, offline) → Task 1. ✓
- `export()` atomic write + build-if-missing → Task 2. ✓
- `hive graph export [--out][--open][--semantic]` → Task 3. ✓
- `desktop.py` / `launch_app` pywebview window + `app` extra → Task 4. ✓
- `hive graph app` + missing-dep guard → Tasks 4 (guard) + 5 (CLI). ✓
- `build/build_app.py` PyInstaller `.exe` + entry + `build` extra → Tasks 4 (extra) + 6. ✓
- Tests: every-node-id, no external refs, empty-graph, `--out`, build-if-missing → Tasks 1-2. ✓
- Core CLI stays zero-dep (pywebview/pyinstaller only in extras) → Task 4 pyproject. ✓
- Docs (README) → Task 7. ✓
- Non-goals (no search/cluster/LLM/semantic-default; Windows-only build) honored — not implemented. ✓

**Placeholder scan:** No TBD/TODO; all code blocks complete; `icon.ico` is optional and the build script tolerates its absence (explicitly handled). ✓

**Type consistency:** `render_html(graph: dict) -> str`, `export(project, out, semantic) -> Path`, `launch_app(project=None) -> None` used consistently across renderer, CLI, desktop, and entry point. Graph dict keys `nodes`/`edges` and node keys `id`/`type`/`label` match `graph.py`. ✓
