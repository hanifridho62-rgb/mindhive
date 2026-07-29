# src/mindhive/mindnet/orchestrator.py
"""MN-7 — the parallel orchestrator: the loop that actually walks the graph.

MN-2 built the hierarchical task graph. MN-1 built the phase spine over it. MN-0
made a parallel run's output survive its own failure. Nothing read the graph and
*ran* it — that is this module.

## What this module deliberately does NOT own

**Readiness.** `tasks.ready()` has answered "which tasks may start" since MN-2:
PENDING, every dependency COMPLETED, and no BLOCKED ancestor. That third clause
is the failure cascade, already transitive, already tested. `frontier()` calls it
and adds the concurrency BOUND — nothing else.

Re-deriving readiness here would be the invariant-10 violation with the worst
possible failure mode: a second rule that disagrees with `can_complete` about the
same graph means the orchestrator spawns work the rest of the machine considers
blocked, and the disagreement is invisible until something corrupts. So this
module holds no status literals at all — a test over its own source enforces that,
because a literal is how the second copy starts.

**Spawning.** `executor.execute` already does it correctly: `shlex.split(posix=
False)` -> argv with `shell=False` (never a shell), a timeout cap, exceptions
narrowed to `(OSError, subprocess.SubprocessError)`, a closed `RUN_RESULTS`
vocabulary, and a durable `RunLog` written as the child runs — MN-0's guarantee
that a failed child's output is not lost. The "non-zero exit trap" is therefore
not new code here; it is reading `RunResult.result`.

## Threads, not asyncio

The work is subprocess-bound and `executor.execute` is a blocking
`subprocess.run`. A `ThreadPoolExecutor` reuses that call verbatim; asyncio would
require a second implementation of the whole run path — its shlex handling, its
timeout, its exception narrowing, its RunLog writes — for no throughput gain on
process-bound work.

## The injected runner

`orchestrate(runner=...)` defaults to `executor.execute`. That one seam is why
concurrency, the failure cascade, wave re-evaluation and stall detection are all
testable without starting a process, and it is why `frontier` is kept pure.
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

    ``limit=None`` means unbounded. Total: any malformed input answers ``[]``
    rather than raising, because this sits under a loop that may be driving real
    subprocesses and a projection fault must not become a crash."""
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
        ready = t_mod.ready(graph)
    except Exception:
        return []
    return list(ready) if limit is None else list(ready)[:limit]
