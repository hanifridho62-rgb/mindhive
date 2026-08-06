# tests/test_replenish_tuning.py
"""The auto-tuning profile, and the anti-lazy hold it feeds.

`test_replenish_autotune.py` covers the threshold arithmetic and its clamps. This
file covers the record the arithmetic is read off — `AutoTuningProfile`, folded
out of the ledger in the same pass as everything else — and the one decision that
profile drives beyond the threshold: whether a run claiming to be finished, on a
system the record proves healthy, is allowed to stop without ever having proposed
an improvement.

The hold is the most intrusive thing this subsystem does, so the tests that matter
most here are the ones that bound it: it fires at most once per run, it never
fires on a struggling system, and any failure to read the record makes it fire not
at all. A hold that could repeat would pin a run open forever, which this project
treats as strictly worse than an early exit.
"""
from pathlib import Path

import pytest

from mindhive import events, replenish
from mindhive.loop import config, engine, safeguards
from mindhive.loop.driver import DriverResult, Usage

COMPLETING = ("Everything is done and verified; no work remains.\n"
              "LOOP_STATUS:\n  summary: done\n  progress: true\n"
              "  STATUS: COMPLETE\n  EXIT_SIGNAL: true\n")

SCAFFOLD = "# Fix plan\n- [x] the only step\n\n## Future / Optional\n"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


# --- the fold -----------------------------------------------------------------

def _landed(fp, surface="loop", lens="probe", run_id=None):
    payload = {"outcome": "landed", "round": 1, "surface": surface,
               "lens": lens, "title": "mined", "fingerprint": fp,
               "evidence": ["a real pointer"]}
    if run_id:
        payload["run_id"] = run_id
    return events.append_event("work_injected", payload)


def _verified(exit_code=0):
    return events.append_event(
        "verification",
        {"text": "pytest", "argv": ["pytest"], "exit_code": exit_code,
         "counts": {"passed": 1}, "excerpt": "", "verified_by": "python"})


def _failed():
    return events.append_event("tool_failed",
                               {"text": "boom", "signature": "sig"})


def _green_run(count):
    """*count* injections that each verified green, oldest first."""
    for index in range(count):
        _landed(f"fp-{index}")
        _verified()


def test_consecutive_green_verifications_become_a_success_streak():
    _green_run(replenish.TUNE_LOWER_WINDOW)
    profile = replenish.tuning_profile(events.read_all(), surface="loop")
    assert profile.success_streak == replenish.TUNE_LOWER_WINDOW
    assert profile.failure_streak == 0
    assert profile.healthy is True
    assert profile.adjustment.delta == replenish.TUNE_LOWER_DELTA


def test_repeated_failures_become_a_failure_streak_and_raise_the_bar():
    for index in range(replenish.TUNE_RAISE_WINDOW):
        _landed(f"fp-{index}")
        _failed()
    profile = replenish.tuning_profile(events.read_all(), surface="loop")
    assert profile.failure_streak == replenish.TUNE_RAISE_WINDOW
    assert profile.strained is True
    assert profile.adjustment.delta == replenish.TUNE_RAISE_DELTA
    assert replenish.tuned_threshold(25, profile.adjustment) == 35


def test_a_single_failure_breaks_a_long_green_streak():
    """Streaks, not totals. A system with ten green results and one fresh failure
    is not healthy, and any measure that averaged its history would say it was."""
    _green_run(10)
    _landed("fp-late")
    _failed()
    profile = replenish.tuning_profile(events.read_all(), surface="loop")
    assert profile.success_streak == 0
    assert profile.failure_streak == 1
    assert profile.healthy is False


def test_an_injection_with_no_outcome_yet_is_not_counted_either_way():
    # It has not verified and it has not broken anything. Treating an unanswered
    # injection as a success is how a tuner talks itself into being bolder on no
    # evidence at all.
    _green_run(replenish.TUNE_LOWER_WINDOW)
    _landed("fp-pending")
    profile = replenish.tuning_profile(events.read_all(), surface="loop")
    assert profile.success_streak == replenish.TUNE_LOWER_WINDOW
    assert len(profile.outcomes) == replenish.TUNE_LOWER_WINDOW


def test_the_profile_is_scoped_by_surface():
    _green_run(replenish.TUNE_LOWER_WINDOW)
    assert replenish.tuning_profile(events.read_all(),
                                    surface="session").healthy is False
    assert replenish.tuning_profile(events.read_all(),
                                    surface="loop").healthy is True


def test_the_profile_ignores_run_scoping_so_a_fresh_run_inherits_it():
    """A run's own first injection has by definition produced nothing yet.

    Narrowing the tuner to one `run_id` would therefore make it permanently
    silent: every run would start with an empty outcome window and end before it
    filled one."""
    _green_run(replenish.TUNE_LOWER_WINDOW)
    hist = replenish.history(events.read_all(), surface="loop",
                             run_id="a-run-that-has-injected-nothing")
    assert hist.landed_count == 0
    assert hist.tuning.healthy is True


def test_the_fold_is_total_on_a_malformed_row():
    events.append_event("work_injected", {"outcome": "landed", "round": 1,
                                          "surface": "loop", "lens": "p",
                                          "title": "t", "fingerprint": "f",
                                          "evidence": ["e"]})
    events.append_event("verification", {"text": "x", "argv": [], "exit_code": 0,
                                         "counts": {}, "excerpt": "",
                                         "verified_by": "python"})
    # A row this version does not understand must be skipped, not raise: the
    # ledger holds history written by every past version of this code.
    events.append_event("verification", {"text": "x", "argv": [],
                                         "exit_code": "not an int",
                                         "counts": {}, "excerpt": "",
                                         "verified_by": "python"})
    assert replenish.tuning_profile(events.read_all(),
                                    surface="loop").success_streak == 1


# --- the capacity record ------------------------------------------------------

def test_capacity_withholds_only_when_all_three_conditions_hold():
    assert safeguards.InnovationCapacity(healthy=True).withholds_exit is True
    assert safeguards.InnovationCapacity(healthy=False).withholds_exit is False
    assert safeguards.InnovationCapacity(
        healthy=True, innovation_proposed=True).withholds_exit is False
    assert safeguards.InnovationCapacity(
        healthy=True, already_held=True).withholds_exit is False


def test_a_default_capacity_withholds_nothing():
    # What every caller that cannot read the record falls back to. Failing toward
    # EXIT is the only safe direction for a mechanism whose failure mode is
    # "never stops".
    assert safeguards.InnovationCapacity().withholds_exit is False


class _Status:
    def __init__(self, exit_signal=True, heuristic_count=9):
        self.exit_signal = exit_signal
        self.has_completion_signal = True
        self.heuristic_count = heuristic_count


def test_the_gate_withholds_exit_for_unused_capacity():
    done, reason = safeguards.should_exit(
        _Status(), 0, json_mode=True,
        capacity=safeguards.InnovationCapacity(healthy=True))
    assert (done, reason) == (False, "unused_innovation_capacity")


def test_the_gate_is_unchanged_when_no_capacity_is_supplied():
    assert safeguards.should_exit(_Status(), 0, json_mode=True) == (
        True, "completed")


def test_capacity_is_the_last_question_the_gate_asks():
    """A real structural stop must always be the reported reason.

    The hold is the weakest reason to keep going, so it may only ever explain a
    run that would otherwise have stopped — never mask one that had a stronger
    reason to continue."""
    done, reason = safeguards.should_exit(
        _Status(), 3, json_mode=True,
        capacity=safeguards.InnovationCapacity(healthy=True))
    assert (done, reason) == (False, "required_items_remain")


# --- the hold, driven through the real engine ---------------------------------

class _AlwaysCompleting:
    """Claims completion every turn, forever. Unbounded on purpose: nothing here
    may stop because the driver ran out of canned replies."""

    def __init__(self):
        self.turns = 0

    def run(self, prompt, loop_context, **kw):
        self.turns += 1
        return DriverResult(text=COMPLETING, usage=Usage(input_tokens=100),
                            session_id="s1", num_turns=1, rate_limited=False)


def _loop_dir(tmp_path):
    d = tmp_path / "proj" / ".hive-loop"
    d.mkdir(parents=True)
    (d / "PROMPT.md").write_text("Build it", encoding="utf-8")
    (d / "fix_plan.md").write_text(SCAFFOLD, encoding="utf-8")
    return d


def _clock():
    ticks = iter(range(0, 100000, 10))
    return lambda: next(ticks)


def _run_with_nothing_to_mine(tmp_path, monkeypatch, *, anti_lazy=True):
    """A run whose lenses find nothing, so only the hold can keep it alive."""
    monkeypatch.setattr(replenish, "lenses_for", lambda _p, registry=None: {})
    cfg = config.LoopConfig(max_iterations=6, min_completion_heuristics=0,
                            replenish_anti_lazy=anti_lazy)
    driver = _AlwaysCompleting()
    result = engine.run_loop(project="test", loop_dir=_loop_dir(tmp_path),
                             cfg=cfg, driver=driver, now_fn=_clock(),
                             sleep_fn=lambda _s: None,
                             progress_fn=lambda _i: False)
    return result, driver


def test_a_healthy_run_that_proposed_no_innovation_is_held_once(tmp_path,
                                                                monkeypatch):
    _green_run(replenish.TUNE_LOWER_WINDOW)
    result, driver = _run_with_nothing_to_mine(tmp_path, monkeypatch)

    # Held exactly once: a second turn happened, and then the run stopped. If the
    # hold could repeat, this would run to `max_iterations` instead.
    assert driver.turns == 2
    assert result["reason"] == "completed"
    holds = [e for e in events.read_all()
             if e.type == "decision" and "anti-lazy" in (e.tags or [])]
    assert len(holds) == 1
    assert "holding the exit once" in holds[0].payload["text"]


def test_an_unproven_run_is_never_held(tmp_path, monkeypatch):
    # No outcome history at all, so nothing proves this system is coping. The
    # run stops the moment it says it is done.
    result, driver = _run_with_nothing_to_mine(tmp_path, monkeypatch)
    assert driver.turns == 1
    assert result["reason"] == "completed"


def test_a_struggling_run_is_never_held(tmp_path, monkeypatch):
    """Pushing a system that is already failing to also innovate is how a bad run
    becomes a worse one."""
    for index in range(replenish.TUNE_RAISE_WINDOW):
        _landed(f"fp-{index}")
        _failed()
    _result, driver = _run_with_nothing_to_mine(tmp_path, monkeypatch)
    assert driver.turns == 1


def test_a_run_that_already_proposed_innovation_is_not_held(tmp_path,
                                                            monkeypatch):
    _green_run(replenish.TUNE_LOWER_WINDOW)
    # The capacity has been spent. Which run spent it matters, so this is scoped
    # to the run under test — and the run id is unknowable in advance, so the
    # engine's own reader is what is exercised here.
    monkeypatch.setattr(engine, "_innovation_capacity",
                        lambda project, run_id, held:
                        safeguards.InnovationCapacity(True, True, held))
    _result, driver = _run_with_nothing_to_mine(tmp_path, monkeypatch)
    assert driver.turns == 1


def test_the_hold_can_be_switched_off(tmp_path, monkeypatch):
    _green_run(replenish.TUNE_LOWER_WINDOW)
    _result, driver = _run_with_nothing_to_mine(tmp_path, monkeypatch,
                                                anti_lazy=False)
    assert driver.turns == 1


def test_an_unreadable_record_never_holds_a_run_open(monkeypatch):
    """The failure direction that matters. A capacity check that cannot see the
    ledger must not be the reason a run refuses to stop."""
    def explode(_project=None):
        raise OSError("the store is gone")

    monkeypatch.setattr(engine.events, "read_all", explode)
    capacity = engine._innovation_capacity("test", "run-1", held=False)
    assert capacity.withholds_exit is False


def test_the_held_round_runs_at_ambitious(tmp_path, monkeypatch):
    """The hold exists to spend innovation capacity, and the innovation lens runs
    at `ambitious` alone — so a hold that mined at the operator's dial would be a
    hold that could not possibly do what it was called for."""
    _green_run(replenish.TUNE_LOWER_WINDOW)
    seen = []

    def spy(profile, registry=None):
        seen.append(profile.name)
        return {}

    monkeypatch.setattr(replenish, "lenses_for", spy)
    cfg = config.LoopConfig(max_iterations=6, min_completion_heuristics=0,
                            replenish_ambition="conservative")
    engine.run_loop(project="test", loop_dir=_loop_dir(tmp_path), cfg=cfg,
                    driver=_AlwaysCompleting(), now_fn=_clock(),
                    sleep_fn=lambda _s: None, progress_fn=lambda _i: False)
    assert "conservative" in seen, "the operator's dial governs the normal round"
    assert "ambitious" in seen, "the held round must reach the innovation lens"


def test_anti_lazy_is_read_from_loop_toml(tmp_path):
    path = tmp_path / "loop.toml"
    path.write_text("[replenish]\nanti_lazy = false\n", encoding="utf-8")
    assert config.load_config(path).replenish_anti_lazy is False


def test_anti_lazy_defaults_on():
    assert config.LoopConfig().replenish_anti_lazy is True
