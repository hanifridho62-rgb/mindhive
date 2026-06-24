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
