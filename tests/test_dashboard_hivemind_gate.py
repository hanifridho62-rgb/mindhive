# SPDX-License-Identifier: Apache-2.0
"""HIVE MIND on /api/skills/status and /api/skills/stats, behind one composite
stat-only stamp (blueprint P5, Pillar 1, runbook step 1.2 of §10.11).

The single-project path of both routes has been gated since the per-project
fingerprint landed. The HIVE MIND path (`?project=*`) was left reading every
namespace on every request, because its fold runs over the MERGED event list
and "last outcome per skill" across namespaces is not the merge of each
namespace's last outcome. The gate added here keeps that fold exactly as it
is and only decides when it may be skipped:

* the stamp is every namespace's `compute_project_fingerprint`, in sorted
  namespace order, so an append in ANY one namespace -- or a namespace
  appearing -- is a miss, and a miss refolds the whole merge;
* a warm request reads nothing;
* one untrusted namespace stamp (a `pg` backend, a namespace holding no
  segment) makes the request uncacheable: fold, store nothing;
* the stamp is taken BEFORE the fold, so a write landing inside it is never
  cached over;
* the body is, byte for byte, what the ungated handler answers today.

The fixture is two namespaces whose merged last outcome for `tdd` (a failure
in `alpha` at :03) is NOT what composing per-namespace folds in namespace
order would say (`beta`'s last, a success at :02), so the byte pins pin the
merge as well as the encoding.

Store isolation is the autouse `isolate_store` fixture in tests/conftest.py;
the memos are process-global, so they are swept around every test here.
"""
import collections
import json
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, dashboard_memo, events, paths, plugins

from tests.serving import serve_in_thread

TOKEN = "test-token-p5-hivemind"
STATUS = "/api/skills/status?project=*"
STATS = "/api/skills/stats?project=*"
ROUTES = [STATUS, STATS]
ROUTE_IDS = ["status", "stats"]
NAMESPACES = ["alpha", "beta"]

# --- fixture builders -------------------------------------------------------

SEED = (
    ("alpha", "invoke_result", {"name": "tdd", "text": "green"}, "sess-a1",
     "2026-01-01T00:00:01Z"),
    ("beta", "invoke_result", {"name": "tdd", "text": "green"}, "sess-b1",
     "2026-01-01T00:00:02Z"),
    ("alpha", "tool_failed", {"signature": "skill:tdd", "text": "red"},
     "sess-a1", "2026-01-01T00:00:03Z"),
    ("beta", "skill_used", {"name": "review"}, "sess-b1",
     "2026-01-01T00:00:04Z"),
    ("alpha", "decision", {"text": "not a skill event"}, "sess-a2",
     "2026-01-01T00:00:05Z"),
)

# Today's answers for `SEED`, read off the ungated handlers (worktree HEAD
# 30a1dee, the owned modules unmodified) and written out as data. `_json`
# sends exactly `json.dumps(obj, ensure_ascii=False)`, so equal encodings are
# equal bytes on the wire, key order included.
GOLDEN = {
    STATUS: {"status": {"tdd": "fail"}, "counts": {"tdd": 3}},
    STATS: {"stats": {
        "tdd": {"calls": 3, "ok": 2, "fail": 1, "unobserved": 0,
                "completion_rate": 0.6666666666666666,
                "last_outcome": "fail", "last_used": "2026-01-01T00:00:03Z",
                "streak": 0, "fail_streak": 1, "provenance": {"invoke": 3},
                "sources": ["invoke_result", "tool_failed"]},
        "review": {"calls": 1, "ok": 1, "fail": 0, "unobserved": 0,
                   "completion_rate": 1.0, "last_outcome": "ok",
                   "last_used": "2026-01-01T00:00:04Z", "streak": 1,
                   "fail_streak": 0, "provenance": {"passive": 1},
                   "sources": ["skill_used"]}}},
}


def _seed():
    for namespace, etype, payload, session, ts in SEED:
        events.append_event(etype, payload, session_id=session, ts=ts,
                            project=namespace)


def _append_lint(namespace, ts="2026-01-01T00:00:06Z", session="sess-new"):
    """A skill call no seeded namespace has made, so its arrival shows in both
    routes' bodies."""
    events.append_event("invoke_result", {"name": "lint", "text": "clean"},
                        session_id=session, ts=ts, project=namespace)


def _skills_in(route, body):
    return set(body["counts"] if route == STATUS else body["stats"])


def _sweep_memos():
    """Empty every module-level `*_MEMO` in both namespaces that hold them."""
    for module in (dashboard, dashboard_memo):
        for name, value in vars(module).items():
            if (name.endswith("_MEMO")
                    and isinstance(value, collections.OrderedDict)):
                value.clear()


@pytest.fixture(autouse=True)
def _clean_memos():
    _sweep_memos()
    yield
    _sweep_memos()


@pytest.fixture
def base():
    """A real dashboard on an ephemeral port, threaded like the live one."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(TOKEN))
    with serve_in_thread(server):
        yield f"http://127.0.0.1:{server.server_address[1]}"


def _raw(base, path):
    try:
        with urllib.request.urlopen(f"{base}{path}&token={TOKEN}",
                                    timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _get(base, path):
    status, raw = _raw(base, path)
    return status, json.loads(raw.decode("utf-8"))


def _count_reads(monkeypatch):
    """Every `events.read_all` from here on, by namespace. The merge reaches
    the ledger only through it (`all_event_pairs`)."""
    real, reads = events.read_all, []

    def counting(project=None):
        reads.append(project)
        return real(project)

    monkeypatch.setattr(events, "read_all", counting)
    return reads


def _folds():
    return dashboard_memo.DASHBOARD_FOLDS_MEMO


# --- the public memo names --------------------------------------------------

def test_the_spec_memo_names_are_one_existing_memo_and_one_new_one():
    """`DASHBOARD_STATS_MEMO` is the spec's name for the picker's existing
    512-slot stats memo -- an identity, never a second memo with a bound of
    its own -- and `DASHBOARD_FOLDS_MEMO` is a memo nothing else shares."""
    assert dashboard_memo.DASHBOARD_STATS_MEMO is dashboard_memo._STATS_MEMO
    folds = _folds()
    assert isinstance(folds, dashboard_memo.BoundedMemo)
    assert folds.max_entries == 512
    others = [value for name, value in vars(dashboard_memo).items()
              if name.endswith("_MEMO") and name != "DASHBOARD_FOLDS_MEMO"]
    assert others and all(folds is not value for value in others)


def test_the_assembly_point_re_exports_the_folds_memo_for_the_sweeps():
    """`tests/test_dashboard_locks.py`, `test_dashboard_http.py` and
    `test_dashboard_injection_status.py` start cold by emptying every
    `*_MEMO` they find in `vars(dashboard)`; a memo that lives only in
    `dashboard_memo` outlives them and hands the next test a warm answer. So
    `dashboard` re-exports THE object, as it does `_WORKSESSIONS_MEMO`, and a
    sweep written the way those files write it finds it."""
    folds = _folds()
    assert dashboard.DASHBOARD_FOLDS_MEMO is folds
    swept = [value for name, value in vars(dashboard).items()
             if name.endswith("_MEMO") and isinstance(value, collections.OrderedDict)]
    assert any(value is folds for value in swept)


# --- the body: today's, byte for byte ---------------------------------------

@pytest.mark.parametrize("route", ROUTES, ids=ROUTE_IDS)
def test_hive_mind_body_is_todays_byte_for_byte_cold_and_warm(base, route):
    """REGRESSION PIN (passes before the gate exists): the cold answer is
    today's, and so is the one a warm gate serves."""
    _seed()
    expected = json.dumps(GOLDEN[route], ensure_ascii=False).encode("utf-8")

    assert _raw(base, route) == (200, expected)
    assert _raw(base, route) == (200, expected)


def test_the_two_routes_are_never_served_each_other_s_fold(base):
    """Keyed per endpoint: one memo, two routes, and neither is ever served
    the other's fold, whichever of them warmed the memo first."""
    _seed()

    for route in (STATUS, STATS, STATUS, STATS):
        assert _get(base, route) == (200, GOLDEN[route])

    assert len(_folds()) == 2


# --- the gate ---------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES, ids=ROUTE_IDS)
def test_a_warm_hive_mind_request_reads_no_ledger(base, monkeypatch, route):
    _seed()
    reads = _count_reads(monkeypatch)

    _raw(base, route)
    # CANARY: the cold request really folds every namespace, so the empty
    # list below is the gate's doing and not a counter that sees nothing.
    assert sorted(set(reads)) == NAMESPACES
    reads.clear()
    status, _body = _raw(base, route)

    assert status == 200
    assert reads == [], f"a warm HIVE MIND request read {reads}"


@pytest.mark.parametrize("route", ROUTES, ids=ROUTE_IDS)
@pytest.mark.parametrize("namespace", NAMESPACES)
def test_an_append_in_any_one_namespace_refolds_the_whole_merge_then_settles(
        base, monkeypatch, route, namespace):
    _seed()
    reads = _count_reads(monkeypatch)
    _get(base, route)
    reads.clear()
    _get(base, route)
    assert reads == [], (
        "the gate did not hold while nothing moved, so the case below cannot "
        "tell an invalidation from a cache that never worked")

    _append_lint(namespace)
    reads.clear()
    status, body = _get(base, route)

    assert status == 200
    assert sorted(set(reads)) == NAMESPACES, (
        "a miss must refold the MERGE, not only the namespace that moved")
    assert "lint" in _skills_in(route, body)
    reads.clear()
    assert _get(base, route) == (status, body)
    assert reads == [], "the gate did not settle after the refold"


@pytest.mark.parametrize("route", ROUTES, ids=ROUTE_IDS)
def test_a_namespace_appearing_is_a_miss(base, monkeypatch, route):
    """The stamp names its namespaces: stamps over the old list alone would
    still match after a new namespace joined the merge."""
    _seed()
    reads = _count_reads(monkeypatch)
    _get(base, route)
    reads.clear()
    _get(base, route)
    assert reads == [], "the gate did not hold while nothing moved"

    _append_lint("gamma")
    reads.clear()
    status, body = _get(base, route)

    assert status == 200
    assert sorted(set(reads)) == ["alpha", "beta", "gamma"]
    assert "lint" in _skills_in(route, body)


def test_a_write_landing_inside_the_hive_mind_fold_is_not_cached_over(
        base, monkeypatch):
    """DISCRIMINATING on the ORDER of stamp and fold. Stamped AFTER the fold,
    the key would already describe the ledger with the late write in it while
    the value was folded without it, and every later request would match the
    key and serve the stale answer."""
    _seed()
    real, folded = plugins.call_stats, []

    def fold(project=None, evs=None):
        folded.append(len(evs))
        if len(folded) == 1:                      # a write lands mid-fold
            _append_lint("beta", ts="2026-01-01T00:00:07Z",
                         session="sess-late")
        return real(project=project, evs=evs)

    monkeypatch.setattr(plugins, "call_stats", fold)

    first = _get(base, STATUS)[1]
    second = _get(base, STATUS)[1]
    third = _get(base, STATUS)[1]

    assert "lint" not in first["counts"]
    assert second["counts"].get("lint") == 1, (
        "the write that landed inside the first fold was cached over")
    assert third == second
    assert folded == [5, 6], "the gate did not settle once nothing moved"


# --- untrusted stamps: fold, store nothing ----------------------------------

@pytest.mark.parametrize("route", ROUTES, ids=ROUTE_IDS)
def test_a_pg_backend_makes_hive_mind_uncacheable(base, monkeypatch, route):
    """Under `pg` the reader answers from Postgres while the local segments
    stop moving, so a stamp over them would match forever. `_maybe_read_from_pg`
    is stubbed so `read_all` still answers from the files: the only variable
    under test is the gate's decision, not whether a server is reachable."""
    _seed()
    monkeypatch.setattr(events, "_maybe_read_from_pg",
                        lambda project=None: None)
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "pg")
    reads = _count_reads(monkeypatch)

    first = _get(base, route)
    second = _get(base, route)

    assert first == second == (200, GOLDEN[route])
    assert sorted(reads) == ["alpha", "alpha", "beta", "beta"], (
        "a pg-backed HIVE MIND request was served from the memo")
    assert len(_folds()) == 0, "an untrusted stamp was stored as a key"
    # CANARY: the same ledger under the file backend does cache.
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "file")
    reads.clear()
    _get(base, route)
    _get(base, route)
    assert sorted(reads) == NAMESPACES and len(_folds()) == 1


@pytest.mark.parametrize("route", ROUTES, ids=ROUTE_IDS)
def test_a_namespace_holding_no_segment_makes_hive_mind_uncacheable(
        base, monkeypatch, route):
    _seed()
    (paths.project_dir("gamma") / "events").mkdir(parents=True)
    # It IS one of the namespaces the merge walks, with nothing to stamp.
    assert "gamma" in paths.event_namespaces()
    reads = _count_reads(monkeypatch)

    first = _get(base, route)
    second = _get(base, route)

    assert first == second == (200, GOLDEN[route])
    assert reads.count("alpha") == 2, (
        "the second request was served from a memo over an untrusted stamp")
    assert len(_folds()) == 0, "an untrusted stamp was stored as a key"
    # CANARY: once `gamma` holds a segment, the same requests cache.
    _append_lint("gamma")
    reads.clear()
    _get(base, route)
    _get(base, route)
    assert reads.count("alpha") == 1 and len(_folds()) == 1
