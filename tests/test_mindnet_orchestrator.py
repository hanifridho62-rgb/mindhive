# tests/test_mindnet_orchestrator.py
"""MN-7 — the parallel orchestrator: the loop that walks the MN-2 graph.

Written TEST-FIRST against
``docs/plans/2026-07-30-001-feat-mindnet-mn7-parallel-orchestrator-plan.md``.
No line of `mindhive/mindnet/orchestrator.py` existed when these were written.

## What this module is NOT

It is not a second readiness rule. `tasks.ready()` has answered "which tasks may
start" since MN-2 — PENDING, every dependency COMPLETED, no BLOCKED ancestor —
and it already satisfies every frontier property the directive asked for,
including the failure cascade. `frontier()` adds the CONCURRENCY BOUND and
nothing else. `test_frontier_delegates_to_the_shared_readiness_predicate` pins
that structurally: if a future edit inlines a second copy of the rule, the two
can drift, and the drift direction is "the orchestrator spawns work the rest of
the machine considers blocked".

## The injected runner is the whole design

`orchestrate` takes `runner=` (defaulting to `executor.execute`). That single
seam is why concurrency, the failure cascade, wave re-evaluation and stall
detection are all testable without starting one real process — and it is why
`frontier` was kept pure. There is exactly one test that uses the real
`executor.execute`, and it exists to prove the seam's contract matches reality.

## Why a fake runner and not a subprocess mock

A patched `subprocess.run` encodes a guess about argv. The fake runner here
returns real `executor.RunResult` values, so the tests constrain the CONTRACT
(`result` in `RUN_RESULTS` -> status) rather than the call shape.

## The MN-6 lesson this file inherits

`sidecar._staleness` counts every event. A status row appended without rewriting
the sidecar reads STALE, and a stale projection silently disables both MN-1's
phase-aware injection and MN-4's teeth for the next wave.
`test_a_wave_leaves_the_sidecar_fresh` carries a canary proving that assertion
can fail, because a freshness test that cannot go red proves nothing.
"""
import ast
import inspect
import threading
import time

import pytest

from mindhive import events
from mindhive.mindnet import executor as x_mod
from mindhive.mindnet import orchestrator as orc
from mindhive.mindnet import sidecar as sc_mod
from mindhive.mindnet import tasks as t_mod

SESSION = "s1"


# --- fixture builders: valid by default, one thing changed per test -----------

def _task(tid, status=t_mod.STATUS_PENDING, deps=(), parent=None, **kw):
    return t_mod.Task(task_id=tid, title=f"task {tid}", status=status,
                      dependencies=tuple(deps), parent_id=parent, **kw)


def _graph(*tasks) -> dict:
    return {t.task_id: t for t in tasks}


def _linear():
    """A -> B -> C. Only A can start."""
    return _graph(_task("a"), _task("b", deps=("a",)), _task("c", deps=("b",)))


def _diamond(a_status=t_mod.STATUS_PENDING, b_status=t_mod.STATUS_PENDING,
             c_status=t_mod.STATUS_PENDING):
    """The directive's shape: A blocks B and C; B and C block D."""
    return _graph(
        _task("a", status=a_status),
        _task("b", status=b_status, deps=("a",)),
        _task("c", status=c_status, deps=("a",)),
        _task("d", deps=("b", "c")))


class FakeRunner:
    """A stand-in for `executor.execute` that records what was submitted.

    Records concurrency honestly: `live` peaks at the true simultaneous count,
    so a serial implementation of a parallel wave fails rather than passing on a
    technicality."""

    def __init__(self, results=None, delay=0.0, raises=()):
        self.results = dict(results or {})
        self.delay = delay
        self.raises = set(raises)
        self.calls = []                  # task_ids, in submission order
        self.kwargs = {}                 # task_id -> kwargs it was called with
        self.live = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, run_key, script, **kw):
        tid = kw.get("task_id") or str(run_key)
        with self._lock:
            self.calls.append(tid)
            self.kwargs[tid] = kw
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            if tid in self.raises:
                raise OSError(f"runner blew up on {tid}")
            if self.delay:
                time.sleep(self.delay)
            return _run_result(self.results.get(tid, "ok"))
        finally:
            with self._lock:
                self.live -= 1

    def submitted(self):
        return set(self.calls)


def _run_result(result):
    """A real `executor.RunResult`, so the tests constrain the contract rather
    than a shape invented here.

    `executed` mirrors the real thing: `refused` and `spawn-failed` mean no child
    process was ever created, and that is the security property the executor's
    own suite reads."""
    return x_mod.RunResult(
        result=result, exit_code=0 if result == "ok" else 1,
        reason="" if result == "ok" else f"synthetic {result}",
        executed=result not in ("refused", "spawn-failed"),
        artifact=None, excerpt="", duration_s=0.01)


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    monkeypatch.setenv("MINDHIVE_SESSION", SESSION)


# ============================================================================
# U2 — frontier(): the bounded DAG read
# ============================================================================

def test_a_linear_graph_offers_only_its_root():
    """The directive's first frontier assertion."""
    assert orc.frontier(_linear()) == ["a"]


def test_a_diamond_offers_both_arms_once_the_root_completes():
    """The directive's second: B and C simultaneously, not one at a time."""
    g = _diamond(a_status=t_mod.STATUS_COMPLETED)
    assert orc.frontier(g) == ["b", "c"]


def test_a_blocked_arm_keeps_the_join_off_the_frontier_forever():
    """The directive's third, and the property the whole failure cascade rests
    on. C is blocked, so D — which needs both arms — must never appear."""
    g = _diamond(a_status=t_mod.STATUS_COMPLETED,
                 b_status=t_mod.STATUS_COMPLETED,
                 c_status=t_mod.STATUS_BLOCKED)
    got = orc.frontier(g)
    assert "d" not in got
    assert "c" not in got, "a blocked task is not itself startable"
    assert got == []


def test_a_disconnected_graph_offers_every_root():
    """The directive's fourth. Sorted, because a frontier that reshuffles
    between calls makes a wave irreproducible."""
    g = _graph(_task("zeta"), _task("alpha"),
               _task("beta", deps=("alpha",)))
    assert orc.frontier(g) == ["alpha", "zeta"]


def test_a_task_whose_ancestor_is_blocked_is_not_offered():
    """Inherited from `tasks.ready` and worth pinning here: the orchestrator
    must not spawn work inside a blocked subtree. Transitive on purpose —
    if only the immediate parent counted, nesting one level deeper would
    escape it."""
    g = _graph(_task("root", status=t_mod.STATUS_BLOCKED),
               _task("mid", parent="root"),
               _task("leaf", parent="mid"))
    assert orc.frontier(g) == []


def test_a_dangling_dependency_is_never_ready():
    """A dependency on an id that is not in the graph cannot be COMPLETED, so
    the task must not start. `check_graph` reports the dangling edge; the
    frontier simply refuses to run it."""
    assert orc.frontier(_graph(_task("a", deps=("ghost",)))) == []


def test_an_in_progress_task_is_not_offered_again():
    """Otherwise every wave re-spawns work already running."""
    g = _graph(_task("a", status=t_mod.STATUS_IN_PROGRESS))
    assert orc.frontier(g) == []


@pytest.mark.parametrize("closed", [t_mod.STATUS_COMPLETED,
                                    t_mod.STATUS_ABANDONED])
def test_a_settled_task_is_not_offered(closed):
    assert orc.frontier(_graph(_task("a", status=closed))) == []


# --- the bound, which is the only thing this function adds --------------------

def test_the_limit_truncates_the_frontier():
    g = _diamond(a_status=t_mod.STATUS_COMPLETED)
    assert orc.frontier(g, limit=1) == ["b"]


def test_the_limit_is_stable_across_calls():
    """A limit that returned a different subset each call would make waves
    irreproducible and a denial impossible to explain."""
    g = _diamond(a_status=t_mod.STATUS_COMPLETED)
    assert [orc.frontier(g, limit=1) for _ in range(5)] == [["b"]] * 5


def test_a_zero_limit_offers_nothing():
    g = _diamond(a_status=t_mod.STATUS_COMPLETED)
    assert orc.frontier(g, limit=0) == []


def test_a_none_limit_offers_everything_ready():
    g = _diamond(a_status=t_mod.STATUS_COMPLETED)
    assert orc.frontier(g, limit=None) == ["b", "c"]


def test_a_negative_limit_offers_nothing_rather_than_slicing_backwards():
    """`ready[:-1]` would silently drop the LAST ready task — a Python slicing
    trap that reads as working code."""
    g = _diamond(a_status=t_mod.STATUS_COMPLETED)
    assert orc.frontier(g, limit=-1) == []


def test_a_limit_larger_than_the_frontier_is_harmless():
    g = _diamond(a_status=t_mod.STATUS_COMPLETED)
    assert orc.frontier(g, limit=99) == ["b", "c"]


# --- totality ----------------------------------------------------------------

def test_an_empty_graph_offers_nothing():
    assert orc.frontier({}) == []


@pytest.mark.parametrize("junk", [None, [], "not a graph", 42])
def test_a_malformed_graph_is_empty_not_a_raise(junk):
    assert orc.frontier(junk) == []


def test_both_task_map_producers_only_ever_yield_task_objects():
    """Why `frontier` does no per-row filtering — asserted, not assumed.

    There are exactly two producers of a ``task_id -> Task`` map, and both
    guarantee the value type: `tasks.fold` constructs `Task` objects, and
    `sidecar._decode_tasks` drops any row `_row` cannot build. So a junk VALUE
    cannot reach here, and adding a defensive per-row filter would cost work on
    every wave for an input production cannot produce — the mirror image of the
    MN-4 P0, where a guard was written against a shape the tools never emit.

    A whole-graph fault is a different matter and IS defended above: a caller
    can genuinely hand over the wrong object.

    If a third producer ever appears, or either of these two loosens, this test
    fails and the filter question gets reopened deliberately."""
    ev = events.append_event(
        "mindnet_task",
        {"action": "declare", "task_id": "prod-check", "title": "a task"},
        tags=["mindnet"])
    assert ev is not None
    folded = t_mod.fold(events.read_all())
    assert folded, "canary: the fold produced nothing, so this proves nothing"
    assert all(isinstance(v, t_mod.Task) for v in folded.values())

    src = inspect.getsource(sc_mod._decode_tasks)
    assert "task is not None" in src, (
        "the sidecar decoder no longer guarantees Task values — frontier's "
        "lack of a per-row filter depended on that")


# --- the one-legend guarantee -------------------------------------------------

def test_frontier_delegates_to_the_shared_readiness_predicate():
    """Structural, not behavioural. A behavioural test passes just as well
    against a hand-rolled copy of the rule that happens to agree today;
    this asserts the call is actually made, so the two cannot drift."""
    assert "ready(" in inspect.getsource(orc.frontier), (
        "frontier must call tasks.ready — a second readiness rule in the "
        "orchestrator can disagree with can_complete about the same graph")
    # And the module must not restate the status strings the readiness rule
    # turns on. A literal here is how the second copy starts.
    body = inspect.getsource(orc)
    for literal in ('"pending"', "'pending'", '"completed"', "'completed'",
                    '"blocked"', "'blocked'"):
        assert literal not in body, (
            f"{literal} is a status literal — reference the tasks.STATUS_* "
            f"constant so this module cannot drift from the legend")


def test_frontier_agrees_with_tasks_ready_on_every_shape_here():
    """The behavioural companion: whatever `ready` says, an unbounded frontier
    says. If this ever diverges, one of them is wrong and it is not this test's
    job to guess which."""
    for g in (_linear(), _diamond(), _diamond(a_status=t_mod.STATUS_COMPLETED),
              _diamond(a_status=t_mod.STATUS_COMPLETED,
                       c_status=t_mod.STATUS_BLOCKED), {}):
        assert orc.frontier(g) == t_mod.ready(g)


# --- the performance budget --------------------------------------------------

def _worst_case_graph():
    """`MAX_NODES` tasks that are ALL ready, each at full `MAX_DEPTH`.

    The shape matters more than the size, and the obvious fixture measures
    nothing: with a linear dependency chain, `ready` short-circuits on the
    dependency test for every task but the root, so the ancestor walk — the
    expensive part — runs once instead of 2000 times. The true worst case is
    every task ready and every task deep, which forces a full
    `ancestors()` climb per node.

    Deps are satisfied rather than absent (each task depends on a COMPLETED
    node), so the dependency loop is exercised too instead of being skipped on
    an empty tuple."""
    graph = {"done": _task("done", status=t_mod.STATUS_COMPLETED)}
    chain = ["done"]
    for i in range(t_mod.MAX_NODES - 1):
        # Rebuild the parent chain every MAX_DEPTH so no node exceeds the cap
        # (past it the fold reports a violation and the shape stops being the
        # legal worst case).
        if len(chain) > t_mod.MAX_DEPTH:
            chain = ["done"]
        tid = f"t{i}"
        graph[tid] = _task(tid, deps=("done",), parent=chain[-1])
        chain.append(tid)
    return graph


def test_the_frontier_is_under_five_milliseconds_at_max_nodes():
    """The plan's R8, on the worst legal shape rather than a convenient one."""
    graph = _worst_case_graph()
    assert len(graph) == t_mod.MAX_NODES

    # The canary: if the fixture were short-circuiting the way a linear chain
    # does, almost nothing would be ready and the timing would prove nothing.
    ready = orc.frontier(graph)
    assert len(ready) == t_mod.MAX_NODES - 1, (
        f"only {len(ready)} of {t_mod.MAX_NODES} tasks are ready — the fixture "
        f"is short-circuiting and this benchmark measures nothing")

    orc.frontier(graph)                                   # warm any import cost
    # Best of three: this runs on shared CI, and a single sample turns an
    # unrelated scheduling hiccup into a red build.
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        orc.frontier(graph)
        timings.append((time.perf_counter() - start) * 1000)
    assert min(timings) < 5.0, (
        f"frontier took {min(timings):.2f} ms on {t_mod.MAX_NODES} ready nodes")


def test_the_module_does_not_read_the_ledger_to_answer_the_frontier():
    """`frontier` is pure. The loop reads the ledger ONCE per wave and folds it;
    a frontier that read events itself would turn every wave into an O(ledger)
    scan per call."""
    def boom(*a, **k):
        raise AssertionError("frontier must not read the ledger")

    real = events.read_all
    try:
        events.read_all = boom
        assert orc.frontier(_linear()) == ["a"]
    finally:
        events.read_all = real
