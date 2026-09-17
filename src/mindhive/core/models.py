# SPDX-License-Identifier: Apache-2.0
# src/mindhive/core/models.py
"""The ledger's domain primitives, moved here VERBATIM from `events.py`
(blueprint P0b, decision K6): the genesis hash, the one chain step and the
frozen `EventRecord` a reader builds per physical row. `events` re-exports
all three by identity -- `events.EventRecord is core.models.EventRecord` --
so every existing spelling still resolves to the same object and no chain
head moves.

Blueprint P1j adds the three frozen domain shapes the MEB names (section
3.5): `CircuitBreakerState`, the integration breaker's persisted state
(P1f writes it to `integration_breaker.json` through `as_dict` /
`from_dict`); `PoissonMetrics`, one row of the failure-rate report; and
`BreakerVerdict`, what a breaker or a lane quota answers when asked. The
BAB 3 mathematics of `mindhive.models` is re-exported here by identity as
well, so `mindhive.core.models` is the one import a domain consumer needs.

A stdlib leaf: `hashlib`, `math`, `dataclasses`, and `mindhive.models`,
which is itself a stdlib leaf. It must never import `events` (which
imports it) nor anything that touches a filesystem.
"""
import hashlib
import math
from dataclasses import dataclass

from .. import models as _bab3
from ..models import *  # noqa: F401,F403  -- BAB 3 re-exported by identity (P1j)

__all__ = ["GENESIS_PREV_HASH", "compute_chain_hash", "EventRecord",
           "CircuitBreakerState", "PoissonMetrics", "BreakerVerdict", *_bab3.__all__]

# --- the hash chain's primitives, exported ------------------------------------
# `chain.py` has always folded the ledger into a SHA-256 chain, but the fold
# lived inline in `chain.compute_chain` and `chain._fold` with nothing exporting
# the formula. The background watcher builds an `EventRecord` per physical row
# and stores `prev_hash` / `chain_hash` in vector metadata; a formula re-derived
# there by hand could drift from `chain.py` by one `.encode()` argument, and the
# two digests for one row would disagree without anything noticing. So the
# primitive lives here. `chain._ZERO` is not rewritten to import this constant
# -- `chain.py` is untouched -- and `tests/test_events_chain_helpers.py` pins
# the two equal and the fold identical in both of `chain.py`'s coordinates.
GENESIS_PREV_HASH = "0" * 64


def compute_chain_hash(prev_hash: str, canonical: str) -> str:
    """One chain step, byte for byte what `chain.compute_chain` and
    `chain._fold` run: SHA-256 over the UTF-8 bytes of `prev_hash + canonical`,
    where *canonical* is the str `canonical_json(to_dict(ev))` returns. NOT
    the PDF's `bytes` parameter with an ASCII-encoded prefix -- a second scheme
    would make the watcher's digest and the checkpoint's two different numbers
    for one row (plan decision AMBIG 3)."""
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventRecord:
    """One physical row as a READER sees it, with its chain coordinates.

    A runtime wrapper, never the row (AMBIG 3): the segment keeps exactly
    `Event`'s five keys, because every checkpoint on disk was folded over
    those bytes and a `prev_hash` written into the row would move every head
    on the first append. Readers build this from a row -- `event_id` is
    `f"{session_id}:{seg_line}"` with `seg_line` the 1-BASED physical line --
    and it is frozen so a record handed to a vector store cannot be edited
    after its `chain_hash` was computed over it."""
    event_id: str
    session_id: str
    seg_line: int
    timestamp: str
    event_type: str
    payload: dict
    tags: list
    prev_hash: str
    chain_hash: str


# --- the MEB domain shapes (P1j) -------------------------------------------------

def _count(name: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} harus bilangan bulat >= 0: {value!r}")


def _finite(name: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} harus bilangan terhingga: {value!r}")


@dataclass(frozen=True)
class CircuitBreakerState:
    """The integration breaker's state as it is persisted (P1f): the DTMC
    state name, the consecutive failures counted towards `FAILURE_THRESHOLD`,
    when it last tripped, the trips still inside the quarantine window, and
    why it is quarantined when it is. `trips_in_window` is a tuple of ISO
    timestamps so the record stays hashable and frozen; `as_dict` /
    `from_dict` are the JSON shape, refusing unknown keys."""
    state: str = CLOSED
    failure_count: int = 0
    last_trip_ts: str | None = None
    trips_in_window: tuple = ()
    quarantine_reason: str | None = None

    _FIELDS = ("state", "failure_count", "last_trip_ts", "trips_in_window", "quarantine_reason")

    def __post_init__(self):
        if self.state not in BREAKER_STATES:
            raise ValueError(f"state harus salah satu dari {BREAKER_STATES}: {self.state!r}")
        _count("failure_count", self.failure_count)
        if self.last_trip_ts is not None and not isinstance(self.last_trip_ts, str):
            raise ValueError(f"last_trip_ts harus str atau None: {self.last_trip_ts!r}")
        if isinstance(self.trips_in_window, str) or not all(
                isinstance(t, str) for t in self.trips_in_window):
            raise ValueError("trips_in_window harus urutan timestamp (str)")
        object.__setattr__(self, "trips_in_window", tuple(self.trips_in_window))
        if self.quarantine_reason is not None and not isinstance(self.quarantine_reason, str):
            raise ValueError(f"quarantine_reason harus str atau None: {self.quarantine_reason!r}")
        if (self.quarantine_reason is not None) != (self.state == QUARANTINED):
            raise ValueError("quarantine_reason ada jika dan hanya jika state == QUARANTINED")

    def as_dict(self) -> dict:
        return {"state": self.state, "failure_count": self.failure_count,
                "last_trip_ts": self.last_trip_ts,
                "trips_in_window": list(self.trips_in_window),
                "quarantine_reason": self.quarantine_reason}

    @classmethod
    def from_dict(cls, data: dict) -> "CircuitBreakerState":
        if not isinstance(data, dict):
            raise ValueError(f"CircuitBreakerState.from_dict butuh dict: {type(data).__name__}")
        unknown = sorted(set(data) - set(cls._FIELDS))
        if unknown:
            raise ValueError(f"kunci tidak dikenal: {unknown}")
        return cls(**{k: data[k] for k in cls._FIELDS if k in data})


@dataclass(frozen=True)
class PoissonMetrics:
    """One row of the failure-rate report (MEB section 2.1): the rate
    `lambda` per action unit, its MTBF `1 / lambda`, and the reliability
    over 100 actions `e^(-100 lambda)`. Plain data with its invariants: the
    rate is finite and non-negative, the MTBF positive (infinite for a zero
    rate is allowed), the reliability a probability."""
    lambda_rate: float
    mtbf: float
    reliability_100: float

    def __post_init__(self):
        _finite("lambda_rate", self.lambda_rate)
        if self.lambda_rate < 0:
            raise ValueError(f"lambda_rate harus >= 0: {self.lambda_rate!r}")
        if isinstance(self.mtbf, bool) or not isinstance(self.mtbf, (int, float)) or not (
                self.mtbf > 0):
            raise ValueError(f"mtbf harus > 0: {self.mtbf!r}")
        _finite("reliability_100", self.reliability_100)
        if not (0.0 <= self.reliability_100 <= 1.0):
            raise ValueError(f"reliability_100 harus dalam [0, 1]: {self.reliability_100!r}")


@dataclass(frozen=True)
class BreakerVerdict:
    """What a breaker (P1f) or a lane quota (K21) answers: whether the call
    may proceed, the state that decided it, and the reason in one line."""
    allowed: bool
    state: str
    reason: str

    def __post_init__(self):
        if not isinstance(self.allowed, bool):
            raise ValueError(f"allowed harus bool: {self.allowed!r}")
        if self.state not in BREAKER_STATES:
            raise ValueError(f"state harus salah satu dari {BREAKER_STATES}: {self.state!r}")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason harus str yang tidak kosong")
