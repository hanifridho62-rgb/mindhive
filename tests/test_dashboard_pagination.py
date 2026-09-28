# SPDX-License-Identifier: Apache-2.0
"""`?page=&page_size=` on /api/memory and /api/sessions (blueprint P5, Pillar 3).

The binding plan decision (Pilar 3 row) says the two polled list routes take
the page parameters ADDITIVELY. That word is the whole contract, and it has
four parts, each held here:

* WITHOUT the parameters the body is byte-for-byte today's. The two
  `*_legacy_body_*` tests pin today's bytes over a fixed-timestamp ledger;
  they pass before the feature exists and are the tests that must still pass
  after it. A blank `?page=` is the caller declining to say (`qs_int`'s rule
  for an empty value), so it gets the same bytes.
* WITH either parameter the body is `pgshape.paginate_records`' envelope: one
  page of exactly the rows the unpaged body lists, in the same order, with
  top-level strings over 500 characters cut by the §10.8.2 marker.
* The request boundary clamps `page_size` through `qs_int`'s new `ceiling`,
  and a value that will not parse is refused by name with a 400 -- the
  existing `qs_int` behaviour, which a 200 that silently ignored it would not
  be.
* Paging runs AFTER the stat-only gate. Turning pages reads nothing.

On /api/memory, `limit` keeps its meaning in both modes: it selects the newest
N before the collapse, and paging slices what it selected. Pinned, because
"additive" could be read the other way. The selection has a default (200), so
a paged answer's `total_items` can be the total of the selection rather than
of the store -- and `pgshape`'s own contract says a wrong total is worse than
none. So whenever the selection DROPPED rows, named `limit` or default, the
envelope says so with a `limit` key holding the N it kept; when it dropped
nothing the key is absent and `total_items` is the whole answer. Detected, not
assumed: a `limit` at or above the row count adds no key.

Store isolation is the autouse `isolate_store` fixture in tests/conftest.py;
the memos are process-global, so they are swept around every test here the
way tests/test_dashboard_locks.py sweeps them.
"""
import collections
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, dashboard_memo, events
from mindhive.dashboard_query import BadRequest, qs_int

TOKEN = "test-token-p5-pages"
ROUTES = ["/api/memory", "/api/sessions"]
MARK = "... [TRUNCATED: {} chars hidden]"      # §10.8.2, verbatim


# --- fixture builders -------------------------------------------------------

def _sweep_memos():
    """Empty every module-level `*_MEMO`, enumerated rather than named -- the
    per-route memos live in `dashboard_memo` and are not all re-exported by
    `dashboard`, so both namespaces are swept."""
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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _raw(base, path):
    """`(status, body bytes)`. Bytes, because the legacy pin is byte-for-byte;
    an error status is part of the contract here, so it is returned, not
    raised."""
    sep = "&" if "?" in path else "?"
    try:
        with urllib.request.urlopen(f"{base}{path}{sep}token={TOKEN}",
                                    timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _get(base, path):
    status, raw = _raw(base, path)
    return status, json.loads(raw.decode("utf-8"))


def _seed_golden():
    """Four events in two sessions at FIXED timestamps: three memory-view
    types and one (`command_run`) that is not, a non-ASCII character to pin
    `ensure_ascii=False`, and stamps old enough that `status` is `completed`
    on any clock."""
    for etype, text, sid, ts in (
            ("decision", "ship the café parser first", "sess-a",
             "2026-01-01T00:00:01Z"),
            ("caveat", "never trim the ledger itself", "sess-b",
             "2026-01-01T00:00:02Z"),
            ("command_run", "pytest -q", "sess-b", "2026-01-01T00:00:03Z"),
            ("user_prompt", "what is left to do?", "sess-a",
             "2026-01-01T00:00:04Z")):
        events.append_event(etype, {"text": text}, session_id=sid, ts=ts,
                            project="test")


# Today's answers for `_seed_golden`, read off the unmodified handlers
# (worktree HEAD c22601d) and written out as data. `json.dumps(...,
# ensure_ascii=False)` is exactly what `_json` sends, so equality of the
# encoded strings is equality of the bytes on the wire -- key order included.
GOLDEN_MEMORY = [
    {"ts": "2026-01-01T00:00:04Z", "type": "user_prompt", "session": "sess-a",
     "project": "test", "text": "what is left to do?", "tags": [],
     "origin": None, "similar_count": 0},
    {"ts": "2026-01-01T00:00:02Z", "type": "caveat", "session": "sess-b",
     "project": "test", "text": "never trim the ledger itself", "tags": [],
     "origin": None, "similar_count": 0},
    {"ts": "2026-01-01T00:00:01Z", "type": "decision", "session": "sess-a",
     "project": "test", "text": "ship the café parser first", "tags": [],
     "origin": None, "similar_count": 0},
]
GOLDEN_SESSIONS = [
    {"session_id": "sess-a", "project": "test", "events": 2, "memory": 2,
     "origin": None, "first_ts": "2026-01-01T00:00:01Z",
     "last_ts": "2026-01-01T00:00:04Z",
     "activity": {"files": 0, "plus": 0, "minus": 0, "commands": 0,
                  "notes": 1, "failures": 0, "loops": 0},
     "status": "completed"},
    {"session_id": "sess-b", "project": "test", "events": 2, "memory": 1,
     "origin": None, "first_ts": "2026-01-01T00:00:02Z",
     "last_ts": "2026-01-01T00:00:03Z",
     "activity": {"files": 0, "plus": 0, "minus": 0, "commands": 1,
                  "notes": 1, "failures": 0, "loops": 0},
     "status": "completed"},
]

N = 25
LONG_TEXT = "note 24 " + "x" * 600       # the newest row's text, 608 characters


def _seed_many():
    """N single-event sessions one second apart, the newest LAST, so both
    routes list them newest first as sess-24 .. sess-00. Texts are distinct
    enough that `dedupe.collapse` keeps every row (Jaccard 1/3 between any
    two), and the newest carries a 608-character text so page 1 has a field
    to trim."""
    for i in range(N):
        text = LONG_TEXT if i == N - 1 else f"note {i:02d}"
        events.append_event("decision", {"text": text},
                            session_id=f"sess-{i:02d}",
                            ts=f"2026-01-01T00:00:{i:02d}Z", project="test")


def _as_paged(rows):
    """*rows* as a page must carry them: every top-level string over 500
    characters cut with the §10.8.2 marker, everything else untouched. Written
    out here rather than borrowed from `pgshape`, so the oracle is not the code
    under test."""
    def cut(value):
        if isinstance(value, str) and len(value) > 500:
            return value[:500] + MARK.format(len(value) - 500)
        return value
    return [{k: cut(v) for k, v in row.items()} for row in rows]


def _envelope(status, body):
    assert status == 200, body
    assert isinstance(body, dict), \
        f"expected the pagination envelope, got a {type(body).__name__}"
    return body


# --- qs_int's ceiling -------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("99", 99), ("100", 100), ("101", 100), ("1000000000", 100),
    ("0", 0), ("-1", -1)])
def test_qs_int_ceiling_caps_the_value_from_above_and_only_from_above(
        raw, expected):
    """A ceiling, not a clamp: the floor for a page size is
    `paginate_records`' business, and a `qs_int` that also floored would be a
    second, disagreeing copy of it."""
    assert qs_int({"page_size": [raw]}, "page_size", 20, ceiling=100) == expected


def test_qs_int_ceiling_leaves_the_default_and_the_refusal_as_they_were():
    assert qs_int({}, "page_size", 20, ceiling=100) == 20
    assert qs_int({"page_size": [""]}, "page_size", 20, ceiling=100) == 20
    with pytest.raises(BadRequest) as exc:
        qs_int({"page_size": ["abc"]}, "page_size", 20, ceiling=100)
    assert str(exc.value) == "invalid page_size"


def test_qs_int_with_no_ceiling_is_unbounded_for_every_existing_caller():
    """REGRESSION PIN (passes before the feature exists): `limit`, `since` and
    the rest never had a ceiling, and an optional parameter must not give them
    one."""
    assert qs_int({"limit": ["100000"]}, "limit", 200) == 100000


def test_qs_int_ceiling_none_is_the_same_as_no_ceiling():
    assert qs_int({"limit": ["100000"]}, "limit", 200, ceiling=None) == 100000


# --- without page parameters: today's body, byte for byte --------------------

BLANKS = ["", "&page=", "&page_size=", "&page=&page_size="]
BLANK_IDS = ["absent", "blank-page", "blank-page_size", "both-blank"]


@pytest.mark.parametrize("suffix", BLANKS, ids=BLANK_IDS)
def test_memory_without_page_parameters_is_the_legacy_body_byte_for_byte(
        base, suffix):
    """REGRESSION PIN (passes before the feature exists)."""
    _seed_golden()
    status, raw = _raw(base, "/api/memory?project=test" + suffix)
    assert status == 200
    assert raw == json.dumps(GOLDEN_MEMORY, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("suffix", BLANKS, ids=BLANK_IDS)
def test_sessions_without_page_parameters_is_the_legacy_body_byte_for_byte(
        base, suffix):
    """REGRESSION PIN (passes before the feature exists)."""
    _seed_golden()
    status, raw = _raw(base, "/api/sessions?project=test" + suffix)
    assert status == 200
    assert raw == json.dumps(GOLDEN_SESSIONS, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("route", ROUTES)
def test_without_page_parameters_every_row_is_listed_and_none_is_trimmed(
        base, route):
    """REGRESSION PIN (passes before the feature exists). Kills paging that is
    always on: 25 rows is more than the default page of 20, and the newest
    memory row is longer than the 500-character field cap."""
    _seed_many()
    status, rows = _get(base, f"{route}?project=test")
    assert status == 200 and isinstance(rows, list)
    assert len(rows) == N
    if route == "/api/memory":
        assert rows[0]["text"] == LONG_TEXT


# --- with page parameters: the envelope --------------------------------------

@pytest.mark.parametrize("route", ROUTES)
def test_page_two_of_ten_is_the_second_slice_of_the_unpaged_list(base, route):
    _seed_many()
    _status, legacy = _get(base, f"{route}?project=test")

    env = _envelope(*_get(base, f"{route}?project=test&page=2&page_size=10"))

    assert env == {"page": 2, "page_size": 10, "total_items": N,
                   "total_pages": 3, "has_next": True, "has_prev": True,
                   "items": _as_paged(legacy[10:20])}


def test_a_paged_memory_row_carries_its_long_text_cut_to_500_characters(base):
    _seed_many()
    _status, legacy = _get(base, "/api/memory?project=test")
    # CANARY: the fixture really has a long field, and unpaged it is whole --
    # so the cut below is the paging, not the fixture.
    assert legacy[0]["text"] == LONG_TEXT

    env = _envelope(*_get(base, "/api/memory?project=test&page=1&page_size=5"))

    assert env["items"][0] == dict(
        legacy[0], text=LONG_TEXT[:500] + MARK.format(len(LONG_TEXT) - 500))
    assert env["items"][1:] == legacy[1:5]


@pytest.mark.parametrize("route", ROUTES)
def test_either_parameter_alone_turns_paging_on_with_the_other_defaulted(
        base, route):
    _seed_many()
    _status, legacy = _get(base, f"{route}?project=test")

    by_size = _envelope(*_get(base, f"{route}?project=test&page_size=5"))
    by_page = _envelope(*_get(base, f"{route}?project=test&page=2"))

    assert (by_size["page"], by_size["page_size"], by_size["total_pages"]) == \
        (1, 5, 5)
    assert by_size["items"] == _as_paged(legacy[0:5])
    assert (by_page["page"], by_page["page_size"], by_page["total_pages"]) == \
        (2, 20, 2)
    assert by_page["items"] == _as_paged(legacy[20:25])
    assert by_page["has_next"] is False and by_page["has_prev"] is True


def test_memory_limit_still_selects_before_paging_slices(base):
    """...and, because the 10 it kept are not the 25 there are, the envelope
    says it was limited: `total_items` is the selection's total, not the
    store's."""
    _seed_many()
    _status, legacy = _get(base, "/api/memory?project=test&limit=10")
    assert len(legacy) == 10

    env = _envelope(*_get(
        base, "/api/memory?project=test&limit=10&page=2&page_size=4"))

    assert env == {"page": 2, "page_size": 4, "total_items": 10,
                   "total_pages": 3, "has_next": True, "has_prev": True,
                   "limit": 10, "items": _as_paged(legacy[4:8])}
    assert list(env)[-2:] == ["limit", "items"], \
        "the page's own keys keep their order; `limit` rides just before items"


@pytest.mark.parametrize("limit", [N, N + 1, 1000])
def test_a_limit_that_drops_nothing_adds_no_key(base, limit):
    """Detected, not assumed. `N` itself is the boundary: a `limit` equal to
    the row count keeps every row, so `total_items` is already the true total
    and a `limit` key would call a whole answer partial."""
    _seed_many()

    env = _envelope(*_get(
        base, f"/api/memory?project=test&limit={limit}&page=1&page_size=10"))

    assert "limit" not in env
    assert env["total_items"] == N


def _seed_past_the_default(n):
    """*n* distinct single-row sessions, one second apart, newest last."""
    for i in range(n):
        events.append_event("decision", {"text": f"memo {i:03d}"},
                            session_id=f"deep-{i:03d}",
                            ts=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
                            project="test")


def test_a_page_of_a_store_past_the_default_selection_says_it_was_limited(base):
    """The failure pgshape names: 201 memories, and a page envelope that
    reported `total_items: 200` with `has_next: False` on its last page and
    nothing to say it had stopped counting. It still selects the newest 200 --
    `dedupe.collapse` is quadratic in what it is handed, so the default bound
    is what keeps a page turn cheap -- but it now says so."""
    _seed_past_the_default(201)
    _status, legacy = _get(base, "/api/memory?project=test")
    assert len(legacy) == 200          # REGRESSION PIN: the legacy default

    env = _envelope(*_get(base, "/api/memory?project=test&page=2&page_size=100"))

    assert env == {"page": 2, "page_size": 100, "total_items": 200,
                   "total_pages": 2, "has_next": False, "has_prev": True,
                   "limit": 200, "items": _as_paged(legacy[100:200])}


def test_a_store_exactly_at_the_default_selection_is_not_called_limited(base):
    """CANARY for the test above, at the boundary: 200 rows, all kept, so the
    total is the true one and the envelope must not claim otherwise."""
    _seed_past_the_default(200)

    env = _envelope(*_get(base, "/api/memory?project=test&page=1&page_size=100"))

    assert "limit" not in env
    assert (env["total_items"], env["total_pages"], env["has_next"]) == \
        (200, 2, True)


# --- the boundaries, on the wire ---------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("asked,used", [
    ("0", 1), ("-1", 1), ("100", 100), ("101", 100), ("1000000000", 100)])
def test_page_size_is_clamped_into_one_to_one_hundred_on_the_wire(
        base, route, asked, used):
    _seed_many()

    env = _envelope(*_get(base, f"{route}?project=test&page_size={asked}"))

    assert env["page_size"] == used
    assert len(env["items"]) == min(used, N)
    assert env["total_pages"] == -(-N // used)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("asked", ["0", "-5"])
def test_a_page_below_one_is_the_first_page_on_the_wire(base, route, asked):
    _seed_many()
    _status, legacy = _get(base, f"{route}?project=test")

    env = _envelope(*_get(base, f"{route}?project=test&page={asked}"))

    assert env["page"] == 1 and env["has_prev"] is False
    assert env["items"] == _as_paged(legacy[0:20])


@pytest.mark.parametrize("route", ROUTES)
def test_a_page_far_past_the_end_is_empty_with_the_true_totals(base, route):
    _seed_many()

    env = _envelope(*_get(base, f"{route}?project=test&page=999999999999"))

    assert env == {"page": 999999999999, "page_size": 20, "total_items": N,
                   "total_pages": 2, "has_next": False, "has_prev": True,
                   "items": []}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("query,named", [
    ("page=abc", "page"), ("page=1e3", "page"),
    ("page_size=abc", "page_size"), ("page_size=2.5", "page_size")])
def test_an_unparseable_page_parameter_is_refused_by_name(base, route, query,
                                                          named):
    """`qs_int`'s existing rule on a new parameter: the caller's mistake,
    named, over a 400 -- not an interpreter sentence over a 500, and not a 200
    that ignored what the caller asked for."""
    _seed_many()

    status, body = _get(base, f"{route}?project=test&{query}")

    assert status == 400
    assert body == {"ok": False, "error": f"invalid {named}"}
    # CANARY: a parseable value on the same route is answered, so the refusal
    # above is about the value and not the parameter.
    ok_status, _body = _get(base, f"{route}?project=test&{named}=2")
    assert ok_status == 200


# --- the gate still holds ---------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
def test_turning_pages_reads_nothing_once_the_gate_is_warm(base, monkeypatch,
                                                           route):
    """Paging is a cut of the cached fold, like `session` and `limit` before
    it. Keying the memo on the page, or bypassing it to page, would put a full
    ledger read back behind every page turn."""
    _seed_many()
    real, reads = events.read_all, []

    def counting(*args, **kwargs):
        reads.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(events, "read_all", counting)
    _get(base, f"{route}?project=test")                   # warms the gate
    assert reads, "the first request must actually fold the ledger"
    reads.clear()

    for page in (1, 2, 3):
        env = _envelope(*_get(
            base, f"{route}?project=test&page={page}&page_size=10"))
        assert env["page"] == page

    assert reads == [], f"{route}: paging re-read the ledger {len(reads)} time(s)"
