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


# --- is the weight any good? --------------------------------------------------
#
# Everything above scores a POLICY given an estimate. This scores the ESTIMATE,
# which is the assumption every number above inherits: `simulate_lanes` packs by
# `metrics.calculate_workload_weight` and then runs for `actual`, and if those
# two are unrelated then the packing is arbitrary and its `4/3` bound buys
# nothing. Until a real mission measured a child's wall time nothing in this
# repository could tell the difference, so `queue.QueueEntry.elapsed_ns` now
# records it and this is what reads it back.

class Sample(NamedTuple):
    """One unit's estimate against the time it really cost.

    *ms* is a FLOAT, and that is load-bearing: `queue.QueueEntry` measures in
    nanoseconds precisely so a unit faster than a millisecond is still a
    measurement, and rounding it to an integer here would throw that away again
    one layer further down — `calibrate` drops a zero as "never measured", so an
    integer `ms` silently deletes the fastest units from the very sample set
    that exists to ask whether the estimate orders them."""

    unit: str = ""
    weight: int = 0
    ms: float = 0.0

    @property
    def ms_per_weight(self) -> float:
        """Milliseconds bought by one weight-unit. ``0.0`` when unweighted."""
        return (self.ms / self.weight) if self.weight > 0 else 0.0


class Calibration(NamedTuple):
    """How well the weight ordered the work, and how sure that is.

    *rho* is Spearman's rank correlation between weight and measured time, and
    it is RANK correlation rather than a plain fit for the reason the packer
    exists: `shard_plan` never reads a weight as a duration, it reads it as an
    order. A weight that is uniformly ten times too large is a perfect estimate
    for this purpose; one that gets the order backwards is a bad estimate no
    matter how close the magnitudes are.

    ``None`` where it cannot be computed rather than 0.0, which would say the
    opposite of what it means: no correlation is a finding, an uncomputable one
    is silence."""

    samples: tuple = ()
    rho: float | None = None
    spread: float = 0.0
    verdict: str = ""

    @property
    def units(self) -> int:
        return len(self.samples)

    def as_dict(self) -> dict:
        return {"units": self.units, "rho": self.rho,
                "spread": round(float(self.spread), 3),
                "verdict": self.verdict,
                "samples": [{"unit": s.unit, "weight": int(s.weight),
                             "ms": round(float(s.ms), 3),
                             "ms_per_weight": round(s.ms_per_weight, 3)}
                            for s in self.samples]}


def _ranks(values) -> list:
    """Ranks, with ties sharing the average of the places they cover.

    Ties are not a detail here: two units estimated at the same weight are the
    common case, and breaking those ties by position would let the ORDER the
    dict happened to arrive in show up as correlation."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = shared
        i = j + 1
    return out


def _pearson(xs, ys):
    """Pearson's r, or ``None`` when either side has no variance."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    vx = sum(d * d for d in dx)
    vy = sum(d * d for d in dy)
    if vx <= 0 or vy <= 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / ((vx ** 0.5) * (vy ** 0.5))


def _reading(rho, spread, n) -> str:
    # The spread is computed whether or not the correlation is, and it is a
    # finding in its own right: units estimated at the SAME weight that cost
    # four times as much as each other say the estimate is wrong about
    # MAGNITUDE, even where nothing can be said about order. Reporting it only
    # alongside a correlation left that finding sitting in the object and out of
    # the one sentence anybody actually reads.
    tail = (f"; the costliest weight-unit ran {spread:.1f}x the cheapest"
            if spread > 0 else "")
    if rho is None:
        return (f"{n} measured unit(s), but weight or time was the same for "
                f"all of them, so nothing about the order can be read{tail}")
    if rho >= 0.7:
        sense = "the weight orders the work"
    elif rho >= 0.3:
        sense = "the weight orders the work loosely"
    elif rho > -0.3:
        sense = "the weight does not predict the order at all"
    else:
        sense = "the weight orders the work BACKWARDS"
    return f"{sense} over {n} measured unit(s), rank correlation {rho:+.2f}{tail}"


def _score(rows) -> Calibration:
    """Score an ORDERED sequence of samples. The half the two entry points share.

    The order it is handed is the order it keeps. Rank correlation does not care
    — but `hive lintester --calibration` prints the TAIL of `samples` as "the
    most recent ones", and a tail taken from a re-sorted list is a different
    dozen entirely."""
    rows = [s for s in (rows or ()) if float(getattr(s, "ms", 0) or 0) > 0]
    if not rows:
        return Calibration(verdict=("no unit reported a measured duration, so "
                                    "the weight is still unchecked"))
    if len(rows) < 2:
        return Calibration(samples=tuple(rows),
                           verdict=("one measured unit cannot show whether the "
                                    "weight orders anything"))
    rho = _pearson(_ranks([s.weight for s in rows]),
                   _ranks([s.ms for s in rows]))
    rates = [s.ms_per_weight for s in rows if s.ms_per_weight > 0]
    spread = (max(rates) / min(rates)) if len(rates) > 1 else 0.0
    return Calibration(samples=tuple(rows), rho=rho, spread=spread,
                       verdict=_reading(rho, spread, len(rows)))


def calibrate(weights, durations) -> Calibration:
    """Score the estimate against measured time. Pure — reads no store.

    A unit is a sample only when BOTH numbers are present. A unit that never
    ran has no measured time, and carrying it as a zero would drag the
    correlation toward whatever the unrun units happened to be estimated at."""
    rows = []
    for unit, ms in sorted((durations or {}).items()):
        if unit not in (weights or {}):
            continue
        try:
            cost = float(ms or 0)
            weight = int((weights or {}).get(unit) or 0)
        except (TypeError, ValueError):
            continue
        if cost <= 0:
            continue
        rows.append(Sample(unit=str(unit), weight=weight, ms=cost))
    return _score(rows)


# --- keeping the samples ------------------------------------------------------

SAMPLES_FILENAME = "orchestrator_calibration.json"

# How many samples the file keeps. Oldest first out: one mission's worth of
# units is a handful, and a cap this size holds many missions while staying a
# file a person can read.
MAX_SAMPLES = 500


def samples_path(project=None):
    from .. import paths
    return paths.project_dir(project) / SAMPLES_FILENAME


def history(project=None) -> tuple:
    """Every sample recorded so far, oldest first. ``()`` when unreadable."""
    import json

    try:
        raw = samples_path(project).read_text(encoding="utf-8")
        doc = json.loads(raw)
        rows = doc.get("samples") if isinstance(doc, dict) else doc
    except (OSError, ValueError, TypeError, AttributeError):
        return ()
    out = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        try:
            out.append(Sample(unit=str(row.get("unit") or ""),
                              weight=int(row.get("weight") or 0),
                              ms=float(row.get("ms") or 0)))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def folded(project=None) -> Calibration:
    """Every recorded sample, scored as one set, in the order it was recorded.

    Scored as a SEQUENCE, never folded through a dict keyed by unit id: the same
    unit is measured again on every mission that touches it, and a dict would
    keep only the last one — quietly turning a history of forty measurements
    into a history of however many distinct ids happened to be in it.

    An earlier cut kept them apart by keying on `f"{i}:{unit}"`, which did stop
    the collapse and cost two things nobody asked to pay: the operator was shown
    `0:writer` instead of `writer`, and sorting those composite keys ordered the
    history lexicographically, so the "most recent samples" the command prints
    were whichever dozen happened to start with a 9."""
    return _score(history(project))


def record_samples(samples, mission_id="", project=None) -> int:
    """Append *samples* to the JSON record. Answers how many are now held.

    Accumulating rather than per-mission, because one mission's four units
    answer nothing about an estimator and forty units across ten missions
    begin to. Total: a store that cannot be written is not a reason to fail a
    mission that already ran."""
    import json

    from .. import paths

    rows = [s for s in (samples or ()) if float(getattr(s, "ms", 0) or 0) > 0]
    if not rows:
        return len(history(project))
    kept = [{"unit": s.unit, "weight": int(s.weight), "ms": round(float(s.ms), 3)}
            for s in history(project)]
    stamp = ""
    try:
        from .. import events as ev_mod
        stamp = ev_mod.now_iso()
    except Exception:                                   # noqa: BLE001
        stamp = ""
    kept += [{"unit": s.unit, "weight": int(s.weight),
              "ms": round(float(s.ms), 3),
              "mission": str(mission_id or ""), "at": stamp} for s in rows]
    kept = kept[-MAX_SAMPLES:]
    try:
        target = samples_path(project)
        target.parent.mkdir(parents=True, exist_ok=True)
        paths.write_text_atomic(target, json.dumps({"samples": kept}, indent=2)
                                + "\n")
    except Exception:                                   # noqa: BLE001
        return len(kept)
    return len(kept)


def record_result(cal, mission_id="", project=None) -> bool:
    """Append the calibration to the ledger as an `experiment_result` row.

    The JSON file above is the working set; this is the durable finding, in the
    store this project treats as its source of truth and in the one type whose
    whole purpose is a measurement somebody took. One row per mission, never one
    per unit — the per-unit numbers live in the file, and forty rows a mission
    would drown the memo stream they would land in."""
    if cal is None or cal.units < 2:
        return False
    try:
        from .. import events as ev_mod
        ev_mod.append_event("experiment_result", {
            "text": f"lintester {mission_id or 'mission'}: {cal.verdict}",
            "rho": cal.rho,
            "units": cal.units,
            "spread": round(float(cal.spread), 3),
        }, tags=["auto", "lintester", "calibration"], project=project)
    except Exception:                                   # noqa: BLE001
        return False
    return True
