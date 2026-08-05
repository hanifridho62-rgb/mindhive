# tests/test_replenish_autotune.py
"""The auto-tuner: how selective the gate becomes, decided by what injections did.

This is homeostasis, not learning, and the two directions are asymmetric on
purpose — 3 bad outcomes to retreat, 5 good ones to advance. Retreating faster
than it advances is the correct bias for a mechanism that spends an operator's
quota.

The clamps are the part worth protecting. A feedback loop on a feedback loop
degrades slowly and silently; `MIN_THRESHOLD` and `TUNE_MAX_DRIFT` are what make
it recoverable, and the `decision` row is what makes it auditable.
"""
from mindhive import events, replenish


def _landed(ts, fp):
    return events.append_event(
        "work_injected",
        {"outcome": "landed", "round": 1, "surface": "loop", "lens": "probe",
         "title": "mined", "fingerprint": fp, "evidence": ["p"]}, ts=ts)


def _failure(ts):
    return events.append_event("tool_failed",
                               {"text": "boom", "signature": "sig"}, ts=ts)


def _verification(ts, exit_code=0):
    return events.append_event(
        "verification",
        {"text": "pytest", "argv": ["pytest"], "exit_code": exit_code,
         "counts": {"passed": 1}, "excerpt": "", "verified_by": "python"},
        ts=ts)


def _breaker_open(ts):
    return events.append_event("loop_run_end",
                               {"run_id": "r", "reason": "breaker_open",
                                "iterations": 3}, ts=ts)


def _tune():
    return replenish.tune_threshold(events.read_all(), surface="loop")


# --- raising ------------------------------------------------------------------

def test_three_injections_each_followed_by_a_failure_raise_the_bar():
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _failure(f"2026-08-0{day}T01:00:00Z")
    adjustment = _tune()
    assert adjustment.delta == 10
    assert adjustment.window == 3
    assert "failure or a tripped breaker" in adjustment.reason


def test_a_tripped_breaker_counts_as_a_failure():
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _breaker_open(f"2026-08-0{day}T01:00:00Z")
    assert _tune().delta == 10


def test_two_failures_are_not_enough():
    for day in (2, 4):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _failure(f"2026-08-0{day}T01:00:00Z")
    assert _tune().delta == 0


def test_a_failing_verification_counts_as_a_failure():
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _verification(f"2026-08-0{day}T01:00:00Z", exit_code=1)
    assert _tune().delta == 10


# --- lowering -----------------------------------------------------------------

def test_five_green_injections_lower_the_bar():
    for day in range(1, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _verification(f"2026-08-0{day}T01:00:00Z")
    adjustment = _tune()
    assert adjustment.delta == -5
    assert adjustment.window == 5
    assert "verified green" in adjustment.reason


def test_four_green_injections_are_not_enough():
    for day in range(1, 5):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _verification(f"2026-08-0{day}T01:00:00Z")
    assert _tune().delta == 0


def test_lowering_needs_five_CONSECUTIVE_green_outcomes():
    for day in range(1, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _verification(f"2026-08-0{day}T01:00:00Z")
    _landed("2026-08-07T00:00:00Z", "fp-bad")
    _failure("2026-08-07T01:00:00Z")
    assert _tune().delta == 0          # the window slid; the streak is broken


# --- the ambiguous cases stay silent -----------------------------------------

def test_a_mixed_window_produces_no_adjustment():
    _landed("2026-08-01T00:00:00Z", "fp-1")
    _verification("2026-08-01T01:00:00Z")
    _landed("2026-08-02T00:00:00Z", "fp-2")
    _failure("2026-08-02T01:00:00Z")
    _landed("2026-08-03T00:00:00Z", "fp-3")
    _verification("2026-08-03T01:00:00Z")
    assert _tune().delta == 0


def test_an_injection_with_no_recorded_outcome_yet_is_not_counted():
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
    assert _tune().delta == 0


def test_an_empty_ledger_produces_no_adjustment():
    assert replenish.tune_threshold([]).delta == 0


def test_a_malformed_row_is_skipped_and_does_not_raise():
    events.append_event("work_injected", ["not", "a", "dict"],
                        ts="2026-08-01T00:00:00Z")
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _failure(f"2026-08-0{day}T01:00:00Z")
    assert _tune().delta == 10


def test_rows_from_another_surface_do_not_tune_this_one():
    for day in (2, 4, 6):
        events.append_event(
            "work_injected",
            {"outcome": "landed", "round": 1, "surface": "session",
             "lens": "probe", "title": "t", "fingerprint": f"s-{day}",
             "evidence": ["p"]}, ts=f"2026-08-0{day}T00:00:00Z")
        _failure(f"2026-08-0{day}T01:00:00Z")
    assert _tune().delta == 0


def test_only_the_first_outcome_after_an_injection_counts():
    # A run that broke and was later fixed says something different about the
    # injected work than one that passed immediately.
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _failure(f"2026-08-0{day}T01:00:00Z")
        _verification(f"2026-08-0{day}T02:00:00Z")
    assert _tune().delta == 10


# --- the clamps ---------------------------------------------------------------

def test_the_floor_is_the_base_score():
    lowered = replenish.tuned_threshold(
        replenish.BASE_SCORE, replenish.ThresholdAdjustment(-50, "r", 5))
    assert lowered == replenish.MIN_THRESHOLD == replenish.BASE_SCORE


def test_a_lowered_threshold_can_never_admit_everything():
    for base in (15, 25, 40):
        assert replenish.tuned_threshold(
            base, replenish.ThresholdAdjustment(-999, "r", 5)) >= 10


def test_drift_is_bounded_upward_too():
    # An unlucky streak must not raise the bar out of every candidate's reach and
    # switch the injector off in a way no operator chose.
    raised = replenish.tuned_threshold(
        25, replenish.ThresholdAdjustment(999, "r", 3))
    assert raised == 25 + replenish.TUNE_MAX_DRIFT


def test_a_neutral_adjustment_leaves_the_base_alone():
    assert replenish.tuned_threshold(25, replenish.ThresholdAdjustment()) == 25


def test_the_adjustment_composes_with_each_dial_position():
    raise_it = replenish.ThresholdAdjustment(10, "r", 3)
    lower_it = replenish.ThresholdAdjustment(-5, "r", 5)
    assert replenish.tuned_threshold(
        replenish.PROFILES["conservative"].min_score, raise_it) == 50
    assert replenish.tuned_threshold(
        replenish.PROFILES["ambitious"].min_score, lower_it) == 10


# --- the round applies it, and says so ---------------------------------------

def _sink(kept):
    return ([replenish.Placement(c.fingerprint, "p", i)
             for i, c in enumerate(kept)], 0)


def _probe(score_terms=(1, False)):
    occurrences, severity = score_terms

    def lens(_ctx):
        return [replenish.Candidate(lens="probe", title="mined",
                                    fingerprint="fp-new",
                                    evidence=("pointer",),
                                    occurrences=occurrences,
                                    severity=severity)]
    return {"probe": lens}


def test_a_raised_threshold_refuses_what_the_base_would_have_kept():
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _failure(f"2026-08-0{day}T01:00:00Z")
    # occurrences 2 + severity = 35, which clears 25 but not the tuned 35... so
    # pick a candidate that sits between the two: 30.
    cfg = replenish.ReplenishConfig(min_score=25)
    res = replenish.run_round(events.read_all(), surface="loop", sink=_sink,
                              cfg=cfg, lenses=_probe((1, True)), tune=True)
    assert res.landed == []
    assert any("below the round's minimum" in reason
               for _cand, reason in res.refused)


def test_the_same_candidate_lands_when_the_tuner_is_off():
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _failure(f"2026-08-0{day}T01:00:00Z")
    cfg = replenish.ReplenishConfig(min_score=25)
    res = replenish.run_round(events.read_all(), surface="loop", sink=_sink,
                              cfg=cfg, lenses=_probe((1, True)), tune=False)
    assert len(res.landed) == 1


def test_a_shift_is_recorded_as_exactly_one_decision():
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _failure(f"2026-08-0{day}T01:00:00Z")
    replenish.run_round(events.read_all(), surface="loop", sink=_sink,
                        cfg=replenish.ReplenishConfig(min_score=25),
                        lenses=_probe(), tune=True)
    decisions = [e.payload["text"] for e in events.read_all()
                 if e.type == "decision"]
    tuning = [d for d in decisions if "value gate" in d]
    assert len(tuning) == 1
    assert "raised by 10 to 35" in tuning[0]
    assert "window: last 3" in tuning[0]


def test_a_neutral_round_records_no_decision():
    # A row per round would flood the ledger and every future briefing, and "the
    # tuner looked and did nothing" is not a decision.
    replenish.run_round([], surface="loop", sink=_sink, lenses=_probe(),
                        tune=True)
    assert not [e for e in events.read_all()
                if e.type == "decision" and "value gate" in e.payload.get("text", "")]


def test_a_dry_run_applies_the_shift_but_records_nothing():
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _failure(f"2026-08-0{day}T01:00:00Z")
    before = len([e for e in events.read_all() if e.type == "decision"])
    replenish.run_round(events.read_all(), surface="loop", sink=_sink,
                        cfg=replenish.ReplenishConfig(min_score=25),
                        lenses=_probe(), tune=True, append=False)
    after = len([e for e in events.read_all() if e.type == "decision"])
    assert after == before


def test_tuning_is_off_by_default():
    # A surface that runs one round per session has no outcome history to tune on.
    for day in (2, 4, 6):
        _landed(f"2026-08-0{day}T00:00:00Z", f"fp-{day}")
        _failure(f"2026-08-0{day}T01:00:00Z")
    replenish.run_round(events.read_all(), surface="loop", sink=_sink,
                        cfg=replenish.ReplenishConfig(min_score=25),
                        lenses=_probe())
    assert not [e for e in events.read_all()
                if e.type == "decision" and "value gate" in e.payload.get("text", "")]
