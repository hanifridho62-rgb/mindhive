# src/mindhive/loop/safeguards.py
from dataclasses import dataclass


# --- dual-condition exit gate ---
def should_exit(status, required_remaining: int, json_mode: bool):
    """Return (should_exit, reason). Stops only when BOTH hold:
      - explicit EXIT_SIGNAL is true (authoritative), AND
      - no required fix_plan items remain.
    Explicit EXIT_SIGNAL: false always continues. In JSON mode, a missing
    explicit signal can never trigger exit (keyword-only exit suppressed)."""
    if status.exit_signal is False:
        return False, "explicit_continue"
    explicit_done = status.exit_signal is True
    inferred_done = (not json_mode) and status.has_completion_signal
    if not (explicit_done or inferred_done):
        return False, "no_completion_signal"
    if required_remaining > 0:
        return False, "required_items_remain"
    return True, "completed"


# --- circuit breaker ---
@dataclass
class BreakerConfig:
    no_progress_threshold: int
    same_error_threshold: int
    cooldown_seconds: float


class CircuitBreaker:
    def __init__(self, cfg: BreakerConfig):
        self.cfg = cfg
        self.state = "CLOSED"
        self.consecutive_no_progress = 0
        self.consecutive_same_error = 0
        self.last_error_sig = None
        self.opened_at = None

    def record(self, progress: bool, error_sig, now: float) -> None:
        # no-progress tracking
        self.consecutive_no_progress = 0 if progress else self.consecutive_no_progress + 1
        # repeated-error tracking
        if error_sig and error_sig == self.last_error_sig:
            self.consecutive_same_error += 1
        elif error_sig:
            self.consecutive_same_error = 1
        else:
            self.consecutive_same_error = 0
        self.last_error_sig = error_sig

        if self.state == "HALF_OPEN":
            # trial iteration: progress closes, otherwise re-open
            if progress and not error_sig:
                self._close()
            else:
                self._open(now)
            return

        if (self.consecutive_no_progress >= self.cfg.no_progress_threshold or
                self.consecutive_same_error >= self.cfg.same_error_threshold):
            self._open(now)

    def allow(self, now: float) -> bool:
        """Whether the next iteration may run. Transitions OPEN -> HALF_OPEN once
        the cooldown has elapsed."""
        if self.state == "OPEN":
            if self.opened_at is not None and now - self.opened_at >= self.cfg.cooldown_seconds:
                self.state = "HALF_OPEN"
                return True
            return False
        return True

    def _open(self, now):
        self.state = "OPEN"
        self.opened_at = now

    def _close(self):
        self.state = "CLOSED"
        self.consecutive_no_progress = 0
        self.consecutive_same_error = 0
        self.opened_at = None
