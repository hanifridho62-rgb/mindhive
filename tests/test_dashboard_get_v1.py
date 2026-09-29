# tests/test_dashboard_get_v1.py — the v1.1 GET mixin (blueprint P1b, BAB 5.5).
"""`dashboard_get_v1.GetRoutesV1` and the route it carries, `/api/worksessions`.

Three things are pinned here: the composition (`make_handler` merges the two
route tables into a table of the handler's OWN, and the tables are disjoint),
the route's payload against a real server over the conftest-isolated store,
and the served page carrying the Oversee hooks that read it. The MEMORY.md walk
is pointed at a temporary home through `worksession_usage.memory_path_index`,
the one seam the route calls, so no test here reads the real ``~/.claude``.
"""
import json
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, dashboard_get_v1, events, initiatives
from mindhive import worksession_usage as wu
from mindhive import worksessions as ws
from mindhive.dashboard_get import GetRoutes
from mindhive.dashboard_get_v1 import GetRoutesV1

from tests.serving import serve_in_thread

TOKEN = "test-token-v1"


@pytest.fixture(autouse=True)
def _empty_memo():
    dashboard_get_v1._WORKSESSIONS_MEMO.clear()
    yield
    dashboard_get_v1._WORKSESSIONS_MEMO.clear()


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(TOKEN))
    with serve_in_thread(srv):
        yield f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def home(tmp_path, monkeypatch):
    real = wu.memory_path_index
    monkeypatch.setattr(wu, "memory_path_index", lambda home=None: real(tmp_path))
    return tmp_path


def _digest(home, encoded, text):
    p = home / ".claude" / "projects" / encoded / "memory" / "MEMORY.md"
    p.parent.mkdir(parents=True)
    p.write_text(text, encoding="utf-8")
    return p


def _get(base, path):
    sep = "&" if "?" in path else "?"
    with urllib.request.urlopen(f"{base}{path}{sep}token={TOKEN}", timeout=10) as r:
        return r.status, r.read().decode("utf-8")


def _step(sid, phase, action, project, **kw):
    step = ws.create_work_session_step(phase, action, False, **kw)
    ws.record_work_session_step(sid, step, project=project)
    return step


# --- 1. composition --------------------------------------------------------------

def test_the_two_route_tables_are_disjoint():
    assert set(GetRoutesV1._GET_ROUTES_V1).isdisjoint(GetRoutes._GET_ROUTES)
    assert "/api/worksessions" in GetRoutesV1._GET_ROUTES_V1


def test_make_handler_mounts_the_mixin_first_and_merges_a_table_of_its_own():
    handler = dashboard.make_handler("t")
    assert handler.__mro__[1] is GetRoutesV1 and handler.__mro__[2] is GetRoutes
    assert handler._GET_ROUTES == {**GetRoutes._GET_ROUTES, **GetRoutesV1._GET_ROUTES_V1}
    assert handler._GET_ROUTES is not GetRoutes._GET_ROUTES
    other = dashboard.make_handler("u")
    other._GET_ROUTES["/api/planted"] = lambda *a: None
    assert "/api/planted" not in handler._GET_ROUTES
    assert "/api/planted" not in GetRoutes._GET_ROUTES


def test_the_memo_is_reexported_for_the_module_level_sweep():
    assert dashboard._WORKSESSIONS_MEMO is dashboard_get_v1._WORKSESSIONS_MEMO
    assert dashboard.GetRoutesV1 is GetRoutesV1


# --- 2. the route ------------------------------------------------------------------

def test_worksessions_route_serves_sessions_initiatives_and_usage(server, home):
    step = _step("s1", "execution", "TDD_RED", "test")
    initiatives.declare_initiative("s1", "lock race", "incident:7", project="test")
    _digest(home, "C--Users-x-test", "# Current State\n" + "y" * 395 + "\n")
    status, body = _get(server, "/api/worksessions?project=test")
    assert status == 200
    doc = json.loads(body)
    assert doc["ok"] is True and doc["context_budget_tokens"] == 200_000
    (row,) = doc["sessions"]
    assert row["session_id"] == "s1" and row["project"] == "test"
    assert row["phase"] == "execution" and row["ws_tag"] == step.technical_action
    (init,) = doc["initiatives"]
    assert init["project"] == "test" and init["session_id"] == "s1"
    assert init["tag"] == "[INITIATIVE: lock race | Ref: incident:7]"
    usage = doc["usage"]["test"]
    assert usage["measured"] is True
    assert usage["bytes"] == 412 and usage["tokens_est"] == 103
    assert usage["safe_budget_tokens"] == 160_000 and usage["truncated"] is False


def test_worksessions_route_is_empty_but_well_formed_for_a_quiet_namespace(server, home):
    status, body = _get(server, "/api/worksessions?project=test")
    assert status == 200
    doc = json.loads(body)
    assert doc["sessions"] == [] and doc["initiatives"] == []
    assert doc["usage"]["test"]["path"] == "" and doc["usage"]["test"]["bytes"] == 0
    assert doc["usage"]["test"]["measured"] is False


def test_worksessions_route_needs_the_token(server):
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(f"{server}/api/worksessions?project=test", timeout=10)
    assert ei.value.code in (401, 403)


def test_hive_mind_merges_every_namespace_newest_first(server, home):
    _step("a1", "plan", "x", "alpha", now_fn=lambda: _utc("2026-09-10T10:00:00Z"))
    _step("b1", "kanban", "y", "beta", now_fn=lambda: _utc("2026-09-11T10:00:00Z"))
    initiatives.declare_initiative("a1", "first", project="alpha")
    initiatives.declare_initiative("b1", "second", project="beta")
    status, body = _get(server, "/api/worksessions?project=*")
    assert status == 200
    doc = json.loads(body)
    assert [(r["session_id"], r["project"]) for r in doc["sessions"]][:2] == \
        [("b1", "beta"), ("a1", "alpha")]
    assert {i["project"] for i in doc["initiatives"]} >= {"alpha", "beta"}
    assert {"alpha", "beta"} <= set(doc["usage"])


def test_an_unchanged_ledger_is_folded_once(server, home, monkeypatch):
    calls = []
    real = dashboard_get_v1.worksession_fold
    monkeypatch.setattr(dashboard_get_v1, "worksession_fold",
                        lambda project: calls.append(project) or real(project))
    _step("s1", "plan", "x", "test")
    _get(server, "/api/worksessions?project=test")
    _get(server, "/api/worksessions?project=test")
    if dashboard._ledger_fingerprint("test") is None:      # pragma: no cover
        pytest.skip("this filesystem gives no trustworthy ledger fingerprint")
    assert calls == ["test"]
    _step("s1", "execution", "y", "test")                   # the ledger moved
    status, body = _get(server, "/api/worksessions?project=test")
    assert calls == ["test", "test"]
    assert json.loads(body)["sessions"][0]["steps"] == 2


def test_the_usage_walk_happens_once_per_request(server, home, monkeypatch):
    walks = []
    real = wu.memory_path_index
    monkeypatch.setattr(wu, "memory_path_index",
                        lambda home=None: walks.append(1) or real())
    _step("a1", "plan", "x", "alpha")
    _step("b1", "plan", "x", "beta")
    _get(server, "/api/worksessions?project=*")
    assert walks == [1]


def test_same_second_initiatives_are_served_newest_first(server, home):
    for reason in ("older", "middle", "newer"):
        initiatives.declare_initiative("s1", reason, project="test")
    step = ws.create_work_session_step("plan", "x", True, initiative_reason="a step")
    ws.record_work_session_step("s1", step, project="test")
    evs = events.read_all("test")
    if len({e.ts for e in evs}) != 1:                       # pragma: no cover
        pytest.skip("the four appends straddled a second boundary")
    status, body = _get(server, "/api/worksessions?project=test")
    got = [(i["reason"], i["step_id"]) for i in json.loads(body)["initiatives"]]
    assert got == [("a step", step.step_id), ("newer", ""), ("middle", ""), ("older", "")]


def test_a_sibling_namespaces_digest_is_not_served_as_ours(server, home):
    _step("s1", "plan", "x", "app")
    _step("s2", "plan", "x", "web-app")
    _digest(home, "C--Users-x-web-app", "# web\n")
    status, body = _get(server, "/api/worksessions?project=*")
    usage = json.loads(body)["usage"]
    assert usage["web-app"]["measured"] is True
    assert usage["app"]["measured"] is False and usage["app"]["path"] == ""


def test_the_registry_is_read_once_per_request(server, home, monkeypatch):
    from mindhive import autosync
    reads = []
    real = autosync.load_registry
    monkeypatch.setattr(autosync, "load_registry", lambda: reads.append(1) or real())
    _step("a1", "plan", "x", "alpha")
    _step("b1", "plan", "x", "beta")
    _get(server, "/api/worksessions?project=*")
    assert reads == [1]


def test_the_fold_parses_only_the_work_session_types(monkeypatch):
    """A moved ledger is re-folded on nearly every live poll, so the fold must
    not build and sort every event `/api/oversee` has just read."""
    _step("s1", "plan", "x", "test")
    initiatives.declare_initiative("s1", "why", project="test")
    events.append_event("command_run", {"cmd": "pytest"}, session_id="s1", project="test")
    asked = []
    real = events.read_types
    monkeypatch.setattr(events, "read_all", lambda *a, **k: pytest.fail("read_all"))
    monkeypatch.setattr(events, "read_types",
                        lambda types, project=None: asked.append(set(types))
                        or real(types, project))
    fold = dashboard_get_v1.worksession_fold("test")
    assert asked == [{"work_session", "initiative_declared"}]
    assert [s["session_id"] for s in fold["sessions"]] == ["s1"]
    assert [i["reason"] for i in fold["initiatives"]] == ["why"]


# --- 3. the other surfaces a work-session row reaches ----------------------------------

def test_the_memory_view_never_shows_a_blank_work_session_row(server):
    step = _step("s1", "plan", "draft the grammar", "wsmem")
    ws.record_step_status("s1", step.step_id, "completed", project="wsmem")
    initiatives.declare_initiative("s1", "close the audit", "INC-7", project="wsmem")
    events.append_event("decision", {"text": "keep the parser"}, session_id="s1",
                        project="wsmem")
    status, body = _get(server, "/api/memory?project=wsmem")
    rows = {(r["type"], r["text"]) for r in json.loads(body)}
    assert all(text for _type, text in rows), rows
    texts = {t for _type, t in rows}
    assert any(step.step_id in t and "draft the grammar" in t for t in texts)
    assert f"[step_status] {step.step_id} (s1) -> completed" in texts
    assert "[initiative] [INITIATIVE: close the audit | Ref: INC-7]" in texts
    assert ("decision", "keep the parser") in rows
    assert dashboard_get_v1.memory_text(events.Event(
        ts="t", session_id="s", type="decision", payload={"text": "x"}, tags=[])) == ""


def test_bookkeeping_rows_never_re_date_a_session_in_the_sessions_view(server):
    from mindhive import oversee
    assert oversee._WS_TYPES is events.BOOKKEEPING_TYPES      # one rule, two views
    events.append_event("decision", {"text": "old work"}, session_id="old",
                        ts="2026-08-01T00:00:00Z", project="wslive")
    events.append_event("decision", {"text": "new work"}, session_id="new",
                        ts="2026-09-01T00:00:00Z", project="wslive")
    ws.record_work_session_step("old", ws.create_work_session_step(
        "plan", "copied", False, now_fn=lambda: _utc("2026-09-15T00:00:00Z")),
        project="wslive")
    initiatives.declare_initiative("only-ws", "bookkeeping only", project="wslive")
    status, body = _get(server, "/api/sessions?project=wslive")
    rows = {s["session_id"]: s for s in json.loads(body)}
    assert rows["old"]["last_ts"] == "2026-08-01T00:00:00Z"
    assert rows["old"]["events"] == 2                           # still counted
    # A session made only of bookkeeping rows is still dated, by its first row.
    assert rows["only-ws"]["first_ts"] == rows["only-ws"]["last_ts"] != ""
    assert [s["session_id"] for s in json.loads(body)] == ["only-ws", "new", "old"]


# --- 4. the page that reads it ---------------------------------------------------------

def test_the_served_page_carries_the_oversee_work_session_hooks(server):
    status, body = _get(server, "/")
    assert status == 200
    for hook in ("apiGet('/api/worksessions')", "WS_DATA", "function wsPills(",
                 "function wsUsage(", "function wsInitiatives(", "Autonomous Initiatives",
                 "!u.measured", "'step '+esc(i.step_id)",
                 ".pill.wsphase", ".pill.wsinit", ".usagebar", ".usagefill", ".usagesafe",
                 "--accent:", "var(--accent)", "% of '+budget.toLocaleString('en-US')"):
        assert hook in body, f"the served page lost {hook!r}"


def _utc(iso):
    from datetime import datetime, timezone
    return datetime.strptime(iso, ws.TS_FORMAT).replace(tzinfo=timezone.utc)
