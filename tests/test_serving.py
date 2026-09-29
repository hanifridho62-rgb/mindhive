# tests/test_serving.py
"""The bounded server stop in `tests/serving.py`.

The contract has two halves, and each needs the other to mean anything: a
live server stops through the helper without the guard ever firing, and a
server that CANNOT be stopped -- one whose `serve_forever` never ran, the
exact shape that parked a full suite run in `shutdown()` -- makes the guard
fire within its bound instead of parking the process. The second half opens
by proving the premise on the stdlib itself, so a future CPython that makes
`shutdown()` safe turns that canary red rather than leaving this file
asserting a fear that no longer exists."""
import contextlib
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.serving import (ServerStopTimeout, serve_in_thread, start_serving,
                           stop_serving)


class _Hello(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):          # keep the run's output clean
        pass


class _Wedged(ThreadingHTTPServer):
    """A server whose loop never reaches the stdlib's: `serve_forever` parks
    until `released` is set, so `shutdown()` waits on an Event nothing sets.
    Released, it runs the real loop once, which sees the pending stop request
    and sets that Event on the way out -- so nothing this double wedged stays
    wedged past the test."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.released = threading.Event()

    def serve_forever(self, poll_interval=0.5):
        self.released.wait()
        # The guard closes the listener when it fires, and the real loop then
        # refuses to register a closed socket -- after its `finally` has set
        # the Event, which is the part that matters here.
        with contextlib.suppress(ValueError):
            super().serve_forever(poll_interval)


def _server(cls=ThreadingHTTPServer):
    return cls(("127.0.0.1", 0), _Hello)


def _url(srv):
    return f"http://127.0.0.1:{srv.server_address[1]}/"


def _stoppers_still_parked():
    return [t.name for t in threading.enumerate()
            if t.name.startswith("stop ") and t.is_alive()]


def _settle(predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --- the ordinary path -------------------------------------------------------

def test_a_live_server_serves_inside_the_block_and_is_released_after_it():
    srv = _server()
    with serve_in_thread(srv):
        with urllib.request.urlopen(_url(srv), timeout=5) as r:
            assert r.read() == b"hello"
    # Stopped AND closed: the listener is released, not merely no longer polled.
    assert srv.socket.fileno() == -1
    assert _stoppers_still_parked() == []


def test_a_stop_costs_one_poll_interval_not_the_stdlib_half_second():
    srv = _server()
    thread = start_serving(srv, poll_interval_s=0.02)
    t0 = time.monotonic()
    stop_serving(srv, thread)
    assert time.monotonic() - t0 < 1.0
    assert not thread.is_alive()


# --- the guard ---------------------------------------------------------------

def test_the_stdlib_shutdown_really_does_park_when_serve_forever_never_ran():
    """The premise, on the stdlib itself. Without it the guard tests below
    could pass against a `shutdown()` that had quietly become safe, and the
    helper would be complexity guarding nothing."""
    srv = _server()
    bare = threading.Thread(target=srv.shutdown, daemon=True)
    bare.start()
    bare.join(0.5)
    assert bare.is_alive(), \
        "BaseServer.shutdown() returned with serve_forever never having run"
    # Let it out: a serve_forever entered now sees the pending request, exits
    # at once, and sets the Event the bare shutdown() is waiting on.
    srv.serve_forever(poll_interval=0.01)
    bare.join(5)
    assert not bare.is_alive()
    srv.server_close()


def test_the_guard_fires_within_its_bound_when_serve_forever_never_ran():
    """The hang, made an error. On a server whose thread never ran
    `serve_forever`, `stop_serving` raises, names the cause, and comes back
    inside its bound instead of parking the process."""
    srv = _server()
    t0 = time.monotonic()
    with pytest.raises(ServerStopTimeout) as info:
        stop_serving(srv, None, timeout_s=0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"the guard took {elapsed:.1f}s against a 0.2s bound"
    msg = str(info.value)
    assert "did not return within 0.2s" in msg
    assert "none was handed" in msg
    assert srv.socket.fileno() == -1, "the listener was left open"
    # Release the stopper the guard left parked in the stdlib's shutdown():
    # the loop sees the pending request and sets the Event; the closed
    # listener makes it raise on the way, after that `finally`.
    with contextlib.suppress(ValueError):
        srv.serve_forever(poll_interval=0.01)
    assert _settle(lambda: not _stoppers_still_parked())


def test_the_guard_names_a_wedged_serving_thread_and_where_it_is_parked():
    """When the serving thread exists, the report carries the two facts the
    hang investigation had to get from py-spy: whether it is alive, and the
    frame it is sitting in."""
    srv = _server(_Wedged)
    thread = start_serving(srv)
    try:
        with pytest.raises(ServerStopTimeout) as info:
            stop_serving(srv, thread, timeout_s=0.2)
    finally:
        srv.released.set()
        thread.join(5)
    msg = str(info.value)
    assert f"serving thread: {thread.name!r} alive=True" in msg
    assert "self.released.wait()" in msg, msg
    assert not thread.is_alive()
    assert _settle(lambda: not _stoppers_still_parked())


def test_serve_in_thread_raises_on_exit_rather_than_parking():
    """The fixture form inherits the bound: a wedged server turns the block's
    exit into an exception the test run can report and move past."""
    srv = _server(_Wedged)
    try:
        with pytest.raises(ServerStopTimeout):
            with serve_in_thread(srv, stop_timeout_s=0.2):
                pass
    finally:
        srv.released.set()
    assert _settle(lambda: not any("serve_forever" in t.name and t.is_alive()
                                   for t in threading.enumerate()))
    assert _settle(lambda: not _stoppers_still_parked())
