# src/mindhive/loop/state.py
from .. import events


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
