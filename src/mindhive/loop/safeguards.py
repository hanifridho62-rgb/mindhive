# src/mindhive/loop/safeguards.py
from dataclasses import dataclass


# --- dual-condition exit gate ---
def should_exit(status, required_remaining: int, json_mode: bool,
                min_heuristics: int = 0):
    """Return (should_exit, reason). Stops only when ALL hold:
      - explicit EXIT_SIGNAL is true (authoritative), AND
      - at least ``min_heuristics`` natural-language completion signals
        corroborate it in the reply text, AND
      - no required fix_plan items remain.
    Explicit EXIT_SIGNAL: false always continues. In JSON mode, a missing
    explicit signal can never trigger exit (keyword-only exit suppressed).
    ``min_heuristics=0`` restores the explicit-signal-only gate."""
    if status.exit_signal is False:
        return False, "explicit_continue"
    explicit_done = status.exit_signal is True
    inferred_done = (not json_mode) and status.has_completion_signal
    if not (explicit_done or inferred_done):
        return False, "no_completion_signal"
    if getattr(status, "heuristic_count", 0) < min_heuristics:
        return False, "insufficient_completion_heuristics"
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


# --- rotation spin guard -----------------------------------------------------

@dataclass
class RotationGuardConfig:
    """Bounds on session rotation. Deliberately separate from `BreakerConfig`:
    the breaker asks "is the agent still making progress", this asks "is the
    rotation machinery eating itself" — different question, different remedy."""

    max_per_window: int = 6
    window_seconds: float = 3600.0
    # A fresh session that crosses the ceiling again within this many turns is
    # evidence about the SEED, not about the work.
    min_turns_before_rotation: int = 3


class RotationGuard:
    """Stops rotation from becoming a spin.

    Rotation is the remedy for a full context. A rotation that immediately
    causes another rotation is therefore not a remedy at all, and the failure
    is quiet: every individual rotation looks correct in isolation. Two runaway
    shapes are bounded.

    **Frequency**, over a trailing window — the crude bound, and the one that
    catches a caller looping far faster than real work could.

    **Immediate refill** — two consecutive fresh sessions that each cross the
    ceiling within `min_turns_before_rotation` turns. That means the handoff
    seed is itself close to the ceiling, so the next rotation will do exactly
    the same thing. Rotating again cannot fix an oversized seed, so the guard
    HALTS and says so rather than burning the window discovering that.

    The halt is terminal on purpose. An oversized seed is a condition an
    operator has to look at; auto-recovering would hide the very signal that
    explains why nothing is getting done. Mirrors the recursion-guard instinct
    `claude_cli` already applies to spawn loops, where the failure mode is also
    unbounded repetition rather than a wrong answer."""

    def __init__(self, cfg: RotationGuardConfig):
        self.cfg = cfg
        self.state = "OK"
        self.reason = ""
        self._at: list = []                 # rotation timestamps
        self._last_was_short = False

    def allow(self, now: float) -> bool:
        """Whether another rotation may happen. Total — never raises."""
        if self.state == "HALTED":
            return False
        recent = [t for t in self._at if now - t < self.cfg.window_seconds]
        if len(recent) >= self.cfg.max_per_window:
            self.reason = (
                f"rotation refused: {len(recent)} rotations already in the "
                f"last {int(self.cfg.window_seconds)}s, cap is "
                f"{self.cfg.max_per_window}")
            return False
        return True

    def record(self, now: float, turns_in_session: int) -> None:
        """Register a rotation that just happened.

        *turns_in_session* is how many turns the session being rotated away
        from actually ran. It is the discriminating input: a long session that
        filled up is healthy, a one-turn session that filled up is a seed
        problem."""
        self._at.append(now)
        short = int(turns_in_session or 0) <= self.cfg.min_turns_before_rotation
        if short and self._last_was_short:
            self.state = "HALTED"
            self.reason = (
                f"rotation halted: two consecutive sessions crossed the smart "
                f"zone within {self.cfg.min_turns_before_rotation} turns, so "
                f"the handoff seed is itself near the ceiling — rotating again "
                f"would repeat this. Shrink the seed rather than retrying")
        self._last_was_short = short
