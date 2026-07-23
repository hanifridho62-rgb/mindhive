# src/mindhive/workspace_budget.py
"""Workspace cost accounting (WS2): persist-and-gate spend for the multi-agent
fleet (MindParallel / MindSwarm).

``loop/budget.py`` owns the pure budget primitives (rolling window,
``would_exceed``, ``wait_seconds``) and is imported here UNCHANGED. This module
supplies the two things the workspace path lacks:

- an **entry source** — per-run cost is now persisted on the terminal events
  (``assistant_response`` carries it on the done path, ``agent_disconnect`` on
  the non-clean paths); :func:`budget_entries` folds those into the
  ``BudgetEntry`` list the primitives consume.
- a **ceiling** — a per-project ``budget.json`` sidecar the user sets, plus a
  ``budget_set`` audit event, mirroring ``enrich.json`` / ``enrich_toggled``.

The gate is honest about being a *local policy*: there is no account
remaining-quota API (PRD §5.2), so it fails **open** when it has no ceiling or
no prior to estimate from. It refuses only against evidence that a ceiling the
user set would be breached.
"""
from __future__ import annotations

import json
import math
import time

from . import events as ev_mod, paths
from .loop.budget import (BudgetConfig, BudgetEntry, rolling_avg, used,
                          wait_seconds, would_exceed)
from .loop.state import _ts_epoch

DEFAULT_WINDOW_HOURS = 5.0            # mirrors the loop's ~5h usage window
ESTIMATE_SAMPLE = 10                  # rolling_avg over the most recent N runs
_COST_EVENT_TYPES = ("assistant_response", "agent_disconnect")


def _settings_path(project=None):
    return paths.project_dir(project) / "budget.json"


def budget_config(project=None) -> BudgetConfig:
    """The user-set ceiling for a project, or an unlimited default. A
    ``cost_budget_usd`` of 0 means unlimited (loop/budget.py's convention).
    Total read: an absent or corrupt sidecar degrades to the default, never
    raises."""
    window = DEFAULT_WINDOW_HOURS
    cost = 0.0
    try:
        obj = json.loads(_settings_path(project).read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            window = float(obj.get("window_hours") or DEFAULT_WINDOW_HOURS)
            cost = float(obj.get("cost_budget_usd") or 0.0)
    except (OSError, ValueError, TypeError):
        pass
    return BudgetConfig(window_hours=window, token_budget=0, cost_budget_usd=cost)


def set_budget(project=None, cost_budget_usd=0.0,
               window_hours=DEFAULT_WINDOW_HOURS) -> dict:
    """Persist the ceiling to the sidecar and append a ``budget_set`` audit
    event. Rejects negative cost / non-positive window. The sidecar is the
    source of truth; a failed audit append never fails the set."""
    try:
        cost = float(cost_budget_usd)
        window = float(window_hours)
    except (TypeError, ValueError):
        return {"ok": False, "error": "budget values must be numbers"}
    if not (math.isfinite(cost) and math.isfinite(window)):
        # NaN/inf slip past the sign checks below (NaN < 0 and NaN <= 0 are both
        # False) and then serialize to invalid JSON — json.dumps emits a bare
        # ``NaN`` / ``Infinity`` token — poisoning both the sidecar and every
        # downstream rolling-window fold that reads it back.
        return {"ok": False, "error": "budget values must be finite numbers"}
    if cost < 0 or window <= 0:
        return {"ok": False,
                "error": "cost must be non-negative and window positive"}
    data = {"cost_budget_usd": cost, "window_hours": window}
    paths.write_text_atomic(_settings_path(project),
                            json.dumps(data, ensure_ascii=False, indent=1))
    try:
        ev_mod.append_event("budget_set", dict(data),
                            tags=["workspace", "budget"], project=project)
    except Exception:
        pass                          # audit is best-effort; sidecar is truth
    return {"ok": True, **data}


def budget_entries(project=None, evs=None) -> list:
    """Fold the fleet's per-run cost from the ledger into ``BudgetEntry`` rows.
    Reads workspace-tagged ``assistant_response`` + ``agent_disconnect`` events
    carrying a numeric ``cost``. Total read: rows with a missing / non-numeric
    cost or an unparseable ts are skipped, never raised. ``tokens`` is 0 — no
    token signal exists for workspace runs (PRD §KTD6)."""
    if evs is None:
        evs = ev_mod.read_all(project)
    out = []
    for e in evs or []:
        if getattr(e, "type", None) not in _COST_EVENT_TYPES:
            continue
        if not ev_mod.is_workspace_tagged(e):     # canonical shared predicate
            continue
        p = e.payload if isinstance(getattr(e, "payload", None), dict) else {}
        cost = p.get("cost")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            continue                  # None / "lots" / True are not a cost
        if not math.isfinite(cost):
            continue                  # NaN/inf would poison the rolling sums
        ts = _ts_epoch(getattr(e, "ts", "") or "")
        if ts is None:
            continue
        out.append(BudgetEntry(ts=ts, tokens=0, cost_usd=float(cost)))
    return out


def gate(project=None, *, n=1, entries=None, config=None, now=None,
         sample=ESTIMATE_SAMPLE) -> dict:
    """Pre-flight budget check for a spawn. Estimates the next run's cost from a
    ``rolling_avg`` of recent entries (x ``n`` for an ``n``-agent mission) and
    refuses when adding it would breach a non-zero ceiling. Fails OPEN
    (``ok=True``) when no ceiling is set or there is no history to estimate from
    (PRD §KTD5 / R4). Returns ``{ok, reason?, wait_seconds?, estimate}``."""
    cfg = config if config is not None else budget_config(project)
    if not cfg.cost_budget_usd:            # unlimited -> never gate
        return {"ok": True, "estimate": 0.0}
    ents = entries if entries is not None else budget_entries(project)
    now_ts = now if now is not None else time.time()
    _, per_run = rolling_avg(ents, sample)    # (0, 0.0) when empty -> fail open
    next_cost = per_run * max(1, int(n))
    if would_exceed(ents, now_ts, cfg, 0, next_cost):
        return {"ok": False,
                "reason": (f"workspace spend ceiling ${cfg.cost_budget_usd:.2f} "
                           f"would be exceeded (est ${next_cost:.4f})"),
                "wait_seconds": wait_seconds(ents, now_ts, cfg, 0, next_cost),
                "estimate": next_cost}
    return {"ok": True, "estimate": next_cost}


def snapshot(project=None, evs=None, now=None) -> dict:
    """Current budget posture for the dashboard: the ceiling, spend in the
    rolling window, and remaining headroom (``None`` when unlimited)."""
    cfg = budget_config(project)
    ents = budget_entries(project, evs)
    now_ts = now if now is not None else time.time()
    _, used_c = used(ents, now_ts, cfg.window_hours)
    remaining = (cfg.cost_budget_usd - used_c) if cfg.cost_budget_usd else None
    return {"cost_budget_usd": cfg.cost_budget_usd,
            "window_hours": cfg.window_hours,
            "used_usd": used_c, "remaining_usd": remaining}
