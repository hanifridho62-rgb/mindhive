# SPDX-License-Identifier: Apache-2.0
"""The optional ASGI front end (blueprint P1k, K11) — a BRIDGE, not a port.

The MEB asks for FastAPI/uvicorn. This repo's runtime `dependencies` are
empty and stay empty, so the extras live behind `pip install -e .[ui]` and
this module is the only thing that imports them.

**It re-implements nothing.** `call_handler` drives the existing
`BaseHTTPRequestHandler` subclass -- the one `dashboard.make_handler` mints,
lifecycle routes and all -- through one request on a fake socket, and hands
back what it wrote. That is what keeps the two front ends from drifting: the
local-host guard, the constant-time token compare, gzip, the body cap, the
HIVE MIND rules and every route are the SAME code under both, because there
is only one copy of each. A second implementation of `do_GET` would be a
second place for a security guard to be forgotten.

The cost is honest and small: one request per call, no streaming, and
hop-by-hop headers dropped on the way out (the ASGI server frames its own
connection). Everything above the socket is untouched.

The bridge itself is stdlib-only and is TESTED WITHOUT THE EXTRAS
(`tests/test_server_asgi.py`); only `make_app` and `serve_asgi` need them.
"""
import io

__all__ = ["call_handler", "make_app", "serve_asgi"]

# Headers that describe ONE hop and must not be forwarded: the stdlib handler
# wrote them about its fake socket, and the ASGI server writes its own about
# the real one. `date` and `server` are here for the same reason -- forwarding
# them would put two of each on the wire.
HOP_BY_HOP = frozenset({"connection", "keep-alive", "transfer-encoding",
                        "upgrade", "proxy-authenticate", "proxy-authorization",
                        "te", "trailer", "date", "server"})

_EXTRAS_MESSAGE = "the ASGI adapter needs extras: pip install -e .[ui]"


def _require_ui() -> None:
    """Raise unless both extras are importable.

    One message for both, naming the single line that fixes either: `.[ui]`
    installs fastapi and uvicorn together, so which of them is missing is not
    a distinction the operator can act on differently."""
    import importlib.util
    for name in ("fastapi", "uvicorn"):
        if importlib.util.find_spec(name) is None:
            raise RuntimeError(_EXTRAS_MESSAGE)


# --------------------------------------------------------------------------
# the bridge
# --------------------------------------------------------------------------

class _Capture(io.BytesIO):
    """A BytesIO that survives `finish()`.

    `StreamRequestHandler.finish` closes `wfile`, and a closed BytesIO cannot
    be read -- `getvalue()` raises. Ignoring the close is the whole trick."""

    def close(self) -> None:
        pass


class _FakeSocket:
    """Enough socket for `StreamRequestHandler.setup`/`finish`.

    `setup` calls `settimeout` (the handler sets `timeout = 30`), then
    `makefile("rb")` for the request and -- because `wbufsize` is 0 --
    wraps this object in a `_SocketWriter` that calls `sendall`. Both write
    paths end up in the same capture buffer, so a future stdlib that chose
    `makefile("wb")` instead would still work."""

    def __init__(self, data: bytes):
        self._in = io.BytesIO(data)
        self._out = _Capture()

    def makefile(self, mode="rb", bufsize=-1):
        return self._out if "w" in mode else self._in

    def sendall(self, data) -> None:
        self._out.write(data)

    def settimeout(self, value) -> None:
        pass

    def setsockopt(self, *args) -> None:
        pass

    def close(self) -> None:
        pass

    def sent(self) -> bytes:
        return self._out.getvalue()


def _parse_response(raw: bytes):
    """`(status, headers, body)` from the bytes the handler wrote.

    Repeated header names collapse, which is exact for this surface: the
    dashboard sends no `Set-Cookie` and no other repeatable header. If one is
    ever added, this is where it would need a multi-dict."""
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        raise ValueError("the handler wrote no complete response")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError(f"unparseable status line: {lines[0]!r}")
    headers = {}
    for line in lines[1:]:
        name, sep2, value = line.partition(":")
        if sep2:
            headers[name.strip()] = value.strip()
    return int(parts[1]), headers, body


def call_handler(handler_cls, method: str, path: str, headers=None, body=b""):
    """Run ONE request through *handler_cls* and return `(status, headers, body)`.

    *path* carries its query string exactly as it arrived, percent-encoding
    intact: `_check_token` parses it itself, and `?token=caf%C3%A9` is a thing
    a caller can send.

    `Connection: close` is forced. The handler speaks HTTP/1.1 and would
    otherwise loop for a second request on the same "socket" -- which is
    harmless when the buffer is empty and wrong when it is not: `_body`
    deliberately refuses an oversized body WITHOUT reading it, and those
    unread bytes would then be parsed as the next request line and answered
    with a second response appended to the first."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    sent = {k: v for k, v in dict(headers or {}).items()
            if k.lower() not in ("connection", "content-length")}
    sent.setdefault("Host", "127.0.0.1")
    sent["Content-Length"] = str(len(body))
    sent["Connection"] = "close"
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(
        f"{name}: {value}\r\n" for name, value in sent.items()) + "\r\n"
    sock = _FakeSocket(head.encode("latin-1") + body)
    # Constructing the handler IS the request: `BaseRequestHandler.__init__`
    # runs setup / handle / finish before it returns.
    handler_cls(sock, ("127.0.0.1", 0), None)
    return _parse_response(sock.sent())


# --------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------

def make_app(handler_cls):
    """A FastAPI app whose every route is *handler_cls*.

    One catch-all rather than a route per endpoint: the route table already
    exists on the handler, and restating it here would create a second list
    to keep in step. `run_in_threadpool` because the handler is blocking
    stdlib code -- calling it on the event loop would stall every other
    request for the duration of one."""
    _require_ui()
    from fastapi import FastAPI, Request, Response
    from fastapi.concurrency import run_in_threadpool

    app = FastAPI(title="MIND HIVE", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.api_route("/{_path:path}", methods=["GET", "POST"])
    async def _bridge(request: Request, _path: str) -> Response:
        payload = await request.body()
        scope = request.scope
        raw = scope.get("raw_path")
        target = raw.decode("latin-1") if raw else request.url.path
        query = scope.get("query_string") or b""
        if query:
            target += "?" + query.decode("latin-1")
        status, out_headers, out_body = await run_in_threadpool(
            call_handler, handler_cls, request.method, target,
            dict(request.headers), payload)
        return Response(content=out_body, status_code=status,
                        headers={k: v for k, v in out_headers.items()
                                 if k.lower() not in HOP_BY_HOP})

    return app


def serve_asgi(*, port=None, monitor=None, token=None,
               open_browser: bool = True) -> None:
    """Serve the panel through uvicorn, with the same lifecycle as `serve`.

    The single-instance guard is honoured here for the same reason it is in
    `dashboard.serve`, and it matters MORE here: this front end binds through
    uvicorn, so a second server on 54461 would replace the user's live panel
    rather than fail to start. The check is a connect probe, before any bind.
    """
    _require_ui()
    import threading
    import webbrowser

    import uvicorn

    from .. import dashboard
    from .lifecycle import watchdog
    from .routes import with_lifecycle

    if port is None:
        port = dashboard.DEFAULT_PORT
    token = token or dashboard.load_or_create_token()
    url = f"http://127.0.0.1:{port}/?token={token}"
    if dashboard._instance_running(port):
        print(f"MIND HIVE dashboard already running — opening {url}",
              flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    handler = dashboard.make_handler(token)
    if monitor is not None:
        handler = with_lifecycle(handler, monitor)
    server = uvicorn.Server(uvicorn.Config(make_app(handler), host="127.0.0.1",
                                           port=port, log_level="warning"))
    print(f"MIND HIVE dashboard (asgi): {url}", flush=True)
    print("Close the tab, or press Ctrl+C, to stop.", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    stop_event = threading.Event()
    if monitor is not None:
        # `should_exit` rather than a method call: uvicorn's loop polls the
        # flag and unwinds its own shutdown, and setting it is the only way
        # to stop it from another thread without touching its event loop.
        threading.Thread(
            target=watchdog, args=(monitor, None, stop_event),
            kwargs={"shutdown": lambda: setattr(server, "should_exit", True)},
            name="hive-ui-watchdog", daemon=True).start()
    try:
        server.run()
    finally:
        stop_event.set()
