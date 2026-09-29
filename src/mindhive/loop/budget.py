# SPDX-License-Identifier: Apache-2.0
# src/mindhive/loop/budget.py
from dataclasses import dataclass


@dataclass
class BudgetEntry:
    ts: float       # epoch seconds
    tokens: int
    cost_usd: float


@dataclass
class BudgetConfig:
    window_hours: float
    token_budget: int        # 0 = unlimited
    cost_budget_usd: float   # 0 = unlimited


def _in_window(entries, now, window_hours):
    cutoff = now - window_hours * 3600.0
    return [e for e in entries if e.ts > cutoff]


def used(entries, now, window_hours):
    """Return (tokens, cost) summed over entries within the rolling window."""
    win = _in_window(entries, now, window_hours)
    return sum(e.tokens for e in win), sum(e.cost_usd for e in win)


def rolling_avg(entries, n):
    """Mean (tokens, cost) of the most recent n entries; (0,0) if empty."""
    recent = entries[-n:] if n > 0 else []
    if not recent:
        return 0, 0.0
    return (sum(e.tokens for e in recent) // len(recent),
            sum(e.cost_usd for e in recent) / len(recent))


def would_exceed(entries, now, cfg, next_tokens, next_cost):
    """True if adding (next_tokens, next_cost) would breach a non-zero budget."""
    used_t, used_c = used(entries, now, cfg.window_hours)
    if cfg.token_budget and used_t + next_tokens > cfg.token_budget:
        return True
    if cfg.cost_budget_usd and used_c + next_cost > cfg.cost_budget_usd:
        return True
    return False


def wait_seconds(entries, now, cfg, next_tokens, next_cost):
    """Seconds to wait until enough oldest entries age out of the window so the
    next iteration fits. 0.0 if it already fits. Drops entries oldest-first and
    returns the time at which the last entry that must leave exits the window."""
    if not would_exceed(entries, now, cfg, next_tokens, next_cost):
        return 0.0
    win = sorted(_in_window(entries, now, cfg.window_hours), key=lambda e: e.ts)
    used_t = sum(e.tokens for e in win)
    used_c = sum(e.cost_usd for e in win)
    wait = 0.0
    for e in win:
        used_t -= e.tokens
        used_c -= e.cost_usd
        # time when this entry leaves the window
        wait = (e.ts + cfg.window_hours * 3600.0) - now
        fits_t = (not cfg.token_budget) or (used_t + next_tokens <= cfg.token_budget)
        fits_c = (not cfg.cost_budget_usd) or (used_c + next_cost <= cfg.cost_budget_usd)
        if fits_t and fits_c:
            break
    return max(0.0, wait)
