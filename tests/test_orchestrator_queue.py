# tests/test_orchestrator_queue.py
"""WB3 — WorkerLane, work stealing, and the resume checkpoint.

Written against
``docs/plans/2026-09-01-001-feat-lintester-orchestrator-full-prd-plan.md`` U3/U4.

`test_a_running_entry_is_never_stolen` is the one that matters most, and it was
written first. `docs/designs/lintester-orchestrator.md:180-181` rejected work
stealing partly because "work cannot be stolen from a child holding a written
stdin" — a true statement about physics. The override is only safe because
`steal()` refuses anything not `queued` and `take()` makes the pop and the
running transition one critical section. A scheduler that passes every other
test in this file while violating that one looks correct and corrupts work.

No `pytest-asyncio` — the `dev` extra is `pytest` alone, deliberately — so every
coroutine here is driven by `asyncio.run` from a plain sync test.
"""
import asyncio
import json

import pytest

from mindhive.orchestrator import queue as q


class _Task:
    """The minimum `QueueEntry` reads off a task: an id."""

    def __init__(self, task_id):
        self.task_id = task_id


def _entry(task_id, state=q.QUEUED):
    e = q.QueueEntry(task=_Task(task_id))
    e.state = state
    return e


def _drive(coro_fn):
    return asyncio.run(coro_fn())


# --- LIFO / FIFO --------------------------------------------------------------

def test_take_is_lifo_and_steal_is_fifo():
    async def go():
        lane = q.WorkerLane(0, [_entry("a"), _entry("b"), _entry("c")])
        newest = await lane.take()
        oldest = await lane.steal()
        return newest.task_id, oldest.task_id, lane.depth()

    assert _drive(go) == ("c", "a", 1)


def test_take_marks_the_entry_running_in_the_same_critical_section():
    """The dispatch transition. If the pop and the state change ever split, two
    lanes can both believe they own one unit."""
    async def go():
        lane = q.WorkerLane(0, [_entry("a")])
        entry = await lane.take()
        return entry.state, entry.stealable, entry.lane_id

    assert _drive(go) == (q.RUNNING, False, 0)


def test_a_running_entry_is_never_stolen():
    """The invariant the whole override rests on."""
    async def go():
        pinned = _entry("pinned", state=q.RUNNING)
        victim = q.WorkerLane(0, [pinned])
        stolen = await victim.steal()
        return stolen, victim.depth(), victim.queued_depth()

    stolen, depth, queued = _drive(go)
    assert stolen is None, "a dispatched entry was handed to a second lane"
    assert depth == 1, "the pinned entry must stay exactly where it was"
    assert queued == 0


def test_steal_skips_a_pinned_entry_without_reordering_the_rest():
    async def go():
        pinned = _entry("pinned", state=q.RUNNING)
        lane = q.WorkerLane(0, [pinned, _entry("free"), _entry("later")])
        stolen = await lane.steal()
        remaining = [e.task_id for e in lane._deque]
        return stolen.task_id, remaining

    assert _drive(go) == ("free", ["pinned", "later"])


def test_stealing_from_an_empty_victim_answers_none():
    async def go():
        return await q.WorkerLane(0).steal()

    assert _drive(go) is None


def test_a_terminal_entry_left_in_the_deque_is_dropped_not_dispatched():
    async def go():
        lane = q.WorkerLane(0, [_entry("a"), _entry("done", state=q.VALIDATED)])
        return (await lane.take()).task_id

    assert _drive(go) == "a"


# --- the scheduler ------------------------------------------------------------

def _recorder():
    seen = []

    def runner(entry, lane):
        seen.append((entry.task_id, lane.lane_id))
        return True

    return seen, runner


def test_an_idle_lane_steals_and_nothing_runs_twice():
    seen, runner = _recorder()
    loaded = q.WorkerLane(0, [_entry(f"t{i}") for i in range(5)])
    idle = q.WorkerLane(1)
    sched = q.WorkStealingScheduler([loaded, idle], runner)
    completed, failed = asyncio.run(sched.run())

    ids = [tid for tid, _ln in seen]
    assert sorted(ids) == [f"t{i}" for i in range(5)]
    assert len(ids) == len(set(ids)), "a unit ran twice"
    assert len(completed) == 5 and not failed
    assert sched.steals >= 1, "the idle lane never stole; the test proves nothing"
    assert {ln for _tid, ln in seen} == {0, 1}, "both lanes must have run work"


def test_two_thieves_get_disjoint_work():
    seen, runner = _recorder()
    loaded = q.WorkerLane(0, [_entry(f"t{i}") for i in range(6)])
    sched = q.WorkStealingScheduler(
        [loaded, q.WorkerLane(1), q.WorkerLane(2)], runner)
    completed, _failed = asyncio.run(sched.run())

    ids = [tid for tid, _ln in seen]
    assert sorted(ids) == [f"t{i}" for i in range(6)]
    assert len(ids) == len(set(ids))
    assert len(completed) == 6


def test_a_lane_with_no_siblings_completes():
    seen, runner = _recorder()
    sched = q.WorkStealingScheduler([q.WorkerLane(0, [_entry("only")])], runner)
    completed, failed = asyncio.run(sched.run())
    assert [t for t, _ in seen] == ["only"]
    assert len(completed) == 1 and not failed


def test_no_lanes_is_an_empty_run_not_a_hang():
    assert asyncio.run(q.WorkStealingScheduler([], lambda e, ln: True).run()) == ([], [])


def test_a_runner_that_raises_fails_only_its_own_unit():
    def runner(entry, lane):
        if entry.task_id == "boom":
            raise RuntimeError("child died")
        return True

    lanes = [q.WorkerLane(0, [_entry("ok0"), _entry("boom")]),
             q.WorkerLane(1, [_entry("ok1")])]
    sched = q.WorkStealingScheduler(lanes, runner)
    completed, failed = asyncio.run(sched.run())

    assert sorted(e.task_id for e in completed) == ["ok0", "ok1"]
    assert [e.task_id for e in failed] == ["boom"]
    assert "RuntimeError: child died" in failed[0].reason
    assert failed[0].state == q.FAILED, (
        "with no repair loop configured nothing DECIDED to give up, so the unit "
        "failed rather than being abandoned — `resume` reads the two words "
        "differently")


def test_a_runner_reporting_failure_by_value_is_a_failure_too():
    class _Bad:
        ok = False
        reason = "nonzero exit"

    sched = q.WorkStealingScheduler([q.WorkerLane(0, [_entry("a")])],
                                    lambda e, ln: _Bad())
    completed, failed = asyncio.run(sched.run())
    assert not completed and [e.reason for e in failed] == ["nonzero exit"]


def test_a_coroutine_runner_is_awaited_not_scheduled_on_a_thread():
    seen = []

    async def runner(entry, lane):
        seen.append(entry.task_id)
        return True

    sched = q.WorkStealingScheduler([q.WorkerLane(0, [_entry("a")])], runner)
    completed, _failed = asyncio.run(sched.run())
    assert seen == ["a"] and len(completed) == 1


def test_repair_requeues_onto_its_own_lane():
    attempts = {}

    def runner(entry, lane):
        attempts.setdefault(entry.task_id, []).append(lane.lane_id)
        return len(attempts[entry.task_id]) > 1        # fails once, then passes

    async def repair(entry, lane):
        await lane.push(entry)
        return True

    lanes = [q.WorkerLane(0, [_entry("a")]), q.WorkerLane(1, [_entry("b")])]
    sched = q.WorkStealingScheduler(lanes, runner, repair=repair)
    completed, failed = asyncio.run(sched.run())

    assert not failed
    assert len(completed) == 2
    assert attempts["a"] == [0, 0], "a repaired unit must return to its own lane"
    assert attempts["b"] == [1, 1]


def test_a_repair_that_raises_leaves_the_unit_failed_and_says_why():
    def repair(entry, lane):
        raise ValueError("repair broke")

    sched = q.WorkStealingScheduler([q.WorkerLane(0, [_entry("a")])],
                                    lambda e, ln: False, repair=repair)
    _completed, failed = asyncio.run(sched.run())
    assert len(failed) == 1
    assert "repair raised ValueError: repair broke" in failed[0].reason


# --- checkpoint ---------------------------------------------------------------

def _read(tmp_path, mission, lane_id):
    return q.read_checkpoint(q.checkpoint_path(tmp_path, mission, lane_id))


def test_an_interrupt_checkpoints_exactly_the_unfinished_ids(tmp_path):
    seen = []

    def runner(entry, lane):
        seen.append(entry.task_id)
        if len(seen) == 2:
            raise KeyboardInterrupt
        return True

    lane = q.WorkerLane(0, [_entry(f"t{i}") for i in range(5)])
    sched = q.WorkStealingScheduler([lane], runner, mission_id="m1",
                                    checkpoint_dir=tmp_path)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(sched.run())

    payload = _read(tmp_path, "m1", 0)
    held = {row["task_id"] for row in payload["entries"]}
    assert held == {"t0", "t1", "t2", "t3"}, (
        "three still queued plus the one that was running when it died")
    assert payload["completed"] == ["t4"]
    assert "t4" not in held, "a finished unit must not be re-queued"


def test_resume_requeues_only_the_unfinished(tmp_path):
    def runner(entry, lane):
        if entry.task_id == "t1":
            raise RuntimeError("no")
        return True

    lane = q.WorkerLane(0, [_entry("t0"), _entry("t1"), _entry("t2")])
    sched = q.WorkStealingScheduler([lane], runner, mission_id="m2",
                                    checkpoint_dir=tmp_path)
    asyncio.run(sched.run())

    rows = q.resume(tmp_path, "m2")
    assert [r["task_id"] for r in rows] == ["t1"]
    assert rows[0]["state"] == q.FAILED


def test_a_repair_that_declines_abandons_rather_than_leaving_it_resumable(
        tmp_path):
    """The other half of the word choice: a decision to stop must survive a
    resume, or the next run silently undoes it."""
    sched = q.WorkStealingScheduler([q.WorkerLane(0, [_entry("a")])],
                                    lambda e, ln: False,
                                    repair=lambda e, ln: False,
                                    mission_id="m9", checkpoint_dir=tmp_path)
    _completed, failed = asyncio.run(sched.run())
    assert failed[0].state == q.ABANDONED
    assert q.resume(tmp_path, "m9") == []


def test_a_validated_entry_is_never_requeued(tmp_path):
    lane = q.WorkerLane(0, [_entry("t0"), _entry("t1")])
    sched = q.WorkStealingScheduler([lane], lambda e, ln: True, mission_id="m3",
                                    checkpoint_dir=tmp_path)
    asyncio.run(sched.run())
    assert q.resume(tmp_path, "m3") == []


def test_a_corrupt_checkpoint_resumes_empty_rather_than_raising(tmp_path):
    target = q.checkpoint_path(tmp_path, "m4", 0)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"entries": [', encoding="utf-8")
    assert q.read_checkpoint(target) == {}
    assert q.resume(tmp_path, "m4") == []


def test_a_missing_checkpoint_resumes_empty(tmp_path):
    assert q.resume(tmp_path, "never-written") == []


def test_two_lanes_checkpoint_to_distinct_paths(tmp_path):
    a = q.checkpoint_path(tmp_path, "m5", 0)
    b = q.checkpoint_path(tmp_path, "m5", 1)
    assert a != b, ("one shared destination is exactly the contention that makes "
                    "paths.write_text_atomic flaky on Windows")
    assert a.parent == b.parent


def test_concurrent_lanes_both_write_their_checkpoint(tmp_path):
    lanes = [q.WorkerLane(i, [_entry(f"l{i}t{j}") for j in range(3)])
             for i in range(3)]
    sched = q.WorkStealingScheduler(lanes, lambda e, ln: True, mission_id="m6",
                                    checkpoint_dir=tmp_path)
    asyncio.run(sched.run())
    for i in range(3):
        payload = _read(tmp_path, "m6", i)
        assert payload["lane"] == i
        assert payload["entries"] == []


def test_a_checkpoint_write_failure_does_not_take_down_the_run(tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(q, "write_checkpoint",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("full")))
    sched = q.WorkStealingScheduler([q.WorkerLane(0, [_entry("a")])],
                                    lambda e, ln: True, mission_id="m7",
                                    checkpoint_dir=tmp_path)
    completed, failed = asyncio.run(sched.run())
    assert len(completed) == 1 and not failed


def test_the_checkpoint_is_valid_json_on_disk(tmp_path):
    lane = q.WorkerLane(0, [_entry("a")])
    q.write_checkpoint(tmp_path, "m8", lane)
    raw = q.checkpoint_path(tmp_path, "m8", 0).read_text(encoding="utf-8")
    assert json.loads(raw)["entries"][0]["task_id"] == "a"


# --- the success half of the repair seam ---------------------------------------

def test_a_validated_unit_is_credited_through_on_success():
    """Without this seam the breaker behind `repair` is failure-only: `repair`
    fires on failures alone, so nothing ever tells it a target recovered."""
    seen = []
    lane = q.WorkerLane(0, [_entry("t0"), _entry("t1")])
    sched = q.WorkStealingScheduler(
        [lane], runner=lambda e, ln: True,
        on_success=lambda e, ln: seen.append(e.task_id))
    done, bad = _drive(sched.run)

    assert [e.task_id for e in done] == ["t1", "t0"]     # LIFO
    assert bad == []
    assert sorted(seen) == ["t0", "t1"]


def test_a_failing_unit_is_not_credited_as_a_success():
    """The negative half. Without it the test above passes for a hook that
    fires on every unit regardless of outcome."""
    seen = []
    lane = q.WorkerLane(0, [_entry("bad")])
    sched = q.WorkStealingScheduler(
        [lane], runner=lambda e, ln: False,
        on_success=lambda e, ln: seen.append(e.task_id))
    done, bad = _drive(sched.run)

    assert done == []
    assert [e.task_id for e in bad] == ["bad"]
    assert seen == [], "a failed unit was credited as a recovery"


def test_a_raising_success_hook_cannot_fail_a_unit_that_passed():
    def _boom(entry, lane):
        raise RuntimeError("bookkeeping blew up")

    lane = q.WorkerLane(0, [_entry("t0")])
    sched = q.WorkStealingScheduler([lane], runner=lambda e, ln: True,
                                    on_success=_boom)
    done, bad = _drive(sched.run)

    assert [e.task_id for e in done] == ["t0"]
    assert bad == []
    assert done[0].state == q.VALIDATED


def test_an_async_success_hook_is_awaited():
    seen = []

    async def _hook(entry, lane):
        await asyncio.sleep(0)
        seen.append(entry.task_id)

    lane = q.WorkerLane(0, [_entry("t0")])
    _drive(q.WorkStealingScheduler([lane], runner=lambda e, ln: True,
                                   on_success=_hook).run)
    assert seen == ["t0"]
