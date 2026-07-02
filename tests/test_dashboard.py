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
    assert json.loads(body) == {"auto": [], "manual": []}


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
