# SPDX-License-Identifier: Apache-2.0
# src/mindhive/orchestrator/quota.py
"""The per-lane budget: the ceiling, and the door that enforces it (P1f, K21).

Split from `breaker.py` because the two answer different questions and because
neither may pass the 500-line ceiling the MEB sets for its own modules. A
circuit REMEMBERS -- `breaker.IntegrationBreaker` persists its state across
processes because the run that tripped it is usually not the run that would
trip it again. A budget FORGETS: `LaneQuota.check` is pure, nothing is
persisted, and being over the ceiling once does not latch. `LaneLedger` is what
remembers for it, and only for the length of one mission.

`breaker.py` re-exports every name here BY IDENTITY, so
`breaker.LaneQuota is quota.LaneQuota` and no caller has to know which file a
name ended up in. The dependency runs one way only -- this module imports
nothing from `breaker` -- so importing either one first is safe.

## What the ceiling can actually see

`usage_of` reads a spend off each unit's outcome, and **an absent figure is
`None`, not zero**. That distinction is the whole reason the report can be
honest: as this lands, `lintester._run_child` answers an
`agenttask._RunOutcome`, which carries `ok`, `reason`, `stderr`, `result`,
`executed` and `exit_code` -- and no token or cost figure at all, because
`mindnet.executor.RunResult` carries `turns` and stops there. So on the default
path `measured()` is False, `hive lintester` prints `usage: unavailable`
instead of a spend, and no lane is ever refused for a number nobody measured.
The ceiling is live the moment a runner reports one: any outcome carrying
`tokens` / `cost_usd`, or `claude_cli`'s own envelope shape
(`usage.input_tokens + usage.output_tokens`, `total_cost_usd`), is counted.
This is stated rather than papered over, because a budget that silently read
every lane as having spent nothing would be a ceiling that can never fire while
looking exactly like one that can.
"""
from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass

from .. import models
from ..core.models import BreakerVerdict

__all__ = ["LaneUsage", "LaneQuota", "LaneLedger", "QUOTA_REFUSED", "usage_of"]


@dataclass(frozen=True)
class LaneUsage:
    """What one lane has spent. Absent usage is ZERO, never unknown: a runner
    that does not report tokens (K21 -- `claude_cli` reports them, a bare
    subprocess runner does not) must not make every lane look over budget."""

    tokens: int = 0
    cost_usd: float = 0.0

    def __post_init__(self):
        if isinstance(self.tokens, bool) or not isinstance(self.tokens, int):
            raise ValueError(f"tokens harus int: {self.tokens!r}")
        if self.tokens < 0:
            raise ValueError(f"tokens tidak boleh negatif: {self.tokens!r}")
        cost = self.cost_usd
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ValueError(f"cost_usd harus angka: {cost!r}")
        if cost != cost or cost in (float("inf"), float("-inf")):
            raise ValueError(f"cost_usd harus berhingga: {cost!r}")
        if cost < 0:
            raise ValueError(f"cost_usd tidak boleh negatif: {cost!r}")
        object.__setattr__(self, "cost_usd", float(cost))

    def plus(self, other: "LaneUsage") -> "LaneUsage":
        """The accumulation an observer does per agent result. Returns a NEW
        record -- the frozen dataclass is the point, so a usage row handed to a
        report cannot be edited underneath it."""
        return LaneUsage(tokens=self.tokens + other.tokens,
                         cost_usd=self.cost_usd + other.cost_usd)


@dataclass(frozen=True)
class LaneQuota:
    """A ceiling on one lane's spend (K21). ``None`` means no ceiling, which is
    the default: a quota nobody configured must never refuse anything.

    Not a circuit. `check` is pure, nothing is persisted, and being over the
    ceiling once does not latch -- the caller decides what to do with the
    verdict, and a later cheaper lane is answered on its own merits."""

    max_tokens: int | None = None
    max_cost_usd: float | None = None

    def __post_init__(self):
        if self.max_tokens is not None:
            if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
                raise ValueError(f"max_tokens harus int atau None: {self.max_tokens!r}")
            if self.max_tokens < 0:
                raise ValueError(f"max_tokens tidak boleh negatif: {self.max_tokens!r}")
        if self.max_cost_usd is not None:
            cost = self.max_cost_usd
            if isinstance(cost, bool) or not isinstance(cost, (int, float)):
                raise ValueError(f"max_cost_usd harus angka atau None: {cost!r}")
            if cost != cost or cost in (float("inf"), float("-inf")):
                raise ValueError(f"max_cost_usd harus berhingga: {cost!r}")
            if cost < 0:
                raise ValueError(f"max_cost_usd tidak boleh negatif: {cost!r}")
            object.__setattr__(self, "max_cost_usd", float(cost))

    @property
    def empty(self) -> bool:
        """No ceiling on either axis -- nothing for a guard to enforce."""
        return self.max_tokens is None and self.max_cost_usd is None

    def check(self, usage: LaneUsage) -> BreakerVerdict:
        """Is *usage* still inside both ceilings? AT the ceiling is inside --
        a budget of 1000 tokens is a permission to spend 1000."""
        if self.max_tokens is not None and usage.tokens > self.max_tokens:
            return BreakerVerdict(
                allowed=False, state=models.OPEN,
                reason=(f"kuota lane terlampaui: {usage.tokens} token melebihi "
                        f"batas {self.max_tokens}"))
        if self.max_cost_usd is not None and usage.cost_usd > self.max_cost_usd:
            return BreakerVerdict(
                allowed=False, state=models.OPEN,
                reason=(f"kuota lane terlampaui: biaya {usage.cost_usd:g} USD "
                        f"melebihi batas {self.max_cost_usd:g} USD"))
        return BreakerVerdict(allowed=True, state=models.CLOSED,
                              reason="lane masih di dalam kuota")


# The `result` string a quota refusal answers with. `refused` is one of
# `executor.RUN_RESULTS` and one of `environment.NEVER_RAN`, which is exactly
# right: no child was created. It is deliberately NOT `spawn-failed`, and the
# reason text deliberately carries no word from `environment._USAGE_HINTS`
# ("usage limit", "rate limit", "quota", ...): `environment.environment_failure`
# reads a refusal's text, and a quota refusal that matched those hints would
# halt every other lane while telling the operator that their ACCOUNT's usage
# limit is exhausted -- a claim about their billing that nothing here checked.
# `tests/test_orchestrator_breaker.py` pins that classification at "".
QUOTA_REFUSED = "refused"


def usage_of(out):
    """The spend *out* reports, or ``None`` when it reports none.

    ``None`` is NOT `LaneUsage()`: one means "this runner does not measure",
    the other means "this unit cost nothing", and a ledger that confused them
    would print a confident 0 for a mission it never measured. Total -- an
    outcome this cannot read answers ``None`` rather than raising into a lane.

    Two shapes are understood, in this order: an outcome carrying `tokens`
    and/or `cost_usd` directly, and `claude_cli`'s envelope, where the tokens
    are `usage.input_tokens + usage.output_tokens` (MEB K21's own spelling) and
    the cost is `total_cost_usd` on either the envelope or the usage mapping.
    Cache fields are left out deliberately: they are what `mindnet.agent`
    measures OCCUPANCY from, and adding them here would bill a lane twice for a
    prompt it only read."""
    try:
        tokens, cost, seen = _direct_usage(out)
        if not seen:
            tokens, cost, seen = _envelope_usage(out)
        if not seen:
            return None
        return LaneUsage(tokens=max(0, int(tokens)), cost_usd=max(0.0, float(cost)))
    except Exception:                                   # noqa: BLE001
        return None


def _field(obj, name):
    """*name* off an attribute or a mapping key, or ``None``."""
    got = getattr(obj, name, None)
    if got is None and isinstance(obj, dict):
        got = obj.get(name)
    return got


def _number(value) -> float:
    """*value* as a finite number, or ``0.0``. Booleans are not numbers here."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return float(value)


def _direct_usage(out) -> tuple:
    """``(tokens, cost, seen)`` from an outcome that reports its own spend."""
    raw_tokens = _field(out, "tokens")
    raw_cost = _field(out, "cost_usd")
    seen = any(v is not None and not isinstance(v, bool) and isinstance(v, (int, float))
               for v in (raw_tokens, raw_cost))
    return _number(raw_tokens), _number(raw_cost), seen


def _envelope_usage(out) -> tuple:
    """``(tokens, cost, seen)`` from `claude_cli`'s envelope shape."""
    usage = _field(out, "usage")
    cost = _field(out, "total_cost_usd")
    if cost is None and isinstance(usage, dict):
        cost = usage.get("total_cost_usd")
    if not isinstance(usage, dict):
        return 0.0, _number(cost), cost is not None and not isinstance(cost, bool)
    tokens = _number(usage.get("input_tokens")) + _number(usage.get("output_tokens"))
    return tokens, _number(cost), True


class LaneLedger:
    """What each lane has spent, and the door that enforces *quota* with it.

    Thread-safe: `queue.WorkStealingScheduler` runs lanes on executor threads
    and a stolen unit is run by a lane that is not its own, so both the read
    and the accumulate happen under one lock.

    Deliberately NOT persisted. A quota is a ceiling on ONE mission -- a
    carried-over total would refuse a fresh mission for what a previous one
    spent, which is the latching behaviour `LaneQuota`'s docstring rules out."""

    def __init__(self, quota=None, *, reader=None):
        self.quota = quota
        self._read = reader or usage_of
        self._spent: dict = {}
        self._measured = False
        self._refusals: list = []
        self._lock = threading.Lock()

    def spent(self, lane_id) -> LaneUsage:
        """What lane *lane_id* has spent so far."""
        with self._lock:
            return self._spent.get(_lane_of(lane_id), LaneUsage())

    def measured(self) -> bool:
        """Whether ANY outcome so far reported a spend at all. False means the
        totals are unmeasured, not zero -- see the module docstring."""
        with self._lock:
            return self._measured

    def totals(self) -> dict:
        """``{lane_id: LaneUsage}``, copied, for the report."""
        with self._lock:
            return dict(self._spent)

    def refusals(self) -> tuple:
        """``(lane_id, task_id, reason)`` for every unit this ledger refused."""
        with self._lock:
            return tuple(self._refusals)

    def record(self, lane_id, out) -> bool:
        """Add what *out* reports to *lane_id*'s total. True if it reported
        anything at all.

        Total. The shipped `usage_of` never raises, but *reader* is a seam and
        this runs on the scheduler's own path: a reader that threw would take
        down a lane over a bookkeeping figure, turning a budget that cannot
        measure into a mission that cannot run."""
        try:
            usage = self._read(out)
        except Exception:                               # noqa: BLE001
            return False
        if usage is None:
            return False
        key = _lane_of(lane_id)
        with self._lock:
            self._spent[key] = self._spent.get(key, LaneUsage()).plus(usage)
            self._measured = True
        return True

    def check(self, lane_id) -> BreakerVerdict:
        """May lane *lane_id* take another unit?

        Allowed whenever there is no quota, or nothing has been measured: a
        ceiling must not refuse a lane on a number that was never read."""
        if self.quota is None or self.quota.empty:
            return BreakerVerdict(allowed=True, state=models.CLOSED,
                                  reason="tidak ada kuota lane")
        if not self.measured():
            return BreakerVerdict(allowed=True, state=models.CLOSED,
                                  reason="kuota lane tidak terukur di jalur ini")
        return self.quota.check(self.spent(lane_id))

    def guard(self, runner):
        """*runner*, refusing any unit on a lane that is already past *quota*.

        The check is BEFORE the child, which is the only place it can save
        anything: a ceiling tested after the spend has already happened is a
        report, not a budget. The refusal is `QUOTA_REFUSED` with
        ``executed=False``, so the pipeline records the plain truth -- no child
        was created -- and the OTHER lanes keep running, because a lane's
        budget is that lane's.

        Returns *runner* itself when there is nothing to enforce, so the
        default `hive lintester` run makes byte for byte the call it always
        made (the shape `_limits`/`_spawn` already use for the fence).

        A STOLEN unit is billed to the lane that ran it, not the lane it was
        queued on: `queue.WorkStealingScheduler` hands the thief's own
        `WorkerLane` to the runner, and this keys on that. That is the reading
        the flag's name asks for -- a ceiling on what one lane may SPEND -- and
        it is the only one that can be enforced before the work, since the
        queue a unit came from says nothing about who will pay for it."""
        if self.quota is None or self.quota.empty:
            return runner

        def _wrapped(entry, lane):
            verdict = self.check(getattr(lane, "lane_id", None))
            if not verdict.allowed:
                return self._refuse(entry, lane, verdict)
            out = runner(entry, lane)
            self.record(getattr(lane, "lane_id", None), out)
            return out

        if inspect.iscoroutinefunction(runner):
            async def _wrapped_async(entry, lane):
                verdict = self.check(getattr(lane, "lane_id", None))
                if not verdict.allowed:
                    return self._refuse(entry, lane, verdict)
                out = await runner(entry, lane)
                self.record(getattr(lane, "lane_id", None), out)
                return out
            return _wrapped_async
        return _wrapped

    def _refuse(self, entry, lane, verdict):
        """The outcome a unit gets when its lane is out of budget."""
        from .agenttask import _RunOutcome

        task_id = str(getattr(getattr(entry, "task", None), "task_id", "") or "")
        with self._lock:
            self._refusals.append(
                (_lane_of(getattr(lane, "lane_id", None)), task_id, verdict.reason))
        return _RunOutcome(ok=False, result=QUOTA_REFUSED, executed=False,
                           reason=verdict.reason, stderr="")


def _lane_of(lane_id) -> int:
    """*lane_id* as the int the lanes key on; an unreadable one is ``-1``."""
    try:
        return int(lane_id)
    except (TypeError, ValueError):
        return -1
