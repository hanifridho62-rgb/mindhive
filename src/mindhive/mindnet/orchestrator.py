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
import os
from dataclasses import dataclass, field, replace

from . import executor as x_mod
from . import tasks as t_mod

# How a child's outcome becomes a task status — consulted ONLY when a child
# actually ran. Enumerated rather than written as "ok, else blocked": a result
# value added to `executor.RUN_RESULTS` later must fail loudly here instead of
# being silently classified, and the silent classification that matters is a new
# failure mode reading as success, marking a task COMPLETED on evidence nobody
# checked.
RESULT_STATUS = {
    "ok": t_mod.STATUS_COMPLETED,
    "fail": t_mod.STATUS_BLOCKED,
}

# Results where NO CHILD PROCESS WAS EVER CREATED. These must never produce a
# status row, and the distinction is the difference between a recoverable state
# and a poisoned graph.
#
# `refused` is what the gate returns for a task with no `validation_script` — the
# shape `hive mindnet declare` produces BY DEFAULT — and also for a destructive
# command. `spawn-failed` is the OS refusing to start the process. Mapping either
# to `blocked`, as this first did, was a P0: `tasks.ready` excludes every task
# with a BLOCKED ancestor and every task whose dependency is not COMPLETED, so one
# script-less task killed its whole subtree AND its dependents — permanently,
# because `orchestrator._persist` is the only writer of `action: "status"` rows in
# the codebase and a re-declare is forbidden from resetting status
# (`tasks.py`: "NEVER reset accumulated status"). There was no CLI remedy at all.
#
# Keyed on `RunResult.executed`, not on the result string. That field is the
# executor's own deliberately-non-derivable signal for "a process was created"
# ("``executed`` is the SECURITY property and is deliberately not derivable from
# ``result``"), so it is the honest axis for "did anything actually happen".
NON_EXECUTING_RESULTS = frozenset({"refused", "spawn-failed"})

# Results that write NO STATUS ROW even though a child DID run. Distinct from
# `NON_EXECUTING_RESULTS`: there the run never happened, here it happened and its
# outcome is not evidence of anything.
#
# `agent.RESULT_RAN_UNVERIFIED` — an agent replied. `RESULT_STATUS["ok"] ->
# COMPLETED` was written for a validation command, where exit 0 means a check
# passed; an implementer's exit 0 only means the model answered, and `prompt_for`
# explicitly invites the failing reply. Marking that COMPLETED unblocked
# dependents and reported `settled` for work nobody did.
#
# `timeout` — the agent was killed on wall-clock. That is an environmental
# condition (a slow model call, a sleeping laptop, a task that needed 40 minutes),
# and `blocked` would delete the task's whole subtree from every future frontier
# permanently. The isolation path already preserves the evidence: a timed-out tree
# is dirty, so `retire` keeps it and reports the path.
UNVERIFIED_RESULTS = frozenset({"ran-unverified", "timeout"})

OUTCOME_SETTLED = "settled"      # nothing left to do
OUTCOME_STALLED = "stalled"      # work remains but nothing can advance
OUTCOME_CAPPED = "capped"        # hit MAX_WAVES with work outstanding
OUTCOME_DRY_RUN = "dry-run"      # planned only; no child ever started
# The founding prompt asked for parallelism only "jika usage memungkinkan". The CLI
# gives no way to ask before spawning, so this is the retrospective half: the moment
# one agent reports a quota/auth condition, stop. Eight tasks against an exhausted
# weekly limit produce eight identical failures and the ninth teaches nobody
# anything — and unlike a task failure, nothing about waiting is going to change by
# running more of them now.
OUTCOME_USAGE = "usage-halted"
OUTCOMES = (OUTCOME_SETTLED, OUTCOME_STALLED, OUTCOME_CAPPED, OUTCOME_DRY_RUN,
            OUTCOME_USAGE)

# A runaway backstop, not a budget — the same role `MAX_NODES` and
# `MAX_DEPS_PER_TASK` play for the graph. A legal graph can need at most one wave
# per dependency level, so anything at or above `MAX_DEPTH` is generous; a cap a
# legal graph could hit would be a bug wearing a safety rail's clothes.
MAX_WAVES = 64

# Ceiling on concurrent children per wave, independent of how wide the frontier
# is. Without it `max_workers` was the batch size, so a graph with a wide
# frontier (up to `MAX_NODES`, 2000) would spawn one thread and one child process
# per ready task — thousands of subprocesses from a single command, by accident.
#
# This bounds EXECUTION, not planning: the wave still contains every ready task
# and they queue through the pool. `--limit` is the other knob and means something
# different — it truncates the frontier, deciding which tasks run in this wave at
# all. Floor of 2 so a single-core CI box still runs a wave concurrently.
DEFAULT_MAX_WORKERS = max(2, os.cpu_count() or 2)

# Machine-written rows, like `telemetry`'s: `auto` is the taxonomy's existing
# mark for a projection the operator did not type.
STATUS_TAGS = ["mindnet", "task", "auto"]

# The faculty an orchestrated wave lights up. `task-graph` rather than a new name:
# `telemetry.FACULTIES` is the one legend and walking the graph is exactly what
# that faculty means.
_WAVE_FACULTY = "task-graph"
# The completion gate's own faculty — the cat for the founding prompt's last stage.
# Named from the legend, never a second spelling of it: writing that stage's name with a
# space instead of a hyphen is itself the invariant-10 violation, and the guard caught me
# doing it here twice.
_GATE_FACULTY = "pr-green"


@dataclass(frozen=True)
class Wave:
    """One round of the loop: what was submitted, and how it came back.

    ``reasons`` maps a settled task to WHY it settled that way. Carried because
    the alternative was discarding it: `_run_wave` receives a full `RunResult`
    per child — with its reason, exit code and durable artifact path — and a
    result that reduced to a bare id list left an operator with "blocked: a" and
    no way to find out why, while the CLI cheerfully suggested inspecting a task
    that had nothing recorded against it."""

    index: int
    tasks: list = field(default_factory=list)
    completed: list = field(default_factory=list)
    blocked: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    reasons: dict = field(default_factory=dict)
    # MN-9, isolated runs only: task_id -> the branch its agent worked on, and
    # task_id -> a worktree path `retire` REFUSED to delete because it still held
    # work. The second is the one an operator has to act on.
    branches: dict = field(default_factory=dict)
    kept: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Orchestration:
    """The whole run. Rendered by the CLI, which never re-derives any of it."""

    outcome: str
    waves: list = field(default_factory=list)
    completed: list = field(default_factory=list)
    blocked: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    reasons: dict = field(default_factory=dict)
    branches: dict = field(default_factory=dict)
    kept: dict = field(default_factory=dict)


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


def _ran(res) -> bool:
    """Whether a child process was actually created for this run.

    Delegates to `agent.ran`, which owns the field and therefore the question. This
    wrapper exists only so the wave loop reads locally and so the import stays lazy
    — a caller supplying its own runner must not be made to import the agent module
    just to have its results classified."""
    from . import agent as ag_ran
    return ag_ran.ran(res)


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


def validation_script_of(task) -> str:
    """The task's stored validation command — the payload `executor.execute` wants.

    Paired with `agent.prompt_for`, which is the payload `agent.run` wants. The
    two runners take the same POSITION but not the same content, so the pairing is
    explicit at the call site rather than guessed from the runner."""
    return getattr(task, "validation_script", "") or ""


def _batch_phase(batch, graph):
    """The declared phase of the batch's representative task, or None.

    None rather than a guess when the batch is empty or the task carries no
    phase: a cat lit for work with no phase must not claim one, and `emit`
    already treats None as absent."""
    if not batch:
        return None
    task = (graph or {}).get(batch[0])
    return getattr(task, "phase", None) or None


def _run_wave(batch, graph, runner, index, project=None, timeout_s=None,
              max_workers=None, root=None, payload_for=None, isolate=False,
              runner_kwargs=None):
    """Submit *batch* concurrently; return ``(completed, blocked, skipped,
    reasons)``.

    **A status row is written only for a task whose child actually RAN.** Tasks
    whose run never started come back as `skipped` and keep their PENDING status —
    see `NON_EXECUTING_RESULTS` for the P0 that made this necessary. The loop's
    no-progress guard then terminates on them and reports `stalled` with the
    reason attached, which is recoverable; a `blocked` row is not.

    *root* is resolved ONCE by the caller and passed in. Letting each child fall
    back to `executor.execute`'s own `paths.worktree_root()` meant one `git
    rev-parse` subprocess per task, fired concurrently — and if git was absent,
    slow past its 2s timeout, or the cwd was not a repo, EVERY task in the wave
    came back refused. Before the fix above that poisoned the whole graph from a
    transient environmental condition.

    **The pool is capped, the batch is not.** `frontier` decides WHICH tasks run
    this wave; `DEFAULT_MAX_WORKERS` decides how many run at once. Sizing the pool
    to the batch, as this first did, meant a wide frontier spawned one thread and
    one child process per ready task — thousands of subprocesses from one command.
    The two knobs are deliberately separate: a wave still contains every ready
    task, they just queue.

    A runner that RAISES blocks its task: `executor.execute` maps its own faults
    to typed results, but a custom runner (or a bug in one) can still throw, and
    one exploding task must not take the orchestration down. An unrecognized
    result VALUE is the opposite case and is re-raised on the main thread — that
    is a programming error in the mapping, not a task failure, and swallowing it
    would mark tasks on evidence nobody classified.

    ``reasons`` keeps each child's own account of itself — the reason string and,
    when there is one, the durable artifact path. Dropping it left "blocked: a"
    as the only thing an operator ever saw."""
    env = _child_env(project)
    done, failed, skipped, unknown, reasons = [], [], [], [], {}
    cap = DEFAULT_MAX_WORKERS if max_workers is None else max(1, int(max_workers))

    payload_of = payload_for or validation_script_of
    isolated: dict = {}

    def one(tid):
        task = graph.get(tid)
        script = payload_of(task)
        # `runner_kwargs` carries what only SOME runners understand — the agent
        # runner's smart-zone config, for instance, which `executor.execute`
        # has no parameter for. Passed by the caller that chose the runner, so
        # the two can never disagree about which extras are legal.
        kwargs = {"env": dict(env), "project": project}
        kwargs.update(runner_kwargs or {})
        task_root = root
        iso = None

        if isolate:
            from . import worktree as wt_mod
            try:
                iso = wt_mod.add(root, tid)
            except Exception as exc:                   # noqa: BLE001
                # `worktree.add` promises to answer a value, but this sits OUTSIDE
                # the try below, so a raise here escapes `one()`, re-raises out of
                # `pool.map`, and discards the results of every sibling agent that
                # already ran — re-running then duplicates real agent work.
                iso = wt_mod.Isolated(
                    task_id=tid, ok=False,
                    reason=f"isolation raised {type(exc).__name__}: {exc}")
            isolated[tid] = iso
            if not iso.ok:
                # An environmental failure, so it must NOT write a status row.
                # Reusing the `refused`/executed=False shape means MN-8's existing
                # classification maps it to `skipped` — no new branch in the
                # mapping, and the MN-7 P0 lesson holds by construction.
                return tid, x_mod.RunResult(
                    result="refused", executed=False, run_key=run_key(index, tid),
                    reason=f"could not isolate a checkout: {iso.reason}"), None
            task_root = iso.path
            # Its OWN ledger segment. `events.append_event` appends to
            # `<session_id>.jsonl` unlocked, so concurrent agents sharing a
            # session interleave into corrupt JSONL — and the ledger is the
            # source of truth for everything.
            kwargs["env"]["MINDHIVE_SESSION"] = f"{wt_mod.PREFIX}-{tid}"

        if task_root is not None:
            kwargs["root"] = task_root
        if timeout_s is not None:
            kwargs["timeout_s"] = timeout_s
        try:
            return tid, runner(run_key(index, tid), script, **kwargs), None
        except Exception as exc:                       # noqa: BLE001 — see above
            return tid, None, f"{type(exc).__name__}: {exc}"
        finally:
            if iso is not None and iso.ok:
                # `finally`: a worktree left behind by a crashed run accumulates
                # and blocks the next attempt for that task. `retire` refuses to
                # delete anything holding work, so this is safe to run
                # unconditionally.
                #
                # Guarded, because a raise in a `finally` REPLACES the result the
                # runner already produced — the agent's real outcome would be lost
                # and reported as a cleanup error.
                try:
                    from . import worktree as wt_mod2
                    kept = wt_mod2.retire(root, iso.path, iso.branch, iso.base)
                    if kept.kept:
                        isolated[tid] = replace(iso, reason=kept.reason)
                except Exception as exc:               # noqa: BLE001
                    isolated[tid] = replace(
                        iso, reason=f"kept {iso.path}: cleanup raised "
                                    f"{type(exc).__name__}: {exc}")

    # The cat board exists to make parallel work visible, and an orchestrated
    # wave IS the parallel work — but nothing here emitted the feed, so the board
    # stayed empty during the one activity it was built for. `busy` brackets the
    # wave (active on the way in, idle on the way out, even on an exception), so
    # the `task-graph` cat is lit for exactly as long as the wave runs.
    from . import telemetry as tel_mod
    # The phase of the SAME task `task_id` names, so the cat's two fields
    # describe one task instead of two. Read off the graph rather than
    # invented: `telemetry.fold` drops any phase outside `tasks.PHASES`, so a
    # made-up label would vanish between the write and the panel and read as
    # if nothing had been emitted.
    with tel_mod.busy(_WAVE_FACULTY, task_id=batch[0] if batch else None,
                      phase=_batch_phase(batch, graph), project=project):
        with cf.ThreadPoolExecutor(
                max_workers=max(1, min(len(batch), cap))) as pool:
            results = list(pool.map(one, batch))

    for tid, res, err in results:
        if err is not None or res is None:
            failed.append(tid)
            reasons[tid] = err or "the runner returned nothing"
            continue
        value = getattr(res, "result", None)
        detail = str(getattr(res, "reason", "") or "").strip()
        artifact = getattr(res, "artifact", None)
        if detail or artifact:
            reasons[tid] = " · ".join(
                p for p in (detail, f"log: {artifact}" if artifact else "") if p)
        # Unknown FIRST, so the mapping-completeness guarantee holds regardless of
        # whether the run executed.
        if (value not in RESULT_STATUS and value not in NON_EXECUTING_RESULTS
                and value not in UNVERIFIED_RESULTS):
            unknown.append((tid, value))
            continue
        if not _ran(res):
            # Nothing ran. Leave the task PENDING — a status row here is the P0.
            skipped.append(tid)
            continue
        if value in UNVERIFIED_RESULTS:
            # It ran, and its outcome is not evidence of anything. No status row:
            # an agent's say-so must not mark a task COMPLETED, and a wall-clock
            # timeout must not mark it BLOCKED and delete its whole subtree.
            skipped.append(tid)
            continue
        if RESULT_STATUS[value] == t_mod.STATUS_COMPLETED:
            done.append(tid)
        else:
            failed.append(tid)

    if unknown:
        tid, value = unknown[0]
        raise ValueError(
            f"task '{tid}' returned run result {value!r}, which is not in "
            f"executor.RUN_RESULTS — orchestrator.RESULT_STATUS and "
            f"NON_EXECUTING_RESULTS must together classify every declared result "
            f"before it can be acted on")
    from . import agent as ag_check
    starved = any(ag_check.is_usage_limit(r) for _t, r, _e in results
                  if r is not None)
    return (sorted(done), sorted(failed), sorted(skipped), reasons,
            isolated, starved)


def _refresh(project=None) -> None:
    """Rebuild the sidecar once, and RAISE if it refused.

    `rebuild` signals refusal with ``written=False`` and never raises — by design,
    it is wired into a hook path. Discarding that answer meant a failed projection
    refresh was silently swallowed and the next wave read a stale sidecar."""
    from . import sidecar as sc_mod
    out = sc_mod.rebuild(project)
    if not out.written:
        raise RuntimeError(
            f"the sidecar could not be rebuilt: {out.reason} — the projection is "
            f"now STALE, which disables phase-aware injection and the PreToolUse "
            f"teeth until `hive mindnet rebuild` succeeds")


def completion_gate_default(graph, task_id, project=None, records=None):
    """``None`` when *task_id* may be marked COMPLETED, else the blocking reason.

    **This is the wire the original ask was missing.** MindNetwork's founding
    requirement said a task "tidak akan selesai hingga pr berwarna hijau" — it is
    not finished until the PR is green — and `tasks.can_complete` implements
    exactly that: no open descendants, evidence recorded, and the terminal phase
    (`pr-green`, or `TRIVIAL_EXIT_PHASE` for an ADR-0005 bypass) satisfied.

    Nothing called it. Every reference to `can_complete` in `src/` was a comment
    or a docstring, so the only writer of a `completed` row — `_persist` — marked
    tasks finished without ever asking. A task could be COMPLETED at `brainstorm`,
    which unblocks its dependents on a promise the machine never checked.

    The satisfaction predicate comes from the SIDECAR, not a fresh ledger fold:
    `_persist` rebuilds the sidecar after every wave, so it is current by
    construction, and a file read costs nothing next to the ~264 ms fold. A stale
    or missing sidecar yields no records, the predicate answers False, and the gate
    REFUSES — which is the direction a gate must fail in."""
    if records is None:
        try:
            from . import sidecar as sc_mod
            sc = sc_mod.read(project)
            records = getattr(getattr(sc, "state", None), "records", None) or {}
        except Exception:
            records = {}
    from . import controller as c_mod
    # MN-17: the gate is the pr-green faculty actually doing its job, so it announces
    # itself. `busy` rather than a hand-written pair, because the check runs
    # inline here and the `finally` guarantees the cat goes idle however the gate
    # ends — including on the exception path below, which is exactly where a cat stuck
    # awake forever would be most misleading.
    from . import telemetry as tel_mod
    # Same rule as the wave: the phase of the task being gated, not the name
    # of the gate. Restating the faculty in its own phase row tells the reader
    # nothing they cannot see from the cat.
    with tel_mod.busy(_GATE_FACULTY, task_id=task_id,
                      phase=_batch_phase([task_id], graph), project=project):
        try:
            return t_mod.can_complete(graph, task_id,
                                      c_mod.satisfaction_predicate(records))
        except Exception as exc:                        # noqa: BLE001
            # A fault in the gate is not a pass. `can_complete` fails closed on a
            # raising predicate for the same reason.
            return (f"'{task_id}' cannot complete: the completion gate raised "
                    f"{type(exc).__name__}: {exc}")


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
    handed.

    A failing append does not abandon the rest of the wave. The tasks already
    RAN; aborting the loop would leave some settled tasks with no ledger row at
    all, so a retry would silently run them a second time — and skipping the
    rebuild on the way out would leave the sidecar stale on top of the rows that
    *did* land, which is the exact condition this function exists to prevent. So
    every row is attempted, the rebuild always runs, and the failures are raised
    together afterwards naming precisely which tasks went unrecorded.

    `rebuild`'s answer is CHECKED. It signals refusal with ``written=False`` and
    never raises (by design — it is wired into a hook path), so discarding the
    return value, as this first did, meant a failed projection refresh was
    silently swallowed and the next wave read a stale sidecar: MN-6's lesson,
    reintroduced by the code documenting it."""
    from .. import events
    from . import sidecar as sc_mod
    rows = ([(tid, t_mod.STATUS_COMPLETED) for tid in completed]
            + [(tid, t_mod.STATUS_BLOCKED) for tid in blocked])
    if not rows:
        # No STATUS rows — but the wave still appended the cat feed's activity
        # rows, and `_staleness` counts EVERY event. Returning here without a
        # rebuild would leave the projection stale on a wave that settled nothing,
        # which is the MN-6 failure exactly: MN-1 injection and MN-4's teeth go
        # quiet until something else happens to rebuild.
        _refresh(project)
        return graph
    unrecorded = []
    for tid, status in rows:
        payload = {"action": "status", "task_id": tid, "status": status}
        try:
            t_mod.validate_payload(payload)
            events.append_event("mindnet_task", payload, tags=STATUS_TAGS,
                                project=project)
        except Exception as exc:                       # noqa: BLE001 — see above
            unrecorded.append(f"{tid} ({type(exc).__name__}: {exc})")
    _refresh(project)
    if unrecorded:
        raise RuntimeError(
            "these tasks RAN but their status could not be recorded: "
            + ", ".join(unrecorded)
            + " — re-running the orchestrator would execute them again; "
              "record them by hand or repair the ledger first")
    return _mark(graph, completed, blocked)


# --- the loop -----------------------------------------------------------------

def orchestrate(graph, runner=x_mod.execute, execute=False, limit=None,
                max_waves=None, project=None, apply_status=None,
                timeout_s=None, max_workers=None, root=None,
                payload_for=None, isolate=False,
                completion_gate=None, runner_kwargs=None) -> Orchestration:
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
    # The DEFAULT is the real pr-green gate. Injectable so loop tests can
    # isolate the loop from the gate, but a test asserts this default so the
    # seam cannot quietly become permissive in production.
    if completion_gate is None:
        def gate(cur, tid):
            return completion_gate_default(cur, tid, project=project)
    else:
        gate = completion_gate

    # `MAX_WAVES` is a floor, not the cap. Its own justification was "one wave per
    # dependency level, and MAX_DEPTH bounds that" — but MAX_DEPTH bounds the
    # PARENT chain, while dependency-chain length is bounded only by MAX_NODES. A
    # perfectly legal 65-task dependency chain therefore hit the "runaway
    # backstop" and exited 1: a cap a legal graph can reach is a bug wearing a
    # safety rail's clothes, which is exactly what the original comment said it
    # must not be. An explicit `max_waves` is still honoured verbatim.
    cap = (max(MAX_WAVES, len(g) + 1) if max_waves is None
           else max(0, int(max_waves)))

    if execute and root is None:
        # Resolved ONCE, and a failure refuses the whole run BEFORE any child.
        # Leaving each child to resolve it meant a missing/slow git turned into
        # every task reporting itself failed — see `_run_wave`.
        from .. import paths
        root = paths.worktree_root()
        if root is None:
            raise RuntimeError(
                "no git worktree here — an orchestrated run resolves its tasks "
                "against the worktree root, so there is nothing to run them in. "
                "Run from inside a checkout, or pass an explicit root.")

    waves: list = []
    completed: list = []
    blocked: list = []
    skipped: list = []
    reasons: dict = {}
    branches: dict = {}
    kept: dict = {}
    capped = False

    for index in range(cap):
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
        done, failed, held, why, iso, starved = _run_wave(
            batch, g, runner, index, project=project, timeout_s=timeout_s,
            max_workers=max_workers, root=root, payload_for=payload_for,
            isolate=isolate, runner_kwargs=runner_kwargs)
        why_map = dict(why)
        # THE PR-GREEN GATE. A run succeeding is not the same as a task being
        # finished: the founding requirement is that a task is not done until its
        # terminal phase is satisfied. A refusal moves the task out of `completed`
        # and into `skipped`, so it keeps its PENDING status, its dependents stay
        # blocked, and the reason says what is still missing.
        allowed = []
        for tid in done:
            why = gate(g, tid)
            if why is None:
                allowed.append(tid)
            else:
                held.append(tid)
                why_map[tid] = (f"{why_map[tid]} · {why}" if tid in why_map
                                else why)
        done = allowed
        why = why_map

        wave_branches = {t: i.branch for t, i in iso.items() if i.ok}
        wave_kept = {t: i.path for t, i in iso.items()
                     if i.ok and i.reason}
        waves.append(Wave(index=index, tasks=list(batch), completed=done,
                          blocked=failed, skipped=held, reasons=dict(why),
                          branches=wave_branches, kept=wave_kept))
        branches.update(wave_branches)
        kept.update(wave_kept)
        completed.extend(done)
        blocked.extend(failed)
        skipped.extend(t for t in held if t not in skipped)
        reasons.update(why)
        if starved:
            # Recorded first: whatever DID run this wave keeps its row and
            # its reported reason. Braking must not discard work.
            g = apply_status(g, done, failed)
            return Orchestration(
                outcome=OUTCOME_USAGE, waves=waves, completed=completed,
                blocked=blocked, skipped=skipped, reasons=reasons,
                branches=branches, kept=kept)
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
                         blocked=blocked, skipped=skipped, reasons=reasons,
                         branches=branches, kept=kept)


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
