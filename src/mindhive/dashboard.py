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
                if route == "/api/selftest":
                    from . import selftest as selftest_mod
                    code = selftest_mod.run()
                    return self._json({"ok": code == 0, "exit_code": code})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 200)
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
