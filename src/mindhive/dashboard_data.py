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
import os
import time

from . import chain, events, paths
from .dashboard_memo import _STATS_MEMO, _fingerprinted

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
