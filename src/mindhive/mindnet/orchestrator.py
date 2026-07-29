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

import concurrent.futures as cf
from dataclasses import dataclass, field, replace

from . import executor as x_mod
from . import tasks as t_mod

# How a child's outcome becomes a task status. Enumerated against
# `executor.RUN_RESULTS` rather than written as "ok, else blocked": a result value
# added to that tuple later must fail loudly here instead of being silently
# classified — and the silent classification that matters is a new failure mode
# reading as success, which marks a task COMPLETED on evidence nobody checked.
RESULT_STATUS = {
    "ok": t_mod.STATUS_COMPLETED,
    "fail": t_mod.STATUS_BLOCKED,
    "timeout": t_mod.STATUS_BLOCKED,
    "refused": t_mod.STATUS_BLOCKED,
    "spawn-failed": t_mod.STATUS_BLOCKED,
}

OUTCOME_SETTLED = "settled"      # nothing left to do
OUTCOME_STALLED = "stalled"      # work remains but nothing can advance
OUTCOME_CAPPED = "capped"        # hit MAX_WAVES with work outstanding
OUTCOME_DRY_RUN = "dry-run"      # planned only; no child ever started
OUTCOMES = (OUTCOME_SETTLED, OUTCOME_STALLED, OUTCOME_CAPPED, OUTCOME_DRY_RUN)

# A runaway backstop, not a budget — the same role `MAX_NODES` and
# `MAX_DEPS_PER_TASK` play for the graph. A legal graph can need at most one wave
# per dependency level, so anything at or above `MAX_DEPTH` is generous; a cap a
# legal graph could hit would be a bug wearing a safety rail's clothes.
MAX_WAVES = 64

# Machine-written rows, like `telemetry`'s: `auto` is the taxonomy's existing
# mark for a projection the operator did not type.
STATUS_TAGS = ["mindnet", "task", "auto"]


@dataclass(frozen=True)
class Wave:
    """One round of the loop: what was submitted, and how it came back."""

    index: int
    tasks: list = field(default_factory=list)
    completed: list = field(default_factory=list)
    blocked: list = field(default_factory=list)


@dataclass(frozen=True)
class Orchestration:
    """The whole run. Rendered by the CLI, which never re-derives any of it."""

    outcome: str
    waves: list = field(default_factory=list)
    completed: list = field(default_factory=list)
    blocked: list = field(default_factory=list)


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


# --- running one wave ---------------------------------------------------------

def _mark(graph, completed, blocked) -> dict:
    """*graph* with the named tasks restatused, in memory only.

    The dry run's whole mechanism: it needs later waves to be plannable without
    anything touching the ledger, so progress is simulated on a copy."""
    out = dict(graph)
    for tid in completed:
        if tid in out:
            out[tid] = replace(out[tid], status=t_mod.STATUS_COMPLETED)
    for tid in blocked:
        if tid in out:
            out[tid] = replace(out[tid], status=t_mod.STATUS_BLOCKED)
    return out


def run_key(index, task_id) -> str:
    """The durable-artifact key for *task_id* in wave *index*.

    The task id travels in the RUN KEY, not as a runner keyword. That is not a
    style choice: `executor.execute`'s signature is ``(run_key, script, ...)``
    with no task parameter, and passing one would `TypeError` on every real run
    while a fake runner accepted it happily — a seam whose contract matched only
    the test double. The key is also what names the RunLog artifact, so
    attribution and identity are the same string rather than two that can
    disagree.

    Wave-scoped so a task retried in a later wave cannot overwrite the earlier
    attempt's log. Parsed back with ``split("-", 2)[-1]``, which is safe for
    hyphenated task ids (``mn7-w0-mn1-u7`` -> ``mn1-u7``)."""
    return f"mn7-w{int(index)}-{task_id}"


def _child_env(project=None):
    """The environment overlay every child gets.

    MN-6 made enforcement opt-in per process. A child that did not inherit the
    parent's decision would run under different rules than the orchestrator that
    started it — armed where the operator disarmed, or the reverse. Passed
    explicitly in both directions rather than by omission, because inheriting the
    ambient variable is not the same as inheriting the DECISION: the parent's
    answer also depends on the per-checkout record."""
    from . import optin
    try:
        armed = optin.armed(project)
    except Exception:
        armed = False
    return {optin.ENV_VAR: "1" if armed else "0"}


def _run_wave(batch, graph, runner, index, project=None, timeout_s=None):
    """Submit *batch* concurrently and return ``(completed, blocked)``.

    Concurrency is bounded by the batch itself — `frontier` already applied the
    limit, so the pool is sized to what it returned rather than applying the
    bound twice in two places that could disagree.

    A runner that RAISES blocks its task: `executor.execute` maps its own faults
    to typed results, but a custom runner (or a bug in one) can still throw, and
    one exploding task must not take the orchestration down. An unrecognized
    result VALUE is the opposite case and is re-raised on the main thread — that
    is a programming error in the mapping, not a task failure, and swallowing it
    would mark tasks on evidence nobody classified."""
    env = _child_env(project)
    done, failed, unknown = [], [], []

    def one(tid):
        task = graph.get(tid)
        script = getattr(task, "validation_script", "") or ""
        kwargs = {"env": env, "project": project}
        if timeout_s is not None:
            kwargs["timeout_s"] = timeout_s
        try:
            return tid, runner(run_key(index, tid), script, **kwargs), None
        except Exception as exc:                       # noqa: BLE001 — see above
            return tid, None, f"{type(exc).__name__}: {exc}"

    with cf.ThreadPoolExecutor(max_workers=max(1, len(batch))) as pool:
        results = list(pool.map(one, batch))

    for tid, res, err in results:
        if err is not None or res is None:
            failed.append(tid)
            continue
        status = RESULT_STATUS.get(getattr(res, "result", None))
        if status is None:
            unknown.append((tid, getattr(res, "result", None)))
        elif status == t_mod.STATUS_COMPLETED:
            done.append(tid)
        else:
            failed.append(tid)

    if unknown:
        tid, value = unknown[0]
        raise ValueError(
            f"task '{tid}' returned run result {value!r}, which is not in "
            f"executor.RUN_RESULTS — orchestrator.RESULT_STATUS must classify "
            f"every declared result before it can be acted on")
    return sorted(done), sorted(failed)


def _persist(graph, completed, blocked, project=None) -> dict:
    """Write the wave's outcomes to the ledger and re-fold the graph.

    **One `sidecar.rebuild` per wave, AFTER the appends.** `sidecar._staleness`
    counts every event, so a status row written without rewriting the sidecar
    makes the projection read STALE — which silently disables MN-1's phase-aware
    injection and MN-4's teeth for the very next wave. Rebuilding BEFORE the
    appends, or once per task, both reintroduce that window.

    Every payload goes through `tasks.validate_payload` first: the writer's half
    of the contract, the same discipline the CLI's phase-axis rows are held to.

    Returns the caller's graph advanced by exactly the rows just written, rather
    than a fresh `tasks.fold` of the whole ledger. Two reasons. The cheap one:
    `rebuild` already folds the ledger internally, so re-folding here would make
    it two ~264 ms reads per wave instead of one. The load-bearing one: a re-fold
    silently DISCARDS any graph that did not come from the ledger, and the
    orchestrator is the only writer of these rows — so the fold could not tell us
    anything we did not just write, while quietly losing everything we were
    handed."""
    from .. import events
    from . import sidecar as sc_mod
    rows = ([(tid, t_mod.STATUS_COMPLETED) for tid in completed]
            + [(tid, t_mod.STATUS_BLOCKED) for tid in blocked])
    if not rows:
        return graph
    for tid, status in rows:
        payload = {"action": "status", "task_id": tid, "status": status}
        t_mod.validate_payload(payload)
        events.append_event("mindnet_task", payload, tags=STATUS_TAGS,
                            project=project)
    sc_mod.rebuild(project)
    return _mark(graph, completed, blocked)


# --- the loop -----------------------------------------------------------------

def orchestrate(graph, runner=x_mod.execute, execute=False, limit=None,
                max_waves=MAX_WAVES, project=None, apply_status=None,
                timeout_s=None) -> Orchestration:
    """Walk *graph* wave by wave until it settles, stalls, or hits the cap.

    ``execute=False`` (the DEFAULT) plans without starting anything. That default
    is deliberate and load-bearing: an orchestrator that spawns real agent
    processes is the most dangerous command on this surface, and the safe form
    must be the one you get by accident.

    *runner* and *apply_status* are the two seams. `runner` defaults to
    `executor.execute`; `apply_status` to `_persist`. Injecting them is what makes
    concurrency, the failure cascade, wave re-evaluation and stall detection
    testable without a subprocess or a ledger write.

    Termination compares the graph's STATUSES across a wave rather than counting
    completions. A wave that reports success while nothing actually moved — the
    shape a mis-wired persistence layer produces — would otherwise re-run an
    identical wave until the cap, which is a spin dressed up as progress."""
    g = graph if isinstance(graph, dict) else {}
    if apply_status is None:
        def apply_status(cur, done, failed):
            return _persist(cur, done, failed, project=project)

    waves: list = []
    completed: list = []
    blocked: list = []
    capped = False

    for index in range(max(0, int(max_waves))):
        batch = frontier(g, limit)
        if not batch:
            break
        if not execute:
            # Dry run: record the plan and simulate the optimistic outcome so the
            # waves BEHIND this one are still plannable. Nothing is written.
            waves.append(Wave(index=index, tasks=list(batch)))
            g = _mark(g, batch, [])
            continue

        before = {tid: getattr(t, "status", None) for tid, t in g.items()}
        done, failed = _run_wave(batch, g, runner, index, project=project,
                                 timeout_s=timeout_s)
        waves.append(Wave(index=index, tasks=list(batch), completed=done,
                          blocked=failed))
        completed.extend(done)
        blocked.extend(failed)
        g = apply_status(g, done, failed)
        after = {tid: getattr(t, "status", None)
                 for tid, t in (g or {}).items()}
        if before == after:
            # Nothing moved. Re-running the same wave cannot produce a different
            # answer, so stop rather than burn the cap discovering that.
            break
    else:
        capped = frontier(g, limit) != [] or _has_open_work(g)

    if not execute:
        return Orchestration(outcome=OUTCOME_DRY_RUN, waves=waves)
    if capped:
        outcome = OUTCOME_CAPPED
    elif _has_open_work(g):
        outcome = OUTCOME_STALLED
    else:
        outcome = OUTCOME_SETTLED
    return Orchestration(outcome=outcome, waves=waves, completed=completed,
                         blocked=blocked)


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
