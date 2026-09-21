# tests/test_server_lifecycle.py
"""The ephemeral UI's clock: when has nobody been watching for long enough.

`hive dashboard` is a server you start and stop. `hive ui` is a PAGE — you open
it, you use it, you close the tab, and the process that was serving it should
not still be holding a port an hour later. The whole mechanism is one number
the page keeps refreshing and one thread that notices when it stops.

**Every seam is injected, because time is the thing under test.** A monitor
built on the real `time.monotonic` can only be tested by sleeping, and a test
that sleeps for a 60-second cutoff is a test nobody runs. `clock`, `sleep` and
the server are all parameters, so the whole state machine is exercised in
microseconds and the assertions are exact rather than "roughly a minute".

**The grace period is deliberate and is pinned twice.** A monitor starts with
`last_ping` set to NOW, not to zero: the page needs time to load, run its
script and send the first beat, and a monitor that considered itself idle from
construction would shut the server down before the browser had finished
parsing the HTML that would have kept it alive.
"""
import threading

import pytest

from mindhive.server import lifecycle


class FakeClock:
    """A monotonic clock a test drives by hand."""

    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += float(dt)
        return self.t


class FakeServer:
    def __init__(self):
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1


def _monitor(clock=None, **kw):
    clock = clock or FakeClock()
    kw.setdefault("idle_cutoff_s", 60.0)
    kw.setdefault("interval_s", 15.0)
    return lifecycle.HeartbeatMonitor(clock=clock, **kw), clock


# --------------------------------------------------------------------------
# the constants
# --------------------------------------------------------------------------

def test_the_two_constants_are_the_blueprints_numbers():
    assert lifecycle.HEARTBEAT_INTERVAL_S == 15.0
    assert lifecycle.IDLE_CUTOFF_S == 60.0


def test_the_cutoff_leaves_room_for_three_missed_beats():
    """Not an arbitrary pair. One dropped beat -- a GC pause, a laptop lid, a
    slow reload -- must never cost the user their page, so the cutoff is four
    intervals and tolerates three consecutive misses."""
    assert lifecycle.IDLE_CUTOFF_S / lifecycle.HEARTBEAT_INTERVAL_S == 4.0


# --------------------------------------------------------------------------
# HeartbeatMonitor
# --------------------------------------------------------------------------

def test_a_fresh_monitor_is_not_idle():
    mon, _ = _monitor()
    assert mon.idle_for() == 0.0
    assert mon.should_shutdown() is False


def test_the_grace_period_is_measured_from_construction_not_from_zero():
    """The failure this prevents: a monitor whose `last_ping` starts at 0 is
    already idle by 1000 seconds the moment it is built, so the watchdog's
    first tick kills the server before the page has sent a single beat."""
    clock = FakeClock(10_000.0)
    mon = lifecycle.HeartbeatMonitor(clock=clock)
    assert mon.idle_for() == 0.0


def test_idle_grows_with_the_clock():
    mon, clock = _monitor()
    clock.advance(20)
    assert mon.idle_for() == 20.0


def test_a_ping_resets_the_clock():
    mon, clock = _monitor()
    clock.advance(45)
    mon.record_ping()
    assert mon.idle_for() == 0.0
    clock.advance(10)
    assert mon.idle_for() == 10.0


def test_pings_are_counted():
    mon, _ = _monitor()
    assert mon.snapshot()["pings"] == 0
    mon.record_ping()
    mon.record_ping()
    assert mon.snapshot()["pings"] == 2


def test_exactly_at_the_cutoff_is_not_yet_idle():
    """Strictly greater, and it is pinned because the boundary is where a
    `>=` would shut down a page whose beat is landing this very tick."""
    mon, clock = _monitor()
    clock.advance(60.0)
    assert mon.idle_for() == 60.0
    assert mon.should_shutdown() is False


def test_one_tick_past_the_cutoff_is_idle():
    mon, clock = _monitor()
    clock.advance(60.01)
    assert mon.should_shutdown() is True


def test_a_caller_may_pass_the_time_rather_than_let_the_monitor_read_it():
    """The watchdog reads the clock once per tick and hands the number to
    every question it asks, so a monitor cannot answer two questions about one
    tick from two different instants."""
    mon, clock = _monitor()
    assert mon.should_shutdown(clock.t + 500) is True
    assert mon.idle_for(clock.t + 30) == 30.0


def test_the_snapshot_is_the_documented_shape():
    mon, clock = _monitor()
    clock.advance(5)
    mon.record_ping()
    clock.advance(2)
    snap = mon.snapshot()
    assert set(snap) == {"idle_for", "idle_cutoff_s", "interval_s", "pings",
                         "started_at"}
    assert snap["idle_for"] == 2.0
    assert snap["idle_cutoff_s"] == 60.0
    assert snap["interval_s"] == 15.0
    assert snap["pings"] == 1


def test_the_snapshot_is_json_safe():
    """It rides in an HTTP response, so every value has to survive
    `json.dumps` with no `default=`."""
    import json
    mon, _ = _monitor()
    assert json.loads(json.dumps(mon.snapshot()))["pings"] == 0


# --------------------------------------------------------------------------
# refusing a configuration that cannot work
# --------------------------------------------------------------------------

@pytest.mark.parametrize("interval,cutoff", [
    (60.0, 60.0),    # equal: every beat lands exactly at the deadline
    (90.0, 60.0),    # slower than the deadline: it can never arrive in time
    (0.0, 60.0),     # no timer at all
    (-1.0, 60.0),
])
def test_an_interval_that_cannot_beat_the_cutoff_is_refused(interval, cutoff):
    """A page that beats every 90 s under a 60 s cutoff is a page that shuts
    its own server down while the user is reading it. That is a configuration
    error, and it is refused at construction rather than discovered as a
    mysterious disconnect."""
    with pytest.raises(ValueError):
        lifecycle.HeartbeatMonitor(idle_cutoff_s=cutoff, interval_s=interval)


@pytest.mark.parametrize("cutoff", [0.0, -5.0])
def test_a_cutoff_that_is_not_positive_is_refused(cutoff):
    with pytest.raises(ValueError):
        lifecycle.HeartbeatMonitor(idle_cutoff_s=cutoff, interval_s=1.0)


def test_the_refusal_names_both_numbers():
    with pytest.raises(ValueError) as e:
        lifecycle.HeartbeatMonitor(idle_cutoff_s=60.0, interval_s=90.0)
    assert "90" in str(e.value) and "60" in str(e.value)


# --------------------------------------------------------------------------
# deriving one number from the other
# --------------------------------------------------------------------------

def test_the_derivation_reproduces_the_blueprints_own_pair():
    """The reason it is a cap AND a ratio rather than either alone: at the
    default cutoff the two agree, so this function existing changes no
    default anywhere."""
    assert (lifecycle.default_interval_for(lifecycle.IDLE_CUTOFF_S)
            == lifecycle.HEARTBEAT_INTERVAL_S)


@pytest.mark.parametrize("cutoff,expected", [
    (5.0, 1.25),            # `hive ui --idle-cutoff 5` — the first thing tried
    (20.0, 5.0),
    (60.0, 15.0),
    (3600.0, 15.0),         # capped: a long cutoff buys no lazy heartbeat
])
def test_a_shorter_cutoff_shortens_the_interval_with_it(cutoff, expected):
    assert lifecycle.default_interval_for(cutoff) == expected


@pytest.mark.parametrize("cutoff", [0.1, 1.0, 5.0, 59.0, 60.0, 600.0])
def test_every_derived_pair_is_one_the_monitor_accepts(cutoff):
    """The whole point. A derivation that produced a refused pair would turn
    a usage error into a usage error one frame further down."""
    monitor = lifecycle.HeartbeatMonitor(
        idle_cutoff_s=cutoff, interval_s=lifecycle.default_interval_for(cutoff))
    assert 0 < monitor.interval_s < monitor.idle_cutoff_s


def test_a_cutoff_of_zero_derives_a_pair_that_is_still_refused():
    """Zero is not rescued, and must not be: `setInterval(f, 0)` is a tight
    loop and a zero cutoff is a server that stops before it starts."""
    with pytest.raises(ValueError):
        lifecycle.HeartbeatMonitor(
            idle_cutoff_s=0.0, interval_s=lifecycle.default_interval_for(0.0))


# --------------------------------------------------------------------------
# the watchdog
# --------------------------------------------------------------------------

def test_the_watchdog_does_not_shut_down_while_the_page_is_alive():
    mon, clock = _monitor()
    server, stop = FakeServer(), threading.Event()
    waits = []

    def _wait(timeout):
        waits.append(timeout)
        clock.advance(15)
        mon.record_ping()          # the page answered this tick
        return len(waits) >= 4     # then the caller stops us

    stop.wait = _wait
    lifecycle.watchdog(mon, server, stop, clock=clock)
    assert server.shutdowns == 0
    assert waits == [15.0, 15.0, 15.0, 15.0]


def test_the_watchdog_shuts_down_once_the_page_stops_answering():
    mon, clock = _monitor()
    server, stop = FakeServer(), threading.Event()

    def _wait(timeout):
        clock.advance(30)
        return False

    stop.wait = _wait
    lifecycle.watchdog(mon, server, stop, clock=clock)
    assert server.shutdowns == 1


def test_the_watchdog_returns_after_shutting_down_and_does_not_shut_down_twice():
    """`server.shutdown()` on an already-stopped server is not harmless on
    every platform, and a watchdog that kept ticking would call it on every
    tick forever."""
    mon, clock = _monitor()
    server, stop = FakeServer(), threading.Event()
    ticks = []

    def _wait(timeout):
        ticks.append(timeout)
        clock.advance(30)
        return False

    stop.wait = _wait
    lifecycle.watchdog(mon, server, stop, clock=clock)
    assert server.shutdowns == 1
    assert len(ticks) == 3            # 30, 60, 90 -> shuts down on the third


def test_a_set_stop_event_ends_the_watchdog_without_touching_the_server():
    """The ordinary exit: the user pressed Ctrl+C, `serve` set the event, and
    the watchdog must not then also call `shutdown` on a server already
    shutting itself down."""
    mon, clock = _monitor()
    server, stop = FakeServer(), threading.Event()
    stop.set()
    clock.advance(10_000)             # idle by any measure
    lifecycle.watchdog(mon, server, stop, clock=clock)
    assert server.shutdowns == 0


def test_the_watchdog_ticks_at_the_monitors_interval_by_default():
    mon, clock = _monitor(interval_s=5.0, idle_cutoff_s=40.0)
    stop = threading.Event()
    waits = []

    def _wait(timeout):
        waits.append(timeout)
        return True

    stop.wait = _wait
    lifecycle.watchdog(mon, FakeServer(), stop, clock=clock)
    assert waits == [5.0]


def test_the_tick_interval_can_be_overridden():
    mon, clock = _monitor()
    stop = threading.Event()
    waits = []

    def _wait(timeout):
        waits.append(timeout)
        return True

    stop.wait = _wait
    lifecycle.watchdog(mon, FakeServer(), stop, interval_s=0.25, clock=clock)
    assert waits == [0.25]


def test_a_custom_shutdown_is_called_instead_of_the_servers():
    """The ASGI path has no `serve_forever` to stop; uvicorn wants
    `should_exit = True`. The watchdog takes the callable rather than growing
    a branch for each server it might be handed."""
    mon, clock = _monitor()
    server, stop = FakeServer(), threading.Event()
    called = []

    def _wait(timeout):
        clock.advance(90)
        return False

    stop.wait = _wait
    lifecycle.watchdog(mon, server, stop, clock=clock,
                       shutdown=lambda: called.append(1))
    assert called == [1]
    assert server.shutdowns == 0


def test_the_watchdog_survives_a_shutdown_that_raises():
    """A watchdog that propagates is a dead daemon thread and a traceback in
    the operator's console at exactly the moment the process is going away
    anyway."""
    mon, clock = _monitor()
    stop = threading.Event()

    def _wait(timeout):
        clock.advance(90)
        return False

    stop.wait = _wait

    def _boom():
        raise RuntimeError("socket already closed")

    lifecycle.watchdog(mon, FakeServer(), stop, clock=clock, shutdown=_boom)


# --------------------------------------------------------------------------
# it really is thread-safe, because the real callers are threads
# --------------------------------------------------------------------------

def test_pings_from_many_threads_are_all_counted():
    """`record_ping` runs on request threads (`ThreadingHTTPServer` hands each
    request its own) while the watchdog thread reads. Counting without a lock
    loses increments under exactly that shape."""
    mon = lifecycle.HeartbeatMonitor()
    start = threading.Barrier(9)

    def _beat():
        start.wait()
        for _ in range(50):
            mon.record_ping()

    threads = [threading.Thread(target=_beat) for _ in range(8)]
    for t in threads:
        t.start()
    start.wait()
    for t in threads:
        t.join()
    assert mon.snapshot()["pings"] == 400


def test_the_module_imports_without_pulling_in_the_dashboard():
    """`server/lifecycle.py` is a clock and a counter. It must not drag an
    HTTP stack, a ledger or a browser opener behind it -- `server/app.py` is
    where those arrive."""
    import subprocess
    import sys
    code = ("import sys, mindhive.server.lifecycle;"
            "print(any(m.startswith('mindhive.dashboard') or m == 'http.server'"
            " for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
