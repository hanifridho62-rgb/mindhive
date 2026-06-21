# Vibrating edges — design

**Date:** 2026-06-22
**Component:** `src/mindhive/graphview.py` (embedded SVG renderer)
**Status:** Approved (pending spec review)

## Goal

Make every edge in the MIND HIVE graph view continuously vibrate with a subtle
"energetic buzz" in real time, so the graph feels alive and connections read as
active links rather than static lines.

## Confirmed parameters

| Parameter | Value |
|-----------|-------|
| Style | Energetic buzz (high-frequency perpendicular ripple) |
| Trigger | All edges, always on |
| Amplitude | ~2 px |
| Speed | "2" → `SPEED ≈ 5.2 rad/s` |
| Lifetime | Real time, continuous — runs the whole time the graph is open |

## Core requirement: vibration reflects real connections

The vibration is **not decorative**. Every vibrating line must map 1:1 to a real
relationship in the graph data:

- The renderer only draws lines from `data.edges`, where each edge is an
  `{src, dst, rel}` relationship built from real memory events.
- Edges are already filtered to those whose **both** endpoints exist
  (`byId[e.src] && byId[e.dst]`); only these are rendered and therefore only
  these vibrate.
- No phantom, synthetic, or purely-visual lines are ever added. If two nodes
  have no relationship, there is no line between them and nothing vibrates.
- A vibrating line therefore always means: "these two nodes are genuinely
  connected by relationship `rel`."

This is preserved, not newly built — the change below must not introduce any
edge that isn't in `data.edges`.

## Behavior

- Every real edge buzzes continuously with a small, fast perpendicular ripple.
- Each edge uses a distinct phase offset so the graph shimmers organically
  instead of pulsing in lockstep.
- The buzz rides on top of wherever each edge's endpoints currently are:
  node-drag, pan, zoom, and click-to-dim selection all keep working unchanged.
- Lines stay visually attached to their nodes at all times (displacement is
  pinned to zero at both endpoints).

## Rendering change

In `_RENDERER_JS` (and the small CSS block in `_TEMPLATE`):

1. **`<line>` → `<path>`.** A straight `<line>` cannot bend, so edge elements
   become `<path>` elements. Add `.edge { fill: none; }` to the stylesheet so
   the open path is stroked, not filled.

2. **Per-frame waveform.** Each frame, rebuild each edge path's `d` from ~20
   sample points along the source→target direction. Each sample is displaced
   perpendicular to the edge by:

   ```
   disp(s) = AMP * sin(π·s) * sin(π·s·WAVES + phaseₑ + t·SPEED)
   ```

   where `s ∈ [0, 1]` runs along the edge. The `sin(π·s)` envelope forces
   `disp = 0` at `s = 0` and `s = 1`, keeping endpoints pinned to the nodes.

3. **Constants:**
   - `AMP = 2` (px)
   - `WAVES = 5` (ripples along each line)
   - `SPEED = 5.2` (rad/s)
   - `phaseₑ = edgeIndex * 0.9` (desync between edges)

## Real-time / decoupling from layout cooling

The `requestAnimationFrame` loop in `tick()` already runs forever — only node
*motion* is gated by `alpha` cooling to 0. The edge buzz uses the frame
timestamp (`t = now / 1000`) directly, independent of `alpha`. Result: nodes
still settle and freeze (preserving the existing fit-on-settle behavior), while
edges keep buzzing indefinitely. The cooling logic is unchanged.

## Color and selection

- Edge stroke color is unchanged (`#30363d` via `.edge` CSS).
- The existing `.dim` class applied to edges on node selection continues to work
  on the `<path>` elements with no change.

## Out of scope (YAGNI)

- No UI controls, settings toggle, or per-edge customization.
- No color or width change to edges.
- No change to nodes, panel, legend, fit, pan/zoom, or the force model.

## Testing

The renderer is opaque JS embedded in a Python string, so automated tests stay
at the HTML-contract level in `tests/test_graphview.py`:

- `render_html` still produces a valid self-contained document for a graph that
  has edges.
- The rendered HTML contains the edge-drawing code / `path` edge elements.
- Edges in the output still correspond only to `data.edges` entries (no
  synthetic lines).

Visual motion is verified manually: rebuild `dist/MindHiveGraph.exe` with Python
3.12 and eyeball the buzz in WebView2 (per the build notes), then
`taskkill //F //IM MindHiveGraph.exe` to close.
