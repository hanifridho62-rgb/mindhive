# Vibrating Edges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every real edge in the MIND HIVE graph view continuously buzz (subtle high-frequency ripple) in real time, so connections read as live links — without inventing any line that isn't a real relationship.

**Architecture:** The graph is rendered by a self-contained vanilla-JS SVG force layout embedded as a string in `src/mindhive/graphview.py`. Edges are currently static `<line>` elements that stop moving once the layout cools (`alpha → 0`). We convert edge elements to `<path>` elements and, every animation frame, rebuild each path as a sine-displaced waveform using the frame timestamp (decoupled from `alpha`, so edges buzz forever). The edge *set* is unchanged — it is still built only from `data.edges` with both endpoints required, so every buzzing line is a genuine connection.

**Tech Stack:** Python 3.12 (build/test), pytest (HTML-contract tests), vanilla JS + SVG (the renderer), pywebview/PyInstaller (desktop `.exe` for visual verification).

---

## File Structure

- **Modify:** `src/mindhive/graphview.py`
  - `_TEMPLATE` CSS: `.edge` gets `fill: none` (open paths must be stroked, not filled).
  - `_RENDERER_JS`: vibration constants; edge elements `<line>` → `<path>`; `tick()` takes the frame timestamp and rebuilds each edge path with a perpendicular sine displacement.
- **Modify (tests):** `tests/test_graphview.py`
  - New HTML-contract tests asserting: edges are vibrating `<path>` elements, the buzz is timestamp-driven (real-time), and edges still map 1:1 to real `data.edges` connections.

No other files change. Nodes, panel, legend, fit, pan/zoom, selection-dim, and the force model are untouched.

---

## Reference: exact constants and formula

Vibration constants (added once near the top of `_RENDERER_JS`):

```javascript
  // Real-time edge "buzz": a subtle perpendicular ripple on every real edge.
  // AMP = peak px offset, WAVES = ripples per line, SPEED = rad/s (user "speed 2"),
  // PHASE = per-edge desync, SAMPLES = polyline resolution of each path.
  var VIBE_AMP = 2, VIBE_WAVES = 5, VIBE_SPEED = 5.2, VIBE_PHASE = 0.9,
      VIBE_SAMPLES = 20;
```

Per-edge waveform (perpendicular displacement at fraction `s ∈ [0,1]` along the edge):

```
disp(s) = VIBE_AMP * sin(π·s) * sin(π·s·VIBE_WAVES + edgeIndex·VIBE_PHASE + t·VIBE_SPEED)
```

The `sin(π·s)` envelope forces `disp = 0` at both endpoints, so lines stay pinned to their nodes.

---

### Task 1: Edges render as real-time vibrating paths

**Files:**
- Modify: `src/mindhive/graphview.py` (CSS in `_TEMPLATE` ~line 24; `_RENDERER_JS` edge element creation ~lines 163-168, `tick` ~lines 205-244)
- Test: `tests/test_graphview.py`

- [ ] **Step 1: Write the failing tests**

Append these three tests to `tests/test_graphview.py` (they reuse the existing module-level `_graph()` helper):

```python
def test_render_html_edges_are_vibrating_paths():
    # Edges render as <path> (so they can bend), not straight <line> elements,
    # and the open path must be stroked rather than filled.
    html = graphview.render_html(_graph())
    assert 'createElementNS(NS, "path")' in html
    assert 'createElementNS(NS, "line")' not in html
    assert "fill: none" in html


def test_render_html_edges_vibrate_in_realtime():
    # The buzz is driven by the frame timestamp and is decoupled from the alpha
    # cooling, so edges keep moving after the layout settles.
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
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `./.build-venv/Scripts/python -m pytest tests/test_graphview.py -k "vibrating_paths or realtime or real_connections" -v`
Expected: `test_render_html_edges_are_vibrating_paths` FAILS (no `"path"` yet; `"line"` still present) and `test_render_html_edges_vibrate_in_realtime` FAILS (constants absent). `test_render_html_edges_reflect_real_connections` may already PASS — that is fine; it is a regression guard so the implementation must not remove the both-endpoints filter.

> Note: if `.build-venv` is absent, create it per the build memory: `py -3.12 -m venv .build-venv` then `./.build-venv/Scripts/python -m pip install -e ".[app,build]"`. Plain `python -m pytest` also works if your environment already has the package installed.

- [ ] **Step 3: Add `fill: none` to the edge CSS**

In `src/mindhive/graphview.py`, in `_TEMPLATE`, change the `.edge` rule:

Old:
```css
  .edge { stroke: #30363d; stroke-width: 1; }
```
New:
```css
  .edge { stroke: #30363d; stroke-width: 1; fill: none; }
```

- [ ] **Step 4: Add the vibration constants**

In `_RENDERER_JS`, immediately after this existing line:
```javascript
  var W = svg.clientWidth || 800, H = svg.clientHeight || 600;
```
insert:
```javascript

  // Real-time edge "buzz": a subtle perpendicular ripple on every real edge.
  // AMP = peak px offset, WAVES = ripples per line, SPEED = rad/s (user "speed 2"),
  // PHASE = per-edge desync, SAMPLES = polyline resolution of each path.
  var VIBE_AMP = 2, VIBE_WAVES = 5, VIBE_SPEED = 5.2, VIBE_PHASE = 0.9,
      VIBE_SAMPLES = 20;
```

- [ ] **Step 5: Convert edge elements from `<line>` to `<path>`**

In `_RENDERER_JS`, change the edge element creation:

Old:
```javascript
  var edgeEls = edges.map(function (e) {
    var l = document.createElementNS(NS, "line");
    l.setAttribute("class", "edge");
    root.appendChild(l);
    return l;
  });
```
New:
```javascript
  var edgeEls = edges.map(function (e) {
    var l = document.createElementNS(NS, "path");
    l.setAttribute("class", "edge");
    root.appendChild(l);
    return l;
  });
```

- [ ] **Step 6: Make `tick` timestamp-aware**

In `_RENDERER_JS`, change the `tick` signature:

Old:
```javascript
  function tick() {
```
New:
```javascript
  function tick(now) {
    now = now || 0;
```

(`requestAnimationFrame(tick)` already passes a `DOMHighResTimeStamp`; the two existing `requestAnimationFrame(tick)` calls stay unchanged.)

- [ ] **Step 7: Rebuild each edge as a vibrating path every frame**

In `_RENDERER_JS`, inside `tick`, replace the edge-position update block:

Old:
```javascript
    edges.forEach(function (e, i) {
      edgeEls[i].setAttribute("x1", e.s.x); edgeEls[i].setAttribute("y1", e.s.y);
      edgeEls[i].setAttribute("x2", e.t.x); edgeEls[i].setAttribute("y2", e.t.y);
    });
```
New:
```javascript
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
```

- [ ] **Step 8: Run the new tests to verify they pass**

Run: `./.build-venv/Scripts/python -m pytest tests/test_graphview.py -k "vibrating_paths or realtime or real_connections" -v`
Expected: all three PASS.

- [ ] **Step 9: Run the full graphview suite (regression)**

Run: `./.build-venv/Scripts/python -m pytest tests/test_graphview.py -v`
Expected: all tests PASS — in particular `test_render_html_layout_cools_and_fits_on_settle` (alpha cooling untouched), `test_render_html_is_offline_no_external_refs` (no new external refs), and `test_render_html_embeds_graph_as_json`.

- [ ] **Step 10: Commit**

```bash
git add src/mindhive/graphview.py tests/test_graphview.py
git commit -m "feat(graph): real-time buzzing edges that reflect real connections

Convert edge <line> elements to <path> and rebuild each as a timestamp-driven
perpendicular sine ripple every frame. Decoupled from the alpha cooling so edges
keep vibrating after the layout settles. Edge set is unchanged (built only from
data.edges with both endpoints required), so every vibrating line is a real link.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Visual verification in the desktop app

No code changes — confirm the buzz actually looks right in WebView2 (the renderer is opaque JS that unit tests can't exercise for motion).

**Files:** none (build + manual observation).

- [ ] **Step 1: Make sure no stale exe is holding the binary lock**

Run: `taskkill //F //IM MindHiveGraph.exe`
Expected: either "SUCCESS" or "not found" — both are fine. (Per the build memory: a lingering exe silently blocks the rebuild from overwriting `dist/MindHiveGraph.exe`.)

- [ ] **Step 2: Build the exe with Python 3.12**

Run: `./.build-venv/Scripts/python build/build_app.py`
Expected: `dist/MindHiveGraph.exe` (~14 MB) is written without error.

- [ ] **Step 3: Launch and observe**

Run: `./dist/MindHiveGraph.exe` (or launch via the app's normal entry on a project that has events).
Expected, by eye:
- Every line connecting two nodes ripples with a small, fast buzz (~2px).
- The buzz keeps going after nodes settle and stop drifting (it does not freeze with the layout).
- Lines stay attached to their node centers (no detached/floating ends).
- Dragging a node still moves it and its edges follow; the buzz rides on top.
- Clicking a node still dims unrelated nodes/edges; dimmed edges still buzz but faded.
- No line appears between nodes that have no relationship.

- [ ] **Step 4: Close the smoke-test window**

Run: `taskkill //F //IM MindHiveGraph.exe`
Expected: "SUCCESS". (Git-bash `kill` won't fully close the pywebview GUI processes.)

- [ ] **Step 5 (optional): tune and re-verify**

If the buzz is too strong/weak or too fast/slow, adjust `VIBE_AMP` / `VIBE_SPEED` in `_RENDERER_JS`, then repeat the full-suite test (Task 1 Step 9) and rebuild (Task 2 Steps 1-4). Commit any tuning change with a `fix(graph): tune edge buzz amplitude/speed` message.

---

## Self-Review

**Spec coverage:**
- "Energetic buzz, amplitude 2px, speed 2, all edges, always-on" → Task 1 constants (`VIBE_AMP=2`, `VIBE_SPEED=5.2`, `VIBE_WAVES=5`) + waveform in Step 7, applied to every edge.
- "Real time, decoupled from alpha cooling" → Task 1 Steps 6-7 (`tick(now)`, `t = now/1000`, displacement independent of `alpha`); guarded by `test_render_html_edges_vibrate_in_realtime` and the untouched `test_render_html_layout_cools_and_fits_on_settle`.
- "`<line>` → `<path>`, `.edge { fill: none }`, endpoints pinned" → Steps 3, 5, 7 (`sin(π·s)` envelope).
- "Vibration reflects real connections, no phantom lines" → `test_render_html_edges_reflect_real_connections` guards the both-endpoints filter; edge set is never modified.
- "Selection/dim unchanged, color unchanged" → no edits to the `select`/dim code or `.edge` stroke color; verified in Task 2 Step 3.
- "Testing at HTML-contract level + manual exe verification" → Task 1 tests + Task 2.

**Placeholder scan:** No TBD/TODO; every code step shows complete old/new code; commands have expected output.

**Type/name consistency:** Constant names (`VIBE_AMP`, `VIBE_WAVES`, `VIBE_SPEED`, `VIBE_PHASE`, `VIBE_SAMPLES`) are identical across the reference section, Steps 4 and 7, and the tests. `tick(now)` / `now / 1000` consistent between Steps 6, 7 and the realtime test. `edgeEls` is the same array the original code already maps over.
