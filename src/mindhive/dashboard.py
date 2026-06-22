# src/mindhive/dashboard.py
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import paths, chain
from . import guard
from . import recall as recall_mod
from . import graph as graph_mod
from . import graphview
from . import plugins as plugins_mod
from . import export as export_mod
from . import events, projector
from . import sync as sync_mod
from .briefing import briefing as build_briefing


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

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        # --- routing ---
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
                if route == "/api/overview":
                    evs = events.read_all()
                    by_type: dict = {}
                    for ev in evs:
                        by_type[ev.type] = by_type.get(ev.type, 0) + 1
                    return self._json({
                        "project": paths.project_dir().name,
                        "events": len(evs),
                        "by_type": by_type,
                        "last_ts": evs[-1].ts if evs else None,
                        "chain_ok": bool(chain.verify().get("ok")),
                    })
                if route == "/api/recall":
                    q = (qs.get("q") or [""])[0]
                    semantic = (qs.get("semantic") or ["0"])[0] in ("1", "true")
                    limit = int((qs.get("limit") or ["10"])[0])
                    force = (qs.get("force") or ["0"])[0] in ("1", "true")
                    hits = recall_mod.recall(q, semantic=semantic, limit=limit, force=force)
                    return self._json(list(hits))
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
            except Exception as e:  # never crash the server
                return self._json({"ok": False, "error": str(e)}, 200)
            return self._json({"ok": False, "error": "not found"}, 404)

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
                if route == "/api/embed":
                    force = bool(body.get("force", False))
                    guard.ensure_safe(force=force)  # raises when LIVE_LOCK active and not force
                    from . import vectors
                    return self._json({"ok": True, "result": vectors.embed(force=force)})
                if route == "/api/plugins/scan":
                    result = plugins_mod.scan_and_store(top_n=int(body.get("top", 5)))
                    return self._json({"ok": True, "result": result})
                if route == "/api/plugins/autoscan":
                    res = plugins_mod.ensure_fresh(
                        top_n=int(body.get("top", 5)),
                        max_age_hours=float(body.get("max_age_hours", 24)),
                        force=bool(body.get("force", False)),
                    )
                    return self._json({"ok": True, "scanned": res["scanned"],
                                       "age_hours": res["age_hours"], "result": res["payload"]})
                if route == "/api/selftest":
                    from . import selftest as selftest_mod
                    code = selftest_mod.run()
                    return self._json({"ok": code == 0, "exit_code": code})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 200)
            return self._json({"ok": False, "error": "not found"}, 404)

    return DashboardHandler


def render_page(token: str) -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MIND HIVE</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.5 system-ui, sans-serif; margin: 0; background:#11131a; color:#e6e8ef; }
  header { padding: 12px 20px; background:#171a23; border-bottom:1px solid #2a2f3d;
    display:flex; align-items:center; gap:14px; position:sticky; top:0; z-index:5; }
  header h1 { margin:0; font-size:17px; letter-spacing:.5px; }
  header .dot { width:9px; height:9px; border-radius:50%; background:#3a4054; }
  header .dot.busy { background:#e0b341; animation: pulse 1s infinite; }
  header .dot.ok { background:#46c46a; }
  header #status { font-size:12px; color:#9aa3b8; }
  header #live { font-size:12px; cursor:pointer; user-select:none; margin-left:auto;
    color:#46c46a; }
  header #live.paused { color:#6b7488; }
  header #live .blip { animation: pulse 1.6s infinite; }
  header #synced { font-size:11px; color:#6b7488; min-width:118px; text-align:right; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  main { display:grid; grid-template-columns: 220px 1fr; min-height: calc(100vh - 50px); }
  nav { background:#141722; border-right:1px solid #2a2f3d; padding:12px; }
  nav .group { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:#6b7488;
    margin:14px 6px 4px; }
  nav button { display:block; width:100%; text-align:left; margin:4px 0; padding:8px 10px;
    background:#1d2230; color:#e6e8ef; border:1px solid #2a2f3d; border-radius:6px; cursor:pointer; }
  nav button:hover:not(:disabled) { background:#262d3d; }
  nav button:disabled { opacity:.45; cursor:wait; }
  section.panel { padding:18px 22px; }
  pre#out { white-space:pre-wrap; background:#0c0e14; border:1px solid #2a2f3d; border-radius:8px;
    padding:14px; min-height:120px; max-height:62vh; overflow:auto; }
  iframe#graph { width:100%; height:72vh; border:1px solid #2a2f3d; border-radius:8px; background:#fff; }
  input, select, textarea { background:#0c0e14; color:#e6e8ef; border:1px solid #2a2f3d;
    border-radius:6px; padding:6px 8px; font:inherit; }
  .row { margin:8px 0; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .hidden { display:none; }
  .err { color:#ff8a8a; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  .card { background:#0c0e14; border:1px solid #2a2f3d; border-radius:10px; padding:14px 16px; }
  .card .big { font-size:26px; font-weight:600; }
  .card .lbl { font-size:12px; color:#9aa3b8; margin-top:2px; }
  .card .big.ok { color:#46c46a; } .card .big.bad { color:#ff8a8a; }
  .types { margin-top:14px; display:flex; flex-wrap:wrap; gap:8px; }
  .pill { background:#1d2230; border:1px solid #2a2f3d; border-radius:999px; padding:3px 10px; font-size:12px; }
  .pill b { color:#fff; }
  .hits { list-style:none; padding:0; margin:0; }
  .hits li { background:#0c0e14; border:1px solid #2a2f3d; border-radius:8px; padding:10px 12px; margin:8px 0; }
  .hits .file { font-size:12px; color:#7fb2ff; word-break:break-all; }
  .hits .snip { white-space:pre-wrap; margin-top:4px; }
  h2.h { margin:2px 0 12px; font-size:16px; }
</style></head>
<body>
<header>
  <h1>MIND HIVE</h1>
  <span id="busy" class="dot"></span>
  <span id="status">ready</span>
  <span id="live" title="Real-time updates — click to pause/resume" onclick="toggleLive()"><span class="blip">&#9679;</span> live</span>
  <span id="synced"></span>
</header>
<main>
  <nav>
    <button onclick="showOverview()">Overview</button>
    <div class="group">View</div>
    <button onclick="showState()">State</button>
    <button onclick="showBriefing()">Briefing</button>
    <button onclick="showRecall()">Recall</button>
    <button onclick="showGraph()">Graph</button>
    <button onclick="showPlugins()">Plugins</button>
    <div class="group">Actions</div>
    <button onclick="showAppend()">Append memory</button>
    <button onclick="doExport()">Export handoff.md</button>
    <button onclick="doAction('/api/project','Project (rebuild)')">Project</button>
    <button onclick="doAction('/api/sync','Sync')">Sync</button>
    <button onclick="doAction('/api/verify','Verify chain', 'GET')">Verify</button>
    <div class="group">Heavy</div>
    <button onclick="confirmAction('/api/embed','Embed')">Embed</button>
    <button onclick="confirmAction('/api/plugins/scan','Plugins scan')">Plugins scan</button>
    <button onclick="confirmAction('/api/selftest','Self-test')">Self-test</button>
  </nav>
  <section class="panel">
    <div id="controls"></div>
    <div id="view"></div>
    <pre id="out" class="hidden"></pre>
    <iframe id="graph" class="hidden"></iframe>
  </section>
</main>
<script>
  const TOKEN = __TOKEN__;
  const out = document.getElementById('out');
  const view = document.getElementById('view');
  const graph = document.getElementById('graph');
  const controls = document.getElementById('controls');
  const busyDot = document.getElementById('busy');
  const statusEl = document.getElementById('status');
  const navButtons = () => Array.from(document.querySelectorAll('nav button'));
  const liveEl = document.getElementById('live');
  const syncedEl = document.getElementById('synced');

  // --- real-time engine ---
  // liveFn is the silent re-renderer for the current view (null = view isn't
  // auto-refreshable). A timer re-runs it on an interval, and actions that
  // mutate memory call refreshLive() so changes appear without a manual click.
  let liveFn = null;
  let liveOn = true;
  const LIVE_MS = 2500;
  function pad2(n){ return (n<10?'0':'')+n; }
  function markSynced(){
    const d = new Date();
    syncedEl.textContent = 'updated ' + pad2(d.getHours())+':'+pad2(d.getMinutes())+':'+pad2(d.getSeconds());
  }
  function toggleLive(){
    liveOn = !liveOn;
    liveEl.classList.toggle('paused', !liveOn);
    liveEl.innerHTML = (liveOn ? '<span class="blip">&#9679;</span> live' : '&#9679; paused');
    if (liveOn) refreshLive();
  }
  // silent refresh: no busy spinner, no nav-disable, errors swallowed so a
  // transient blip never disrupts what the user is reading.
  async function refreshLive(){
    if (!liveOn || !liveFn) return;
    if (document.hidden) return;
    if (busyDot.classList.contains('busy')) return;  // don't fight a manual op
    try { await liveFn(); markSynced(); } catch(_) { /* keep last good view */ }
  }
  setInterval(refreshLive, LIVE_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshLive(); });

  function esc(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
  function u(path){ return path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN); }
  function setBusy(on, label){
    busyDot.className = 'dot' + (on ? ' busy' : '');
    statusEl.textContent = on ? (label || 'working…') : 'ready';
    navButtons().forEach(b => b.disabled = on);
  }
  // panel switching
  function only(el){ for (const e of [out, view, graph]) e.classList.toggle('hidden', e !== el); }
  function showText(v){ only(out); out.textContent = (typeof v === 'string') ? v : JSON.stringify(v, null, 2); }
  function showHTML(html){ only(view); view.innerHTML = html; }
  function fail(e){
    only(out);
    const msg = (e && e.message) || String(e);
    if (/failed to fetch|networkerror|load failed/i.test(msg)) {
      out.innerHTML = '<span class="err">Can\\'t reach the MIND HIVE server.</span>\\n\\n'
        + 'The server has stopped, or this tab is from an earlier run. Re-open '
        + '<b>MindHive.exe</b> \\u2014 it opens a fresh tab. Each run uses its own one-time '
        + 'link, so older tabs stop working.';
    } else {
      out.innerHTML = '<span class="err">'+ esc(msg) +'</span>';
    }
  }

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
  // wrap any async unit of work with busy state + error handling
  async function run(label, fn){
    setBusy(true, label);
    try { await fn(); } catch(e) { fail(e); } finally { setBusy(false); }
  }

  async function renderOverview(){
    const o = await apiGet('/api/overview');
    const chip = o.chain_ok
      ? '<span class="big ok">OK</span>' : '<span class="big bad">CHECK</span>';
    const types = Object.entries(o.by_type || {}).sort((a,b)=>b[1]-a[1])
      .map(([t,n]) => '<span class="pill">'+esc(t)+' <b>'+n+'</b></span>').join('') || '<span class="lbl">no events yet</span>';
    showHTML(
      '<h2 class="h">'+esc(o.project)+'</h2>'
      + '<div class="cards">'
      + '<div class="card"><div class="big">'+o.events+'</div><div class="lbl">events</div></div>'
      + '<div class="card"><div>'+chip+'</div><div class="lbl">hash chain</div></div>'
      + '<div class="card"><div class="big" style="font-size:15px">'+esc(o.last_ts || '—')+'</div><div class="lbl">last activity</div></div>'
      + '</div><div class="types">'+types+'</div>');
  }
  function showOverview(){ controls.innerHTML=''; liveFn = renderOverview; run('loading overview', renderOverview); }

  async function renderState(){ showText(await apiGet('/api/state')); }
  function showState(){ controls.innerHTML=''; liveFn = renderState; run('loading state', renderState); }
  function showBriefing(){ controls.innerHTML=''; liveFn = null; run('loading briefing', async () => showText(await apiGet('/api/briefing'))); }

  function showRecall(){
    liveFn = null;
    controls.innerHTML = '<div class="row"><input id="q" placeholder="search query" style="min-width:260px">'
      + '<label><input type="checkbox" id="sem"> semantic</label>'
      + '<button onclick="runRecall()">Search</button></div>';
    showText('Enter a query and search.');
    const q = document.getElementById('q');
    q.focus();
    q.addEventListener('keydown', e => { if (e.key === 'Enter') runRecall(); });
  }
  function runRecall(){
    const q = document.getElementById('q').value;
    const sem = document.getElementById('sem').checked ? '1' : '0';
    run('searching', async () => {
      const hits = await apiGet('/api/recall?q='+encodeURIComponent(q)+'&semantic='+sem);
      if (!hits.length) { showText('No matches for "'+q+'".'); return; }
      showHTML('<h2 class="h">'+hits.length+' result'+(hits.length>1?'s':'')+'</h2><ul class="hits">'
        + hits.map(h => '<li><div class="file">'+esc(h.file||'')+'</div><div class="snip">'+esc(h.snippet||'')+'</div></li>').join('')
        + '</ul>');
    });
  }

  function showGraph(){ controls.innerHTML=''; liveFn = null; run('building graph', async () => {
    const html = await apiGet('/api/graph'); only(graph); graph.srcdoc = html;
  }); }

  function showAppend(){
    liveFn = null;
    controls.innerHTML = '<div class="row"><select id="etype">'
      + ['decision','caveat','experiment_result','next_step','open_question','state_set']
        .map(t=>'<option>'+t+'</option>').join('')
      + '</select></div>'
      + '<div class="row"><textarea id="etext" rows="3" style="width:min(100%,520px)" placeholder="one line"></textarea></div>'
      + '<div class="row"><input id="etags" placeholder="tags,comma,separated">'
      + '<button onclick="runAppend()">Append</button></div>';
    showText('Fill the form and append a memory event.');
  }
  function runAppend(){
    const body = { type: document.getElementById('etype').value,
      payload: document.getElementById('etext').value, tags: document.getElementById('etags').value };
    if (!body.payload.trim()) { showText('Nothing to append — type a line first.'); return; }
    run('appending', async () => {
      const res = await apiPost('/api/append', body);
      if (res.ok) { showOverview(); } else { showText(res); }
    });
  }

  // Plugins view auto-refreshes when stale: it asks the server to scan only if
  // the catalog is missing or >24h old, so just opening the view keeps it current
  // without forcing a heavy rescan. The Heavy "Plugins scan" button forces one.
  function showPlugins(){ controls.innerHTML=''; liveFn = null; run('refreshing plugins', async () => {
    const res = await apiPost('/api/plugins/autoscan', {});
    const note = res.scanned ? 'scanned just now' : ('catalog fresh ('+(Number(res.age_hours)||0).toFixed(1)+'h old)');
    showText(note + '\\n\\n' + JSON.stringify(res.result || res, null, 2));
  }); }
  function doExport(){ window.location = u('/api/export'); }

  function doAction(path, label, method){
    controls.innerHTML=''; liveFn = null;
    run(label+'…', async () => showText(method === 'GET' ? await apiGet(path) : await apiPost(path, {})));
  }
  function confirmAction(path, label){
    if(!confirm(label + ' — this can take a while. Continue?')) return;
    let body = {};
    if(path === '/api/embed' && confirm('Force past LIVE_LOCK? (only if the box is idle)')) body = { force: true };
    controls.innerHTML=''; liveFn = null;
    run(label+' running…', async () => showText(await apiPost(path, body)));
  }

  // land on the overview
  showOverview();
</script>
</body></html>""".replace("__TOKEN__", json.dumps(token))


def serve(port: int = 0, open_browser: bool = True) -> None:
    """Start the dashboard, open the browser, and serve until interrupted."""
    token = secrets.token_urlsafe(16)
    handler = make_handler(token)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    actual = server.server_address[1]
    url = f"http://127.0.0.1:{actual}/?token={token}"
    print(f"MIND HIVE dashboard: {url}", flush=True)
    print("Close this window or press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
