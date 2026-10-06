# SPDX-License-Identifier: Apache-2.0
# src/mindhive/dashboard_data.py
"""The dashboard's data layer: what a project IS, and the folds the panel reads.

Extracted from ``dashboard.py`` when that file reached 2043 lines. Pure functions
over the event store plus the constants that name the HIVE MIND selection — no
HTTP, no locks, no request state, so every one of these is testable without
standing a server up.

Three concerns, in the order they appear:

* **project / session / memory separation** — the distinction the whole panel is
  organised around. ``_project_stats`` is the memoized face of
  ``_project_stats_uncached``; the gate itself lives in ``dashboard_memo``, which
  is why that is the one sibling this module imports.
* **the Improvements efficiency strip** — failure rate per window, with the
  dispositions that keep a thin sample from rendering as a confident percent.
* **HIVE MIND** — the ``project="*"`` unified read-only lens across namespaces.
"""
import collections
import time

from . import chain, events, paths
from .dashboard_memo import _STATS_MEMO, _fingerprinted, _ledger_fingerprint

# --- project / session / memory separation -------------------------------
# A "project" is a store namespace; a "session" is one append stream
# (session_id) within it; "memory" is the semantic subset of events (decisions,
# caveats, …) as opposed to deterministic session telemetry. Keeping the three
# distinct is what makes intent and context legible at a glance.

def _folder_for_project(proj: str) -> str:
    """The project's own folder (where its native MEMORY.md belongs), taken from
    the autosync registry. Falls back to the main repository of the server's
    cwd (`context._memory_folder`, a linked worktree's included) only when the
    project isn't registered — so picking a project in the UI syncs THAT
    project's folder, not wherever the dashboard happens to be launched from,
    and never a worktree, which Claude keys by its main repository."""
    from . import autosync, context
    for e in autosync.load_registry():
        if e.get("project") == proj and e.get("folder"):
            return e["folder"]
    return context._memory_folder()


def _all_projects() -> list:
    """Names of every store namespace that has an events log -- canonical names
    only (`paths.event_namespaces`), so no entry reads a different directory
    from the one it names."""
    return paths.event_namespaces()


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


def _tool_action_rows(evs) -> list:
    """`(ts, type)` for the events the efficiency windows count, and nothing else.

    The whole ledger reduced to the part `_add_tallies_rows` reads. It exists so
    a caller can CACHE the reduction: the windows are cut against `now`, so the
    tallies themselves must be recomputed every request, but the events they are
    cut from do not change between requests. On the real store this turns 44951
    Event objects into a few thousand two-tuples of a timestamp string and an
    interned type name — small enough to keep, and clock-free, which is the
    property that makes keeping it safe.

    A null `ts` is dropped here rather than guarded downstream, exactly as
    `signal_stats` drops it: a corrupt segment must not land in any window."""
    return [(ev.ts, ev.type) for ev in evs
            if ev.type in _TOOL_ACTION_TYPES and ev.ts]


def _add_tallies_rows(tallies: dict, rows, now: float) -> None:
    """Accumulate `(ts, type)` rows into the window tallies at *now*."""
    cuts = {key: (_iso_utc(now - span), _iso_utc(now - 2 * span))
            for key, span in _EFF_WINDOWS}
    for ts, etype in rows:
        for key, _ in _EFF_WINDOWS:
            cur_cut, prev_cut = cuts[key]
            if ts >= cur_cut:
                i = 0
            elif ts >= prev_cut:
                i = 2
            else:
                continue
            t = tallies[key]
            t[i + 1] += 1
            if etype == "tool_failed":
                t[i] += 1


def _add_tallies(tallies: dict, evs, now: float) -> None:
    """Accumulate one project's tool-action counts into the window tallies.
    Called once per project; HIVE MIND sums across projects before rating.

    Kept as the events-shaped door onto `_add_tallies_rows` — it is this
    module's long-standing public surface and four tests call it directly."""
    _add_tallies_rows(tallies, _tool_action_rows(evs), now)


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


def _safe_chain_ok(name: str) -> bool:
    # No pre-read event list to thread in: the chain checkpoint is defined by
    # segment and line, which `read_all` discards, so `chain.verify` scans the
    # segments itself. Measured on the live store that is not a regression
    # (4.32s against the old read_all+fold path's 4.76s), and this value is
    # memoized behind `_overview_fingerprint`'s chain.json stat regardless.
    try:
        return bool(chain.verify(name).get("ok"))
    except Exception:
        return False


# --- the overview fold, memoized -------------------------------------------
# The endpoint a human waits on after clicking a section, and — unlike the cats
# gate — one whose answer does NOT come from the event log alone. Two things
# therefore differ from `_fingerprinted`, and both are deliberate:
#
# * **The fingerprint is compound.** `chain_ok` is read out of `chain.json`, a
#   file beside the segments that `hive project` rewrites without appending a
#   single event. On a `segment_stats`-only stamp the integrity light freezes at
#   whatever it said on the first poll, for the life of the process, while every
#   count beside it stays correct — the most convincing way to be wrong.
#   `test_a_chain_refresh_invalidates_even_though_no_event_moved` was shown to
#   fail against exactly that version before this line existed.
# * **The bound is its own, and it is 128.** `_MEMO_MAX` is 8, sized for the
#   picker's one-hot-key access pattern. HIVE MIND folds EVERY namespace in a
#   single request — 60 on this machine — so an 8-slot memo evicts 52 of the 60
#   entries that request just wrote and can never hit. That is not a hypothesis:
#   /api/projects was measured at 1329 / 1214 / 2134 ms for three identical
#   passes with 8 entries held after each. Raising the shared constant is the
#   wrong fix (a `_SESSIONS_MEMO` / `_CATS_MEMO` entry holds a full telemetry
#   fold, which is what that cap was chosen for); an overview entry is six
#   integers and a type tally, so it can afford a bound above the namespace
#   count.
_OVERVIEW_MEMO_MAX = 128
_OVERVIEW_MEMO: "collections.OrderedDict" = collections.OrderedDict()


def _overview_memo_get(proj):
    if proj not in _OVERVIEW_MEMO:
        return None
    _OVERVIEW_MEMO.move_to_end(proj)   # a read counts as use, or the hot key ages out
    return _OVERVIEW_MEMO[proj]


def _overview_memo_put(proj, value) -> None:
    _OVERVIEW_MEMO[proj] = value
    _OVERVIEW_MEMO.move_to_end(proj)
    while len(_OVERVIEW_MEMO) > _OVERVIEW_MEMO_MAX:
        _OVERVIEW_MEMO.popitem(last=False)


def _chain_fingerprint(proj):
    """`chain.json`'s stat, or **None when it cannot be trusted** — the same
    contract `_ledger_fingerprint` keeps, for the same reason.

    Absent is a real, stable state and gets a real stamp: `verify` answers
    `no chain.json` for it, and the first `hive project` moves the stamp off
    `(0, 0)`. An `OSError` that is not "missing" is a fault, and a fault must
    never be encoded as a value a later poll can match — hence None, which no
    `==` can hit."""
    try:
        st = (paths.project_dir(proj) / "chain.json").stat()
    except FileNotFoundError:
        return (0, 0)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _overview_fingerprint(proj):
    """The stamp for *proj*'s overview: its ledger AND its chain file.

    None from either half poisons the whole thing rather than being dropped —
    a compound stamp with an untrusted component is an untrusted stamp, and the
    half that is still good would otherwise license a hit on the half that
    is not."""
    ledger = _ledger_fingerprint(proj)
    if ledger is None:
        return None
    chain_stat = _chain_fingerprint(proj)
    if chain_stat is None:
        return None
    return (ledger, chain_stat)


def injection_status() -> dict | None:
    """What the per-prompt SOP injection is delivering, or **None when it is
    healthy** — the panel's half of the same verdict `sopgate.notice` writes into
    STATE.md and the MEMORY.md digest.

    Three properties, each deliberate:

    * **Silent when healthy.** `verbatim` returns None, so the strip appears only
      for `silent` / `pointers_only` / `partial`. A banner in every healthy panel
      is a banner every reader learns to skip, which is how the degraded case
      gets missed. `sopgate.NOTICE_VERDICTS` is the shared list — this does not
      re-enumerate the states.
    * **Not memoized, and computed OUTSIDE `overview_stats`.** The verdict is a
      fact about the SOP sidecar and `PROMPT_BUDGET`, not about the ledger, so it
      does not move when `segment_stats` moves and does not stay put when they
      stay put. Inside the overview memo it would freeze at whatever it said when
      the ledger last changed — a stale answer on the surface whose entire job is
      to report staleness.
    * **The verdict is GLOBAL.** `probe` reads the global mandatory-SOP sidecar
      and ignores its project argument, because the hook is registered
      machine-wide. The caller renders it beside per-namespace numbers, so the
      response says so and the panel prints it.

    `probe` documents that it never raises; the guard is here anyway, because the
    cost of being wrong about that is a 500 on the overview rather than one
    missing line."""
    from . import sopgate
    try:
        measured = sopgate.probe()
        if str(measured.get("verdict") or "") not in sopgate.NOTICE_VERDICTS:
            return None
        return {
            "verdict": str(measured.get("verdict")),
            "sentence": sopgate.verdict_sentence(measured),
            "scope": "global",
            "pointers": [str(row.get("sop_id")) for row in measured.get("sops") or []
                         if isinstance(row, dict) and row.get("delivered") == "pointer"],
        }
    except Exception:                                       # noqa: BLE001
        return None


def _overview_uncached(proj: str) -> dict:
    """One read of *proj*'s log, folded into the overview counts, with the chain
    verdict derived from the same events — no second full read."""
    evs = events.read_all(proj)
    stats = _overview_counts((ev.session_id, ev) for ev in evs)
    # `_safe_chain_ok` rather than a bare `chain.verify(...)["ok"]`: this value
    # is now stored, and an exception escaping into a memoized path would take
    # the whole overview down instead of one field's worth of it.
    stats["chain_ok"] = _safe_chain_ok(proj)
    return stats


def overview_stats(proj: str) -> dict:
    """*proj*'s overview counts plus its chain verdict, recomputed only when the
    ledger or the chain file moved. The single-project route returns this
    directly; HIVE MIND sums one of these per namespace.

    **Treat the result as read-only.** On a hit this IS the cached dict, and its
    `by_type` is the cached dict's own — mutating either would rewrite history
    for every later poll. Both current callers only read it: the single-project
    route spreads it into a fresh response, and the am branch accumulates into a
    new tally rather than adding into the first project's."""
    fingerprint = _overview_fingerprint(proj)
    if fingerprint is None:
        return _overview_uncached(proj)
    cached = _overview_memo_get(proj)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    value = _overview_uncached(proj)
    _overview_memo_put(proj, (fingerprint, value))
    return value
