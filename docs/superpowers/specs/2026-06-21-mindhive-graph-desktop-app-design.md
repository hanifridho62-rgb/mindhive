# MIND HIVE Graph — Desktop App Design

**Date:** 2026-06-21
**Status:** Approved (pending spec review)
**Author:** Hanif + Claude

## Summary

Add a free, installable desktop application that visually renders MIND HIVE's
existing memory knowledge graph (`graph.json`) as an interactive, explorable
view. Inspired by [Egonex-AI/Understand-Anything](https://github.com/Egonex-AI/Understand-Anything),
but graphing **session memory** (events/decisions/files/tags), not a codebase.

The app reuses one HTML/JS renderer in two delivery forms:

1. A portable self-contained `graph.html` (opens in any browser, free bonus).
2. A native desktop window packaged as a Windows `.exe`.

No paid APIs, no API keys, no network at runtime. Consistent with MIND HIVE's
"zero API tokens, offline, deterministic" ethos.

## Goals

- Render the existing `graph.json` as an interactive force-directed graph.
- Deliver it as a double-click Windows desktop app with its own window and icon.
- Stay free: no paid services, no keys, no network calls at view time.
- Keep the core `hive` CLI zero-dependency; app tooling is opt-in extras only.

## Non-Goals (explicitly cut to stay lean)

- In-page search / filter.
- Cluster / topic summaries.
- LLM-generated node summaries (node text is already plain-English).
- Semantic edges on by default (still available via `--semantic`, unchanged).
- Cross-OS binaries in this iteration (Windows `.exe` only; Mac/Linux are the
  same build command on those OSes, but not built now).
- Changing how the graph is *built* — this work only consumes `graph.json`.

## Reference vs. This Project

| Understand-Anything            | MIND HIVE Graph (this)                     |
|--------------------------------|--------------------------------------------|
| Graphs a codebase              | Graphs session memory                      |
| Tree-sitter + LLM summaries    | Existing event projections, no LLM         |
| Astro web dashboard + server   | Self-contained HTML in a native window     |
| Needs an AI platform/API       | No API, no key, fully offline              |

## Architecture

### Components

```
src/mindhive/
  graphview.py   NEW  render_html(graph: dict) -> str
                      Pure function: inline nodes+edges JSON + JS renderer
                      into one self-contained HTML string. No I/O, no network.
  desktop.py     NEW  launch_app(project=None)
                      Opens a pywebview native window that loads the HTML
                      produced by graphview.render_html(). Rebuilds graph.json
                      via graph.build() if missing.
build/
  build_app.py   NEW  PyInstaller build script -> dist/MindHiveGraph.exe
  icon.ico       NEW  Windows app icon
tests/
  test_graphview.py  NEW  deterministic tests for render_html / export
```

### Reuse boundary

`graphview.render_html()` is the single source of truth for the visual. Both
the browser export and the desktop window call it. The window is just a host;
the file export is the same HTML written to disk.

### Data flow

```
events (JSONL) --[existing]--> graph.build() --> graph.json
                                                    |
                                graphview.render_html(graph) --> HTML string
                                       /                    \
                  export: write graph.html          desktop: pywebview window
                  (open in any browser)             (own window, packaged .exe)
```

### Dependencies

- **Core CLI:** unchanged, zero runtime dependencies.
- **Optional extras in `pyproject.toml`:**
  - `app = ["pywebview>=5"]` — needed only to *run* the native window.
  - `build = ["pyinstaller>=6"]` — needed only to *freeze* the binary.
- `pip install -e .` (plain CLI) installs nothing new.
- Expected binary size ~30–50 MB (uses OS WebView2; no bundled browser).
  Acceptable ceiling: 150 MB.

## The Renderer (`graphview.render_html`)

A single HTML string with three inlined pieces, no external references:

1. **Data** — `{nodes, edges}` from `graph.json` embedded as a JSON `<script>`
   block. No `fetch`, no CDN.
2. **Renderer** — hand-written vanilla-JS SVG force-directed layout:
   - Nodes as circles, colored by `type`: event, session, file, tag.
   - Edges as lines, labeled by `rel`: in_session, touched, tagged, next,
     shares_tag, semantic_near.
   - Interactions: pan, zoom, drag nodes. (This is the full "interactive
     explore" scope; nothing more.)
3. **Side panel** — clicking a node shows: `label`, `type`, full payload text
   (for event nodes), `tags`, and the list of directly connected nodes with the
   relation linking each. Built client-side by walking the inlined edges.

Constraint: the output MUST contain no `http://` or `https://` external
resource references — this is what proves it is offline/self-contained and is
asserted by tests.

## Desktop App (`desktop.py`)

- `launch_app()` loads current `graph.json` (calls `graph.build()` if absent),
  passes it to `render_html()`, and opens a pywebview window titled
  "MIND HIVE Graph" with the app icon.
- Minimal chrome: just the graph view + side panel. No menu system.
- Closing the window exits the process.

## Packaging (`build/build_app.py`)

- Invokes PyInstaller in one-file mode against an entry script that calls
  `mindhive.desktop.launch_app()`.
- Sets the Windows icon (`build/icon.ico`) and product name `MindHiveGraph`.
- Output: `dist/MindHiveGraph.exe`, double-clickable, no Python required to run.
- Documented as a manual `python build/build_app.py` step (not run in CI).

## CLI Surface

Extend the existing `graph` subcommand group:

- `hive graph export [--out PATH] [--open] [--semantic]`
  Writes the self-contained `graph.html` (default into the project dir, atomic
  tmp+replace like `graph.json`). `--open` opens it in the default browser.
  `--semantic` forces a semantic rebuild first (same semantics as `graph build`).
- `hive graph app`
  Launches the native pywebview window directly (requires the `app` extra; on a
  dev machine with Python). Prints a clear message if pywebview is missing.

Existing `graph build` and `graph related` are unchanged.

## Error Handling

- `graph app` without the `app` extra installed: catch ImportError, print
  `desktop app needs extras: pip install -e .[app]` and exit non-zero
  (mirrors the `vectors._require_deps()` pattern).
- Missing `graph.json` on export/app: build it first (existing `graph.build()`).
- Empty graph (no events): render a valid HTML file showing an empty-state
  message; never crash.
- Atomic writes (tmp + replace) for `graph.html`, matching `graph.json`.

## Testing

`tests/test_graphview.py` (deterministic, no GUI/display required):

1. `render_html` on a known graph contains every node `id`.
2. Output contains no `http://` or `https://` external references (offline proof).
3. Empty graph (`{"nodes": [], "edges": []}`) returns valid, non-crashing HTML.
4. `graph export --out PATH` writes the file to the requested path.
5. Export builds `graph.json` first if it is missing.

Wired into `hive selftest`.

GUI launch (`desktop.py`) and the PyInstaller build are validated by a
documented manual smoke test (launch a real window; freeze and run the binary).
These are not unit-testable without a display and are excluded from `selftest`.

## Build / Run Order (for the implementer)

1. `graphview.py` + tests (pure, TDD-friendly).
2. Wire `hive graph export` into `__main__.py`.
3. `desktop.py` (pywebview window) + `app` extra.
4. Wire `hive graph app`.
5. `build/build_app.py` + icon + `build` extra; produce `dist/MindHiveGraph.exe`.
6. Manual smoke test of window and binary; update README.

## Open Questions

None outstanding. Cross-OS binaries deferred by decision.
