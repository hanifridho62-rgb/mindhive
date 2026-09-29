# SPDX-License-Identifier: Apache-2.0
"""The integration circuit breaker and the per-lane quota (blueprint P1f).

Two controls that both answer `core.models.BreakerVerdict` and are deliberately
NOT the same object:

* `IntegrationBreaker` is a circuit -- it remembers. Three consecutive merge
  failures open it, and while it is open `integrate_lanes` refuses to merge at
  all rather than grinding the integration branch against the same conflict
  every mission. Its state is persisted per project, because the process that
  tripped it is usually not the process that would trip it again.
* `LaneQuota` is a budget -- it forgets. It answers one question about one
  usage number and keeps nothing. It, `LaneUsage` and the `LaneLedger` that
  enforces them live in `quota.py` and are re-exported here BY IDENTITY
  (`breaker.LaneQuota is quota.LaneQuota`), the carve-out this repo already
  uses for `events` -> `contracts` / `core.models`: K21 names them on this
  module, and neither file may pass the MEB's 500-line ceiling.

## Why a SECOND breaker

`orchestrator/resilience.ToolCircuitBreaker` already exists and stays exactly
as it is (decision K1). It guards a TOOL inside one mission: `same_error = 2`,
`no_progress = 3`, `cooldown = 30 s` -- numbers tuned for a loop that retries in
seconds. This one guards INTEGRATION across missions, on MEB 2.2's numbers
(`FAILURE_THRESHOLD = 3`, `COOLDOWN_PERIOD_S = 300`), because a merge that
keeps failing is a human-scale problem and retrying it in thirty seconds only
produces the same conflict faster. Unifying them would have to pick one pair of
numbers and would be wrong for whichever caller lost.

## The quarantine window is this module's decision

MEB 2.2 B gives `p_q`, the probability of the OPEN -> QUARANTINED edge, but no
window. Two numbers close it here and are named so that a reader knows they are
a decision rather than a quotation: `MAX_TRIPS_IN_WINDOW = 3` trips inside
`TRIP_WINDOW_S = 3600.0` quarantines the circuit, which then leaves ONLY by
`manual_reset()`. An hour is chosen because it is long enough to catch "this
lane collides with master every time" and short enough that three unrelated bad
days do not add up to a quarantine.

## Fail-open on its own state

An unreadable or malformed `integration_breaker.json` reads as CLOSED with a
warning on `.warnings`, never as an exception and never as OPEN. A breaker that
refuses every merge because its own sidecar was truncated is a worse outage
than the one it exists to prevent, and it is an outage nothing else in the
system could diagnose.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .. import models, paths
from ..core import locks
from ..core.models import BreakerVerdict, CircuitBreakerState
# By identity, and at the TOP: `quota` imports nothing from here, so there is
# no cycle and either module may be imported first.
from .quota import (QUOTA_REFUSED, LaneLedger,  # noqa: F401
                    LaneQuota, LaneUsage, usage_of)

__all__ = ["STATE_FILENAME", "LOCK_FILENAME", "IntegrationBreaker",
           "LaneUsage", "LaneQuota", "LaneLedger", "QUOTA_REFUSED", "usage_of"]

STATE_FILENAME = "integration_breaker.json"
LOCK_FILENAME = "integration_breaker.lock"

# Fail-open, like every other total in this module: a breaker that cannot take
# its own lock must still record what it saw, because a control that refuses to
# answer is a worse outage than the one it exists to prevent. 5 s is far longer
# than any mutation here takes and shorter than a person's patience; a lock file
# older than a minute belongs to a crashed writer, not a live one.
_LOCK_POLICY = locks.LockPolicy(timeout_s=5.0, stale_s=60.0, fail_open=True)


def _iso(ts: float) -> str:
    """A clock reading as the ISO-shaped string `CircuitBreakerState` stores.
    The clock is a seam (`clock=`), so this must not reach for one of its own:
    the tests drive the order, and the default supplies the meaning.

    That default is `time.time`, not `time.monotonic`. This value is PERSISTED
    and read back by another process -- which is the whole reason the circuit
    has a state file -- and a monotonic reading is an offset from an arbitrary
    origin that restarts at every boot. A stamp written before a reboot came
    back as a trip in the far future: the cooldown never elapsed (measured:
    "percobaan berikutnya dalam 900258s", growing with uptime, until someone ran
    `hive ci breaker reset`) and the stale trip never aged out of the window, so
    the next trip quarantined the circuit at once. Refusing every integration
    because of its own bookkeeping is the outage this module's fail-open rule
    exists to forbid."""
    return f"t{float(ts):.6f}"


def _num(value) -> float:
    return float(value)

class IntegrationBreaker:
    """The four-state integration circuit (MEB 2.2 / ORCH-03).

    `FAILURE_THRESHOLD` consecutive failures trip CLOSED -> OPEN.
    `COOLDOWN_PERIOD_S` past the trip, `check` admits ONE probe (HALF_OPEN);
    that probe closes it or reopens it. `MAX_TRIPS_IN_WINDOW` trips inside
    `TRIP_WINDOW_S` take it to QUARANTINED, which only `manual_reset` leaves.
    The last two are this module's decision, not MEB's -- see the module
    docstring.

    Every transition goes through `models.circuit_breaker_transition`, so the
    DTMC table is the single spelling of the machine and a state this object
    invents would raise rather than persist.
    """

    FAILURE_THRESHOLD = 3
    COOLDOWN_PERIOD_S = 300.0
    MAX_TRIPS_IN_WINDOW = 3
    TRIP_WINDOW_S = 3600.0

    def __init__(self, project=None, clock=time.time):
        self.project = project
        self._clock = clock
        self.warnings: list = []
        # The failure text of the trip THIS process saw. Not persisted: the
        # record keeps the machine's state, the ledger keeps the story
        # (`_anomaly`), and inventing a reason for a trip another process made
        # would be the one thing worse than saying nothing.
        self._reason = ""
        # Re-entrancy depth for `_exclusive`: `_trip` takes two steps through
        # the table and both belong to ONE mutation, under one lock.
        self._held = False
        self._state = self._load()

    # --- persistence ----------------------------------------------------------

    def _path(self):
        return paths.state_dir(self.project) / STATE_FILENAME

    def _lock_path(self):
        """Beside the record it guards, under `paths.state_dir` like every
        other sidecar in the store -- never at the store root."""
        return paths.state_dir(self.project) / LOCK_FILENAME

    @contextmanager
    def _exclusive(self):
        """Hold the record for a WHOLE read-modify-write, and re-read it inside
        the lock.

        `integration_breaker.json` is CROSS-PROCESS state -- that is the entire
        reason it is a file. Each `IntegrationBreaker` loaded a snapshot when it
        was built and wrote the whole record back on every mutation, so two
        missions integrating at once made the second writer publish a state
        that no longer existed: a clean merge erasing a trip recorded moments
        earlier, and with it the cooldown and the quarantine window. Reloading
        inside the lock is what makes the mutation a transition from the state
        that is actually there."""
        if self._held:
            yield
            return
        self._held = True
        try:
            with locks.file_lock(self._lock_path(), _LOCK_POLICY,
                                 name=LOCK_FILENAME):
                self._state = self._load()
                yield
        finally:
            self._held = False

    def _load(self) -> CircuitBreakerState:
        path = self._path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return CircuitBreakerState()
        except OSError as exc:
            self.warnings.append(f"{path.name} tidak terbaca ({exc}); "
                                 "breaker dianggap CLOSED")
            return CircuitBreakerState()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("bukan objek JSON")
            return CircuitBreakerState.from_dict(data)
        except Exception as exc:                            # noqa: BLE001
            self.warnings.append(f"{path.name} rusak ({exc}); "
                                 "breaker dianggap CLOSED")
            return CircuitBreakerState()

    def _save(self) -> None:
        try:
            paths.write_text_atomic(
                self._path(),
                json.dumps(self._state.as_dict(), indent=2, ensure_ascii=False))
        except Exception as exc:                            # noqa: BLE001
            self.warnings.append(f"{STATE_FILENAME} tidak tertulis ({exc})")

    def state(self) -> CircuitBreakerState:
        """The persisted record, as the frozen dataclass (K6)."""
        return self._state

    # --- the machine ----------------------------------------------------------

    def _step(self, event: str, **fields) -> None:
        nxt = models.circuit_breaker_transition(self._state.state, event)
        self._state = CircuitBreakerState(
            state=nxt,
            failure_count=fields.get("failure_count", self._state.failure_count),
            last_trip_ts=fields.get("last_trip_ts", self._state.last_trip_ts),
            trips_in_window=fields.get("trips_in_window",
                                       self._state.trips_in_window),
            quarantine_reason=fields.get("quarantine_reason",
                                         None if nxt != models.QUARANTINED
                                         else self._state.quarantine_reason))
        self._save()

    def _recent_trips(self, now: float) -> tuple:
        """The trips still inside `TRIP_WINDOW_S`. A window that is not swept
        would turn three unrelated incidents a month apart into a quarantine.

        A stamp in the FUTURE is dropped rather than kept. It cannot be a recent
        trip, and `now - ts` for one is negative, which is <= the window -- so a
        clock that went backwards (a reboot under the old monotonic default, or
        a wall clock the operator moved) would have made the window unsweepable
        and quarantined the circuit on its very next trip."""
        out = []
        for stamp in self._state.trips_in_window:
            try:
                ts = float(str(stamp).lstrip("t"))
            except (TypeError, ValueError):
                continue                    # an unreadable stamp is not a trip
            if 0.0 <= now - ts <= self.TRIP_WINDOW_S:
                out.append(stamp)
        return tuple(out)

    def record_failure(self, reason: str = "") -> CircuitBreakerState:
        """One failed integration. The THIRD consecutive one trips the circuit;
        a failure while HALF_OPEN is the probe failing and reopens it at once,
        whatever the count says."""
        with self._exclusive():
            now = _num(self._clock())
            detail = str(reason or "integrasi gagal")[:500]
            if self._state.state == models.HALF_OPEN:
                self._trip(now, detail, probe=True)
                return self._state
            if self._state.state in (models.OPEN, models.QUARANTINED):
                return self._state                  # already refusing
            count = self._state.failure_count + 1
            if count < self.FAILURE_THRESHOLD:
                self._state = CircuitBreakerState(
                    state=self._state.state, failure_count=count,
                    last_trip_ts=self._state.last_trip_ts,
                    trips_in_window=self._state.trips_in_window,
                    quarantine_reason=self._state.quarantine_reason)
                self._save()
                return self._state
            self._trip(now, detail)
            return self._state

    def _trip(self, now: float, detail: str, probe: bool = False) -> None:
        """CLOSED/HALF_OPEN -> OPEN, and then OPEN -> QUARANTINED when this
        trip is the `MAX_TRIPS_IN_WINDOW`-th inside the window. Two steps, both
        through the table: the escalation IS the MEB `p_q` edge, and spelling
        it as its own transition is what makes it visible in the machine rather
        than hidden in an assignment."""
        trips = self._recent_trips(now) + (_iso(now),)
        self._reason = detail
        self._step("probe_fail" if probe else "failure_threshold",
                   failure_count=self.FAILURE_THRESHOLD,
                   last_trip_ts=_iso(now), trips_in_window=trips)
        if len(trips) >= self.MAX_TRIPS_IN_WINDOW:
            self._step("trip_repeat", quarantine_reason=(
                f"{len(trips)} trip dalam {self.TRIP_WINDOW_S:g}s terakhir: "
                + detail))
            self._anomaly(detail, quarantined=True)
            return
        self._anomaly(detail, quarantined=False)

    def record_success(self) -> CircuitBreakerState:
        """A clean integration. From HALF_OPEN it closes the circuit; from
        CLOSED it clears the consecutive-failure count, which is what keeps a
        flaky-but-recovering lane from tripping on noise."""
        with self._exclusive():
            if self._state.state == models.QUARANTINED:
                return self._state
            event = ("probe_ok" if self._state.state == models.HALF_OPEN
                     else "success")
            self._step(event, failure_count=0)
            return self._state

    def cooldown_left(self) -> float:
        """Seconds still to wait before `check` would admit a probe; ``0.0``
        when the circuit is not OPEN or the cooldown has already elapsed.

        Reads only. `check` is a TRANSITION -- an OPEN circuit whose cooldown
        has elapsed moves itself to HALF_OPEN and appends an event as it
        answers -- so an operator asking what the circuit is doing cannot be
        served by it. `hive ci breaker status` is built on this instead. A
        stamp in the future answers 0.0, for the reason `_iso` records."""
        if self._state.state != models.OPEN:
            return 0.0
        waited = _num(self._clock()) - self._trip_at()
        if waited < 0.0:
            return 0.0
        return max(0.0, self.COOLDOWN_PERIOD_S - waited)

    def manual_reset(self) -> CircuitBreakerState:
        """The operator's way out, and the ONLY way out of QUARANTINED
        (`hive ci breaker reset`, P1f -- `ci/cli.py`). The trip history goes
        with it: the operator has looked, so the next trip starts a fresh
        window."""
        with self._exclusive():
            if self._state.state == models.QUARANTINED:
                # Asserted against the table, so a reset out of quarantine
                # stays a transition the machine admits, not an assignment.
                models.circuit_breaker_transition(models.QUARANTINED,
                                                  "manual_reset")
            self._reason = ""
            self._state = CircuitBreakerState()
            self._save()
            return self._state

    def check(self) -> BreakerVerdict:
        """May an integration run now?

        CLOSED and HALF_OPEN allow; OPEN allows once the cooldown has STRICTLY
        elapsed, moving itself to HALF_OPEN as it answers -- the one probe MEB
        2.2 admits. QUARANTINED never allows: there is no cooldown out of it.

        Under the lock, because it is a TRANSITION and not a read: an OPEN
        circuit whose cooldown has elapsed moves itself to HALF_OPEN as it
        answers. `cooldown_left` is the read-only twin for anyone who only
        wants to look."""
        with self._exclusive():
            st = self._state
            if st.state == models.QUARANTINED:
                return BreakerVerdict(
                    allowed=False, state=models.QUARANTINED,
                    reason=("breaker integrasi DIKARANTINA: "
                            + (st.quarantine_reason or "trip berulang")
                            + " -- jalankan hive ci breaker reset setelah "
                              "diperiksa"))
            if st.state == models.OPEN:
                # A NEGATIVE wait means the stamp is in the future, so the
                # clock has gone backwards under a persisted record. Fail open
                # and let the cooldown count as elapsed: one probe integration
                # is the bounded cost, and an OPEN circuit that can never cool
                # down is not.
                waited = _num(self._clock()) - self._trip_at()
                if 0.0 <= waited <= self.COOLDOWN_PERIOD_S:
                    left = self.COOLDOWN_PERIOD_S - waited
                    why = self._reason or "kegagalan integrasi berulang"
                    return BreakerVerdict(
                        allowed=False, state=models.OPEN,
                        reason=(f"breaker integrasi TERBUKA: {why}"
                                f" -- percobaan berikutnya dalam {left:.0f}s"))
                self._step("cooldown_elapsed")
                return BreakerVerdict(
                    allowed=True, state=models.HALF_OPEN,
                    reason="cooldown selesai: satu percobaan integrasi "
                           "diizinkan")
            if st.state == models.HALF_OPEN:
                return BreakerVerdict(allowed=True, state=models.HALF_OPEN,
                                      reason="satu percobaan integrasi "
                                             "diizinkan")
            return BreakerVerdict(allowed=True, state=models.CLOSED,
                                  reason="breaker integrasi tertutup")

    def _trip_at(self) -> float:
        try:
            return float(str(self._state.last_trip_ts or "t0").lstrip("t"))
        except (TypeError, ValueError):
            return 0.0

    def _anomaly(self, detail: str, quarantined: bool) -> None:
        """The half an operator reads. The state file is the machine-readable
        record; this puts the trip where `hive rules` and Oversee already look,
        through the existing `tool_failed` row rather than a new event type."""
        try:
            from . import resilience
            resilience.record_anomaly(
                "integration_breaker",
                "quarantined" if quarantined else "open",
                detail=("breaker integrasi "
                        + ("dikarantina" if quarantined else "terbuka")
                        + f" setelah {self.FAILURE_THRESHOLD} kegagalan: {detail}"),
                project=self.project)
        except Exception:                                   # noqa: BLE001
            pass
