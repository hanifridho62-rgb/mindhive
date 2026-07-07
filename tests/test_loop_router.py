# tests/test_loop_router.py — deterministic model-tier routing (U8).
from mindhive import events
from mindhive.loop import config, engine, router
from mindhive.loop.driver import DriverResult, Usage


def test_size_class_static_signals():
    assert router.size_class(1, 100) == "small"
    assert router.size_class(3, 800) == "medium"
    assert router.size_class(7, 100) == "large"
    assert router.size_class(1, 3000) == "large"


def test_escalate_saturates_at_heavy():
    assert router.escalate("light") == "mid"
    assert router.escalate("mid") == "heavy"
    assert router.escalate("heavy") == "heavy"
    assert router.escalate("bogus") == "mid"   # unknown -> up from the floor


def _h(size, tier, reason):
    return {"size": size, "tier": tier, "reason": reason}


def test_initial_tier_static_without_history():
    assert router.pick_initial_tier("small") == "light"
    assert router.pick_initial_tier("medium") == "mid"
    assert router.pick_initial_tier("large") == "heavy"


def test_completed_history_lowers_the_starting_tier():
    hist = [_h("medium", "light", "completed")]
    assert router.pick_initial_tier("medium", hist) == "light"


def test_breaker_history_at_static_tier_starts_one_up():
    hist = [_h("small", "light", "breaker_open")]
    assert router.pick_initial_tier("small", hist) == "mid"


def test_other_size_history_is_ignored():
    hist = [_h("large", "light", "completed")]
    assert router.pick_initial_tier("small", hist) == "light"


def test_run_history_from_events():
    events.append_event("loop_run_start",
                        {"run_id": "r1", "size_class": "small"},
                        ts="2026-07-07T10:00:00Z")
    events.append_event("loop_iteration", {"run_id": "r1", "tier": "light"},
                        ts="2026-07-07T10:01:00Z")
    events.append_event("loop_run_end", {"run_id": "r1", "reason": "completed"},
                        ts="2026-07-07T10:02:00Z")
    events.append_event("loop_run_start", {"run_id": "r2", "size_class": "small"},
                        ts="2026-07-07T11:00:00Z")   # unfinished — excluded
    hist = router.run_history(events.read_all("test"))
    assert hist == [{"size": "small", "tier": "light", "reason": "completed"}]


# --- engine wiring ---------------------------------------------------------

def _setup(tmp_path, monkeypatch, fix_plan="- [ ] step one\n"):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")
    d = tmp_path / "proj" / ".hive-loop"
    d.mkdir(parents=True)
    (d / "PROMPT.md").write_text("Build it", encoding="utf-8")
    (d / "fix_plan.md").write_text(fix_plan, encoding="utf-8")
    return d


class RecordingDriver:
    def __init__(self, results):
        self.results = list(results)
        self.models = []

    def run(self, prompt, loop_context, **kw):
        self.models.append(kw.get("model"))
        return self.results.pop(0)


def _ok(text="ok\nLOOP_STATUS:\n  progress: true\n  EXIT_SIGNAL: false\n"):
    return DriverResult(text=text, usage=Usage(input_tokens=100), session_id="s",
                        num_turns=1, rate_limited=False)


def _err():
    return DriverResult(text="", usage=Usage(), session_id=None, num_turns=0,
                        rate_limited=False, error="timeout")


def _clock():
    t = {"v": 0.0}

    def now():
        t["v"] += 1.0
        return t["v"]
    return now


def test_engine_routes_small_task_to_light_and_escalates_on_error(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    drv = RecordingDriver([_err(), _ok()])
    cfg = config.LoopConfig(max_iterations=2)
    engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                    now_fn=_clock(), sleep_fn=lambda s: None,
                    progress_fn=lambda i: True)
    assert drv.models[0] == cfg.tier_light      # small task starts light
    assert drv.models[1] == cfg.tier_mid        # error escalated one tier
    iters = [e for e in events.read_all() if e.type == "loop_iteration"]
    assert iters[0].payload["model"] == cfg.tier_light
    assert iters[0].payload["tier"] == "light"
    assert iters[1].payload["tier"] == "mid"


def test_engine_pinned_model_bypasses_routing(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch)
    drv = RecordingDriver([_ok()])
    cfg = config.LoopConfig(max_iterations=1, model="my-pinned-model")
    engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                    now_fn=_clock(), sleep_fn=lambda s: None,
                    progress_fn=lambda i: True)
    assert drv.models == ["my-pinned-model"]
    it = [e for e in events.read_all() if e.type == "loop_iteration"][0]
    assert it.payload["model"] == "my-pinned-model"
    assert it.payload["tier"] is None


def test_engine_starts_from_history_success_tier(tmp_path, monkeypatch):
    loop_dir = _setup(tmp_path, monkeypatch,
                      fix_plan="- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d\n")  # medium
    # prior comparable (medium) run completed on light
    events.append_event("loop_run_start", {"run_id": "r0", "size_class": "medium"},
                        ts="2026-07-07T09:00:00Z")
    events.append_event("loop_iteration", {"run_id": "r0", "tier": "light"},
                        ts="2026-07-07T09:01:00Z")
    events.append_event("loop_run_end", {"run_id": "r0", "reason": "completed"},
                        ts="2026-07-07T09:02:00Z")
    drv = RecordingDriver([_ok()])
    cfg = config.LoopConfig(max_iterations=1)
    engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg, driver=drv,
                    now_fn=_clock(), sleep_fn=lambda s: None,
                    progress_fn=lambda i: True)
    assert drv.models == [cfg.tier_light]


def test_tier_config_round_trip(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[model.tiers]\nlight = "l-model"\nmid = "m-model"\n',
                 encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.tier_light == "l-model"
    assert cfg.tier_mid == "m-model"
    assert cfg.tier_heavy == config.LoopConfig().tier_heavy
