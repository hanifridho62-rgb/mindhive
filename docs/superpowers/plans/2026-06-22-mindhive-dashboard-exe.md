# MIND HIVE Dashboard (.exe) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship one double-clickable `MindHive.exe` that starts a localhost web dashboard surfacing every `hive` feature (view + actions), replacing the graph-only exe.

**Architecture:** A single new module `src/mindhive/dashboard.py` runs a stdlib `ThreadingHTTPServer` bound to `127.0.0.1`, opens the browser, and serves one HTML page plus thin JSON endpoints that call existing `mindhive` functions. A new `hive dashboard` subcommand and the PyInstaller entrypoint both call `dashboard.serve()`. No new runtime dependencies.

**Tech Stack:** Python 3.12 stdlib (`http.server`, `json`, `secrets`, `threading`, `webbrowser`, `urllib`), PyInstaller for packaging, pytest for tests.

---

## File Structure

- **Create** `src/mindhive/dashboard.py` — the server: handler, routing, page HTML, `serve()`. Single responsibility: HTTP presentation over existing functions.
- **Create** `tests/test_dashboard.py` — spins the server on an ephemeral port and exercises endpoints.
- **Modify** `src/mindhive/__main__.py` — add the `hive dashboard` subcommand; remove `graph app`.
- **Modify** `build/app_entry.py` — call `dashboard.serve()` instead of `desktop.launch_app()`.
- **Modify** `build/build_app.py` — rename output to `MindHive`, drop `--windowed` and `--collect-all webview`.
- **Modify** `pyproject.toml` — drop the `app` (pywebview) extra.
- **Modify** `src/mindhive/selftest.py` — include `test_dashboard` in the suite.
- **Delete** `src/mindhive/desktop.py` — pywebview window, now obsolete.
- **Modify** `README.md`, `USAGE.md` — document `hive dashboard` / `MindHive.exe`; drop `graph app`.

Each endpoint is a thin adapter — no business logic is duplicated.

---

## Reference: existing function signatures (already in the codebase)

```
events.append_event(event_type, payload, session_id=None, tags=None, ...) -> Event   # Event has .type, .ts
recall.recall(query, project=None, semantic=False, limit=10, force=False) -> list[{"file","snippet"}]
graph.build(project=None, semantic=False) -> dict
graphview.render_html(graph: dict) -> str
projector.project(project=None) -> dict
chain.verify(project=None) -> dict
briefing.briefing(project=None) -> Path
plugins.scan_and_store(cache_dir=None, top_n=5) -> dict
plugins.latest_catalog() -> dict | None
sync.sync(folder, include_briefing=True) -> Path
export.export(recent=20, out=None) -> Path
vectors.embed(force=False) -> dict
guard.ensure_safe(force=False) -> None        # raises RuntimeError when LIVE_LOCK active and not force
guard.live_lock_active() -> bool
paths.project_dir() -> Path                    # holds STATE.md
paths.store_home() -> Path                     # holds LIVE_LOCK
```

---

## Task 1: Dashboard module skeleton — server, nonce, root page

**Files:**
- Create: `src/mindhive/dashboard.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard.py
import json
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from mindhive import dashboard


def _start():
    """Start a dashboard server on an ephemeral port; return (server, base, token)."""
    token = "test-token-123"
    handler = dashboard.make_handler(token)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}", token


def _get(base, path, token=None):
    url = base + path
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def test_root_page_serves_html_without_token():
    server, base, token = _start()
    try:
        status, body = _get(base, "/")
        assert status == 200
        assert "<html" in body.lower()
        assert "MIND HIVE" in body
    finally:
        server.shutdown()


def test_api_requires_token():
    server, base, token = _start()
    try:
        try:
            _get(base, "/api/state")  # no token
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dashboard.py -v`
Expected: FAIL — `module 'mindhive.dashboard' has no attribute 'make_handler'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mindhive/dashboard.py
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def make_handler(token: str):
    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        # --- helpers ---
        def _check_token(self) -> bool:
            qs = parse_qs(urlparse(self.path).query)
            supplied = self.headers.get("X-Hive-Token") or (qs.get("token") or [None])[0]
            return supplied == token

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

        # --- routing ---
        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/":
                return self._send(200, render_page(token), "text/html; charset=utf-8")
            if not self._check_token():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            return self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            if not self._check_token():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            return self._json({"ok": False, "error": "not found"}, 404)

    return DashboardHandler


def render_page(token: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>MIND HIVE</title></head><body>"
        "<h1>MIND HIVE</h1>"
        f"<script>window.HIVE_TOKEN={json.dumps(token)};</script>"
        "</body></html>"
    )


def serve(port: int = 0, open_browser: bool = True) -> None:
    """Start the dashboard, open the browser, and serve until interrupted."""
    token = secrets.token_urlsafe(16)
    handler = make_handler(token)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    actual = server.server_address[1]
    url = f"http://127.0.0.1:{actual}/?token={token}"
    print(f"MIND HIVE dashboard: {url}")
    print("Close this window or press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_dashboard.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): server skeleton with token-gated API and root page"
```

---

## Task 2: Read endpoints — state, briefing, verify, recall

**Files:**
- Modify: `src/mindhive/dashboard.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_dashboard.py
def test_state_endpoint_returns_text():
    server, base, token = _start()
    try:
        status, body = _get(base, "/api/state", token)
        assert status == 200
        # STATE.md may be absent in a clean store; endpoint must still 200 with text
        assert isinstance(body, str)
    finally:
        server.shutdown()


def test_verify_endpoint_returns_json():
    server, base, token = _start()
    try:
        status, body = _get(base, "/api/verify", token)
        assert status == 200
        obj = json.loads(body)
        assert isinstance(obj, dict)
    finally:
        server.shutdown()


def test_recall_endpoint_returns_list():
    server, base, token = _start()
    try:
        status, body = _get(base, "/api/recall?q=anything", token)
        assert status == 200
        obj = json.loads(body)
        assert isinstance(obj, list)
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dashboard.py -k "state or verify or recall" -v`
Expected: FAIL — these routes return 404.

- [ ] **Step 3: Write minimal implementation**

Add imports at the top of `dashboard.py` (below existing imports):

```python
from . import paths, chain
from . import recall as recall_mod
from .briefing import briefing as build_briefing
```

Replace the `do_GET` body's final two lines (the 404 fallthrough) with a route table:

```python
        def do_GET(self):
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/":
                return self._send(200, render_page(token), "text/html; charset=utf-8")
            if not self._check_token():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            qs = parse_qs(parsed.query)
            try:
                if route == "/api/state":
                    p = paths.project_dir() / "STATE.md"
                    text = p.read_text(encoding="utf-8") if p.exists() else "(no STATE.md — run project)"
                    return self._send(200, text, "text/plain; charset=utf-8")
                if route == "/api/briefing":
                    bpath = build_briefing()
                    return self._send(200, bpath.read_text(encoding="utf-8"), "text/plain; charset=utf-8")
                if route == "/api/verify":
                    return self._json(chain.verify())
                if route == "/api/recall":
                    q = (qs.get("q") or [""])[0]
                    semantic = (qs.get("semantic") or ["0"])[0] in ("1", "true")
                    limit = int((qs.get("limit") or ["10"])[0])
                    force = (qs.get("force") or ["0"])[0] in ("1", "true")
                    hits = recall_mod.recall(q, semantic=semantic, limit=limit, force=force)
                    return self._json(list(hits))
            except Exception as e:  # never crash the server
                return self._json({"ok": False, "error": str(e)}, 200)
            return self._json({"ok": False, "error": "not found"}, 404)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_dashboard.py -v`
Expected: PASS (all tests so far).

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): read endpoints — state, briefing, verify, recall"
```

---

## Task 3: Graph + plugins-recommend + export endpoints

**Files:**
- Modify: `src/mindhive/dashboard.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_dashboard.py
def test_graph_endpoint_returns_html():
    server, base, token = _start()
    try:
        status, body = _get(base, "/api/graph", token)
        assert status == 200
        assert "<" in body  # rendered HTML fragment/document
    finally:
        server.shutdown()


def test_plugins_recommend_returns_json():
    server, base, token = _start()
    try:
        status, body = _get(base, "/api/plugins/recommend", token)
        assert status == 200
        obj = json.loads(body)
        assert isinstance(obj, dict)  # {} when no catalog yet
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dashboard.py -k "graph or plugins" -v`
Expected: FAIL — routes 404.

- [ ] **Step 3: Write minimal implementation**

Add imports to `dashboard.py`:

```python
from . import graph as graph_mod
from . import graphview
from . import plugins as plugins_mod
from . import export as export_mod
```

Add these branches inside the `try:` of `do_GET`, before the final `return`:

```python
                if route == "/api/graph":
                    html = graphview.render_html(graph_mod.build())
                    return self._send(200, html, "text/html; charset=utf-8")
                if route == "/api/plugins/recommend":
                    return self._json(plugins_mod.latest_catalog() or {})
                if route == "/api/export":
                    path = export_mod.export()
                    data = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="handoff.md"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_dashboard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): graph, plugins-recommend, and export endpoints"
```

---

## Task 4: Action endpoints — append, project, sync

**Files:**
- Modify: `src/mindhive/dashboard.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_dashboard.py
def _post(base, path, payload, token):
    url = f"{base}{path}?token={token}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_append_creates_event():
    server, base, token = _start()
    try:
        status, obj = _post(base, "/api/append",
                            {"type": "decision", "payload": "dashboard test note",
                             "tags": "test"}, token)
        assert status == 200
        assert obj["ok"] is True
        assert obj["type"] == "decision"
        assert "ts" in obj
    finally:
        server.shutdown()


def test_post_requires_token():
    server, base, token = _start()
    try:
        url = f"{base}/api/project"
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dashboard.py -k "append or post_requires" -v`
Expected: FAIL — `/api/append` and `/api/project` 404.

- [ ] **Step 3: Write minimal implementation**

Add imports to `dashboard.py`:

```python
from . import events, projector
from . import sync as sync_mod
```

Add a body-reading helper inside the handler class (next to `_json`):

```python
        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
```

Replace `do_POST` with:

```python
        def do_POST(self):
            route = urlparse(self.path).path
            if not self._check_token():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            try:
                body = self._body()
                if route == "/api/append":
                    etype = body.get("type")
                    text = body.get("payload", "")
                    tags = [t for t in (body.get("tags") or "").split(",") if t]
                    if not etype:
                        return self._json({"ok": False, "error": "type is required"}, 200)
                    ev = events.append_event(etype, {"text": text}, tags=tags)
                    projector.project()
                    return self._json({"ok": True, "type": ev.type, "ts": ev.ts})
                if route == "/api/project":
                    return self._json({"ok": True, "result": projector.project()})
                if route == "/api/sync":
                    import os
                    path = sync_mod.sync(os.getcwd())
                    return self._json({"ok": True, "path": str(path)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 200)
            return self._json({"ok": False, "error": "not found"}, 404)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_dashboard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): action endpoints — append, project, sync"
```

---

## Task 5: Guarded/heavy endpoints — embed (LIVE_LOCK), plugins scan, selftest

**Files:**
- Modify: `src/mindhive/dashboard.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_dashboard.py
from mindhive import paths as hp


def test_embed_blocked_by_live_lock():
    server, base, token = _start()
    lock = hp.store_home() / "LIVE_LOCK"
    created = False
    if not lock.exists():
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("test", encoding="utf-8")
        created = True
    try:
        status, obj = _post(base, "/api/embed", {"force": False}, token)
        assert status == 200
        assert obj["ok"] is False
        assert "LIVE_LOCK" in obj["error"]
    finally:
        if created:
            lock.unlink()
        server.shutdown()
```

- [ ] **Step 2: Run tests to verify it fails**

Run: `python -m pytest tests/test_dashboard.py -k embed -v`
Expected: FAIL — `/api/embed` 404.

- [ ] **Step 3: Write minimal implementation**

Add import to `dashboard.py`:

```python
from . import guard
```

Add these branches inside the `try:` of `do_POST`, before the final `return`:

```python
                if route == "/api/embed":
                    force = bool(body.get("force", False))
                    guard.ensure_safe(force=force)  # raises when LIVE_LOCK active and not force
                    from . import vectors
                    return self._json({"ok": True, "result": vectors.embed(force=force)})
                if route == "/api/plugins/scan":
                    result = plugins_mod.scan_and_store(top_n=int(body.get("top", 5)))
                    return self._json({"ok": True, "result": result})
                if route == "/api/selftest":
                    from . import selftest as selftest_mod
                    code = selftest_mod.run()
                    return self._json({"ok": code == 0, "exit_code": code})
```

Note: `guard.ensure_safe` raises `RuntimeError` with "LIVE_LOCK" in the message; the existing `except Exception` turns it into `{"ok": false, "error": "...LIVE_LOCK..."}`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_dashboard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): guarded heavy endpoints — embed, plugins scan, selftest"
```

---

## Task 6: The dashboard page (front-end shell)

**Files:**
- Modify: `src/mindhive/dashboard.py` (replace `render_page`)
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_dashboard.py
def test_page_has_feature_controls():
    server, base, token = _start()
    try:
        status, body = _get(base, "/")
        assert status == 200
        for needle in ["State", "Recall", "Graph", "Append", "Plugins", "Export"]:
            assert needle in body, f"missing control: {needle}"
        assert "fetch(" in body  # the page wires buttons to the API
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dashboard.py -k feature_controls -v`
Expected: FAIL — skeleton page lacks these labels.

- [ ] **Step 3: Write minimal implementation**

Replace `render_page` in `dashboard.py` with a self-contained page (inline CSS/JS, token-aware fetch helper, one section per feature):

```python
def render_page(token: str) -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MIND HIVE</title>
<style>
  :root { color-scheme: dark; }
  body { font: 14px/1.5 system-ui, sans-serif; margin: 0; background:#11131a; color:#e6e8ef; }
  header { padding: 14px 20px; background:#171a23; border-bottom:1px solid #2a2f3d; }
  header h1 { margin:0; font-size:18px; letter-spacing:.5px; }
  main { display:grid; grid-template-columns: 220px 1fr; min-height: calc(100vh - 52px); }
  nav { background:#141722; border-right:1px solid #2a2f3d; padding:12px; }
  nav button { display:block; width:100%; text-align:left; margin:4px 0; padding:8px 10px;
    background:#1d2230; color:#e6e8ef; border:1px solid #2a2f3d; border-radius:6px; cursor:pointer; }
  nav button:hover { background:#262d3d; }
  section.panel { padding:18px 22px; }
  pre#out { white-space:pre-wrap; background:#0c0e14; border:1px solid #2a2f3d; border-radius:8px;
    padding:14px; min-height:120px; max-height:60vh; overflow:auto; }
  iframe#graph { width:100%; height:70vh; border:1px solid #2a2f3d; border-radius:8px; background:#fff; }
  input, select, textarea { background:#0c0e14; color:#e6e8ef; border:1px solid #2a2f3d;
    border-radius:6px; padding:6px 8px; }
  .row { margin:8px 0; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .hidden { display:none; }
  .err { color:#ff8a8a; }
</style></head>
<body>
<header><h1>MIND HIVE &mdash; control panel</h1></header>
<main>
  <nav>
    <button onclick="showState()">State</button>
    <button onclick="showBriefing()">Briefing</button>
    <button onclick="showRecall()">Recall</button>
    <button onclick="showGraph()">Graph</button>
    <button onclick="showAppend()">Append memory</button>
    <button onclick="showPlugins()">Plugins</button>
    <button onclick="doExport()">Export handoff.md</button>
    <button onclick="doAction('/api/project','Project (rebuild)')">Project</button>
    <button onclick="doAction('/api/sync','Sync')">Sync</button>
    <button onclick="doAction('/api/verify','Verify chain', 'GET')">Verify</button>
    <button onclick="confirmAction('/api/embed','Embed (heavy)')">Embed</button>
    <button onclick="confirmAction('/api/plugins/scan','Plugins scan (heavy)')">Plugins scan</button>
    <button onclick="confirmAction('/api/selftest','Self-test (heavy)')">Self-test</button>
  </nav>
  <section class="panel">
    <div id="controls"></div>
    <pre id="out">Pick a feature on the left.</pre>
    <iframe id="graph" class="hidden"></iframe>
  </section>
</main>
<script>
  const TOKEN = __TOKEN__;
  const out = document.getElementById('out');
  const graph = document.getElementById('graph');
  const controls = document.getElementById('controls');
  function showOut(){ graph.classList.add('hidden'); out.classList.remove('hidden'); }
  function u(path){ return path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN); }
  async function apiGet(path){
    const r = await fetch(u(path), { headers: { 'X-Hive-Token': TOKEN } });
    const ct = r.headers.get('content-type') || '';
    return ct.includes('json') ? r.json() : r.text();
  }
  async function apiPost(path, body){
    const r = await fetch(u(path), { method:'POST', headers:{ 'Content-Type':'application/json',
      'X-Hive-Token': TOKEN }, body: JSON.stringify(body || {}) });
    return r.json();
  }
  function render(v){ showOut(); out.textContent = (typeof v === 'string') ? v : JSON.stringify(v, null, 2); }
  function fail(e){ showOut(); out.innerHTML = '<span class="err">'+ (e && e.message || e) +'</span>'; }

  async function showState(){ controls.innerHTML=''; try{ render(await apiGet('/api/state')); }catch(e){ fail(e); } }
  async function showBriefing(){ controls.innerHTML=''; try{ render(await apiGet('/api/briefing')); }catch(e){ fail(e); } }
  function showRecall(){
    controls.innerHTML = '<div class="row"><input id="q" placeholder="search query">'
      + '<label><input type="checkbox" id="sem"> semantic</label>'
      + '<button onclick="runRecall()">Search</button></div>';
    showOut(); out.textContent = 'Enter a query.';
  }
  async function runRecall(){
    const q = document.getElementById('q').value;
    const sem = document.getElementById('sem').checked ? '1' : '0';
    try{ render(await apiGet('/api/recall?q='+encodeURIComponent(q)+'&semantic='+sem)); }catch(e){ fail(e); }
  }
  async function showGraph(){
    controls.innerHTML='';
    try{ const html = await apiGet('/api/graph'); out.classList.add('hidden');
      graph.classList.remove('hidden'); graph.srcdoc = html; }catch(e){ fail(e); }
  }
  function showAppend(){
    controls.innerHTML = '<div class="row"><select id="etype">'
      + ['decision','caveat','experiment_result','next_step','open_question','state_set']
        .map(t=>'<option>'+t+'</option>').join('')
      + '</select></div>'
      + '<div class="row"><textarea id="etext" rows="3" cols="60" placeholder="one line"></textarea></div>'
      + '<div class="row"><input id="etags" placeholder="tags,comma,separated">'
      + '<button onclick="runAppend()">Append</button></div>';
    showOut(); out.textContent = 'Fill the form and append.';
  }
  async function runAppend(){
    const body = { type: document.getElementById('etype').value,
      payload: document.getElementById('etext').value, tags: document.getElementById('etags').value };
    try{ render(await apiPost('/api/append', body)); }catch(e){ fail(e); }
  }
  async function showPlugins(){ controls.innerHTML=''; try{ render(await apiGet('/api/plugins/recommend')); }catch(e){ fail(e); } }
  function doExport(){ window.location = u('/api/export'); }
  async function doAction(path, label, method){
    controls.innerHTML=''; render(label + '…');
    try{ render(method === 'GET' ? await apiGet(path) : await apiPost(path, {})); }catch(e){ fail(e); }
  }
  async function confirmAction(path, label){
    if(!confirm(label + ' — this can take a while. Continue?')) return;
    let body = {};
    if(path === '/api/embed' && confirm('Force past LIVE_LOCK? (only if the box is idle)')) body = { force: true };
    controls.innerHTML=''; render(label + ' running…');
    try{ render(await apiPost(path, body)); }catch(e){ fail(e); }
  }
</script>
</body></html>""".replace("__TOKEN__", json.dumps(token))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dashboard.py -v`
Expected: PASS (all dashboard tests).

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): full control-panel page wiring all features"
```

---

## Task 7: CLI subcommand `hive dashboard`; remove `graph app`

**Files:**
- Modify: `src/mindhive/__main__.py`
- Delete: `src/mindhive/desktop.py`
- Test: `tests/test_cli.py` (or `tests/test_cli_new.py`)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_cli_new.py
from mindhive import __main__ as cli


def test_dashboard_subcommand_parses(monkeypatch):
    called = {}
    import mindhive.dashboard as dash
    monkeypatch.setattr(dash, "serve", lambda **kw: called.update(kw))
    rc = cli.main(["dashboard", "--no-browser", "--port", "8123"])
    assert rc == 0
    assert called == {"port": 8123, "open_browser": False}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_new.py -k dashboard -v`
Expected: FAIL — `invalid choice: 'dashboard'`.

- [ ] **Step 3: Write minimal implementation**

In `src/mindhive/__main__.py`, add the parser (next to the other `sub.add_parser` calls, e.g. after the `sync` block):

```python
    # --- dashboard (one-window control panel; powers MindHive.exe) ---
    dp = sub.add_parser("dashboard", help="open the web control panel for all features")
    dp.add_argument("--port", type=int, default=0, help="port (default: a free one)")
    dp.add_argument("--no-browser", dest="no_browser", action="store_true",
                    help="don't auto-open the browser")
```

Add the dispatch branch (next to the other `elif args.cmd == ...` blocks):

```python
    elif args.cmd == "dashboard":
        from . import dashboard
        dashboard.serve(port=args.port, open_browser=not args.no_browser)
```

Remove the obsolete `graph app` subcommand and its dispatch. Delete this line in the graph parser block:

```python
    gsub.add_parser("app", help="open the desktop graph window")
```

And delete this dispatch branch:

```python
        elif args.graph_cmd == "app":
            from . import desktop
            desktop.launch_app()
```

Then delete the file:

```bash
git rm src/mindhive/desktop.py
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli_new.py tests/test_dashboard.py -v`
Expected: PASS. Also run `python -m pytest tests/ -q` to confirm nothing referenced `graph app` / `desktop`.

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/__main__.py tests/test_cli_new.py
git rm src/mindhive/desktop.py
git commit -m "feat(cli): add 'hive dashboard'; remove obsolete 'graph app' window"
```

---

## Task 8: Build wiring — entrypoint, PyInstaller spec, pyproject

**Files:**
- Modify: `build/app_entry.py`
- Modify: `build/build_app.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Update the exe entrypoint**

Replace the contents of `build/app_entry.py`:

```python
# build/app_entry.py
"""PyInstaller entrypoint: launch the MIND HIVE web dashboard."""
from mindhive import dashboard

if __name__ == "__main__":
    dashboard.serve()
```

- [ ] **Step 2: Update the build script**

In `build/build_app.py`, change the PyInstaller args:
- `--name` value `MindHiveGraph` → `MindHive`
- remove `"--windowed",` (keep a console so the URL shows and Ctrl+C works)
- remove `"--collect-all", "webview",` (pywebview is gone)

Resulting `args` list:

```python
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--name", "MindHive",
        "--paths", str(ROOT / "src"),
    ]
    if ICON.exists():
        args += ["--icon", str(ICON)]
    args.append(str(ENTRY))
```

- [ ] **Step 3: Drop the pywebview extra**

In `pyproject.toml`, remove the `app` extra line:

```
app = ["pywebview>=5"]
```

Leave `vectors`, `dev`, and `build` extras unchanged.

- [ ] **Step 4: Build and smoke-test (Windows, Python 3.12)**

```bash
pip install -e .[build]
python build/build_app.py
```

Expected: `dist/MindHive.exe` is produced. Double-click it: a console window shows `MIND HIVE dashboard: http://127.0.0.1:<port>/?token=…` and the browser opens the control panel. To stop a stuck instance, use `taskkill /IM MindHive.exe /F` (not git-bash `kill`).

- [ ] **Step 5: Commit**

```bash
git add build/app_entry.py build/build_app.py pyproject.toml
git commit -m "build: produce MindHive.exe (dashboard) and drop pywebview"
```

---

## Task 9: Wire `test_dashboard` into selftest; update docs

**Files:**
- Modify: `src/mindhive/selftest.py`
- Modify: `README.md`
- Modify: `USAGE.md`

- [ ] **Step 1: Inspect how selftest selects tests**

Run: `python -c "import inspect, mindhive.selftest as s; print(inspect.getsource(s.run))"`
Expected: shows how `run()` discovers/lists tests (e.g. a pytest invocation over `tests/` or an explicit module list).

- [ ] **Step 2: Ensure test_dashboard is included**

If `run()` discovers the whole `tests/` directory, no change is needed — confirm by running `hive selftest` and seeing `test_dashboard` execute. If `run()` lists modules explicitly, add `"test_dashboard"` to that list in the same style as the existing entries.

- [ ] **Step 3: Update README.md**

In the Commands list, replace the `hive graph app` line with:

```
- `hive dashboard [--port N] [--no-browser]` — open the web control panel for every feature
```

In the "Desktop graph app" section, replace the build/run instructions so they describe the unified dashboard:

```
## Desktop control panel

`hive dashboard` starts a localhost web app (bound to 127.0.0.1) and opens your
browser to a control panel for every feature — view state/briefing/graph, run
recall, append memories, project, sync, verify, export, embed, and plugins
scan. The graph is one of its views.

To build a double-click binary (Windows, Python 3.12):

    pip install -e .[build]
    python build/build_app.py        # -> dist/MindHive.exe

The exe runs a small console window showing the URL; close it to stop the server.
```

- [ ] **Step 4: Update USAGE.md**

Add a line under the recall/connections tips:

```
Open everything in one window:  hive dashboard   (or double-click MindHive.exe)
```

- [ ] **Step 5: Run the full suite and commit**

Run: `python -m pytest tests/ -q`
Expected: PASS (including `test_dashboard`).

```bash
git add src/mindhive/selftest.py README.md USAGE.md
git commit -m "docs: document hive dashboard / MindHive.exe; cover dashboard in selftest"
```

---

## Self-Review notes

- **Spec coverage:** form (Task 1 `serve`), full view+actions (Tasks 2–6), one unified exe replacing graph-only (Task 8), stdlib-only/no new deps (all tasks), localhost bind (Task 1), session nonce (Tasks 1, 4, 6), LIVE_LOCK guard (Task 5), heavy-action confirms (Task 6 UI), retire `graph app`/`desktop.py` (Task 7), docs (Task 9), selftest entry (Task 9). All spec sections map to a task.
- **Heavy ops async:** the spec mentions worker threads for `plugins scan`/`selftest`. The plan runs them synchronously inside the request handler (which is already on a `ThreadingHTTPServer` worker thread, so the server stays responsive to other requests). This satisfies the responsiveness intent without extra machinery; the UI shows a "running…" state. Documented here so the implementer doesn't add redundant threading.
- **Type consistency:** event uses `ev.type`/`ev.ts`; recall returns a list; `latest_catalog()` may be `None` (coerced to `{}`); `guard.ensure_safe` raises with "LIVE_LOCK" in the message — all consistent across tasks.
- **Token check:** root `/` is open; every `/api/*` (GET and POST) requires the token — consistent in Tasks 1, 2, 4.
