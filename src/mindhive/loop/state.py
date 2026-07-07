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


def _fix_plan_items(goal_dir: str) -> dict:
    """Best-effort snapshot of the run's fix_plan checklist; the goal dir may
    live outside this machine's reach, so any read failure degrades to empty."""
    empty = {"done": [], "planned": [], "not_yet": []}
    if not goal_dir:
        return empty
    fp = Path(goal_dir) / "fix_plan.md"
    try:
        if not fp.exists():
            return empty
        return task_mod.plan_items(fp.read_text(encoding="utf-8"))
    except OSError:
        return empty


def list_runs(project=None, *, now_fn=time.time,
              heartbeat_seconds=DEFAULT_HEARTBEAT_SECONDS) -> dict:
    """Project loop events into per-run Oversee records, newest first.

    Dismissed runs (graceful exit, breaker trip, manual delete) are excluded —
    only their count is reported. Events written before run_id stamping group
    into synthetic per-start legacy buckets, so old ledgers still render."""
    evs = [e for e in events.read_all(project) if e.type in _LOOP_TYPES]
    runs, order, dismissed = {}, [], set()
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
                dismissed.add(rid)
            continue
        rid = bucket(e)
        r = runs.get(rid)
        if r is None:
            r = runs[rid] = {
                "run_id": rid, "title": "", "goal_dir": "", "started": e.ts,
                "status": "running", "iterations": 0, "total_cost_usd": 0.0,
                "total_tokens": 0, "last_event_ts": e.ts, "summaries": [],
            }
            order.append(rid)
        r["last_event_ts"] = e.ts
        if e.type == "loop_run_start":
            r["started"] = e.ts
            r["goal_dir"] = str(p.get("goal_dir") or "")
            r["title"] = str(p.get("title") or "").strip()
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
    for rid in order:
        if rid in dismissed:
            continue
        r = runs[rid]
        running = r["status"] == "running"
        last = _ts_epoch(r["last_event_ts"])
        r["online"] = bool(running and last is not None
                           and (now - last) < heartbeat_seconds)
        summaries = r.pop("summaries")
        if summaries:
            r["title"] = summaries[-1][1]
        items = _fix_plan_items(r["goal_dir"])
        r["doing"] = summaries[-1][1] if (running and summaries) else None
        past = summaries[:-1] if running else summaries
        r["done"] = [f"iter {i}: {s}" for i, s in past] + items["done"]
        r["planned"] = items["planned"]
        r["not_yet"] = items["not_yet"]
        out.append(r)
    out.reverse()
    return {"runs": out, "dismissed": len(dismissed)}
