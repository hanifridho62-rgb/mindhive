# tests/test_dashboard.py
import json
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    """Point every dashboard test at a throwaway store so the live ~/.mindhive
    ledger is never touched. paths.* read these env vars at call time, and the
    handler runs in-process, so the server threads see the same isolation."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


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


def test_graph_endpoint_returns_html():
    server, base, token = _start()
    try:
        status, body = _get(base, "/api/graph", token)
        assert status == 200
        assert "<" in body
    finally:
        server.shutdown()


def test_overview_endpoint_returns_stats():
    server, base, token = _start()
    try:
        status, body = _get(base, "/api/overview", token)
        assert status == 200
        obj = json.loads(body)
        for key in ("project", "events", "by_type", "last_ts", "chain_ok"):
            assert key in obj, f"overview missing {key}"
        assert isinstance(obj["events"], int)
        assert isinstance(obj["by_type"], dict)
        assert isinstance(obj["chain_ok"], bool)
    finally:
        server.shutdown()


def test_overview_counts_an_appended_event():
    server, base, token = _start()
    try:
        before = json.loads(_get(base, "/api/overview", token)[1])["events"]
        _post(base, "/api/append", {"type": "decision", "payload": "ov note"}, token)
        after = json.loads(_get(base, "/api/overview", token)[1])
        assert after["events"] == before + 1
        assert after["by_type"].get("decision", 0) >= 1
        assert after["last_ts"]
    finally:
        server.shutdown()


def test_plugins_recommend_returns_json():
    server, base, token = _start()
    try:
        status, body = _get(base, "/api/plugins/recommend", token)
        assert status == 200
        obj = json.loads(body)
        assert isinstance(obj, dict)
    finally:
        server.shutdown()


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


def test_page_has_feature_controls():
    server, base, token = _start()
    try:
        status, body = _get(base, "/")
        assert status == 200
        for needle in ["State", "Recall", "Graph", "Append", "Plugins", "Export"]:
            assert needle in body, f"missing control: {needle}"
        assert "fetch(" in body
    finally:
        server.shutdown()
