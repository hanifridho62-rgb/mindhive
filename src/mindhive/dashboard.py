# src/mindhive/dashboard.py
from __future__ import annotations

import gzip
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import paths
from . import projector
from . import jobs
from . import taskview
from .dashboard_page import render_page
# The route halves, mixed into the handler by `make_handler` below, and the
# job module the background harvest loop reaches through.
from . import dashboard_jobs as jobs_mod
from .dashboard_get import GetRoutes
from .dashboard_post import PostRoutes
# Re-exported, not re-implemented. Callers read these off THIS module and
# must get the same OBJECTS the handlers use: `tests/test_dashboard_locks`
# acquires `dashboard._HARVEST_LOCK` and empties every `*_MEMO` it finds in
# `vars(dashboard)`, and `do_POST` right here calls `default_project` /
# `is_all` / `_all_projects`. The rest is this module's long-standing public
# surface, kept so that splitting an over-long file is not also an API change.
#
# `F401` on these blocks only: a re-export is unused *in this file* by
# definition, which is the whole point of it.
from .dashboard_data import (ALL_TOKENS, HIVEMIND_LABEL,  # noqa: F401
                             HIVEMIND_PROJECT, LOW_SAMPLE_FLOOR,
                             _OVERVIEW_MEMO, _OVERVIEW_MEMO_MAX,
                             _add_tallies, _all_projects, _efficiency,
                             _empty_tallies, _folder_for_project, _iso_utc,
                             _overview_counts, _overview_fingerprint,
                             _project_stats, _project_stats_uncached,
                             _safe_chain_ok, all_event_pairs, default_project,
                             event_pairs, injection_status, is_all,
                             overview_stats)
from .dashboard_jobs import run_catch_up  # noqa: F401
from .dashboard_locks import (REFLECT_BATCH_LIMIT,  # noqa: F401
                              _EMBED_LOCK, _HARVEST_LOCK, _INVOKE_LOCK,
                              _PLUGINS_LOCK, _PROJECT_LOCK, _REFLECT_LOCK,
                              _SELFTEST_LOCK, _SYNC_LOCK, _VERIFY_LOCK,
                              _one_at_a_time)
from .dashboard_memo import (_CATS_MEMO, _MEMO_MAX,  # noqa: F401
                             _SESSIONS_MEMO, _STATS_MEMO, _fingerprinted,
                             _ledger_fingerprint, _memo_get, _memo_put,
                             _sessions_cached, _with_online)

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


def make_handler(token: str):
    class DashboardHandler(GetRoutes, PostRoutes, BaseHTTPRequestHandler):
        # The token, as a class attribute rather than a closure cell. Two
        # GET handlers render an iframe page with it and now live in
        # `dashboard_get`, which cannot see this function's locals; nothing
        # else in either route module ever needed it.
        _token = token

        # HTTP/1.1, so the browser reuses one connection for the whole page
        # instead of a TCP handshake per request — this panel issues dozens of
        # small JSON requests per poll cycle and two pollers run on timers.
        #
        # Keep-alive brings two obligations, and BOTH are prerequisites rather
        # than nice-to-haves; this line is deliberately the LAST of the three to
        # ship. (1) Every response must carry an exact Content-Length, or the
        # client cannot find where one body ends and the next begins — both
        # response paths here set it, and `send_error` sets its own. (2) A
        # connection may only be REUSED when the last request was read to its end
        # and answered exactly once; see `_body` and `_fail`. Without those,
        # keep-alive does not produce an error — it produces answers attached to
        # the wrong questions.
        protocol_version = "HTTP/1.1"

        # And keep-alive costs a thread per idle connection: the base class's
        # `timeout` is None, so `rfile.readline()` on a connection the browser is
        # holding open blocks forever and the worker never returns to the pool. A
        # timeout turns "pinned until the tab closes" into "released after 30s of
        # silence"; the client just reconnects, which is what keep-alive already
        # assumes can happen.
        timeout = 30

        # Whether bytes of a response for the request in flight are already on
        # the wire. Class-level default so it reads correctly even if a failure
        # happens before `handle_one_request` runs.
        _responded = False

        def log_message(self, *args):  # keep the console quiet
            pass

        def handle_one_request(self):
            # Keep-alive reuses ONE handler instance for every request on the
            # connection, so this is per-REQUEST state and has to be reset here.
            # Setting it in __init__ would make the flag mean "this connection
            # has ever answered", which would silence every error response after
            # the first.
            self._responded = False
            return BaseHTTPRequestHandler.handle_one_request(self)

        def send_response(self, *args, **kwargs):
            # The one place every response passes through — this class's two
            # writers and the base class's `send_error` alike — so it is the only
            # place that can reliably know a reply has started.
            self._responded = True
            return BaseHTTPRequestHandler.send_response(self, *args, **kwargs)

        def _fail(self, exc):
            """The single error exit for a route that raised.

            A second full response for one request is not a duplicate: under
            keep-alive it is FRAMED as the next response, so every later
            request/response pair on that connection is answered off by one. Once
            headers are out, the honest move is to stop talking and let the
            connection close — the client sees one truncated response and
            reconnects, instead of parsing our error JSON as the body of
            something it asked for later."""
            if self._responded:
                self.close_connection = True
                return None
            return self._json({"ok": False, "error": str(exc)}, 500)

        # --- helpers ---
        def _check_token(self) -> bool:
            qs = parse_qs(urlparse(self.path).query)
            supplied = self.headers.get("X-Hive-Token") or (qs.get("token") or [None])[0]
            if supplied is None:
                return False
            # constant-time compare to avoid leaking the token via timing
            return secrets.compare_digest(str(supplied), str(token))

        # Compress bodies above one MTU when the client accepts gzip — the
        # page is ~72KB (→ ~22KB) and the Improvements/graph JSON payloads
        # shrink similarly; below the threshold the header overhead wins.
        GZIP_MIN_BYTES = 1400

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            accepts = "gzip" in (self.headers.get("Accept-Encoding") or "")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if accepts and len(data) >= self.GZIP_MIN_BYTES:
                data = gzip.compress(data, 6)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

        # Bodies are tiny JSON control messages; cap the read so an
        # unauthenticated caller (do_POST drains the body before the token
        # check to avoid a WinError 10053 reset) can't force an arbitrarily
        # large allocation or hold a worker thread on a slow/oversized body.
        MAX_BODY_BYTES = 1 << 20  # 1 MiB

        def _body(self) -> dict:
            """The request body as JSON, or ValueError with the connection marked
            for closing.

            **A body we refuse to read is a connection we stop reusing**, and
            that is the whole of the rule — one answer for all three refusals
            below.

            Keep-alive is what makes this load-bearing. The refused bytes are
            still sitting in the socket, so the server's next `readline()` would
            read the middle of that body as a request line: the refusal would
            stop being a refusal of ONE request and desynchronise every request
            after it on the connection, answering calls the client never made.
            Reading the refused body would also restore the framing, and the
            first cut of this did exactly that — but it is an UNTRUSTED read. A
            caller that declares a megabyte and then sends nothing holds a worker
            thread until the handler timeout, which is precisely the cost
            MAX_BODY_BYTES exists to refuse; no byte cap fixes it, because the
            bytes never arrive. Closing needs no cap, no chunk loop, and no trust
            in the caller.

            What closing costs: with unread bytes still inbound, Windows aborts
            the connection (WinError 10053) instead of delivering our 500, so an
            oversized caller may see a reset rather than the reason. These bodies
            are tiny JSON control messages by construction — the callers that
            reach these branches are broken or probing, a reset answers both, and
            the alternative is a thread any local process can pin on demand.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                # A length we cannot parse is a body whose END we cannot find.
                self.close_connection = True
                raise ValueError("invalid Content-Length")
            if length < 0:
                self.close_connection = True
                raise ValueError("invalid Content-Length")
            if length > self.MAX_BODY_BYTES:
                self.close_connection = True
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        # --- routing ---
        def do_GET(self):
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/":
                return self._send(200,
                                  render_page(token,
                                              tasks_css=taskview.CARDS_CSS,
                                              tasks_js=taskview.CARDS_JS),
                                  "text/html; charset=utf-8")
            if not self._check_token():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            handler = self._GET_ROUTES.get(route)
            if handler is None:
                return self._json({"ok": False, "error": "not found"}, 404)
            qs = parse_qs(parsed.query)
            proj = (qs.get("project") or [None])[0] or default_project()
            am = is_all(proj)
            try:
                return handler(self, proj, am, qs)
            except Exception as e:  # never crash the server
                # Unexpected failure -> 500 so monitoring and clients can tell a
                # server fault apart from a normal empty/negative result. Expected
                # domain rejections return their own ok:False, 200 inside handlers.
                # Via `_fail`, because a route that raised AFTER writing (a
                # streaming export, a handler that sends then post-processes) must
                # not have a second response framed behind its first.
                return self._fail(e)

        def do_POST(self):
            route = urlparse(self.path).path
            # Drain the request body BEFORE any early response: replying 403
            # while the client is still sending aborts the connection on
            # Windows (WinError 10053) instead of delivering the status.
            try:
                body = self._body()
            except Exception as e:
                return self._fail(e)
            if not self._check_token():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            try:
                proj = body.get("project") or default_project()
                if is_all(proj):
                    if route == "/api/project":
                        # Takes the SAME lock as _post_project. This branch
                        # answers and returns before _POST_ROUTES dispatch, so
                        # it never passes through the decorated handler —
                        # HIVE MIND was the one remaining door into
                        # projector.project() that T8's decorator could not see,
                        # and it is the widest one (every namespace, not one).
                        if not _PROJECT_LOCK.acquire(blocking=False):
                            return self._json({"ok": False, "error":
                                "a projection rebuild is already running"}, 200)
                        try:
                            res = {n: projector.project(n)
                                   for n in _all_projects()}
                        finally:
                            _PROJECT_LOCK.release()
                        return self._json({"ok": True, "result": res})
                    # selftest + harvest + plugin scans + auth verification are
                    # global (machine-wide, not tied to one project); every other
                    # mutating action needs a concrete target. The plugin
                    # handlers substitute the default project for storage when
                    # in all-mode.
                    if route not in ("/api/selftest", "/api/harvest",
                                     "/api/plugins/scan", "/api/plugins/autoscan",
                                     "/api/workspace/auth_verify"):
                        return self._json({"ok": False, "error":
                            "HIVE MIND is a unified read-only view across all projects — "
                            "pick a specific project for this action."}, 200)
                handler = self._POST_ROUTES.get(route)
                if handler is None:
                    return self._json({"ok": False, "error": "not found"}, 404)
                return handler(self, proj, body)
            except Exception as e:
                # Unexpected failure -> 500 (see do_GET). The HIVE MIND read-only
                # guard and the "type is required" check return ok:False, 200
                # explicitly as expected domain rejections.
                return self._fail(e)

    return DashboardHandler


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
        jobs_mod.run_catch_up()
    except Exception:
        pass
    last_fp = _transcripts_fingerprint()
    while not stop_event.wait(poll):
        try:
            fp = _transcripts_fingerprint()
            if fp == last_fp:
                continue            # nothing changed — skip the harvest entirely
            last_fp = fp
            res = jobs_mod.run_catch_up()
            if res.get("total_new"):
                print(f"MIND HIVE auto-harvest: +{res['total_new']} events "
                      f"across {len(res['projects'])} project(s)", flush=True)
        except Exception:
            pass   # never let the healer disturb the server


def stable_url(port: int | None = None, token: str | None = None) -> str:
    """The dashboard's stable URL for the given (or default) port and token.
    Kept in one place so the launcher, the server, and any caller all report the
    identical http://127.0.0.1:<port>/?token=<tok> that a browser will open."""
    port = DEFAULT_PORT if port is None else port
    token = token or load_or_create_token()
    return f"http://127.0.0.1:{port}/?token={token}"


def _instance_running(port: int | None, host: str = "127.0.0.1",
                      timeout: float = 0.3) -> bool:
    """True when a dashboard is already listening on host:port.

    Deliberately a client *connect* probe, not a bind-test: on Windows
    SO_REUSEADDR lets a second bind to an in-use port SUCCEED (hijack
    semantics), so a bind-based "is it free?" check wrongly reports free and a
    duplicate pid gets spawned. Connecting is unambiguous — it succeeds only
    when something is actually accepting there. Port 0 (ephemeral) is always a
    fresh server, so it is never "already running"."""
    if not port:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def launch_background(port: int | None = None) -> dict:
    """Spawn `hive dashboard` as a *detached* background process so a caller
    (e.g. the autonomous loop) can bring the panel up without blocking on
    serve()'s serve_forever(). The child outlives this process and — thanks to
    serve()'s fixed-port reuse — a second launch just reopens the same stable URL
    instead of erroring.

    Best-effort by contract: any spawn failure is swallowed and reported in the
    return value rather than raised, so a dashboard problem can never abort the
    caller. Returns {"launched": True, "url": ..., "pid": ...} on success or
    {"launched": False, "reason": ...} when the spawn could not start.
    """
    url = stable_url(port)
    # Single-instance guard: never spawn a second pid when one is already
    # serving. The loop calls this on every run, so without the check repeated
    # runs would pile up detached dashboards.
    if _instance_running(DEFAULT_PORT if port is None else port):
        return {"launched": False, "already_running": True, "url": url,
                "reason": "already running"}
    argv = [sys.executable, "-m", "mindhive", "dashboard"]
    if port is not None:
        argv += ["--port", str(port)]
    # Detach so the child survives this process and doesn't hold its console.
    kwargs: dict = {"stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except Exception as e:   # never let a launch failure disturb the caller
        return {"launched": False, "reason": str(e)}
    return {"launched": True, "url": url, "pid": proc.pid}


def open_panel(port: int | None = None) -> str:
    """Bring the dashboard to the foreground for the `mind hive` shortcut.

    Launch it detached if nothing is running; if one is already up, open its
    stable URL in the browser so the user lands straight on the live panel —
    seamless, and never blocking the terminal. Returns a one-line status for the
    shell to print. On a fresh launch the detached child opens its own browser,
    so we don't double-open here."""
    res = launch_background(port)
    url = res.get("url")
    if res.get("launched"):
        return f"MIND HIVE dashboard: {url}"
    if res.get("already_running"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return f"MIND HIVE dashboard already running — opening {url}"
    return f"dashboard: not launched ({res.get('reason')})"


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
    # Single-instance guard (primary): if a dashboard is already serving this
    # fixed port, reopen its URL instead of starting a second pid. A connect
    # probe is reliable on Windows, where SO_REUSEADDR would otherwise let the
    # bind below succeed and leave a duplicate server. The OSError path stays as
    # a fallback for the rare probe-then-bind race. port=0 skips the probe.
    if _instance_running(port):
        url = f"http://127.0.0.1:{port}/?token={token}"
        print(f"MIND HIVE dashboard already running — opening {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
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
    # The boot seal (T14). Jobs run on daemon threads, so anything still running
    # when this process ended died with it and left a `job_started` row with no
    # close — which, unsealed, reads as still-running forever. Close them now,
    # while nothing of this process's own is in flight yet.
    #
    # Best-effort: an unreadable ledger must not stop the dashboard from serving,
    # and this runs BEFORE serve_forever precisely so the seal cannot race a job
    # the user starts. Read `jobs`' module docstring for the single-writer
    # assumption; the scope is one namespace for the same reason.
    try:
        sealed = jobs.seal_orphans(default_project())
        if sealed:
            print(f"sealed {sealed} job(s) abandoned by a previous run",
                  flush=True)
    except Exception as e:                      # noqa: BLE001 — see comment
        print(f"job seal skipped: {type(e).__name__}: {e}", flush=True)
    stop_event = threading.Event()
    threading.Thread(target=_auto_harvest_loop, args=(stop_event,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
