# src/mindhive/dashboard_memo.py
"""The dashboard's caching layer: the bounded LRU memos, the stat-only ledger
fingerprint, and the gate built on it.

Extracted from ``dashboard.py`` when that file reached 2043 lines. The bottom of
the dashboard's own dependency graph — it imports nothing from its siblings, so
``dashboard_data`` can build ``_project_stats`` on ``_fingerprinted`` without a
cycle.

``_with_online`` is the rule that makes caching this endpoint safe at all: the
FOLD is memoized, never the response, because `online` is a function of the wall
clock rather than of the events. Every layer here stores the fold and re-derives
that field on the way out.

Each memo carries its OWN bound (``_BoundedMemo.max_entries``) because one
shared cap could not fit both access patterns here: the two fold-holding memos
want a small cap for entries that are individually huge, while the picker's
``_STATS_MEMO`` is scanned end-to-end every poll and a cap under the namespace
count drove its hit rate to exactly zero.

``dashboard`` re-exports the memos and the LRU helpers: the dashboard tests empty
every ``*_MEMO`` between cases by scanning ``vars(dashboard)``, so they have to
be reachable there, and a re-export is the same OrderedDict object —
``_BoundedMemo`` subclasses ``OrderedDict``, so that scan still finds them."""
import collections
import os

# The FOLD-HOLDING memos (`_SESSIONS_MEMO`, `_CATS_MEMO`) are process-global and
# live for the life of the server, so they are bounded rather than left to grow
# with the number of namespaces ever polled. `hive projects` reports 60 stores on
# this machine, and HIVE MIND mode plus a project picker makes visiting many of
# them a normal afternoon; each entry holds a full telemetry fold and every
# session record for that project. THIS is the size the cap was chosen for.
#
# An LRU rather than a size-only cap because their access pattern is extreme: one
# namespace is hot (the picker's current selection) and the rest are cold the
# moment it changes. Eviction costs a single recompute of the endpoint the gate
# was added to make cheap — the same price a first visit pays.
_MEMO_MAX = 8

# `_STATS_MEMO` is a different animal and gets its own bound. Its entries are the
# picker's small count dict plus the stat fingerprint, and — decisively — its
# access pattern is the OPPOSITE of the one above: nothing is hot, because both
# callers (`_get_projects` and `default_project`) walk EVERY name that
# `_all_projects()` returns, in sorted order, on every poll. A sequential scan
# longer than the bound is the one pattern an LRU degrades to zero on: one pass
# writes an entry per namespace and evicts all but the last few, and the next
# pass starts again at the name evicted earliest, so every single key misses.
# Measured on the real store (60 namespaces, 8 slots), three identical passes
# cost 1329 / 1214 / 2134 ms with 8 entries retained after each, while one hot
# namespace read twice went 1207 -> 4.0 ms. The gate worked; the shared bound
# made it unreachable. The same three passes with the bound below: 1002 / 9.3 /
# 8.4 ms, 60 entries retained.
#
# 512 is a backstop, not the working size. The keys can only ever be namespaces
# on disk — no other caller writes this memo — so the live entry count IS the
# namespace count, and the cap exists for a pathological store rather than for
# this one. Measured cost of a FULLY populated memo over this machine's real
# store (60 namespaces, 1788 segment files): 443.5 KiB total, 7.6 KiB mean per
# entry. The size tracks segment FILES rather than entries — the fingerprint is
# one `[size, mtime_ns]` pair per segment, so two projects hold 87% of it
# (mindhive 197 KiB / 888 segments, hanif 155 KiB / 667) and the 57 others cost
# 1.2-10 KiB each. A store eight times this one still fits in single-digit MiB.
_STATS_MEMO_MAX = 512


class _BoundedMemo(collections.OrderedDict):
    """An LRU memo carrying its OWN bound.

    The bound travels with the memo rather than arriving as an argument because
    `_memo_put` has seven call sites across three modules (two here, one in
    `dashboard_get`, four in `tests/test_dashboard_locks.py`), and a `bound=`
    parameter makes every one of them responsible for remembering which memo it
    is writing to. One that forgets restores the exact defect this class was
    added to fix — silently, since a memo that evicts too eagerly still returns
    correct answers, just never a cheap one."""

    # `max_entries` is deliberately REQUIRED, which means `copy()` raises
    # `TypeError` — `OrderedDict.copy` calls `self.__class__(self)` and passes
    # no bound. That is the better failure. Giving it a default would make
    # `copy()` succeed and hand back a memo silently carrying the SMALL bound,
    # which is the under-caching bug this class exists to fix, reintroduced
    # through the back door. Nothing copies these memos today; if something
    # ever needs to, override `copy`/`__reduce__` to carry the bound across
    # rather than adding a default here.
    def __init__(self, max_entries: int):
        super().__init__()
        self.max_entries = max_entries


def _memo_put(memo: "collections.OrderedDict", key, value) -> None:
    # A plain OrderedDict (a test's scratch memo) falls back to the small shared
    # bound: the conservative direction, since over-caching is the failure that
    # costs memory rather than time.
    bound = getattr(memo, "max_entries", _MEMO_MAX)
    memo[key] = value
    memo.move_to_end(key)
    while len(memo) > bound:
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
# EVERY namespace on disk — 60 on this machine — at launch and on every project
# list, and each one was a full `read_all`. Bounded for the namespace count, not
# at `_MEMO_MAX`: see `_STATS_MEMO_MAX` above for why a shared bound cost this
# memo its entire hit rate.
# {project: (fingerprint, stats dict)}
_STATS_MEMO = _BoundedMemo(_STATS_MEMO_MAX)


# One memo of the session fold, shared by every poll of /api/mindnet/cats.
# {(project, all_mode): (fingerprint, records)}
_SESSIONS_MEMO = _BoundedMemo(_MEMO_MAX)

# The outer gate for the same endpoint, keyed on the stat-only ledger
# fingerprint so an unchanged ledger is never even READ.
# {project: (segment_stats, telemetry fold, session records)}
_CATS_MEMO = _BoundedMemo(_MEMO_MAX)


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
