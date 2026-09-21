# SPDX-License-Identifier: Apache-2.0
"""The two lifecycle routes, added by SUBCLASSING (blueprint P1k, MEB §1.4).

`with_lifecycle(base, monitor)` mints a subclass of the handler
`dashboard.make_handler` already produced, with its own merged route tables
and one extra string. That is the whole mechanism, and it is chosen against
the two obvious alternatives:

* **Not** adding `/api/heartbeat` to `dashboard_post.py`. Those modules are
  the 24/7 panel's surface, and a heartbeat route there would exist on every
  dashboard -- on a server with no monitor to record it against.
  `dashboard_get.py` also stands at the 1000-line cap.
* **Not** re-implementing `do_GET` / `do_POST`. They dispatch through
  `self._GET_ROUTES` / `self._POST_ROUTES`, so a subclass with merged tables
  inherits the token guard, the local-host guard, gzip, keep-alive and the
  HIVE MIND rules -- and cannot DRIFT from them, because there is still only
  one copy of each.

**Why a fresh class per call rather than a module-level flag.** Two `hive ui`
servers can exist in one interpreter -- that is what the test suite is -- and a
module-level monitor would let them keep each other alive. Each wrap carries
its own `_monitor`, and `{**base._GET_ROUTES, ...}` builds a NEW dict so the
handler being wrapped is never mutated.
"""
from .js import heartbeat_script

__all__ = ["LifecycleRoutes", "with_lifecycle"]


class LifecycleRoutes:
    """The two route bodies, and no monitor of its own.

    `_monitor` is None here on purpose: this mixin is behaviour, and the
    monitor arrives on the subclass `with_lifecycle` mints. A default instance
    at class scope would be a process-wide singleton nobody asked for, shared
    by every UI in the interpreter."""

    _monitor = None

    def _post_heartbeat(self, proj, body):
        """The page is still open. Records a beat and reports the state back.

        It returns the snapshot rather than a bare `{"ok": true}` so the page
        can show an operator how long it has before the server gives up --
        one round trip instead of a second route polled beside this one."""
        self._monitor.record_ping()
        return self._json({"ok": True, "lifecycle": self._monitor.snapshot()})

    def _get_lifecycle(self, proj, am, qs):
        """What the monitor currently believes, WITHOUT recording a beat.

        Reading must not be evidence of life: if it were, `curl` in a loop --
        or a monitoring probe -- would keep the server up forever, which is
        precisely the state this feature exists to prevent."""
        return self._json({"ok": True, "lifecycle": self._monitor.snapshot()})


def with_lifecycle(base, monitor, *, heartbeat_ms=None):
    """*base* plus the lifecycle routes and the page fragment that drives them.

    *heartbeat_ms* overrides how often the PAGE beats, independently of the
    monitor's own cutoff; left None it is the monitor's interval, which is the
    only setting that makes sense outside a test.

    `LifecycleRoutes` comes first in the bases so its `_json` resolves to the
    handler's -- it has none of its own, deliberately: these are route bodies
    that belong to a handler, not a second response layer."""
    ms = (int(round(monitor.interval_s * 1000)) if heartbeat_ms is None
          else int(heartbeat_ms))

    class UiHandler(LifecycleRoutes, base):
        _monitor = monitor
        _extra_js = heartbeat_script(ms)
        _GET_ROUTES = {**base._GET_ROUTES,
                       "/api/lifecycle": LifecycleRoutes._get_lifecycle}
        _POST_ROUTES = {**base._POST_ROUTES,
                        "/api/heartbeat": LifecycleRoutes._post_heartbeat}

    return UiHandler
