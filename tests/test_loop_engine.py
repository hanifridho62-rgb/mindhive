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
        _result("All tasks have been completed.\nLOOP_STATUS:\n  progress: true\n  STATUS: COMPLETE\n  EXIT_SIGNAL: true\n"),
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
        _result("All tasks have been completed.\nLOOP_STATUS:\n  progress: true\n  EXIT_SIGNAL: true\n  STATUS: COMPLETE\n",
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


def test_loop_survives_repeated_timeouts(tmp_path, monkeypatch):
    # A driver that always times out must NOT crash the loop: the breaker opens
    # and the run ends gracefully with a loop_run_end event.
    loop_dir = _setup(tmp_path, monkeypatch)
    timeout = DriverResult(text="", usage=Usage(), session_id=None, num_turns=0,
                           rate_limited=False, error="timeout")

    class TimeoutDriver:
        def __init__(self):
            self.calls = []

        def run(self, prompt, loop_context, **kw):
            self.calls.append(kw)
            return timeout

    drv = TimeoutDriver()
    cfg = config.LoopConfig(max_iterations=20, no_progress_threshold=3,
                            same_error_threshold=3)
    result = engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                             now_fn=_fake_clock(), sleep_fn=lambda s: None,
                             progress_fn=lambda i: False)
    assert result["reason"] == "breaker_open"
    types = [e.type for e in events.read_all()]
    assert "loop_run_end" in types          # graceful end, not a crash
    # each timed-out iteration drops the session (never resumes a dead one)
    assert all(c["session_id"] is None for c in drv.calls)


def _fake_clock():
    t = {"v": 0.0}

    def now():
        t["v"] += 1.0
        return t["v"]
    return now


def test_completed_run_is_auto_dismissed(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    (loop_dir / "fix_plan.md").write_text("- [x] step one\n", encoding="utf-8")
    drv = FakeDriver([
        _result("All tasks have been completed.\nLOOP_STATUS:\n  progress: true\n"
                "  STATUS: COMPLETE\n  EXIT_SIGNAL: true\n"),
    ])
    cfg = config.LoopConfig(max_iterations=5)
    result = engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                             now_fn=_fake_clock(), sleep_fn=lambda s: None,
                             progress_fn=lambda i: True)
    assert result["reason"] == "completed"
    dismissals = [e for e in events.read_all() if e.type == "loop_run_dismissed"]
    assert len(dismissals) == 1
    assert dismissals[0].payload["cause"] == "completed"
    assert dismissals[0].payload["run_id"] == result["run_id"]


def test_breaker_tripped_run_is_auto_dismissed(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    timeout = DriverResult(text="", usage=Usage(), session_id=None, num_turns=0,
                           rate_limited=False, error="timeout")
    drv = FakeDriver([timeout] * 10)
    cfg = config.LoopConfig(max_iterations=20, no_progress_threshold=3,
                            same_error_threshold=3)
    result = engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                             now_fn=_fake_clock(), sleep_fn=lambda s: None,
                             progress_fn=lambda i: False)
    assert result["reason"] == "breaker_open"
    dismissals = [e for e in events.read_all() if e.type == "loop_run_dismissed"]
    assert len(dismissals) == 1
    assert dismissals[0].payload["cause"] == "breaker"


def test_max_iterations_run_is_not_dismissed(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    drv = FakeDriver([_result("noop\nLOOP_STATUS:\n  progress: true\n  EXIT_SIGNAL: false\n")
                      for _ in range(3)])
    cfg = config.LoopConfig(max_iterations=3)
    engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                    now_fn=_fake_clock(), sleep_fn=lambda s: None,
                    progress_fn=lambda i: True)
    assert not [e for e in events.read_all() if e.type == "loop_run_dismissed"]


def test_loop_events_carry_run_id_and_title(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    drv = FakeDriver([_result("noop\nLOOP_STATUS:\n  progress: true\n  EXIT_SIGNAL: false\n")])
    cfg = config.LoopConfig(max_iterations=1)
    result = engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                             now_fn=_fake_clock(), sleep_fn=lambda s: None,
                             progress_fn=lambda i: True)
    rid = result["run_id"]
    evs = events.read_all()
    start = next(e for e in evs if e.type == "loop_run_start")
    assert start.payload["run_id"] == rid
    assert start.payload["title"] == "Build it"
    assert all(e.payload.get("run_id") == rid for e in evs
               if e.type in ("loop_iteration", "loop_run_end"))


def test_interrupted_run_still_gets_a_run_end(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)

    class ExplodingDriver:
        def run(self, prompt, loop_context, **kw):
            raise KeyboardInterrupt

    cfg = config.LoopConfig(max_iterations=5)
    import pytest
    with pytest.raises(KeyboardInterrupt):
        engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg,
                        driver=ExplodingDriver(), now_fn=_fake_clock(),
                        sleep_fn=lambda s: None, progress_fn=lambda i: True)
    ends = [e for e in events.read_all() if e.type == "loop_run_end"]
    assert ends and ends[-1].payload["reason"] == "interrupted"


def test_heuristic_blocked_iteration_does_not_trip_breaker(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    (loop_dir / "fix_plan.md").write_text("- [x] step one\n", encoding="utf-8")
    # agent asserts EXIT_SIGNAL: true every time but never restates completion
    # in prose (0 heuristics): blocked by the corroboration gate, files never
    # change — yet this is a responsive agent, so the breaker must NOT open
    blocked = _result("ok then\nLOOP_STATUS:\n  progress: true\n"
                      "  EXIT_SIGNAL: true\n")
    drv = FakeDriver([blocked] * 6)
    cfg = config.LoopConfig(max_iterations=6, no_progress_threshold=3)
    result = engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                             now_fn=_fake_clock(), sleep_fn=lambda s: None,
                             progress_fn=lambda i: False)
    assert result["reason"] == "max_iterations"       # ran out, not breaker_open
