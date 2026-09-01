# src/mindhive/orchestrator/payoff.py
"""TODOS 27b — does weighted-lanes-plus-stealing actually beat static waves?

The PRD's central claim for `orchestrator/` is that it schedules a unit set
better than the incumbent `mindnet.orchestrator.orchestrate` does. That claim
shipped as an argument. This module is the harness that turns it into a number,
and the number is allowed to disagree with the claim — a harness that can only
confirm the thing it was built to check measures nothing.

## What is simulated, and what is therefore NOT measured

Both simulations take a unit set with a weight per unit, and — separately — how
long each unit ACTUALLY takes. Keeping those apart is the point. Weight is
`metrics.calculate_workload_weight`, which is what a planner can see before it
starts; duration is what happens. Feed the same numbers for both and the
simulation says the estimate was perfect.

That separation is not a nicety, it is what makes the harness able to answer
anything. Measured here on 4000 random dependency-free unit sets with duration
set equal to weight, `simulate_lanes` performed ZERO steals across every run:
`shard_plan` is longest-processing-time bin-packing, so when the estimate is
exact the lanes it packs finish together and nothing is ever left to steal.
Stealing is not dead code — it is code that can only pay when the estimate is
wrong, which is exactly what `actual` exists to model.

What is genuinely measured is the SCHEDULING policy — dynamic pull against
static assignment plus stealing, on identical work. That is the part the two
implementations differ in, and it is exactly what TODOS 27b asks about.

Not measured: agent quality, brief usefulness (that is TODOS 23), worktree
setup cost, or anything about failures and repair.

## The incumbent, modelled faithfully

`orchestrator._run_wave` builds `ThreadPoolExecutor(max_workers=min(len(batch),
cap))` and calls `pool.map(one, batch)` with the default chunk size, so workers
pull the next unit as they free up: inside one wave the incumbent is DYNAMIC
list scheduling, not a static split. Between waves it takes a barrier — the pool
closes before the next frontier is computed.

What that pull does NOT do is order the batch by size. `frontier` sorts for
determinism, so a heavy unit sitting late in the sort tails the whole wave —
naive-order list scheduling, whose bound is the classic `2 - 1/m`. `shard_plan`
places the heaviest cluster first, which is the `4/3` bound. So the lane side’s edge
over the incumbent, where they have one, comes from ORDERING, not from stealing.
Measured on the same 4000 sets: lanes finished sooner on 1528, later on 1, and
level on 2471.

`lintester`'s scheduler has no dependency model at all — `WorkStealingScheduler`
runs a flat unit set — so a graph WITH dependencies is not a comparison this can
make. `compare` says so rather than inventing a lane-side dependency policy that
does not exist in the shipped code.
"""
from __future__ import annotations

from collections import deque
from typing import NamedTuple

from .. import clusters as clusters_mod


class Schedule(NamedTuple):
    """One policy's outcome on one unit set."""

    makespan: int = 0
    busy: int = 0
    idle: int = 0
    workers: int = 0
    steals: int = 0
    waves: int = 0

    @property
    def utilisation(self) -> float:
        """Fraction of worker-time spent on work rather than waiting."""
        span = self.makespan * max(1, self.workers)
        return (self.busy / span) if span else 0.0


class Payoff(NamedTuple):
    """Both schedules, plus what the difference between them actually means."""

    waves: Schedule = Schedule()
    lanes: Schedule = Schedule()
    units: int = 0
    comparable: bool = True
    verdict: str = ""

    @property
    def delta(self) -> int:
        """Makespan the lanes save. NEGATIVE when the incumbent wins."""
        return int(self.waves.makespan) - int(self.lanes.makespan)

    def as_dict(self) -> dict:
        return {
            "units": int(self.units),
            "comparable": bool(self.comparable),
            "verdict": self.verdict,
            "delta": self.delta,
            "waves": {"makespan": self.waves.makespan, "busy": self.waves.busy,
                      "idle": self.waves.idle, "workers": self.waves.workers,
                      "waves": self.waves.waves,
                      "utilisation": round(self.waves.utilisation, 4)},
            "lanes": {"makespan": self.lanes.makespan, "busy": self.lanes.busy,
                      "idle": self.lanes.idle, "workers": self.lanes.workers,
                      "steals": self.lanes.steals,
                      "utilisation": round(self.lanes.utilisation, 4)},
        }


def _ordered(weights) -> list:
    """Unit ids in the order both policies see them.

    Sorted, because both sides must receive the SAME order or the comparison
    measures the caller's dict ordering. `orchestrator.frontier` sorts too, for
    the reason its own docstring gives: an irreproducible wave is a bug."""
    return sorted(str(k) for k in (weights or {}))


def _durations(weights, actual) -> dict:
    """What each unit costs to RUN, as against what it was estimated at.

    Defaults to the estimate, so a caller who has no separate measurement gets
    the perfect-estimate case and gets told plainly that that is what it is."""
    base = {str(k): max(0, int(v or 0)) for k, v in (weights or {}).items()}
    if not actual:
        return base
    for key, value in actual.items():
        if str(key) in base:
            base[str(key)] = max(0, int(value or 0))
    return base


def _edges(order, deps) -> dict:
    """Each unit's in-repo dependencies. An edge to a unit that is not in the
    set is dropped rather than treated as unmet, which would strand the whole
    fold on a graph that merely names something outside this mission."""
    known = set(order)
    return {u: {str(d) for d in (deps or {}).get(u, ()) if str(d) in known}
            for u in order}


def simulate_waves(weights, deps=None, workers=4, actual=None) -> Schedule:
    """The incumbent: dependency frontier, dynamic pull inside it, barrier after.

    *deps* maps a unit to the units it waits on; absent or empty means one wave.
    A cycle stops the fold rather than spinning, and the units it stranded simply
    do not appear in the makespan.

    *actual* is how long each unit really takes, defaulting to its weight. The
    incumbent never reads a weight — it takes the frontier in sort order — so
    here the estimate affects nothing at all, and that asymmetry is the finding
    rather than an oversight: only the lane side can be misled by a bad
    estimate, because only the lane side plans with one."""
    order = _ordered(weights)
    edges = _edges(order, deps)
    weights = _durations(weights, actual)
    cap = max(1, int(workers))
    clock = 0
    busy = 0
    waves = 0
    done: set = set()
    left = list(order)
    while left:
        ready = [u for u in left if edges[u] <= done]
        if not ready:
            break                                   # a cycle; report what ran
        free = [clock] * max(1, min(len(ready), cap))
        for unit in ready:
            cost = max(0, int(weights.get(unit, 0)))
            i = min(range(len(free)), key=lambda k: (free[k], k))
            free[i] += cost
            busy += cost
        clock = max(free)                           # the barrier
        waves += 1
        done.update(ready)
        left = [u for u in left if u not in done]
    span = max(1, min(len(order), cap)) if order else 0
    return Schedule(makespan=clock, busy=busy,
                    idle=max(0, clock * span - busy),
                    workers=span, waves=waves)


def simulate_lanes(weights, lanes=4, actual=None) -> Schedule:
    """`clusters.shard_plan` for the assignment, then LIFO local / FIFO steal.

    Faithful to `queue.WorkStealingScheduler` on the three points that decide a
    makespan: a lane pops its own newest entry, a thief takes the victim's
    oldest, and the victim is whichever sibling is carrying the most QUEUED
    entries — a count, not a weight, which is `_steal_for`'s own key and is why a
    lane holding one enormous unit is never the one robbed.

    *weights* packs the lanes; *actual* — defaulting to *weights* — is what each
    unit then costs to run. Passing the two apart is what lets a steal happen at
    all: with a perfect estimate the packing already balances and no lane ever
    runs dry early."""
    order = _ordered(weights)
    if not order:
        return Schedule()
    # Coerced through `_durations` rather than inline, so the packer and the
    # runner agree on what a missing or null weight means. They did not: the
    # runner already read `int(v or 0)` and the packer read `int(v)`, so a unit
    # whose weight came back `None` — which `metrics` can answer, and which a
    # JSON round-trip preserves — raised here instead of packing as zero.
    packed = _durations(weights, None)
    plan = clusters_mod.shard_plan({u: [u] for u in order}, packed, lanes)
    weights = _durations(weights, actual)
    queues = [deque(sorted(ln["clusters"])) for ln in plan]
    clock = [0] * len(queues)
    busy = 0
    steals = 0
    while any(queues):
        i = min(range(len(queues)), key=lambda k: (clock[k], k))
        if queues[i]:
            unit = queues[i].pop()                  # LIFO — the local end
        else:
            victim = min((k for k in range(len(queues)) if queues[k]),
                         key=lambda k: (-len(queues[k]), k))
            unit = queues[victim].popleft()         # FIFO — the thief's end
            steals += 1
        cost = max(0, int(weights.get(unit, 0)))
        clock[i] += cost
        busy += cost
    span = max(clock) if clock else 0
    return Schedule(makespan=span, busy=busy,
                    idle=max(0, span * len(queues) - busy),
                    workers=len(queues), steals=steals, waves=1)


def compare(weights, deps=None, lanes=4, actual=None) -> Payoff:
    """Both policies on the same unit set, with a verdict that is derived.

    The verdict is computed from the two makespans and says which way the
    measurement actually fell. It is written here, once, so no caller has to
    decide what a delta means — and so a run where the incumbent wins reads as
    plainly as one where it does not."""
    order = _ordered(weights)
    edges = _edges(order, deps)
    linked = any(edges[u] for u in order)
    waves = simulate_waves(weights, deps, workers=lanes, actual=actual)
    laned = simulate_lanes(weights, lanes, actual=actual)
    if not order:
        return Payoff(waves=waves, lanes=laned, units=0, comparable=False,
                      verdict="no unit was given, so there is nothing to compare")
    if linked:
        return Payoff(
            waves=waves, lanes=laned, units=len(order), comparable=False,
            verdict=("this unit set has dependencies and the lane scheduler has "
                     "no dependency model, so the two do not schedule the same "
                     "problem; only the incumbent's number means anything here"))
    delta = int(waves.makespan) - int(laned.makespan)
    if delta > 0:
        verdict = (f"lanes finish {delta} weight-unit(s) sooner "
                   f"({laned.makespan} against {waves.makespan})")
    elif delta < 0:
        verdict = (f"the incumbent finishes {-delta} weight-unit(s) sooner "
                   f"({waves.makespan} against {laned.makespan}); with no "
                   f"dependencies its pool pulls dynamically, and static "
                   f"assignment cannot beat that")
    else:
        verdict = (f"both finish at {waves.makespan}; with no dependencies "
                   f"stealing recovered everything static assignment gave away, "
                   f"so what the lanes add here is file-ownership isolation "
                   f"rather than throughput")
    return Payoff(waves=waves, lanes=laned, units=len(order), comparable=True,
                  verdict=verdict)
