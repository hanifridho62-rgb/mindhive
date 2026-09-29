# tests/test_server_app.py
"""`serve_ui` — and the invariant that it must never cost the user their panel.

The user's real dashboard runs at `http://127.0.0.1:54461` and is the thing
this repo's CLAUDE.md spends its first section protecting. `hive ui` defaults
to that same port ON PURPOSE -- one panel, one URL, one bookmark -- which
makes exactly one question load-bearing: what happens when the 24/7 dashboard
is already there? The answer has to be "open its URL and return", never "bind
over it" and never "shut it down after a minute".

So the tests below are in two halves. The first is ordinary wiring: the
monitor gets the numbers, `serve` gets the monitor. The second is that
invariant, asserted from both sides -- the busy-port path never constructs a
server at all, and a plain `hive dashboard` never starts a watchdog thread
even though the code that could start one now lives in the same function.

The end-to-end case runs a REAL server on an ephemeral port with a sub-second
cutoff and asserts it stops by itself. It is the only test here that proves
the feature works rather than that it is wired; everything else could pass
with a watchdog that never ticks.
"""
import threading

import pytest

from mindhive import dashboard
from mindhive.server import app
from mindhive.server.lifecycle import HeartbeatMonitor


@pytest.fixture(autouse=True)
def _no_harvest(monkeypatch):
    """The auto-harvest loop reads the machine's Claude transcripts. Nothing
    here wants that, and a test that walked the developer's real
    `~/.claude/projects` would be slow and non-deterministic."""
    monkeypatch.setenv("MINDHIVE_AUTOHARVEST", "0")


@pytest.fixture
def served(monkeypatch):
    """Capture what `serve_ui` hands `dashboard.serve`, without serving."""
    calls = []
    monkeypatch.setattr(dashboard, "serve",
                        lambda *a, **k: calls.append((a, k)))
    return calls


# --------------------------------------------------------------------------
# the wiring
# --------------------------------------------------------------------------

def test_it_serves_with_a_monitor(served):
    app.serve_ui(port=0, open_browser=False)
    (_args, kw), = served
    assert isinstance(kw["lifecycle"], HeartbeatMonitor)


def test_the_monitor_carries_the_numbers_it_was_given(served):
    app.serve_ui(port=0, open_browser=False, idle_cutoff_s=90.0,
                 interval_s=20.0)
    (_args, kw), = served
    assert kw["lifecycle"].idle_cutoff_s == 90.0
    assert kw["lifecycle"].interval_s == 20.0


def test_the_defaults_are_the_blueprints_numbers(served):
    app.serve_ui(port=0, open_browser=False)
    (_args, kw), = served
    assert kw["lifecycle"].idle_cutoff_s == 60.0
    assert kw["lifecycle"].interval_s == 15.0


def test_a_short_cutoff_brings_the_interval_down_with_it(served):
    """`--idle-cutoff 5` against the default 15 s beat is a pair the monitor
    refuses. Deriving the interval is what makes a short cutoff a short run
    instead of a usage error — and `tests/test_server_lifecycle.py` pins that
    the derivation leaves the default pair untouched."""
    app.serve_ui(port=0, open_browser=False, idle_cutoff_s=5.0)
    (_args, kw), = served
    assert kw["lifecycle"].idle_cutoff_s == 5.0
    assert kw["lifecycle"].interval_s == 1.25


def test_an_explicit_interval_still_wins_over_the_derivation(served):
    app.serve_ui(port=0, open_browser=False, idle_cutoff_s=40.0,
                 interval_s=2.0)
    (_args, kw), = served
    assert kw["lifecycle"].interval_s == 2.0


def test_the_default_port_is_the_dashboards_own(served):
    """One panel, one URL. `hive ui` and `hive dashboard` are two ways into
    the same page, so a second port would mean a second bookmark and a user
    wondering which of their two dashboards is the real one."""
    app.serve_ui(open_browser=False)
    (_args, kw), = served
    assert kw["port"] == dashboard.DEFAULT_PORT


def test_the_browser_flag_is_passed_through(served):
    app.serve_ui(port=0, open_browser=False)
    app.serve_ui(port=0, open_browser=True)
    assert [kw["open_browser"] for _a, kw in served] == [False, True]


def test_a_token_is_passed_through(served):
    app.serve_ui(port=0, open_browser=False, token="fixed-token")
    (_args, kw), = served
    assert kw["token"] == "fixed-token"


def test_an_unusable_pair_of_numbers_is_refused_before_anything_binds(served):
    """`HeartbeatMonitor` refuses an interval that cannot beat the cutoff.
    Building it BEFORE calling `serve` is what turns that into a usage error
    instead of a server that shuts itself down while the user watches."""
    with pytest.raises(ValueError):
        app.serve_ui(port=0, open_browser=False, idle_cutoff_s=10.0,
                     interval_s=30.0)
    assert served == []


# --------------------------------------------------------------------------
# the invariant: the live dashboard is never disturbed
# --------------------------------------------------------------------------

def test_a_busy_port_never_reaches_the_constructor(monkeypatch):
    """`serve`'s single-instance guard answers BEFORE the bind. If it did not,
    `hive ui` against a running dashboard would race for port 54461 -- and on
    Windows `SO_REUSEADDR` would let the second bind succeed, leaving two
    servers and a user whose panel silently changed identity."""
    monkeypatch.setattr(dashboard, "_instance_running", lambda port: True)

    def _boom(*a, **k):                                  # pragma: no cover
        raise AssertionError("a server was constructed against a busy port")

    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", _boom)
    dashboard.serve(port=54461, open_browser=False, token="t",
                    lifecycle=HeartbeatMonitor())


def test_a_busy_port_starts_no_watchdog(monkeypatch):
    """The other half: returning early must also mean no thread is left
    ticking against a server this process does not own."""
    monkeypatch.setattr(dashboard, "_instance_running", lambda port: True)
    before = _watchdogs()
    dashboard.serve(port=54461, open_browser=False, token="t",
                    lifecycle=HeartbeatMonitor())
    assert _watchdogs() == before


def _watchdogs():
    return [t.name for t in threading.enumerate()
            if t.name == "hive-ui-watchdog"]


class _HeldServer:
    """A `ThreadingHTTPServer` stand-in that serves until this test lets go.

    Callable, so it can stand in where the class is looked up; the instance
    it returns is itself, which is enough for one server."""

    def __init__(self):
        self.server_address = ("127.0.0.1", 0)
        self.serving = threading.Event()
        self._released = threading.Event()

    def __call__(self, address, handler):
        return self

    def serve_forever(self):
        self.serving.set()
        self._released.wait(30.0)

    def shutdown(self):
        self._released.set()


def test_a_plain_dashboard_starts_no_watchdog_thread(monkeypatch):
    """`hive dashboard` must not acquire a way to shut itself down just
    because `serve` now knows how. No lifecycle, no thread, no fragment.

    The server is HELD rather than left running, and that is not squeamishness
    about an idle socket. The first version of this test started a real
    `serve` in a daemon thread and never stopped it -- and `serve` starts an
    auto-harvest loop that reads `MINDHIVE_AUTOHARVEST` when the THREAD runs,
    which is after this test's monkeypatch of that variable has been undone.
    The loop then harvested into whatever `MINDHIVE_HOME` later tests were
    using, and storage tests hundreds of files away failed on chain heads and
    event counts a background writer had moved, in a different place each
    run. A leaked server is a leaked WRITER.

    Holding it also makes the assertion stronger: `serve` is blocked INSIDE
    `serve_forever`, so everything it starts has started, and a watchdog it
    started late cannot slip past the check."""
    held = _HeldServer()
    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", held)
    before = _watchdogs()
    done = threading.Event()

    def _run():
        try:
            dashboard.serve(port=0, open_browser=False, token="t")
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    assert held.serving.wait(20.0), "serve never reached serve_forever"
    assert _watchdogs() == before
    held.shutdown()
    assert done.wait(20.0), "serve did not return once the server stopped"


# --------------------------------------------------------------------------
# it really stops by itself
# --------------------------------------------------------------------------

def test_a_server_nobody_beats_at_shuts_itself_down():
    """THE end-to-end case. A real `ThreadingHTTPServer` on an ephemeral port,
    a 0.3 s cutoff, and nothing beating: `serve` must RETURN on its own.

    Everything else in this file would pass with a watchdog that never ticks.
    The generous 15 s bound is for a loaded CI box, not for the mechanism --
    the real answer arrives in well under a second."""
    done = threading.Event()

    def _run():
        try:
            dashboard.serve(port=0, open_browser=False, token="t",
                            lifecycle=HeartbeatMonitor(idle_cutoff_s=0.3,
                                                       interval_s=0.1))
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    assert done.wait(15.0), "the ephemeral server was still serving"


def test_a_server_that_keeps_being_beaten_at_keeps_serving():
    """The complement, and the one that would catch a watchdog that ignores
    the monitor and just shuts down on a timer."""
    monitor = HeartbeatMonitor(idle_cutoff_s=0.4, interval_s=0.1)
    done = threading.Event()
    stop_beating = threading.Event()

    def _beat():
        while not stop_beating.wait(0.05):
            monitor.record_ping()

    def _run():
        try:
            dashboard.serve(port=0, open_browser=False, token="t",
                            lifecycle=monitor)
        finally:
            done.set()

    threading.Thread(target=_beat, daemon=True).start()
    threading.Thread(target=_run, daemon=True).start()
    try:
        assert not done.wait(1.5), "a beaten-at server shut itself down anyway"
    finally:
        stop_beating.set()
    assert done.wait(15.0), "it never stopped once the beating did"
