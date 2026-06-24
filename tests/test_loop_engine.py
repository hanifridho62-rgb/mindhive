# tests/test_loop_engine.py
from mindhive import events
from mindhive.loop import engine, config
from mindhive.loop.driver import DriverResult, Usage


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")
    d = tmp_path / "proj" / ".hive-loop"
    d.mkdir(parents=True)
    (d / "PROMPT.md").write_text("Build it", encoding="utf-8")
    (d / "fix_plan.md").write_text("- [ ] step one\n", encoding="utf-8")
    return d


class FakeDriver:
    """Returns canned results in sequence; records calls."""
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, prompt, loop_context, **kw):
        self.calls.append((prompt, loop_context, kw))
        return self.results.pop(0)


def _result(text, occupancy=1000, cost=0.01, session="s1"):
    # occupancy split across input + cache_read
    return DriverResult(text=text, usage=Usage(input_tokens=occupancy, cost_usd=cost),
                        session_id=session, num_turns=1, rate_limited=False)


def test_loop_completes_on_dual_condition(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    # fix_plan still has an unchecked item, so iteration 1's EXIT_SIGNAL is ignored;
    # iteration 2 checks the box and exits.
    drv = FakeDriver([
        _result("working\nLOOP_STATUS:\n  progress: true\n  EXIT_SIGNAL: false\n"),
        _result("done\nLOOP_STATUS:\n  progress: true\n  STATUS: COMPLETE\n  EXIT_SIGNAL: true\n"),
    ])

    # the engine re-reads fix_plan each iteration; simulate the box being checked
    # by having a progress hook overwrite it before iteration 2.
    def progress_fn(i):
        if i == 1:
            (loop_dir / "fix_plan.md").write_text("- [x] step one\n", encoding="utf-8")
        return True

    cfg = config.LoopConfig(max_iterations=5)
    result = engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                             now_fn=_fake_clock(), sleep_fn=lambda s: None,
                             progress_fn=progress_fn)
    assert result["reason"] == "completed"
    assert result["iterations"] == 2
    types = [e.type for e in events.read_all()]
    assert "loop_run_start" in types
    assert types.count("loop_iteration") == 2
    assert "loop_run_end" in types


def test_loop_stops_at_max_iterations(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    drv = FakeDriver([_result("noop\nLOOP_STATUS:\n  progress: true\n  EXIT_SIGNAL: false\n")
                      for _ in range(10)])
    cfg = config.LoopConfig(max_iterations=3)
    result = engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                             now_fn=_fake_clock(), sleep_fn=lambda s: None,
                             progress_fn=lambda i: True)
    assert result["reason"] == "max_iterations"
    assert result["iterations"] == 3


def test_loop_context_resets_when_window_half_full(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    # occupancy 120000 of 200000 = 0.6 >= 0.5 -> reset: next call has session_id=None
    drv = FakeDriver([
        _result("a\nLOOP_STATUS:\n  progress: true\n  EXIT_SIGNAL: false\n",
                occupancy=120000, session="big"),
        _result("b\nLOOP_STATUS:\n  progress: true\n  EXIT_SIGNAL: true\n  STATUS: COMPLETE\n",
                occupancy=1000, session="fresh"),
    ])
    cfg = config.LoopConfig(max_iterations=5)

    # check the box before iteration 2 so the dual-condition gate lets the
    # completion at iteration 2 actually exit (required_remaining -> 0).
    def progress_fn(i):
        if i == 1:
            (loop_dir / "fix_plan.md").write_text("- [x] step one\n", encoding="utf-8")
        return True

    engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                    now_fn=_fake_clock(), sleep_fn=lambda s: None,
                    progress_fn=progress_fn)
    # iteration 2 must NOT resume the bloated session
    assert drv.calls[1][2]["session_id"] is None
    assert any(e.type == "compaction" for e in events.read_all())


def _fake_clock():
    t = {"v": 0.0}

    def now():
        t["v"] += 1.0
        return t["v"]
    return now
