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


def test_state_endpoint_returns_text():
    server, base, token = _start()
    try:
        status, body = _get(base, "/api/state", token)
        assert status == 200
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
