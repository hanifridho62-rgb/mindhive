# tests/test_dashboard.py
import json
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard

# Store isolation is provided by the autouse `isolate_store` fixture in
# tests/conftest.py — every dashboard test (and its in-process server threads)
# runs against a throwaway ~/.mindhive so the live ledger is never touched.


@pytest.fixture
def dashboard_server():
    """Start a dashboard server on an ephemeral port; yield (base, token) and
    shut it down on teardown. Replaces the per-test _start() + try/finally."""
    token = "test-token-123"
    handler = dashboard.make_handler(token)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", token
    finally:
        server.shutdown()


def _get(base, path, token=None):
    url = base + path
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def test_root_page_serves_html_without_token(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    assert "<html" in body.lower()
    assert "MIND HIVE" in body


def test_api_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/state")  # no token
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_state_endpoint_returns_text(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/state", token)
    assert status == 200
    # Real STATE.md content (or the known empty sentinel) — not just "is a str".
    assert "# STATE" in body or "no STATE.md" in body


def test_verify_endpoint_returns_json(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/verify", token)
    assert status == 200
    obj = json.loads(body)
    # /api/verify wraps chain.verify() — assert its real contract key.
    assert "ok" in obj and isinstance(obj["ok"], bool)


def test_recall_endpoint_returns_list(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/recall?q=anything", token)
    assert status == 200
    obj = json.loads(body)
    assert isinstance(obj, list)


def test_graph_endpoint_returns_html(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/graph", token)
    assert status == 200
    # Real rendered graph page (the template emits these), not just any "<".
    assert "<html" in body.lower() and '<svg id="svg">' in body


def test_overview_endpoint_returns_stats(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/overview", token)
    assert status == 200
    obj = json.loads(body)
    for key in ("project", "events", "by_type", "last_ts", "chain_ok"):
        assert key in obj, f"overview missing {key}"
    assert isinstance(obj["events"], int)
    assert isinstance(obj["by_type"], dict)
    assert isinstance(obj["chain_ok"], bool)


def test_overview_counts_an_appended_event(dashboard_server):
    base, token = dashboard_server
    before = json.loads(_get(base, "/api/overview", token)[1])["events"]
    _post(base, "/api/append", {"type": "decision", "payload": "ov note"}, token)
    after = json.loads(_get(base, "/api/overview", token)[1])
    assert after["events"] == before + 1
    assert after["by_type"].get("decision", 0) >= 1
    assert after["last_ts"]


def test_plugins_recommend_returns_json(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/plugins/recommend", token)
    assert status == 200
    obj = json.loads(body)
    assert isinstance(obj, dict)


def _post(base, path, payload, token):
    url = f"{base}{path}?token={token}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_plugins_autoscan_is_stale_gated(dashboard_server):
    base, token = dashboard_server
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


def test_plugins_inventory_endpoint_returns_live_snapshot(dashboard_server, monkeypatch):
    """GET /api/plugins/inventory serves the signature-gated live inventory —
    the payload the Skills & plugins view polls every LIVE_MS."""
    from mindhive import plugins as plugins_mod
    fake = {"changed": True, "signature": "abc",
            "inventory": {"plugins": [], "local_skills": [],
                          "counts": {"plugins": 0, "plugin_skills": 0,
                                     "local_skills": 0, "skills_total": 0}}}
    monkeypatch.setattr(plugins_mod, "live_inventory", lambda: fake)
    base, token = dashboard_server
    status, body = _get(base, "/api/plugins/inventory", token)
    assert status == 200
    assert json.loads(body) == fake


def test_plugins_inventory_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/plugins/inventory")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_page_has_skill_and_plugin_view(dashboard_server):
    """The nav entry carries the Skill & Plugin panel wired to the live engine."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    assert "Skill &amp; Plugin" in body
    for needle in ("renderPlugins", "showSkillPanel", "Recommended now",
                   "/api/plugins/inventory", "/api/skills/recommend"):
        assert needle in body, f"missing skill&plugin hook: {needle}"


def test_skills_detail_endpoint_returns_intended_use_body_and_date(dashboard_server, monkeypatch):
    from mindhive import plugins as plugins_mod
    detail = {"name": "one", "intended_use": "Use for testing.",
              "added_at": "2026-06-11T04:07:43.030Z", "body": "# One\n\nbody",
              "source": "alpha"}
    captured = {}
    monkeypatch.setattr(plugins_mod, "skill_detail",
                        lambda name, plugin=None: captured.update(name=name, plugin=plugin) or detail)
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/detail?name=one&plugin=alpha", token)
    assert status == 200
    assert json.loads(body) == detail
    assert captured == {"name": "one", "plugin": "alpha"}


def test_skills_detail_unknown_returns_404(dashboard_server, monkeypatch):
    from mindhive import plugins as plugins_mod
    monkeypatch.setattr(plugins_mod, "skill_detail", lambda name, plugin=None: None)
    base, token = dashboard_server
    try:
        _get(base, "/api/skills/detail?name=nope", token)
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_skills_detail_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/skills/detail?name=one")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_skills_invoke_records_and_returns_ok(dashboard_server, monkeypatch):
    from mindhive import invoker as invoker_mod
    seen = {}
    monkeypatch.setattr(invoker_mod, "invoke_skill",
        lambda name, plugin=None, project=None: seen.update(name=name, plugin=plugin)
        or {"ok": True, "exit": 0, "excerpt": "done"})
    base, token = dashboard_server
    status, obj = _post(base, "/api/skills/invoke",
                        {"name": "brainstorming", "plugin": "superpowers"}, token)
    assert status == 200 and obj["ok"] is True
    assert seen == {"name": "brainstorming", "plugin": "superpowers"}


def test_skills_invoke_requires_name(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/skills/invoke", {}, token)
    assert obj["ok"] is False and "name" in obj["error"]


def test_skills_invoke_requires_token(dashboard_server):
    base, token = dashboard_server
    req = urllib.request.Request(f"{base}/api/skills/invoke", data=b"{}",
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_skills_history_endpoint_returns_list(dashboard_server, monkeypatch):
    from mindhive import plugins as plugins_mod
    seen = {}
    monkeypatch.setattr(plugins_mod, "call_history",
        lambda name, project=None, limit=20: seen.update(name=name, limit=limit)
        or [{"ts": "t", "ok": True, "text": "x", "session": "s"}])
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/history?name=foo&limit=7", token)
    assert status == 200
    assert json.loads(body) == [{"ts": "t", "ok": True, "text": "x", "session": "s"}]
    assert seen == {"name": "foo", "limit": 7}


def test_skills_history_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/skills/history?name=foo")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_page_has_call_history_hook(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("/api/skills/history", "Call history"):
        assert needle in body, f"missing call-history hook: {needle}"


def test_skills_status_endpoint_returns_map(dashboard_server, monkeypatch):
    from mindhive import plugins as plugins_mod
    monkeypatch.setattr(plugins_mod, "call_status", lambda project=None: {"a": "ok", "b": "fail"})
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/status", token)
    assert status == 200
    assert json.loads(body) == {"a": "ok", "b": "fail"}


def test_skills_stats_endpoint_returns_stats_keyed_by_skill(dashboard_server):
    from mindhive import events
    events.append_event("invoke_result", {"name": "a", "ok": True, "text": ""},
                        session_id="s", ts="2026-07-01T10:00:00Z", project="test")
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/stats?project=test", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["stats"]["a"]["calls"] == 1
    assert obj["stats"]["a"]["completion_rate"] == 1.0


def test_skills_stats_endpoint_unknown_project_is_empty_not_500(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/stats?project=no-such-ns", token)
    assert status == 200
    assert json.loads(body) == {"stats": {}}


def test_skills_recommend_endpoint_returns_ranked_payload(dashboard_server, monkeypatch):
    from mindhive import plugins as plugins_mod
    monkeypatch.setattr(plugins_mod, "live_inventory", lambda: {
        "changed": True, "signature": "x", "inventory": {
            "plugins": [{"name": "p", "skills": [
                {"name": "tester", "trigger": "Use when tests fail."}]}],
            "local_skills": [], "counts": {}}})
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/recommend?project=test", token)
    assert status == 200
    obj = json.loads(body)
    assert "coverage" in obj and "recommendations" in obj
    assert obj["recommendations"][0]["name"] == "tester"
    assert obj["recommendations"][0]["provenance"] in ("measured", "estimated")


def test_page_has_invoke_and_status_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("invokeSkill", "/api/skills/invoke", "/api/skills/status", "statusDot"):
        assert needle in body, f"missing invoke/status hook: {needle}"


def test_skills_plugins_view_has_improvements_style_controls(dashboard_server):
    """The Skills & plugins view mirrors the Improvements mechanism: a fetch/paint
    split, persistent search + filter + sort controls, and a status legend."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("paintPlugins", "onPlugSearch", "plugstatus", "plugmarket",
                   "plugsort", "plugdir", "plugLegend", "PLUG_QUERY"):
        assert needle in body, f"missing plugins filter/sort hook: {needle}"


def test_skills_plugins_view_has_card_call_button_and_clickable_legend(dashboard_server):
    """Callable straight from the list (per-card ▶ call) and a legend whose
    green/red/grey items filter by status on click."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("callbtn", "setPlugStatus", "legitem"):
        assert needle in body, f"missing polish hook: {needle}"


def test_page_has_skill_detail_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("showSkillDetail", "/api/skills/detail", "Intended use"):
        assert needle in body, f"missing detail hook: {needle}"


def test_append_creates_event(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/append",
                        {"type": "decision", "payload": "dashboard test note",
                         "tags": "test"}, token)
    assert status == 200
    assert obj["ok"] is True
    assert obj["type"] == "decision"
    assert "ts" in obj


def test_post_requires_token(dashboard_server):
    base, token = dashboard_server
    url = f"{base}/api/project"
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


from mindhive import paths as hp


def test_embed_blocked_by_live_lock(dashboard_server):
    base, token = dashboard_server
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


def test_page_has_feature_controls(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    for needle in ["State", "Recall", "Graph", "Append", "Plugins", "Export"]:
        assert needle in body, f"missing control: {needle}"
    assert "fetch(" in body


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

def test_projects_endpoint_lists_namespaces_with_stats(dashboard_server):
    base, token = dashboard_server
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


def test_overview_separates_memory_from_auto(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "intent", "project": "p"}, token)
    _post(base, "/api/append", {"type": "file_touched", "payload": "x.py", "project": "p"}, token)
    status, body = _get(base, "/api/overview?project=p", token)
    o = json.loads(body)
    assert o["events"] == 2
    assert o["memory_events"] == 1             # decision only
    assert o["auto_events"] == 1               # file_touched


def test_memory_endpoint_returns_semantic_only(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "caveat", "payload": "watch out", "project": "p"}, token)
    _post(base, "/api/append", {"type": "command_run", "payload": "ls", "project": "p"}, token)
    status, body = _get(base, "/api/memory?project=p", token)
    items = json.loads(body)
    assert len(items) == 1
    assert items[0]["type"] == "caveat"
    assert items[0]["text"] == "watch out"


def test_transcripts_fingerprint_advances_on_append(tmp_path):
    """The low-latency loop gates on this: it must change when a session file
    grows, so a new prompt triggers a harvest within one poll."""
    import os, time
    root = tmp_path / "projects"
    (root / "enc").mkdir(parents=True)
    f = root / "enc" / "s.jsonl"
    f.write_text("line1\n", encoding="utf-8")
    fp1 = dashboard._transcripts_fingerprint(root)
    assert fp1 > 0
    newer = time.time() + 5
    os.utime(f, (newer, newer))            # simulate an append a moment later
    fp2 = dashboard._transcripts_fingerprint(root)
    assert fp2 > fp1


def test_transcripts_fingerprint_empty_root_is_zero(tmp_path):
    assert dashboard._transcripts_fingerprint(tmp_path / "nope") == 0.0


def test_memory_endpoint_includes_user_prompts(dashboard_server):
    """Every captured prompt shows in the Memory view (a user_prompt event),
    while deterministic activity (command_run) still does not."""
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "user_prompt", "payload": "what is the plan?", "project": "p"}, token)
    _post(base, "/api/append", {"type": "command_run", "payload": "ls", "project": "p"}, token)
    status, body = _get(base, "/api/memory?project=p", token)
    items = json.loads(body)
    texts = {(m["type"], m["text"]) for m in items}
    assert ("user_prompt", "what is the plan?") in texts
    assert not any(m["type"] == "command_run" for m in items)


def test_memory_endpoint_collapses_near_duplicates(dashboard_server):
    base, token = dashboard_server
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


def test_sessions_endpoint_groups_by_session(dashboard_server, monkeypatch):
    base, token = dashboard_server
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


def test_project_scoping_isolates_overview(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "only-A", "project": "A"}, token)
    status, body = _get(base, "/api/overview?project=B", token)
    o = json.loads(body)
    assert o["project"] == "B"
    assert o["events"] == 0                     # A's event must not leak into B


def test_hivemind_overview_aggregates_all_projects(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "a", "project": "alpha"}, token)
    _post(base, "/api/append", {"type": "file_touched", "payload": "x.py", "project": "alpha"}, token)
    _post(base, "/api/append", {"type": "caveat", "payload": "b", "project": "beta"}, token)
    status, body = _get(base, "/api/overview?project=*", token)
    o = json.loads(body)
    assert o["events"] == 3                      # alpha(2) + beta(1)
    assert o["memory_events"] == 2               # decision + caveat
    assert o["auto_events"] == 1                 # file_touched
    assert o["projects"] == 2


def test_hivemind_memory_spans_projects_and_labels_them(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "from-alpha", "project": "alpha"}, token)
    _post(base, "/api/append", {"type": "caveat", "payload": "from-beta", "project": "beta"}, token)
    status, body = _get(base, "/api/memory?project=*", token)
    items = json.loads(body)
    projs = {m["project"] for m in items}
    assert projs == {"alpha", "beta"}
    assert {m["text"] for m in items} == {"from-alpha", "from-beta"}


def test_hivemind_projects_endpoint_exposes_unified_totals(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "a", "project": "alpha"}, token)
    _post(base, "/api/append", {"type": "decision", "payload": "b", "project": "beta"}, token)
    status, body = _get(base, "/api/projects", token)
    data = json.loads(body)
    assert data["hivemind"]["events"] == 2
    assert data["hivemind"]["id"] == "*"


def test_hivemind_append_is_rejected(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/append",
                        {"type": "decision", "payload": "nope", "project": "*"}, token)
    assert obj["ok"] is False
    assert "HIVE MIND" in obj["error"]


def test_harvest_endpoint_runs_catch_up(dashboard_server, monkeypatch):
    import mindhive.harvest as h
    monkeypatch.setattr(h, "catch_up", lambda skip_captured=True: {"projects": {}, "total_new": 0})
    base, token = dashboard_server
    status, obj = _post(base, "/api/harvest", {}, token)
    assert obj["ok"] is True
    assert obj["result"]["total_new"] == 0


def test_harvest_endpoint_allowed_under_hivemind(dashboard_server, monkeypatch):
    import mindhive.harvest as h
    monkeypatch.setattr(h, "catch_up", lambda skip_captured=True: {"projects": {}, "total_new": 0})
    base, token = dashboard_server
    status, obj = _post(base, "/api/harvest", {"project": "*"}, token)
    assert obj["ok"] is True            # harvest is global, not blocked by HIVE MIND guard


def test_plugins_scan_allowed_under_hivemind(dashboard_server):
    # Plugins/skills are installed machine-wide, so scanning is global — the
    # HIVE MIND (all-projects) view must NOT reject it with the read-only guard.
    base, token = dashboard_server
    status, obj = _post(base, "/api/plugins/autoscan", {"project": "*"}, token)
    assert obj["ok"] is True
    assert "HIVE MIND" not in str(obj.get("error", ""))
    status, obj2 = _post(base, "/api/plugins/scan", {"project": "*"}, token)
    assert obj2["ok"] is True


def test_page_has_hivemind_picker(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert 'value="*"' in body or "hivemind" in body
    assert "all projects" in body.lower()


def test_page_has_project_and_memory_controls(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ['id="proj"', "onProjChange", "showMemory", "showSessions", "/api/projects"]:
        assert needle in body, f"missing: {needle}"


# --------------------------------------------------------------------------
# Token comparison (constant-time) + /api/sync folder resolution
# --------------------------------------------------------------------------

def test_wrong_token_is_rejected(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/state", token="totally-wrong-token")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_correct_token_is_accepted(dashboard_server):
    base, token = dashboard_server
    status, _ = _get(base, "/api/state", token)
    assert status == 200


def test_sync_uses_registered_project_folder(dashboard_server, monkeypatch):
    """/api/sync must write the selected project's OWN folder (from the autosync
    registry), not the directory the dashboard server was launched from."""
    from mindhive import autosync
    autosync.register("test", "C:/tx/test", "C:/code/test-project")
    captured = {}

    def spy_sync(folder, project=None):
        captured["folder"] = folder
        captured["project"] = project
        return "C:/code/test-project/MEMORY.md"

    monkeypatch.setattr(dashboard.sync_mod, "sync", spy_sync)
    base, token = dashboard_server
    status, obj = _post(base, "/api/sync", {"project": "test"}, token)
    assert status == 200 and obj["ok"] is True
    assert captured["folder"] == "C:/code/test-project"   # registry folder, not cwd
    assert captured["project"] == "test"
    assert obj["folder"] == "C:/code/test-project"


def test_sync_falls_back_to_cwd_when_unregistered(dashboard_server, monkeypatch):
    """An unregistered project has no folder mapping, so sync uses the cwd."""
    import os
    captured = {}
    monkeypatch.setattr(dashboard.sync_mod, "sync",
                        lambda folder, project=None: captured.update(folder=folder) or "x")
    base, token = dashboard_server
    _post(base, "/api/sync", {"project": "never-registered"}, token)
    assert captured["folder"] == os.getcwd()


# --- launch_background(): detached background launcher for the loop ----------

def test_stable_url_uses_default_port_and_token(monkeypatch):
    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "tok-xyz")
    url = dashboard.stable_url()
    assert url == f"http://127.0.0.1:{dashboard.DEFAULT_PORT}/?token=tok-xyz"


def test_launch_background_spawns_dashboard_subprocess(monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 4321

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "tok-abc")
    monkeypatch.setattr(dashboard.subprocess, "Popen", fake_popen)
    res = dashboard.launch_background()
    assert res["launched"] is True
    assert res["pid"] == 4321
    assert "tok-abc" in res["url"]
    assert captured["argv"][0] == dashboard.sys.executable
    assert captured["argv"][1:] == ["-m", "mindhive", "dashboard"]
    # child stdio is fully detached from the parent console (all three streams)
    assert captured["kwargs"]["stdin"] == dashboard.subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] == dashboard.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == dashboard.subprocess.DEVNULL


def test_launch_background_passes_explicit_port(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "t")
    monkeypatch.setattr(dashboard.subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or type("P", (), {"pid": 1})())
    dashboard.launch_background(port=0)
    assert captured["argv"][-2:] == ["--port", "0"]


def test_launch_background_uses_platform_detach_flags(monkeypatch):
    import os as _os
    captured = {}
    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "t")
    monkeypatch.setattr(dashboard.subprocess, "Popen",
                        lambda argv, **kw: captured.update(kw=kw) or type("P", (), {"pid": 1})())
    dashboard.launch_background()
    if _os.name == "nt":
        assert captured["kw"].get("creationflags", 0) != 0
    else:
        assert captured["kw"].get("start_new_session") is True


def test_launch_background_is_non_fatal_on_spawn_failure(monkeypatch):
    def boom(argv, **kw):
        raise OSError("cannot spawn")

    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "t")
    monkeypatch.setattr(dashboard.subprocess, "Popen", boom)
    res = dashboard.launch_background()
    assert res["launched"] is False
    assert "cannot spawn" in res["reason"]


# --------------------------------------------------------------------------
# /api/improvements: lessons learned (auto incidents + manual caveats)
# --------------------------------------------------------------------------
from mindhive import events as ev_mod, incidents as inc_mod


def _seed_incident(project, sig="pymodule:mindhive", n=2):
    for i in range(n):
        ev_mod.append_event("tool_failed",
                            {"text": f"err {sig}", "signature": sig,
                             "action": f"cmd{i}"},
                            session_id=f"s{i}", tags=["auto", "failure"],
                            project=project)
    inc_mod.audit(project=project)
    inc_mod.project(project=project)


def test_improvements_returns_auto_and_manual(dashboard_server):
    base, token = dashboard_server
    _seed_incident("test")
    ev_mod.append_event("caveat", {"text": "never run embed while V17 trades"},
                        session_id="s9", project="test")
    status, body = _get(base, "/api/improvements?project=test", token)
    assert status == 200
    obj = json.loads(body)
    assert len(obj["auto"]) == 1
    a = obj["auto"][0]
    assert a["signature"] == "pymodule:mindhive"
    assert a["occurrences"] == 2
    assert a["source"] == "auto" and a["project"] == "test"
    assert a["status"] == "active"
    assert a["lesson"] == ""                     # not yet reflected
    assert a["excerpt"].startswith("err ")
    assert len(obj["manual"]) == 1
    m = obj["manual"][0]
    assert m["source"] == "manual" and "V17" in m["text"]


def test_improvements_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/improvements")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_improvements_empty_store_returns_empty_lists(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/improvements?project=nothing-here", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["auto"] == [] and obj["manual"] == []
    # four tiles, all honest about having no data at all
    assert set(obj["stats"]) == {"day", "week", "month", "quarter"}
    for row in obj["stats"].values():
        assert row["disposition"] == "no_current_data"
        assert row["delta_pct"] is None


def test_improvements_hivemind_aggregates_projects(dashboard_server):
    base, token = dashboard_server
    ev_mod.append_event("caveat", {"text": "p1 lesson about flaky socket"},
                        session_id="a", project="p1")
    ev_mod.append_event("caveat", {"text": "p2 note on schema drift"},
                        session_id="b", project="p2")
    status, body = _get(base, "/api/improvements?project=*", token)
    assert status == 200
    obj = json.loads(body)
    projects = {m["project"] for m in obj["manual"]}
    assert {"p1", "p2"} <= projects


def test_improvements_unexpected_error_returns_500_json(dashboard_server, monkeypatch):
    base, token = dashboard_server

    def boom(project=None):
        raise RuntimeError("kaput")

    monkeypatch.setattr(inc_mod, "summaries", boom)
    try:
        _get(base, "/api/improvements?project=test", token)
        assert False, "expected 500"
    except urllib.error.HTTPError as e:
        assert e.code == 500
        obj = json.loads(e.read().decode("utf-8"))
        assert obj["ok"] is False and "kaput" in obj["error"]


def test_page_has_improvements_view(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    for needle in ("Improvements", "renderImprovements", "paintImprovements",
                   "impstatus", "impcat"):
        assert needle in body, f"missing improvements hook: {needle}"


# --------------------------------------------------------------------------
# POST /api/rules/reflect: bounded, guarded reflection from the panel
# --------------------------------------------------------------------------
from mindhive import reflect as refl_mod, dashboard as dash_mod


def _write_verdict(project, sig):
    path = (hp.project_dir(project) / "rules" / "incidents"
            / f"{inc_mod._safe_name(sig)}.md")
    path.write_text(inc_mod._replace_section(
        path.read_text(encoding="utf-8"), "Auto Evaluation",
        "- Root cause: x\n- Lesson: y\n- Prevention rule: z\n- Category: tool_use"),
        encoding="utf-8")


def test_reflect_happy_path(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:one")
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
    assert status == 200
    assert obj == {"ok": True, "reflected": 1, "failed": 0, "pending": 0}


def test_reflect_skips_already_reflected(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:done")
    _write_verdict("test", "sig:done")
    calls = []
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: calls.append(sig) or {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
    assert obj["ok"] is True and obj["reflected"] == 0 and obj["pending"] == 0
    assert calls == []


def test_reflect_batch_cap_reports_pending(dashboard_server, monkeypatch):
    base, token = dashboard_server
    for i in range(5):
        _seed_incident("test", sig=f"sig:batch{i}")
    calls = []
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: calls.append(sig) or {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect",
                        {"project": "test", "limit": 3}, token)
    assert len(calls) == 3
    assert obj["reflected"] == 3 and obj["pending"] == 2


def test_reflect_excludes_candidates(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:single", n=1)      # below threshold
    calls = []
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: calls.append(sig) or {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
    assert obj == {"ok": True, "reflected": 0, "failed": 0, "pending": 0}
    assert calls == []


def test_reflect_rejects_concurrent_run(dashboard_server):
    base, token = dashboard_server
    assert dash_mod._REFLECT_LOCK.acquire(blocking=False)
    try:
        status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
        assert status == 200
        assert obj["ok"] is False and "already running" in obj["error"]
    finally:
        dash_mod._REFLECT_LOCK.release()


def test_reflect_all_failed_missing_cli_is_domain_rejection(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:nocli")
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: {"reflected": False,
                                                   "reason": "empty verdict"})
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
    assert status == 200
    assert obj["ok"] is False and "claude CLI unavailable" in obj["error"]


def test_reflect_noop_on_empty_store(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/reflect", {"project": "bare"}, token)
    assert obj == {"ok": True, "reflected": 0, "failed": 0, "pending": 0}


def test_reflect_rejected_for_hivemind(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/reflect", {"project": "*"}, token)
    assert status == 200
    assert obj["ok"] is False and "read-only" in obj["error"]


# --------------------------------------------------------------------------
# POST /api/rules/reflect_one: on-demand reflection of ONE failure signature —
# lets a single skill-call failure be learned from immediately (the UI's
# "reflect this failure" action), bypassing the recurrence-threshold gate.
# --------------------------------------------------------------------------

def test_reflect_one_reflects_a_single_named_failure(dashboard_server, monkeypatch):
    base, token = dashboard_server
    # one failure (below the recurrence threshold) -> the endpoint still reflects it
    ev_mod.append_event("tool_failed",
                        {"text": "boom", "signature": "skill:foo",
                         "action": "invoke skill foo"},
                        session_id="s1", project="test")
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect_one",
                        {"project": "test", "signature": "skill:foo"}, token)
    assert status == 200
    assert obj["ok"] is True and obj["reflected"] is True
    assert obj["signature"] == "skill:foo"


def test_reflect_one_unknown_signature_rejected(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/reflect_one",
                        {"project": "test", "signature": "skill:never"}, token)
    assert obj["ok"] is False and "no such incident" in obj["error"]


def test_reflect_one_requires_signature(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/reflect_one", {"project": "test"}, token)
    assert obj["ok"] is False and "signature" in obj["error"]


def test_reflect_one_rejected_for_hivemind(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/reflect_one",
                        {"project": "*", "signature": "skill:x"}, token)
    assert status == 200
    assert obj["ok"] is False and "read-only" in obj["error"]


def test_reflect_one_requires_token(dashboard_server):
    base, token = dashboard_server
    req = urllib.request.Request(f"{base}/api/rules/reflect_one", data=b"{}",
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_page_has_reflect_failure_hook(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("/api/rules/reflect_one", "reflectFailure"):
        assert needle in body, f"missing reflect-failure hook: {needle}"


def test_reflect_unexpected_error_returns_500(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:boom")

    def boom(sig, project=None):
        raise RuntimeError("reflector exploded")

    monkeypatch.setattr(refl_mod, "reflect_incident", boom)
    url = f"{base}/api/rules/reflect?token={token}"
    data = json.dumps({"project": "test"}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "expected 500"
    except urllib.error.HTTPError as e:
        assert e.code == 500
        obj = json.loads(e.read().decode("utf-8"))
        assert obj["ok"] is False and "exploded" in obj["error"]


# --------------------------------------------------------------------------
# /api/improvements efficiency stats + per-lesson enrichment
# --------------------------------------------------------------------------
import time as time_mod


def _iso_ago(seconds):
    return time_mod.strftime("%Y-%m-%dT%H:%M:%S",
                             time_mod.gmtime(time_mod.time() - seconds)) + "Z"


def _seed_actions(project, session, ts, n, failures=0, sig="sig:x"):
    for i in range(n - failures):
        ev_mod.append_event("command_run", {"text": f"cmd{i}"},
                            session_id=session, ts=ts, project=project)
    for i in range(failures):
        ev_mod.append_event("tool_failed",
                            {"text": "boom", "signature": sig, "action": "x"},
                            session_id=session, ts=ts, project=project)


def _stats(base, token, project):
    status, body = _get(base, f"/api/improvements?project={project}", token)
    assert status == 200
    return json.loads(body)["stats"]


def test_efficiency_week_improvement_is_ok_with_negative_delta(dashboard_server):
    base, token = dashboard_server
    _seed_actions("eff", "s1", _iso_ago(10 * 86400), 22, failures=2)  # prev week
    _seed_actions("eff", "s2", _iso_ago(2 * 86400), 21, failures=1)   # this week
    week = _stats(base, token, "eff")["week"]
    assert week["disposition"] == "ok"
    assert week["cur_actions"] == 21 and week["prev_actions"] == 22
    assert week["delta_pct"] < 0                     # fewer failures = improvement


def test_efficiency_ignores_conversation_events(dashboard_server):
    base, token = dashboard_server
    _seed_actions("eff", "s1", _iso_ago(10 * 86400), 22, failures=2)
    _seed_actions("eff", "s2", _iso_ago(2 * 86400), 21, failures=1)
    before = _stats(base, token, "eff")["week"]
    for i in range(50):
        ev_mod.append_event("user_prompt", {"text": f"p{i}"}, session_id="s2",
                            ts=_iso_ago(3600), project="eff")
    after = _stats(base, token, "eff")["week"]
    assert after == before


def test_efficiency_sparse_dispositions(dashboard_server):
    base, token = dashboard_server
    # current data, no previous week -> no_baseline; empty day -> no_current_data
    _seed_actions("ebase", "s1", _iso_ago(2 * 86400), 21, failures=1)
    st = _stats(base, token, "ebase")
    assert st["week"]["disposition"] == "no_baseline"
    assert st["day"]["disposition"] == "no_current_data"
    # thin samples in both windows -> low_sample, never a colored percent
    _seed_actions("elow", "s1", _iso_ago(10 * 86400), 3, failures=1)
    _seed_actions("elow", "s2", _iso_ago(2 * 86400), 3)
    assert _stats(base, token, "elow")["week"]["disposition"] == "low_sample"
    # clean in both windows -> no_change at 0.0
    _seed_actions("ezero", "s1", _iso_ago(10 * 86400), 20)
    _seed_actions("ezero", "s2", _iso_ago(2 * 86400), 20)
    zero = _stats(base, token, "ezero")["week"]
    assert zero["disposition"] == "no_change" and zero["delta_pct"] == 0.0
    # failures where the baseline was zero -> regression marker, no percent
    _seed_actions("ereg", "s1", _iso_ago(10 * 86400), 20)
    _seed_actions("ereg", "s2", _iso_ago(2 * 86400), 21, failures=1)
    reg = _stats(base, token, "ereg")["week"]
    assert reg["disposition"] == "regression_from_zero" and reg["delta_pct"] is None


def test_efficiency_hivemind_aggregates_tallies(dashboard_server):
    base, token = dashboard_server
    # 12 + 12 actions per window: below the floor alone, above it combined
    _seed_actions("p1", "s1", _iso_ago(10 * 86400), 12, failures=2)
    _seed_actions("p1", "s2", _iso_ago(2 * 86400), 12, failures=1)
    _seed_actions("p2", "s1", _iso_ago(10 * 86400), 12)
    _seed_actions("p2", "s2", _iso_ago(2 * 86400), 12)
    week = _stats(base, token, "*")["week"]
    assert week["cur_actions"] == 24 and week["prev_actions"] == 24
    assert week["disposition"] == "ok"


def test_improvements_items_carry_resolution_and_timestamps(dashboard_server):
    base, token = dashboard_server
    # reflected rule, then a clean newer session -> resolved (green)
    _seed_actions("test", "g1", _iso_ago(9000), 1, failures=1, sig="sig:green")
    _seed_actions("test", "g1", _iso_ago(8000), 1, failures=1, sig="sig:green")
    # reflected rule that recurred cross-session -> unresolved (red)
    _seed_actions("test", "r1", _iso_ago(9000), 1, failures=1, sig="sig:red")
    _seed_actions("test", "r1", _iso_ago(8000), 1, failures=1, sig="sig:red")
    inc_mod.audit(project="test")
    inc_mod.project(project="test")
    _write_verdict("test", "sig:green")
    _write_verdict("test", "sig:red")
    inc_mod.project(project="test")
    ev_mod.append_event("command_run", {"text": "fine"}, session_id="g2",
                        ts=_iso_ago(1000), project="test")          # exposure
    _seed_actions("test", "r2", _iso_ago(500), 1, failures=1, sig="sig:red")
    status, body = _get(base, "/api/improvements?project=test", token)
    items = {a["signature"]: a for a in json.loads(body)["auto"]}
    green, red = items["sig:green"], items["sig:red"]
    assert green["resolution"] == "resolved" and green["leaks"] == 0
    assert green["first_seen"] and green["last_seen"] > green["first_seen"]
    assert red["resolution"] == "unresolved" and red["leaks"] == 1
    assert red["last_seen"] > red["first_seen"]


def test_page_has_sort_glyph_and_strip_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    for needle in ("impsort", "impdir", "toggleImpDir", "impCompare", "effStrip",
                   "effTile", "glyphSpan", "impLegend", "dismissLesson",
                   "last seen", "first seen"):
        assert needle in body, f"missing view hook: {needle}"


# --------------------------------------------------------------------------
# POST /api/rules/dismiss: explicit demotion from a lesson card
# --------------------------------------------------------------------------


def test_dismiss_happy_path_turns_card_gray(dashboard_server):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:bye")
    status, obj = _post(base, "/api/rules/dismiss",
                        {"project": "test", "signature": "sig:bye"}, token)
    assert status == 200
    assert obj == {"ok": True, "signature": "sig:bye"}
    demos = [e for e in ev_mod.read_all("test") if e.type == "rule_demoted"]
    assert demos and demos[0].payload["signature"] == "sig:bye"
    assert inc_mod.load("sig:bye", project="test")["status"] == "demoted"
    status, body = _get(base, "/api/improvements?project=test", token)
    item = [a for a in json.loads(body)["auto"] if a["signature"] == "sig:bye"][0]
    assert item["resolution"] == "dismissed"


def test_dismiss_unknown_or_missing_signature_rejected(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/dismiss",
                        {"project": "test", "signature": "nope:x"}, token)
    assert status == 200 and obj["ok"] is False
    assert "unknown signature" in obj["error"]
    status, obj = _post(base, "/api/rules/dismiss", {"project": "test"}, token)
    assert obj["ok"] is False
    assert not [e for e in ev_mod.read_all("test") if e.type == "rule_demoted"]


def test_post_oversized_body_is_rejected_without_unbounded_read(dashboard_server):
    # A caller advertising a huge Content-Length must be rejected before the
    # body is read, so an (even unauthenticated) POST can't force a large
    # allocation. We send a modest real body but lie about its length.
    base, token = dashboard_server
    url = f"{base}/api/append?token={token}"
    big = (1 << 20) + 1
    req = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "Content-Length": str(big)})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected rejection"
    except urllib.error.HTTPError as e:
        assert e.code == 500
        obj = json.loads(e.read().decode("utf-8"))
        assert obj["ok"] is False and "too large" in obj["error"]
    except (urllib.error.URLError, ConnectionError):
        pass          # server closed the connection without reading the body — also acceptable


def test_dismiss_rejected_for_hivemind(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/dismiss",
                        {"project": "*", "signature": "x"}, token)
    assert status == 200
    assert obj["ok"] is False and "read-only" in obj["error"]


def test_dismiss_requires_token(dashboard_server):
    base, token = dashboard_server
    req = urllib.request.Request(f"{base}/api/rules/dismiss", data=b"{}",
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_dismiss_unexpected_error_returns_500(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:boomd")

    def boom(sig, project=None):
        raise RuntimeError("demoter exploded")

    monkeypatch.setattr(inc_mod, "demote", boom)
    url = f"{base}/api/rules/dismiss?token={token}"
    data = json.dumps({"project": "test", "signature": "sig:boomd"}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "expected 500"
    except urllib.error.HTTPError as e:
        assert e.code == 500
        obj = json.loads(e.read().decode("utf-8"))
        assert obj["ok"] is False and "exploded" in obj["error"]


def test_improvements_stats_miss_is_watching_not_resolved(dashboard_server):
    base, token = dashboard_server
    # incident file with no backing events (projection outlived its events)
    d = hp.project_dir("ghosty") / "rules" / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ghost-sig.md").write_text(
        "---\nsignature: ghost:sig\nstatus: active\noccurrences: 2\n"
        "rule_source: auto\nflag: \nsessions: 1\n---\n\n# Incident: ghost:sig\n\n"
        "## Raw Signal\n- Error excerpt: `boom`\n\n"
        "## Auto Evaluation\n- Prevention rule: do X\n\n"
        "## Human Evaluation\n<!-- -->\n\n"
        "## Active Prevention Rule\n> do X\n", encoding="utf-8")
    status, body = _get(base, "/api/improvements?project=ghosty", token)
    assert status == 200
    a = json.loads(body)["auto"][0]
    assert a["resolution"] == "watching"            # never green without events
    assert a["first_seen"] is None and a["last_seen"] is None
    assert a["leaks"] == 0


# --- review fixes: limit clamp, non-numeric guard, pending accounting ---

def test_reflect_clamps_oversized_limit(dashboard_server, monkeypatch):
    base, token = dashboard_server
    for i in range(5):
        _seed_incident("test", sig=f"sig:clamp{i}")
    calls = []
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: calls.append(sig) or {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect",
                        {"project": "test", "limit": 100}, token)
    assert len(calls) == 3                       # clamped to REFLECT_BATCH_LIMIT
    assert obj["reflected"] == 3 and obj["pending"] == 2


def test_reflect_rejects_non_numeric_limit(dashboard_server):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:badlimit")
    status, obj = _post(base, "/api/rules/reflect",
                        {"project": "test", "limit": "abc"}, token)
    assert status == 200
    assert obj["ok"] is False and "integer" in obj["error"]


def test_reflect_pending_excludes_failed(dashboard_server, monkeypatch):
    base, token = dashboard_server
    for i in range(3):
        _seed_incident("test", sig=f"sig:mix{i}")
    seen = []

    def one_ok_rest_fail(sig, project=None):
        seen.append(sig)
        return {"reflected": len(seen) == 1}     # first ok, rest fail

    monkeypatch.setattr(refl_mod, "reflect_incident", one_ok_rest_fail)
    status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
    assert obj["reflected"] == 1 and obj["failed"] == 2
    assert obj["pending"] == 0                    # attempted-but-failed is not pending


# --- Oversee: work sessions view (U3/U4) ---------------------------------

def _seed_loop_run(run_id, hour, reason=None, title="Fix the parser"):
    ev_mod.append_event("loop_run_start",
                        {"run_id": run_id, "title": title, "goal_dir": "d"},
                        ts=f"2026-07-07T{hour:02d}:00:00Z", project="test")
    ev_mod.append_event("loop_iteration",
                        {"run_id": run_id, "iteration": 1, "summary": "working",
                         "cost_usd": 0.1, "total_tokens": 100},
                        ts=f"2026-07-07T{hour:02d}:01:00Z", project="test")
    if reason:
        ev_mod.append_event("loop_run_end", {"run_id": run_id, "reason": reason},
                            ts=f"2026-07-07T{hour:02d}:02:00Z", project="test")


def test_oversee_lists_runs_and_online_flag(dashboard_server):
    base, token = dashboard_server
    _seed_loop_run("run1", 10)                     # running (no end event)
    status, body = _get(base, "/api/oversee?project=test", token)
    assert status == 200
    obj = json.loads(body)
    assert [r["run_id"] for r in obj["runs"]] == ["run1"]
    r = obj["runs"][0]
    assert r["status"] == "running"
    assert r["title"] == "working"                 # latest summary supersedes
    # events are seeded in the past, so the heartbeat window has lapsed
    assert r["online"] is False and obj["any_online"] is False


def test_oversee_dismiss_removes_run(dashboard_server):
    base, token = dashboard_server
    _seed_loop_run("run1", 10, reason="max_iterations")
    status, obj = _post(base, "/api/oversee/dismiss",
                        {"project": "test", "run_id": "run1"}, token)
    assert status == 200 and obj == {"ok": True, "run_id": "run1"}
    tombs = [e for e in ev_mod.read_all("test") if e.type == "loop_run_dismissed"]
    assert tombs and tombs[0].payload["cause"] == "manual"
    status, body = _get(base, "/api/oversee?project=test", token)
    obj = json.loads(body)
    assert obj["runs"] == [] and obj["dismissed"] == 1


def test_oversee_dismiss_unknown_or_missing_run_rejected(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/oversee/dismiss",
                        {"project": "test", "run_id": "nope"}, token)
    assert status == 200 and obj["ok"] is False
    status, obj = _post(base, "/api/oversee/dismiss", {"project": "test"}, token)
    assert status == 200 and obj["ok"] is False
    assert not [e for e in ev_mod.read_all("test")
                if e.type == "loop_run_dismissed"]


def test_oversee_dismiss_rejected_for_hivemind(dashboard_server):
    base, token = dashboard_server
    _seed_loop_run("run1", 10)
    status, obj = _post(base, "/api/oversee/dismiss",
                        {"project": "*", "run_id": "run1"}, token)
    assert status == 200 and obj["ok"] is False
    assert "read-only" in obj["error"]


def test_oversee_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/oversee")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_page_has_oversee_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert "showOversee" in body and "Oversee" in body
    assert "/api/oversee" in body and "deleteRun" in body


def test_oversee_double_dismiss_is_idempotent(dashboard_server):
    base, token = dashboard_server
    _seed_loop_run("run1", 10, reason="max_iterations")
    status, obj = _post(base, "/api/oversee/dismiss",
                        {"project": "test", "run_id": "run1"}, token)
    assert obj == {"ok": True, "run_id": "run1"}
    status, obj = _post(base, "/api/oversee/dismiss",
                        {"project": "test", "run_id": "run1"}, token)
    assert status == 200
    assert obj["ok"] is True and obj.get("already_dismissed") is True
    tombs = [e for e in ev_mod.read_all("test") if e.type == "loop_run_dismissed"]
    assert len(tombs) == 1                             # no duplicate tombstone
