# SPDX-License-Identifier: Apache-2.0
# src/mindhive/orchestrator/queue.py
"""Lanes, work stealing, and the checkpoint that survives an interrupt.

## The invariant that makes stealing safe here

`docs/designs/lintester-orchestrator.md` rejected a work-stealing queue partly
because "work cannot be stolen from a child holding a written stdin." That is a
statement about physics, not a preference, and it is still true. Once
`executor.execute` has written a prompt into a child's stdin, that unit of work
belongs to that process until the process ends.

So this module adopts the objection instead of arguing with it:

    an entry is stealable ONLY while its state is `queued`.

`take()` pops and transitions to running inside ONE critical section — the
lane's `asyncio.Lock` is held across both — and `steal()` refuses any entry that
is not queued. A running entry is pinned to its lane for life. Stealing is
therefore queue rebalancing, which is where the win actually is: a lane holding
eight queued entries behind one slow child has eight stealable entries, and that
is exactly the case the PRD describes.

## Why asyncio here and threads there

`mindnet/orchestrator.py:29-35` rejects asyncio for the WAVE RUNNER, which
shells `subprocess.run` and is correctly a thread pool. This module does not
convert it and does not replace it. The event loop governs SCHEDULING only; a
blocking runner is reached through `run_in_executor`, so a sync child call still
runs on a thread and both statements stay true at once.

## LIFO local, FIFO steal

`take()` pops the right end, `steal()` the left. Two reasons, and only the first
is about speed: the owner keeps directory locality (the entry it just queued is
the one it just had context for), and the thief and the owner contend on
opposite ends of the deque, so the common case takes the lock without waiting on
each other's work.
"""
from __future__ import annotations

import asyncio
import collections
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths

# --- entry states -------------------------------------------------------------
# `queued` is the only stealable one. See the module docstring.
QUEUED = "queued"
RUNNING = "running"
VALIDATED = "validated"
FAILED = "failed"
ABANDONED = "abandoned"

STATES = (QUEUED, RUNNING, VALIDATED, FAILED, ABANDONED)

# Terminal states never return to a lane.
TERMINAL = (VALIDATED, ABANDONED)

# How long an idle worker waits before looking for stealable work again. Small
# enough that a lane freed by a repair is picked up promptly, large enough that
# an idle worker is not a busy loop burning the event loop's only thread.
IDLE_POLL_S = 0.005


@dataclass
class QueueEntry:
    """One schedulable unit and its mutable lifecycle.

    Deliberately SEPARATE from `AgentTask`, which is frozen. The task is the
    content an agent is given; this is the bookkeeping the scheduler owns. Fusing
    them would mean either a mutable task — and `AgentTask`'s whole job is to be
    a closed set of fields nothing can quietly widen — or a scheduler that
    rebuilds a frozen object on every state change and loses identity across
    steals."""

    task: object
    state: str = QUEUED
    attempts: int = 0
    reason: str = ""
    lane_id: int = -1
    result: object = None
    # Wall time this unit has actually cost, summed over every attempt. The
    # scheduler is the only place that can measure it, and it is measured
    # because nothing else in this repository records what a unit REALLY took
    # against the weight it was estimated at — which is the assumption every
    # scheduling number here rests on. Accumulated rather than replaced: two
    # attempts at one unit cost the mission both of them.
    #
    # NANOSECONDS, and that is the whole point rather than a flourish.
    # `payoff.calibrate` scores the weight against this number, and every reader
    # between here and there drops a duration of zero as "this unit never
    # reported one". Held as an integer millisecond it was floored by `int()`,
    # so a unit faster than a millisecond was indistinguishable from a unit that
    # never ran — and the calibration was left scored only on the units slow
    # enough to survive the flooring, which is exactly the bias that would make
    # a bad estimator look good. Integer nanoseconds also add exactly, where a
    # float accumulated across retries drifts.
    elapsed_ns: int = 0

    @property
    def elapsed_ms(self) -> float:
        """`elapsed_ns` in milliseconds — the unit the rest of this repository
        reports durations in, and the one `payoff.Sample` carries."""
        return self.elapsed_ns / 1_000_000.0

    @property
    def task_id(self) -> str:
        return str(getattr(self.task, "task_id", "") or "")

    @property
    def stealable(self) -> bool:
        return self.state == QUEUED

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "state": self.state,
                "attempts": int(self.attempts), "reason": str(self.reason),
                "lane": int(self.lane_id),
                "elapsed_ms": round(self.elapsed_ms, 3)}


class WorkerLane:
    """One lane: a deque of entries, an `asyncio.Lock`, and a worktree binding.

    `depth()` reads `len(deque)` without the lock on purpose. It is ADVISORY —
    used to pick a steal victim — and a thief that races and finds the victim
    already empty gets ``None`` from `steal()` and moves on. Taking the lock to
    read a length that is stale the moment it is returned buys nothing."""

    def __init__(self, lane_id, entries=(), worktree=None):
        self.lane_id = int(lane_id)
        self.worktree = worktree
        self._deque: collections.deque = collections.deque()
        self._lock = asyncio.Lock()
        for entry in entries or ():
            entry.lane_id = self.lane_id
            self._deque.append(entry)

    def __repr__(self) -> str:                          # pragma: no cover - debug aid
        return f"<WorkerLane {self.lane_id} depth={self.depth()}>"

    def depth(self) -> int:
        return len(self._deque)

    def queued_depth(self) -> int:
        """How many entries a thief could actually take. `depth()` counts
        everything; only queued entries are stealable, so a victim chosen on
        `depth()` alone can be one whose whole deque is pinned."""
        return sum(1 for e in tuple(self._deque) if e.stealable)

    @property
    def worktree_path(self) -> str:
        return str(getattr(self.worktree, "path", "") or "")

    async def push(self, entry) -> None:
        """Queue *entry* on this lane, at the local (LIFO) end."""
        async with self._lock:
            entry.lane_id = self.lane_id
            entry.state = QUEUED
            self._deque.append(entry)

    async def take(self):
        """Pop the most recent queued entry and mark it running. ``None`` if none.

        The pop and the state change are ONE critical section, and that is the
        dispatch transition the design depends on: between them, the entry is
        neither in a deque a thief can reach nor still marked stealable. Splitting
        them — pop here, transition at the call site — reopens exactly the window
        where two lanes can both believe they own the same work.

        Entries that reached a terminal state while sitting in the deque are
        dropped rather than returned."""
        async with self._lock:
            while self._deque:
                entry = self._deque.pop()               # LIFO — local end
                if entry.state in TERMINAL:
                    continue
                entry.state = RUNNING
                entry.lane_id = self.lane_id
                return entry
            return None

    async def steal(self):
        """Take the OLDEST *queued* entry from this lane, for another lane.

        Returns ``None`` when the lane is empty or holds nothing stealable.
        Non-queued entries are left exactly where they are — a running entry is
        pinned to its lane, and skipping past it must not reorder or drop it."""
        async with self._lock:
            if not self._deque:
                return None
            skipped: list = []
            found = None
            while self._deque:
                entry = self._deque.popleft()           # FIFO — thief's end
                if entry.stealable:
                    found = entry
                    break
                skipped.append(entry)
            # Put back what was passed over, in its original order.
            for entry in reversed(skipped):
                self._deque.appendleft(entry)
            return found

    def snapshot(self) -> list:
        """Every entry currently held, as plain dicts. For the checkpoint."""
        return [e.as_dict() for e in tuple(self._deque)]


def _succeeded(value) -> bool:
    """Did a runner's answer mean success?

    Handles the three shapes this codebase already returns: ``None`` (a runner
    that signals failure by raising), anything with an ``ok`` flag
    (`worktree.Isolated`), and anything with a ``result`` string
    (`executor.RunResult`, whose only passing value is a clean exit)."""
    if value is None:
        return True
    ok = getattr(value, "ok", None)
    if isinstance(ok, bool):
        return ok
    res = getattr(value, "result", None)
    if isinstance(res, str):
        return res == "ok"
    return bool(value)


class WorkStealingScheduler:
    """Runs every lane concurrently; idle lanes steal from the deepest sibling.

    *runner* is called as ``runner(entry, lane)``. It may be a coroutine function
    or a plain blocking callable — a blocking one is handed to the loop's default
    executor so a child process does not stall the whole scheduler.

    *repair* is optional and is the seam `resilience.RepairLoop` fills. It is
    called as ``repair(entry, lane)`` after a failure and answers whether the
    entry was re-queued. It must push the entry back onto ITS OWN lane; the
    scheduler does not move failed work between lanes, because a repair prompt is
    composed against the lane's worktree.

    *on_success* is `repair`'s other half and exists because without it the
    breaker behind `repair` only ever hears about failures: it can open and it
    can never close, and a HALF_OPEN probe that SUCCEEDS is thrown away, so the
    breaker's state records the worst the mission ever saw rather than where the
    target stands now. Called as ``on_success(entry, lane)`` when a unit
    validates. Its return value is ignored and an exception from it is swallowed
    — crediting a recovery must never fail a unit that already passed.

    *clock* answers nanoseconds and defaults to `time.perf_counter_ns`. It is a
    seam for the same reason `bench.run_once` takes one: what this scheduler
    measures is scored against an estimate by `payoff.calibrate`, and a test of
    that arithmetic must not double as a test of how fast the machine running
    it happens to be."""

    def __init__(self, lanes, runner, repair=None, mission_id="",
                 checkpoint_dir=None, on_success=None, clock=None, bus=None):
        self.lanes = list(lanes or ())
        self.runner = runner
        self.repair = repair
        self.on_success = on_success
        self.clock = clock if clock is not None else time.perf_counter_ns
        self.mission_id = str(mission_id or "")
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        self.completed: list = []
        self.failed: list = []
        self.steals = 0
        self._inflight = 0
        self._interrupted = False
        # Set by `halt`, and distinct from an interrupt on purpose. An interrupt
        # is the operator's: it propagates as an exception and writes a
        # checkpoint on the way out. A halt is the mission's own decision that
        # dispatching more would only repeat a failure the machine has already
        # reported, so the loops wind down normally and `run` answers what it
        # has. `left()` names what was never started.
        self.halted = ""
        # Entries currently executing, per lane. The checkpoint needs them: an
        # entry that `take()` has dispatched is in no lane's deque any more, so a
        # checkpoint built from the deques alone LOSES exactly the unit that was
        # running when the process died — the one most worth resuming.
        self._active: dict = {}
        # WB7's observation seam, and the ONLY thing this module knows about a
        # display: something with `emit(kind, **fields)`. No import, no event
        # class, no rendering — see `dashboard.py`'s first paragraph for why the
        # arrow is only allowed to point that way.
        self.bus = bus
        self.observer_drops = 0
        self.observer_error = ""

    def _emit(self, kind, **fields) -> None:
        """Publish one state change. Total, and reported when it is not.

        A board is derived state: losing a frame costs a frame. Swallowing the
        failure with no count would cost something else — an operator watching a
        board that silently stopped being about this mission."""
        bus = self.bus
        if bus is None:
            return
        try:
            bus.emit(kind, **fields)
        except Exception as exc:                        # noqa: BLE001
            self.observer_drops += 1
            if not self.observer_error:
                self.observer_error = f"{type(exc).__name__}: {exc}"

    async def _run_one(self, entry, lane):
        import inspect

        loop = asyncio.get_running_loop()
        # `inspect`, not `asyncio.iscoroutinefunction`: the asyncio alias is
        # deprecated in 3.14 and slated for removal in 3.16, and this repo's CI
        # matrix already runs 3.14.
        if inspect.iscoroutinefunction(self.runner):
            return await self.runner(entry, lane)
        return await loop.run_in_executor(None, self.runner, entry, lane)

    async def _steal_for(self, lane):
        """Take one entry from whichever sibling is carrying the most.

        Siblings are ordered by *queued* depth, and every one of them is tried:
        the deepest lane's entries may all be pinned, and giving up after one
        miss would leave a shallower lane's genuinely stealable work sitting
        there while this worker exits."""
        victims = sorted((ln for ln in self.lanes if ln is not lane),
                         key=lambda ln: (-ln.queued_depth(), ln.lane_id))
        for victim in victims:
            if victim.queued_depth() <= 0:
                continue
            entry = await victim.steal()
            if entry is not None:
                self.steals += 1
                self._emit("work_stolen", lane=lane.lane_id,
                           victim=victim.lane_id, task_id=entry.task_id,
                           target=target_of(entry))
                await lane.push(entry)
                return await lane.take()
        return None

    def halt(self, reason) -> None:
        """Stop taking new work. Idempotent; the first reason is the one kept.

        In-flight units are NOT cancelled. A child that is running is a child
        that could run, and killing it would turn a halt into damage -- the
        opposite of the point, which is that nothing else gets hurt."""
        if not self.halted:
            self.halted = str(reason or "").strip() or "halted"

    def left(self) -> list:
        """Entries still queued on any lane -- what a halt left unstarted.

        Empty after a run that drained every lane. Read after `run`, and only
        then: while lanes are working the answer is stale by the time it is
        returned, for the same reason `WorkerLane.depth` takes no lock."""
        out = []
        for lane in self.lanes:
            out.extend(e for e in tuple(lane._deque) if e.state == QUEUED)
        return out

    async def _work(self, lane):
        while not self._interrupted and not self.halted:
            entry = await lane.take()
            if entry is None:
                entry = await self._steal_for(lane)
            if entry is None:
                # Nothing here and nothing stealable. Another lane may still be
                # mid-run and about to re-queue a repair, so idle rather than
                # exit while anything is in flight.
                if self._inflight <= 0:
                    return
                await asyncio.sleep(IDLE_POLL_S)
                continue
            self._inflight += 1
            self._active.setdefault(lane.lane_id, []).append(entry)
            started = int(self.clock())
            try:
                entry.attempts += 1
                self._emit("task_started", lane=lane.lane_id,
                           task_id=entry.task_id, target=target_of(entry),
                           weight=weight_of(entry), attempt=entry.attempts)
                try:
                    result = await self._run_one(entry, lane)
                except asyncio.CancelledError:
                    entry.state = QUEUED
                    raise
                except Exception as exc:                # noqa: BLE001
                    entry.state = FAILED
                    entry.reason = f"{type(exc).__name__}: {exc}"
                    entry.result = None
                else:
                    entry.result = result
                    if _succeeded(result):
                        entry.state = VALIDATED
                        entry.reason = ""
                    else:
                        entry.state = FAILED
                        entry.reason = (str(getattr(result, "reason", "") or "")
                                        or "the runner reported failure")
            finally:
                self._inflight -= 1
                # In the `finally`, so a cancelled attempt still reports what it
                # spent before it was cut off. A unit that burned four minutes
                # and was then interrupted did cost the mission four minutes.
                #
                # `max(0, ...)` because an interval is never negative: the
                # default clock is monotonic, an injected one is whatever the
                # caller made it, and a negative interval added to a running
                # total would make a unit that ran twice look cheaper than one
                # that ran once.
                entry.elapsed_ns += max(0, int(self.clock()) - started)
            # Reached only when the attempt RESOLVED. An interrupt or a
            # cancellation propagates past this line, leaving the entry in
            # `_active` deliberately: the checkpoint `run()` writes on its way
            # out has to name the unit that was executing when the process died,
            # and that unit is in no lane's deque any more. Doing this in a
            # `finally` — where it started — dropped exactly that unit.
            try:
                self._active.get(lane.lane_id, []).remove(entry)
            except ValueError:                          # pragma: no cover
                pass
            self._emit("task_finished", lane=lane.lane_id,
                       task_id=entry.task_id, state=entry.state,
                       elapsed_ms=entry.elapsed_ms, reason=entry.reason)
            if entry.state == VALIDATED:
                self.completed.append(entry)
                if self.on_success is not None:
                    try:
                        await _maybe_await(self.on_success(entry, lane))
                    except Exception:                   # noqa: BLE001
                        # A unit that passed has passed. Bookkeeping about that
                        # success must not be able to turn it into a failure.
                        pass
                continue
            requeued = False
            if self.repair is not None:
                try:
                    requeued = bool(await _maybe_await(
                        self.repair(entry, lane)))
                except Exception as exc:                # noqa: BLE001
                    entry.reason = (f"{entry.reason} (repair raised "
                                    f"{type(exc).__name__}: {exc})").strip()
                    requeued = False
            if not requeued:
                if entry.state not in TERMINAL:
                    # `abandoned` means something DECIDED to stop — the repair
                    # loop declined, or the breaker refused. With no repair loop
                    # configured nothing decided anything, and calling that
                    # abandoned would make `resume` skip a unit that merely
                    # failed. The two words are not synonyms and `resume` reads
                    # them differently.
                    entry.state = ABANDONED if self.repair is not None else FAILED
                self.failed.append(entry)
            self._checkpoint(lane)

    def _checkpoint(self, lane) -> None:
        if self.checkpoint_dir is None:
            return
        try:
            write_checkpoint(self.checkpoint_dir, self.mission_id, lane,
                             self.completed, self.failed,
                             active=self._active.get(lane.lane_id, ()))
        except Exception:                               # noqa: BLE001
            # A checkpoint is DERIVED state: losing it costs re-work on resume
            # and nothing else. It must never take down the mission it exists to
            # make resumable.
            return

    async def run(self):
        """Run every lane to completion. Answers ``(completed, failed)``.

        `KeyboardInterrupt` and cancellation both land here as a checkpoint
        write, because the whole point of the sidecar is the run that did not
        finish."""
        if not self.lanes:
            return [], []
        for lane in self.lanes:
            self._emit("lane_opened", lane=lane.lane_id,
                       worktree=lane.worktree_path, depth=lane.depth())
        try:
            await asyncio.gather(*(self._work(ln) for ln in self.lanes))
        except (asyncio.CancelledError, KeyboardInterrupt):
            self._interrupted = True
            for lane in self.lanes:
                self._checkpoint(lane)
            raise
        for lane in self.lanes:
            self._checkpoint(lane)
        return self.completed, self.failed


def target_of(entry) -> str:
    """The file a unit is about, for the board's Target column.

    Public because `resilience` and `lintester` publish the same column and two
    spellings of "which file is this" would drift the moment one of them learned
    about a second field. Falls back to the task id, which is never empty and is
    a truer answer than a blank cell: a unit with no file really is about
    itself."""
    task = getattr(entry, "task", None)
    files = tuple(getattr(task, "files", ()) or ())
    if files:
        return str(files[0])
    return str(getattr(entry, "task_id", "") or "")


def weight_of(entry) -> int:
    """A unit's estimated weight — the Load Metric column."""
    try:
        return int(getattr(getattr(entry, "task", None), "weight", 0) or 0)
    except (TypeError, ValueError):                     # pragma: no cover - guard
        return 0


async def _maybe_await(value):
    """*value*, awaited when it is awaitable. Lets `repair` be either kind."""
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


# --- checkpoint sidecar --------------------------------------------------------
# One file per lane, never a shared one. `paths.write_text_atomic` is recorded in
# this project's ledger as flaky on Windows when two writers contend on the SAME
# destination (os.replace raises WinError 5 on the destination), and parallel
# lanes are exactly two writers. Per-lane paths make the contention impossible
# rather than making it rare.

CHECKPOINT_PREFIX = "lane-"


def checkpoint_path(checkpoint_dir, mission_id, lane_id) -> Path:
    safe = paths.normalize(str(mission_id or "mission"))
    return (Path(checkpoint_dir) / safe /
            f"{CHECKPOINT_PREFIX}{int(lane_id)}.json")


def write_checkpoint(checkpoint_dir, mission_id, lane, completed=(),
                     failed=(), active=()) -> Path:
    """Record what this lane still holds, plus what already resolved.

    The *entries* list is what `resume` re-queues. It is the deque PLUS *active*
    — the entries dispatched out of the deque and still executing — because a
    process killed mid-run leaves those in neither place, and a checkpoint that
    omits them silently drops the unit it was written to rescue.

    `completed` and `failed` ride along so a resume can report what it is
    skipping instead of quietly doing less work than the first run."""
    target = checkpoint_path(checkpoint_dir, mission_id, lane.lane_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    done = [e.task_id for e in completed if e.lane_id == lane.lane_id]
    bad = [e.as_dict() for e in failed if e.lane_id == lane.lane_id]
    held = lane.snapshot() + [e.as_dict() for e in tuple(active)
                              if e.state not in TERMINAL]
    payload = {
        "mission": str(mission_id or ""),
        "lane": int(lane.lane_id),
        "worktree": lane.worktree_path,
        "entries": held,
        "completed": done,
        "failed": bad,
    }
    paths.write_text_atomic(target, json.dumps(payload, indent=2,
                                               sort_keys=True) + "\n")
    return target


def read_checkpoint(path) -> dict:
    """One checkpoint file, or ``{}``.

    Total, and that is the contract that matters: a truncated or hand-edited
    sidecar must resume as "nothing to resume", never brick the mission it was
    written to rescue."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def resume(checkpoint_dir, mission_id) -> list:
    """Entries a resumed mission should re-queue, oldest lane first.

    `queued`, `running` and `failed` come back. A `validated` entry is finished
    work and re-running it is the exact waste the checkpoint exists to prevent;
    an `abandoned` one was given up on deliberately, and silently retrying it
    would undo a decision the breaker made.

    `running` is in the resume set and that is not a contradiction: a checkpoint
    is only ever READ after the process that wrote it is gone, so nothing it
    calls running is still running. The alternative — treating it as finished —
    is the one reading that can lose work."""
    root = Path(checkpoint_dir) / paths.normalize(str(mission_id or "mission"))
    if not root.is_dir():
        return []
    out: list = []
    for path in sorted(root.glob(f"{CHECKPOINT_PREFIX}*.json")):
        payload = read_checkpoint(path)
        for row in payload.get("entries") or ():
            if not isinstance(row, dict):
                continue
            if str(row.get("state") or "") in (QUEUED, RUNNING, FAILED):
                out.append({**row, "lane": payload.get("lane", row.get("lane"))})
        for row in payload.get("failed") or ():
            if isinstance(row, dict) and str(row.get("state") or "") == FAILED:
                out.append({**row, "lane": payload.get("lane", row.get("lane"))})
    return out


@dataclass
class LanePlan:
    """What `lintester` hands the scheduler for one lane: its entries and the
    worktree they run in. A plain container, kept here so `queue` owns the shape
    its own constructor consumes."""

    lane_id: int
    entries: list = field(default_factory=list)
    worktree: object = None
