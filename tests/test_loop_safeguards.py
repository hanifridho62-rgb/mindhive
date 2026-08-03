# tests/test_loop_safeguards.py
from mindhive.loop import safeguards
from mindhive.loop.contract import LoopStatus


def _status(exit_signal=None, status=None, has=False):
    return LoopStatus(summary=None, progress=None, status=status,
                      exit_signal=exit_signal, has_completion_signal=has)


# --- exit gate ---
def test_exit_requires_both_signal_and_no_required_items():
    s = _status(exit_signal=True, status="COMPLETE", has=True)
    done, reason = safeguards.should_exit(s, required_remaining=0, json_mode=True)
    assert done is True and reason == "completed"


def test_no_exit_when_required_items_remain():
    s = _status(exit_signal=True, has=True)
    done, reason = safeguards.should_exit(s, required_remaining=2, json_mode=True)
    assert done is False


def test_no_exit_on_explicit_false():
    s = _status(exit_signal=False, status="COMPLETE", has=False)
    done, _ = safeguards.should_exit(s, required_remaining=0, json_mode=True)
    assert done is False


def test_json_mode_blocks_keyword_only_exit():
    # no explicit signal, no STATUS field -> has_completion_signal False
    s = _status(exit_signal=None, status=None, has=False)
    done, _ = safeguards.should_exit(s, required_remaining=0, json_mode=True)
    assert done is False


# --- circuit breaker ---
def test_breaker_opens_on_no_progress():
    cfg = safeguards.BreakerConfig(no_progress_threshold=3, same_error_threshold=5,
                                   cooldown_seconds=1800)
    cb = safeguards.CircuitBreaker(cfg)
    for i in range(3):
        cb.record(progress=False, error_sig=None, now=float(i))
    assert cb.state == "OPEN"


def test_breaker_resets_no_progress_on_progress():
    cfg = safeguards.BreakerConfig(3, 5, 1800)
    cb = safeguards.CircuitBreaker(cfg)
    cb.record(progress=False, error_sig=None, now=0.0)
    cb.record(progress=True, error_sig=None, now=1.0)
    cb.record(progress=False, error_sig=None, now=2.0)
    assert cb.state == "CLOSED"


def test_breaker_opens_on_repeated_error():
    cfg = safeguards.BreakerConfig(99, 3, 1800)
    cb = safeguards.CircuitBreaker(cfg)
    for i in range(3):
        cb.record(progress=True, error_sig="boom", now=float(i))
    assert cb.state == "OPEN"


def test_breaker_half_open_after_cooldown_then_closes_on_progress():
    cfg = safeguards.BreakerConfig(1, 5, cooldown_seconds=100)
    cb = safeguards.CircuitBreaker(cfg)
    cb.record(progress=False, error_sig=None, now=0.0)
    assert cb.state == "OPEN"
    assert cb.allow(now=50.0) is False        # still cooling down
    assert cb.allow(now=150.0) is True         # cooldown elapsed -> HALF_OPEN
    assert cb.state == "HALF_OPEN"
    cb.record(progress=True, error_sig=None, now=151.0)
    assert cb.state == "CLOSED"


def _hstatus(exit_signal=None, status=None, has=False, heuristics=0):
    return LoopStatus(summary=None, progress=None, status=status,
                      exit_signal=exit_signal, has_completion_signal=has,
                      heuristic_count=heuristics)


def test_explicit_signal_alone_fails_heuristic_gate():
    s = _hstatus(exit_signal=True, has=True, heuristics=0)
    done, reason = safeguards.should_exit(s, required_remaining=0, json_mode=True,
                                          min_heuristics=2)
    assert done is False and reason == "insufficient_completion_heuristics"


def test_explicit_signal_plus_two_heuristics_exits():
    s = _hstatus(exit_signal=True, status="COMPLETE", has=True, heuristics=2)
    done, reason = safeguards.should_exit(s, required_remaining=0, json_mode=True,
                                          min_heuristics=2)
    assert done is True and reason == "completed"


def test_heuristics_never_override_explicit_false():
    s = _hstatus(exit_signal=False, status="COMPLETE", has=False, heuristics=3)
    done, _ = safeguards.should_exit(s, required_remaining=0, json_mode=True,
                                     min_heuristics=2)
    assert done is False


def test_heuristics_do_not_bypass_required_items():
    s = _hstatus(exit_signal=True, status="COMPLETE", has=True, heuristics=3)
    done, reason = safeguards.should_exit(s, required_remaining=1, json_mode=True,
                                          min_heuristics=2)
    assert done is False and reason == "required_items_remain"


def test_min_heuristics_zero_restores_explicit_only_gate():
    s = _hstatus(exit_signal=True, has=True, heuristics=0)
    done, _ = safeguards.should_exit(s, required_remaining=0, json_mode=True,
                                     min_heuristics=0)
    assert done is True


# --- rotation spin guard -----------------------------------------------------
#
# Rotation is the remedy for a full context; a rotation that itself triggers a
# rotation is a spin. Two distinct runaway shapes are bounded here:
#
#   1. Sheer frequency, over a trailing window.
#   2. A seed so large that each FRESH session refills the window almost
#      immediately. This one is the dangerous shape, because every individual
#      rotation looks correct — the handoff is simply too big to fit.

def _guard(**kw):
    cfg = safeguards.RotationGuardConfig(**kw)
    return safeguards.RotationGuard(cfg)


def test_rotations_under_the_cap_are_allowed():
    g = _guard(max_per_window=3, window_seconds=3600, min_turns_before_rotation=2)
    now = 1000.0
    for i in range(3):
        assert g.allow(now=now + i) is True
        g.record(now=now + i, turns_in_session=10)


def test_rotation_at_the_cap_is_refused_with_a_reason():
    g = _guard(max_per_window=2, window_seconds=3600, min_turns_before_rotation=2)
    g.record(now=1000.0, turns_in_session=10)
    g.record(now=1001.0, turns_in_session=10)
    assert g.allow(now=1002.0) is False
    assert "2" in g.reason and "3600" in g.reason


def test_the_cap_window_slides():
    # CANARY for the test above: the same two rotations, once they age out of
    # the trailing window, must stop blocking. Without this, the refusal
    # assertion could pass against an implementation that simply never allows a
    # third rotation ever.
    g = _guard(max_per_window=2, window_seconds=3600, min_turns_before_rotation=2)
    g.record(now=1000.0, turns_in_session=10)
    g.record(now=1001.0, turns_in_session=10)
    assert g.allow(now=1002.0) is False
    assert g.allow(now=1000.0 + 3601) is True


def test_two_consecutive_immediate_refills_halt():
    # A fresh session that crosses the ceiling again within a couple of turns
    # means the SEED is oversized, not that the work is long. Rotating again
    # cannot fix that, so the guard stops rather than spinning.
    g = _guard(max_per_window=99, window_seconds=3600, min_turns_before_rotation=3)
    g.record(now=1000.0, turns_in_session=1)
    assert g.allow(now=1001.0) is True        # one is not yet a pattern
    g.record(now=1001.0, turns_in_session=2)
    assert g.allow(now=1002.0) is False
    assert g.state == "HALTED"
    assert "seed" in g.reason.lower() or "turn" in g.reason.lower()


def test_a_healthy_run_between_them_does_not_halt():
    # THE canary for the halt above. Without it, "halts on two short sessions"
    # would pass equally well against a guard that halts on any two rotations —
    # i.e. against a guard that is simply always-on.
    g = _guard(max_per_window=99, window_seconds=3600, min_turns_before_rotation=3)
    g.record(now=1000.0, turns_in_session=1)
    g.record(now=1001.0, turns_in_session=50)     # a real, productive session
    g.record(now=1002.0, turns_in_session=1)
    assert g.allow(now=1003.0) is True
    assert g.state == "OK"


def test_halt_is_terminal():
    g = _guard(max_per_window=99, window_seconds=3600, min_turns_before_rotation=3)
    g.record(now=1000.0, turns_in_session=1)
    g.record(now=1001.0, turns_in_session=1)
    assert g.allow(now=1002.0) is False
    # A later healthy-looking rotation must not silently un-halt it; the
    # operator has to look at why the seed is oversized.
    g.record(now=2000.0, turns_in_session=90)
    assert g.allow(now=2001.0) is False


def test_guard_defaults_are_usable_without_configuration():
    g = safeguards.RotationGuard(safeguards.RotationGuardConfig())
    assert g.allow(now=0.0) is True
    assert g.state == "OK"
    assert g.reason == ""
