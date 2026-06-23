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


def test_plugins_autoscan_is_stale_gated():
    server, base, token = _start()
    try:
        # empty store -> first autoscan must actually scan
        status, obj = _post(base, "/api/plugins/autoscan", {}, token)
        assert status == 200 and obj["ok"] is True
        assert obj["scanned"] is True
        assert isinstance(obj["result"], dict)
        # second autoscan finds a fresh snapshot -> skips the scan
        status, obj2 = _post(base, "/api/plugins/autoscan", {}, token)
        assert obj2["scanned"] is False
        # force overrides the freshness gate
        status, obj3 = _post(base, "/api/plugins/autoscan", {"force": True}, token)
        assert obj3["scanned"] is True
    finally:
        server.shutdown()


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


# --------------------------------------------------------------------------
# Stable URL: fixed port + persisted token
# --------------------------------------------------------------------------

def test_default_port_is_fixed():
    assert dashboard.DEFAULT_PORT == 54461


def test_token_is_persisted_and_stable(tmp_path, monkeypatch):
    monkeypatch.delenv("MINDHIVE_DASHBOARD_TOKEN", raising=False)
    t1 = dashboard.load_or_create_token()
    t2 = dashboard.load_or_create_token()
    assert t1 and t1 == t2                     # identical across launches
    # and it's actually on disk under the store home
    assert dashboard._token_path().read_text(encoding="utf-8").strip() == t1


def test_token_env_overrides_file(monkeypatch):
    monkeypatch.setenv("MINDHIVE_DASHBOARD_TOKEN", "fixed-env-token")
    assert dashboard.load_or_create_token() == "fixed-env-token"


# --------------------------------------------------------------------------
# Project / session / memory separation
# --------------------------------------------------------------------------

def test_projects_endpoint_lists_namespaces_with_stats():
    server, base, token = _start()
    try:
        _post(base, "/api/append", {"type": "decision", "payload": "a", "project": "alpha"}, token)
        _post(base, "/api/append", {"type": "decision", "payload": "b", "project": "alpha"}, token)
        _post(base, "/api/append", {"type": "decision", "payload": "c", "project": "beta"}, token)
        status, obj = _get(base, "/api/projects", token)
        assert status == 200
        data = json.loads(obj)
        names = {p["name"]: p for p in data["projects"]}
        assert "alpha" in names and "beta" in names
        assert names["alpha"]["events"] == 2
        assert data["default"] == "alpha"          # richest wins
    finally:
        server.shutdown()


def test_overview_separates_memory_from_auto():
    server, base, token = _start()
    try:
        _post(base, "/api/append", {"type": "decision", "payload": "intent", "project": "p"}, token)
        _post(base, "/api/append", {"type": "file_touched", "payload": "x.py", "project": "p"}, token)
        status, body = _get(base, "/api/overview?project=p", token)
        o = json.loads(body)
        assert o["events"] == 2
        assert o["memory_events"] == 1             # decision only
        assert o["auto_events"] == 1               # file_touched
    finally:
        server.shutdown()


def test_memory_endpoint_returns_semantic_only():
    server, base, token = _start()
    try:
        _post(base, "/api/append", {"type": "caveat", "payload": "watch out", "project": "p"}, token)
        _post(base, "/api/append", {"type": "command_run", "payload": "ls", "project": "p"}, token)
        status, body = _get(base, "/api/memory?project=p", token)
        items = json.loads(body)
        assert len(items) == 1
        assert items[0]["type"] == "caveat"
        assert items[0]["text"] == "watch out"
    finally:
        server.shutdown()


def test_memory_endpoint_collapses_near_duplicates():
    server, base, token = _start()
    try:
        # Two re-phrasings of the same caveat + one distinct memo.
        _post(base, "/api/append", {"type": "caveat",
              "payload": "prefer the browser for visual content like mockups and diagrams",
              "project": "p"}, token)
        _post(base, "/api/append", {"type": "caveat",
              "payload": "prefer browser for visual content like mockups and diagrams",
              "project": "p"}, token)
        _post(base, "/api/append", {"type": "caveat",
              "payload": "rebuild the exe with python 3.12 before launching", "project": "p"}, token)
        status, body = _get(base, "/api/memory?project=p", token)
        items = json.loads(body)
        assert len(items) == 2                      # the pair collapsed to one
        rep = next(m for m in items if "browser" in m["text"])
        assert rep["similar_count"] == 1            # absorbed one older duplicate
        # newest wording wins (the second append, without "the")
        assert rep["text"] == "prefer browser for visual content like mockups and diagrams"
    finally:
        server.shutdown()


def test_sessions_endpoint_groups_by_session(monkeypatch):
    server, base, token = _start()
    try:
        monkeypatch.setenv("MINDHIVE_SESSION", "sess-1")
        _post(base, "/api/append", {"type": "decision", "payload": "one", "project": "p"}, token)
        monkeypatch.setenv("MINDHIVE_SESSION", "sess-2")
        _post(base, "/api/append", {"type": "decision", "payload": "two", "project": "p"}, token)
        _post(base, "/api/append", {"type": "file_touched", "payload": "y.py", "project": "p"}, token)
        status, body = _get(base, "/api/sessions?project=p", token)
        sessions = {s["session_id"]: s for s in json.loads(body)}
        assert set(sessions) == {"sess-1", "sess-2"}
        assert sessions["sess-2"]["events"] == 2
        assert sessions["sess-2"]["memory"] == 1   # only the decision counts as memory
    finally:
        server.shutdown()


def test_project_scoping_isolates_overview():
    server, base, token = _start()
    try:
        _post(base, "/api/append", {"type": "decision", "payload": "only-A", "project": "A"}, token)
        status, body = _get(base, "/api/overview?project=B", token)
        o = json.loads(body)
        assert o["project"] == "B"
        assert o["events"] == 0                     # A's event must not leak into B
    finally:
        server.shutdown()


def test_hivemind_overview_aggregates_all_projects():
    server, base, token = _start()
    try:
        _post(base, "/api/append", {"type": "decision", "payload": "a", "project": "alpha"}, token)
        _post(base, "/api/append", {"type": "file_touched", "payload": "x.py", "project": "alpha"}, token)
        _post(base, "/api/append", {"type": "caveat", "payload": "b", "project": "beta"}, token)
        status, body = _get(base, "/api/overview?project=*", token)
        o = json.loads(body)
        assert o["events"] == 3                      # alpha(2) + beta(1)
        assert o["memory_events"] == 2               # decision + caveat
        assert o["auto_events"] == 1                 # file_touched
        assert o["projects"] == 2
    finally:
        server.shutdown()


def test_hivemind_memory_spans_projects_and_labels_them():
    server, base, token = _start()
    try:
        _post(base, "/api/append", {"type": "decision", "payload": "from-alpha", "project": "alpha"}, token)
        _post(base, "/api/append", {"type": "caveat", "payload": "from-beta", "project": "beta"}, token)
        status, body = _get(base, "/api/memory?project=*", token)
        items = json.loads(body)
        projs = {m["project"] for m in items}
        assert projs == {"alpha", "beta"}
        assert {m["text"] for m in items} == {"from-alpha", "from-beta"}
    finally:
        server.shutdown()


def test_hivemind_projects_endpoint_exposes_unified_totals():
    server, base, token = _start()
    try:
        _post(base, "/api/append", {"type": "decision", "payload": "a", "project": "alpha"}, token)
        _post(base, "/api/append", {"type": "decision", "payload": "b", "project": "beta"}, token)
        status, body = _get(base, "/api/projects", token)
        data = json.loads(body)
        assert data["hivemind"]["events"] == 2
        assert data["hivemind"]["id"] == "*"
    finally:
        server.shutdown()


def test_hivemind_append_is_rejected():
    server, base, token = _start()
    try:
        status, obj = _post(base, "/api/append",
                            {"type": "decision", "payload": "nope", "project": "*"}, token)
        assert obj["ok"] is False
        assert "HIVE MIND" in obj["error"]
    finally:
        server.shutdown()


def test_harvest_endpoint_runs_catch_up(monkeypatch):
    import mindhive.harvest as h
    monkeypatch.setattr(h, "catch_up", lambda skip_captured=True: {"projects": {}, "total_new": 0})
    server, base, token = _start()
    try:
        status, obj = _post(base, "/api/harvest", {}, token)
        assert obj["ok"] is True
        assert obj["result"]["total_new"] == 0
    finally:
        server.shutdown()


def test_harvest_endpoint_allowed_under_hivemind(monkeypatch):
    import mindhive.harvest as h
    monkeypatch.setattr(h, "catch_up", lambda skip_captured=True: {"projects": {}, "total_new": 0})
    server, base, token = _start()
    try:
        status, obj = _post(base, "/api/harvest", {"project": "*"}, token)
        assert obj["ok"] is True            # harvest is global, not blocked by HIVE MIND guard
    finally:
        server.shutdown()


def test_page_has_hivemind_picker():
    server, base, token = _start()
    try:
        status, body = _get(base, "/")
        assert 'value="*"' in body or "hivemind" in body
        assert "all projects" in body.lower()
    finally:
        server.shutdown()


def test_page_has_project_and_memory_controls():
    server, base, token = _start()
    try:
        status, body = _get(base, "/")
        for needle in ['id="proj"', "onProjChange", "showMemory", "showSessions", "/api/projects"]:
            assert needle in body, f"missing: {needle}"
    finally:
        server.shutdown()
