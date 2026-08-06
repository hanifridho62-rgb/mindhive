# tests/test_loop_replenish_trigger.py
"""The loop refusing to exit, and the four structural stops that bound it.

These drive the REAL `engine.run_loop` — the driver is a stub because `claude -p`
cannot authenticate in CI (and does not on this machine), but the control flow
under test is the production one: the exit gate, the replenishment round, the
breaker, and the counters folded out of `work_injected`.

The discriminating shape throughout is a pair: the same run with the injector
finding something and finding nothing. A test that only asserted the refusal
would pass against an injector that refused unconditionally, which is the failure
mode that would pin every run open forever.
"""
from pathlib import Path

import pytest

from mindhive import events, replenish
from mindhive.loop import config, engine
from mindhive.loop import task as task_mod
from mindhive.loop.driver import DriverResult, Usage

COMPLETING = ("Everything is done and verified; no work remains.\n"
              "LOOP_STATUS:\n  summary: done\n  progress: true\n"
              "  STATUS: COMPLETE\n  EXIT_SIGNAL: true\n")

SCAFFOLD = "# Fix plan\n- [x] the only step\n\n## Future / Optional\n"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


class AlwaysCompleting:
    """A driver that claims completion every turn, forever.

    Unbounded on purpose: nothing here may rely on the driver running out of
    canned replies to stop the loop. If a stop does not hold, the test must fail
    on `max_iterations`, not on an IndexError that looks like a stop."""

    def __init__(self):
        self.turns = 0

    def run(self, prompt, loop_context, **kw):
        self.turns += 1
        return DriverResult(text=COMPLETING, usage=Usage(input_tokens=100),
                            session_id="s1", num_turns=1, rate_limited=False)


def _loop_dir(tmp_path, text=SCAFFOLD):
    d = tmp_path / "proj" / ".hive-loop"
    d.mkdir(parents=True)
    (d / "PROMPT.md").write_text("Build it", encoding="utf-8")
    (d / "fix_plan.md").write_text(text, encoding="utf-8")
    return d


def _one_candidate(fp="fp-a", title="Mined work", depth=1, occurrences=2,
                   severity=True):
    """A lens returning one candidate that CLEARS the default threshold.

    The defaults are not arbitrary: `balanced` wants 25, and base 10 + one repeat
    + severity is 35. A bare candidate scores 10 and is correctly refused — see
    `test_a_candidate_below_the_threshold_never_lands`, which asserts exactly
    that so this fixture's numbers cannot quietly become the only thing tested."""
    def lens(_ctx):
        return [replenish.Candidate(lens="probe", title=title, fingerprint=fp,
                                    evidence=("a real pointer",), depth=depth,
                                    occurrences=occurrences, severity=severity)]
    return {"probe": lens}


def _run(loop_dir, lenses, *, cfg=None, monkeypatch=None, max_iterations=6):
    """Drive a real run whose only lens set is the one given."""
    cfg = cfg or config.LoopConfig(max_iterations=max_iterations,
                                   min_completion_heuristics=0)
    monkeypatch.setattr(replenish, "lenses_for", lambda _p, registry=None: lenses)
    driver = AlwaysCompleting()
    result = engine.run_loop(project="test", loop_dir=loop_dir, cfg=cfg,
                             driver=driver, now_fn=_clock(),
                             sleep_fn=lambda _s: None,
                             progress_fn=lambda _i: False)
    return result, driver


def _clock():
    ticks = iter(range(0, 100000, 10))
    return lambda: next(ticks)


def _rows(outcome=None):
    return [e.payload for e in events.read_all()
            if e.type == "work_injected"
            and (outcome is None or e.payload.get("outcome") == outcome)]


# --- the refusal, and its mirror ----------------------------------------------

def test_a_run_whose_round_injects_does_not_end(tmp_path, monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    result, driver = _run(loop_dir, _one_candidate(), monkeypatch=monkeypatch)
    # The item landed, so the next iteration saw required work and the run kept
    # going until its iteration ceiling rather than exiting as "completed".
    assert result["reason"] != "completed"
    assert driver.turns > 1
    assert task_mod.required_remaining_count(
        (loop_dir / "fix_plan.md").read_text(encoding="utf-8")) >= 1


def test_a_candidate_below_the_threshold_never_lands(tmp_path, monkeypatch):
    # The dial has to actually gate. A bare candidate scores the base 10, and the
    # default `balanced` position wants 25 — so the run exits despite the lens
    # having found something.
    loop_dir = _loop_dir(tmp_path)
    result, driver = _run(loop_dir,
                          _one_candidate(occurrences=1, severity=False),
                          monkeypatch=monkeypatch)
    assert result["reason"] == "completed"
    assert driver.turns == 1
    assert _rows("landed") == []
    # And the refusal is NOT recorded. "Below the bar" is "not yet", not "no": a
    # recorded refusal spends the fingerprint (history() folds refused rows into
    # the spent set), which would make this condition unproposable forever — even
    # once the record insists on it hard enough to clear the threshold.
    assert _rows("refused") == []


def test_a_run_whose_round_injects_nothing_ends_completed(tmp_path, monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    result, driver = _run(loop_dir, {"probe": lambda _ctx: []},
                          monkeypatch=monkeypatch)
    assert result["reason"] == "completed"
    assert driver.turns == 1


def test_the_landed_item_is_marked_new_in_the_file(tmp_path, monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    _run(loop_dir, _one_candidate(), monkeypatch=monkeypatch)
    items = task_mod.plan_items(
        (loop_dir / "fix_plan.md").read_text(encoding="utf-8"))
    mined = [i for i in items["planned"] if i.text.startswith("Mined work")]
    assert mined and mined[0].is_new is True


def test_the_audit_row_carries_the_run_id_and_the_loop_surface(tmp_path,
                                                               monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    result, _driver = _run(loop_dir, _one_candidate(), monkeypatch=monkeypatch)
    landed = _rows("landed")
    assert landed
    assert landed[0]["surface"] == "loop"
    assert landed[0]["run_id"] == result["run_id"]


# --- stop 1: the dry streak, scoped to one run --------------------------------

def test_two_dry_rounds_in_one_run_end_it(tmp_path, monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    # Round 1 lands, round 2 and 3 find nothing: the streak reaches 2 and the run
    # stops mining, so the completing reply is finally honoured.
    calls = {"n": 0}

    def lens(_ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return [replenish.Candidate(lens="probe", title="Mined once",
                                        fingerprint="fp-1",
                                        evidence=("pointer",))]
        return []

    result, _driver = _run(loop_dir, {"probe": lens}, monkeypatch=monkeypatch,
                           max_iterations=12)
    assert result["reason"] == "completed"
    assert [r["outcome"] for r in _rows() if r["outcome"] == "dry"]


def test_a_different_run_starts_with_a_clean_dry_streak(tmp_path, monkeypatch):
    # Without the run_id scope the first run to go quiet twice would disable the
    # loop surface project-wide, permanently. This is that regression.
    events.append_event("work_injected",
                        {"outcome": "dry", "round": 1, "surface": "loop",
                         "run_id": "an-older-run"})
    events.append_event("work_injected",
                        {"outcome": "dry", "round": 2, "surface": "loop",
                         "run_id": "an-older-run"})
    loop_dir = _loop_dir(tmp_path)
    result, _driver = _run(loop_dir, _one_candidate(), monkeypatch=monkeypatch)
    assert result["reason"] != "completed"        # this run still mined


def test_the_streak_is_scoped_when_folded_by_run_id():
    for round_no in (1, 2):
        events.append_event("work_injected",
                            {"outcome": "dry", "round": round_no,
                             "surface": "loop", "run_id": "run-a"})
    evs = events.read_all()
    assert replenish.history(evs, surface="loop", run_id="run-a").dry_rounds == 2
    assert replenish.history(evs, surface="loop", run_id="run-b").dry_rounds == 0


# --- stop 2: the per-run injection budget -------------------------------------

def test_the_per_run_injection_cap_is_folded_not_counted():
    # Folded from the ledger so a resumed run inherits its spent budget instead
    # of starting again with a full one.
    for i in range(3):
        events.append_event("work_injected",
                            {"outcome": "landed", "round": i + 1,
                             "surface": "loop", "run_id": "run-a",
                             "lens": "probe", "title": f"t{i}",
                             "fingerprint": f"fp-{i}", "evidence": ["p"]})
    hist = replenish.history(events.read_all(), surface="loop", run_id="run-a")
    assert hist.landed_count == 3


def test_a_run_at_its_injection_budget_stops_mining():
    cfg = replenish.ReplenishConfig(max_injections_per_run=2)
    for i in range(2):
        events.append_event("work_injected",
                            {"outcome": "landed", "round": i + 1,
                             "surface": "loop", "run_id": "run-a",
                             "lens": "probe", "title": f"t{i}",
                             "fingerprint": f"fp-{i}", "evidence": ["p"]})
    res = replenish.run_round(
        events.read_all(), surface="loop", sink=lambda kept: ([], 0),
        cfg=cfg, run_id="run-a",
        lenses={"probe": lambda _c: [replenish.Candidate(
            lens="probe", title="more", fingerprint="fp-new",
            evidence=("p",))]})
    assert res.skipped == "injection budget exhausted"


def test_the_budget_trims_a_round_rather_than_refusing_it():
    cfg = replenish.ReplenishConfig(max_injections_per_run=2, max_per_round=10)
    events.append_event("work_injected",
                        {"outcome": "landed", "round": 1, "surface": "loop",
                         "run_id": "run-a", "lens": "probe", "title": "t",
                         "fingerprint": "fp-0", "evidence": ["p"]})

    def sink(kept):
        return ([replenish.Placement(c.fingerprint, "run-a", i)
                 for i, c in enumerate(kept)], 0)

    res = replenish.run_round(
        events.read_all(), surface="loop", sink=sink, cfg=cfg, run_id="run-a",
        lenses={"probe": lambda _c: [
            replenish.Candidate(lens="probe", title=f"t{i}",
                                fingerprint=f"new-{i}", evidence=("p",))
            for i in range(5)]})
    assert len(res.landed) == 1              # only one of the budget's 2 was left
    assert res.dropped == 4
    assert res.dry is False                 # it FOUND work; it could not fit it


# --- stop 3: the per-run round ceiling ----------------------------------------

def test_a_run_stops_mining_after_its_round_ceiling():
    cfg = replenish.ReplenishConfig(max_rounds_per_run=2)
    for round_no in (1, 2):
        events.append_event("work_injected",
                            {"outcome": "dry", "round": round_no,
                             "surface": "loop", "run_id": "run-a"})
    res = replenish.run_round(
        events.read_all(), surface="loop", sink=lambda kept: ([], 0),
        cfg=cfg, run_id="run-a",
        lenses={"probe": lambda _c: [replenish.Candidate(
            lens="probe", title="t", fingerprint="fp", evidence=("p",))]})
    assert res.skipped == "injection rounds exhausted"


def test_the_round_ceiling_is_scoped_to_the_run():
    cfg = replenish.ReplenishConfig(max_rounds_per_run=2)
    for round_no in (1, 2):
        events.append_event("work_injected",
                            {"outcome": "dry", "round": round_no,
                             "surface": "loop", "run_id": "someone-else"})
    res = replenish.run_round(
        events.read_all(), surface="loop", sink=lambda kept: ([], 0),
        cfg=cfg, run_id="run-a",
        lenses={"probe": lambda _c: []})
    assert res.ran                                   # not skipped


# --- stop 4: injection depth --------------------------------------------------

def test_a_candidate_past_the_depth_ceiling_is_refused(tmp_path, monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    result, driver = _run(loop_dir, _one_candidate(depth=3),
                          monkeypatch=monkeypatch)
    assert result["reason"] == "completed"           # nothing could land
    assert driver.turns == 1
    assert any("exceeds the maximum injection depth" in r.get("reason", "")
               for r in _rows("refused"))


def test_a_candidate_at_the_depth_ceiling_still_lands(tmp_path, monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    result, _driver = _run(loop_dir, _one_candidate(depth=2),
                           monkeypatch=monkeypatch)
    assert result["reason"] != "completed"


# --- the breaker must not fire on success ------------------------------------

def _breaker_run(loop_dir, monkeypatch, *, reset_works):
    """A run that accrues one no-progress iteration, then injects.

    `progress_fn` is False throughout and the threshold is 2, so the streak is at
    1 when the injection happens. If landing work clears it the run gets two more
    iterations before the breaker opens; if it does not, the very next iteration
    opens it. The iteration count is therefore the observable difference."""
    monkeypatch.setattr(replenish, "lenses_for",
                        lambda _p, registry=None: _one_candidate())
    if not reset_works:
        # The canary: with the reset neutered the same run must open the breaker
        # sooner. Without this companion the assertion below would pass against an
        # engine that never reset anything.
        from mindhive.loop import safeguards
        monkeypatch.setattr(safeguards.CircuitBreaker, "reset",
                            lambda _self: None)

    class Scripted:
        def __init__(self):
            self.turns = 0

        def run(self, prompt, loop_context, **kw):
            self.turns += 1
            text = ("still working\nLOOP_STATUS:\n  progress: false\n"
                    "  EXIT_SIGNAL: false\n") if self.turns == 1 else COMPLETING
            return DriverResult(text=text, usage=Usage(input_tokens=10),
                                session_id="s1", num_turns=1,
                                rate_limited=False)

    return engine.run_loop(
        project="test", loop_dir=loop_dir,
        cfg=config.LoopConfig(max_iterations=8, min_completion_heuristics=0,
                              no_progress_threshold=2),
        driver=Scripted(), now_fn=_clock(), sleep_fn=lambda _s: None,
        progress_fn=lambda _i: False)


def test_an_injection_resets_the_breakers_no_progress_counter(tmp_path,
                                                              monkeypatch):
    result = _breaker_run(_loop_dir(tmp_path), monkeypatch, reset_works=True)
    assert result["reason"] == "breaker_open"     # eventually, and that is fine
    assert result["iterations"] == 4


def test_without_the_reset_the_breaker_opens_a_full_iteration_sooner(tmp_path,
                                                                    monkeypatch):
    # Landing evidence-backed work IS progress. Letting the streak carry across it
    # would trip the breaker on the exact behaviour the feature exists to produce.
    result = _breaker_run(_loop_dir(tmp_path), monkeypatch, reset_works=False)
    assert result["reason"] == "breaker_open"
    assert result["iterations"] == 3


def test_the_breaker_reset_is_a_public_method():
    from mindhive.loop import safeguards
    breaker = safeguards.CircuitBreaker(safeguards.BreakerConfig(1, 1, 0))
    breaker.record(progress=False, error_sig=None, now=0)
    assert breaker.state == "OPEN"
    breaker.reset()
    assert breaker.state == "CLOSED"
    assert breaker.consecutive_no_progress == 0


# --- the round only runs at the one moment it should -------------------------

def test_no_round_runs_when_the_exit_reason_is_not_completed(tmp_path,
                                                            monkeypatch):
    loop_dir = _loop_dir(tmp_path, "# Fix plan\n- [ ] unfinished\n")
    # An unchecked required item means `should_exit` never reports completed, so
    # the injector must never be consulted.
    seen = {"calls": 0}

    def lens(_ctx):
        seen["calls"] += 1
        return []

    _run(loop_dir, {"probe": lens}, monkeypatch=monkeypatch, max_iterations=2)
    assert seen["calls"] == 0


def test_a_rate_limited_run_does_not_mine(tmp_path, monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    seen = {"calls": 0}

    def lens(_ctx):
        seen["calls"] += 1
        return []

    monkeypatch.setattr(replenish, "lenses_for",
                        lambda _p, registry=None: {"probe": lens})

    class Limited:
        def run(self, prompt, loop_context, **kw):
            return DriverResult(text="", usage=Usage(input_tokens=10),
                                session_id="s1", num_turns=1,
                                rate_limited=True)

    result = engine.run_loop(project="test", loop_dir=loop_dir,
                             cfg=config.LoopConfig(max_iterations=3),
                             driver=Limited(), now_fn=_clock(),
                             sleep_fn=lambda _s: None,
                             progress_fn=lambda _i: True)
    assert result["reason"] == "api_cap"
    assert seen["calls"] == 0


def test_replenish_disabled_honours_the_exit_immediately(tmp_path, monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    cfg = config.LoopConfig(max_iterations=4, min_completion_heuristics=0,
                            replenish_enabled=False)
    result, driver = _run(loop_dir, _one_candidate(), cfg=cfg,
                          monkeypatch=monkeypatch)
    assert result["reason"] == "completed"
    assert driver.turns == 1
    assert _rows() == []


# --- failure must not pin a run open -----------------------------------------

def test_an_injector_exception_lets_the_run_finish_and_is_recorded(tmp_path,
                                                                  monkeypatch):
    loop_dir = _loop_dir(tmp_path)

    def boom(_p, registry=None):
        raise RuntimeError("profile resolution exploded")

    monkeypatch.setattr(replenish, "lenses_for", boom)
    driver = AlwaysCompleting()
    result = engine.run_loop(
        project="test", loop_dir=loop_dir,
        cfg=config.LoopConfig(max_iterations=3, min_completion_heuristics=0),
        driver=driver, now_fn=_clock(), sleep_fn=lambda _s: None,
        progress_fn=lambda _i: True)
    assert result["reason"] == "completed"        # the safe direction
    failures = [e.payload for e in events.read_all() if e.type == "tool_failed"]
    assert any(f.get("signature") == "replenish:round" for f in failures)


def test_a_broken_lens_does_not_end_the_run_abnormally(tmp_path, monkeypatch):
    loop_dir = _loop_dir(tmp_path)

    def lens(_ctx):
        raise RuntimeError("lens exploded")

    result, _driver = _run(loop_dir, {"probe": lens}, monkeypatch=monkeypatch)
    assert result["reason"] == "completed"
    failures = [e.payload for e in events.read_all() if e.type == "tool_failed"]
    assert any(f.get("signature") == "lens:probe" for f in failures)


def test_loop_iteration_events_still_bracket_every_iteration(tmp_path,
                                                            monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    result, driver = _run(loop_dir, _one_candidate(), monkeypatch=monkeypatch)
    iterations = [e for e in events.read_all() if e.type == "loop_iteration"]
    assert len(iterations) == driver.turns == result["iterations"]


def test_an_unknown_ambition_is_reported_once_as_a_decision(tmp_path,
                                                            monkeypatch):
    loop_dir = _loop_dir(tmp_path)
    cfg = config.LoopConfig(max_iterations=3, min_completion_heuristics=0,
                            replenish_ambition="aggressive")
    _run(loop_dir, _one_candidate(), cfg=cfg, monkeypatch=monkeypatch)
    decisions = [e.payload["text"] for e in events.read_all()
                 if e.type == "decision"]
    fallbacks = [d for d in decisions if "not a known position" in d]
    assert len(fallbacks) == 1
    assert "balanced" in fallbacks[0]
