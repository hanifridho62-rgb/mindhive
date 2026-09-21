# tests/test_server_routes.py
"""The two lifecycle routes, and the fact that they exist only on `hive ui`.

`with_lifecycle(base, monitor)` mints a SUBCLASS of the handler
`dashboard.make_handler` already produced, with its own merged route tables.
That shape is the whole design, and it is chosen against two alternatives:

* **Not** adding the routes to `dashboard_get.py` / `dashboard_post.py`. Those
  modules are the 24/7 panel's surface; a `/api/heartbeat` there would be a
  route every dashboard answers, on a server that has no monitor to record it
  against. `dashboard_get.py` also stands at the 1000-line cap.
* **Not** re-implementing `do_GET` / `do_POST`. They look the route up through
  `self._GET_ROUTES` / `self._POST_ROUTES`, so a subclass with merged tables
  inherits the token guard, the host guard, gzip, keep-alive and the HIVE MIND
  rules for free -- and CANNOT drift from them, because there is only one copy.

So the tests below are mostly about the seam rather than the handlers: that
the base classes are untouched, that a plain `hive dashboard` page carries no
heartbeat at all, and that the guards really do still apply to the new routes.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, dashboard_get, dashboard_post
from mindhive.dashboard_page import render_page
from mindhive.server.lifecycle import HeartbeatMonitor
from mindhive.server.routes import LifecycleRoutes, with_lifecycle

TOKEN = "test-token-lifecycle"


@pytest.fixture
def ui():
    """A `hive ui` server on an ephemeral port, plus its monitor."""
    monitor = HeartbeatMonitor()
    handler = with_lifecycle(dashboard.make_handler(TOKEN), monitor)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", monitor, handler
    finally:
        httpd.shutdown()


def _get(base, path, token=None, timeout=10.0):
    url = f"{base}{path}"
    if token:
        url += ("&" if "?" in path else "?") + f"token={token}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _post(base, path, body=None, token=None, timeout=10.0):
    data = json.dumps(body if body is not None else {}).encode("utf-8")
    req = urllib.request.Request(f"{base}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Hive-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


# --------------------------------------------------------------------------
# POST /api/heartbeat
# --------------------------------------------------------------------------

def test_a_beat_with_the_token_is_recorded(ui):
    base, monitor, _ = ui
    status, body = _post(base, "/api/heartbeat", token=TOKEN)
    assert status == 200
    got = json.loads(body)
    assert got["ok"] is True
    assert got["lifecycle"]["pings"] == 1
    assert monitor.snapshot()["pings"] == 1


def test_a_beat_without_the_token_is_refused_and_records_nothing(ui):
    """The heartbeat is the one route whose whole job is to keep a process
    alive. An unauthenticated caller who could reach it could keep any user's
    UI running forever from outside the page."""
    base, monitor, _ = ui
    status, _body = _post(base, "/api/heartbeat")
    assert status == 403
    assert monitor.snapshot()["pings"] == 0


def test_the_beat_resets_the_idle_clock(ui):
    base, monitor, _ = ui
    monitor.record_ping(now=monitor.started_at - 500)      # long idle
    assert monitor.should_shutdown() is True
    _post(base, "/api/heartbeat", token=TOKEN)
    assert monitor.should_shutdown() is False


def test_a_beat_under_hive_mind_is_answered_rather_than_refused(ui):
    """`do_POST` refuses mutating actions when the project is `*` unless the
    route is on its exception tuple. A beat names no namespace, so refusing it
    would shut the UI down under a user reading HIVE MIND."""
    base, monitor, _ = ui
    status, body = _post(base, "/api/heartbeat", body={"project": "*"},
                         token=TOKEN)
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert monitor.snapshot()["pings"] == 1


def test_the_response_carries_the_snapshot_so_the_page_can_show_it(ui):
    base, _monitor, _ = ui
    _status, body = _post(base, "/api/heartbeat", token=TOKEN)
    snap = json.loads(body)["lifecycle"]
    assert set(snap) == {"idle_for", "idle_cutoff_s", "interval_s", "pings",
                         "started_at"}


# --------------------------------------------------------------------------
# GET /api/lifecycle
# --------------------------------------------------------------------------

def test_the_lifecycle_route_reports_without_recording_a_beat(ui):
    """A monitoring read must not itself be evidence of life, or `curl` in a
    loop would keep the server up forever."""
    base, monitor, _ = ui
    status, body = _get(base, "/api/lifecycle", token=TOKEN)
    assert status == 200
    assert json.loads(body)["lifecycle"]["pings"] == 0
    assert monitor.snapshot()["pings"] == 0


def test_the_lifecycle_route_needs_the_token(ui):
    base, _monitor, _ = ui
    status, _body = _get(base, "/api/lifecycle")
    assert status == 403


def test_the_lifecycle_route_reports_the_configured_numbers(ui):
    base, _monitor, _ = ui
    _status, body = _get(base, "/api/lifecycle", token=TOKEN)
    snap = json.loads(body)["lifecycle"]
    assert snap["idle_cutoff_s"] == 60.0
    assert snap["interval_s"] == 15.0


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

def test_the_served_page_carries_the_heartbeat(ui):
    base, _monitor, _ = ui
    status, page = _get(base, "/")
    assert status == 200
    assert "/api/heartbeat" in page
    assert "HEARTBEAT_MS = 15000;" in page


def test_the_heartbeat_in_the_served_page_has_no_visibility_guard(ui):
    """Asserted on the FRAGMENT as the page carries it, not on the page.

    The page legitimately contains `document.hidden` -- the live poller pauses
    in a hidden tab, which is right for a poller and wrong for a heartbeat.
    The first draft of this test asserted the substring was absent from the
    whole page and failed against correct code, which is worse than no test:
    it would have been "fixed" by weakening it."""
    base, _monitor, handler = ui
    _status, page = _get(base, "/")
    assert handler._extra_js in page
    assert "document.hidden" not in handler._extra_js


def test_the_heartbeat_lands_after_the_pages_own_helpers(ui):
    """It calls `u()` and reads `TOKEN`, both defined by the core fragment. A
    splice point ABOVE them would produce a ReferenceError on load -- which
    the browser reports in a console nobody is watching."""
    base, _monitor, _ = ui
    _status, page = _get(base, "/")
    assert page.index("function u(") < page.index("u('/api/heartbeat')")
    assert page.index("const TOKEN =") < page.index("u('/api/heartbeat')")


def test_the_interval_the_page_beats_at_matches_the_monitors():
    monitor = HeartbeatMonitor(idle_cutoff_s=20.0, interval_s=5.0)
    handler = with_lifecycle(dashboard.make_handler(TOKEN), monitor)
    assert "HEARTBEAT_MS = 5000;" in handler._extra_js


def test_the_page_interval_can_be_overridden_without_moving_the_cutoff():
    monitor = HeartbeatMonitor()
    handler = with_lifecycle(dashboard.make_handler(TOKEN), monitor,
                             heartbeat_ms=2000)
    assert "HEARTBEAT_MS = 2000;" in handler._extra_js
    assert monitor.idle_cutoff_s == 60.0


# --------------------------------------------------------------------------
# `hive dashboard` is untouched — the property this whole shape exists for
# --------------------------------------------------------------------------

def test_the_plain_page_carries_no_heartbeat_at_all():
    page = render_page("t")
    assert "/api/heartbeat" not in page
    assert "HEARTBEAT_MS" not in page


def test_the_base_route_tables_never_learn_the_lifecycle_routes():
    assert "/api/lifecycle" not in dashboard_get.GetRoutes._GET_ROUTES
    assert "/api/heartbeat" not in dashboard_post.PostRoutes._POST_ROUTES


def test_a_plain_dashboard_handler_answers_neither_route():
    plain = dashboard.make_handler(TOKEN)
    assert "/api/lifecycle" not in plain._GET_ROUTES
    assert "/api/heartbeat" not in plain._POST_ROUTES


def test_wrapping_does_not_mutate_the_handler_it_wrapped():
    """`{**base._GET_ROUTES, ...}` builds a NEW dict. Mutating the base's
    would put the route on every handler minted before or after."""
    plain = dashboard.make_handler(TOKEN)
    before_get = dict(plain._GET_ROUTES)
    before_post = dict(plain._POST_ROUTES)
    with_lifecycle(plain, HeartbeatMonitor())
    assert plain._GET_ROUTES == before_get
    assert plain._POST_ROUTES == before_post


def test_two_uis_do_not_share_a_monitor():
    """Each wrap is its own class with its own `_monitor`. A module-level
    monitor would make two `hive ui` processes in one interpreter -- which is
    what the test suite is -- keep each other alive."""
    a, b = HeartbeatMonitor(), HeartbeatMonitor()
    ha = with_lifecycle(dashboard.make_handler(TOKEN), a)
    hb = with_lifecycle(dashboard.make_handler(TOKEN), b)
    assert ha._monitor is a and hb._monitor is b


def test_the_wrapper_keeps_every_route_the_base_had(ui):
    _base, _monitor, handler = ui
    plain = dashboard.make_handler(TOKEN)
    assert set(plain._GET_ROUTES) < set(handler._GET_ROUTES)
    assert set(handler._GET_ROUTES) - set(plain._GET_ROUTES) == {"/api/lifecycle"}
    assert set(handler._POST_ROUTES) - set(plain._POST_ROUTES) == {"/api/heartbeat"}


def test_an_ordinary_route_still_answers_through_the_wrapper(ui):
    """The point of subclassing rather than reimplementing: everything that
    worked before still works, and not because a test lists it."""
    base, _monitor, _ = ui
    status, body = _get(base, "/api/projects", token=TOKEN)
    assert status == 200
    got = json.loads(body)
    assert set(got) == {"projects", "hivemind", "default", "current"}


def test_the_mixin_carries_no_monitor_of_its_own():
    """`LifecycleRoutes._monitor` is None: the mixin is route bodies, and the
    monitor arrives on the subclass `with_lifecycle` mints. A default monitor
    here would be a process-wide singleton nobody asked for."""
    assert LifecycleRoutes._monitor is None
