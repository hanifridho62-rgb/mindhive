# tests/test_loop_engine_budget.py
from mindhive import events
from mindhive.loop import engine, config
from mindhive.loop.driver import DriverResult, Usage


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")
    d = tmp_path / "proj" / ".hive-loop"
    d.mkdir(parents=True)
    (d / "PROMPT.md").write_text("go", encoding="utf-8")
    (d / "fix_plan.md").write_text("- [ ] x\n", encoding="utf-8")
    return d


class FakeDriver:
    def __init__(self, r):
        self.r = r
        self.calls = 0

    def run(self, *a, **k):
        self.calls += 1
        return self.r


def test_on_cap_stop_halts_when_budget_exceeded(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    big = DriverResult(text="LOOP_STATUS:\n  EXIT_SIGNAL: false\n",
                       usage=Usage(input_tokens=5000, cost_usd=1.0),
                       session_id="s", num_turns=1, rate_limited=False)
    drv = FakeDriver(big)
    cfg = config.LoopConfig(max_iterations=10, window_token_budget=6000,
                            window_hours=10, on_cap="stop")
    t = {"v": 0.0}

    def now():
        t["v"] += 1.0
        return t["v"]

    result = engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                             now_fn=now, sleep_fn=lambda s: None, progress_fn=lambda i: True)
    # iter1 records 5000 tokens; before iter2 the projection (5000+5000>6000) trips stop
    assert result["reason"] == "budget_cap"
    assert drv.calls == 1
