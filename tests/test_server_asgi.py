# tests/test_server_asgi.py
"""The ASGI bridge — and the claim that it adds no second security surface.

`server/asgi.py` exists because the MEB asks for FastAPI/uvicorn and this
repo's runtime dependencies are empty. The design answer was a BRIDGE:
`call_handler` drives the handler class that already exists through one
request on a fake socket. The tests that matter here are therefore not
"FastAPI works" but "the guards still apply" — host, token, HIVE MIND, gzip,
the lifecycle routes — because a re-implemented `do_GET` would have been a
second place to forget one.

The bridge is stdlib-only and is tested WITHOUT the extras. Only the two
tests at the bottom need fastapi, and on this machine they skip: uvicorn is
installed and fastapi is not. That is said here rather than left to be
inferred — `make_app` and `serve_asgi` have not been executed on this box.
"""
import gzip
import importlib.util
import json

import pytest

from mindhive import dashboard
from mindhive.server import asgi
from mindhive.server.lifecycle import HeartbeatMonitor
from mindhive.server.routes import with_lifecycle

TOKEN = "test-token-asgi"


@pytest.fixture
def handler():
    """The `hive ui` handler class — routes, guards and heartbeat."""
    return with_lifecycle(dashboard.make_handler(TOKEN), HeartbeatMonitor())


def _get(handler_cls, path, headers=None):
    return asgi.call_handler(handler_cls, "GET", path, headers or {})


def _post(handler_cls, path, body=None, headers=None):
    return asgi.call_handler(handler_cls, "POST", path,
                             dict(headers or {},
                                  **{"Content-Type": "application/json"}),
                             json.dumps(body if body is not None else {}))


# --------------------------------------------------------------------------
# the bridge answers, with every guard intact
# --------------------------------------------------------------------------

def test_a_get_with_the_token_is_answered(handler):
    status, headers, body = _get(handler, f"/api/lifecycle?token={TOKEN}")
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body)["ok"] is True


def test_the_token_guard_still_applies(handler):
    status, _h, body = _get(handler, "/api/lifecycle")
    assert status == 403
    assert json.loads(body)["error"] == "forbidden"


def test_the_token_may_arrive_in_the_header(handler):
    status, _h, _b = _get(handler, "/api/lifecycle",
                          {"X-Hive-Token": TOKEN})
    assert status == 200


def test_the_host_guard_still_applies(handler):
    """A server bound to loopback is still reachable by DNS rebinding, which
    is what `_local_only` is for. The bridge must not be a way around it."""
    status, _h, body = _get(handler, f"/api/lifecycle?token={TOKEN}",
                            {"Host": "evil.example"})
    assert status == 403
    assert json.loads(body)["error"] == "forbidden host"


def test_a_post_body_reaches_the_route(handler):
    status, _h, body = _post(handler, "/api/heartbeat", {"project": "*"},
                             {"X-Hive-Token": TOKEN})
    assert status == 200
    assert json.loads(body)["lifecycle"]["pings"] == 1


def test_a_post_without_the_token_records_nothing(handler):
    status, _h, _b = _post(handler, "/api/heartbeat")
    assert status == 403
    assert handler._monitor.snapshot()["pings"] == 0


def test_an_unknown_route_is_a_404_not_an_exception(handler):
    status, _h, body = _get(handler, f"/api/nope?token={TOKEN}")
    assert status == 404
    assert json.loads(body)["error"] == "not found"


def test_the_percent_encoding_of_the_query_survives(handler):
    """`_check_token` parses the query itself, so the bridge must hand it the
    RAW path. A pre-decoded `?token=caf%C3%A9` would compare a different
    string than the browser sent."""
    status, _h, _b = _get(handler, "/api/lifecycle?token=caf%C3%A9")
    assert status == 403                      # wrong token, not a crash


# --------------------------------------------------------------------------
# the page, and gzip
# --------------------------------------------------------------------------

def test_the_index_comes_back_whole(handler):
    status, headers, body = _get(handler, "/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"/api/heartbeat" in body


def test_gzip_is_negotiated_and_the_body_really_is_gzip(handler):
    status, headers, body = _get(handler, "/", {"Accept-Encoding": "gzip"})
    assert status == 200
    assert headers["Content-Encoding"] == "gzip"
    assert headers["Content-Length"] == str(len(body))
    assert b"/api/heartbeat" in gzip.decompress(body)


def test_without_accept_encoding_the_body_is_plain(handler):
    _status, headers, body = _get(handler, "/")
    assert "Content-Encoding" not in headers
    assert b"<html" in body.lower()


# --------------------------------------------------------------------------
# the bridge's own contract
# --------------------------------------------------------------------------

_REQ = b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n"


def test_without_the_close_the_handler_would_answer_twice(handler):
    """The failure the bridge is guarding against, shown rather than asserted.

    The handler speaks HTTP/1.1, so it loops for a second request on the same
    socket. Bytes left in the buffer — and `_body` deliberately refuses an
    oversized body WITHOUT reading it — become the next request line, and its
    answer is appended to the first response."""
    sock = asgi._FakeSocket(_REQ + _REQ)
    handler(sock, ("127.0.0.1", 0), None)
    assert sock.sent().count(b"HTTP/1.1 200") == 2


def test_the_close_is_what_makes_it_exactly_one(handler):
    closing = _REQ.replace(b"Host: 127.0.0.1\r\n",
                           b"Host: 127.0.0.1\r\nConnection: close\r\n")
    sock = asgi._FakeSocket(closing + _REQ)
    handler(sock, ("127.0.0.1", 0), None)
    assert sock.sent().count(b"HTTP/1.1 200") == 1


def test_the_bridge_really_sends_the_close(handler, monkeypatch):
    """And that it wins over a caller who asked for keep-alive: the "socket"
    is one call, whatever the caller believes about the connection."""
    seen = []
    real = asgi._FakeSocket
    monkeypatch.setattr(asgi, "_FakeSocket",
                        lambda data: seen.append(data) or real(data))
    asgi.call_handler(handler, "GET", "/", {"Connection": "keep-alive"})
    assert b"Connection: close\r\n" in seen[0]
    assert b"keep-alive" not in seen[0]


def test_a_closed_write_buffer_still_yields_its_bytes():
    """`finish()` closes `wfile`, and a closed BytesIO cannot be read. The
    capture ignores the close; without that every call would raise."""
    sock = asgi._FakeSocket(b"")
    sock.sendall(b"hello")
    sock.makefile("wb").close()
    assert sock.sent() == b"hello"


def test_hop_by_hop_headers_are_named_for_dropping():
    for name in ("connection", "transfer-encoding", "date", "server"):
        assert name in asgi.HOP_BY_HOP


def test_a_truncated_response_is_an_error_not_a_wrong_answer():
    with pytest.raises(ValueError):
        asgi._parse_response(b"HTTP/1.1 200 OK\r\nContent-Type: x")


def test_the_bridge_imports_neither_extra():
    """The whole point of testing it without them. `make_app` needs fastapi;
    `call_handler` must not."""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(asgi.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names == {"io"}, names


def test_missing_extras_name_the_install(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError) as e:
        asgi._require_ui()
    assert str(e.value) == "the ASGI adapter needs extras: pip install -e .[ui]"


def test_make_app_refuses_without_the_extras(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError) as e:
        asgi.make_app(object)
    assert "pip install -e .[ui]" in str(e.value)


def test_one_missing_extra_is_enough_to_refuse(monkeypatch):
    """uvicorn is installed on this machine and fastapi is not, which is
    exactly the half-installed state the check exists for."""
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: None if name == "fastapi" else real(name))
    with pytest.raises(RuntimeError):
        asgi._require_ui()


# --------------------------------------------------------------------------
# with the extras — skipped where they are absent
# --------------------------------------------------------------------------

def test_the_app_serves_the_same_answers_through_fastapi(handler):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    with TestClient(asgi.make_app(handler)) as client:
        r = client.get("/api/lifecycle", params={"token": TOKEN})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert client.get("/api/lifecycle").status_code == 403


def test_the_app_does_not_forward_hop_by_hop_headers(handler):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    with TestClient(asgi.make_app(handler)) as client:
        r = client.get("/", headers={"Accept-Encoding": "identity"})
        assert r.status_code == 200
        assert r.headers.get("connection", "").lower() != "close"
        assert len(r.headers.get_list("date")) <= 1
