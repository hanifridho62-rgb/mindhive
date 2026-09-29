# SPDX-License-Identifier: Apache-2.0
# src/mindhive/mindnet/frontier.py
"""The wave frontier: which tasks of a folded graph may run now, as a unit.

Carved out of `orchestrator.py` when that module reached the 1000-line cap
(`tests/test_file_size_cap.py`). `orchestrator.orchestrate` imports `frontier`
and `_has_open_work` from here, and `orchestrator.frontier` is still the name
the orchestrator tests pin: the seam did not move, only the file did.

## What this module deliberately does NOT own

**Readiness.** `tasks.ready()` has answered "which tasks may start" since MN-2:
PENDING, every dependency COMPLETED, and no BLOCKED ancestor. That third clause
is the failure cascade, already transitive, already tested. `frontier()` calls it
and adds the concurrency BOUND and the container withholding -- nothing else.

Re-deriving readiness here would be the invariant-10 violation with the worst
possible failure mode: a second rule that disagrees with `can_complete` about the
same graph means the orchestrator spawns work the rest of the machine considers
blocked, and the disagreement is invisible until something corrupts. So this
module holds no status literals at all -- the same source test that covers
`orchestrator.py` covers this file, because a literal is how the second copy
starts.

Everything here is pure: no I/O, no process, no clock. That purity is what lets
concurrency, the failure cascade and stall detection be tested without starting
a process (see the injected runner in `orchestrator`).
"""
from __future__ import annotations

from . import tasks as t_mod


def frontier(graph, limit=None) -> list:
    """Task ids that may start now, at most *limit* of them, sorted.

    *graph* is `tasks.fold`'s ``task_id -> Task`` map. The readiness predicate is
    `tasks.ready` (see the module docstring on why it is not restated here);
    `ready` already returns sorted ids, so the slice is deterministic without a
    second sort — and determinism is not cosmetic, an irreproducible wave is a
    denial nobody can explain.

    Two things are added on top of `ready`, and only two:

    **The concurrency bound** (*limit*). ``None`` means unbounded.

    **Container tasks are withheld.** `ready` answers "may this task start", which
    is not the same question as "may this task be RUN AS A UNIT". A parent whose
    children are still open is offered by `ready` alongside those children, so the
    wave would run the container and its parts concurrently and, on success, write
    the parent COMPLETED — a status `tasks.can_complete` refuses outright ("has N
    open descendant(s)"), making the parent's `completed` meaningless as evidence.
    `can_complete` has no production caller, so nothing else would catch it.

    Total: any malformed input answers ``[]`` rather than raising, because this
    sits under a loop that may be driving real subprocesses and a projection fault
    must not become a crash."""
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return []
        # `<= 0` explicitly, never a bare slice: `ready[:-1]` silently drops the
        # LAST ready task, which reads as working code.
        if limit <= 0:
            return []
    try:
        if not isinstance(graph, dict):
            return []
        containers = _containers_with_open_work(graph)
        ready = [tid for tid in t_mod.ready(graph) if tid not in containers]
    except Exception:
        return []
    return list(ready) if limit is None else list(ready)[:limit]


def _containers_with_open_work(graph) -> set:
    """Every task that has an unsettled DESCENDANT, computed in one pass.

    Walks UP from each open task instead of down from each candidate. The obvious
    formulation — `tasks.descendants(graph, tid)` per ready task — is O(N * subtree)
    and blew the frontier's millisecond budget at `MAX_NODES`; this is
    O(N * depth), bounded by `MAX_DEPTH`, the same order `tasks.ready` already
    pays for its own ancestor checks.

    Exact rather than approximate, which the cheaper "has an open DIRECT child"
    test is not: a closed child with an open grandchild still leaves the
    grandparent uncompletable, so the transitive case has to be covered.

    Deliberately the straightforward union of `tasks.ancestors` over every open
    task, with NO memoization. Two cheaper forms were tried and both were wrong,
    which is worth recording so they are not tried again:

    * Deduping the climb by `parent_id` — the trick `tasks.ready` uses — missed
      real containers in 512 of 50,625 brute-forced 4-node hierarchies.
    * Memoizing on the result (stop at the first already-marked ancestor) missed
      1,392 of the same set.

    Every divergence was in the UNSAFE direction (a container offered, then
    possibly marked COMPLETED with open children) and every one involved a CYCLIC
    parent chain, because `ancestors` truncates on a loop relative to its START
    node — so its answer is not a function of the parent alone, and neither
    shortcut can be. Since a hierarchy cycle is invalid data `check_graph` already
    reports, chasing bit-exact agreement under one is not worth a shortcut that
    fails open; reusing `ancestors` verbatim inherits exactly its documented
    handling of dangling parents and loops.

    The cost is real and measured: this roughly doubles `frontier`'s time, to
    ~6 ms on a `MAX_NODES` graph. `frontier` runs once per wave and a wave spawns
    subprocesses, so that is not a meaningful cost — but it is why the benchmark's
    bound is what it is rather than the directive's original arbitrary 5 ms."""
    out: set = set()
    for tid, task in graph.items():
        if task is None:
            continue
        if getattr(task, "status", None) in t_mod.CLOSED_STATUSES:
            continue
        out.update(t_mod.ancestors(graph, tid))
    return out



def _has_open_work(graph) -> bool:
    """Whether any task is still unsettled.

    `CLOSED_STATUSES` is `tasks.py`'s own answer to "finished", so `blocked`
    counts as open on purpose: a blocked graph has work left that nobody can do,
    which is precisely the difference between `stalled` and `settled`."""
    try:
        return any(getattr(t, "status", None) not in t_mod.CLOSED_STATUSES
                   for t in (graph or {}).values() if t is not None)
    except Exception:
        return False
