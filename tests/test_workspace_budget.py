# tests/test_workspace_budget.py
"""WS2 cost accounting: the budget.json ceiling sidecar, the ledger-folded
entries source, and the pre-flight gate. loop/budget.py is imported unchanged;
this module supplies its entry source and the user-set ceiling. Enforces
docs/plans/2026-07-22-002-feat-workspace-cost-accounting-plan.md (R1-R5)."""
from mindhive import workspace_budget as wb, events, paths
from mindhive.loop.budget import BudgetConfig, BudgetEntry


def _wscost(etype, cost, minute, sid="ws-c1"):
    """A workspace-tagged terminal event carrying a per-run cost, as WS1a/U1
    write it."""
    events.append_event(etype, {"agent": "A", "cost": cost},
                        session_id=sid, tags=["workspace", "parallel", "agent"],
                        ts=f"2026-07-08T10:{minute:02d}:00Z", project="test")


# --- ceiling sidecar (R2, R5) --------------------------------------------------

def test_budget_config_defaults_to_unlimited_when_absent():
    cfg = wb.budget_config("test")
    assert cfg.cost_budget_usd == 0.0        # 0 == unlimited
    assert cfg.token_budget == 0
    assert cfg.window_hours == wb.DEFAULT_WINDOW_HOURS


def test_budget_config_survives_a_corrupt_sidecar():
    d = paths.project_dir("test")
    d.mkdir(parents=True, exist_ok=True)     # lazily-created store dir
    (d / "budget.json").write_text("{not json", encoding="utf-8")
    cfg = wb.budget_config("test")           # must not raise (R5)
    assert cfg.cost_budget_usd == 0.0


def test_set_budget_roundtrips_and_appends_one_audit_event():
    res = wb.set_budget("test", cost_budget_usd=5.0, window_hours=6.0)
    assert res["ok"]
    cfg = wb.budget_config("test")
    assert cfg.cost_budget_usd == 5.0 and cfg.window_hours == 6.0
    audits = [e for e in events.read_all("test") if e.type == "budget_set"]
    assert len(audits) == 1
    assert audits[0].payload["cost_budget_usd"] == 5.0


def test_set_budget_rejects_negative_and_writes_nothing():
    assert not wb.set_budget("test", cost_budget_usd=-1.0)["ok"]
    assert not (paths.project_dir("test") / "budget.json").exists()
    assert [e for e in events.read_all("test") if e.type == "budget_set"] == []


def test_set_budget_rejects_non_finite_values():
    # NaN/inf slip past the sign checks (NaN < 0 is False) and would serialize to
    # invalid JSON — reject them so they never reach the sidecar or the fold.
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert not wb.set_budget("test", cost_budget_usd=bad)["ok"]
    assert not wb.set_budget("test", cost_budget_usd=5.0,
                             window_hours=float("inf"))["ok"]
    assert not (paths.project_dir("test") / "budget.json").exists()
    assert [e for e in events.read_all("test") if e.type == "budget_set"] == []


# --- entries fold (R1, R5) -----------------------------------------------------

def test_budget_entries_folds_cost_from_both_terminal_events():
    _wscost("assistant_response", 0.10, 1)
    _wscost("agent_disconnect", 0.20, 2)
    ents = wb.budget_entries("test")
    assert sorted(e.cost_usd for e in ents) == [0.10, 0.20]
    assert all(e.tokens == 0 for e in ents)


def test_budget_entries_ignores_non_workspace_and_bad_cost():
    # ordinary chat assistant_response (no workspace tag) — ignored
    events.append_event("assistant_response", {"text": "hi", "cost": 0.9},
                        session_id="sess-plain", ts="2026-07-08T10:00:00Z",
                        project="test")
    _wscost("assistant_response", None, 1)         # no cost — skipped
    events.append_event("assistant_response", {"agent": "A", "cost": "lots"},
                        session_id="ws-c1", tags=["workspace", "parallel", "agent"],
                        ts="2026-07-08T10:02:00Z", project="test")   # non-numeric
    _wscost("agent_disconnect", 0.33, 3)           # the one valid entry
    ents = wb.budget_entries("test")               # must not raise
    assert [e.cost_usd for e in ents] == [0.33]


def test_budget_entries_skips_non_finite_cost():
    # NaN/inf are floats and pass the isinstance check, but poison the rolling
    # sums (NaN makes every comparison False -> gate fails open even over budget;
    # inf makes every check exceed). They must be dropped like a bad cost.
    _wscost("assistant_response", float("nan"), 1)
    _wscost("agent_disconnect", float("inf"), 2)
    _wscost("assistant_response", 0.25, 3)         # the one clean entry
    ents = wb.budget_entries("test")               # must not raise
    assert [e.cost_usd for e in ents] == [0.25]


# --- pre-flight gate (R3, R4) --------------------------------------------------

def _entries(*costs, base=1900.0):
    return [BudgetEntry(ts=base + i, tokens=0, cost_usd=c)
            for i, c in enumerate(costs)]


def test_gate_fails_open_with_no_ceiling():
    # config=None -> budget_config("test") -> unlimited default -> never gate
    assert wb.gate("test", entries=_entries(5.0, 5.0), now=2000.0)["ok"]


def test_gate_fails_open_with_no_history():
    cfg = BudgetConfig(window_hours=5.0, token_budget=0, cost_budget_usd=1.0)
    assert wb.gate("test", entries=[], config=cfg, now=2000.0)["ok"]  # est 0 -> allow


def test_gate_refuses_when_estimate_would_breach_ceiling():
    cfg = BudgetConfig(window_hours=5.0, token_budget=0, cost_budget_usd=1.0)
    # window holds $0.90; rolling estimate $0.45 -> 1.35 > 1.00 ceiling
    res = wb.gate("test", entries=_entries(0.45, 0.45), config=cfg, now=2000.0)
    assert not res["ok"]
    assert "ceiling" in res["reason"]
    assert res["estimate"] > 0


def test_gate_allows_when_estimate_fits_under_ceiling():
    cfg = BudgetConfig(window_hours=5.0, token_budget=0, cost_budget_usd=100.0)
    assert wb.gate("test", entries=_entries(0.45, 0.45), config=cfg, now=2000.0)["ok"]
