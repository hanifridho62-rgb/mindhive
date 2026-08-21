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
import hashlib
import json
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

# `_OVERSEE_MEMO` gets the third bound, and for `_STATS_MEMO`'s reason rather
# than `_MEMO_MAX`'s: HIVE MIND folds EVERY namespace in one request, so a cap
# under the namespace count would evict most of what that single request just
# wrote and could never hit. 128 over this machine's 60 is the same headroom
# `_OVERVIEW_MEMO_MAX` was given for the same access pattern.
#
# Affordable because an oversee entry is bounded by the BOARD, not by the fold:
# it holds the live cards and the roadmap plans, not the events they came from.
# Measured on the real store — mindhive, 40554 events, 6 cards, 2 plans: 6.9
# KiB; hanif, 8490 events, 0 cards, 2 plans: 1.2 KiB. A full 128 slots of the
# busiest namespace still fits under a megabyte.
_OVERSEE_MEMO_MAX = 128


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
    * **A `pg` backend has no fingerprint at all.** `events.read_all`
      short-circuits to Postgres when `MINDHIVE_EVENT_BACKEND` is exactly `pg`,
      while `segment_stats` keeps stat'ing the local `.jsonl` segments — which,
      on the documented file → dual → pg migration path, are still on disk and
      never change again. The fingerprint would be non-empty and stable forever:
      every poll a hit, the view frozen at the first response, nothing reporting
      it.

      **`dual` is NOT such a backend, and reading it as one was a defect.** This
      guard said `!= "file"` and this paragraph said "`pg` or `dual`"; the
      reader (`events._maybe_read_from_pg`) says `!= "pg"`. Under `dual` the
      fold still comes off the local segments, so the stamp is exactly as
      trustworthy as under `file` — and disabling the memo there cost a measured
      1.6 ms → 751 ms on every poll, on the FIRST step of the migration path,
      for no correctness at all. The comparison below is now spelled the same
      way as the reader's, `.lower()` and all, so the two cannot disagree about
      a value like `"pg "` either.

    Returning None for both, rather than a falsy-but-comparable value, is what
    makes the guards impossible to half-apply: there is no value here that a `==`
    can match, so an untrusted fingerprint cannot become a hit by accident."""
    if os.environ.get("MINDHIVE_EVENT_BACKEND", "file").lower() == "pg":
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

# The gate for /api/oversee. Holds the whole BOARD fold — the cards, their
# attached roadmaps, the orphan-plan candidates, and the goal_dirs the entry was
# built from (which have to join its own fingerprint; see
# `_fix_plan_fingerprint`). Every clock-dependent field is re-derived by
# `_with_liveness` on the way out.
# {project: (fingerprint, fold stamp, board)}
_OVERSEE_MEMO = _BoundedMemo(_OVERSEE_MEMO_MAX)


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


def _with_liveness(records, stamp):
    """*records* with liveness re-derived at *stamp*, dispatching on KIND.

    The seam that makes gating /api/oversee safe, and the reason `_with_online`
    could not simply be reused: the wall clock decides THREE things on that
    board and they are not the same rule.

    * A **loop run** is online while its status is still `running` and its last
      event is inside ITS OWN `heartbeat_seconds` — default 15 min + 60 s.
      Applying the chat window here would call a perfectly live run offline
      after 180 seconds, twelve times too early, which is worse than no
      liveness at all: it reports running work as dead.
    * A **chat session** is online inside `ONLINE_WINDOW_SECONDS` (180 s).
    * A chat session idle past `ACTIVE_WINDOW_SECONDS` (6 h) is not offline, it
      is GONE — `oversee.interactive_sessions` refuses to emit it. That makes
      the third decision a drop rather than a field, and it is also what
      justifies caching the fold at all: ageing can only ever REMOVE a card,
      never add one, so a fold taken at T0 and filtered at T1 >= T0 is exactly
      what a fresh fold at T1 would have produced. `_oversee_cached` enforces
      the T1 >= T0 half.

    Both rules are the producers' own lines applied to the producers' own
    constants, read off fields the rows already carry — not a second
    implementation of liveness, which would be free to drift from them.

    An unrecognised `kind` gets the chat rule. Of the two guesses that is the
    one that cannot report a dead session as working, which is the failure that
    matters on a board someone watches to decide whether to intervene.

    Idempotent by construction — `dict(r, online=…)` replaces the key rather
    than reading it, and never mutates the cached row."""
    from . import oversee as oversee_mod
    from .loop.state import DEFAULT_HEARTBEAT_SECONDS, _ts_epoch
    out = []
    for r in records:
        last = _ts_epoch(r.get("last_event_ts"))
        if r.get("kind") == "loop":
            window = r.get("heartbeat_seconds") or DEFAULT_HEARTBEAT_SECONDS
            online = bool(r.get("status") == "running" and last is not None
                          and (stamp - last) < window)
        else:
            if (last is None
                    or (stamp - last) >= oversee_mod.ACTIVE_WINDOW_SECONDS):
                continue
            online = (stamp - last) < oversee_mod.ONLINE_WINDOW_SECONDS
        out.append(dict(r, online=online))
    return out


def _fix_plan_fingerprint(goal_dirs):
    """A stat-only stamp over each run's ``fix_plan.md``, or None for a fault.

    The second term of /api/oversee's fingerprint, and the same shape as the
    `chain.json` term in `_overview_fingerprint`: `loop.state._fix_plan_items`
    reads this file off the run's `goal_dir`, so a run's checklist can change
    with NO event appended and a ledger-only stamp does not move for it. Cached
    without this term, a run's plan would be frozen at whatever it said on the
    first poll for the life of the process.

    A missing file gets a real stamp (`-1`) rather than being skipped, so
    creating one counts as a change. Any OTHER `OSError` returns None — the same
    rule `_ledger_fingerprint` applies to `{}`, because a fault that compares
    equal to itself is how a transient error pins a cache open for good.

    `ValueError` is in that same arm because `os.stat` raises it, NOT an OSError,
    for a path with an embedded NUL — before the call reaches the OS. `goal_dir`
    comes off an event payload and the reader this mirrors treats it as untrusted
    and survives it (`pathlib`'s `is_file()` swallows the ValueError, so the card
    renders with an empty checklist). Letting it escape here 500s the whole
    Oversee board on every poll, permanently, since an append-only ledger cannot
    un-write the row.

    Every named `goal_dir` is stat'ed, including ones `_fix_plan_items` would
    refuse to read (it requires a real `.hive-loop` directory). That
    over-invalidates in the safe direction and keeps this function from having
    to re-implement, and then drift from, the reader's admission rule."""
    out = []
    for d in goal_dirs or ():
        try:
            st = os.stat(os.path.join(d, "fix_plan.md"))
        except FileNotFoundError:
            out.append((d, -1, 0))
        except (OSError, ValueError):
            return None
        else:
            out.append((d, st.st_size, st.st_mtime_ns))
    return tuple(out)


def _intended_use_digest(iu) -> str:
    """A content digest of `oversee.intended_use_map()`, the third term.

    That map comes off the plugin inventory on disk, is TTL-memoized for 60 s,
    and feeds `skill_trails` INSIDE the fold — so a board cached against a
    ledger stamp can outlive the map it was built from and report a skill's
    "why" from an inventory that no longer says it.

    A CONTENT digest, not the object's identity: the TTL rebuilds an equal dict
    every minute, and keying on identity would throw the whole memo away on a
    schedule for no change at all. Computed once per request by the caller and
    never once per namespace — HIVE MIND would otherwise hash the same 347
    entries sixty times over."""
    blob = json.dumps(iu or {}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _oversee_fingerprint(proj, goal_dirs, iu_digest):
    """/api/oversee's compound fingerprint: the ledger, the fix-plan files of
    the runs on the board, and the intended-use map.

    None from ANY term poisons the whole, exactly as in `_overview_fingerprint`:
    a compound stamp with one untrustworthy half is not a weaker stamp, it is a
    cache key that can compare equal after the world has moved."""
    ledger = _ledger_fingerprint(proj)
    if ledger is None:
        return None
    fixes = _fix_plan_fingerprint(goal_dirs)
    if fixes is None:
        return None
    return (ledger, fixes, iu_digest)


def _oversee_cached(proj, iu_digest, stamp, compute):
    """*compute*'s board for *proj*, recomputed only when something it depends
    on actually moved.

    The `goal_dirs` in the fingerprint come from the CACHED entry, which is not
    circular: `goal_dir` is read off an event, so a board whose runs changed had
    its ledger term move first.

    `folded_at <= stamp` is what keeps `_with_liveness`'s drop rule exact.
    Ageing only ever removes cards, so filtering an older fold at a newer stamp
    is right — but a clock that jumped BACKWARD (an NTP correction) would invert
    that and hide a card that belongs on the board. One comparison retires the
    whole class by treating it as a miss.

    The ledger is stamped BEFORE the fold and the stamp is required to still
    hold after it. Stamping only afterwards described the ledger as of T+delta
    while the board was read at T, so an event that landed inside that window was
    in the KEY and absent from the BOARD — and the next poll matched the key and
    served the older board, until some further append happened to move the stamp
    again. That window is the fold itself: 900 ms on the real store, on the
    dashboard's hottest projection. Nothing cached at all is the correct outcome
    for a fold that raced an append; the next poll simply folds again.

    Treat the result as read-only: it IS the cached object. The caller's
    clock-dependent fields come from `_with_liveness`, which copies."""
    cached = _memo_get(_OVERSEE_MEMO, proj)
    if cached is not None:
        fingerprint, folded_at, board = cached
        if folded_at <= stamp and fingerprint == _oversee_fingerprint(
                proj, board["goal_dirs"], iu_digest):
            return board
    before = _ledger_fingerprint(proj)
    board = compute()
    fingerprint = _oversee_fingerprint(proj, board["goal_dirs"], iu_digest)
    # `before is None` fails this comparison too, which is right: an unstampable
    # ledger was never cacheable.
    if fingerprint is not None and fingerprint[0] == before:
        _memo_put(_OVERSEE_MEMO, proj, (fingerprint, stamp, board))
    return board


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
