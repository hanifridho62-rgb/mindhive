# tests/test_dashboard_oversee_memo.py
"""The gate in front of /api/oversee — TODOS item 17, the harder half.

`/api/overview` got a stat-only gate (tests/test_dashboard_overview_memo.py) and
`/api/mindnet/cats` got one before it (tests/test_dashboard_locks.py). This
endpoint was left out deliberately, and the recorded reason is not the
fingerprint: both producers ARE pure folds over `read_all`, so `segment_stats`
keys them correctly. The obstacle is that the **wall clock decides three
separate things inside the fold**, so a fold cached as-is freezes a BOARD rather
than a number:

1. a loop run's `online` — `status == "running"` and last event within that run's
   OWN `heartbeat_seconds`, not the chat window;
2. a chat session's `online` — last event within `ONLINE_WINDOW_SECONDS` (180 s);
3. whether a chat session is on the board AT ALL — idle past
   `ACTIVE_WINDOW_SECONDS` (6 h) and the fold drops it.

`dashboard_memo._with_online` was the seam for (2) only, and applying it to a
loop row would be actively wrong: it would call a run with a 15-minute heartbeat
offline after 180 seconds. So the three clock tests below hold the ledger
perfectly still and move only the clock, each asserting a cache HIT (zero reads)
in the same breath as the correct aged answer. Against a fold cached without a
re-derivation seam every one of them is red, which is the point.

Two genuine off-ledger inputs get a test each, for the same reason `chain.json`
got one on the overview: a file that changes with no event appended is how a
gated endpoint freezes while every number beside it still looks right.

Behavioural throughout: real HTTP into a real handler, counting the reads the
gate exists to skip. Never elapsed time (a flaky proxy), never source strings
(which cannot tell a working gate from a gate-shaped comment).
"""
import collections
import importlib
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, dashboard_get, events, oversee

OVERSEE = "/api/oversee?project=test"
OVERSEE_ALL = "/api/oversee?project=*"


def _clear_memos():
    for name, value in vars(dashboard).items():
        if name.endswith("_MEMO") and isinstance(value, collections.OrderedDict):
            value.clear()


@pytest.fixture(autouse=True)
def clean_module_state():
    _clear_memos()
    oversee._IU_CACHE.update(ts=0.0, map={})
    yield
    _clear_memos()
    oversee._IU_CACHE.update(ts=0.0, map={})


@pytest.fixture
def dashboard_server():
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
    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return self.fn(*a, **kw)


@pytest.fixture
def counters(monkeypatch):
    """A counter over the cost this gate exists to skip. Patched on the module
    that OWNS `read_all`, so `loop.state.list_runs`'s own call is counted too —
    it is half of what this endpoint currently spends."""
    ev_mod = importlib.import_module("mindhive.events")
    reads = _Counter(ev_mod.read_all)
    monkeypatch.setattr(ev_mod, "read_all", reads)

    class _Pair:
        def reset(self):
            reads.calls = 0
    pair = _Pair()
    pair.reads = reads
    return pair


class _FakeTime:
    """The real `time` module with a movable `time()`, substituted for
    `dashboard_get`'s module-level `time` — the same idiom
    tests/test_dashboard_locks.py uses to observe a cache hit across a clock
    change without sleeping."""

    def __init__(self, start):
        self.now = start

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch):
    c = _FakeTime(time.time())
    monkeypatch.setattr(dashboard_get, "time", c)
    return c


def _iso(epoch) -> str:
    """`events.now_iso()`'s format for an arbitrary epoch, so a seeded event can
    be placed exactly where the fake clock needs it."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + "Z"


def _chat(at, *, sid="sess-1", project="test", text="do the thing"):
    events.append_event("user_prompt", {"text": text}, session_id=sid,
                        ts=_iso(at), project=project)


def _loop(at, *, rid="run-1", project="test", heartbeat=60.0, goal_dir=""):
    events.append_event("loop_run_start",
                        {"run_id": rid, "title": "a loop run",
                         "goal_dir": goal_dir,
                         "heartbeat_seconds": heartbeat},
                        session_id="loop-sess", ts=_iso(at), project=project)


def _card(body, run_id):
    for r in body["runs"]:
        if r["run_id"] == run_id:
            return r
    return None


# --- the free win: one read per namespace, not two -------------------------

def test_the_ledger_is_read_once_per_namespace_not_twice(dashboard_server,
                                                        counters, clock):
    """`_get_oversee` read the ledger, then `list_runs` read it AGAIN, because
    `list_runs` had no `evs=` parameter the way `interactive_sessions` does.
    Halving the cost of every MISS matters more here than on /api/overview:
    misses stay common on an actively-written namespace, which is exactly the
    namespace anyone is looking at."""
    _chat(clock.now)
    _loop(clock.now)
    status, body = _get(dashboard_server[0], OVERSEE, dashboard_server[1])
    assert status == 200
    assert len(body["runs"]) == 2, "fixture must produce both kinds of card"
    assert counters.reads.calls == 1, (
        f"{counters.reads.calls} ledger reads for one namespace's board")


def test_an_unchanged_board_reads_no_ledger_at_all(dashboard_server, counters,
                                                   clock):
    base, token = dashboard_server
    _chat(clock.now)
    _loop(clock.now)
    _status, warm = _get(base, OVERSEE, token)
    assert counters.reads.calls >= 1, "the first poll must do the work"

    counters.reset()
    status, again = _get(base, OVERSEE, token)
    assert status == 200
    assert counters.reads.calls == 0, \
        f"an unchanged store was read {counters.reads.calls}x anyway"
    assert again == warm, "the gate served a different board"


def test_an_append_re_reads_that_namespace_and_only_that_one(dashboard_server,
                                                             counters, clock):
    base, token = dashboard_server
    _chat(clock.now)
    _chat(clock.now, sid="sess-other", project="other")
    _get(base, OVERSEE, token)
    _get(base, "/api/oversee?project=other", token)

    _chat(clock.now, text="a second ask")            # an append from outside

    counters.reset()
    status, _other = _get(base, "/api/oversee?project=other", token)
    assert status == 200
    assert counters.reads.calls == 0, \
        "an append to one namespace invalidated another one's board"

    counters.reset()
    status, after = _get(base, OVERSEE, token)
    assert status == 200
    assert counters.reads.calls == 1, \
        f"{counters.reads.calls} reads for one namespace's board"
    assert _card(after, "sess-1")["title"], "the append never reached the board"


# --- the three the clock decides ------------------------------------------

def test_a_loop_card_goes_offline_past_its_own_heartbeat(dashboard_server,
                                                         counters, clock):
    """RED against a cached fold, and NOT fixable by reusing `_with_online`:
    that seam applies the 180-second CHAT window, which would call a run with a
    15-minute heartbeat offline twelve times too early. A loop row carries
    `status` and its own `heartbeat_seconds`, so the rule is re-derivable from
    the row — which is what makes caching the fold legitimate at all."""
    base, token = dashboard_server
    _loop(clock.now, heartbeat=60.0)
    _status, warm = _get(base, OVERSEE, token)
    assert _card(warm, "run-1")["online"] is True, \
        "fixture must start online or there is nothing to age"
    assert warm["any_online"] is True

    counters.reset()
    clock.now += 61                                   # past its own heartbeat
    status, aged = _get(base, OVERSEE, token)
    assert status == 200
    assert counters.reads.calls == 0, \
        "the ledger did not move, so this must be a cache hit"
    assert _card(aged, "run-1")["online"] is False, \
        "a cached fold froze a loop run as online past its own heartbeat"
    assert aged["any_online"] is False, \
        "`any_online` was computed before the clock was re-derived"


def test_a_loop_card_stays_online_inside_its_own_heartbeat(dashboard_server,
                                                           counters, clock):
    """The other half, and the one a chat-window seam gets wrong. 200 seconds is
    past `ONLINE_WINDOW_SECONDS` and well inside a 15-minute heartbeat, so a
    seam that does not dispatch on `kind` fails HERE while passing above."""
    base, token = dashboard_server
    _loop(clock.now, heartbeat=None)                  # default: 15 min + slack
    _status, warm = _get(base, OVERSEE, token)
    assert _card(warm, "run-1")["online"] is True

    counters.reset()
    clock.now += 200                                  # > 180 s, << 16 min
    status, still = _get(base, OVERSEE, token)
    assert status == 200
    assert counters.reads.calls == 0
    assert _card(still, "run-1")["online"] is True, (
        "a live loop run was called offline after 200 s — the chat window was "
        "applied to a loop row instead of its own heartbeat")


def test_a_chat_card_goes_offline_past_the_online_window(dashboard_server,
                                                         counters, clock):
    base, token = dashboard_server
    _chat(clock.now)
    _status, warm = _get(base, OVERSEE, token)
    assert _card(warm, "sess-1")["online"] is True

    counters.reset()
    clock.now += 200                                  # past 180 s
    status, aged = _get(base, OVERSEE, token)
    assert status == 200
    assert counters.reads.calls == 0
    assert _card(aged, "sess-1")["online"] is False, \
        "a cached fold froze a chat session as working"
    assert _card(aged, "sess-1") is not None, \
        "offline is not gone — the card stays for six hours"


def test_a_chat_card_leaves_the_board_past_the_active_window(dashboard_server,
                                                             counters, clock):
    """The third clock decision, and the one that is a DROP rather than a field.
    `interactive_sessions` refuses to emit a session idle past
    `ACTIVE_WINDOW_SECONDS` at all, so a cached fold keeps an aged-out ghost on
    the board until the ledger next moves — on a namespace nobody is writing to,
    that is forever."""
    base, token = dashboard_server
    _chat(clock.now)
    _status, warm = _get(base, OVERSEE, token)
    assert _card(warm, "sess-1") is not None

    counters.reset()
    clock.now += oversee.ACTIVE_WINDOW_SECONDS + 1
    status, gone = _get(base, OVERSEE, token)
    assert status == 200
    assert counters.reads.calls == 0
    assert _card(gone, "sess-1") is None, \
        "a cached fold kept a session on the board six hours after it went idle"
    assert gone["runs"] == []


def test_a_plan_orphaned_by_an_aged_out_card_moves_to_the_plans_list(
        dashboard_server, counters, clock):
    """The consequence of that drop, one layer out, and the reason the plan
    split cannot be cached either. `plans` carries every roadmap plan with NO
    live card, computed by excluding the plan ids attached to cards — so when
    the clock removes a card, its plan has to appear in that list. Cache the
    split and the plan is invisible in both places at once: gone from the board
    with the card, and absent from the orphan list that exists to catch it."""
    base, token = dashboard_server
    from mindhive import roadmap_auto
    _chat(clock.now)
    roadmap_auto.sync_from_todos(
        "sess-1", [{"content": "the step", "status": "in_progress"}],
        project="test", ts=_iso(clock.now))

    _status, warm = _get(base, OVERSEE, token)
    assert _card(warm, "sess-1")["roadmap"]["plan_id"] == "sess-1"
    assert [p["plan_id"] for p in warm["plans"]] == [], \
        "a plan with a live card must not also be listed as orphaned"

    counters.reset()
    clock.now += oversee.ACTIVE_WINDOW_SECONDS + 1
    status, gone = _get(base, OVERSEE, token)
    assert status == 200
    assert counters.reads.calls == 0
    assert _card(gone, "sess-1") is None
    assert [p["plan_id"] for p in gone["plans"]] == ["sess-1"], (
        "the card aged out and took its plan with it — the roadmap is now "
        "orphaned in neither list")


# --- the two genuine off-ledger inputs ------------------------------------

def test_a_fix_plan_edit_invalidates_even_though_no_event_moved(
        dashboard_server, counters, clock, tmp_path):
    """DISCRIMINATING, and the exact shape of the `chain.json` guard on the
    overview. `list_runs` reads `<goal_dir>/fix_plan.md` off disk, so a run's
    plan can change with no event appended — and a ledger-only fingerprint does
    not move for it. The goal_dirs the cached entry holds have to join the
    stamp."""
    base, token = dashboard_server
    goal = tmp_path / ".hive-loop"
    goal.mkdir()
    plan = goal / "fix_plan.md"
    plan.write_text("- [ ] first thing\n", encoding="utf-8")
    _loop(clock.now, goal_dir=str(goal))

    _status, warm = _get(base, OVERSEE, token)
    card = _card(warm, "run-1")
    assert [i["text"] for i in card["planned"]] == ["first thing"], \
        "fixture must reach the fix-plan reader"

    plan.write_text("- [ ] first thing\n- [ ] second thing\n", encoding="utf-8")

    counters.reset()
    status, after = _get(base, OVERSEE, token)
    assert status == 200
    assert counters.reads.calls >= 1, (
        "the fix plan changed on disk and the board was served from the memo: "
        "a run's checklist would be frozen at whatever it said on the first "
        "poll, for the life of the process")
    assert [i["text"] for i in _card(after, "run-1")["planned"]] == \
        ["first thing", "second thing"]


def test_a_changed_intended_use_map_invalidates_even_though_no_event_moved(
        dashboard_server, counters, clock, monkeypatch):
    """DISCRIMINATING. `intended_use_map()` comes from the plugin inventory on
    disk and is TTL-memoized for 60 s, and it feeds `skill_trails` INSIDE the
    fold — so a fold cached against a ledger stamp can outlive the map it was
    built from, and a skill's "why" would report a description the inventory no
    longer carries."""
    base, token = dashboard_server
    _chat(clock.now)
    events.append_event("skill_used", {"name": "some-skill"},
                        session_id="sess-1", ts=_iso(clock.now), project="test")

    served = {"map": {"some-skill": "the first description"}}
    monkeypatch.setattr(oversee, "intended_use_map",
                        lambda *_a, **_kw: served["map"])
    _status, _warm = _get(base, OVERSEE, token)

    served["map"] = {"some-skill": "a different description"}
    counters.reset()
    status, _after = _get(base, OVERSEE, token)
    assert status == 200
    assert counters.reads.calls >= 1, (
        "the intended-use map changed and the board came from the memo — the "
        "fold was built from a map it has now outlived")


# --- the fingerprint's own two failure modes ------------------------------

def test_a_non_file_event_backend_bypasses_the_oversee_memo(
        dashboard_server, counters, clock, monkeypatch):
    """`read_all` short-circuits to Postgres when the backend is exactly `pg`,
    while `segment_stats` keeps stat'ing local segments that pg never writes:
    non-empty stamp, stable forever, every poll a hit, the board frozen at its
    first response."""
    base, token = dashboard_server
    _chat(clock.now)
    _status, warm = _get(base, OVERSEE, token)

    ev_mod = importlib.import_module("mindhive.events")
    monkeypatch.setattr(ev_mod, "_maybe_read_from_pg", lambda project=None: None)
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "pg")

    counters.reset()
    status, again = _get(base, OVERSEE, token)
    assert status == 200
    assert counters.reads.calls >= 1, \
        "a pg backend served the memo instead of reading"
    assert len(again["runs"]) == len(warm["runs"])


def test_an_empty_fingerprint_never_freezes_the_board(dashboard_server,
                                                      counters, clock,
                                                      monkeypatch):
    """`segment_stats` answers `{}` for ANY OSError as well as for "no events
    directory". Fail-safe for the sidecar, INVERTED as a cache key: `{} == {}`
    scores a hit, so one transient directory error would pin this board for the
    life of the process."""
    base, token = dashboard_server
    _chat(clock.now)
    _get(base, OVERSEE, token)
    sidecar = importlib.import_module("mindhive.mindnet.sidecar")
    monkeypatch.setattr(sidecar, "segment_stats", lambda *_a, **_kw: {})

    for attempt in (1, 2):
        counters.reset()
        status, body = _get(base, OVERSEE, token)
        assert status == 200
        assert counters.reads.calls >= 1, \
            f"poll {attempt} served an empty fingerprint from the memo"
        assert len(body["runs"]) == 1


def test_the_oversee_fingerprint_is_a_real_stamp_when_it_can_be_trusted():
    """CANARY for both guards above. They assert an ABSENCE of caching, which a
    fingerprint returning None unconditionally would also produce — and that
    version would be a cache that never caches, passing every guard here while
    restoring the full read on every poll."""
    from mindhive import dashboard_memo
    _chat(time.time())
    trusted = dashboard_memo._oversee_fingerprint("test", (), {})
    assert trusted, "a file-backend namespace produced no fingerprint at all"
    assert trusted == dashboard_memo._oversee_fingerprint("test", (), {}), \
        "the fingerprint moved on its own, with nothing appended"
    events.append_event("decision", {"text": "moved"}, session_id="s",
                        project="test")
    assert trusted != dashboard_memo._oversee_fingerprint("test", (), {}), \
        "the fingerprint did not move for a real append"


# --- HIVE MIND -------------------------------------------------------------

def test_hive_mind_does_not_evict_the_entries_it_just_wrote(dashboard_server,
                                                            counters, clock):
    """DISCRIMINATING against reusing `_MEMO_MAX`, which is 8 and sized for the
    picker's one-hot-key pattern. One HIVE MIND poll folds EVERY namespace — 60
    on the real machine — so an 8-slot memo evicts most of what the request just
    wrote and can never hit. Measured on /api/projects when that mistake was
    live: three identical passes cost 1329 / 1214 / 2134 ms."""
    base, token = dashboard_server
    for i in range(12):
        _chat(clock.now, sid=f"s{i}", project=f"p{i}")

    status, warm = _get(base, OVERSEE_ALL, token)
    assert status == 200
    assert counters.reads.calls >= 12, "the first pass must read every namespace"
    assert len(warm["runs"]) == 12

    counters.reset()
    status, again = _get(base, OVERSEE_ALL, token)
    assert status == 200
    assert counters.reads.calls == 0, (
        f"{counters.reads.calls} namespaces re-read on an unchanged store — the "
        f"memo is evicting entries the same request wrote")
    assert again == warm


# --- the two faults an adversarial audit found in the gate itself --------------
# Reported by independent review of the commit that added it, and each one
# REPRODUCED here before being fixed — the audit's own verification stage died to
# a usage limit, so nothing it said was taken on trust.

def test_an_append_that_lands_during_the_fold_is_not_baked_into_the_key():
    """RED against a fingerprint stamped AFTER the fold.

    The fold reads the ledger at T and the fingerprint was stat'ed at T+delta, so
    an event appended inside that window was described by the KEY while being
    absent from the BOARD. The next poll then matched the key and served the
    board that predates the event — and kept serving it, because only a further
    append could move the stamp again. On the real store this fold takes 900 ms,
    which is how wide that window is on the hottest projection in the dashboard.

    Direct rather than over HTTP, deliberately: the failure is a two-instant race
    inside one call, and the only way to place an append at the instant between
    them is to be the thing doing the folding."""
    from mindhive import dashboard_memo as dm

    events.append_event("loop_run_start",
                        {"run_id": "r1", "title": "first", "goal_dir": ""},
                        session_id="s1", project="test")
    folds = {"n": 0}

    def fold(inject):
        def _c():
            folds["n"] += 1
            titles = [e.payload.get("title") for e in events.read_all("test")
                      if e.type == "loop_run_start"]
            if inject:
                # Lands after the read, before the caller stamps.
                events.append_event(
                    "loop_run_start",
                    {"run_id": "r2", "title": "mid-fold", "goal_dir": ""},
                    session_id="s1", project="test")
            return {"runs": [{"run_id": t} for t in titles], "plans": [],
                    "dismissed": 0, "goal_dirs": ()}
        return _c

    iu = dm._intended_use_digest({})
    dm._oversee_cached("test", iu, time.time(), fold(inject=True))
    board = dm._oversee_cached("test", iu, time.time(), fold(inject=False))

    assert folds["n"] == 2, (
        "the second poll was a cache HIT, so the event that landed during the "
        "first fold can never reach the board")
    assert [r["run_id"] for r in board["runs"]] == ["first", "mid-fold"]


def test_an_unstattable_goal_dir_degrades_like_the_reader_instead_of_raising():
    """RED against `except OSError` alone: `os.stat` raises ValueError, not
    OSError, for a path with an embedded NUL — before it reaches the OS.

    `goal_dir` comes straight off a `loop_run_start` payload, which
    `loop.state._fix_plan_items` documents as untrusted and survives (pathlib's
    `is_file()` swallows the ValueError, so the card renders with an empty
    checklist). The gate went through `os.stat` directly and let it escape, and
    this store already holds 7 corrupt rows, so a NUL is not hypothetical. The
    escape reaches `_get_oversee` and 500s the endpoint on EVERY poll — the whole
    Oversee board, loop cards and awaiting-resume plans alike — permanently,
    because an append-only ledger cannot un-write the row."""
    from mindhive import dashboard_memo as dm
    from mindhive.loop import state as loop_state

    bad = "C:/x\x00y/.hive-loop"
    # The rule being matched: the reader degrades rather than raising.
    assert loop_state._fix_plan_items(bad) == {"done": [], "planned": [],
                                              "not_yet": []}
    # So the gate must treat it as a fault (None poisons the fingerprint, which
    # means "do not cache"), never as an exception.
    assert dm._fix_plan_fingerprint((bad,)) is None
    assert dm._oversee_fingerprint("test", (bad,), "digest") is None
