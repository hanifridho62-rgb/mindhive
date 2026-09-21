# SPDX-License-Identifier: Apache-2.0
"""`hive ui` — the dashboard as a PAGE rather than a service (blueprint P1k).

`hive dashboard` is a server you start and stop. `hive ui` is the same panel,
on the same port, that goes away on its own once you close the tab. One
function, and almost all of it is argument handling: the mechanism is
`server.lifecycle` and the routes are `server.routes`; this is where the two
meet `dashboard.serve`.

**It defaults to port 54461, the dashboard's own, on purpose.** One panel, one
URL, one bookmark. That makes one question load-bearing -- what happens when
the 24/7 dashboard is already there -- and `dashboard.serve` already answers
it: its single-instance guard opens the running server's URL and returns
BEFORE binding. So `hive ui` against a live dashboard is a browser tab, not a
second server, and the user's panel is never disturbed. That property is
pinned from both sides in `tests/test_server_app.py`, because the whole first
section of this repo's CLAUDE.md exists because of the one time it was not.

**The monitor is built before anything binds.** `HeartbeatMonitor` refuses an
interval that cannot beat its cutoff, and building it first turns that into a
usage error the operator reads instead of a server that shuts itself down
while they are looking at it.
"""
__all__ = ["serve_ui"]


def serve_ui(port=None, open_browser: bool = True, idle_cutoff_s: float = None,
             interval_s: float = None, asgi: bool = False, token=None) -> None:
    """Serve the panel until the page stops beating, then stop.

    *idle_cutoff_s* defaults to `lifecycle.IDLE_CUTOFF_S`; it is None here
    rather than that constant so the default lives in ONE module and a caller
    passing nothing gets whatever that module currently says.

    *interval_s* is DERIVED from the cutoff when it is not given, because a
    monitor refuses an interval that cannot fit inside its cutoff and
    `--idle-cutoff 5` would otherwise be a usage error rather than a short
    test. With the blueprint's own numbers the derivation returns 15.0, so
    the default pair is exactly what it was.

    *asgi* serves through `server.asgi` instead of the stdlib server. It needs
    `pip install -e .[ui]` and says so when the extras are absent."""
    from .. import dashboard
    from .lifecycle import (IDLE_CUTOFF_S, HeartbeatMonitor,
                            default_interval_for)

    cutoff = IDLE_CUTOFF_S if idle_cutoff_s is None else idle_cutoff_s
    monitor = HeartbeatMonitor(
        idle_cutoff_s=cutoff,
        interval_s=(default_interval_for(cutoff) if interval_s is None
                    else interval_s))
    if port is None:
        port = dashboard.DEFAULT_PORT
    if asgi:
        from .asgi import serve_asgi
        return serve_asgi(port=port, open_browser=open_browser,
                          monitor=monitor, token=token)
    return dashboard.serve(port=port, open_browser=open_browser, token=token,
                           lifecycle=monitor)
