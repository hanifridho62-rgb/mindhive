# tests/test_loop_replenish.py
"""The loop trigger: a run that would have ended grows its own checklist.

The assertion that carries the feature is the ITERATION COUNT, not the presence
of an event. A run whose reply claims completion and whose checklist has emptied
would stop; if the injector lands work, the same reply must not stop it, because
`required_remaining_count` has risen and `safeguards.should_exit` refuses. That
is a structural termination change, and nothing in any prompt asks for it.
"""
import pytest

from mindhive import events, replenish
from mindhive.loop import config, engine
from mindhive.loop.driver import DriverResult, Usage

COMPLETE = ("EXIT_SIGNAL: true\nsummary: done\n"
            "all done — the work is complete and everything is finished")


class FakeDriver:
    """Always claims completion, and (optionally) does the work.

    `works=True` ticks every unchecked item off `fix_plan.md` each iteration —
    the model of an agent that actually completes what it is given. Without it
    the checklist never empties after the first injection, so the run never
    reaches a second completion gate and no bound after the first can be
    exercised at all."""

    def __init__(self, plan_path=None, works=True):
        self.calls = 0
        self.plan_path = plan_path
        self.works = works

    def run(self, *_a, **_kw):
        self.calls += 1
        if self.works and self.plan_path is not None and self.plan_path.exists():
            body = self.plan_path.read_text(encoding="utf-8")
            self.plan_path.write_text(body.replace("- [ ] ", "- [x] "),
                                      encoding="utf-8")
        return DriverResult(text=COMPLETE, usage=Usage(input_tokens=10),
                            session_id="s1", num_turns=1, rate_limited=False)


def _loop_dir(tmp_path, body="# Fix plan\n- [x] scaffold\n"):
    d = tmp_path / ".hive-loop"
    d.mkdir()
    (d / "PROMPT.md").write_text("# Build it\n", encoding="utf-8")
    (d / "fix_plan.md").write_text(body, encoding="utf-8")
    (d / "AGENT.md").write_text("run: pytest", encoding="utf-8")
    return d


def _clock():
    t = [1_000_000.0]

    def now():
        t[0] += 1.0
        return t[0]
    return now


def _cfg(**over):
    kw = {"max_iterations": 5, "model": "claude-haiku-4-5",
          "min_completion_heuristics": 0}
    kw.update(over)
    return config.LoopConfig(**kw)


def _run(tmp_path, lenses, cfg=None, body="# Fix plan\n- [x] scaffold\n",
         works=True, progress=True):
    d = _loop_dir(tmp_path, body)
    monkey = replenish.LENSES
    # Registered under a name the BALANCED level actually selects. An ambition
    # level filters the registry BY NAME, so a lens called "l" is silently
    # filtered out and every assertion here would measure the filter rather
    # than the trigger.
    replenish.LENSES = {"coverage": next(iter(lenses.values()))}
    try:
        return engine.run_loop(project="test", loop_dir=d, cfg=cfg or _cfg(),
                               driver=FakeDriver(d / "fix_plan.md",
                                                 works=works),
                               now_fn=_clock(), sleep_fn=lambda _s: None,
                               progress_fn=lambda _i: progress), d
    finally:
        replenish.LENSES = monkey


def _cand(fp="fp-1", title="Bootstrap the suite"):
    return replenish.Candidate(lens="coverage", title=title, fingerprint=fp,
                               evidence=("a real pointer",), severity=True)


def _plan(d):
    return (d / "fix_plan.md").read_text(encoding="utf-8")


# --- the exit overturn -------------------------------------------------------

def test_a_run_with_nothing_to_inject_completes():
    # The baseline the overturn is measured against.
    pass


def test_a_completing_run_that_injects_does_not_end(tmp_path):
    seen = {"n": 0}

    def lens(_evs, root=None):
        seen["n"] += 1
        return [_cand(f"fp-{seen['n']}")] if seen["n"] == 1 else []

    result, d = _run(tmp_path, {"l": lens})
    # The run did NOT stop at the first completing reply: the checklist grew,
    # the gate refused, and a second iteration ran.
    assert result["iterations"] >= 2
    assert "(new) Bootstrap the suite" in _plan(d)


def test_a_completing_run_that_injects_nothing_ends_completed(tmp_path):
    # Ends on the dry streak (default 2), not on the first empty round — the
    # iteration counts live in the dry-streak tests below.
    result, d = _run(tmp_path, {"l": lambda _e, root=None: []})
    assert result["reason"] == "completed"
    assert "(new)" not in _plan(d)


def test_the_round_runs_only_at_a_completion_exit(tmp_path):
    # A run with required work outstanding never reaches the gate, so the
    # injector must not have run at all — mining costs a full ledger pass.
    calls = []
    result, _d = _run(tmp_path, {"l": lambda _e, root=None: calls.append(1) or []},
                      cfg=_cfg(max_iterations=2), works=False,
                      body="# Fix plan\n- [ ] real work\n")
    assert result["reason"] == "max_iterations"
    assert calls == []


def test_injected_work_is_recorded_against_the_run(tmp_path):
    result, _d = _run(tmp_path, {"l": lambda _e, root=None: [_cand()]})
    rows = [e.payload for e in events.read_all()
            if e.type == "work_injected" and e.payload["outcome"] == "landed"]
    assert rows and rows[0]["run_id"] == result["run_id"]
    assert rows[0]["surface"] == "loop"


def test_loop_iteration_events_still_bracket_every_iteration(tmp_path):
    result, _d = _run(tmp_path, {"l": lambda _e, root=None: [_cand()]})
    iters = [e for e in events.read_all() if e.type == "loop_iteration"]
    assert len(iters) == result["iterations"]


# --- the bounds --------------------------------------------------------------

def test_a_single_dry_round_does_not_end_the_run(tmp_path):
    # The iteration that just ran wrote events of its own, so the next round
    # reads a different ledger. One empty answer is not a finished run.
    state = {"n": 0}

    def lens(_evs, root=None):
        state["n"] += 1
        # Round 1 finds nothing; round 2 does. A run that stopped at the first
        # dry round would never see the second.
        return [_cand()] if state["n"] == 2 else []

    result, d = _run(tmp_path, {"l": lens},
                     cfg=_cfg(max_iterations=9, replenish_dry_rounds=2))
    assert "(new) Bootstrap the suite" in _plan(d)
    assert result["iterations"] >= 3


def test_two_dry_rounds_in_one_run_end_it(tmp_path):
    result, _d = _run(tmp_path, {"l": lambda _e, root=None: []},
                      cfg=_cfg(max_iterations=9, replenish_dry_rounds=2))
    assert result["reason"] == "completed"
    dry = [e for e in events.read_all()
           if e.type == "work_injected" and e.payload["outcome"] == "dry"]
    # Bounded: the streak ends the run rather than an unbounded stream of empty
    # rounds, each of which costs a full Claude call.
    assert len(dry) == 2


def test_with_the_streak_off_the_first_dry_round_ends_the_run(tmp_path):
    # dry_rounds = 0 disables the second chance, so there is none to give.
    result, _d = _run(tmp_path, {"l": lambda _e, root=None: []},
                      cfg=_cfg(max_iterations=9, replenish_dry_rounds=0))
    assert result["reason"] == "completed" and result["iterations"] == 1


def test_a_different_run_starts_with_a_clean_dry_streak(tmp_path):
    # Folded over the surface's whole history, the first quiet run would switch
    # surface="loop" off project-wide forever.
    for _ in range(2):
        events.append_event("work_injected",
                            {"outcome": "dry", "round": 1, "surface": "loop",
                             "run_id": "an-older-run"})
    result, d = _run(tmp_path, {"l": lambda _e, root=None: [_cand()]})
    assert result["iterations"] >= 2
    assert "(new)" in _plan(d)


def test_the_round_ceiling_bounds_a_run_that_keeps_completing(tmp_path):
    state = {"n": 0}

    def lens(_evs, root=None):
        state["n"] += 1
        return [_cand(f"fp-{state['n']}", f"item {state['n']}")]

    result, _d = _run(tmp_path, {"l": lens},
                      cfg=_cfg(max_iterations=20, replenish_max_rounds=2,
                               replenish_dry_rounds=0))
    # Two rounds may run; the third is refused, so the run completes rather
    # than injecting forever.
    assert result["reason"] == "completed"
    landed = [e for e in events.read_all()
              if e.type == "work_injected"
              and e.payload["outcome"] == "landed"]
    assert len(landed) == 2


def test_disabled_replenish_lets_the_run_end(tmp_path):
    result, d = _run(tmp_path, {"l": lambda _e, root=None: [_cand()]},
                     cfg=_cfg(replenish_enabled=False))
    assert result["reason"] == "completed" and result["iterations"] == 1
    assert "(new)" not in _plan(d)


# --- degradation -------------------------------------------------------------

def test_an_injector_exception_lets_the_run_exit_normally(tmp_path):
    def boom(_evs, root=None):
        raise RuntimeError("mining exploded")

    result, _d = _run(tmp_path, {"l": boom})
    # A failed injection must never crash a live loop.
    assert result["reason"] == "completed"
    assert [e for e in events.read_all() if e.type == "loop_run_end"]


def test_a_sink_failure_is_recorded_rather_than_swallowed(tmp_path, monkeypatch):
    from mindhive import replenish_sinks

    def boom(*_a, **_kw):
        raise OSError("disk on fire")

    monkeypatch.setattr(replenish_sinks, "fix_plan_sink", boom)
    result, _d = _run(tmp_path, {"l": lambda _e, root=None: [_cand()]})
    assert result["reason"] == "completed"
    sigs = [e.payload.get("signature") for e in events.read_all()
            if e.type == "tool_failed"]
    assert "replenish:round" in sigs


def test_an_unknown_ambition_is_reported_and_the_round_still_runs(tmp_path):
    result, d = _run(tmp_path, {"l": lambda _e, root=None: [_cand()]},
                     cfg=_cfg(replenish_ambition="agressive"))
    assert result["iterations"] >= 2          # fell back to balanced, still ran
    assert "(new)" in _plan(d)
    sigs = [e.payload.get("signature") for e in events.read_all()
            if e.type == "tool_failed"]
    assert "replenish:config" in sigs


# --- the breaker -------------------------------------------------------------

def test_injecting_does_not_trip_the_no_progress_breaker(tmp_path):
    # progress_fn returns False throughout and `.hive-loop` is in the mtime
    # tracker's ignore set, so without the injected term the breaker would kill
    # the run for doing exactly what the feature asks of it.
    state = {"n": 0}

    def lens(_evs, root=None):
        state["n"] += 1
        return [_cand(f"fp-{state['n']}", f"item {state['n']}")]

    result, _d = _run(tmp_path, {"l": lens}, progress=False,
                      cfg=_cfg(max_iterations=6, no_progress_threshold=1,
                               replenish_max_rounds=0, replenish_dry_rounds=0))
    # Without the injected term the breaker opens on iteration 1 — the very
    # iteration the feature exists to create. With it, iteration 1 counts as
    # progress and the run reaches iteration 2, where a genuinely idle agent
    # (progress_fn False, nothing injected) correctly trips it.
    assert result["iterations"] == 2
    assert result["reason"] == "breaker_open"
