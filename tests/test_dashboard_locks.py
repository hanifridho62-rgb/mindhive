# tests/test_dashboard_locks.py
"""Server-side mutual exclusion on the mutating POST routes (T8), and the
stat-only gate in front of /api/mindnet/cats (T6).

Both features exist because the CLIENT was the only thing enforcing them. The
page disabled its own nav while a request was in flight, so "one harvest at a
time" held only for a user clicking one tab with JavaScript enabled — two tabs,
a double click, or one curl started two full runs over the same repo. Removing
that client-side guard (T9) makes the server the only place the rule can live.

These are behavioural tests, not source-string assertions: every one drives a
real HTTP request into a real handler and asserts what the underlying work
actually did (how many times it ran, whether the endpoint still answers
afterwards), which is the only thing that distinguishes a working lock from a
lock-shaped comment.

Store isolation comes from the autouse `isolate_store` fixture in
tests/conftest.py, like every other dashboard test.
"""
import collections
import importlib
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import (dashboard, dashboard_get, dashboard_jobs,
                      dashboard_memo, dedupe, events, jobs, paths, taskview)

# Patch targets are the module that OWNS the name, not the one that re-exports
# it. `dashboard` still exposes the locks and memos (same objects), but the code
# under test reads `run_catch_up` and `time` from the modules imported above, and
# patching a re-export would silently miss — the call would run the real thing
# while the assertion below still passed.

# The locks and both memos are MODULE level on purpose — they have to hold
# across every handler class `make_handler` mints, and across every thread the
# ThreadingHTTPServer serves on. That also means they outlive a test, so each
# test starts and ends from a known state.
_ALL_LOCKS = (dashboard._HARVEST_LOCK, dashboard._PROJECT_LOCK,
              dashboard._SYNC_LOCK, dashboard._EMBED_LOCK,
              dashboard._PLUGINS_LOCK, dashboard._SELFTEST_LOCK,
              dashboard._VERIFY_LOCK)


def _clear_memos():
    """Empty every module-level memo, enumerated rather than listed by name.

    Naming them here couples this whole file's SETUP to the current cache
    inventory: a memo that does not exist in the version under test turns every
    behavioural failure in the file into an AttributeError at setup, which is the
    one thing a test run must not do — it hides the result it was run to get."""
    for name, value in vars(dashboard).items():
        if name.endswith("_MEMO") and isinstance(value, collections.OrderedDict):
            value.clear()


@pytest.fixture(autouse=True)
def clean_module_state():
    _clear_memos()
    yield
    _clear_memos()
    # A job outlives the request that started it, so a test that leaves one
    # running would hold its kind's slot and get the NEXT test's first click
    # refused — which reads as a concurrency bug in whichever test ran second.
    jobs._RUNNING.clear()
    jobs._JOBS.clear()
    for lock in _ALL_LOCKS:
        if lock.locked():           # a failed test must not wedge the next one
            lock.release()


@pytest.fixture
def dashboard_server():
    """A dashboard on an ephemeral port; yields (base, token). Same shape as the
    fixture in test_dashboard.py — the server must be threaded, because the
    whole point is one request arriving while another is still running."""
    token = "test-token-123"
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", token
    finally:
        server.shutdown()


def _post(base, path, payload, token):
    """POST and return (status, decoded body). A 500 body is part of the
    contract here (do_POST turns an unexpected fault into one), so it is decoded
    rather than raised."""
    req = urllib.request.Request(
        f"{base}{path}?token={token}", data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(base, path, token):
    sep = "&" if "?" in path else "?"
    with urllib.request.urlopen(f"{base}{path}{sep}token={token}",
                                timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


# --- T8: one at a time, per operation --------------------------------------
# (route, module holding the real work, attribute, what the fake returns).
# Each entry names the ONE call the handler's cost actually lives in, so
# "the work ran exactly once" is a claim about the expensive thing, not about
# the HTTP round trip.
LOCKED = [
    ("/api/project", "mindhive.projector", "project", {"wrote": []}),
    ("/api/sync", "mindhive.sync", "sync", "MEMORY.md"),
    ("/api/plugins/autoscan", "mindhive.plugins", "ensure_fresh",
     {"scanned": True, "age_hours": 0.0, "payload": {}}),
]
LOCKED_IDS = [row[0] for row in LOCKED]

# --- T13: the six long operations, now 202-accepted jobs --------------------
# These used to be in LOCKED above, and the lock still exists and is still per
# operation — it is just held by the JOB for as long as the work runs, instead of
# by the handler for as long as the response takes. Two things therefore changed
# shape and are re-asserted below rather than dropped: the accepted response is
# 202, and a run that raises can no longer surface as a 500, because by then the
# response is long gone. It surfaces as a job whose outcome is `failed`.
#
# `work` is the ONE call each operation's cost actually lives in, so "the work ran
# exactly once" stays a claim about the expensive thing. `reach` is what has to be
# stubbed for that call to be reached at all, and `empty` is what the operation
# reports when there is genuinely nothing to do — the case T13 requires be
# exercised, and the one an `ok: True` could never distinguish from real work.
Op = collections.namedtuple("Op", "route work result reach empty")

NOTHING = "nothing_to_do"

JOBS = [
    Op("/api/harvest", ("mindhive.dashboard_jobs", "run_catch_up"),
       {"projects": {"test": {"new": 2}}, "total_new": 2},
       {}, ({"projects": {}, "total_new": 0}, NOTHING)),
    Op("/api/embed", ("mindhive.vectors", "embed"), {"embedded": 3},
       {}, ({"embedded": 0}, NOTHING)),
    Op("/api/plugins/scan", ("mindhive.plugins", "scan_and_store"),
       {"counts": {"plugins_total": 4, "skills_total": 9}},
       {}, ({"counts": {"plugins_total": 0, "skills_total": 0}}, NOTHING)),
    Op("/api/selftest", ("mindhive.selftest", "run"),
       {"exit_code": 0, "counts": {"passed": 4602}},
       {}, ({"exit_code": 0, "counts": {}}, "did_work")),
    # Reflection only reaches its expensive call when an eligible incident
    # exists, so the empty case is the DEFAULT here and `reach` is what makes the
    # work happen at all.
    Op("/api/rules/reflect", ("mindhive.reflect", "reflect_incident"),
       {"reflected": True},
       {("mindhive.incidents", "summaries"): lambda *a, **k: [
           {"signature": "sig-1", "status": "active", "rule_source": "none"}]},
       (None, NOTHING)),
    Op("/api/rules/resolve", ("mindhive.resolve", "resolve_batch"),
       {"rerouted": 1, "reflected": 0, "failed": 0},
       {}, ({"rerouted": 0, "reflected": 0, "failed": 0}, NOTHING)),
]
JOB_IDS = [op.route for op in JOBS]


def _stub_incident_projection(monkeypatch):
    """The reflect job self-heals the incident projection before it looks. Neither
    call is the work under test, and both touch the store."""
    incidents = importlib.import_module("mindhive.incidents")
    monkeypatch.setattr(incidents, "audit", lambda *a, **k: None)
    monkeypatch.setattr(incidents, "project", lambda *a, **k: None)
    monkeypatch.setattr(incidents, "summaries", lambda *a, **k: [])


def _apply(monkeypatch, patches):
    for (mod, attr), value in patches.items():
        monkeypatch.setattr(importlib.import_module(mod), attr, value)


def _await_job(base, token, job_id, timeout=20):
    """Poll a job to completion the way the page does, and return its final
    snapshot."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, obj = _get(base, f"/api/job?id={job_id}", token)
        assert status == 200 and obj["ok"] is True, obj
        if obj["job"]["state"] == "finished":
            return obj["job"]
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")


def _blocking(result):
    """A stand-in for the real work that parks until it is told to finish, and
    counts how many times it was entered."""
    entered, release, calls = threading.Event(), threading.Event(), []

    def fake(*_a, **_kw):
        calls.append(1)
        entered.set()
        release.wait(timeout=20)
        return result
    return fake, entered, release, calls


def _fire(base, route, token, out):
    t = threading.Thread(
        target=lambda: out.append(_post(base, route, {"project": "test"}, token)))
    t.start()
    return t


@pytest.mark.parametrize("route,mod,attr,result", LOCKED, ids=LOCKED_IDS)
def test_a_second_call_is_refused_while_the_first_still_holds_the_lock(
        dashboard_server, monkeypatch, route, mod, attr, result):
    base, token = dashboard_server
    fake, entered, release, calls = _blocking(result)
    monkeypatch.setattr(importlib.import_module(mod), attr, fake)

    out: list = []
    first = _fire(base, route, token, out)
    assert entered.wait(10), f"{route} never reached its work"

    status, obj = _post(base, route, {"project": "test"}, token)
    # An expected domain rejection: ok:False with HTTP 200, never 409 and never
    # 500 — this file's convention, so the page renders the reason instead of
    # treating it as a server fault.
    assert status == 200, f"{route} refused with {status}, not 200"
    assert obj["ok"] is False
    assert "already running" in obj["error"]

    release.set()
    first.join(20)
    assert out and out[0][0] == 200 and out[0][1]["ok"] is True
    # THE guarantee: the refusal did not merely answer differently, it stopped
    # the second run from happening at all.
    assert calls == [1], f"{route} ran the work {len(calls)} times, expected 1"


@pytest.mark.parametrize("route,mod,attr,result", LOCKED, ids=LOCKED_IDS)
def test_the_lock_is_released_when_the_handler_raises(
        dashboard_server, monkeypatch, route, mod, attr, result):
    # A lock released anywhere but a `finally` survives the happy path and
    # wedges the endpoint for the life of the process the first time anything
    # throws — the failure mode that is invisible until production.
    base, token = dashboard_server
    module = importlib.import_module(mod)

    def boom(*_a, **_kw):
        raise RuntimeError("work exploded")
    monkeypatch.setattr(module, attr, boom)
    status, obj = _post(base, route, {"project": "test"}, token)
    assert status == 500 and obj["ok"] is False   # do_POST's unexpected-fault path

    monkeypatch.setattr(module, attr, lambda *_a, **_kw: result)
    status, obj = _post(base, route, {"project": "test"}, token)
    assert status == 200 and obj["ok"] is True, \
        f"{route} stayed locked after the handler raised"


@pytest.mark.parametrize("op", JOBS, ids=JOB_IDS)
def test_a_long_operation_is_accepted_not_completed(dashboard_server,
                                                    monkeypatch, op):
    """202, and a job id to poll. A 200 would claim the work was done, which is
    the lie that made the browser give up on runs that were still going.

    The stub BLOCKS rather than returning instantly, and that is load-bearing
    rather than incidental. `_start` reads `job.snapshot(0)` after `jobs.start`
    has already spawned the job's daemon thread, so with instant work the state
    in the accepted response is a race: usually `running`, but `finished` the
    moment the runner schedules that thread first. It lost that race on CI
    (ubuntu/py3.11) while every other leg passed. Parking the work until this
    test releases it makes "the response went out before the work was done" a
    fact about the code instead of a fact about the scheduler.

    This is also why the POST below is expected to return at once while the work
    is still parked: the request thread hands off and answers, which is the whole
    claim. A regression to inline work would show up here as a hang until the
    socket timeout — same trade the blocking tests further down already take."""
    base, token = dashboard_server
    _stub_incident_projection(monkeypatch)
    _apply(monkeypatch, op.reach)
    mod, attr = op.work
    fake, _entered, release, _calls = _blocking(op.result)
    monkeypatch.setattr(importlib.import_module(mod), attr, fake)
    try:
        status, obj = _post(base, op.route, {"project": "test"}, token)
        assert status == 202, f"{op.route} answered {status}, not 202 Accepted"
        assert obj["ok"] is True and obj["job"]["id"]
        assert obj["job"]["state"] == "running"
        assert "outcome" not in obj["job"], \
            "an accepted job cannot know its outcome"
    finally:
        # In a `finally` so a failed assertion above does not leave the job
        # parked for 20s holding its kind's lock — the next test's first click
        # would then be refused, and read as a concurrency bug in whichever
        # test happened to run second.
        release.set()
    final = _await_job(base, token, obj["job"]["id"])
    assert final["outcome"] in ("did_work", "degraded"), final


@pytest.mark.parametrize("op", JOBS, ids=JOB_IDS)
def test_a_second_click_is_refused_while_the_job_still_holds_the_lock(
        dashboard_server, monkeypatch, op):
    base, token = dashboard_server
    _stub_incident_projection(monkeypatch)
    _apply(monkeypatch, op.reach)
    mod, attr = op.work
    fake, entered, release, calls = _blocking(op.result)
    monkeypatch.setattr(importlib.import_module(mod), attr, fake)

    status, obj = _post(base, op.route, {"project": "test"}, token)
    assert status == 202 and obj["ok"] is True
    assert entered.wait(10), f"{op.route} never reached its work"

    status, refused = _post(base, op.route, {"project": "test"}, token)
    # An expected domain rejection: ok:False with HTTP 200, never 409 and never
    # 500 — unchanged from when these ran inline, so the page reads it the same.
    assert status == 200, f"{op.route} refused with {status}, not 200"
    assert refused["ok"] is False
    assert "already running" in refused["error"], refused

    release.set()
    _await_job(base, token, obj["job"]["id"])
    # THE guarantee: the refusal did not merely answer differently, it stopped
    # the second run from happening at all.
    assert calls == [1], f"{op.route} ran the work {len(calls)} times, expected 1"


@pytest.mark.parametrize("op", JOBS, ids=JOB_IDS)
def test_work_that_raises_is_reported_as_a_failed_job_and_frees_the_lock(
        dashboard_server, monkeypatch, op):
    """The 500 that used to carry this news cannot: the response is long gone by
    the time the work runs. So the failure has to arrive as an OUTCOME — and a
    lock released anywhere but a `finally` would wedge the operation for the life
    of the process the first time anything threw."""
    base, token = dashboard_server
    _stub_incident_projection(monkeypatch)
    _apply(monkeypatch, op.reach)
    mod, attr = op.work
    module = importlib.import_module(mod)

    def boom(*_a, **_kw):
        raise RuntimeError("work exploded")
    monkeypatch.setattr(module, attr, boom)
    status, obj = _post(base, op.route, {"project": "test"}, token)
    assert status == 202 and obj["ok"] is True
    final = _await_job(base, token, obj["job"]["id"])
    assert final["outcome"] == "failed", final
    assert final["worked"] is False
    assert "work exploded" in final["summary"], final

    # The operation still answers, which is what proves the lock came back.
    monkeypatch.setattr(module, attr, lambda *_a, **_kw: op.result)
    status, obj = _post(base, op.route, {"project": "test"}, token)
    assert status == 202, f"{op.route} stayed locked after the work raised"
    _await_job(base, token, obj["job"]["id"])


@pytest.mark.parametrize("op", JOBS, ids=JOB_IDS)
def test_an_operation_with_nothing_to_do_says_so(dashboard_server, monkeypatch,
                                                 op):
    """The empty-work case T13 requires be exercised. `ok: True` could not tell
    "harvested 40 sessions" from "there was nothing to harvest"; these two facts
    now have different names."""
    base, token = dashboard_server
    _stub_incident_projection(monkeypatch)
    empty_result, expected = op.empty
    if empty_result is not None:
        mod, attr = op.work
        monkeypatch.setattr(importlib.import_module(mod), attr,
                            lambda *_a, **_kw: empty_result)
    status, obj = _post(base, op.route, {"project": "test"}, token)
    assert status == 202
    final = _await_job(base, token, obj["job"]["id"])
    assert final["outcome"] == expected, final
    assert final["worked"] is True, "an honest 'nothing to do' is still a success"
    assert final["summary"].strip(), "an outcome with no reason is a dead end"


def test_absent_semantic_extras_are_degraded_not_failed(dashboard_server,
                                                        monkeypatch):
    """The semantic layer is optional by invariant (DATA_MODEL §5), so missing
    extras are a degraded store, not a broken dashboard.

    Its sibling above — the same route raising a plain RuntimeError and having to
    report `failed` — is what stops this from being satisfied by a blanket
    `except RuntimeError`, which would call every crash a missing dependency.
    """
    base, token = dashboard_server

    def missing_extras(*_a, **_kw):
        try:
            raise ImportError("No module named 'chromadb'")
        except ImportError as e:
            raise RuntimeError(
                "semantic layer needs extras: pip install -e .[vectors]") from e

    monkeypatch.setattr(importlib.import_module("mindhive.vectors"), "embed",
                        missing_extras)
    status, obj = _post(base, "/api/embed", {"project": "test"}, token)
    assert status == 202
    final = _await_job(base, token, obj["job"]["id"])
    assert final["outcome"] == "degraded", final
    assert "extras" in final["summary"]
    assert final["worked"] is False


def test_a_job_id_the_server_has_never_heard_of_is_not_a_transport_error(
        dashboard_server):
    """After a restart the page still holds an id from the previous process. The
    honest answer names that, and it is a 200 the page can render."""
    base, token = dashboard_server
    status, obj = _get(base, "/api/job?id=deadbeefdeadbeef", token)
    assert status == 200
    assert obj["ok"] is False
    assert "restarted" in obj["error"]


def test_polling_carries_only_output_that_is_new(dashboard_server, monkeypatch):
    """The whole buffer used to be re-sent on every 2.5s tick (T16). The offset
    the previous poll handed back is what makes the next one incremental."""
    base, token = dashboard_server
    release = threading.Event()

    def chatty(*_a, **_kw):
        chatty.emit("first line\n")
        release.wait(timeout=20)
        chatty.emit("second line\n")
        return {"exit_code": 0, "counts": {}}

    # `selftest.run` receives the job's sink as `emit`; capture it so the fake can
    # write through the same channel the real one does.
    real = importlib.import_module("mindhive.selftest")
    monkeypatch.setattr(real, "run",
                        lambda emit=None, echo=True: (
                            setattr(chatty, "emit", emit), chatty())[1])

    status, obj = _post(base, "/api/selftest", {"project": "test"}, token)
    assert status == 202
    job_id = obj["job"]["id"]

    first = None
    deadline = time.time() + 10
    while time.time() < deadline:
        _, body = _get(base, f"/api/job?id={job_id}&offset=0", token)
        if "first line" in body["job"]["output"]:
            first = body["job"]
            break
        time.sleep(0.05)
    assert first, "the first line never reached the pane"
    release.set()

    later = None
    deadline = time.time() + 10
    while time.time() < deadline:
        _, body = _get(
            base, f"/api/job?id={job_id}&offset={first['offset']}", token)
        if "second line" in body["job"]["output"]:
            later = body["job"]
            break
        time.sleep(0.05)
    assert later, "the second line never arrived"
    assert "first line" not in later["output"], \
        "the poll re-sent output the caller already had"
    _await_job(base, token, job_id)


@pytest.mark.parametrize("held,blocked", [
    ("/api/plugins/scan", "/api/plugins/autoscan"),
    ("/api/plugins/autoscan", "/api/plugins/scan"),
])
def test_the_two_plugin_routes_exclude_each_other(
        dashboard_server, monkeypatch, held, blocked):
    # /api/plugins/autoscan reaches plugins.ensure_fresh, which runs the SAME
    # machine-wide scan /api/plugins/scan runs directly. A lock per route would
    # leave two scans racing over one plugin cache through two different doors,
    # which defeats the whole guarantee — so both share _PLUGINS_LOCK.
    base, token = dashboard_server
    attr = ("scan_and_store" if held == "/api/plugins/scan" else "ensure_fresh")
    result = ({"top": []} if held == "/api/plugins/scan"
              else {"scanned": True, "age_hours": 0.0, "payload": {}})
    plugins = importlib.import_module("mindhive.plugins")
    fake, entered, release, _calls = _blocking(result)
    monkeypatch.setattr(plugins, attr, fake)
    # The route we expect to be REFUSED must never reach its own work.
    other_calls = []
    other = ("ensure_fresh" if attr == "scan_and_store" else "scan_and_store")
    monkeypatch.setattr(plugins, other,
                        lambda *_a, **_kw: other_calls.append(1))

    out: list = []
    first = _fire(base, held, token, out)
    assert entered.wait(10), f"{held} never reached its work"
    status, obj = _post(base, blocked, {"project": "test"}, token)
    assert status == 200 and obj["ok"] is False
    assert "plugin scan is already running" in obj["error"]
    assert other_calls == [], f"{blocked} scanned while {held} was running"
    release.set()
    first.join(20)


def test_unrelated_operations_do_not_block_each_other(dashboard_server,
                                                      monkeypatch):
    # Per-operation locks, not one shared lock. A harvest and a sync touch
    # different files; making them wait on each other would turn two legitimate
    # clicks into a stall for no safety gained.
    base, token = dashboard_server
    fake, entered, release, _calls = _blocking({"projects": {}})
    monkeypatch.setattr(dashboard_jobs, "run_catch_up", fake)
    monkeypatch.setattr(importlib.import_module("mindhive.sync"), "sync",
                        lambda *_a, **_kw: "MEMORY.md")

    out: list = []
    first = _fire(base, "/api/harvest", token, out)
    assert entered.wait(10)
    status, obj = _post(base, "/api/sync", {"project": "test"}, token)
    assert status == 200 and obj["ok"] is True, \
        "a running harvest must not refuse an unrelated sync"
    release.set()
    first.join(20)


def test_hive_mind_project_rebuild_takes_the_same_lock(dashboard_server,
                                                       monkeypatch):
    # /api/project in HIVE MIND mode is answered inside do_POST and RETURNS
    # before _POST_ROUTES dispatch, so it never passes through the decorated
    # _post_project. It was therefore the one remaining door into
    # projector.project() the decorator could not see — and the widest one,
    # since it rebuilds EVERY namespace rather than one. A decorator-shaped
    # audit of the handlers alone would report this endpoint as locked.
    base, token = dashboard_server
    fake, entered, release, calls = _blocking({"wrote": []})
    monkeypatch.setattr(importlib.import_module("mindhive.projector"),
                        "project", fake)
    # One namespace, so "the work ran once" is unambiguous.
    monkeypatch.setattr(dashboard, "_all_projects", lambda: ["test"])

    out: list = []
    first = threading.Thread(
        target=lambda: out.append(_post(base, "/api/project",
                                        {"project": "*"}, token)))
    first.start()
    assert entered.wait(10), \
        "the HIVE MIND branch never reached projector.project"

    # Both doors must be refused while the rebuild is in flight: the concrete
    # single-project one (which goes through the decorator) ...
    status, obj = _post(base, "/api/project", {"project": "test"}, token)
    assert status == 200 and obj["ok"] is False, \
        "a single-project rebuild ran while a HIVE MIND rebuild held the lock"
    assert "already running" in obj["error"]

    # ... and a second HIVE MIND one (which does not).
    status, obj = _post(base, "/api/project", {"project": "*"}, token)
    assert status == 200 and obj["ok"] is False, \
        "a second HIVE MIND rebuild ran concurrently with the first"
    assert "already running" in obj["error"]

    release.set()
    first.join(20)
    assert out and out[0][0] == 200 and out[0][1]["ok"] is True
    assert calls == [1], \
        f"projector.project ran {len(calls)} times, expected 1"


def test_the_hive_mind_branch_releases_its_lock_when_the_rebuild_raises(
        dashboard_server, monkeypatch):
    # The all-mode branch acquires OUTSIDE the decorator, so it needs its own
    # `finally`. Without one, the first failing HIVE MIND rebuild wedges every
    # projection — both doors — for the life of the process.
    base, token = dashboard_server
    projector_mod = importlib.import_module("mindhive.projector")

    def boom(*_a, **_kw):
        raise RuntimeError("projection exploded")
    monkeypatch.setattr(projector_mod, "project", boom)
    monkeypatch.setattr(dashboard, "_all_projects", lambda: ["test"])
    status, obj = _post(base, "/api/project", {"project": "*"}, token)
    assert status == 500 and obj["ok"] is False   # do_POST's unexpected-fault path

    monkeypatch.setattr(projector_mod, "project", lambda *_a, **_kw: {"wrote": []})
    status, obj = _post(base, "/api/project", {"project": "*"}, token)
    assert status == 200 and obj["ok"] is True, \
        "HIVE MIND stayed locked after the rebuild raised"
    # The decorated single-project door must be free too — same lock object.
    status, obj = _post(base, "/api/project", {"project": "test"}, token)
    assert status == 200 and obj["ok"] is True, \
        "the single-project door stayed locked after a HIVE MIND rebuild raised"


# --- T6: an unchanged ledger does no work ----------------------------------

class _Counter:
    """Call-counting pass-through. The plan's stated verification for T6 is
    that an unchanged ledger performs NO read_all and NO fold, which is a claim
    about calls — not about elapsed time, which would be a flaky proxy."""

    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return self.fn(*a, **kw)


@pytest.fixture
def counters(monkeypatch):
    """Counters over the two costs the gate exists to skip, installed but not
    yet armed — call `.reset()` after warming the memo."""
    ev_mod = importlib.import_module("mindhive.events")
    tel_mod = importlib.import_module("mindhive.mindnet.telemetry")
    reads, folds = _Counter(ev_mod.read_all), _Counter(tel_mod.fold)
    monkeypatch.setattr(ev_mod, "read_all", reads)
    monkeypatch.setattr(tel_mod, "fold", folds)

    class _Pair:
        def reset(self):
            reads.calls = folds.calls = 0
    pair = _Pair()
    pair.reads, pair.folds = reads, folds
    return pair


def _seed(session_id="sess-1"):
    """One real interactive session plus one awake faculty, so both folds have
    something to produce and an empty board cannot pass a test by accident."""
    events.append_event("user_prompt", {"text": "do the thing"},
                        session_id=session_id, project="test")
    events.append_event("mindnet_activity", {"faculty": "sop", "status": "active"},
                        session_id=session_id, project="test")


# `?project=test` on every poll, deliberately: without it do_GET calls
# default_project(), which reads EVERY namespace's log to pick one — a read_all
# the gate is not responsible for and that would make the counts unreadable.
CATS = "/api/mindnet/cats?project=test"


def test_an_unchanged_ledger_reads_nothing_and_folds_nothing(
        dashboard_server, counters):
    base, token = dashboard_server
    _seed()
    status, warm = _get(base, CATS, token)      # miss: warms the memo
    assert status == 200
    assert counters.reads.calls >= 1 and counters.folds.calls >= 1, \
        "the first poll must actually do the work"

    counters.reset()
    status, again = _get(base, CATS, token)
    assert status == 200
    assert counters.reads.calls == 0, "an unchanged ledger was read anyway"
    assert counters.folds.calls == 0, "an unchanged ledger was folded anyway"
    # Shape and content are unchanged, not merely cheap — a gate that served a
    # different payload would be a regression dressed as a speedup.
    assert set(again) == set(warm)
    assert again["cats"] == warm["cats"]
    assert again["producers"] == warm["producers"]
    assert [c["session_id"] for c in again["sessions"]] == \
           [c["session_id"] for c in warm["sessions"]]


def test_appending_an_event_invalidates_the_gate(dashboard_server, counters):
    base, token = dashboard_server
    _seed()
    _get(base, CATS, token)
    counters.reset()
    _get(base, CATS, token)
    assert counters.folds.calls == 0             # gate holds while nothing moves

    events.append_event("mindnet_activity",
                        {"faculty": "history", "status": "active"},
                        session_id="sess-1", project="test")
    counters.reset()
    _status, body = _get(base, CATS, token)
    assert counters.reads.calls == 1, "an appended event must re-read the ledger"
    assert counters.folds.calls == 1, "an appended event must re-fold"
    assert {c["faculty"] for c in body["cats"]} == {"sop", "history"}, \
        "the new event must reach the board"


def test_an_empty_fingerprint_never_matches_and_never_freezes_the_board(
        dashboard_server, counters, monkeypatch):
    # segment_stats answers {} for a missing events dir AND for ANY OSError. If
    # {} == {} counted as a hit, one transient directory error would pin this
    # board's payload for the life of the process — silently, which is the one
    # failure mode a cache must not have.
    base, token = dashboard_server
    _seed()
    _get(base, CATS, token)                      # warm with a real fingerprint
    sidecar = importlib.import_module("mindhive.mindnet.sidecar")
    monkeypatch.setattr(sidecar, "segment_stats", lambda *_a, **_kw: {})

    for attempt in (1, 2):
        counters.reset()
        status, body = _get(base, CATS, token)
        assert status == 200
        assert counters.reads.calls == 1, \
            f"poll {attempt} served an empty fingerprint from the memo"
        assert counters.folds.calls == 1
        assert {c["faculty"] for c in body["cats"]} == {"sop"}


def test_a_non_file_event_backend_bypasses_the_gate(
        dashboard_server, counters, monkeypatch):
    """The second mandated guard, which did not ship the first time.

    `events.read_all` short-circuits to Postgres when MINDHIVE_EVENT_BACKEND is
    `pg` (events.py:314, :356), while `segment_stats` keeps stat'ing the local
    .jsonl segments. On the documented file -> dual -> pg migration path those
    segments are still on disk and never change again, so the fingerprint would
    be non-empty and stable forever, every poll after the first a hit, and the
    board frozen at its first response for the life of the process — silently.

    `_maybe_read_from_pg` is stubbed to None so `read_all` still answers from
    the file segments: the only variable under test is the gate's decision, not
    whether a Postgres server happens to be reachable from this machine.
    """
    base, token = dashboard_server
    _seed()
    status, warm = _get(base, CATS, token)          # miss: warms the memo
    assert status == 200

    ev_mod = importlib.import_module("mindhive.events")
    monkeypatch.setattr(ev_mod, "_maybe_read_from_pg", lambda project=None: None)
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "pg")

    counters.reset()
    status, again = _get(base, CATS, token)
    assert status == 200
    assert counters.reads.calls >= 1, (
        "a non-file backend served the memo instead of reading: segment_stats "
        "fingerprints local segments that pg never writes, so the gate can "
        "never invalidate and the board freezes for the life of the process")
    assert set(again) == set(warm), "the bypass changed the payload shape"


def test_hive_mind_bypasses_the_gate(dashboard_server, counters):
    # All-projects mode reads EVERY namespace via all_event_pairs, but
    # segment_stats is per-project — a single-project stamp would miss an append
    # in any other namespace. The bypass is the guard.
    base, token = dashboard_server
    _seed()
    _get(base, "/api/mindnet/cats?project=*", token)
    counters.reset()
    status, _body = _get(base, "/api/mindnet/cats?project=*", token)
    assert status == 200
    assert counters.folds.calls >= 1, "HIVE MIND must not be served from the memo"


class _FakeTime:
    """The real `time` module with a movable `time()`. Substituted for
    `dashboard_get`'s module-level `time` — the module that stamps the response —
    so a cache HIT can be observed across a clock change without the test
    sleeping."""

    def __init__(self, start):
        self.now = start

    def time(self):
        return self.now

    def __getattr__(self, name):          # everything else stays real
        return getattr(time, name)


def test_a_cache_hit_still_ages_the_wall_clock_fields(dashboard_server,
                                                      counters, monkeypatch):
    # The trap the whole design is built around: `now` and `online` are
    # functions of the CLOCK, not of the events. Memoizing the response instead
    # of the folds would freeze a finished session as working and a stuck
    # faculty as fresh, on a board whose only job is to say which is which.
    base, token = dashboard_server
    _seed()
    clock = _FakeTime(time.time())
    monkeypatch.setattr(dashboard_get, "time", clock)

    _status, warm = _get(base, CATS, token)
    assert [c["display"] for c in warm["cats"]] == ["active"]
    assert [c["display"] for c in warm["sessions"]] == ["working"]

    counters.reset()
    clock.now += 200                    # past DIM_AFTER_SECS and ONLINE_WINDOW
    _status, aged = _get(base, CATS, token)
    assert counters.reads.calls == 0 and counters.folds.calls == 0, \
        "the ledger did not move, so this must be a cache hit"
    assert aged["cats"][0]["display"] == "dim", "a cached fold froze the cat age"
    assert aged["cats"][0]["age_secs"] >= 200
    assert aged["sessions"][0]["display"] == "resting", \
        "a cached fold froze `online`"


# --- the one GET that belongs with the POSTs -------------------------------

def test_a_second_chain_verification_is_refused_while_the_first_runs(
        dashboard_server, monkeypatch):
    """`/api/verify` reads every event and recomputes the hash chain — as
    expensive as most of the POSTs above — and it is a GET, so the decorator
    that guards them did not fit it and the nav disable was its only protection.
    T9 removed that. One double-click then started two full chain recomputations
    over the same ledger.

    The decorator now takes `*a` rather than `(proj, body)` for exactly this: a
    read-only endpoint still deserves the guard when the READ is the expensive
    part."""
    base, token = dashboard_server
    fake, entered, release, calls = _blocking({"ok": True})
    monkeypatch.setattr(importlib.import_module("mindhive.chain"), "verify", fake)

    out: list = []
    first = threading.Thread(
        target=lambda: out.append(_get(base, "/api/verify?project=test", token)))
    first.start()
    assert entered.wait(10), "/api/verify never reached chain.verify"

    status, obj = _get(base, "/api/verify?project=test", token)
    assert status == 200, f"the refusal came back as {status}, not 200"
    assert obj["ok"] is False and "already running" in obj["error"]

    release.set()
    first.join(20)
    assert out and out[0][0] == 200 and out[0][1]["ok"] is True
    assert calls == [1], \
        f"chain.verify ran {len(calls)} times — the refusal answered but did not stop it"


def _get_any(base, path, token):
    """`_get` lets urllib raise on a 500; do_GET's unexpected-fault path IS part
    of the contract here, so this one decodes it instead."""
    sep = "&" if "?" in path else "?"
    try:
        with urllib.request.urlopen(f"{base}{path}{sep}token={token}",
                                    timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_the_verify_lock_is_released_when_the_read_raises(dashboard_server,
                                                          monkeypatch):
    base, token = dashboard_server
    chain_mod = importlib.import_module("mindhive.chain")

    def boom(*_a, **_kw):
        raise RuntimeError("ledger exploded")
    monkeypatch.setattr(chain_mod, "verify", boom)
    status, obj = _get_any(base, "/api/verify?project=test", token)
    assert status == 500 and obj["ok"] is False

    monkeypatch.setattr(chain_mod, "verify", lambda *_a, **_kw: {"ok": True})
    status, obj = _get(base, "/api/verify?project=test", token)
    assert status == 200 and obj["ok"] is True, \
        "/api/verify stayed locked after the read raised"


# --- the memos are bounded --------------------------------------------------

def test_both_memos_evict_instead_of_growing_with_the_namespace_count():
    """Process-global, server-lifetime, one entry per project ever polled — each
    holding a full telemetry fold and every session record for that project.
    `hive projects` reports 56 stores on this machine, and the picker plus HIVE
    MIND make visiting many of them a normal afternoon.

    LRU rather than a plain cap because the access pattern is extreme: one
    namespace is hot and the rest go cold the moment the picker moves. The
    re-read below is what proves it is an LRU and not a FIFO — a hot key read
    on every pass must survive an eviction sweep that is longer than the bound.
    """
    for memo in (dashboard._CATS_MEMO, dashboard._SESSIONS_MEMO):
        memo.clear()
        dashboard._memo_put(memo, "hot", "kept")
        for i in range(dashboard._MEMO_MAX * 3):
            dashboard._memo_put(memo, f"cold-{i}", i)
            assert dashboard._memo_get(memo, "hot") == "kept", (
                f"the hot key was evicted after {i + 1} cold inserts — this is a "
                f"FIFO, not an LRU, and the entry every poll touches is the one "
                f"it drops")
        assert len(memo) <= dashboard._MEMO_MAX, (
            f"{len(memo)} entries retained against a bound of {dashboard._MEMO_MAX}")
        assert dashboard._memo_get(memo, "cold-0") is None, \
            "the oldest cold entry survived a full sweep, so nothing is evicting"
        memo.clear()


# --- T5: the project picker stops re-reading every ledger on disk ------------
#
# `default_project()` is a loop over `_project_stats`, so ONE dashboard launch
# read every namespace's full log — 56 of them on this machine — and `/api/
# projects` did it again on every poll of the picker.
#
# `_SESSIONS_MEMO` is the wrong precedent and the reason `_fingerprinted` exists
# separately: it stamps `(len(evs), evs[-1].ts)`, which needs `evs`, so by the
# time it can answer, the read it was meant to avoid has already happened — it
# memoizes the FOLD. `segment_stats` is `os.scandir` over size and mtime: it
# answers "may I skip the read?" without a read.
#
# The two guards are the whole test surface, and the naive "read_all is not
# called twice" test passes in BOTH broken states, so on its own it proves
# nothing: `{} == {}` scores a hit, and a pg backend leaves the local segments
# frozen. Each guard therefore gets a test whose failure mode is the specific
# thing that goes wrong — a cache that can never invalidate again.

PROJECTS = "/api/projects?project=test"


def _seed_other(session_id="sess-other"):
    """A second namespace, so "one project was re-read" is distinguishable from
    "everything was re-read" — per-project invalidation is the whole claim."""
    events.append_event("user_prompt", {"text": "elsewhere"},
                        session_id=session_id, project="other")


def _stats_for(body, name):
    for row in body["projects"]:
        if row["name"] == name:
            return row
    raise AssertionError(f"{name} missing from the picker: {body['projects']}")


def test_an_unchanged_picker_reads_no_ledger_at_all(dashboard_server, counters):
    base, token = dashboard_server
    _seed()
    _seed_other()
    status, warm = _get(base, PROJECTS, token)
    assert status == 200
    assert counters.reads.calls >= 2, \
        "the first poll must actually read both namespaces"

    counters.reset()
    status, again = _get(base, PROJECTS, token)
    assert status == 200
    assert counters.reads.calls == 0, \
        f"an unchanged store was read {counters.reads.calls}x anyway"
    # Cheap AND identical: a memo that served a different picker would be a
    # regression dressed as a speedup.
    assert again["projects"] == warm["projects"]
    assert again["hivemind"] == warm["hivemind"]
    assert again["default"] == warm["default"]


def test_an_append_re_reads_that_project_and_only_that_project(
        dashboard_server, counters):
    """The invalidation that matters comes from OUTSIDE this process — the CLI, a
    hook, `hive loop`. A real event goes into the real segment file, so the stat
    the memo depends on moves exactly the way it does in production."""
    base, token = dashboard_server
    _seed()
    _seed_other()
    _status, warm = _get(base, PROJECTS, token)
    before = _stats_for(warm, "test")["events"]

    events.append_event("decision", {"text": "an append from outside"},
                        session_id="sess-1", project="test")
    counters.reset()
    status, after = _get(base, PROJECTS, token)
    assert status == 200
    assert counters.reads.calls == 1, (
        f"{counters.reads.calls} ledgers re-read for an append to one of them — "
        f"the fingerprint is not per-project")
    assert _stats_for(after, "test")["events"] == before + 1, \
        "the appended event never reached the picker"
    assert _stats_for(after, "other") == _stats_for(warm, "other")


def _seed_namespaces(n):
    """*n* distinct namespaces with one event each, named so they sort stably.

    `_all_projects()` returns sorted names and BOTH loops in `/api/projects`
    walk that whole list in that order — a sequential scan, which is the access
    pattern an LRU is worst at."""
    names = [f"ns-{i:02d}" for i in range(n)]
    for name in names:
        events.append_event("user_prompt", {"text": "hello"},
                            session_id="sess-1", project=name)
    return names


def test_the_picker_caches_every_namespace_it_walks(dashboard_server, counters):
    """The memo has to hold one entry per NAMESPACE, not per `_MEMO_MAX`.

    `_get_projects` calls `_project_stats` for every name `_all_projects()`
    returns, and `default_project()` — called in the same handler — loops the
    same list again. With more namespaces than slots, one pass writes an entry
    for each and evicts all but the last few, and because the next pass starts
    again at the name evicted earliest, EVERY key misses. The memo stayed full,
    the hit rate was zero, and nothing reported it.

    Measured on the real store (60 namespaces, 8 slots) before this test
    existed: three identical passes cost 1329 / 1214 / 2134 ms with 8 entries
    retained after each, while one hot namespace read twice went 1207 -> 4.0 ms.
    The gate worked; it was the bound that made it unreachable."""
    base, token = dashboard_server
    names = _seed_namespaces(dashboard._MEMO_MAX + 4)
    status, warm = _get(base, PROJECTS, token)
    assert status == 200
    # CANARY for the assertion below: `reads == 0` is also what a picker that
    # stopped looking at the store at all would produce. The first poll must be
    # seen doing the real work, over every namespace seeded.
    assert counters.reads.calls >= len(names), (
        f"the first poll read {counters.reads.calls} ledgers for "
        f"{len(names)} namespaces — it never did the work being cached")
    assert {row["name"] for row in warm["projects"]} >= set(names), \
        "the picker did not list the namespaces this test seeded"

    counters.reset()
    status, again = _get(base, PROJECTS, token)
    assert status == 200
    assert counters.reads.calls == 0, (
        f"an unchanged store was re-read {counters.reads.calls}x across "
        f"{len(names)} namespaces — the memo evicts entries it is about to "
        f"need, so the second pass costs the same as the first")
    assert len(dashboard._STATS_MEMO) == len(names), (
        f"{len(dashboard._STATS_MEMO)} entries retained after walking "
        f"{len(names)} namespaces — the bound, not the fingerprint, is what "
        f"drops them")
    assert again["projects"] == warm["projects"], \
        "the cheap poll served a different picker than the expensive one"


def test_the_fold_holding_memos_keep_their_own_small_bound():
    """DISCRIMINATING, and the reason the test above is not fixed by raising
    `_MEMO_MAX`.

    The bound is shared by three memos with entries that differ by orders of
    magnitude. `_STATS_MEMO` holds a count dict and a stat fingerprint;
    `_SESSIONS_MEMO` and `_CATS_MEMO` each hold a full telemetry fold plus
    every session record for that project, which is the size the small cap was
    chosen for. Raising one number would pass the picker test by making the two
    expensive memos hold 60 folds instead of 8."""
    assert dashboard._MEMO_MAX <= 16, (
        f"the fold-holding memos are bounded at {dashboard._MEMO_MAX}; their "
        f"entries each carry a full telemetry fold, so this is not the knob "
        f"the picker's namespace count may turn")
    for memo in (dashboard._CATS_MEMO, dashboard._SESSIONS_MEMO):
        memo.clear()
        for i in range(dashboard._MEMO_MAX * 3):
            dashboard._memo_put(memo, f"k-{i}", i)
        assert len(memo) <= dashboard._MEMO_MAX, (
            f"{len(memo)} folds retained against a bound of "
            f"{dashboard._MEMO_MAX}")
        memo.clear()

    dashboard._STATS_MEMO.clear()
    wanted = dashboard._MEMO_MAX * 3
    for i in range(wanted):
        dashboard._memo_put(dashboard._STATS_MEMO, f"ns-{i}", i)
    assert len(dashboard._STATS_MEMO) == wanted, (
        f"{len(dashboard._STATS_MEMO)} of {wanted} picker entries survived — "
        f"the bound travels with the memo, or every call site has to remember "
        f"which one it is writing to")
    dashboard._STATS_MEMO.clear()


def test_the_picker_memo_still_evicts_at_its_own_bound():
    """`_STATS_MEMO_MAX` is a real ceiling, not "unbounded with a number on it".

    The bound this change RAISED is the same one whose smallness caused the
    defect, so the ceiling deserves a guard of its own. A store with more
    namespaces than `_STATS_MEMO_MAX` returns to the sequential-scan pathology
    exactly as before — 0% hit rate, memo full, nothing reported — and the
    number is the only thing standing between here and there.

    Two directions are asserted because each fails differently: eviction that
    never fires makes the memo grow without limit on a pathological store, and
    eviction that fires early is the original bug at a larger number."""
    memo = dashboard._BoundedMemo(dashboard._STATS_MEMO_MAX)
    for i in range(dashboard._STATS_MEMO_MAX + 4):
        dashboard._memo_put(memo, f"ns-{i:04d}", i)
    assert len(memo) == dashboard._STATS_MEMO_MAX, (
        f"{len(memo)} entries retained against a bound of "
        f"{dashboard._STATS_MEMO_MAX} — the picker's memo either stopped "
        f"evicting or evicts before its own ceiling")
    assert dashboard._memo_get(memo, "ns-0000") is None, \
        "the oldest entry survived past the bound, so nothing is evicting"
    assert dashboard._memo_get(memo, f"ns-{dashboard._STATS_MEMO_MAX + 3:04d}") \
        is not None, "the newest entry was evicted, so this is a FIFO backwards"
    # The ceiling has to clear a real machine's namespace count by a margin, or
    # it is the old bug with a bigger number. 60 namespaces here when this was
    # written; `hive projects` is the live count.
    assert dashboard._STATS_MEMO_MAX >= 256, (
        f"_STATS_MEMO_MAX is {dashboard._STATS_MEMO_MAX} — too close to a "
        f"plausible namespace count to be a backstop")


def test_a_memo_carrying_no_bound_falls_back_to_the_small_one():
    """The `getattr(memo, "max_entries", _MEMO_MAX)` default, exercised.

    Every memo the server owns is a `_BoundedMemo`, so the fallback branch is
    unreachable from production code and every other test in this file passes a
    bounded memo — which means the branch would be dead in coverage terms and
    the comment above it would be documenting a behaviour nothing verifies.

    The direction matters, not just the number: an unrecognised memo must land
    on the SMALL bound. Over-caching an unknown entry shape costs memory, which
    is the failure worth defaulting away from; under-caching costs time, which
    is the one the whole `_STATS_MEMO_MAX` split exists to buy back."""
    plain = collections.OrderedDict()
    assert not hasattr(plain, "max_entries"), \
        "a plain OrderedDict grew a bound, so this no longer tests the default"
    for i in range(dashboard._STATS_MEMO_MAX + 4):
        dashboard._memo_put(plain, f"k-{i}", i)
    assert len(plain) == dashboard._MEMO_MAX, (
        f"{len(plain)} entries retained in a memo carrying no bound — the "
        f"fallback must be `_MEMO_MAX` ({dashboard._MEMO_MAX}), not "
        f"`_STATS_MEMO_MAX` ({dashboard._STATS_MEMO_MAX}) and not unbounded")


def test_an_empty_fingerprint_never_matches_and_never_freezes_the_picker(
        dashboard_server, counters, monkeypatch):
    """DISCRIMINATING. `segment_stats` answers `{}` for ANY OSError as well as
    for "no events directory". Fail-safe for the sidecar, INVERTED as a cache
    key: `{} == {}` scores a hit, so one transient directory error would pin this
    picker's counts for the life of the process — silently, which is the one
    failure mode a cache must not have."""
    base, token = dashboard_server
    _seed()
    _get(base, PROJECTS, token)                  # warm with a real fingerprint
    sidecar = importlib.import_module("mindhive.mindnet.sidecar")
    monkeypatch.setattr(sidecar, "segment_stats", lambda *_a, **_kw: {})

    for attempt in (1, 2):
        counters.reset()
        status, body = _get(base, PROJECTS, token)
        assert status == 200
        assert counters.reads.calls >= 1, \
            f"poll {attempt} served an empty fingerprint from the memo"
        assert _stats_for(body, "test")["events"] == 2
    # The warm entry from before the fault is still there and is harmless — while
    # the fingerprint is untrusted, `_fingerprinted` returns before it consults
    # the memo at all. What must NOT have happened is `{}` being written as a
    # stamp: that is the value a later poll could match against.
    stored, _stats = dashboard._STATS_MEMO["test"]
    assert stored, "an empty fingerprint was stored as a key a later poll can hit"


def test_a_non_file_event_backend_bypasses_the_stats_memo(
        dashboard_server, counters, monkeypatch):
    """DISCRIMINATING, and the same gap that shipped the first time on the cats
    gate. `events.read_all` short-circuits to Postgres when the backend is `pg`,
    while `segment_stats` keeps stat'ing the local `.jsonl` segments — which, on
    the documented file → dual → pg path, are still on disk and never change
    again. Non-empty fingerprint, stable forever, every poll a hit, the picker
    frozen at its first response.

    `_maybe_read_from_pg` is stubbed to None so `read_all` still answers from the
    file segments: the variable under test is the memo's decision, not whether a
    Postgres server happens to be reachable from this machine."""
    base, token = dashboard_server
    _seed()
    _status, warm = _get(base, PROJECTS, token)

    ev_mod = importlib.import_module("mindhive.events")
    monkeypatch.setattr(ev_mod, "_maybe_read_from_pg", lambda project=None: None)
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "pg")

    counters.reset()
    status, again = _get(base, PROJECTS, token)
    assert status == 200
    assert counters.reads.calls >= 1, (
        "a non-file backend served the memo instead of reading: segment_stats "
        "fingerprints local segments that pg never writes, so the memo can "
        "never invalidate and the picker freezes for the life of the process")
    assert again["projects"] == warm["projects"], \
        "the bypass changed the picker's contents"


def test_the_fingerprint_is_a_real_stamp_when_it_can_be_trusted():
    """CANARY for both guards above. They assert an ABSENCE of caching, which a
    `_ledger_fingerprint` that returned None unconditionally would also produce —
    and that version would be a cache that never caches, passing every test
    above while restoring the original 56-read launch."""
    _seed()
    trusted = dashboard._ledger_fingerprint("test")
    assert trusted, "a file-backend namespace produced no fingerprint at all"
    assert trusted == dashboard._ledger_fingerprint("test"), \
        "two stats of one unchanged ledger disagreed, so nothing can ever hit"
    events.append_event("decision", {"text": "moves the stamp"},
                        session_id="sess-1", project="test")
    assert dashboard._ledger_fingerprint("test") != trusted, \
        "an appended event did not move the stamp"


# --- the gate in front of /api/improvements ---------------------------------
# The last polled route that read the WHOLE ledger on every tick. Measured on
# the real store before the gate: 415 ms of `read_all` over 44951 events plus
# 118 ms of `summaries` over 155 signatures, and `renderPlugins` asks for this
# route on every 2.5 s poll while Skills is open. A/B on the same store, four
# identical requests, byte-identical 438886-byte response either way: 562 ms per
# repeat without the gate, 107 ms with it.
#
# Its fingerprint has a term the cats gate does not need — the incident RECORDS
# on disk — because `incidents.summaries` reads them and a reflection pass
# rewrites one with no event appended.

IMPROVE = "/api/improvements?project=test"


def _seed_lesson(project="test", signature="pymodule:mindhive"):
    """One real incident record plus one caveat, so both halves of the response
    have content and an empty payload cannot pass a test by accident."""
    incidents = importlib.import_module("mindhive.incidents")
    for i in range(2):
        events.append_event("tool_failed",
                            {"text": f"err {signature}", "signature": signature,
                             "action": f"cmd{i}"},
                            session_id=f"s{i}", tags=["auto", "failure"],
                            project=project)
    incidents.audit(project=project)
    incidents.project(project=project)
    events.append_event("caveat", {"text": "never embed while a trade is open"},
                        session_id="s9", project=project)
    return incidents


def test_an_unchanged_improvements_store_is_never_read_again(dashboard_server,
                                                             counters):
    base, token = dashboard_server
    _seed_lesson()
    status, warm = _get(base, IMPROVE, token)
    assert status == 200
    assert counters.reads.calls >= 1, "the first poll must actually do the work"
    assert warm["auto"] and warm["manual"],         "the fixture produced nothing, so a cheap second poll would prove nothing"

    counters.reset()
    status, again = _get(base, IMPROVE, token)
    assert status == 200
    assert counters.reads.calls == 0, "an unchanged ledger was read anyway"
    # Cheap AND the same: a gate that served a different payload would be a
    # regression dressed as a speedup.
    assert again == warm


def test_appending_a_caveat_invalidates_the_improvements_gate(dashboard_server,
                                                              counters):
    base, token = dashboard_server
    _seed_lesson()
    _get(base, IMPROVE, token)
    counters.reset()
    _get(base, IMPROVE, token)
    assert counters.reads.calls == 0             # the gate holds while nothing moves

    events.append_event("caveat", {"text": "the second lesson, learned later"},
                        session_id="s9", project="test")
    counters.reset()
    _status, body = _get(base, IMPROVE, token)
    assert counters.reads.calls == 1, "an appended event must re-read the ledger"
    assert any("learned later" in m["text"] for m in body["manual"]),         "the new caveat never reached the panel"


def test_rewriting_an_incident_record_invalidates_the_improvements_gate(
        dashboard_server, counters):
    """The term a ledger-only stamp cannot see, and the reason the fingerprint
    is compound. `incidents.summaries` reads `rules/incidents/*.md`; a reflection
    pass, a resolution or a decay rewrites one of those files with NO event
    appended, so a ledger-only gate would serve the old verdict for the life of
    the process.

    CANARY by MUTATION at the module seam: with `_incidents_fingerprint` pinned
    to a constant — the ledger-only gate this test exists to forbid — the very
    same rewrite must go unnoticed. Without that half, a test that merely sees
    the new text would also pass against a gate that never caches at all.
    """
    import pytest as _pytest
    base, token = dashboard_server
    _seed_lesson()
    _get(base, IMPROVE, token)                   # warm

    record = next(iter(sorted(
        (paths.project_dir("test") / "rules" / "incidents").glob("*.md"))))
    original = record.read_text(encoding="utf-8")
    # The Human Evaluation placeholder is what an operator (or `hive rules
    # resolve`) replaces, and `derive_active` reads it straight back out as the
    # record's active rule — a real edit to a real field, not a synthetic one.
    placeholder = "<!-- leave blank to accept Auto; fill to override -->"
    assert original.count(placeholder) == 1, \
        f"the record has no Human Evaluation placeholder:\n{original[:400]}"

    def rewrite(verdict):
        record.write_text(original.replace(placeholder, verdict), encoding="utf-8")

    rewrite("REFLECTED-LATER")
    counters.reset()
    _status, body = _get(base, IMPROVE, token)
    assert counters.reads.calls == 1, \
        "a rewritten incident record must invalidate the memo"
    assert any(a.get("active_rule") == "REFLECTED-LATER" for a in body["auto"]), \
        f"the rewrite never reached the panel: {[a.get('active_rule') for a in body['auto']]}"
    assert any(a.get("rule_source") == "human" for a in body["auto"])

    # The canary. A gate stamped on the ledger alone cannot see this rewrite.
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(dashboard_memo, "_incidents_fingerprint",
                   lambda _proj: ("pinned",))
        _get(base, IMPROVE, token)               # warm under the pinned stamp
        rewrite("NEVER-SEEN")
        counters.reset()
        _status, stale = _get(base, IMPROVE, token)
        assert counters.reads.calls == 0, \
            "the pinned-stamp control did not even score a cache hit"
        assert not any(a.get("active_rule") == "NEVER-SEEN" for a in stale["auto"]), \
            ("a ledger-only stamp saw the rewrite anyway, so the test above "
             "proves nothing about the incidents term")


def test_hive_mind_improvements_is_gated_one_namespace_at_a_time(dashboard_server,
                                                                 counters):
    """Unlike /api/mindnet/cats, HIVE MIND here is NOT a bypass. The fold is
    per-project and so is its fingerprint, so the unified view is the sum of
    per-namespace hits — which is what makes the all-projects poll affordable at
    all, since it is the mode that reads every ledger on the machine."""
    base, token = dashboard_server
    _seed_lesson("test")
    _seed_lesson("other", signature="pymodule:other")

    status, warm = _get(base, "/api/improvements?project=*", token)
    assert status == 200
    assert counters.reads.calls >= 2, "both namespaces must be read the first time"
    assert {a["project"] for a in warm["auto"]} == {"test", "other"}

    counters.reset()
    status, again = _get(base, "/api/improvements?project=*", token)
    assert status == 200
    assert counters.reads.calls == 0, \
        "HIVE MIND re-read every namespace although none of them moved"
    assert again == warm


def test_the_efficiency_tiles_follow_the_clock_across_a_cache_hit(
        dashboard_server, counters, monkeypatch):
    """The trap this memo is shaped around. The lessons are a function of the
    STORE; the efficiency tiles are a function of the CLOCK — their windows are
    cut against `now`. Caching the response instead of the fold would freeze the
    window boundaries at whatever they were when the store last moved, so a board
    left open overnight would go on reporting yesterday's "last 24h" as today's,
    with no event able to correct it."""
    base, token = dashboard_server
    _seed_lesson()
    clock = _FakeTime(time.time())
    monkeypatch.setattr(dashboard_get, "time", clock)

    _status, warm = _get(base, IMPROVE, token)
    day = warm["stats"]["day"]
    assert day["cur_actions"] >= 2, \
        "no actions landed in the current day window, so ageing it proves nothing"

    counters.reset()
    clock.now += 3 * 86400              # the seeded failures fall out of `day`
    _status, aged = _get(base, IMPROVE, token)
    assert counters.reads.calls == 0, \
        "the store did not move, so this must have been a cache hit"
    assert aged["stats"]["day"]["cur_actions"] == 0, \
        "a cached response froze the efficiency window against the clock"
    assert aged["auto"] == warm["auto"], "the lessons are not clock-dependent"


# --- the four polled routes that read the WHOLE ledger on every tick ---------
#
# The shell polls the live section's endpoint every 2500 ms. Four of those
# endpoints had no gate at all. Measured on the real store (45k events, 60
# namespaces), three identical requests each, cold / warm / warm2 in ms:
#
#     /api/changelog   450 / 426 / 407   ->   386 /  37 /   4
#     /api/sops        427 / 425 / 433   ->   408 /   3 /   3
#     /api/memory      467 / 477 / 482   ->   437 /  52 /  36
#     /api/sessions    464 / 457 / 444   ->   485 /  13 /  52
#
# Warm costing the same as cold is the signature of no gate: the whole
# `events.read_all` ran again for a fold whose inputs had not moved. Each answer
# was compared byte for byte against the ungated fold afterwards, single project
# AND HIVE MIND — which is how the sessions list turned out to need one stable
# sort on `first_ts` to reproduce its old tie order: without it, two sessions
# sharing an exact `last_ts` swapped at row 508 of 2101.
#
# `?project=test` on every path for the reason `CATS` gives above: without it
# do_GET calls default_project(), which reads EVERY namespace and would drown
# the counts the gate is being measured by.

POLLED_GATES = [
    ("changelog", "/api/changelog?project=test",
     lambda t: events.append_event("code_change",
                                   {"category": "update", "text": "seeded " + t},
                                   session_id="s-" + t, project="test"),
     lambda b: b["entries"]),
    ("sops", "/api/sops?project=test",
     lambda t: events.append_event("sop",
                                   {"action": "publish", "sop_id": "sop-" + t,
                                    "title": "t " + t, "text": "body " + t},
                                   session_id="s-" + t, project="test"),
     lambda b: b["history"]),
    ("memory", "/api/memory?project=test",
     lambda t: events.append_event("decision", {"text": "seeded decision " + t},
                                   session_id="s-" + t, project="test"),
     lambda b: b),
    ("sessions", "/api/sessions?project=test",
     lambda t: events.append_event("user_prompt", {"text": "seeded prompt " + t},
                                   session_id="s-" + t, project="test"),
     lambda b: b),
    # Not on the 2.5 s poll — the Skills board polls /api/plugins/inventory —
    # but both of these ran a full `read_all` on every visit to the section, and
    # `/api/skills/status` spent 378 ms of it to return 28 bytes.
    ("skills-status", "/api/skills/status?project=test",
     lambda t: events.append_event("invoke_result", {"name": "skill-" + t},
                                   session_id="s-" + t, project="test"),
     lambda b: b["counts"]),
    ("skills-stats", "/api/skills/stats?project=test",
     lambda t: events.append_event("invoke_result", {"name": "skill-" + t},
                                   session_id="s-" + t, project="test"),
     lambda b: b["stats"]),
]

_GATE_IDS = [g[0] for g in POLLED_GATES]


@pytest.mark.parametrize("name,path,seed,rows", POLLED_GATES, ids=_GATE_IDS)
def test_an_unchanged_ledger_is_never_read_again_for_a_polled_route(
        dashboard_server, counters, name, path, seed, rows):
    base, token = dashboard_server
    seed("one")
    status, warm = _get(base, path, token)
    assert status == 200
    assert counters.reads.calls >= 1, "the first poll must actually do the work"
    assert rows(warm), \
        "the fixture produced nothing, so a cheap second poll would prove nothing"

    counters.reset()
    status, again = _get(base, path, token)
    assert status == 200
    assert counters.reads.calls == 0, f"{name}: an unchanged ledger was read anyway"
    # Cheap AND the same: a gate that served a different payload would be a
    # regression dressed as a speedup.
    assert again == warm, f"{name}: the gate served a different payload"


@pytest.mark.parametrize("name,path,seed,rows", POLLED_GATES, ids=_GATE_IDS)
def test_an_appended_event_invalidates_a_polled_routes_gate(
        dashboard_server, counters, name, path, seed, rows):
    base, token = dashboard_server
    seed("one")
    _status, warm = _get(base, path, token)
    counters.reset()
    _get(base, path, token)
    assert counters.reads.calls == 0, \
        f"{name}: the gate did not hold while nothing moved, so the case below " \
        f"cannot tell an invalidation from a cache that never worked"

    seed("two")
    counters.reset()
    _status, after = _get(base, path, token)
    assert counters.reads.calls >= 1, \
        f"{name}: an appended event must re-read the ledger"
    assert len(rows(after)) > len(rows(warm)), \
        f"{name}: the new event never reached the panel"


def _seed_card(session_id, text):
    """One row on one task card. `command_run` is a projected kind, so the card
    it lands on is provably non-empty — a card that projects to nothing cannot
    tell a working gate from a broken one."""
    events.append_event("command_run", {"text": text},
                        session_id=session_id, project="test")


def _card(base, token, session_id):
    return _get(base, "/api/task?project=test&session=" + session_id, token)


def test_an_unchanged_ledger_is_never_read_again_for_a_task_card(
        dashboard_server, counters):
    """Expanding the same card twice must read the ledger once."""
    base, token = dashboard_server
    _seed_card("sess-a", "ran one")

    status, warm = _card(base, token, "sess-a")
    assert status == 200
    assert counters.reads.calls >= 1, "the first expand must actually do the work"
    assert warm["items"], \
        "the fixture produced no rows, so a cheap second expand proves nothing"

    counters.reset()
    status, again = _card(base, token, "sess-a")
    assert status == 200
    assert counters.reads.calls == 0, "an unchanged ledger was read anyway"
    assert again == warm, "the gate served a different card"


def test_a_second_task_card_is_served_from_the_first_cards_read(
        dashboard_server, counters):
    """The test that separates this gate from a per-session cache.

    `/api/task` used to read the WHOLE ledger and keep one session's events, so
    every new card paid a full read — the cost a user meets when they expand
    several cards in a row, and exactly the cost a per-(project, session) memo
    would NOT have removed. The fold is grouped by `session_id` and cached as a
    whole precisely so the SECOND card is free, so that is what is asserted
    here: a different session, zero reads, and its own correct rows."""
    base, token = dashboard_server
    _seed_card("sess-a", "ran one")
    _seed_card("sess-b", "ran two")

    _status, first = _card(base, token, "sess-a")
    assert first["items"], "card A produced nothing to compare against"

    counters.reset()
    status, second = _card(base, token, "sess-b")
    assert status == 200
    assert counters.reads.calls == 0, \
        "a second card re-read the ledger; the fold is not grouped"
    assert [r["text"] for r in second["items"]] == ["ran two"], \
        "card B was served card A's rows, or none at all"


def test_an_appended_event_reaches_the_task_card(dashboard_server, counters):
    base, token = dashboard_server
    _seed_card("sess-a", "ran one")
    _status, warm = _card(base, token, "sess-a")
    counters.reset()
    _card(base, token, "sess-a")
    assert counters.reads.calls == 0, \
        "the gate did not hold while nothing moved, so the case below cannot " \
        "tell an invalidation from a cache that never worked"

    _seed_card("sess-a", "ran two")
    counters.reset()
    _status, after = _card(base, token, "sess-a")
    assert counters.reads.calls >= 1, "an appended event must re-read the ledger"
    assert len(after["items"]) > len(warm["items"]), \
        "the new event never reached the card"


def test_an_unknown_session_is_an_empty_card_not_an_error(dashboard_server):
    """A session the grouped fold never saw answers exactly as the old
    per-session filter did: an empty card. Anything else — a 404, a null, a
    KeyError — would be a new failure mode introduced by the lookup."""
    base, token = dashboard_server
    _seed_card("sess-a", "ran one")
    status, body = _card(base, token, "no-such-session")
    assert status == 200
    assert body == {"items": [], "total": 0, "truncated": 0}


# --- the Skills board's own poll -------------------------------------------
# `renderPlugins` is the LIVE function for that section, so everything below is
# on a 2.5 s timer, not a click.

@pytest.fixture
def fake_inventory(monkeypatch):
    """One skill, from a fake disk. The real `live_inventory` walks this
    machine's ~/.claude (347 skills, 256 ms cold), which would make these tests
    depend on the developer's plugin folder and pay for a scan they do not test.
    The returned dict is mutable so a test can MOVE the signature — that is the
    inventory term of the stamp, and the term a ledger-only gate would miss."""
    from mindhive import plugins as plugins_mod
    state = {"sig": "sig-one"}
    # THREE skills, not one: `top=1` has to be a real truncation or the test
    # that pins `top` into the memo key compares a list against itself.
    monkeypatch.setattr(plugins_mod, "live_inventory", lambda *a, **k: {
        "changed": True, "signature": state["sig"], "inventory": {
            "plugins": [{"name": "p", "skills": [
                {"name": "tester", "trigger": "Use when tests fail."},
                {"name": "shipper", "trigger": "Use when shipping a release."},
                {"name": "digger", "trigger": "Use when digging into a bug."}]}],
            "local_skills": [], "counts": {}}})
    monkeypatch.setattr(plugins_mod, "inventory_signature",
                        lambda *a, **k: state["sig"])
    return state


def _rank(base, token, extra=""):
    return _get(base, "/api/skills/recommend?project=test" + extra, token)


def test_the_skills_ranking_is_not_recomputed_while_nothing_moved(
        dashboard_server, counters, fake_inventory):
    base, token = dashboard_server
    _seed("sess-1")
    status, warm = _rank(base, token)
    assert status == 200
    assert counters.reads.calls >= 1, "the first tick must actually do the work"
    assert warm["recommendations"], \
        "the fixture ranked nothing, so a cheap second tick proves nothing"

    counters.reset()
    status, again = _rank(base, token)
    assert status == 200
    assert counters.reads.calls == 0, "an unchanged store was read anyway"
    assert again == warm, "the gate served a different ranking"


def test_an_appended_event_invalidates_the_skills_ranking(
        dashboard_server, counters, fake_inventory):
    base, token = dashboard_server
    _seed("sess-1")
    _rank(base, token)
    counters.reset()
    _rank(base, token)
    assert counters.reads.calls == 0, \
        "the gate did not hold while nothing moved, so the case below cannot " \
        "tell an invalidation from a cache that never worked"

    events.append_event("next_step", {"text": "run the tests"},
                        session_id="sess-2", project="test")
    counters.reset()
    _rank(base, token)
    assert counters.reads.calls >= 1, "an appended event must re-read the ledger"


def test_an_installed_skill_invalidates_the_skills_ranking(
        dashboard_server, counters, fake_inventory):
    """The term a ledger-only stamp would miss.

    `recommend_for` ranks the skills that are ON DISK. Installing or removing a
    plugin appends no event and touches no incident record, so a stamp made of
    those two alone compares equal after the world moved — and the board would
    go on recommending a skill that is gone, or never offer one that arrived,
    until the server restarted."""
    base, token = dashboard_server
    _seed("sess-1")
    _rank(base, token)
    counters.reset()
    _rank(base, token)
    assert counters.reads.calls == 0, "the gate did not hold, so this proves nothing"

    fake_inventory["sig"] = "sig-two"            # a plugin arrived on disk
    counters.reset()
    status, after = _rank(base, token)
    assert status == 200
    assert counters.reads.calls >= 1, \
        "a changed skill inventory did not invalidate the ranking"


def test_the_ranking_top_is_part_of_the_gates_key(dashboard_server,
                                                  fake_inventory):
    """`top` TRUNCATES the ranking, so one key for every `top` would serve a
    short list to a caller that asked for the whole thing."""
    base, token = dashboard_server
    _seed("sess-1")
    _status, full = _rank(base, token)
    _status, one = _rank(base, token, extra="&top=1")
    assert len(full["recommendations"]) > 1, \
        "the fixture ranked one skill, so `top=1` truncates nothing and this " \
        "test cannot tell a shared key from a separate one"
    assert len(one["recommendations"]) == 1, \
        "top=1 was served the full ranking from the other key"


def _seed_call(name, session_id, text):
    events.append_event("invoke_result", {"name": name, "text": text},
                        session_id=session_id, project="test")


def _history(base, token, name, limit=25):
    return _get(base, "/api/skills/history?project=test&name=" + name
                + "&limit=" + str(limit), token)


def test_every_expanded_skill_is_served_from_one_read(dashboard_server,
                                                      counters):
    """The board re-fetches the history of EVERY expanded skill on every tick,
    and each one used to be its own `read_all` — three skills open meant three
    whole-ledger reads a tick. One grouped fold has to serve all of them."""
    base, token = dashboard_server
    _seed_call("alpha", "s1", "ran alpha")
    _seed_call("beta", "s1", "ran beta")

    _status, first = _history(base, token, "alpha")
    assert [r["text"] for r in first] == ["ran alpha"]

    counters.reset()
    status, second = _history(base, token, "beta")
    assert status == 200
    assert counters.reads.calls == 0, \
        "a second expanded skill re-read the ledger; the fold is not grouped"
    assert [r["text"] for r in second] == ["ran beta"], \
        "the second skill was served the first one's calls, or none at all"


def test_a_second_limit_is_served_from_the_same_fold(dashboard_server,
                                                     counters):
    """`limit` slices the cached list rather than being part of the key."""
    base, token = dashboard_server
    for i in range(3):
        _seed_call("alpha", "s1", "call %d" % i)
    _status, all_rows = _history(base, token, "alpha", limit=25)
    assert len(all_rows) == 3

    counters.reset()
    _status, two = _history(base, token, "alpha", limit=2)
    assert counters.reads.calls == 0, "a different limit re-read the ledger"
    assert two == all_rows[:2], "the slice did not match the full fold's head"


def test_an_appended_call_reaches_the_skill_history(dashboard_server, counters):
    base, token = dashboard_server
    _seed_call("alpha", "s1", "first")
    _status, warm = _history(base, token, "alpha")
    counters.reset()
    _history(base, token, "alpha")
    assert counters.reads.calls == 0, \
        "the gate did not hold while nothing moved, so the case below cannot " \
        "tell an invalidation from a cache that never worked"

    _seed_call("alpha", "s1", "second")
    counters.reset()
    _status, after = _history(base, token, "alpha")
    assert counters.reads.calls >= 1, "an appended call must re-read the ledger"
    assert len(after) == len(warm) + 1, "the new call never reached the panel"
    assert after[0]["text"] == "second", "newest-first order was lost"


@pytest.fixture
def collapses(monkeypatch):
    """Counts calls to the near-duplicate collapse.

    The claim is about CALLS, not milliseconds — the same reason `counters`
    counts reads. `dashboard_get` looks the function up on the module at call
    time, so patching the owner is enough."""
    counter = _Counter(dedupe.collapse)
    monkeypatch.setattr(dedupe, "collapse", counter)
    return counter


def _seed_caveat(text, ts=None):
    """A manual lesson — a `caveat` is what `_improvements_fold` puts in the
    manual half, and it is the half `dedupe.collapse` runs over.

    Named for the EVENT, not for the concept, because `_seed_lesson` above is
    already a different helper with a different signature. Two module-level
    helpers sharing a name do not collide at import — the second simply wins,
    and the six callers of the first then fail with a `TypeError` about an
    argument they never passed, raised from a line that names neither."""
    events.append_event("caveat", {"text": text}, session_id="s1",
                        project="test", **({"ts": ts} if ts else {}))


def test_the_lesson_collapse_is_not_repeated_while_nothing_moved(
        dashboard_server, collapses):
    """76 ms of the ~108 ms this route cost warm, after its fold was already
    gated: `dedupe.collapse` compares every lesson against every representative
    it has kept. It is pure and its input is a deterministic sort of an
    already-cached list, so a second poll must not run it again."""
    base, token = dashboard_server
    _seed_caveat("the ledger is append-only and hash chained")
    _seed_caveat("a stop hook must fail open, never block")

    status, warm = _get(base, "/api/improvements?project=test", token)
    assert status == 200
    assert collapses.calls >= 1, "the first poll must actually collapse"
    assert warm["manual"], \
        "the fixture produced no manual lessons, so a cheap second poll " \
        "would prove nothing"

    collapses.calls = 0
    status, again = _get(base, "/api/improvements?project=test", token)
    assert status == 200
    assert collapses.calls == 0, "an unchanged store was collapsed again"
    assert again == warm, "the gate served a different set of lessons"


def test_an_appended_lesson_is_collapsed_again(dashboard_server, collapses):
    base, token = dashboard_server
    _seed_caveat("the ledger is append-only and hash chained")
    _status, warm = _get(base, "/api/improvements?project=test", token)
    collapses.calls = 0
    _get(base, "/api/improvements?project=test", token)
    assert collapses.calls == 0, \
        "the gate did not hold while nothing moved, so the case below cannot " \
        "tell an invalidation from a cache that never worked"

    _seed_caveat("a worktree serves whatever branch is checked out")
    collapses.calls = 0
    _status, after = _get(base, "/api/improvements?project=test", token)
    assert collapses.calls >= 1, "an appended lesson must be collapsed again"
    assert len(after["manual"]) > len(warm["manual"]), \
        "the new lesson never reached the panel"


def test_hive_mind_keeps_the_lessons_in_one_time_order(dashboard_server):
    """HIVE MIND collapses the MERGED list, and the difference is the order.

    The tempting optimisation is to collapse each namespace separately and
    concatenate — every namespace then gets a cache entry and `*` is free. The
    REPRESENTATIVES would be identical: `dedupe.collapse` clusters on
    `(type, project)` and every manual row carries its project, so two
    namespaces can never merge. What changes is the sequence: collapse emits in
    input order, so merging first gives one global newest-first list while
    concatenating gives them grouped by project.

    The fixture interleaves two namespaces in time so the two orders DISAGREE —
    `zzz` owns both the newest and the oldest lesson. A fixture where each
    namespace occupied its own time range would pass either way."""
    base, token = dashboard_server
    for proj, ts, text in (
            ("zzz", "2026-01-03T00:00:00Z", "zzz is the newest lesson"),
            ("aaa", "2026-01-02T00:00:00Z", "aaa sits in the middle"),
            ("zzz", "2026-01-01T00:00:00Z", "zzz is also the oldest lesson")):
        events.append_event("caveat", {"text": text}, session_id="s1",
                            project=proj, ts=ts)

    status, merged = _get(base, "/api/improvements?project=*", token)
    assert status == 200
    order = [(m["project"], m["ts"]) for m in merged["manual"]
             if m["project"] in ("aaa", "zzz")]
    assert order == [("zzz", "2026-01-03T00:00:00Z"),
                     ("aaa", "2026-01-02T00:00:00Z"),
                     ("zzz", "2026-01-01T00:00:00Z")], \
        f"HIVE MIND lessons are grouped by namespace, not by time: {order}"


def test_the_session_status_follows_the_clock_across_a_cache_hit(
        dashboard_server, counters, monkeypatch):
    """`status` is a function of the WALL CLOCK and is the one field the sessions
    gate deliberately does not store.

    A cached status freezes: a session that was `ongoing` when the fold ran would
    still read `ongoing` an hour later, on a board whose entire job is to say
    which sessions are alive. The clock is moved at the module seam
    `taskview.status_of` actually reads, rather than by patching `status_of`
    itself — a test that stubbed the function would pass against an
    implementation that cached its result and never called it.

    Note the ordering: the clock is moved BEFORE the second request, and the
    session board it produces has to disagree with the first while the ledger
    behind it did not move at all."""
    base, token = dashboard_server
    events.append_event("user_prompt", {"text": "still going"},
                        session_id="live-1", project="test")
    _status, warm = _get(base, "/api/sessions?project=test", token)
    assert [r for r in warm if r["status"] == "ongoing"], \
        "the seeded session did not read as ongoing, so ageing it proves nothing"

    # `_FakeTime`, not a bare stub: `status_of` is not the only thing in
    # `taskview` that reaches into `time`, and a stub carrying only `time()`
    # answered the whole request with a 500.
    clock = _FakeTime(time.time())
    monkeypatch.setattr(taskview, "time", clock)
    clock.now += taskview.ACTIVE_WINDOW_SECS + 60
    counters.reset()
    _status, aged = _get(base, "/api/sessions?project=test", token)
    assert counters.reads.calls == 0, \
        "the ledger did not move, so this must have been a cache hit"
    assert not [r for r in aged if r["status"] == "ongoing"], \
        "a cached status froze the session board against the clock"
    # Everything else is a pure fold of the ledger and must NOT have moved.
    assert [{k: v for k, v in r.items() if k != "status"} for r in aged] == \
           [{k: v for k, v in r.items() if k != "status"} for r in warm]


def test_sessions_sharing_a_last_ts_keep_their_original_order(dashboard_server):
    """The regression the gate introduced and a byte-comparison caught.

    Before the gate this list came out of ONE globally ts-sorted stream, so
    `sorted(..., key=last_ts, reverse=True)` — which is stable — broke ties in
    each session's order of FIRST APPEARANCE. Folding per namespace and
    concatenating breaks them in `_all_projects()` order instead, which is
    alphabetical. On the real store that swapped two sessions sharing
    `2026-08-10T00:27:10Z` at row 508 of 2101: a view whose contents had not
    changed, reordered.

    The fixture is built so the two answers DISAGREE. `aaa` sorts first
    alphabetically but its session starts five seconds later, so a concatenation
    order puts it first and the original order puts `zzz` first. A fixture where
    both orders agree would pass either way."""
    base, token = dashboard_server
    for ts in ("2026-01-01T00:00:05Z", "2026-01-02T00:00:00Z"):
        events.append_event("user_prompt", {"text": "a"}, session_id="sess-aaa",
                            ts=ts, project="aaa")
    for ts in ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"):
        events.append_event("user_prompt", {"text": "z"}, session_id="sess-zzz",
                            ts=ts, project="zzz")

    _status, rows = _get(base, "/api/sessions?project=*", token)
    tied = [r["session_id"] for r in rows
            if r["last_ts"] == "2026-01-02T00:00:00Z"]
    assert tied == ["sess-zzz", "sess-aaa"], (
        "sessions sharing a last_ts must stay in first-appearance order; got "
        f"{tied}")


def test_the_changelog_pending_marker_is_read_on_every_poll(dashboard_server,
                                                            counters):
    """`pending` is a per-project SIDECAR, not an event, so the segment
    fingerprint cannot see it move. It is read outside the gate on purpose:
    cached, the blocking "unlogged code changes" banner would stay up — or stay
    down — until some unrelated event happened to move the stamp."""
    base, token = dashboard_server
    events.append_event("code_change", {"category": "update", "text": "an entry"},
                        session_id="s1", project="test")
    _status, warm = _get(base, "/api/changelog?project=test", token)
    assert warm["pending"] is None, "the fixture already had a marker"

    changelog_mod = importlib.import_module("mindhive.changelog")
    paths.write_text_atomic(changelog_mod.pending_path("test"),
                            json.dumps({"files": ["src/mindhive/thing.py"]}))
    counters.reset()
    _status, after = _get(base, "/api/changelog?project=test", token)
    assert counters.reads.calls == 0, \
        "the ledger did not move, so the ENTRIES must still have been cached"
    assert after["pending"] == {"files": ["src/mindhive/thing.py"]}, \
        "the pending marker was cached along with the entries"
    assert after["entries"] == warm["entries"]
