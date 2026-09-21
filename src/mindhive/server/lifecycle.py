# SPDX-License-Identifier: Apache-2.0
"""When has nobody been watching for long enough (blueprint P1k, MEB §1.4).

`hive dashboard` is a server: you start it, it serves until you stop it, and
that is right for a panel meant to stay up. `hive ui` is a PAGE: you open it,
you use it, you close the tab -- and the process behind it should not still be
holding port 54461 an hour later, because the next thing the operator does is
run `hive ui` again and get a single-instance message about a server they
thought they had closed.

The mechanism is deliberately two moving parts and no more. The page sends a
beat every `interval_s`; a thread ticks at the same rate and stops the server
when the last beat is older than `idle_cutoff_s`. There is no session table,
no connection tracking and no "is the socket still open" heuristic -- a
keep-alive connection outlives the tab that opened it, which is exactly why
the socket cannot answer this question.

**Why four intervals and not two.** A missed beat is ordinary: a GC pause, a
backgrounded tab getting throttled, a laptop lid, a reload. At a 4:1 ratio
three consecutive misses are survivable and the page still dies within a
minute of being closed. At 2:1 a single hiccup costs the user their page,
which is the failure mode that makes people disable the feature.

**Every seam is injected.** `clock` here, and `clock`/`sleep`/`shutdown` in
`watchdog`, because the alternative is a test suite that sleeps for a minute
to check a minute-long cutoff -- which is a test nobody runs and therefore a
guarantee nobody has.
"""
import threading
import time

__all__ = ["BEATS_PER_CUTOFF", "HEARTBEAT_INTERVAL_S", "IDLE_CUTOFF_S",
           "HeartbeatMonitor", "default_interval_for", "watchdog"]

#: How often the page beats, and how often the watchdog looks.
HEARTBEAT_INTERVAL_S = 15.0

#: How long without a beat before the server stops. Four intervals: see the
#: module docstring for why three tolerated misses is the design and not an
#: accident of rounding.
IDLE_CUTOFF_S = 60.0

#: The ratio the two numbers above already are (60 / 15), named so that
#: deriving one from the other cannot drift from the pair it was taken from.
BEATS_PER_CUTOFF = 4


def default_interval_for(idle_cutoff_s: float) -> float:
    """The beat interval a cutoff of *idle_cutoff_s* implies.

    A monitor refuses an interval that cannot fit inside its cutoff, so a
    caller who shortens only the CUTOFF -- `hive ui --idle-cutoff 5`, which is
    how anyone tries this feature out for the first time -- would otherwise be
    told their reasonable request is a usage error. Deriving the interval
    keeps the pair consistent by construction.

    Capped at `HEARTBEAT_INTERVAL_S` rather than scaled freely, so a long
    cutoff does not buy a lazy heartbeat: an hour-long cutoff still beats
    every 15 s, and the page that just closed is still noticed inside a
    minute. With the blueprint's own numbers the cap and the ratio agree
    exactly, which is why the defaults are unchanged by this existing."""
    return min(HEARTBEAT_INTERVAL_S, float(idle_cutoff_s) / BEATS_PER_CUTOFF)


class HeartbeatMonitor:
    """The last beat, and whether it is old enough to stop serving.

    Thread-safe by construction: `record_ping` runs on `ThreadingHTTPServer`
    request threads -- one per request -- while the watchdog thread reads.
    Without the lock the counter loses increments under exactly that shape,
    and `last_ping` could be read halfway through a write on an interpreter
    without the GIL's incidental protection.

    A fresh monitor is NOT idle. `last_ping` starts at the construction
    instant rather than at zero, because the page needs time to load, parse
    and run its script before it can beat at all -- a monitor that counted
    idle time from the epoch would be an hour idle at birth and the watchdog's
    first tick would kill the server before the browser had finished reading
    the HTML that would have kept it alive."""

    def __init__(self, idle_cutoff_s: float = IDLE_CUTOFF_S,
                 interval_s: float = HEARTBEAT_INTERVAL_S,
                 clock=time.monotonic):
        idle_cutoff_s = float(idle_cutoff_s)
        interval_s = float(interval_s)
        if idle_cutoff_s <= 0:
            raise ValueError(
                f"idle_cutoff_s must be positive, got {idle_cutoff_s}")
        if not 0 < interval_s < idle_cutoff_s:
            # A page beating every 90 s under a 60 s cutoff shuts its own
            # server down while the user is reading it. Refused here, where it
            # is a configuration error with a message, rather than discovered
            # later as a page that mysteriously disconnects.
            raise ValueError(
                f"interval_s must be greater than 0 and less than "
                f"idle_cutoff_s, got interval_s={interval_s} with "
                f"idle_cutoff_s={idle_cutoff_s}")
        self.idle_cutoff_s = idle_cutoff_s
        self.interval_s = interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._pings = 0
        self.started_at = clock()
        self._last_ping = self.started_at

    # --- the page's end ---------------------------------------------------

    def record_ping(self, now=None) -> None:
        """A beat arrived. Called from a request thread."""
        stamp = self._clock() if now is None else float(now)
        with self._lock:
            self._last_ping = stamp
            self._pings += 1

    # --- the watchdog's end -----------------------------------------------

    def idle_for(self, now=None) -> float:
        """Seconds since the last beat, never negative.

        *now* is a parameter so the watchdog can read the clock ONCE per tick
        and ask every question about that same instant. A monitor that read
        the clock itself for each question could answer two questions about
        one tick from two different moments, which is how a boundary test
        passes in a suite and flaps in production."""
        stamp = self._clock() if now is None else float(now)
        with self._lock:
            last = self._last_ping
        return max(0.0, stamp - last)

    def should_shutdown(self, now=None) -> bool:
        """STRICTLY greater than the cutoff.

        `>=` would stop a page whose beat is landing on this very tick, and at
        a 4:1 ratio the tick and the beat share a period -- so the boundary is
        not a rare case, it is every fourth tick."""
        return self.idle_for(now) > self.idle_cutoff_s

    def snapshot(self, now=None) -> dict:
        """What `GET /api/lifecycle` returns. Plain floats and ints, so it
        survives `json.dumps` with no `default=`."""
        with self._lock:
            pings = self._pings
        return {"idle_for": self.idle_for(now),
                "idle_cutoff_s": self.idle_cutoff_s,
                "interval_s": self.interval_s,
                "pings": pings,
                "started_at": self.started_at}


def watchdog(monitor: HeartbeatMonitor, server, stop_event, *,
             interval_s=None, clock=time.monotonic, shutdown=None) -> None:
    """Tick until the page stops answering, then stop the server. Then RETURN.

    Returning matters: `server.shutdown()` on an already-stopped server is not
    harmless on every platform, and a loop that kept ticking would call it
    once per interval forever.

    `stop_event.wait(interval)` is both the sleep and the exit: `serve`'s
    `finally` sets the event, so an ordinary Ctrl+C ends this thread at once
    instead of after up to one interval, and -- because `wait` returns True
    when the event is set -- the loop exits WITHOUT calling shutdown on a
    server that is already shutting itself down.

    *shutdown* overrides `server.shutdown`, because uvicorn has no
    `serve_forever` to stop and wants `should_exit = True` instead. A callable
    rather than a branch per server type.

    A shutdown that raises is swallowed. This runs on a daemon thread at the
    moment the process is going away; propagating turns a tidy exit into a
    traceback in the operator's console and kills the thread that was supposed
    to be cleaning up."""
    tick = float(monitor.interval_s if interval_s is None else interval_s)
    stop = shutdown if shutdown is not None else server.shutdown
    while not stop_event.wait(tick):
        if monitor.should_shutdown(clock()):
            try:
                stop()
            except Exception:                              # noqa: BLE001
                pass
            return
