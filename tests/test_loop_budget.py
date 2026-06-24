# tests/test_loop_budget.py
from mindhive.loop import budget

HOUR = 3600.0


def _entries():
    # (ts_epoch, tokens, cost)
    return [budget.BudgetEntry(0.0, 1000, 0.10),
            budget.BudgetEntry(1 * HOUR, 2000, 0.20),
            budget.BudgetEntry(2 * HOUR, 3000, 0.30)]


def test_used_within_window():
    # window 1.5h ending at now=2h includes only the 1h and 2h entries
    t, c = budget.used(_entries(), now=2 * HOUR, window_hours=1.5)
    assert t == 5000
    assert round(c, 2) == 0.50


def test_rolling_avg_last_n():
    t, c = budget.rolling_avg(_entries(), n=2)
    assert t == 2500          # mean of 2000 and 3000
    assert round(c, 2) == 0.25


def test_would_exceed_token_budget():
    cfg = budget.BudgetConfig(window_hours=10, token_budget=6000, cost_budget_usd=0)
    # used over 10h window = 6000; adding 1000 exceeds
    assert budget.would_exceed(_entries(), now=2 * HOUR, cfg=cfg,
                               next_tokens=1000, next_cost=0.0) is True


def test_zero_budget_is_unlimited():
    cfg = budget.BudgetConfig(window_hours=10, token_budget=0, cost_budget_usd=0)
    assert budget.would_exceed(_entries(), now=2 * HOUR, cfg=cfg,
                               next_tokens=10**9, next_cost=10**9) is False


def test_wait_seconds_until_oldest_ages_out():
    cfg = budget.BudgetConfig(window_hours=2, token_budget=6000, cost_budget_usd=0)
    w = budget.wait_seconds(_entries(), now=2 * HOUR, cfg=cfg,
                            next_tokens=1000, next_cost=0.0)
    assert w == 0.0
