# MIND HIVE Dashboard — one .exe for every feature

**Date:** 2026-06-22
**Status:** Approved (design)
**Author:** Hanif + Claude Opus 4.8

## Goal

Ship a single double-clickable program that opens **all** of MIND HIVE's
features from one place, instead of remembering ~15 `hive` CLI subcommands.
The program is a **local web dashboard**: the exe starts a localhost server and
opens the default browser to a dashboard page. It **replaces** the existing
graph-only `MindHiveGraph.exe` (the graph becomes one view inside the
dashboard).

## Decisions (locked)

| Question | Decision |
|---|---|
| Form | Local web dashboard (server + browser), bound to `127.0.0.1` only |
| Scope | Full — view **and** actions (append, project, sync, embed, plugins scan, selftest, verify, export) |
| Exe strategy | One unified exe (`MindHive`), retire `MindHiveGraph.exe` |
| Web stack | Python stdlib `http.server` — **zero new dependencies** |

## Architecture

One new module `src/mindhive/dashboard.py` holds the server. Everything else
reuses existing modules. The dashboard is a thin presentation layer — **no
business logic is duplicated**; each endpoint calls the existing function.

```
build/app_entry.py  ──►  dashboard.serve()      # exe double-click entrypoint
hive dashboard      ──►  dashboard.serve()      # new CLI subcommand, same path
                            │
                            ├─ ThreadingHTTPServer on 127.0.0.1:<port>
                            ├─ opens default browser to the URL (unless --no-browser)
                            └─ DashboardHandler(BaseHTTPRequestHandler)
```

### Endpoints

| Method / path | Calls | Notes |
|---|---|---|
| `GET /` | — | One self-contained HTML page (inline CSS/JS), served from a string constant |
| `GET /api/state` | reads `STATE.md` (`paths`) | text |
| `GET /api/briefing` | `briefing.briefing()` | text |
| `GET /api/graph` | `graphview.render_html(graph.build())` | full graph HTML, shown in an iframe |
| `GET /api/recall?q=&semantic=&limit=` | `recall.recall(...)` | JSON hits; `--semantic` honors LIVE_LOCK guard |
| `GET /api/plugins/recommend` | `plugins.latest_catalog()` | JSON |
| `GET /api/export` | `export.export()` | returns handoff.md as a file download |
| `POST /api/append` | `events.append_event` + `projector.project` | body `{type, payload, tags}`; returns new event id/ts |
| `POST /api/project` | `projector.project()` | JSON result |
| `POST /api/verify` | `chain.verify()` | JSON result |
| `POST /api/sync` | `sync.sync(cwd)` | JSON path |
| `POST /api/embed` | `vectors.embed(force=…)` | **LIVE_LOCK gated** (see Safety) |
| `POST /api/plugins/scan` | `plugins.scan_and_store(...)` | heavy; runs in a worker thread |
| `POST /api/selftest` | `selftest.run()` | heavy; runs in a worker thread |

### Server lifecycle

- Picks a free ephemeral port (bind `127.0.0.1:0`, read the assigned port) so it
  never collides with another instance. `hive dashboard --port N` forces a port.
- Opens the browser via `webbrowser.open` after the server is listening.
- The exe runs **with a console window** (PyInstaller, no `--windowed`): the
  console prints the URL and acts as the "server is running" indicator — closing
  it (or Ctrl+C) stops the server. This is better UX and more debuggable for a
  local single-user server than a hidden window.

## Action handling & safety

- **localhost-only.** The socket binds `127.0.0.1`; the dashboard is never
  reachable from the network. No auth/login.
- **Session nonce.** On start the server mints a random token; `/` embeds it and
  every `/api/*` call must present it (header or query param). This cheaply
  blocks other local processes / drive-by browser tabs from poking the action
  endpoints. Missing/wrong token → `403`.
- **LIVE_LOCK guard preserved.** `embed` and `recall --semantic` call the
  existing `guard.ensure_safe(force)`. When `~/.mindhive/LIVE_LOCK` exists the
  embed button renders disabled with the V17 warning; a "force" checkbox maps to
  `force=True`. The dashboard never bypasses the guard silently.
- **Confirm destructive/heavy actions.** The UI shows a confirm prompt before
  `embed --force`, `plugins scan`, and `selftest`.
- **Errors never crash the server.** Every handler wraps its call in try/except
  and returns `{ok: false, error: "..."}` with a non-500 JSON body where
  sensible; the page shows the error inline.
- **Heavy ops are async.** `plugins scan` and `selftest` run in a worker thread
  so the UI stays responsive; the page polls or streams their output.

## Build & packaging

- `build/app_entry.py` → calls `dashboard.serve()` (was `desktop.launch_app()`).
- `build/build_app.py` → name becomes `MindHive`; drop `--windowed` and
  `--collect-all webview` (no pywebview anymore). Keep `--onefile`, `--icon`.
- Built on **Python 3.12** (per project build note). Output: `dist/MindHive.exe`.
- `pyproject.toml`: the `app` extra (`pywebview`) is dropped; `build`
  (`pyinstaller`) stays. Core remains zero-dependency.

## Retirement / cleanup

- Remove `src/mindhive/desktop.py` and the `hive graph app` subcommand (the
  pywebview window). Its job is now covered by the dashboard's graph view.
- Update `README.md` and `USAGE.md`: document `hive dashboard` and the new
  `MindHive.exe`; remove `hive graph app` and the old build instructions.

## CLI change

New subcommand wired in `src/mindhive/__main__.py`:

```
hive dashboard [--port N] [--no-browser]
```

Default: pick a free port, open the browser, serve until interrupted.

## Testing

`tests/test_dashboard.py`:

- Start `ThreadingHTTPServer` on an ephemeral port in setup; tear down after.
- Hit each read endpoint with `urllib.request`; assert status and shape
  (`/api/state` text, `/api/recall` JSON list, `/api/verify` JSON).
- `POST /api/append` creates an event and `projector` reflects it.
- A request without the session nonce returns `403`.
- With a fake `LIVE_LOCK` present, `POST /api/embed` (no force) returns the guard
  error and does not run; `force=true` is allowed.
- Add `test_dashboard` to the `hive selftest` suite list.

Note: the built exe / any spawned process is killed with `taskkill` on Windows
(not git-bash `kill`).

## Out of scope (YAGNI)

- No multi-user, no remote access, no HTTPS (localhost only).
- No live auto-refresh / websockets — explicit buttons + manual refresh.
- No new event-type editor beyond the six semantic types in the append form.
