# SPDX-License-Identifier: Apache-2.0
# src/mindhive/dashboard_locks.py
"""The dashboard's concurrency guards: one lock per mutating operation, and the
decorator that refuses a second concurrent call instead of running it.

Extracted from ``dashboard.py`` when that file reached 2043 lines. It sits BELOW
the route and job modules on purpose — ``dashboard_get``, ``dashboard_post`` and
``dashboard_jobs`` all take locks from here, and a lock defined in any one of
them would have to be imported by the others. A lock reached through an import
cycle is a lock you cannot reason about.

``dashboard`` re-exports every name here, because ``tests/test_dashboard_locks``
acquires them as ``dashboard._HARVEST_LOCK`` and they must be the SAME objects
the handlers take — a re-export is a second NAME for one lock, which is what that
test needs; a second lock would pass every assertion while guaranteeing nothing.
"""
import functools
import threading

# One reflection batch at a time: a second Reflect click while one is running
# would double token spend and race two writers over the same incident files.
_REFLECT_LOCK = threading.Lock()
# Serialize headless-invoke calls: each spawns a full `claude` subprocess, so
# concurrent clicks would fork a fleet of agents. One at a time, like reflect.
_INVOKE_LOCK = threading.Lock()

# The remaining mutating endpoints, one lock EACH. Until now their only mutual
# exclusion was the page disabling its own nav while a request was in flight —
# a client-side guard, so two tabs, a double click, or one curl started two full
# runs over the same repo. Selftest is the worst case (two concurrent pytest
# runs over the same tree), but a doubled harvest, sync or embed all put two
# writers over the same files.
#
# Projection is the one that is NO LONGER on that list: `projector.project()`
# now takes its own per-project lock, because nine call sites reach it and only
# some are handlers this decorator can wrap. `_PROJECT_LOCK` still earns its
# place — it refuses the SECOND CLICK at the door with a message, instead of
# queueing it to rebuild everything again a moment later — but it is no longer
# what makes concurrent rebuilds safe. Deleting it would cost a UX affordance,
# not a guarantee.
#
# Per operation rather than one shared lock: a harvest and a sync are unrelated
# jobs touching different files, and making them wait on each other would turn
# two legitimate clicks into a stall. Each lock is only ever about its OWN
# duplicate.
#
# WHO HOLDS THEM CHANGED IN T13, AND FOR HOW LONG. The six long operations now
# run as jobs (see `jobs.py`, and the `register_kind` calls at the foot of
# `dashboard_jobs.py` — the import that makes the six kinds exist), so their lock
# is taken by the JOB for as long as the work runs, not by the handler for as
# long as the response takes — `_one_at_a_time` would have released it the
# instant the 202 was written, before the work began. `_SYNC_LOCK`,
# `_PROJECT_LOCK` and `_VERIFY_LOCK` are still the decorator's, because those
# three still do their work inside the request.
#
# The locks did NOT become redundant with "one job per kind at a time". Each one
# also guards doors this module cannot see: `/api/plugins/autoscan` reaches the
# same machine-wide scan `/api/plugins/scan` runs, and the CLI reaches most of
# this work directly. Dropping them would have quietly narrowed the guarantee
# from "this work runs once" to "this endpoint starts it once".
_HARVEST_LOCK = threading.Lock()
_PROJECT_LOCK = threading.Lock()
_SYNC_LOCK = threading.Lock()
_EMBED_LOCK = threading.Lock()
_SELFTEST_LOCK = threading.Lock()
# ONE lock across BOTH plugin routes, deliberately. `/api/plugins/autoscan`
# reaches `plugins.ensure_fresh`, which runs the same machine-wide scan
# `/api/plugins/scan` runs directly — a lock per route would leave two scans
# racing over one plugin cache through two different doors, which is exactly the
# guarantee this is supposed to make.
_PLUGINS_LOCK = threading.Lock()
# The one GET that belongs in this list. `/api/verify` reads every event and
# recomputes the chain; T9 removed the nav disable that was its only guard, and
# the button is one click away from being clicked twice.
_VERIFY_LOCK = threading.Lock()

# Reflect batch size per request. Each incident is one `claude` CLI call (up
# to 120s), so an unbounded batch could outlive the browser's fetch timeout.
REFLECT_BATCH_LIMIT = 3


def _one_at_a_time(lock, busy):
    """Refuse a second concurrent call to a handler instead of running it.

    Written once as a decorator rather than six copies of the same
    acquire/try/finally: the boilerplate is identical every time and the two
    things that are easy to get wrong — `blocking=False`, and releasing from a
    `finally` — are exactly the parts a copy would drift on.

    The refusal is ``ok:False`` with HTTP **200**, not 409. This file's
    convention is that an expected domain rejection is a 200 body the page can
    render (see `_post_embed`'s LIVE_LOCK and `_post_append`'s validation), and
    only an unexpected server fault becomes a 500 in `do_POST`. "Something else
    is already running" is the former.

    `blocking=False` so the loser is answered immediately rather than parked on
    a worker thread waiting for a pytest run to finish; the `finally` so a
    handler that raises releases the endpoint instead of wedging it for the life
    of the process.

    `*a` rather than `(proj, body)`: GET handlers take `(proj, am, qs)`, and one
    of them needs this. `/api/verify` re-reads the whole ledger and recomputes
    the hash chain — as expensive as most of the POSTs guarded here — and its
    only protection was the nav disable that T9 removed. A read-only endpoint
    still deserves the guard when the read is the expensive part."""
    def decorate(fn):
        @functools.wraps(fn)
        def guarded(self, *a):
            if not lock.acquire(blocking=False):
                return self._json({"ok": False, "error": busy}, 200)
            # RELEASE BEFORE THE ANSWER IS PUBLISHED, not after. The `finally`
            # below used to be the only release, and it runs AFTER the handler
            # has already written its response: `wbufsize` is 0 on
            # BaseHTTPRequestHandler, so `wfile` is an unbuffered
            # `_SocketWriter` and those bytes are on the wire inside `fn`. A
            # caller holding a complete response could therefore call again and
            # be refused by the endpoint its OWN previous call had not finished
            # releasing -- and the refusal body has no payload keys, so the
            # second call looked like a scan that had been skipped for
            # freshness. That is what turned
            # test_plugins_autoscan_is_stale_gated red with `KeyError:
            # 'scanned'` on CI run 33693887912, and reproduced off-CI at 8/400
            # with request #2 sent down a pre-connected socket.
            #
            # `_send` calls this hook before its first write (see
            # `dashboard.py`), which is safe for every route guarded here
            # because in all four the send IS the last act -- the work is
            # already done by the time a byte moves. The `finally` stays for the
            # paths that never send: a handler that raises still releases.
            held = [True]
            outer = getattr(self, "_release_before_send", None)

            def release_once():
                if held:
                    held.clear()
                    lock.release()
                if outer is not None:      # a nested guard releases both
                    outer()

            self._release_before_send = release_once
            try:
                return fn(self, *a)
            finally:
                self._release_before_send = outer
                release_once()
        return guarded
    return decorate
