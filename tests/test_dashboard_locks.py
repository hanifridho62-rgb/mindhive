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
import importlib
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, events

# The locks and both memos are MODULE level on purpose — they have to hold
# across every handler class `make_handler` mints, and across every thread the
# ThreadingHTTPServer serves on. That also means they outlive a test, so each
# test starts and ends from a known state.
_ALL_LOCKS = (dashboard._HARVEST_LOCK, dashboard._PROJECT_LOCK,
              dashboard._SYNC_LOCK, dashboard._EMBED_LOCK,
              dashboard._PLUGINS_LOCK, dashboard._SELFTEST_LOCK)


@pytest.fixture(autouse=True)
def clean_module_state():
    dashboard._CATS_MEMO.clear()
    dashboard._SESSIONS_MEMO.clear()
    yield
    dashboard._CATS_MEMO.clear()
    dashboard._SESSIONS_MEMO.clear()
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
    ("/api/harvest", "mindhive.dashboard", "run_catch_up",
     {"projects": {}, "total_new": 0}),
    ("/api/project", "mindhive.projector", "project", {"wrote": []}),
    ("/api/sync", "mindhive.sync", "sync", "MEMORY.md"),
    ("/api/embed", "mindhive.vectors", "embed", {"embedded": 0}),
    ("/api/plugins/scan", "mindhive.plugins", "scan_and_store", {"top": []}),
    ("/api/plugins/autoscan", "mindhive.plugins", "ensure_fresh",
     {"scanned": True, "age_hours": 0.0, "payload": {}}),
    ("/api/selftest", "mindhive.selftest", "run", 0),
]
LOCKED_IDS = [row[0] for row in LOCKED]


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
    monkeypatch.setattr(dashboard, "run_catch_up", fake)
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
    dashboard's module-level `time` so a cache HIT can be observed across a
    clock change without the test sleeping."""

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
    monkeypatch.setattr(dashboard, "time", clock)

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
