# tests/test_mindnet_cat_board.py
"""MN-5 U9 — the cat board reaches the dashboard.

Written TEST-FIRST against
``docs/plans/2026-07-27-001-feat-mindnet-mn3-mn4-mn5-mechanical-enforcement-plan.md``
U9.

## The endpoint is tested over real HTTP, not by calling the handler

`ui_cats.board` is already covered as a pure function in
`test_mindnet_ui_cats.py`. What this file adds is everything BETWEEN that
function and the browser: the route being registered, the token gate applying,
the payload surviving JSON serialization, and — the one that actually matters —
the 6-hour filter still being in force at the HTTP layer. A filter that works in
the provider and is bypassed by the endpoint is a filter that does not work.

## The anti-regression assertion is deliberate

`swarm_page.py` renders the MindSwarm UI the user works in daily. This unit adds
a strip to the top of it, and the project's own instructions record what happens
when a dashboard edit silently drops a section the user relies on: it reads
exactly like their work was deleted. `test_the_existing_swarm_page_hooks_survive`
is cheap insurance against precisely that.
"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, events
from mindhive.mindnet import telemetry as tel
from mindhive.mindnet import ui_cats as ui
from mindhive.swarm_page import render_swarm


@pytest.fixture
def server():
    token = "cat-token-123"
    handler = dashboard.make_handler(token)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", token
    finally:
        srv.shutdown()


def _get(base, path, token=None, expect=200):
    url = base + path
    if token:
        url += ("&" if "?" in url else "?") + f"token={token}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _emit(faculty, status=tel.STATUS_ACTIVE, ago=5, task_id="t1",
          phase="execution"):
    """A real `mindnet_activity` row on the real ledger, aged *ago* seconds."""
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(seconds=ago)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    return tel.emit(faculty, status, task_id=task_id, phase=phase, ts=ts)


# --- the endpoint -----------------------------------------------------------------

def test_the_route_is_registered(server):
    base, token = server
    status, _body = _get(base, "/api/mindnet/cats", token)
    assert status == 200


def test_an_empty_feed_is_an_empty_board_not_an_error(server):
    """A project that has never run a mindnet command must get an empty list and
    a 200, not a 404 or a 500 — the UI polls this every second."""
    base, token = server
    status, body = _get(base, "/api/mindnet/cats", token)
    assert status == 200
    assert body["cats"] == []


def test_the_route_requires_a_token(server):
    base, _token = server
    status, body = _get(base, "/api/mindnet/cats")
    assert status == 403
    assert body["ok"] is False


def test_an_active_faculty_appears_on_the_board(server):
    base, token = server
    _emit("task-graph", ago=5)
    _status, body = _get(base, "/api/mindnet/cats", token)
    assert [c["faculty"] for c in body["cats"]] == ["task-graph"]
    assert body["cats"][0]["display"] == ui.DISPLAY_ACTIVE


def test_a_faculty_idle_past_the_dim_threshold_renders_dim(server):
    base, token = server
    _emit("sop", ago=ui.DIM_AFTER_SECS + 60)
    _status, body = _get(base, "/api/mindnet/cats", token)
    assert body["cats"][0]["display"] == ui.DISPLAY_DIM


def test_a_faculty_older_than_six_hours_is_filtered_out_at_the_endpoint(server):
    """R13 asserted where it actually has to hold. A provider-only filter that
    the endpoint bypasses is not a filter."""
    base, token = server
    _emit("sop", ago=ui.HIDE_AFTER_SECS + 60)
    _status, body = _get(base, "/api/mindnet/cats", token)
    assert body["cats"] == []


def test_a_mix_of_ages_returns_only_the_live_ones(server):
    base, token = server
    _emit("task-graph", ago=5)
    _emit("sop", ago=ui.DIM_AFTER_SECS + 10)
    _emit("history", ago=ui.HIDE_AFTER_SECS + 10)
    _status, body = _get(base, "/api/mindnet/cats", token)
    got = {c["faculty"]: c["display"] for c in body["cats"]}
    assert got == {"task-graph": ui.DISPLAY_ACTIVE, "sop": ui.DISPLAY_DIM}


def test_the_board_is_in_canonical_faculty_order(server):
    base, token = server
    for f in ("pr-green", "sop", "task-graph"):
        _emit(f, ago=5)
    _status, body = _get(base, "/api/mindnet/cats", token)
    got = [c["faculty"] for c in body["cats"]]
    assert got == [f for f in tel.FACULTIES if f in set(got)]


def test_each_cat_carries_the_metadata_the_panel_shows(server):
    """R12 — skill/plugin, task_id and phase, over the wire."""
    base, token = server
    _emit("task-graph", ago=5, task_id="mn5-u9", phase="execution")
    _status, body = _get(base, "/api/mindnet/cats", token)
    cat = body["cats"][0]
    assert cat["task_id"] == "mn5-u9"
    assert cat["phase"] == "execution"
    assert "skill" in cat and "age_secs" in cat


def test_the_payload_names_the_thresholds_so_the_ui_need_not_respell_them(
        server):
    """One home for 180 s and 6 h (invariant 10). A second copy in JavaScript is
    a second thing that can drift, and the drift would be invisible — the board
    would just quietly disagree with the tests about what 'idle' means."""
    base, token = server
    _status, body = _get(base, "/api/mindnet/cats", token)
    assert body["dim_after_secs"] == ui.DIM_AFTER_SECS
    assert body["hide_after_secs"] == ui.HIDE_AFTER_SECS
    assert body["idle_vanish_secs"] == ui.IDLE_VANISH_SECS


def test_a_finished_faculty_is_gone_at_the_endpoint_too():
    """The disappearance rule asserted where it has to hold. A rule that works in
    the provider and is bypassed by the endpoint is not a rule — the same reason
    the 6-hour filter has its own endpoint test."""
    # Deliberately not the `server` fixture: this needs two separate feeds.
    handler = dashboard.make_handler("t")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        _emit("sop", status=tel.STATUS_IDLE, ago=ui.IDLE_VANISH_SECS + 30)
        _status, body = _get(base, "/api/mindnet/cats", "t")
        assert body["cats"] == [], f"finished faculty still served: {body}"

        # Canary: the same feed with an ACTIVE row at the same age must render,
        # so the empty result above is the status rule and not an empty feed.
        _emit("task-graph", status=tel.STATUS_ACTIVE,
              ago=ui.IDLE_VANISH_SECS + 30)
        _status, body = _get(base, "/api/mindnet/cats", "t")
        assert [c["faculty"] for c in body["cats"]] == ["task-graph"]
    finally:
        srv.shutdown()


def test_the_page_does_not_hardcode_the_threshold_numbers():
    """The other half: the payload carrying them is only useful if the page is
    not also carrying its own copy."""
    import re
    script = render_swarm("tok").split("<script>")[-1]
    # Comments stripped first: the block explains WHY the thresholds live
    # server-side and names them doing so, and a raw substring check would fail
    # on its own rationale.
    code = re.sub(r"//[^\n]*", "", script)
    assert str(ui.HIDE_AFTER_SECS) not in code
    assert str(ui.DIM_AFTER_SECS) not in code


def test_a_torn_activity_row_does_not_take_out_the_board(server):
    """`telemetry.fold` skips malformed rows and the endpoint must inherit that
    — the board is a debugging tool and is least useful when it is down."""
    base, token = server
    events.append_event("mindnet_activity", {"faculty": "nope", "status": 42},
                        tags=["mindnet"])
    _emit("task-graph", ago=5)
    status, body = _get(base, "/api/mindnet/cats", token)
    assert status == 200
    assert [c["faculty"] for c in body["cats"]] == ["task-graph"]


# --- the page ---------------------------------------------------------------------

def test_the_swarm_page_renders_the_cat_strip():
    page = render_swarm("tok")
    assert "mn-cats" in page


def test_the_swarm_page_polls_the_cat_endpoint():
    assert "/api/mindnet/cats" in render_swarm("tok")


def test_the_swarm_page_carries_a_metadata_panel():
    """R12: clicking a cat has to show something."""
    page = render_swarm("tok")
    assert "mn-cat-panel" in page


def test_the_existing_swarm_page_hooks_survive():
    """Anti-regression. The project's instructions record what a silently
    dropped dashboard section looks like from the user's side: exactly like
    their work was deleted."""
    page = render_swarm("tok")
    for hook in ("mission", "/api/workspace/agents", "stage", "poll"):
        assert hook in page, f"the swarm page lost '{hook}'"


# --- MN-7 U1: seven faculties, seven cats -----------------------------------------
#
# The board shipped with one shared `CAT_SVG` silhouette assigned to every
# faculty, so all seven were distinguishable only by their text label — against
# the ask that each faculty map to a distinct sprite or palette. These tests pin
# the fix and, more importantly, pin the ONE LEGEND: the faculty order comes from
# `telemetry.FACULTIES` and nowhere else, so a faculty added there cannot end up
# uncoloured or colliding with an existing one.

def _palettes(page) -> list:
    """Every ``(body, patch, eye)`` colour triple in the page's palette table.

    The triple, not the body colour alone: `white` and `calico` deliberately
    share a body and differ by patch, so a body-only comparison would call two
    genuinely distinct cats duplicates.

    Parsed as JSON rather than scraped with a regex: the table is now serialized
    from `cat_board.CAT_PALETTES`, so `json.loads` reads exactly what the browser
    will and a colour-format change cannot make this helper silently return [].
    An earlier regex version did exactly that — it matched the hand-written
    ``b:'#fff'`` form, and when the same table started arriving JSON-quoted it
    reported zero palettes for a page that carried all seven."""
    marker = "const CAT_PALETTES = "
    block = page[page.index(marker) + len(marker):]
    table = json.loads(block[:block.index(";")])
    assert table, "the palette table parsed as empty — the helper is broken"
    return [(p["b"], p["p"], p["e"]) for p in table]


def test_the_page_carries_at_least_one_palette_per_faculty():
    """The structural guard. `FACULTIES` has 7 entries; a 6-entry palette table
    means two faculties share a cat, which is the defect this unit fixes."""
    pals = _palettes(render_swarm("tok"))
    assert len(pals) >= len(tel.FACULTIES), (
        f"{len(pals)} palettes for {len(tel.FACULTIES)} faculties — at least "
        f"two faculties would render identically")


def test_every_palette_is_visually_distinct():
    """A 7-entry table that repeats a triple fixes nothing."""
    pals = [tuple(c.lower() for c in p) for p in _palettes(render_swarm("tok"))]
    assert len(set(pals)) == len(pals), f"duplicate palettes: {pals}"


def test_the_faculty_order_is_injected_from_the_one_legend():
    """Derived, not hand-written. A second faculty list in JavaScript is a place
    for the legend to drift, and the drift direction is 'the new faculty renders
    as whatever index -1 happens to mean'."""
    page = render_swarm("tok")
    assert json.dumps(list(tel.FACULTIES)) in page


def test_the_faculty_cats_no_longer_share_one_sprite():
    """The canary for the three tests above. `el.innerHTML = CAT_SVG` was the
    defect; if that assignment is still in the page, the palettes exist and
    nothing uses them."""
    page = render_swarm("tok")
    assert "el.innerHTML = CAT_SVG" not in page
    assert "CAT_SVG" not in page, (
        "the shared silhouette constant is now dead code — remove it rather "
        "than leaving a second sprite path a future edit can wire back up")


def test_the_faculty_cat_index_is_derived_from_the_injected_order():
    page = render_swarm("tok")
    assert "FACULTY_ORDER" in page
    assert "indexOf" in page[page.index("FACULTY_ORDER"):]


def test_a_dimmed_cat_is_desaturated_not_merely_faded():
    """Full-colour sprites break the old dim signal: the shared silhouette used
    `currentColor`, so `.mn-cat.dim`'s grey text colour greyed the cat too. A
    coloured cat needs an explicit filter or the 180-second band — the whole
    hang-detection feature — stops being visible at a glance."""
    page = render_swarm("tok")
    dim = page[page.index(".mn-cat.dim"):]
    dim = dim[:dim.index("#mn-cat-panel")]
    assert "grayscale" in dim
    assert "opacity" in page[page.index(".mn-cat.dim"):][:200]


def test_the_palette_modulo_follows_the_table_length():
    """A hardcoded `% 6` beside a 7-entry table silently makes the 7th palette
    unreachable — the defect would survive the fix."""
    page = render_swarm("tok")
    # Paired positive: absence of the old literal alone would also pass if a
    # regression swapped `% 6` for a hardcoded `% 7`, reintroducing the exact
    # defect class (the next added palette becomes unreachable).
    assert "% CAT_PALETTES.length" in page
    assert "% 6" not in page, "hardcoded palette modulo disagrees with the table"


def test_the_token_is_still_escaped_in_the_rendered_page():
    """`render_swarm` escapes every '<' in the JSON-encoded token so neither
    '</script>' nor '<!--' can break out of the inline script. Adding markup
    above it must not disturb that."""
    page = render_swarm("</script><script>alert(1)</script>")
    assert "</script><script>alert(1)" not in page
    assert "\\u003c" in page


# --- MN-19: session cats ride the same poll ---------------------------------------

def _prompt(session_id="sess-live", ago=2, text="fixing the parser"):
    """A real `user_prompt` row — one of `oversee._ACTIVITY_TYPES`, i.e. the
    telemetry that already means "this session is doing something"."""
    from datetime import datetime, timedelta, timezone

    from mindhive import events as ev_mod
    ts = (datetime.now(timezone.utc) - timedelta(seconds=ago)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    return ev_mod.append_event("user_prompt", {"text": text},
                               session_id=session_id, ts=ts)


def test_the_payload_carries_both_boards_and_the_producer_map(server):
    """One poll, two questions. A second endpoint would double the request rate of
    a strip that already polls every 1.2 s for data this fold has in hand."""
    base, token = server
    _status, body = _get(base, "/api/mindnet/cats", token)
    for key in ("cats", "sessions", "sessions_total", "sessions_hidden",
                "producers", "online_after_secs"):
        assert key in body, f"payload is missing {key}"


def test_the_faculty_shape_is_unchanged_for_existing_consumers(server):
    """The session board is additive. `paintCats` and its tests predate it and
    must keep reading exactly what they read before."""
    base, token = server
    _status, body = _get(base, "/api/mindnet/cats", token)
    for key in ("cats", "dim_after_secs", "hide_after_secs", "idle_vanish_secs"):
        assert key in body


def test_a_working_session_appears_even_with_no_faculty_rows(server):
    """The operator's actual situation: sessions running, faculty feed empty. The
    board previously had nothing to say in that state, which read as "nothing is
    happening"."""
    base, token = server
    _prompt("sess-live", ago=2)
    _status, body = _get(base, "/api/mindnet/cats", token)
    assert body["cats"] == [], "no faculty rows were written by this test"
    ids = [s["session_id"] for s in body["sessions"]]
    assert "sess-live" in ids, body["sessions"]
    row = next(s for s in body["sessions"] if s["session_id"] == "sess-live")
    assert row["display"] == "working"


def test_a_quiet_session_still_renders_but_not_as_working(server):
    """Past the online window it is resting, not gone — the session has not ended,
    and hiding it would lose the operator's place."""
    base, token = server
    from mindhive import oversee
    _prompt("sess-quiet", ago=int(oversee.ONLINE_WINDOW_SECONDS) + 30)
    _status, body = _get(base, "/api/mindnet/cats", token)
    row = next((s for s in body["sessions"] if s["session_id"] == "sess-quiet"), None)
    assert row is not None and row["display"] == "resting"


def test_the_producer_map_reports_this_builds_signals(server):
    """The gap that cost this repo an entire investigation: a checkout with no
    `playbook` module rendered identically to a quiet one. Asserted by asking the
    same question the endpoint does, not by restating an expected literal."""
    base, token = server
    from mindhive.mindnet import telemetry as tel_mod
    _status, body = _get(base, "/api/mindnet/cats", token)
    assert body["producers"] == tel_mod.producers()
    assert set(body["producers"]) >= {"faculty", "swarm", "session"}


def test_the_session_thresholds_come_from_the_server(server):
    """Invariant 10: the page must never restate a number the server owns."""
    base, token = server
    from mindhive import oversee
    _status, body = _get(base, "/api/mindnet/cats", token)
    assert body["online_after_secs"] == oversee.ONLINE_WINDOW_SECONDS


def test_the_overflow_is_reported_rather_than_dropped(server):
    """A capped strip that under-reports is a strip that lies about how much is
    running."""
    base, token = server
    from mindhive.mindnet import ui_sessions as sess_mod
    for i in range(sess_mod.SESSION_CAP + 3):
        _prompt(f"sess-{i}", ago=2 + i)
    _status, body = _get(base, "/api/mindnet/cats", token)
    assert len(body["sessions"]) == sess_mod.SESSION_CAP
    assert body["sessions_total"] >= sess_mod.SESSION_CAP + 3
    assert body["sessions_hidden"] == body["sessions_total"] - sess_mod.SESSION_CAP
