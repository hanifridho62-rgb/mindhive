# src/mindhive/dashboard.py
from __future__ import annotations

import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import paths, chain

# A fixed default port + a token persisted under the store home keep the
# dashboard URL identical on every launch (http://127.0.0.1:54461/?token=…).
DEFAULT_PORT = 54461


def _token_path():
    return paths.store_home() / "dashboard.token"


def load_or_create_token() -> str:
    """Return a stable dashboard token so the URL doesn't change between
    launches. Resolution order: MINDHIVE_DASHBOARD_TOKEN env → persisted file →
    freshly generated (and then persisted). Best-effort: a read/write failure
    falls back to an in-memory token rather than crashing the launch."""
    env = os.environ.get("MINDHIVE_DASHBOARD_TOKEN")
    if env:
        return env
    p = _token_path()
    try:
        if p.exists():
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except Exception:
        pass
    tok = secrets.token_urlsafe(16)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(tok, encoding="utf-8")
    except Exception:
        pass
    return tok
from . import guard
from . import recall as recall_mod
from . import graph as graph_mod
from . import graphview
from . import plugins as plugins_mod
from . import export as export_mod
from . import events, projector
from . import dedupe
from . import sync as sync_mod
from .briefing import briefing as build_briefing


# --- project / session / memory separation -------------------------------
# A "project" is a store namespace; a "session" is one append stream
# (session_id) within it; "memory" is the semantic subset of events (decisions,
# caveats, …) as opposed to deterministic session telemetry. Keeping the three
# distinct is what makes intent and context legible at a glance.

def _folder_for_project(proj: str) -> str:
    """The project's own folder (where its native MEMORY.md belongs), taken from
    the autosync registry. Falls back to the server's cwd only when the project
    isn't registered — so picking a project in the UI syncs THAT project's
    folder, not wherever the dashboard happens to be launched from."""
    from . import autosync
    for e in autosync.load_registry():
        if e.get("project") == proj and e.get("folder"):
            return e["folder"]
    return os.getcwd()


def _all_projects() -> list:
    """Names of every store namespace that has an events log."""
    home = paths.store_home()
    names = []
    if home.exists():
        for d in sorted(home.iterdir()):
            if d.is_dir() and (d / "events").is_dir():
                names.append(d.name)
    return names


def _project_stats(name: str) -> dict:
    evs = events.read_all(name)
    sessions, mem, auto = set(), 0, 0
    for ev in evs:
        sessions.add(ev.session_id)
        if ev.type in events.MEMORY_VIEW_TYPES:
            mem += 1
        else:
            auto += 1
    return {"name": name, "events": len(evs), "memory": mem, "auto": auto,
            "sessions": len(sessions), "last_ts": evs[-1].ts if evs else None}


def default_project() -> str:
    """The project the panel opens on: the one with the most events (so a fresh
    launch shows real data, not an empty cwd-derived namespace). Falls back to
    the cwd-detected project when the store is empty."""
    stats = [s for s in (_project_stats(n) for n in _all_projects()) if s["events"] > 0]
    if stats:
        stats.sort(key=lambda s: (s["events"], s["name"]), reverse=True)
        return stats[0]["name"]
    return paths.detect_project()


# --- HIVE MIND: one unified view across every project --------------------
# Selecting project="*" in the panel aggregates all namespaces into a single
# stream — every session, log, memory and bit of context unified. It's a
# read-only lens; mutating actions still need a concrete target project.
ALL_TOKENS = {"*", "__all__", "all"}
HIVEMIND_LABEL = "⨁ HIVE MIND (all projects)"
HIVEMIND_PROJECT = "__hivemind__"   # synthetic namespace for the unified graph cache


def is_all(proj) -> bool:
    return proj in ALL_TOKENS


def all_event_pairs() -> list:
    """(project_name, Event) across every namespace, ordered oldest→newest."""
    pairs = []
    for name in _all_projects():
        for ev in events.read_all(name):
            pairs.append((name, ev))
    pairs.sort(key=lambda pe: pe[1].ts or "")
    return pairs


def _overview_counts(pairs) -> dict:
    """Tally overview stats from an iterable of (session_key, Event). Shared by
    the single-project and HIVE MIND overviews so the counting lives in one
    place; the caller chooses the session key (a bare id, or (project, id) when
    unifying across projects)."""
    by_type, sessions, mem, auto, total, last_ts = {}, set(), 0, 0, 0, None
    for skey, ev in pairs:
        by_type[ev.type] = by_type.get(ev.type, 0) + 1
        sessions.add(skey)
        if ev.type in events.MEMORY_VIEW_TYPES:
            mem += 1
        else:
            auto += 1
        total += 1
        if ev.ts and (last_ts is None or ev.ts > last_ts):
            last_ts = ev.ts
    return {"events": total, "memory_events": mem, "auto_events": auto,
            "sessions": len(sessions), "by_type": by_type, "last_ts": last_ts}


def _safe_chain_ok(name: str, evs=None) -> bool:
    try:
        return bool(chain.verify(name, evs=evs).get("ok"))
    except Exception:
        return False


def make_handler(token: str):
    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        # --- helpers ---
        def _check_token(self) -> bool:
            qs = parse_qs(urlparse(self.path).query)
            supplied = self.headers.get("X-Hive-Token") or (qs.get("token") or [None])[0]
            if supplied is None:
                return False
            # constant-time compare to avoid leaking the token via timing
            return secrets.compare_digest(str(supplied), str(token))

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
            proj = (qs.get("project") or [None])[0] or default_project()
            am = is_all(proj)
            try:
                if route == "/api/projects":
                    stats = [_project_stats(n) for n in _all_projects()]
                    stats.sort(key=lambda s: (s["events"], s["name"]), reverse=True)
                    total = {"name": HIVEMIND_LABEL, "id": "*",
                             "events": sum(s["events"] for s in stats),
                             "memory": sum(s["memory"] for s in stats),
                             "auto": sum(s["auto"] for s in stats),
                             "sessions": sum(s["sessions"] for s in stats)}
                    return self._json({"projects": stats, "hivemind": total,
                                       "default": default_project(), "current": proj})
                if route == "/api/state":
                    if am:
                        return self._send(200, "HIVE MIND unifies every project — pick a single "
                                          "project to read its STATE.md.", "text/plain; charset=utf-8")
                    p = paths.project_dir(proj) / "STATE.md"
                    text = p.read_text(encoding="utf-8") if p.exists() else "(no STATE.md — run project)"
                    return self._send(200, text, "text/plain; charset=utf-8")
                if route == "/api/briefing":
                    if am:
                        return self._send(200, "HIVE MIND unifies every project — pick a single "
                                          "project for its briefing.", "text/plain; charset=utf-8")
                    bpath = build_briefing(proj)
                    return self._send(200, bpath.read_text(encoding="utf-8"), "text/plain; charset=utf-8")
                if route == "/api/verify":
                    if am:
                        per = {n: _safe_chain_ok(n) for n in _all_projects()}
                        return self._json({"ok": all(per.values()) if per else False, "per_project": per})
                    return self._json(chain.verify(proj))
                if route == "/api/overview":
                    if am:
                        names = _all_projects()
                        # read each project's log ONCE, then derive stats AND the
                        # chain check from the same events (no second full read)
                        evs_by_proj = {n: events.read_all(n) for n in names}
                        stats = _overview_counts(
                            ((name, ev.session_id), ev)
                            for name, evs in evs_by_proj.items() for ev in evs)
                        return self._json({
                            "project": HIVEMIND_LABEL, "projects": len(names), **stats,
                            "chain_ok": (all(_safe_chain_ok(n, evs_by_proj[n]) for n in names)
                                         if names else False),
                        })
                    evs = events.read_all(proj)
                    stats = _overview_counts((ev.session_id, ev) for ev in evs)
                    return self._json({
                        "project": proj, **stats,
                        # reuse the events we just read — no second full read
                        "chain_ok": bool(chain.verify(proj, evs=evs).get("ok")),
                    })
                if route == "/api/memory":
                    # the semantic subset only — the intent/context layer
                    session = (qs.get("session") or [None])[0]
                    limit = int((qs.get("limit") or ["200"])[0])
                    if am:
                        rows = [(n, e) for n, e in all_event_pairs()
                                if e.type in events.MEMORY_VIEW_TYPES]
                        if session:
                            rows = [(n, e) for n, e in rows if e.session_id == session]
                        rows = list(reversed(rows))[:limit]
                        return self._json(dedupe.collapse([
                            {"ts": e.ts, "type": e.type, "session": e.session_id, "project": n,
                             "text": e.payload.get("text", "") if isinstance(e.payload, dict) else str(e.payload),
                             "tags": e.tags}
                            for n, e in rows
                        ]))
                    evs = [e for e in events.read_all(proj) if e.type in events.MEMORY_VIEW_TYPES]
                    if session:
                        evs = [e for e in evs if e.session_id == session]
                    evs = list(reversed(evs))[:limit]      # newest first
                    # Collapse near-duplicate memories by meaning (same type +
                    # project), keeping the newest; display-time only, ledger
                    # untouched. similar_count rides along for the UI hint.
                    return self._json(dedupe.collapse([
                        {"ts": e.ts, "type": e.type, "session": e.session_id, "project": proj,
                         "text": e.payload.get("text", "") if isinstance(e.payload, dict) else str(e.payload),
                         "tags": e.tags}
                        for e in evs
                    ]))
                if route == "/api/sessions":
                    by: dict = {}
                    src = all_event_pairs() if am else [(proj, e) for e in events.read_all(proj)]
                    for name, e in src:
                        key = (name, e.session_id)
                        s = by.get(key)
                        if s is None:
                            s = by[key] = {"session_id": e.session_id, "project": name, "events": 0,
                                           "memory": 0, "first_ts": e.ts, "last_ts": e.ts}
                        s["events"] += 1
                        if e.type in events.MEMORY_VIEW_TYPES:
                            s["memory"] += 1
                        if e.ts and e.ts < s["first_ts"]:
                            s["first_ts"] = e.ts
                        if e.ts and e.ts > s["last_ts"]:
                            s["last_ts"] = e.ts
                    out = sorted(by.values(), key=lambda s: s["last_ts"] or "", reverse=True)
                    return self._json(out)
                if route == "/api/recall":
                    q = (qs.get("q") or [""])[0]
                    semantic = (qs.get("semantic") or ["0"])[0] in ("1", "true")
                    limit = int((qs.get("limit") or ["10"])[0])
                    force = (qs.get("force") or ["0"])[0] in ("1", "true")
                    if am:
                        hits = []
                        for n in _all_projects():
                            for h in recall_mod.recall(q, project=n, semantic=semantic, limit=limit, force=force):
                                h = dict(h)
                                h["project"] = n
                                hits.append(h)
                        return self._json(hits)
                    return self._json(list(recall_mod.recall(q, project=proj, semantic=semantic, limit=limit, force=force)))
                if route == "/api/graph":
                    if am:
                        evs = [e for _, e in all_event_pairs()]
                        html = graphview.render_html(graph_mod.build(project=HIVEMIND_PROJECT, _evs=evs))
                    else:
                        html = graphview.render_html(graph_mod.build(proj))
                    return self._send(200, html, "text/html; charset=utf-8")
                if route == "/api/plugins/recommend":
                    if am:
                        for n in _all_projects():
                            cat = plugins_mod.latest_catalog(n)
                            if cat:
                                return self._json(cat)
                        return self._json({})
                    return self._json(plugins_mod.latest_catalog(proj) or {})
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
                proj = body.get("project") or default_project()
                if is_all(proj):
                    if route == "/api/project":
                        res = {n: projector.project(n) for n in _all_projects()}
                        return self._json({"ok": True, "result": res})
                    # selftest + harvest are global (not tied to one project)
                    if route not in ("/api/selftest", "/api/harvest"):
                        return self._json({"ok": False, "error":
                            "HIVE MIND is a unified read-only view across all projects — "
                            "pick a specific project for this action."}, 200)
                if route == "/api/harvest":
                    # self-healing backfill: capture any session the hooks missed
                    res = run_catch_up()
                    return self._json({"ok": True, "result": res})
                if route == "/api/append":
                    etype = body.get("type")
                    text = body.get("payload", "")
                    tags = [t for t in (body.get("tags") or "").split(",") if t]
                    if not etype:
                        return self._json({"ok": False, "error": "type is required"}, 200)
                    ev = events.append_event(etype, {"text": text}, tags=tags, project=proj)
                    projector.project(proj)
                    return self._json({"ok": True, "type": ev.type, "ts": ev.ts, "project": proj})
                if route == "/api/project":
                    return self._json({"ok": True, "result": projector.project(proj)})
                if route == "/api/sync":
                    folder = _folder_for_project(proj)
                    path = sync_mod.sync(folder, project=proj)
                    return self._json({"ok": True, "path": str(path), "folder": folder})
                if route == "/api/embed":
                    force = bool(body.get("force", False))
                    guard.ensure_safe(force=force)  # raises when LIVE_LOCK active and not force
                    from . import vectors
                    return self._json({"ok": True, "result": vectors.embed(proj, force=force)})
                if route == "/api/plugins/scan":
                    result = plugins_mod.scan_and_store(top_n=int(body.get("top", 5)), project=proj)
                    return self._json({"ok": True, "result": result})
                if route == "/api/plugins/autoscan":
                    res = plugins_mod.ensure_fresh(
                        top_n=int(body.get("top", 5)),
                        max_age_hours=float(body.get("max_age_hours", 24)),
                        force=bool(body.get("force", False)),
                        project=proj,
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
  header #projwrap { font-size:12px; color:#9aa3b8; display:flex; align-items:center; gap:6px; }
  header #projwrap select { padding:3px 6px; }
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
  .hits .dup { color:#8a93a6; }
  h2.h { margin:2px 0 12px; font-size:16px; }
</style></head>
<body>
<header>
  <h1>MIND HIVE</h1>
  <span id="busy" class="dot"></span>
  <span id="status">ready</span>
  <label id="projwrap" title="Which project's memory to show">project
    <select id="proj" onchange="onProjChange()"></select></label>
  <span id="live" title="Real-time updates — click to pause/resume" onclick="toggleLive()"><span class="blip">&#9679;</span> live</span>
  <span id="synced"></span>
</header>
<main>
  <nav>
    <button onclick="showOverview()">Overview</button>
    <div class="group">Memory</div>
    <button onclick="showMemory()">Memory</button>
    <button onclick="showSessions()">Sessions</button>
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
    <button onclick="doAction('/api/harvest','Catch up (harvest missed sessions)')">Catch up</button>
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

  // --- project / session scoping ---
  // PROJECT is the selected store namespace ('' = server's default, the
  // richest one). SESSION optionally narrows the Memory view to one session.
  let PROJECT = localStorage.getItem('mh_project') || '';
  let SESSION = null;
  function escAttr(s){ return esc(s).replace(/"/g,'&quot;'); }
  function withProj(path){
    return PROJECT ? path + (path.includes('?')?'&':'?') + 'project=' + encodeURIComponent(PROJECT) : path;
  }
  async function apiGet(path){
    const r = await fetch(u(withProj(path)), { headers: { 'X-Hive-Token': TOKEN } });
    const ct = r.headers.get('content-type') || '';
    return ct.includes('json') ? r.json() : r.text();
  }
  async function apiPost(path, body){
    const b = Object.assign({}, body || {});
    if (PROJECT) b.project = PROJECT;
    const r = await fetch(u(withProj(path)), { method:'POST', headers:{ 'Content-Type':'application/json',
      'X-Hive-Token': TOKEN }, body: JSON.stringify(b) });
    return r.json();
  }
  async function loadProjects(){
    try {
      const data = await apiGet('/api/projects');
      const sel = document.getElementById('proj');
      const cur = PROJECT || data.current || data.default || '';
      let html = '';
      if (data.hivemind) {                       // unified view, always first
        html += '<option value="*"'+(cur==='*'?' selected':'')+'>'
              + esc(data.hivemind.name)+' ('+data.hivemind.events+')</option>';
      }
      html += (data.projects || []).map(p => '<option value="'+escAttr(p.name)+'"'
          +(p.name===cur?' selected':'')+'>'+esc(p.name)+' ('+p.events+')</option>').join('');
      sel.innerHTML = html || '<option value="">(no projects)</option>';
      PROJECT = cur;
    } catch(_) { /* keep server default */ }
  }
  function onProjChange(){
    PROJECT = document.getElementById('proj').value;
    localStorage.setItem('mh_project', PROJECT);
    SESSION = null;
    showOverview();
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
      + (o.projects != null ? '<div class="card"><div class="big">'+o.projects+'</div><div class="lbl">projects</div></div>' : '')
      + '<div class="card"><div class="big">'+o.events+'</div><div class="lbl">events</div></div>'
      + '<div class="card"><div class="big ok">'+(o.memory_events ?? '—')+'</div><div class="lbl">memory</div></div>'
      + '<div class="card"><div class="big">'+(o.auto_events ?? '—')+'</div><div class="lbl">session / auto</div></div>'
      + '<div class="card"><div class="big">'+(o.sessions ?? '—')+'</div><div class="lbl">sessions</div></div>'
      + '<div class="card"><div>'+chip+'</div><div class="lbl">hash chain</div></div>'
      + '<div class="card"><div class="big" style="font-size:15px">'+esc(o.last_ts || '—')+'</div><div class="lbl">last activity</div></div>'
      + '</div><div class="types">'+types+'</div>');
  }

  // --- Memory: the semantic layer (decisions/caveats/…) — intent & context ---
  // MEM_ITEMS caches the last fetch so search/sort repaint instantly without a
  // round-trip; MEM_QUERY / MEM_SORT live outside the view so the 2.5s live
  // refresh (which only rewrites #view) preserves what you typed and picked.
  let MEM_ITEMS = [];
  let MEM_QUERY = '';
  let MEM_SORT = 'new';            // 'new' = newest first, 'old' = oldest first
  async function renderMemory(){
    const path = '/api/memory' + (SESSION ? '?session='+encodeURIComponent(SESSION) : '');
    MEM_ITEMS = await apiGet(path);
    paintMemory();
  }
  function paintMemory(){
    if (!MEM_ITEMS.length) { showText(SESSION ? 'No memory in this session.'
      : 'No memory yet — append a decision / caveat / experiment_result / …'); return; }
    const q = MEM_QUERY.trim().toLowerCase();
    let items = MEM_ITEMS;
    if (q) items = items.filter(m => (
        (m.text||'')+' '+(m.type||'')+' '+(m.session||'')+' '+(m.project||'')
        +' '+((m.tags||[]).join(' '))).toLowerCase().includes(q));
    // ts is an ISO timestamp, so a lexical compare orders it chronologically.
    items = items.slice().sort((a,b) => MEM_SORT==='new'
      ? String(b.ts||'').localeCompare(String(a.ts||''))
      : String(a.ts||'').localeCompare(String(b.ts||'')));
    if (!items.length) { showHTML('<h2 class="h">No memory matches “'+esc(MEM_QUERY)+'”.</h2>'); return; }
    const showProj = (PROJECT === '*');
    const total = MEM_ITEMS.length;
    const head = q ? items.length+' of '+total+' memory item'+(total>1?'s':'')+' match “'+esc(MEM_QUERY)+'”'
                   : items.length+' memory item'+(items.length>1?'s':'');
    showHTML('<h2 class="h">'+head
      + (SESSION ? ' in '+esc(SESSION) : '')+(showProj?' across all projects':'')+'</h2><ul class="hits">'
      + items.map(m => '<li><div class="file">'
          + (showProj && m.project ? esc(m.project)+' · ' : '')
          + esc(m.type)+' · '+esc(m.ts)+' · '+esc(m.session)
          + (m.tags && m.tags.length ? ' · ['+esc(m.tags.join(', '))+']' : '')
          + (m.similar_count ? ' · <span class="dup">+'+m.similar_count+' similar</span>' : '')
          + '</div><div class="snip">'+esc(m.text)+'</div></li>').join('')
      + '</ul>');
  }
  function onMemSearch(v){ MEM_QUERY = v; paintMemory(); }
  function toggleMemSort(){
    MEM_SORT = MEM_SORT==='new' ? 'old' : 'new';
    document.getElementById('memsort').textContent = MEM_SORT==='new' ? 'newest first' : 'oldest first';
    paintMemory();
  }
  function showMemory(){
    controls.innerHTML =
      (SESSION ? '<div class="row">filtered to session <b>'+esc(SESSION)+'</b> '
        + '<button onclick="clearSession()">show all</button></div>' : '')
      + '<div class="row"><input id="memq" placeholder="search memory…" style="min-width:260px" '
        + 'value="'+escAttr(MEM_QUERY)+'" oninput="onMemSearch(this.value)">'
      + '<button id="memsort" onclick="toggleMemSort()">'
        + (MEM_SORT==='new' ? 'newest first' : 'oldest first')+'</button></div>';
    liveFn = renderMemory; run('loading memory', renderMemory);
  }
  function clearSession(){ SESSION = null; showMemory(); }

  // --- Sessions: the episode layer; click one to scope Memory to it ---
  async function renderSessions(){
    const items = await apiGet('/api/sessions');
    if (!items.length) { showText('No sessions yet.'); return; }
    showHTML('<h2 class="h">'+items.length+' session'+(items.length>1?'s':'')+'</h2><ul class="hits">'
      + items.map(s => '<li class="sessrow" data-sid="'+escAttr(s.session_id)+'" style="cursor:pointer">'
          + '<div class="file">'+(PROJECT==='*' && s.project ? esc(s.project)+' · ' : '')+esc(s.session_id)+'</div>'
          + '<div class="snip">'+s.events+' events · '+s.memory+' memory · '
          + esc(s.first_ts||'')+' → '+esc(s.last_ts||'')+'</div></li>').join('')
      + '</ul>');
    view.querySelectorAll('.sessrow').forEach(el => el.onclick = () => openSession(el.dataset.sid));
  }
  function showSessions(){ controls.innerHTML=''; liveFn = renderSessions; run('loading sessions', renderSessions); }
  function openSession(sid){ SESSION = sid; showMemory(); }
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

  // populate the project picker, then land on the overview
  (async () => { await loadProjects(); showOverview(); })();
</script>
</body></html>""".replace("__TOKEN__", json.dumps(token))


def run_catch_up() -> dict:
    """Smart-harvest every transcript dir into its project and refresh the
    touched projections. Used by the manual endpoint and the background loop."""
    from . import harvest as harvest_mod
    res = harvest_mod.catch_up(skip_captured=True)
    for proj in res.get("projects", {}):
        try:
            projector.project(proj)
        except Exception:
            pass
    return res


def _transcripts_fingerprint(root=None) -> float:
    """Cheap activity signal: the newest mtime across all transcript .jsonl
    files. Advances whenever any session file is created or appended, so the
    harvest loop can skip work entirely when nothing has changed. Stat-only —
    no file contents are read."""
    if root is None:
        from . import harvest as harvest_mod
        root = harvest_mod.claude_projects_root()
    newest = 0.0
    try:
        for d in os.scandir(root):
            if not d.is_dir():
                continue
            try:
                for f in os.scandir(d.path):
                    if f.name.endswith(".jsonl"):
                        m = f.stat().st_mtime
                        if m > newest:
                            newest = m
            except OSError:
                continue
    except OSError:
        pass
    return newest


def _auto_harvest_loop(stop_event) -> None:
    """Low-latency background capture: poll a cheap mtime fingerprint of the
    transcripts and harvest ONLY when something actually changed. Idle ticks cost
    just stat() calls, so the poll can be fast without wasting CPU/IO; a freshly
    typed prompt is captured within one poll interval.

    Off via MINDHIVE_AUTOHARVEST=0; poll seconds via MINDHIVE_AUTOHARVEST_SECS
    (default 3, floor 1). The cross-process harvest lock keeps this safe to run
    alongside the scheduled autosync task."""
    if os.environ.get("MINDHIVE_AUTOHARVEST", "1").lower() in ("0", "false", "off", "no"):
        return
    try:
        poll = max(1, int(os.environ.get("MINDHIVE_AUTOHARVEST_SECS", "3")))
    except Exception:
        poll = 3
    # Catch up once on startup so an already-running session is captured promptly.
    try:
        run_catch_up()
    except Exception:
        pass
    last_fp = _transcripts_fingerprint()
    while not stop_event.wait(poll):
        try:
            fp = _transcripts_fingerprint()
            if fp == last_fp:
                continue            # nothing changed — skip the harvest entirely
            last_fp = fp
            res = run_catch_up()
            if res.get("total_new"):
                print(f"MIND HIVE auto-harvest: +{res['total_new']} events "
                      f"across {len(res['projects'])} project(s)", flush=True)
        except Exception:
            pass   # never let the healer disturb the server


def serve(port: int | None = None, open_browser: bool = True,
          token: str | None = None) -> None:
    """Start the dashboard on a fixed port with a persisted token so the URL is
    stable across launches, open the browser, and serve until interrupted.

    ``port=None`` uses the fixed DEFAULT_PORT; pass ``port=0`` for an ephemeral
    port. If the fixed port is already bound (the dashboard is likely already
    running), this reopens that same stable URL and returns instead of crashing.

    A background loop also auto-harvests sessions the live hooks miss, so the
    store self-heals even where hooks never fire (disable: MINDHIVE_AUTOHARVEST=0).
    """
    if port is None:
        port = DEFAULT_PORT
    token = token or load_or_create_token()
    handler = make_handler(token)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as e:
        url = f"http://127.0.0.1:{port}/?token={token}"
        print(f"MIND HIVE dashboard could not bind port {port}: {e}", flush=True)
        print(f"It may already be running — opening {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    actual = server.server_address[1]
    url = f"http://127.0.0.1:{actual}/?token={token}"
    print(f"MIND HIVE dashboard: {url}", flush=True)
    print("Close this window or press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    stop_event = threading.Event()
    threading.Thread(target=_auto_harvest_loop, args=(stop_event,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
