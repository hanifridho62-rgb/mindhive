# src/mindhive/loop/state.py
import time
from datetime import datetime
from pathlib import Path

from .. import events
from . import task as task_mod

# A run is "online" while its last event is fresher than one iteration timeout
# (config default 15 min) plus slack — a legal iteration can't exceed it.
HEARTBEAT_SLACK_SECONDS = 60.0
DEFAULT_HEARTBEAT_SECONDS = 15 * 60 + HEARTBEAT_SLACK_SECONDS

_LOOP_TYPES = {"loop_run_start", "loop_iteration", "loop_run_end",
               "loop_run_dismissed"}


def last_run_status(project=None):
    """Summarize the most recent loop run from the event ledger, or None."""
    evs = events.read_all(project)
    starts = [e for e in evs if e.type == "loop_run_start"]
    if not starts:
        return None
    last_start_ts = starts[-1].ts
    iters, ends = [], []
    for e in evs:
        if e.ts < last_start_ts:
            continue
        if e.type == "loop_iteration":
            iters.append(e)
        elif e.type == "loop_run_end":
            ends.append(e)
    end = ends[-1] if ends else None
    return {
        "started": last_start_ts,
        "reason": (end.payload.get("reason") if end else "running"),
        "iterations": (end.payload.get("iterations") if end else len(iters)),
        "total_cost_usd": sum(e.payload.get("cost_usd", 0.0) for e in iters),
        "total_tokens": sum(e.payload.get("total_tokens", 0) for e in iters),
    }


def _ts_epoch(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


_FIX_PLAN_MAX_BYTES = 256 * 1024


def _fix_plan_items(goal_dir: str) -> dict:
    """Best-effort snapshot of the run's fix_plan checklist; the goal dir may
    live outside this machine's reach, so any read failure degrades to empty.
    goal_dir comes from an event payload (untrusted): only a real `.hive-loop`
    directory is read, and oversized files are skipped so a hostile or bloated
    path can't stall the single-threaded dashboard."""
    empty = {"done": [], "planned": [], "not_yet": []}
    if not goal_dir:
        return empty
    try:
        d = Path(goal_dir)
        if d.name != ".hive-loop":
            return empty
        fp = d / "fix_plan.md"
        if not fp.is_file() or fp.stat().st_size > _FIX_PLAN_MAX_BYTES:
            return empty
        parsed = task_mod.plan_items(fp.read_text(encoding="utf-8"))
        # Records, not `PlanItem`s: this dict goes straight onto the Oversee wire
        # as JSON, and a dataclass cannot cross that boundary. The marker rides
        # along as `new` so the card can show where an item came from.
        return {group: [it.as_record() for it in items]
                for group, items in parsed.items()}
    except OSError:
        return empty


def _injected_counts(evs) -> dict:
    """`run_id -> how many pieces of work the injector LANDED in that run`.

    Landed only. A refusal or a dry round is a decision the injector recorded, not
    work it created, and counting those would tell the operator the loop grew its
    plan when it had in fact declined to. Total, like every fold that feeds a
    dashboard projection: a malformed row is skipped, never raised."""
    counts = {}
    for ev in evs or []:
        if getattr(ev, "type", None) != "work_injected":
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        if payload.get("outcome") != "landed":
            continue
        run_id = payload.get("run_id") or payload.get("plan_id")
        if isinstance(run_id, str) and run_id:
            counts[run_id] = counts.get(run_id, 0) + 1
    return counts


def list_runs(project=None, *, now_fn=time.time,
              heartbeat_seconds=DEFAULT_HEARTBEAT_SECONDS,
              evs=None) -> dict:
    """Project loop events into per-run Oversee records, newest first.

    Dismissed runs (graceful exit, breaker trip, manual delete) are excluded —
    only their count is reported. Events written before run_id stamping group
    into synthetic per-start legacy buckets, so old ledgers still render."""
    # One read, split two ways. The injected counts come off `work_injected`
    # rows, which are not loop events, and a second `read_all` for them would
    # double the cost of the single-threaded dashboard's hottest projection.
    #
    # `evs` lets a caller that has ALREADY read the ledger hand it over, spelled
    # the same way as `oversee.interactive_sessions`'s parameter because
    # /api/oversee calls both on one read. Without it that endpoint read the
    # same ledger TWICE per namespace — 365 ms a time on the real store — to
    # fold two views of the same events.
    all_evs = evs if evs is not None else events.read_all(project)
    evs = [e for e in all_evs if e.type in _LOOP_TYPES]
    injected = _injected_counts(all_evs)
    runs, order, dismissed = {}, [], {}
    legacy = None

    def bucket(e):
        nonlocal legacy
        rid = (e.payload or {}).get("run_id")
        if rid:
            return rid
        if e.type == "loop_run_start" or legacy is None:
            legacy = f"legacy-{e.ts}"
        return legacy

    for e in evs:
        p = e.payload or {}
        if e.type == "loop_run_dismissed":
            rid = p.get("run_id")
            if rid:
                dismissed[rid] = e.ts
            continue
        rid = bucket(e)
        r = runs.get(rid)
        if r is None:
            r = runs[rid] = {
                "run_id": rid, "kind": "loop",
                "title": "", "goal_dir": "", "started": e.ts,
                "status": "running", "iterations": 0, "total_cost_usd": 0.0,
                "total_tokens": 0, "last_event_ts": e.ts, "summaries": [],
                "heartbeat_seconds": None,
            }
            order.append(rid)
        r["last_event_ts"] = e.ts
        if e.type == "loop_run_start":
            r["started"] = e.ts
            r["goal_dir"] = str(p.get("goal_dir") or "")
            r["title"] = str(p.get("title") or "").strip()
            hb = p.get("heartbeat_seconds")
            r["heartbeat_seconds"] = float(hb) if hb else None
        elif e.type == "loop_iteration":
            r["iterations"] += 1
            r["total_cost_usd"] += p.get("cost_usd", 0.0) or 0.0
            r["total_tokens"] += p.get("total_tokens", 0) or 0
            s = str(p.get("summary") or "").strip()
            if s:
                r["summaries"].append((p.get("iteration", r["iterations"]), s))
        elif e.type == "loop_run_end":
            r["status"] = str(p.get("reason") or "ended")

    now = now_fn()
    out = []
    excluded = 0
    for rid in order:
        r = runs[rid]
        # A dismissal hides a run only while it stays the run's LAST word: a
        # still-live run that keeps appending events after a (mistaken) manual
        # delete resurfaces instead of silently burning money out of sight.
        if rid in dismissed and dismissed[rid] >= r["last_event_ts"]:
            excluded += 1
            continue
        running = r["status"] == "running"
        last = _ts_epoch(r["last_event_ts"])
        window = r.get("heartbeat_seconds") or heartbeat_seconds
        r["online"] = bool(running and last is not None
                           and (now - last) < window)
        summaries = r.pop("summaries")
        if summaries:
            r["title"] = summaries[-1][1]
        items = _fix_plan_items(r["goal_dir"])
        r["doing"] = summaries[-1][1] if (running and summaries) else None
        past = summaries[:-1] if running else summaries
        # Every group is a list of records with the SAME keys, including the
        # iteration summaries. A mixed list — bare strings beside records — is
        # what would break the Oversee renderer, and it would break it silently
        # on runs that happened to have both.
        r["done"] = ([{"text": f"iter {i}: {s}", "new": False, "status": "done"}
                      for i, s in past] + items["done"])
        r["planned"] = items["planned"]
        r["not_yet"] = items["not_yet"]
        r["injected_count"] = injected.get(r["run_id"], 0)
        out.append(r)
    out.reverse()
    return {"runs": out, "dismissed": excluded}
