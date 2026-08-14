# src/mindhive/dashboard.py
from __future__ import annotations

import collections
import functools
import gzip
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import paths, chain
from . import guard
from . import recall as recall_mod
from . import graph as graph_mod
from . import graphview
from . import plugins as plugins_mod
from . import invoker as invoker_mod
from . import export as export_mod
from . import events, projector
from . import jobs, outcomes
from . import dedupe
from . import taskview
from . import sync as sync_mod
from . import workspace as workspace_mod
from . import research as research_mod
from .briefing import briefing as build_briefing
from .dashboard_page import render_page
from .workspace_page import render_parallel
from .swarm_page import render_swarm
from .research_page import render_research

# A fixed default port + a token persisted under the store home keep the
# dashboard URL identical on every launch (http://127.0.0.1:54461/?token=…).
DEFAULT_PORT = 54461

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
# run as jobs (see `jobs.py` and the `register_kind` calls below), so their lock
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
            try:
                return fn(self, *a)
            finally:
                lock.release()
        return guarded
    return decorate


def _token_path():
    return paths.store_home() / "dashboard.token"


def load_or_create_token() -> str:
    """Return a stable dashboard token so the URL doesn't change between
    launches. Resolution order: MINDHIVE_DASHBOARD_TOKEN env → persisted file →
    freshly generated (and then persisted). Best-effort: a read/write failure
    falls back to an in-memory token rather than crashing the launch."""
    env = os.environ.get("MINDHIVE_DASHBOARD_TOKEN")
    if env:
        return env
    p = _token_path()
    try:
        if p.exists():
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except Exception:
        pass
    tok = secrets.token_urlsafe(16)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(tok, encoding="utf-8")
    except Exception:
        pass
    return tok


# --- project / session / memory separation -------------------------------
# A "project" is a store namespace; a "session" is one append stream
# (session_id) within it; "memory" is the semantic subset of events (decisions,
# caveats, …) as opposed to deterministic session telemetry. Keeping the three
# distinct is what makes intent and context legible at a glance.

def _folder_for_project(proj: str) -> str:
    """The project's own folder (where its native MEMORY.md belongs), taken from
    the autosync registry. Falls back to the server's cwd only when the project
    isn't registered — so picking a project in the UI syncs THAT project's
    folder, not wherever the dashboard happens to be launched from."""
    from . import autosync
    for e in autosync.load_registry():
        if e.get("project") == proj and e.get("folder"):
            return e["folder"]
    return os.getcwd()


def _all_projects() -> list:
    """Names of every store namespace that has an events log."""
    home = paths.store_home()
    names = []
    if home.exists():
        for d in sorted(home.iterdir()):
            if d.is_dir() and (d / "events").is_dir():
                names.append(d.name)
    return names


def _project_stats(name: str) -> dict:
    """Per-project counts for the picker, folded at most once per ledger change.

    Cached because `default_project()` is a loop over this: at launch it runs for
    every namespace on disk, so one dashboard start read every ledger on the
    machine. The fingerprint is stat-only and both of its lying states are
    handled in `_ledger_fingerprint` — on either one this is exactly the
    uncached function it was before."""
    return _fingerprinted(_STATS_MEMO, name, lambda: _project_stats_uncached(name))


def _project_stats_uncached(name: str) -> dict:
    evs = events.read_all(name)
    sessions, mem, auto = set(), 0, 0
    for ev in evs:
        sessions.add(ev.session_id)
        if ev.type in events.MEMORY_VIEW_TYPES:
            mem += 1
        else:
            auto += 1
    return {"name": name, "events": len(evs), "memory": mem, "auto": auto,
            "sessions": len(sessions), "last_ts": evs[-1].ts if evs else None}


# --- Improvements efficiency strip ----------------------------------------
# Failure rate = tool_failed per 100 tool actions. The denominator is tool
# activity only — user_prompt/assistant_response are logged for every exchange
# and would make the rate track conversation length instead of tool behavior.
_TOOL_ACTION_TYPES = ("command_run", "file_touched", "tool_failed")
_EFF_WINDOWS = (("day", 86400), ("week", 7 * 86400),
                ("month", 30 * 86400), ("quarter", 90 * 86400))
LOW_SAMPLE_FLOOR = 20     # tool actions per window below which a % is noise


def _iso_utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + "Z"


def _empty_tallies() -> dict:
    # window -> [cur_failures, cur_actions, prev_failures, prev_actions]
    return {key: [0, 0, 0, 0] for key, _ in _EFF_WINDOWS}


def _add_tallies(tallies: dict, evs, now: float) -> None:
    """Accumulate one project's tool-action counts into the window tallies.
    Called once per project; HIVE MIND sums across projects before rating."""
    cuts = {key: (_iso_utc(now - span), _iso_utc(now - 2 * span))
            for key, span in _EFF_WINDOWS}
    for ev in evs:
        if ev.type not in _TOOL_ACTION_TYPES or not ev.ts:
            continue          # guard a null ts (corrupt segment) like signal_stats
        for key, _ in _EFF_WINDOWS:
            cur_cut, prev_cut = cuts[key]
            if ev.ts >= cur_cut:
                i = 0
            elif ev.ts >= prev_cut:
                i = 2
            else:
                continue
            t = tallies[key]
            t[i + 1] += 1
            if ev.type == "tool_failed":
                t[i] += 1


def _efficiency(tallies: dict) -> dict:
    """Rates, deltas, and honesty dispositions per window: an empty current
    window and thin samples render as neutral states, never fake percents.
    delta_pct is signed relative change — negative means fewer failures."""
    out = {}
    for key, _ in _EFF_WINDOWS:
        cf, ca, pf, pa = tallies[key]
        cur_rate = round(cf * 100.0 / ca, 1) if ca else None
        prev_rate = round(pf * 100.0 / pa, 1) if pa else None
        row = {"cur_failures": cf, "cur_actions": ca,
               "prev_failures": pf, "prev_actions": pa,
               "cur_rate": cur_rate, "prev_rate": prev_rate, "delta_pct": None}
        if ca == 0:
            row["disposition"] = "no_current_data"
        elif pa == 0:
            row["disposition"] = "no_baseline"
        elif ca < LOW_SAMPLE_FLOOR or pa < LOW_SAMPLE_FLOOR:
            row["disposition"] = "low_sample"
        elif prev_rate == 0 and cur_rate == 0:
            row["disposition"], row["delta_pct"] = "no_change", 0.0
        elif prev_rate == 0:
            row["disposition"] = "regression_from_zero"
        else:
            row["delta_pct"] = round((cur_rate - prev_rate) * 100.0 / prev_rate, 1)
            row["disposition"] = "ok"
        out[key] = row
    return out


def default_project() -> str:
    """The project the panel opens on: the one with the most events (so a fresh
    launch shows real data, not an empty cwd-derived namespace). Falls back to
    the cwd-detected project when the store is empty."""
    stats = [s for s in (_project_stats(n) for n in _all_projects()) if s["events"] > 0]
    if stats:
        stats.sort(key=lambda s: (s["events"], s["name"]), reverse=True)
        return stats[0]["name"]
    return paths.detect_project()


# --- HIVE MIND: one unified view across every project --------------------
# Selecting project="*" in the panel aggregates all namespaces into a single
# stream — every session, log, memory and bit of context unified. It's a
# read-only lens; mutating actions still need a concrete target project.
ALL_TOKENS = {"*", "__all__", "all"}
HIVEMIND_LABEL = "⨁ HIVE MIND (all projects)"
HIVEMIND_PROJECT = "__hivemind__"   # synthetic namespace for the unified graph cache


def is_all(proj) -> bool:
    return proj in ALL_TOKENS


def all_event_pairs() -> list:
    """(project_name, Event) across every namespace, ordered oldest→newest."""
    pairs = []
    for name in _all_projects():
        for ev in events.read_all(name):
            pairs.append((name, ev))
    pairs.sort(key=lambda pe: pe[1].ts or "")
    return pairs


def event_pairs(proj: str, am: bool) -> list:
    """(project_name, Event) for the current selection, oldest→newest — the one
    place the HIVE MIND (am) fork lives for event-iterating routes. am → every
    project's events unified; otherwise → just ``proj``'s, each labelled ``proj``."""
    if am:
        return all_event_pairs()
    return [(proj, e) for e in events.read_all(proj)]


def _overview_counts(pairs) -> dict:
    """Tally overview stats from an iterable of (session_key, Event). Shared by
    the single-project and HIVE MIND overviews so the counting lives in one
    place; the caller chooses the session key (a bare id, or (project, id) when
    unifying across projects)."""
    by_type, sessions, mem, auto, total, last_ts = {}, set(), 0, 0, 0, None
    for skey, ev in pairs:
        by_type[ev.type] = by_type.get(ev.type, 0) + 1
        sessions.add(skey)
        if ev.type in events.MEMORY_VIEW_TYPES:
            mem += 1
        else:
            auto += 1
        total += 1
        if ev.ts and (last_ts is None or ev.ts > last_ts):
            last_ts = ev.ts
    return {"events": total, "memory_events": mem, "auto_events": auto,
            "sessions": len(sessions), "by_type": by_type, "last_ts": last_ts}


def _safe_chain_ok(name: str, evs=None) -> bool:
    try:
        return bool(chain.verify(name, evs=evs).get("ok"))
    except Exception:
        return False


# Both memos below are process-global and live for the life of the server, so
# they are bounded rather than left to grow with the number of namespaces ever
# polled. `hive projects` reports 56 stores on this machine, and HIVE MIND mode
# plus a project picker makes visiting many of them a normal afternoon; each
# entry holds a full telemetry fold and every session record for that project.
#
# An LRU rather than a size-only cap because the access pattern is extreme: one
# namespace is hot (the picker's current selection) and the rest are cold the
# moment it changes. Eviction costs a single recompute of the endpoint the gate
# was added to make cheap — the same price a first visit pays.
_MEMO_MAX = 8


def _memo_put(memo: "collections.OrderedDict", key, value) -> None:
    memo[key] = value
    memo.move_to_end(key)
    while len(memo) > _MEMO_MAX:
        memo.popitem(last=False)


def _memo_get(memo: "collections.OrderedDict", key):
    if key not in memo:
        return None
    memo.move_to_end(key)      # a read counts as use, or the hot key ages out
    return memo[key]


def _ledger_fingerprint(proj) -> dict | None:
    """The stat-only fingerprint of *proj*'s ledger, or **None when no
    fingerprint can be trusted**.

    `sidecar.segment_stats` is `os.scandir` over `[size, mtime_ns]` — microseconds,
    and it never opens or parses a byte. That makes it the right primitive for
    "may I skip the read?", and it lies in exactly two states. Both are answered
    here, once, because they are two ways of asking the same question and a
    caller that remembers one and forgets the other builds a cache that is worse
    than no cache:

    * **`{}` is never-equal.** `segment_stats` answers `{}` both for "no events
      directory" and for ANY `OSError`. That is the fail-safe direction for the
      sidecar (`{} != a real stamp` reads as stale) and it INVERTS as a cache
      key: `{} == {}` would score a hit, so one transient directory error would
      freeze the cached answer for the life of the process — silently, which is
      the one failure mode a cache must not have.
    * **A non-file backend has no fingerprint at all.** `events.read_all`
      short-circuits to Postgres when `MINDHIVE_EVENT_BACKEND` is `pg` or `dual`,
      while `segment_stats` keeps stat'ing the local `.jsonl` segments — which,
      on the documented file → dual → pg migration path, are still on disk and
      never change again. The fingerprint would be non-empty and stable forever:
      every poll a hit, the view frozen at the first response, nothing reporting
      it.

    Returning None for both, rather than a falsy-but-comparable value, is what
    makes the guards impossible to half-apply: there is no value here that a `==`
    can match, so an untrusted fingerprint cannot become a hit by accident."""
    if os.environ.get("MINDHIVE_EVENT_BACKEND", "file").strip().lower() != "file":
        return None
    from .mindnet import sidecar as sidecar_mod
    stats = sidecar_mod.segment_stats(proj)
    return stats or None


def _fingerprinted(memo: "collections.OrderedDict", proj, compute):
    """*compute*'s result for *proj*, recomputed only when the ledger moved.

    The whole point is to skip the READ. `_SESSIONS_MEMO` is the wrong precedent
    for this and is kept deliberately distinct: it fingerprints
    `(len(evs), evs[-1].ts)`, which needs `evs`, so by the time it can answer,
    the read it was meant to avoid has already happened — it memoizes the fold.

    An untrusted fingerprint (see `_ledger_fingerprint`) computes and stores
    NOTHING: no entry to go stale, and no chance of a later hit against a stamp
    that cannot move."""
    fingerprint = _ledger_fingerprint(proj)
    if fingerprint is None:
        return compute()
    cached = _memo_get(memo, proj)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    value = compute()
    _memo_put(memo, proj, (fingerprint, value))
    return value


# Per-project counts for the picker. `default_project()` computes these for
# EVERY namespace on disk — 56 on this machine — at launch and on every project
# list, and each one was a full `read_all`.
# {project: (fingerprint, stats dict)}
_STATS_MEMO: "collections.OrderedDict" = collections.OrderedDict()


# One memo of the session fold, shared by every poll of /api/mindnet/cats.
# {(project, all_mode): (fingerprint, records)}
_SESSIONS_MEMO: "collections.OrderedDict" = collections.OrderedDict()

# The outer gate for the same endpoint, keyed on the stat-only ledger
# fingerprint so an unchanged ledger is never even READ.
# {project: (segment_stats, telemetry fold, session records)}
_CATS_MEMO: "collections.OrderedDict" = collections.OrderedDict()


def _with_online(records, stamp):
    """*records* with `online` re-derived at *stamp*.

    THE field that must never be memoized: `online` is a function of the wall
    clock, not of the events, so a cached copy would freeze a session as working
    (or as gone) until the ledger next moved. Every layer of caching over this
    endpoint therefore stores the FOLD and calls this on the way out.

    Idempotent by construction — `dict(r, online=…)` replaces the key rather
    than reading it — so re-deriving over already-derived rows is correct, which
    is what lets `_CATS_MEMO` hold rows that already went through here once.

    The rule itself is oversee's own final line applied to oversee's own
    constant, not a second implementation of liveness."""
    from . import oversee as oversee_mod
    from .loop.state import _ts_epoch
    out = []
    for r in records:
        last = _ts_epoch(r.get("last_event_ts"))
        out.append(dict(r, online=(last is not None
                                   and (stamp - last)
                                   < oversee_mod.ONLINE_WINDOW_SECONDS)))
    return out


def _sessions_cached(proj, am, evs, stamp):
    """`oversee.interactive_sessions` for *evs*, folded at most once per ledger
    change rather than once per poll.

    Measured on the real store (25.5k events) this fold costs 150-300 ms, and
    both pages poll this endpoint on a 1.2-2.5 s timer regardless of which
    section is visible — on the swarm page it is chained INTO the agent-graph
    repaint, so the graph refreshed measurably less often for it. The fold is
    also doubly wasteful internally: `skill_trails` and the main loop each
    re-derive `headline()` for the same rows.

    The fingerprint is the event count plus the newest timestamp, which is exactly
    what `sidecar._staleness` already uses for the same class of problem: nothing
    appended means the fold cannot have changed.

    `online` is the one field that must NOT be memoized — it is a function of the
    wall clock, not of the events — so `_with_online` re-derives it per call; the
    FOLD is what is cached.

    This is the INNER gate. `_CATS_MEMO` sits in front of it and answers the
    common case without reading the ledger at all; this one still earns its keep
    on the paths that gate cannot serve — HIVE MIND (which bypasses it), and a
    miss whose segment stats moved without the event count or newest ts
    changing."""
    from . import oversee as oversee_mod
    key = (proj, bool(am))
    fingerprint = (len(evs), evs[-1].ts if evs else "")
    cached = _memo_get(_SESSIONS_MEMO, key)
    if cached is None or cached[0] != fingerprint:
        records = oversee_mod.interactive_sessions(
            project=proj, evs=evs, now_fn=lambda: stamp)
        _memo_put(_SESSIONS_MEMO, key, (fingerprint, records))
    else:
        records = cached[1]
    return _with_online(records, stamp)


def make_handler(token: str):
    class DashboardHandler(BaseHTTPRequestHandler):
        # HTTP/1.1, so the browser reuses one connection for the whole page
        # instead of a TCP handshake per request — this panel issues dozens of
        # small JSON requests per poll cycle and two pollers run on timers.
        #
        # Keep-alive brings two obligations, and BOTH are prerequisites rather
        # than nice-to-haves; this line is deliberately the LAST of the three to
        # ship. (1) Every response must carry an exact Content-Length, or the
        # client cannot find where one body ends and the next begins — both
        # response paths here set it, and `send_error` sets its own. (2) A
        # connection may only be REUSED when the last request was read to its end
        # and answered exactly once; see `_body` and `_fail`. Without those,
        # keep-alive does not produce an error — it produces answers attached to
        # the wrong questions.
        protocol_version = "HTTP/1.1"

        # And keep-alive costs a thread per idle connection: the base class's
        # `timeout` is None, so `rfile.readline()` on a connection the browser is
        # holding open blocks forever and the worker never returns to the pool. A
        # timeout turns "pinned until the tab closes" into "released after 30s of
        # silence"; the client just reconnects, which is what keep-alive already
        # assumes can happen.
        timeout = 30

        # Whether bytes of a response for the request in flight are already on
        # the wire. Class-level default so it reads correctly even if a failure
        # happens before `handle_one_request` runs.
        _responded = False

        def log_message(self, *args):  # keep the console quiet
            pass

        def handle_one_request(self):
            # Keep-alive reuses ONE handler instance for every request on the
            # connection, so this is per-REQUEST state and has to be reset here.
            # Setting it in __init__ would make the flag mean "this connection
            # has ever answered", which would silence every error response after
            # the first.
            self._responded = False
            return BaseHTTPRequestHandler.handle_one_request(self)

        def send_response(self, *args, **kwargs):
            # The one place every response passes through — this class's two
            # writers and the base class's `send_error` alike — so it is the only
            # place that can reliably know a reply has started.
            self._responded = True
            return BaseHTTPRequestHandler.send_response(self, *args, **kwargs)

        def _fail(self, exc):
            """The single error exit for a route that raised.

            A second full response for one request is not a duplicate: under
            keep-alive it is FRAMED as the next response, so every later
            request/response pair on that connection is answered off by one. Once
            headers are out, the honest move is to stop talking and let the
            connection close — the client sees one truncated response and
            reconnects, instead of parsing our error JSON as the body of
            something it asked for later."""
            if self._responded:
                self.close_connection = True
                return None
            return self._json({"ok": False, "error": str(exc)}, 500)

        # --- helpers ---
        def _check_token(self) -> bool:
            qs = parse_qs(urlparse(self.path).query)
            supplied = self.headers.get("X-Hive-Token") or (qs.get("token") or [None])[0]
            if supplied is None:
                return False
            # constant-time compare to avoid leaking the token via timing
            return secrets.compare_digest(str(supplied), str(token))

        # Compress bodies above one MTU when the client accepts gzip — the
        # page is ~72KB (→ ~22KB) and the Improvements/graph JSON payloads
        # shrink similarly; below the threshold the header overhead wins.
        GZIP_MIN_BYTES = 1400

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            accepts = "gzip" in (self.headers.get("Accept-Encoding") or "")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if accepts and len(data) >= self.GZIP_MIN_BYTES:
                data = gzip.compress(data, 6)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

        # Bodies are tiny JSON control messages; cap the read so an
        # unauthenticated caller (do_POST drains the body before the token
        # check to avoid a WinError 10053 reset) can't force an arbitrarily
        # large allocation or hold a worker thread on a slow/oversized body.
        MAX_BODY_BYTES = 1 << 20  # 1 MiB

        def _body(self) -> dict:
            """The request body as JSON, or ValueError with the connection marked
            for closing.

            **A body we refuse to read is a connection we stop reusing**, and
            that is the whole of the rule — one answer for all three refusals
            below.

            Keep-alive is what makes this load-bearing. The refused bytes are
            still sitting in the socket, so the server's next `readline()` would
            read the middle of that body as a request line: the refusal would
            stop being a refusal of ONE request and desynchronise every request
            after it on the connection, answering calls the client never made.
            Reading the refused body would also restore the framing, and the
            first cut of this did exactly that — but it is an UNTRUSTED read. A
            caller that declares a megabyte and then sends nothing holds a worker
            thread until the handler timeout, which is precisely the cost
            MAX_BODY_BYTES exists to refuse; no byte cap fixes it, because the
            bytes never arrive. Closing needs no cap, no chunk loop, and no trust
            in the caller.

            What closing costs: with unread bytes still inbound, Windows aborts
            the connection (WinError 10053) instead of delivering our 500, so an
            oversized caller may see a reset rather than the reason. These bodies
            are tiny JSON control messages by construction — the callers that
            reach these branches are broken or probing, a reset answers both, and
            the alternative is a thread any local process can pin on demand.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                # A length we cannot parse is a body whose END we cannot find.
                self.close_connection = True
                raise ValueError("invalid Content-Length")
            if length < 0:
                self.close_connection = True
                raise ValueError("invalid Content-Length")
            if length > self.MAX_BODY_BYTES:
                self.close_connection = True
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        # --- GET route handlers -------------------------------------------
        # Each takes (proj, am, qs) and sends its own response. They are wired to
        # their path in _GET_ROUTES (bottom of the class) and dispatched once by
        # do_GET, so there is no long if/elif ladder. Routes that iterate events
        # get their (project, Event) source from event_pairs(), which is the one
        # place the HIVE MIND (am) fork lives.
        def _get_projects(self, proj, am, qs):
            stats = [_project_stats(n) for n in _all_projects()]
            stats.sort(key=lambda s: (s["events"], s["name"]), reverse=True)
            total = {"name": HIVEMIND_LABEL, "id": "*",
                     "events": sum(s["events"] for s in stats),
                     "memory": sum(s["memory"] for s in stats),
                     "auto": sum(s["auto"] for s in stats),
                     "sessions": sum(s["sessions"] for s in stats)}
            return self._json({"projects": stats, "hivemind": total,
                               "default": default_project(), "current": proj})

        def _get_state(self, proj, am, qs):
            if am:
                return self._send(200, "HIVE MIND unifies every project — pick a single "
                                  "project to read its STATE.md.", "text/plain; charset=utf-8")
            p = paths.project_dir(proj) / "STATE.md"
            text = p.read_text(encoding="utf-8") if p.exists() else "(no STATE.md — run project)"
            return self._send(200, text, "text/plain; charset=utf-8")

        def _get_briefing(self, proj, am, qs):
            if am:
                return self._send(200, "HIVE MIND unifies every project — pick a single "
                                  "project for its briefing.", "text/plain; charset=utf-8")
            bpath = build_briefing(proj)
            return self._send(200, bpath.read_text(encoding="utf-8"), "text/plain; charset=utf-8")

        @_one_at_a_time(_VERIFY_LOCK, "a chain verification is already running")
        def _get_verify(self, proj, am, qs):
            if am:
                per = {n: _safe_chain_ok(n) for n in _all_projects()}
                return self._json({"ok": all(per.values()) if per else False, "per_project": per})
            return self._json(chain.verify(proj))

        def _get_overview(self, proj, am, qs):
            if am:
                names = _all_projects()
                # read each project's log ONCE, then derive stats AND the chain
                # check from the same events (no second full read)
                evs_by_proj = {n: events.read_all(n) for n in names}
                stats = _overview_counts(
                    ((name, ev.session_id), ev)
                    for name, evs in evs_by_proj.items() for ev in evs)
                return self._json({
                    "project": HIVEMIND_LABEL, "projects": len(names), **stats,
                    "chain_ok": (all(_safe_chain_ok(n, evs_by_proj[n]) for n in names)
                                 if names else False),
                })
            evs = events.read_all(proj)
            stats = _overview_counts((ev.session_id, ev) for ev in evs)
            return self._json({
                "project": proj, **stats,
                # reuse the events we just read — no second full read
                "chain_ok": bool(chain.verify(proj, evs=evs).get("ok")),
            })

        def _get_memory(self, proj, am, qs):
            # the semantic subset only — the intent/context layer. Build the
            # (project, Event) rows once so the projection + dedupe.collapse lives
            # in a single place instead of once per am / single-project branch.
            from . import roadmap as roadmap_mod
            session = (qs.get("session") or [None])[0]
            limit = int((qs.get("limit") or ["200"])[0])
            rows = [(n, e) for n, e in event_pairs(proj, am)
                    if e.type in events.MEMORY_VIEW_TYPES]
            if session:
                rows = [(n, e) for n, e in rows if e.session_id == session]
            rows = list(reversed(rows))[:limit]      # newest first

            def _text(e):
                if not isinstance(e.payload, dict):
                    return str(e.payload)
                if e.type == "roadmap":
                    # roadmap payloads are structured (no "text"): render the
                    # same self-describing body the LEDGER projection uses so
                    # the Memory view never shows a blank plan row.
                    return roadmap_mod.ledger_body(e.payload)
                return e.payload.get("text", "")
            # Collapse near-duplicate memories by meaning (same type + project),
            # keeping the newest; display-time only, ledger untouched.
            # similar_count rides along for the UI hint.
            return self._json(dedupe.collapse([
                {"ts": e.ts, "type": e.type, "session": e.session_id, "project": n,
                 "text": _text(e), "tags": e.tags,
                 "origin": events.origin_of(e.session_id, e.tags)}
                for n, e in rows
            ]))

        def _get_improvements(self, proj, am, qs):
            # Lessons learned: machine-learned incident lessons (auto) plus
            # user-dictated caveat notes (manual), kept as two shapes so long
            # status memos can't masquerade as lesson cards. One event pass per
            # project feeds the caveats, the per-signature enrichment
            # (timestamps, leaks, resolution glyph), and the efficiency tiles.
            from . import incidents
            names = _all_projects() if am else [proj]
            now = time.time()
            auto, manual = [], []
            tallies = _empty_tallies()
            for n in names:
                evs = events.read_all(n)
                _add_tallies(tallies, evs, now)
                stats_map = incidents.signal_stats(n, evs=evs)
                for s in incidents.summaries(n):
                    st = stats_map.get(s["signature"], {})
                    resolution = incidents.classify_resolution(s, st)
                    auto.append(dict(
                        s, project=n, source="auto",
                        first_seen=st.get("first_seen"),
                        last_seen=st.get("last_seen"),
                        leaks=st.get("leaks", 0),
                        reroutes=st.get("reroutes", 0),
                        last_reroute=st.get("last_reroute"),
                        resolution=resolution,
                        **incidents.classify_triage(resolution, s)))
                for e in evs:
                    if e.type != "caveat":
                        continue
                    manual.append({
                        "ts": e.ts, "type": e.type, "session": e.session_id,
                        "project": n, "source": "manual", "tags": e.tags,
                        "origin": events.origin_of(e.session_id, e.tags),
                        "text": (e.payload.get("text", "")
                                 if isinstance(e.payload, dict) else str(e.payload)),
                    })
            auto.sort(key=lambda s: s.get("occurrences", 0), reverse=True)
            manual.sort(key=lambda m: m.get("ts") or "", reverse=True)
            return self._json({"auto": auto, "manual": dedupe.collapse(manual),
                               "stats": _efficiency(tallies)})

        def _get_sessions(self, proj, am, qs):
            by: dict = {}
            for name, e in event_pairs(proj, am):
                key = (name, e.session_id)
                s = by.get(key)
                if s is None:
                    s = by[key] = {"session_id": e.session_id, "project": name,
                                   "events": 0, "memory": 0,
                                   "origin": None,
                                   "first_ts": e.ts, "last_ts": e.ts, "_evs": []}
                s["events"] += 1
                s["_evs"].append(e)
                if not s["origin"]:
                    s["origin"] = events.origin_of(e.session_id, e.tags)
                if e.type in events.MEMORY_VIEW_TYPES:
                    s["memory"] += 1
                if e.ts and e.ts < s["first_ts"]:
                    s["first_ts"] = e.ts
                if e.ts and e.ts > s["last_ts"]:
                    s["last_ts"] = e.ts
            out = sorted(by.values(), key=lambda s: s["last_ts"] or "", reverse=True)
            for s in out:
                s["activity"] = taskview.tally(s.pop("_evs"))
                s["status"] = taskview.status_of(s["last_ts"])
            return self._json(out)

        def _get_changelog(self, proj, am, qs):
            # Mandatory code change-log. Shape and ordering delegate to
            # changelog.entries() — one source of truth, so this view can
            # never disagree with `hive log show`. The pending marker is a
            # per-project sidecar, so HIVE MIND shows entries without a
            # pending banner; categories ride along so the UI never hardcodes
            # the taxonomy.
            from . import changelog as changelog_mod
            if am:
                rows = []
                for n in _all_projects():
                    rows.extend(changelog_mod.entries(events.read_all(n),
                                                      project=n))
                rows.sort(key=lambda r: r["ts"] or "", reverse=True)
                pending = None
            else:
                rows = changelog_mod.entries(events.read_all(proj),
                                             project=proj)
                pending = changelog_mod.load_pending(proj)
            return self._json({"entries": rows,
                               "categories": list(changelog_mod.CATEGORIES),
                               "pending": pending})

        def _get_sops(self, proj, am, qs):
            # Managed SOP registry. Active set and history both delegate to
            # sop.py (one source of truth for fold/ordering), so this view
            # can never disagree with `hive sop list` / `hive sop show`.
            from . import sop as sop_mod
            if am:
                active, hist = [], []
                for n in _all_projects():
                    evs = events.read_all(n)
                    for r in sop_mod.current(evs):
                        active.append(dict(r, project=n))
                    hist.extend(sop_mod.entries(evs, project=n))
                active.sort(key=lambda r: (r.get("created") or "",
                                           r.get("sop_id") or ""))
                hist.sort(key=lambda r: r["ts"] or "", reverse=True)
            else:
                evs = events.read_all(proj)
                active = sop_mod.current(evs)
                hist = sop_mod.entries(evs, project=proj)
            return self._json({"active": active, "history": hist,
                               "actions": list(sop_mod.ACTIONS)})

        def _get_task(self, proj, am, qs):
            sid = (qs.get("session") or [""])[0]
            # In HIVE MIND mode each card carries its own project — a concrete
            # namespace is required to read the right ledger.
            tproj = (qs.get("session_project") or [None])[0]
            if am and not tproj:
                return self._json({"ok": False,
                                   "error": "session_project required"}, 400)
            evs = [e for e in events.read_all(tproj or proj)
                   if e.session_id == sid]
            return self._json(taskview.items(evs))

        def _get_task_detail(self, proj, am, qs):
            sid = (qs.get("session") or [""])[0]
            return self._json(taskview.detail(sid))

        def _get_recall(self, proj, am, qs):
            q = (qs.get("q") or [""])[0]
            semantic = (qs.get("semantic") or ["0"])[0] in ("1", "true")
            limit = int((qs.get("limit") or ["10"])[0])
            force = (qs.get("force") or ["0"])[0] in ("1", "true")
            if am:
                hits = []
                for n in _all_projects():
                    for h in recall_mod.recall(q, project=n, semantic=semantic, limit=limit, force=force):
                        h = dict(h)
                        h["project"] = n
                        hits.append(h)
                return self._json(hits)
            return self._json(list(recall_mod.recall(q, project=proj, semantic=semantic, limit=limit, force=force)))

        def _get_graph(self, proj, am, qs):
            # Serve the persisted graph.json when it is still fresh (autosync
            # keeps it current), rebuilding only when events actually changed —
            # so re-opening the Graph tab is a cheap cache read, not a full
            # rebuild + 60 MB serialize + disk write. Semantic edges fold in the
            # embedding-derived semantic_near links but cost an embedding search
            # per event, so they are the opt-in path (?semantic=1), never the
            # default load.
            semantic = (qs.get("semantic") or ["0"])[0] in ("1", "true")
            if am:
                # HIVE MIND aggregates every namespace; freshness is the newest
                # event across all of them (a cheap stat sweep), and the full
                # aggregate log is only read on a cache miss.
                src_mtime = max((graph_mod._events_mtime(n) for n in _all_projects()),
                                default=0.0)
                graph = graph_mod.load_or_build(
                    project=HIVEMIND_PROJECT, semantic=semantic,
                    source_mtime=src_mtime,
                    build_evs=lambda: [e for _, e in all_event_pairs()])
            else:
                graph = graph_mod.load_or_build(proj, semantic=semantic)
            html = graphview.render_html(graph)
            return self._send(200, html, "text/html; charset=utf-8")

        def _get_plugins_recommend(self, proj, am, qs):
            if am:
                for n in _all_projects():
                    cat = plugins_mod.latest_catalog(n)
                    if cat:
                        return self._json(cat)
                return self._json({})
            return self._json(plugins_mod.latest_catalog(proj) or {})

        def _get_plugins_inventory(self, proj, am, qs):
            # machine-level (what THIS Claude install has), so project-agnostic
            return self._json(plugins_mod.live_inventory())

        def _get_skills_detail(self, proj, am, qs):
            name = (qs.get("name") or [""])[0]
            plugin = (qs.get("plugin") or [None])[0] or None
            detail = plugins_mod.skill_detail(name, plugin=plugin)
            if detail is None:
                return self._json({"ok": False, "error": "not found"}, 404)
            return self._json(detail)

        def _get_skills_status(self, proj, am, qs):
            # status: green/red last outcome per skill; counts: total invokes —
            # both derived from the ledger in a single pass. HIVE MIND merges
            # every namespace's ledger, mirroring /api/improvements — skills
            # telemetry must not read "*" as a literal (empty) namespace.
            evs = [e for _, e in all_event_pairs()] if am else None
            return self._json(plugins_mod.call_stats(project=proj, evs=evs))

        def _get_skills_history(self, proj, am, qs):
            name = (qs.get("name") or [""])[0]
            limit = int((qs.get("limit") or ["20"])[0])
            evs = [e for _, e in all_event_pairs()] if am else None
            return self._json(plugins_mod.call_history(name, project=proj,
                                                       limit=limit, evs=evs))

        def _get_mindnet_cats(self, proj, am, qs):
            # MN-5's cat board: which faculties are awake, dimming, or gone.
            # Imported lazily like the other mindnet touchpoints so a dashboard
            # in a project that has never used the phase spine pays nothing.
            #
            # `now` is resolved HERE and passed in, because `ui_cats.board` is
            # pure by design — that is what makes its 180 s and 6 h boundaries
            # testable at the boundary instead of approximately.
            from datetime import datetime, timezone

            from . import oversee as oversee_mod
            from .mindnet import telemetry as tel_mod
            from .mindnet import ui_cats as cats_mod
            from .mindnet import ui_sessions as sess_mod
            # ONE clock for both boards. The faculty side ages rows from an ISO
            # string and `oversee` decides liveness from an epoch float; reading
            # the clock twice would let a session straddle the two and render as
            # working on one axis and stale on the other in the same payload.
            stamp = time.time()
            now = datetime.fromtimestamp(stamp, timezone.utc) \
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            # THE gate. Measured on the real store this endpoint returned 503
            # bytes in 6.0 s under load — all of it `read_all` plus the two
            # folds, on a route every open page polls every 2.5 s. An unchanged
            # ledger cannot change either fold, so a hit reads nothing and folds
            # nothing.
            #
            # `segment_stats` is the same stat-only fingerprint the phase sidecar
            # already answers staleness with: `os.scandir`'s cached stat data,
            # microseconds, never opens or parses a byte.
            #
            # THREE properties are load-bearing, and the cache is worse than no
            # cache without any one of them:
            #   * An EMPTY fingerprint is an unconditional MISS, and so is a
            #     NON-FILE BACKEND. Both now live in `_ledger_fingerprint`, which
            #     returns None for either — one place, so the pair cannot be
            #     half-applied by the next caller that wants this gate. Their
            #     reasoning is there; the gap that shipped is recorded below.
            #   * HIVE MIND (am) bypasses the gate outright. It reads EVERY
            #     namespace through `all_event_pairs`, and `segment_stats` is
            #     per-project — a single-project stamp would miss an append in
            #     any other namespace, and would miss a whole new namespace
            #     appearing. Stamping every namespace is possible but is a
            #     scandir each plus a re-listing to notice new ones; bypassing is
            #     unconditionally correct, and this is the rarer path.
            #
            # The non-file-backend guard was specified alongside the {} one and
            # did not ship: the comment here said "two properties are
            # load-bearing" and named a different second, so the gap read as
            # completeness. That is the failure mode this whole plan exists to
            # remove, so it is named rather than fixed quietly — and both guards
            # now sit behind one helper so the next gate inherits them.
            #
            # What is memoized is the two FOLDS, never the response: `now` and
            # `online` are functions of the wall clock and are re-derived below
            # on every call, hit or miss (see `_with_online`).
            fp = None if am else _ledger_fingerprint(proj)
            memo = _memo_get(_CATS_MEMO, proj) if fp else None
            if memo is not None and memo[0] == fp:
                tel_state, records, have_sessions = memo[1], memo[2], True
            else:
                # ONE ledger read for both folds. They ask different questions of
                # the SAME events, and `interactive_sessions` takes `evs` for
                # exactly this reason — re-reading would double the cost of the
                # miss against a store measured in tens of thousands of events.
                evs = [e for _, e in all_event_pairs()] if am else \
                    events.read_all(proj)
                tel_state = tel_mod.fold(evs)
                # The session half is isolated. `oversee.interactive_sessions` is
                # not documented as total and it folds every event twice
                # internally — letting it raise would 500 the whole endpoint and
                # take the faculty strip, which was reliable before this, down
                # with it. The page's own rule is that a failing board must not
                # take out the page; the server owes it the same.
                try:
                    records = _sessions_cached(proj, am, evs, stamp)
                    have_sessions = True
                except Exception:               # noqa: BLE001
                    records, have_sessions = [], False
                # Only a fold that SUCCEEDED is memoized. Storing the degraded
                # answer would pin "cannot know" behind the gate until the ledger
                # next moved; re-attempting costs exactly what this endpoint cost
                # before the gate existed, which is the honest price of a fault.
                if fp and have_sessions:
                    _memo_put(_CATS_MEMO, proj, (fp, tel_state, records))
            # Both boards render from THIS call's clock, whatever the gate did.
            # `_with_online` runs unconditionally — on a miss it is a redundant
            # (idempotent) second pass over a few dozen rows, which is the price
            # of there being exactly one place that can freeze `online`.
            cats = cats_mod.board(tel_state, now)
            try:
                sessions = sess_mod.board(_with_online(records, stamp), now)
            except Exception:                       # noqa: BLE001
                sessions = {"cats": (), "total": 0, "hidden": 0}
                have_sessions = False
            producers = tel_mod.producers()
            # Report the session signal as unobservable when the fold that feeds
            # it just failed, so the strip says "cannot know" instead of the
            # confident "nothing running" — the exact distinction this map exists
            # to make.
            producers["session"] = producers.get("session", False) and have_sessions
            # The thresholds ride along so the page renders "idle 4h" without a
            # second copy of 180/21600 in JavaScript (invariant 10).
            return self._json({"cats": list(cats),
                               "sessions": list(sessions["cats"]),
                               "sessions_total": sessions["total"],
                               "sessions_hidden": sessions["hidden"],
                               # Which signals this BUILD can observe at all, so
                               # an empty strip can say "nothing running" rather
                               # than being mistaken for "cannot know".
                               "producers": producers,
                               "dim_after_secs": cats_mod.DIM_AFTER_SECS,
                               "hide_after_secs": cats_mod.HIDE_AFTER_SECS,
                               "idle_vanish_secs": cats_mod.IDLE_VANISH_SECS,
                               "online_after_secs":
                                   oversee_mod.ONLINE_WINDOW_SECONDS})

        def _get_skills_stats(self, proj, am, qs):
            # per-skill completion-rate telemetry aggregated from the ledger
            # (skill_used / invoke_result / tool_failed skill:*) — U5/KTD5
            evs = [e for _, e in all_event_pairs()] if am else None
            return self._json({"stats": plugins_mod.skill_stats(project=proj,
                                                                evs=evs)})

        def _get_skills_recommend(self, proj, am, qs):
            # deterministic most-needed-skills ranking (U6/KTD6): one ledger
            # read feeds both the stats and the need extraction. HIVE MIND
            # merges events AND incident summaries across every namespace.
            top = (qs.get("top") or [None])[0]
            evs = summ = None
            if am:
                from . import incidents
                evs = [e for _, e in all_event_pairs()]
                summ = [s for n in _all_projects()
                        for s in incidents.summaries(project=n)]
            return self._json(plugins_mod.recommend_for(
                project=proj, evs=evs, top=int(top) if top else None,
                incident_summaries=summ))

        def _get_skills_trail(self, proj, am, qs):
            # Durable per-session skill trail (agent-native parity with the CLI
            # and dashboard cards) — reads the SAME shared fold Oversee uses, so
            # ended/stale sessions stay auditable after they leave the board.
            # HIVE MIND merges every namespace like the other skills endpoints.
            from . import oversee as oversee_mod
            iu = oversee_mod.intended_use_map()
            session = (qs.get("session") or [None])[0] or None
            names = _all_projects() if am else [proj]
            out = {}
            for n in names:
                for sid, entries in oversee_mod.skill_trails(
                        project=n, intended_use=iu).items():
                    if session and sid != session:
                        continue
                    out[sid] = entries
            return self._json({"trails": out})

        def _get_oversee(self, proj, am, qs):
            # Oversee: every kind of live Claude work — hive loop runs AND
            # interactive sessions (straight from hook telemetry, no /circle
            # needed). Dismissed/finished/stale entries are already excluded
            # by the projections. Each card carries its roadmap plan (the ask
            # + plan steps + progress, see roadmap.py) when one exists for its
            # run/session id; plans with no live card — notably interrupted
            # ones whose run already left the board — ride the top-level
            # ``plans`` list so a roadmap is never orphaned.
            from . import oversee as oversee_mod
            from . import roadmap as roadmap_mod
            from .loop import state as loop_state
            names = _all_projects() if am else [proj]
            # Built once per request (60 s memoized) so every card's skill trail
            # gets the intended-use "why" fallback without a per-session rescan.
            iu = oversee_mod.intended_use_map()
            runs, plans, dismissed = [], [], 0
            for n in names:
                evs = events.read_all(n)
                res = loop_state.list_runs(n)
                rmap = roadmap_mod.fold(evs)
                live_ids = set()
                for r in res["runs"] + oversee_mod.interactive_sessions(
                        n, evs=evs, intended_use=iu):
                    r["project"] = n
                    plan = rmap.get(r["run_id"])
                    if plan is not None and plan["status"] != "completed":
                        r["roadmap"] = plan
                    live_ids.add(r["run_id"])
                    runs.append(r)
                for p in roadmap_mod.plans(evs):
                    if p["plan_id"] not in live_ids:
                        plans.append(dict(p, project=n))
                dismissed += res["dismissed"]
            runs.sort(key=lambda r: r.get("started") or "", reverse=True)
            plans.sort(key=lambda p: p.get("updated") or "", reverse=True)
            return self._json({"runs": runs, "dismissed": dismissed,
                               "plans": plans,
                               "any_online": any(r["online"] for r in runs)})

        def _get_workspace(self, proj, am, qs):
            # /api/workspace/<action>: the two iframe pages render HTML; every
            # other action is JSON, delegated to workspace.api_get. HIVE MIND
            # (am) falls back to the default project — workspace agents always
            # need a concrete namespace to log their I/O into.
            action = urlparse(self.path).path.rsplit("/", 1)[-1]
            if action in ("parallel", "swarm"):
                html = render_parallel(token) if action == "parallel" else render_swarm(token)
                return self._send(200, html, "text/html; charset=utf-8")
            return self._json(workspace_mod.api_get(
                action, proj=default_project() if am else proj, qs=qs))

        def _post_workspace(self, proj, body):
            action = urlparse(self.path).path.rsplit("/", 1)[-1]
            return self._json(workspace_mod.api_post(action, proj=proj, body=body))

        def _get_research(self, proj, am, qs):
            # /api/research/<action>: "page" renders the Self-Reliant iframe
            # view; everything else is JSON via research.api_get. Like the
            # workspace, HIVE MIND falls back to the default project so run
            # events land in a concrete namespace.
            action = urlparse(self.path).path.rsplit("/", 1)[-1]
            if action == "page":
                return self._send(200, render_research(token),
                                  "text/html; charset=utf-8")
            return self._json(research_mod.api_get(
                action, proj=default_project() if am else proj, qs=qs))

        def _post_research(self, proj, body):
            action = urlparse(self.path).path.rsplit("/", 1)[-1]
            return self._json(research_mod.api_post(action, proj=proj, body=body))

        def _get_export(self, proj, am, qs):
            path = export_mod.export()
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="handoff.md"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # --- routing ---
        def do_GET(self):
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/":
                return self._send(200,
                                  render_page(token,
                                              tasks_css=taskview.CARDS_CSS,
                                              tasks_js=taskview.CARDS_JS),
                                  "text/html; charset=utf-8")
            if not self._check_token():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            handler = self._GET_ROUTES.get(route)
            if handler is None:
                return self._json({"ok": False, "error": "not found"}, 404)
            qs = parse_qs(parsed.query)
            proj = (qs.get("project") or [None])[0] or default_project()
            am = is_all(proj)
            try:
                return handler(self, proj, am, qs)
            except Exception as e:  # never crash the server
                # Unexpected failure -> 500 so monitoring and clients can tell a
                # server fault apart from a normal empty/negative result. Expected
                # domain rejections return their own ok:False, 200 inside handlers.
                # Via `_fail`, because a route that raised AFTER writing (a
                # streaming export, a handler that sends then post-processes) must
                # not have a second response framed behind its first.
                return self._fail(e)

        # --- POST route handlers ------------------------------------------
        # Each takes (proj, body) and sends its own response, wired via
        # _POST_ROUTES. The HIVE MIND read-only guard and the mutating-action
        # target check live in do_POST, before dispatch.
        def _start(self, kind, proj, body, busy):
            """Accept a long operation and hand back a job to poll.

            **202, not 200.** The work has not been done when this returns, and a
            200 would be a claim that it had. The refusal below stays ``ok:False``
            with a 200 — this file's convention for an expected domain rejection
            (see `_one_at_a_time`), and unchanged from when these ran inline, so
            "already running" reads the same to the page either way.

            No `_one_at_a_time` decorator on the routes that come through here:
            the JOB holds the operation's lock for as long as the work runs. A
            decorator would hold it only for the length of this response and
            release it before the work began.
            """
            job = jobs.start(kind, proj, body)
            if job is None:
                return self._json({"ok": False, "error": busy}, 200)
            return self._json({"ok": True, "job": job.snapshot(0)}, 202)

        def _post_harvest(self, proj, body):
            # self-healing backfill: capture any session the hooks missed
            return self._start("harvest", proj, body,
                               "a harvest is already running")

        def _post_append(self, proj, body):
            etype = body.get("type")
            raw = body.get("payload", "")
            tags = [t for t in (body.get("tags") or "").split(",") if t]
            if not etype:
                return self._json({"ok": False, "error": "type is required"}, 200)
            # A structured body IS the payload; only a plain string is free
            # text needing the {"text": …} wrapper. Wrapping unconditionally
            # meant this endpoint could never produce a valid registry payload
            # (a mindnet_task/roadmap/sop/code_change/autoresume contract is
            # never one text field) — so validating without this first would
            # have rejected every registry append instead of gating it.
            payload = raw if isinstance(raw, dict) else {"text": raw}
            try:
                # The same dispatch `hive append` runs (events._PAYLOAD_
                # CONTRACTS), so the CLI and this endpoint cannot drift.
                events.validate_typed_payload(etype, payload)
            except ValueError as e:
                # An expected domain rejection, not a server fault: ok:False,
                # 200 like _post_embed's LIVE_LOCK. Nothing is appended, and
                # nothing raises out of the handler.
                return self._json({"ok": False, "error": str(e)}, 200)
            ev = events.append_event(etype, payload, tags=tags, project=proj)
            projector.project(proj)
            return self._json({"ok": True, "type": ev.type, "ts": ev.ts, "project": proj})

        @_one_at_a_time(_PROJECT_LOCK, "a projection rebuild is already running")
        def _post_project(self, proj, body):
            return self._json({"ok": True, "result": projector.project(proj)})

        @_one_at_a_time(_SYNC_LOCK, "a sync is already running")
        def _post_sync(self, proj, body):
            folder = _folder_for_project(proj)
            path = sync_mod.sync(folder, project=proj)
            return self._json({"ok": True, "path": str(path), "folder": folder})

        def _post_embed(self, proj, body):
            # The guard stays SYNCHRONOUS: it is instant, and answering an active
            # LIVE_LOCK with a job id the caller must poll to discover it was
            # refused would be a worse contract than saying so now.
            try:
                guard.ensure_safe(force=bool(body.get("force", False)))
            except RuntimeError as e:
                # LIVE_LOCK is an expected, safe-to-report rejection — not a
                # server fault — so keep it ok:False, 200.
                return self._json({"ok": False, "error": str(e)}, 200)
            return self._start("embed", proj, body,
                               "an embedding pass is already running")

        def _post_plugins_scan(self, proj, body):
            # The all-projects fallback lives in the job body, which is the one
            # place that knows where the catalog is written.
            return self._start("plugins_scan", proj, body,
                               "a plugin scan is already running")

        def _post_skills_invoke(self, proj, body):
            name = (body.get("name") or "").strip()
            plugin = (body.get("plugin") or None)
            if not name:
                return self._json({"ok": False, "error": "name is required"}, 200)
            if not _INVOKE_LOCK.acquire(blocking=False):
                return self._json({"ok": False,
                                   "error": "another skill call is running"}, 200)
            try:
                res = invoker_mod.invoke_skill(name, plugin=plugin, project=proj)
            finally:
                _INVOKE_LOCK.release()
            return self._json({"ok": res["ok"], "name": name, "plugin": plugin,
                               "exit": res["exit"], "excerpt": res["excerpt"]})

        # Shares _PLUGINS_LOCK with /api/plugins/scan on purpose: `ensure_fresh`
        # runs that same scan when the catalog is stale, so a lock per route
        # would leave two scans reachable over one cache through two doors.
        @_one_at_a_time(_PLUGINS_LOCK, "a plugin scan is already running")
        def _post_plugins_autoscan(self, proj, body):
            if is_all(proj):                       # global scan; see _post_plugins_scan
                proj = default_project()
            res = plugins_mod.ensure_fresh(
                top_n=int(body.get("top", 5)),
                max_age_hours=float(body.get("max_age_hours", 24)),
                force=bool(body.get("force", False)),
                project=proj,
            )
            return self._json({"ok": True, "scanned": res["scanned"],
                               "age_hours": res["age_hours"], "result": res["payload"]})

        def _bad_limit(self, body):
            """The `limit` check both reflection routes share, kept synchronous:
            a non-numeric limit is a caller mistake to report now, not a job to
            start and then fail. Returns the rejection response, or None."""
            try:
                int(body.get("limit", REFLECT_BATCH_LIMIT))
            except (TypeError, ValueError):
                return self._json({"ok": False,
                                   "error": "limit must be an integer"}, 200)
            return None

        def _post_rules_reflect(self, proj, body):
            return (self._bad_limit(body)
                    or self._start("rules_reflect", proj, body,
                                   "reflection already running"))

        def _post_rules_resolve(self, proj, body):
            return (self._bad_limit(body)
                    or self._start("rules_resolve", proj, body,
                                   "resolution already running"))

        def _post_rules_reflect_one(self, proj, body):
            # Reflect ONE named failure signature on demand — the "reflect this
            # failure" action on a failed skill call. Unlike the batch endpoint,
            # this bypasses the recurrence-threshold gate: the user is explicitly
            # asking to learn from this single failure now.
            import shutil
            from . import incidents, reflect as reflect_mod
            signature = (body.get("signature") or "").strip()
            if not signature:
                return self._json({"ok": False, "error": "signature is required"}, 200)
            if not _REFLECT_LOCK.acquire(blocking=False):
                return self._json({"ok": False,
                                   "error": "reflection already running"}, 200)
            try:
                # ensure the incident projection exists (even a single-occurrence
                # candidate gets a file), then reflect that specific signature.
                incidents.audit(project=proj)
                incidents.project(project=proj)
                if not incidents.load(signature, project=proj):
                    return self._json({"ok": False,
                                       "error": f"no such incident: {signature}"}, 200)
                res = reflect_mod.reflect_incident(signature, project=proj)
                if not res.get("reflected"):
                    if not shutil.which("claude"):
                        return self._json({"ok": False, "error":
                            "claude CLI unavailable — reflection needs it on PATH"}, 200)
                    return self._json({"ok": False, "signature": signature,
                        "error": res.get("reason", "reflection produced no rule")}, 200)
                return self._json({"ok": True, "reflected": True,
                                   "signature": signature})
            finally:
                _REFLECT_LOCK.release()

        def _post_rules_dismiss(self, proj, body):
            # Explicit demotion from a lesson card — the only way the gray
            # "dismissed" state is reachable without dropping to the CLI.
            # demote() appends the rule_demoted event and re-projects the
            # record, so the next Improvements fetch classifies it dismissed.
            from . import incidents
            sig = (body.get("signature") or "").strip()
            if not sig or not incidents.load(sig, project=proj):
                return self._json({"ok": False, "error": "unknown signature"}, 200)
            incidents.demote(sig, project=proj)
            return self._json({"ok": True, "signature": sig})

        def _post_oversee_dismiss(self, proj, body):
            # Manual session delete: append-only tombstone, so the hash chain
            # stays intact and every projection rebuild keeps the run hidden.
            # Deleting an already-dismissed run (e.g. the engine auto-dismissed
            # it between poll and click) is a benign no-op, not an error.
            from . import oversee as oversee_mod
            from .loop import state as loop_state
            run_id = (body.get("run_id") or "").strip()
            if not run_id:
                return self._json({"ok": False, "error": "run_id is required"}, 200)
            known = {r["run_id"] for r in loop_state.list_runs(proj)["runs"]}
            known |= {s["run_id"] for s in oversee_mod.interactive_sessions(proj)}
            if run_id not in known:
                already = any(
                    e.type == "loop_run_dismissed"
                    and (e.payload or {}).get("run_id") == run_id
                    for e in events.read_all(proj))
                if already:
                    return self._json({"ok": True, "run_id": run_id,
                                       "already_dismissed": True})
                return self._json({"ok": False,
                                   "error": f"unknown run: {run_id}"}, 200)
            events.append_event("loop_run_dismissed",
                                {"run_id": run_id, "cause": "manual"}, project=proj)
            return self._json({"ok": True, "run_id": run_id})

        def _get_enrich_settings(self, proj, am, qs):
            # Prompt enrichment toggle (default ON — see enrich.py). HIVE MIND
            # reads the default project's setting: the panel is hidden under
            # "*", but the fetch must still answer coherently.
            from . import enrich as enrich_mod
            return self._json(enrich_mod.settings(
                default_project() if am else proj))

        def _post_enrich_toggle(self, proj, body):
            # Concrete project required — "*" is already rejected by the
            # do_POST read-only guard before this handler runs.
            from . import enrich as enrich_mod
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                return self._json({"ok": False,
                                   "error": "enabled must be true or false"}, 200)
            st = enrich_mod.set_enabled(enabled, project=proj)
            return self._json({"ok": True, "settings": st})

        def _post_enrich_preview(self, proj, body):
            # Deterministic enrichment of one draft prompt. ``record`` appends
            # the prompt_enriched audit event — set only when the user actually
            # uses the result (copy), so previews never spam the ledger.
            from . import enrich as enrich_mod
            text = body.get("prompt")
            if not isinstance(text, str) or not text.strip():
                return self._json({"ok": False, "error": "prompt is required"}, 200)
            answers = body.get("answers")
            res = enrich_mod.enrich(
                text, project=proj,
                answers=answers if isinstance(answers, dict) else None)
            if body.get("record"):
                enrich_mod.record(text, res, project=proj)
            return self._json({"ok": True, "result": res})

        def _post_selftest(self, proj, body):
            return self._start("selftest", proj, body,
                               "a self test is already running")

        def _get_job(self, proj, am, qs):
            """Poll one job. `offset` is how many output bytes the caller already
            has, so a 2.5s poll carries only what is new — the whole buffer used
            to be re-sent every tick (T16).

            An unknown id is ``ok:False`` with a 200, not a 404: the honest reason
            is almost always that the server restarted, and the page needs to say
            that rather than render a transport error.
            """
            job_id = (qs.get("id") or [""])[0]
            job = jobs.get(job_id)
            if job is None:
                return self._json({"ok": False, "error":
                    "no such job — the dashboard may have restarted since it "
                    "started"}, 200)
            offset = (qs.get("offset") or ["0"])[0]
            return self._json({"ok": True, "job": job.snapshot(offset)})

        def do_POST(self):
            route = urlparse(self.path).path
            # Drain the request body BEFORE any early response: replying 403
            # while the client is still sending aborts the connection on
            # Windows (WinError 10053) instead of delivering the status.
            try:
                body = self._body()
            except Exception as e:
                return self._fail(e)
            if not self._check_token():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            try:
                proj = body.get("project") or default_project()
                if is_all(proj):
                    if route == "/api/project":
                        # Takes the SAME lock as _post_project. This branch
                        # answers and returns before _POST_ROUTES dispatch, so
                        # it never passes through the decorated handler —
                        # HIVE MIND was the one remaining door into
                        # projector.project() that T8's decorator could not see,
                        # and it is the widest one (every namespace, not one).
                        if not _PROJECT_LOCK.acquire(blocking=False):
                            return self._json({"ok": False, "error":
                                "a projection rebuild is already running"}, 200)
                        try:
                            res = {n: projector.project(n)
                                   for n in _all_projects()}
                        finally:
                            _PROJECT_LOCK.release()
                        return self._json({"ok": True, "result": res})
                    # selftest + harvest + plugin scans + auth verification are
                    # global (machine-wide, not tied to one project); every other
                    # mutating action needs a concrete target. The plugin
                    # handlers substitute the default project for storage when
                    # in all-mode.
                    if route not in ("/api/selftest", "/api/harvest",
                                     "/api/plugins/scan", "/api/plugins/autoscan",
                                     "/api/workspace/auth_verify"):
                        return self._json({"ok": False, "error":
                            "HIVE MIND is a unified read-only view across all projects — "
                            "pick a specific project for this action."}, 200)
                handler = self._POST_ROUTES.get(route)
                if handler is None:
                    return self._json({"ok": False, "error": "not found"}, 404)
                return handler(self, proj, body)
            except Exception as e:
                # Unexpected failure -> 500 (see do_GET). The HIVE MIND read-only
                # guard and the "type is required" check return ok:False, 200
                # explicitly as expected domain rejections.
                return self._fail(e)

        # --- route tables (path -> handler) -------------------------------
        # Defined after the handlers so the names resolve; dispatched once by
        # do_GET / do_POST in place of a long if/elif ladder.
        _GET_ROUTES = {
            "/api/projects": _get_projects,
            "/api/state": _get_state,
            "/api/briefing": _get_briefing,
            "/api/verify": _get_verify,
            "/api/overview": _get_overview,
            "/api/memory": _get_memory,
            "/api/improvements": _get_improvements,
            "/api/sessions": _get_sessions,
            "/api/task": _get_task,
            "/api/task_detail": _get_task_detail,
            "/api/oversee": _get_oversee,
            "/api/changelog": _get_changelog,
            "/api/sops": _get_sops,
            "/api/recall": _get_recall,
            "/api/graph": _get_graph,
            "/api/mindnet/cats": _get_mindnet_cats,
            "/api/plugins/recommend": _get_plugins_recommend,
            "/api/plugins/inventory": _get_plugins_inventory,
            "/api/skills/detail": _get_skills_detail,
            "/api/skills/status": _get_skills_status,
            "/api/skills/history": _get_skills_history,
            "/api/skills/stats": _get_skills_stats,
            "/api/skills/trail": _get_skills_trail,
            "/api/skills/recommend": _get_skills_recommend,
            "/api/enrich/settings": _get_enrich_settings,
            "/api/export": _get_export,
            "/api/job": _get_job,
        }
        for _p in ("parallel", "swarm", "agents", "output", "profiles",
                   "auth", "board"):
            _GET_ROUTES[f"/api/workspace/{_p}"] = _get_workspace
        del _p
        for _p in ("page", "status", "leaderboard", "output", "learnings",
                   "prds", "sources"):
            _GET_ROUTES[f"/api/research/{_p}"] = _get_research
        del _p
        _POST_ROUTES = {
            "/api/harvest": _post_harvest,
            "/api/append": _post_append,
            "/api/project": _post_project,
            "/api/sync": _post_sync,
            "/api/embed": _post_embed,
            "/api/plugins/scan": _post_plugins_scan,
            "/api/skills/invoke": _post_skills_invoke,
            "/api/plugins/autoscan": _post_plugins_autoscan,
            "/api/rules/reflect": _post_rules_reflect,
            "/api/rules/reflect_one": _post_rules_reflect_one,
            "/api/rules/resolve": _post_rules_resolve,
            "/api/rules/dismiss": _post_rules_dismiss,
            "/api/oversee/dismiss": _post_oversee_dismiss,
            "/api/enrich/toggle": _post_enrich_toggle,
            "/api/enrich/preview": _post_enrich_preview,
            "/api/selftest": _post_selftest,
        }
        for _p in ("spawn", "send", "run_recipe", "set_model", "stop",
                   "remove", "clear_idle", "profile_add", "profile_remove",
                   "profile_login", "profile_trust", "auth_verify",
                   "board_save", "board_dispatch", "mission", "mission_stop",
                   "command"):
            _POST_ROUTES[f"/api/workspace/{_p}"] = _post_workspace
        del _p
        for _p in ("start", "stop"):
            _POST_ROUTES[f"/api/research/{_p}"] = _post_research
        del _p

    return DashboardHandler


def run_catch_up() -> dict:
    """Smart-harvest every transcript dir into its project and refresh the
    touched projections. Used by the manual endpoint and the background loop."""
    from . import harvest as harvest_mod
    res = harvest_mod.catch_up(skip_captured=True)
    for proj in res.get("projects", {}):
        try:
            projector.project(proj)
        except Exception:
            pass
    return res


# --- the six long operations, as jobs (T13) ----------------------------------
# Each body answers ONE question the endpoint could not: what did the run
# actually do? `ok: True` said only that the code reached the end, so a harvest
# that found nothing and a harvest that ingested forty sessions reported
# identically — and that ambiguity was the whole complaint. Every body below has
# to choose between DID_WORK and NOTHING_TO_DO, which is the one place the
# distinction can be made honestly, because it is the only place that has the
# counts.
#
# Module level, not inside `make_handler`: registration happens once at import,
# and a runner that closed over a handler instance could not be tested without
# an HTTP server. The endpoints reach them by kind NAME, so nothing here needs to
# be defined before the handler class.
#
# Validation and refusals stay SYNCHRONOUS at the endpoint (a bad `limit`, an
# active LIVE_LOCK): they are instant, and answering them with a job id the
# caller then has to poll to learn it was rejected would be a worse contract.

def _job_harvest(project, request, emit) -> outcomes.Report:
    res = run_catch_up()
    if res.get("locked"):
        # Another harvester — the background loop, or the CLI — holds the file
        # lock. Not NOTHING_TO_DO: there may well have been work, and this run
        # simply did not get to look.
        return outcomes.Report(outcomes.DEGRADED,
                               "another harvest was already running — nothing "
                               "was captured by this one", res)
    new = int(res.get("total_new") or 0)
    touched = len(res.get("projects") or {})
    skipped = res.get("skipped_unknown") or []
    tail = (f"; skipped {len(skipped)} untracked namespace(s)" if skipped else "")
    if not new:
        return outcomes.Report(outcomes.NOTHING_TO_DO,
                               f"no sessions the hooks missed{tail}", res)
    return outcomes.Report(
        outcomes.DID_WORK,
        f"harvested {new} session(s) into {touched} project(s){tail}", res)


def _job_embed(project, request, emit) -> outcomes.Report:
    from . import vectors
    force = bool(request.get("force", False))
    try:
        res = vectors.embed(project, force=force)
    except RuntimeError as e:
        # The semantic layer is optional by invariant (DATA_MODEL §5), so absent
        # extras are a DEGRADED store rather than a broken dashboard — reporting
        # that as FAILED would send someone hunting a bug in a dependency they
        # chose not to install.
        #
        # Narrowed to the chained ImportError rather than every RuntimeError, and
        # that distinction is load-bearing: `vectors._require_deps` is the ONLY
        # path that raises from an ImportError, so anything else reaching here is
        # a real fault and must stay FAILED. A blanket `except RuntimeError` had
        # exactly the flattening this module exists to stop — it labelled a
        # genuine crash as "optional dependency missing".
        if isinstance(e.__cause__, ImportError):
            return outcomes.Report(outcomes.DEGRADED, str(e))
        raise
    embedded = int(res.get("embedded") or 0)
    if not embedded:
        return outcomes.Report(outcomes.NOTHING_TO_DO,
                               "no LEDGER entries to embed yet", res)
    return outcomes.Report(outcomes.DID_WORK,
                           f"embedded {embedded} LEDGER entr(ies)", res)


def _job_plugins_scan(project, request, emit) -> outcomes.Report:
    # Plugins/skills are installed machine-wide, so a scan is global. In HIVE
    # MIND mode there is no concrete namespace to store the catalog under — fall
    # back to the default project so the recommend route can still find it.
    target = default_project() if is_all(project) else project
    res = plugins_mod.scan_and_store(top_n=int(request.get("top", 5)),
                                     project=target)
    counts = res.get("counts") or {}
    total = int(counts.get("plugins_total") or 0)
    skills = int(counts.get("skills_total") or 0)
    if not total and not skills:
        return outcomes.Report(outcomes.NOTHING_TO_DO,
                               "no plugins or skills found in the cache", res)
    return outcomes.Report(
        outcomes.DID_WORK,
        f"catalogued {total} plugin(s) and {skills} skill(s)", res)


def _reflect_limit(request) -> int:
    """Clamp to [0, REFLECT_BATCH_LIMIT]. A direct API caller must not be able to
    request an unbounded batch of 120s-per-incident claude calls."""
    return max(0, min(int(request.get("limit", REFLECT_BATCH_LIMIT)),
                      REFLECT_BATCH_LIMIT))


def _job_rules_reflect(project, request, emit) -> outcomes.Report:
    # Reflect a bounded batch of active, not-yet-reflected incidents into
    # structured lessons. Candidates below the recurrence threshold wait until
    # they recur; already-reflected records are skipped (the CLI's `hive rules
    # reflect` re-reflects everything — this deliberately does not).
    import shutil
    from . import incidents, reflect as reflect_mod
    limit = _reflect_limit(request)
    incidents.audit(project=project)       # self-heal a stale projection first
    incidents.project(project=project)
    eligible = [s["signature"] for s in incidents.summaries(project)
                if s["status"] == "active" and s["rule_source"] == "none"]
    if not eligible:
        # Names the filter, not just its result. "no active failure without a
        # lesson already" was true and unreadable: it reported that the count
        # was zero without saying what had been counted, so a healthy store and
        # a filter looking at the wrong fields produced the identical sentence.
        # The two fields are the ones on the line above.
        return outcomes.Report(outcomes.NOTHING_TO_DO,
                               "nothing to reflect: no incident with "
                               "status=active and rule_source=none. Zero is "
                               "the healthy answer here — it means every live "
                               "failure has already produced a lesson, not "
                               "that the search came up empty",
                               {"reflected": 0, "failed": 0, "pending": 0})
    reflected = failed = 0
    for signature in eligible[:limit]:
        emit(f"reflecting {signature}\n")
        if reflect_mod.reflect_incident(signature, project=project).get("reflected"):
            reflected += 1
        else:
            failed += 1
    # pending = eligible not yet turned into a lesson, excluding the ones tried
    # this round: they were attempted, not skipped.
    res = {"reflected": reflected, "failed": failed,
           "pending": max(0, len(eligible) - reflected - failed)}
    if failed and not reflected and not shutil.which("claude"):
        return outcomes.Report(
            outcomes.FAILED,
            "claude CLI unavailable — reflection needs it on PATH", res)
    if failed:
        return outcomes.Report(
            outcomes.DEGRADED,
            f"reflected {reflected}, {failed} failed, {res['pending']} pending",
            res)
    return outcomes.Report(
        outcomes.DID_WORK,
        f"reflected {reflected} lesson(s), {res['pending']} pending", res)


def _job_rules_resolve(project, request, emit) -> outcomes.Report:
    # Active resolution over the red (unresolved) set: reflect incidents with no
    # rule yet, reroute leaking auto rules to the best distinct alternative
    # (human rules are never touched). Same clamp as Reflect — each red incident
    # can cost a 120s claude call.
    import shutil
    from . import resolve as resolve_mod
    res = resolve_mod.resolve_batch(project=project, limit=_reflect_limit(request))
    rerouted = int(res.get("rerouted") or 0)
    reflected = int(res.get("reflected") or 0)
    failed = int(res.get("failed") or 0)
    done = rerouted + reflected
    if failed and not done and not shutil.which("claude"):
        return outcomes.Report(
            outcomes.FAILED,
            "claude CLI unavailable — resolution needs it on PATH", res)
    if failed:
        return outcomes.Report(
            outcomes.DEGRADED,
            f"rerouted {rerouted}, reflected {reflected}, {failed} failed", res)
    if not done:
        return outcomes.Report(outcomes.NOTHING_TO_DO,
                               "nothing red left to resolve", res)
    return outcomes.Report(
        outcomes.DID_WORK,
        f"rerouted {rerouted}, reflected {reflected}", res)


def _job_selftest(project, request, emit) -> outcomes.Report:
    """Run the repo's own pytest suite, from the web server, on a browser click.

    That sentence is alarming on purpose, so here is the justification the
    five-surfaces review asked for — including the parts that weaken it.

    **What it actually is.** Not pytest in-process. `verify.default_argv`
    (`verify.py:69`) builds `[sys.executable, "-m", "pytest", "-q", <tests>]` and
    `verify.stream` spawns it through `subprocess.Popen` (`verify.py:141`) from a
    jobs worker thread, argv-list only — `shell=True` appears nowhere in that
    module and a test pins its absence. Output is streamed line by line into the
    job artifact rather than captured wholesale, so a four-minute suite is not
    four minutes of an empty pane, and a process that dies mid-run is sealed as
    `job_abandoned` on the next boot instead of staying "running" forever.

    **What holds it safe — three things, stated at their real strength:**

    * **The token.** One token for the WHOLE server, not per route:
      `make_handler(token)` (`:527`) closes over a single value and `_check_token`
      (`:592-598`) compares every request against it (constant-time, so it does
      not leak by timing). It is normally delivered in the URL query string —
      `stable_url` (`:1888-1894`) builds `http://127.0.0.1:<port>/?token=<tok>`,
      which is what the launcher opens. A URL-borne bearer token is a materially
      weaker hold than "per-route auth" would imply: it lands in shell history,
      in whatever the browser keeps, and in any log that records a request line.
      It is stated that way here rather than oversold, because a hold nobody has
      described accurately is a hold nobody can audit.
    * **The single-flight lock.** `_SELFTEST_LOCK` (`:88`) is passed to
      `jobs.register_kind` (`:1832-1833`), so a second click while a run is in
      flight is refused rather than queued. The worst case it prevents is
      concrete: two pytest processes over the same working tree, writing the same
      caches and temp files, each reporting results the other invalidated.
    * **Local-only binding.** The server binds `127.0.0.1` explicitly
      (`ThreadingHTTPServer(("127.0.0.1", port), handler)`, `:2014`), so nothing
      off this machine can reach the route to begin with.

    **What it costs when someone does click it.** A multi-minute, CPU-heavy
    subprocess starts, on the machine already running the dashboard whose own
    latency this project has spent a plan measuring. That is the honest price and
    it is not hidden behind the word "selftest".

    **Revocation.** Remove any ONE of those three holds and this justification is
    void, not merely weaker: bind to `0.0.0.0` and the local-only argument is
    gone; drop the lock and concurrent runs corrupt each other's tree; make the
    token optional and an unauthenticated request starts a process. Writing that
    down is the point of the docstring — the next person to touch any of the
    three should find the condition stated before they trip it."""
    from . import selftest as selftest_mod
    # echo=False: the server's stdout is the terminal the dashboard was launched
    # from, and the person who wants this output is looking at the browser. `emit`
    # is what puts it there, line by line, so a four-minute suite is not four
    # minutes of an empty pane.
    res = selftest_mod.run(emit=emit, echo=False)
    code = res["exit_code"]
    counts = res.get("counts") or {}
    tally = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no counts parsed"
    if code == 0:
        return outcomes.Report(outcomes.DID_WORK, f"suite green — {tally}", res)
    return outcomes.Report(outcomes.FAILED,
                           f"suite exited {code} — {tally}", res)


# Opt-in, one line each. The lock is the pre-existing per-operation lock: the
# same work is reachable from the CLI and, for plugins, from a sibling endpoint,
# so "one job per kind" alone would have quietly dropped that guarantee.
jobs.register_kind("harvest", "Catching up on missed sessions",
                   _job_harvest, _HARVEST_LOCK)
jobs.register_kind("embed", "Embedding the ledger", _job_embed, _EMBED_LOCK)
jobs.register_kind("plugins_scan", "Scanning plugins and skills",
                   _job_plugins_scan, _PLUGINS_LOCK)
jobs.register_kind("rules_reflect", "Reflecting failures into lessons",
                   _job_rules_reflect, _REFLECT_LOCK)
jobs.register_kind("rules_resolve", "Resolving red lessons",
                   _job_rules_resolve, _REFLECT_LOCK)
jobs.register_kind("selftest", "Running the test suite",
                   _job_selftest, _SELFTEST_LOCK)


def _transcripts_fingerprint(root=None) -> float:
    """Cheap activity signal: the newest mtime across all transcript .jsonl
    files. Advances whenever any session file is created or appended, so the
    harvest loop can skip work entirely when nothing has changed. Stat-only —
    no file contents are read."""
    if root is None:
        from . import harvest as harvest_mod
        root = harvest_mod.claude_projects_root()
    newest = 0.0
    try:
        for d in os.scandir(root):
            if not d.is_dir():
                continue
            try:
                for f in os.scandir(d.path):
                    if f.name.endswith(".jsonl"):
                        m = f.stat().st_mtime
                        if m > newest:
                            newest = m
            except OSError:
                continue
    except OSError:
        pass
    return newest


def _auto_harvest_loop(stop_event) -> None:
    """Low-latency background capture: poll a cheap mtime fingerprint of the
    transcripts and harvest ONLY when something actually changed. Idle ticks cost
    just stat() calls, so the poll can be fast without wasting CPU/IO; a freshly
    typed prompt is captured within one poll interval.

    Off via MINDHIVE_AUTOHARVEST=0; poll seconds via MINDHIVE_AUTOHARVEST_SECS
    (default 3, floor 1). The cross-process harvest lock keeps this safe to run
    alongside the scheduled autosync task."""
    if os.environ.get("MINDHIVE_AUTOHARVEST", "1").lower() in ("0", "false", "off", "no"):
        return
    try:
        poll = max(1, int(os.environ.get("MINDHIVE_AUTOHARVEST_SECS", "3")))
    except Exception:
        poll = 3
    # Catch up once on startup so an already-running session is captured promptly.
    try:
        run_catch_up()
    except Exception:
        pass
    last_fp = _transcripts_fingerprint()
    while not stop_event.wait(poll):
        try:
            fp = _transcripts_fingerprint()
            if fp == last_fp:
                continue            # nothing changed — skip the harvest entirely
            last_fp = fp
            res = run_catch_up()
            if res.get("total_new"):
                print(f"MIND HIVE auto-harvest: +{res['total_new']} events "
                      f"across {len(res['projects'])} project(s)", flush=True)
        except Exception:
            pass   # never let the healer disturb the server


def stable_url(port: int | None = None, token: str | None = None) -> str:
    """The dashboard's stable URL for the given (or default) port and token.
    Kept in one place so the launcher, the server, and any caller all report the
    identical http://127.0.0.1:<port>/?token=<tok> that a browser will open."""
    port = DEFAULT_PORT if port is None else port
    token = token or load_or_create_token()
    return f"http://127.0.0.1:{port}/?token={token}"


def _instance_running(port: int | None, host: str = "127.0.0.1",
                      timeout: float = 0.3) -> bool:
    """True when a dashboard is already listening on host:port.

    Deliberately a client *connect* probe, not a bind-test: on Windows
    SO_REUSEADDR lets a second bind to an in-use port SUCCEED (hijack
    semantics), so a bind-based "is it free?" check wrongly reports free and a
    duplicate pid gets spawned. Connecting is unambiguous — it succeeds only
    when something is actually accepting there. Port 0 (ephemeral) is always a
    fresh server, so it is never "already running"."""
    if not port:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def launch_background(port: int | None = None) -> dict:
    """Spawn `hive dashboard` as a *detached* background process so a caller
    (e.g. the autonomous loop) can bring the panel up without blocking on
    serve()'s serve_forever(). The child outlives this process and — thanks to
    serve()'s fixed-port reuse — a second launch just reopens the same stable URL
    instead of erroring.

    Best-effort by contract: any spawn failure is swallowed and reported in the
    return value rather than raised, so a dashboard problem can never abort the
    caller. Returns {"launched": True, "url": ..., "pid": ...} on success or
    {"launched": False, "reason": ...} when the spawn could not start.
    """
    url = stable_url(port)
    # Single-instance guard: never spawn a second pid when one is already
    # serving. The loop calls this on every run, so without the check repeated
    # runs would pile up detached dashboards.
    if _instance_running(DEFAULT_PORT if port is None else port):
        return {"launched": False, "already_running": True, "url": url,
                "reason": "already running"}
    argv = [sys.executable, "-m", "mindhive", "dashboard"]
    if port is not None:
        argv += ["--port", str(port)]
    # Detach so the child survives this process and doesn't hold its console.
    kwargs: dict = {"stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except Exception as e:   # never let a launch failure disturb the caller
        return {"launched": False, "reason": str(e)}
    return {"launched": True, "url": url, "pid": proc.pid}


def open_panel(port: int | None = None) -> str:
    """Bring the dashboard to the foreground for the `mind hive` shortcut.

    Launch it detached if nothing is running; if one is already up, open its
    stable URL in the browser so the user lands straight on the live panel —
    seamless, and never blocking the terminal. Returns a one-line status for the
    shell to print. On a fresh launch the detached child opens its own browser,
    so we don't double-open here."""
    res = launch_background(port)
    url = res.get("url")
    if res.get("launched"):
        return f"MIND HIVE dashboard: {url}"
    if res.get("already_running"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return f"MIND HIVE dashboard already running — opening {url}"
    return f"dashboard: not launched ({res.get('reason')})"


def serve(port: int | None = None, open_browser: bool = True,
          token: str | None = None) -> None:
    """Start the dashboard on a fixed port with a persisted token so the URL is
    stable across launches, open the browser, and serve until interrupted.

    ``port=None`` uses the fixed DEFAULT_PORT; pass ``port=0`` for an ephemeral
    port. If the fixed port is already bound (the dashboard is likely already
    running), this reopens that same stable URL and returns instead of crashing.

    A background loop also auto-harvests sessions the live hooks miss, so the
    store self-heals even where hooks never fire (disable: MINDHIVE_AUTOHARVEST=0).
    """
    if port is None:
        port = DEFAULT_PORT
    token = token or load_or_create_token()
    # Single-instance guard (primary): if a dashboard is already serving this
    # fixed port, reopen its URL instead of starting a second pid. A connect
    # probe is reliable on Windows, where SO_REUSEADDR would otherwise let the
    # bind below succeed and leave a duplicate server. The OSError path stays as
    # a fallback for the rare probe-then-bind race. port=0 skips the probe.
    if _instance_running(port):
        url = f"http://127.0.0.1:{port}/?token={token}"
        print(f"MIND HIVE dashboard already running — opening {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    handler = make_handler(token)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as e:
        url = f"http://127.0.0.1:{port}/?token={token}"
        print(f"MIND HIVE dashboard could not bind port {port}: {e}", flush=True)
        print(f"It may already be running — opening {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    actual = server.server_address[1]
    url = f"http://127.0.0.1:{actual}/?token={token}"
    print(f"MIND HIVE dashboard: {url}", flush=True)
    print("Close this window or press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    # The boot seal (T14). Jobs run on daemon threads, so anything still running
    # when this process ended died with it and left a `job_started` row with no
    # close — which, unsealed, reads as still-running forever. Close them now,
    # while nothing of this process's own is in flight yet.
    #
    # Best-effort: an unreadable ledger must not stop the dashboard from serving,
    # and this runs BEFORE serve_forever precisely so the seal cannot race a job
    # the user starts. Read `jobs`' module docstring for the single-writer
    # assumption; the scope is one namespace for the same reason.
    try:
        sealed = jobs.seal_orphans(default_project())
        if sealed:
            print(f"sealed {sealed} job(s) abandoned by a previous run",
                  flush=True)
    except Exception as e:                      # noqa: BLE001 — see comment
        print(f"job seal skipped: {type(e).__name__}: {e}", flush=True)
    stop_event = threading.Event()
    threading.Thread(target=_auto_harvest_loop, args=(stop_event,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
