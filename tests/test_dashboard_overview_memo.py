# tests/test_dashboard_overview_memo.py
"""The stat-only gate in front of /api/overview.

`/api/mindnet/cats` got this treatment first (T6, tests/test_dashboard_locks.py);
this is the same gate over the endpoint a human actually waits on after clicking
a section, and over the ONE route whose answer does not come from the event log
alone. `chain_ok` is read out of `chain.json`, a file that lives beside the event
segments and is rewritten by `hive project` without a single event being
appended — so a segment-only fingerprint would freeze the integrity light at
whatever it said the first time, which is the one failure mode a cache over an
integrity check must not have. `test_a_chain_refresh_invalidates_...` below is
that guard, and it was shown to bite: with the fingerprint built from
`segment_stats` alone the endpoint kept reporting the pre-refresh verdict.

Behavioural, like its sibling: every test drives a real HTTP request into a real
handler and counts the calls the gate exists to skip, rather than asserting on
elapsed time (a flaky proxy) or on source strings (which cannot tell a working
gate from a gate-shaped comment).

Store isolation comes from the autouse `isolate_store` fixture in
tests/conftest.py, like every other dashboard test.
"""
import collections
import importlib
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import chain, dashboard, events

OVERVIEW = "/api/overview?project=test"
OVERVIEW_OTHER = "/api/overview?project=other"
# HIVE MIND. `?project=` is given on every poll deliberately: without it do_GET
# calls default_project(), which reads EVERY namespace's log just to pick one —
# a read the gate is not responsible for and that would make the counts
# unreadable.
OVERVIEW_ALL = "/api/overview?project=*"


def _clear_memos():
    """Empty every module-level memo, enumerated rather than listed by name — a
    memo absent from the version under test must not turn every behavioural
    failure in this file into an AttributeError at setup."""
    for name, value in vars(dashboard).items():
        if name.endswith("_MEMO") and isinstance(value, collections.OrderedDict):
            value.clear()


@pytest.fixture(autouse=True)
def clean_module_state():
    _clear_memos()
    yield
    _clear_memos()


@pytest.fixture
def dashboard_server():
    """A dashboard on an ephemeral port; yields (base, token)."""
    token = "test-token-123"
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", token
    finally:
        server.shutdown()


def _get(base, path, token):
    sep = "&" if "?" in path else "?"
    with urllib.request.urlopen(f"{base}{path}{sep}token={token}",
                                timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


class _Counter:
    """Call-counting pass-through. The claim under test is that an unchanged
    store performs NO read_all, which is a claim about calls."""

    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return self.fn(*a, **kw)


@pytest.fixture
def counters(monkeypatch):
    """A counter over the cost this gate exists to skip. Patched on the module
    that OWNS `read_all` — patching a re-export would silently miss, running the
    real read while the assertion still passed."""
    ev_mod = importlib.import_module("mindhive.events")
    reads = _Counter(ev_mod.read_all)
    monkeypatch.setattr(ev_mod, "read_all", reads)

    class _Pair:
        def reset(self):
            reads.calls = 0
    pair = _Pair()
    pair.reads = reads
    return pair


def _seed(project="test", session_id="sess-1"):
    """One event from each side of the memory/auto split (`MEMORY_VIEW_TYPES`
    holds the semantic types plus user_prompt / assistant_response), so both
    counters are non-zero and an empty store cannot pass by accident."""
    events.append_event("user_prompt", {"text": "do the thing"},
                        session_id=session_id, project=project)
    events.append_event("session_start", {"source": "test"},
                        session_id=session_id, project=project)


# --- the gate itself -------------------------------------------------------

def test_an_unchanged_overview_reads_no_ledger_at_all(dashboard_server, counters):
    base, token = dashboard_server
    _seed()
    status, warm = _get(base, OVERVIEW, token)
    assert status == 200
    assert counters.reads.calls >= 1, "the first poll must actually do the work"
    assert warm["events"] == 2
    assert warm["memory_events"] == 1 and warm["auto_events"] == 1

    counters.reset()
    status, again = _get(base, OVERVIEW, token)
    assert status == 200
    assert counters.reads.calls == 0, \
        f"an unchanged store was read {counters.reads.calls}x anyway"
    # Cheap AND identical: an overview that served a different payload would be
    # a regression dressed as a speedup.
    assert again == warm


def test_an_append_re_reads_that_project_and_only_that_project(
        dashboard_server, counters):
    """The invalidation that matters comes from OUTSIDE this process — the CLI, a
    hook, `hive loop`. A real event goes into the real segment file, so the stat
    the memo depends on moves exactly the way it does in production."""
    base, token = dashboard_server
    _seed()
    _seed(project="other", session_id="sess-other")
    _status, warm_test = _get(base, OVERVIEW, token)
    _status, warm_other = _get(base, OVERVIEW_OTHER, token)

    events.append_event("decision", {"text": "an append from outside"},
                        session_id="sess-1", project="test")

    counters.reset()
    status, after_other = _get(base, OVERVIEW_OTHER, token)
    assert status == 200
    assert counters.reads.calls == 0, \
        "an append to one namespace invalidated another one's overview"
    assert after_other == warm_other

    counters.reset()
    status, after_test = _get(base, OVERVIEW, token)
    assert status == 200
    assert counters.reads.calls == 1, \
        f"{counters.reads.calls} reads for one project's overview"
    assert after_test["events"] == warm_test["events"] + 1, \
        "the appended event never reached the overview"


def test_a_chain_refresh_invalidates_even_though_no_event_moved(
        dashboard_server, counters):
    """DISCRIMINATING, and the reason this endpoint could not reuse the cats
    gate's fingerprint as-is.

    `chain_ok` is the one field on this response that is NOT a function of the
    events: `chain.verify` reads `chain.json`, which `hive project` rewrites
    without appending anything. A `segment_stats`-only fingerprint therefore does
    not move here at all — the integrity light would stay stuck on the verdict
    from the first poll, for the life of the process, and every count beside it
    would still look correct."""
    base, token = dashboard_server
    _seed()
    _status, warm = _get(base, OVERVIEW, token)
    assert warm["chain_ok"] is False, \
        "expected no chain.json yet — this test needs a verdict that can flip"

    chain.write_chain(project="test")           # exactly what `hive project` does

    counters.reset()
    status, after = _get(base, OVERVIEW, token)
    assert status == 200
    assert after["chain_ok"] is True, (
        "the integrity light is frozen: chain.json was rewritten and the "
        "overview still served the pre-refresh verdict")
    assert after["events"] == warm["events"], \
        "no event was appended, so no count may have moved"


def test_an_empty_fingerprint_never_matches_and_never_freezes_the_overview(
        dashboard_server, counters, monkeypatch):
    """DISCRIMINATING. `segment_stats` answers `{}` for ANY OSError as well as
    for "no events directory". Fail-safe for the sidecar, INVERTED as a cache
    key: `{} == {}` scores a hit, so one transient directory error would pin this
    overview for the life of the process — silently."""
    base, token = dashboard_server
    _seed()
    _get(base, OVERVIEW, token)                  # warm with a real fingerprint
    sidecar = importlib.import_module("mindhive.mindnet.sidecar")
    monkeypatch.setattr(sidecar, "segment_stats", lambda *_a, **_kw: {})

    for attempt in (1, 2):
        counters.reset()
        status, body = _get(base, OVERVIEW, token)
        assert status == 200
        assert counters.reads.calls >= 1, \
            f"poll {attempt} served an empty fingerprint from the memo"
        assert body["events"] == 2
    # The warm entry from before the fault is still there and is harmless — while
    # the fingerprint is untrusted the gate returns before it consults the memo
    # at all. What must NOT have happened is `{}` being written as a stamp.
    stamp, _stats = dashboard._OVERVIEW_MEMO["test"]
    assert stamp[0], "an empty fingerprint was stored as a key a later poll can hit"


def test_a_non_file_event_backend_bypasses_the_overview_memo(
        dashboard_server, counters, monkeypatch):
    """DISCRIMINATING. `events.read_all` short-circuits to Postgres when the
    backend is `pg`, while `segment_stats` keeps stat'ing the local `.jsonl`
    segments — which, on the documented file → dual → pg path, are still on disk
    and never change again. Non-empty fingerprint, stable forever, every poll a
    hit, the overview frozen at its first response.

    `_maybe_read_from_pg` is stubbed to None so `read_all` still answers from the
    file segments: the variable under test is the memo's decision, not whether a
    Postgres server happens to be reachable from this machine."""
    base, token = dashboard_server
    _seed()
    _status, warm = _get(base, OVERVIEW, token)

    ev_mod = importlib.import_module("mindhive.events")
    monkeypatch.setattr(ev_mod, "_maybe_read_from_pg", lambda project=None: None)
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "pg")

    counters.reset()
    status, again = _get(base, OVERVIEW, token)
    assert status == 200
    assert counters.reads.calls >= 1, (
        "a non-file backend served the memo instead of reading: segment_stats "
        "fingerprints local segments that pg never writes, so the memo can "
        "never invalidate and the overview freezes for the life of the process")
    assert again["events"] == warm["events"], "the bypass changed the overview"


def test_the_overview_fingerprint_is_a_real_stamp_when_it_can_be_trusted():
    """CANARY for both guards above. They assert an ABSENCE of caching, which a
    fingerprint that returned None unconditionally would also produce — and that
    version would be a cache that never caches, passing every guard here while
    restoring the full read on every poll."""
    _seed()
    trusted = dashboard._overview_fingerprint("test")
    assert trusted, "a file-backend namespace produced no fingerprint at all"
    assert trusted == dashboard._overview_fingerprint("test"), \
        "the fingerprint moved on its own, with nothing appended"
    events.append_event("decision", {"text": "moved"}, session_id="s", project="test")
    assert trusted != dashboard._overview_fingerprint("test"), \
        "the fingerprint did not move for a real append"


# --- HIVE MIND -------------------------------------------------------------

def test_hive_mind_totals_match_a_directly_unified_fold(dashboard_server):
    """The am branch now sums per-project folds instead of unifying every event
    into one pass. That is only allowed if it produces the SAME numbers: distinct
    (project, session) pairs is exactly the sum of each project's distinct
    sessions, and the type tallies add. Asserted against the direct computation
    rather than against remembered figures."""
    base, token = dashboard_server
    _seed()
    _seed(project="other", session_id="sess-other")
    _seed(project="other", session_id="sess-other-2")

    status, body = _get(base, OVERVIEW_ALL, token)
    assert status == 200
    names = dashboard._all_projects()
    direct = dashboard._overview_counts(
        ((n, ev.session_id), ev) for n in names for ev in events.read_all(n))
    for field in ("events", "memory_events", "auto_events", "sessions",
                  "by_type", "last_ts"):
        assert body[field] == direct[field], f"HIVE MIND {field} disagrees"
    assert body["projects"] == len(names)


def test_hive_mind_does_not_evict_the_entries_it_just_wrote(
        dashboard_server, counters):
    """DISCRIMINATING against reusing `_MEMO_MAX`. That bound is 8, and one HIVE
    MIND request folds EVERY namespace — on the real machine, 60 of them — so a
    memo sized for the picker's hot-key access pattern evicts most of what a
    single request just wrote and can never hit. Measured on /api/projects:
    three identical passes cost 1329 / 1214 / 2134 ms with 8 entries held after
    each. Twelve namespaces here is above that bound and cheap to seed."""
    base, token = dashboard_server
    for i in range(12):
        _seed(project=f"p{i}", session_id=f"s{i}")

    status, warm = _get(base, OVERVIEW_ALL, token)
    assert status == 200
    assert counters.reads.calls >= 12, "the first pass must read every namespace"

    counters.reset()
    status, again = _get(base, OVERVIEW_ALL, token)
    assert status == 200
    assert counters.reads.calls == 0, (
        f"{counters.reads.calls} namespaces re-read on an unchanged store — the "
        f"memo is evicting entries the same request wrote")
    assert again == warm
