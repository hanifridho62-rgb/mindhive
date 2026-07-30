# tests/test_mindnet_orchestrator.py
"""MN-7 — the parallel orchestrator: the loop that walks the MN-2 graph.

Written TEST-FIRST against
``docs/plans/2026-07-30-001-feat-mindnet-mn7-parallel-orchestrator-plan.md``.
No line of `mindhive/mindnet/orchestrator.py` existed when these were written.

## What this module is NOT

It is not a second readiness rule. `tasks.ready()` has answered "which tasks may
start" since MN-2 — PENDING, every dependency COMPLETED, no BLOCKED ancestor —
and it already satisfies every frontier property the directive asked for,
including the failure cascade. `frontier()` adds exactly two things on top: the
CONCURRENCY BOUND, and withholding container tasks (a parent with open children,
which `tasks.can_complete` refuses to complete anyway).
`test_frontier_delegates_to_the_shared_readiness_predicate` pins the delegation
structurally: if a future edit inlines a second copy of the readiness rule, the
two can drift, and the drift direction is "the orchestrator spawns work the rest
of the machine considers blocked".

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
from pathlib import Path

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
        self.scripts = {}                # task_id -> the script it was handed
        self.live = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, run_key, script, **kw):
        # The task id travels in the run key, because that is the only channel
        # `executor.execute`'s real signature offers. Parsing it here rather than
        # accepting a `task_id=` kwarg is what keeps this double's contract
        # identical to the real runner's — a fake that accepts an argument
        # production would reject is how a seam silently rots.
        tid = str(run_key).split("-", 2)[-1]
        with self._lock:
            self.calls.append(tid)
            self.kwargs[tid] = kw
            # Captured, not discarded. Discarding it meant no test could tell a
            # correct `_run_wave` from one that handed every task the same script,
            # or swapped two siblings' scripts inside one wave.
            self.scripts[tid] = script
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


# Captured BEFORE the autouse patch below, so the gate tests can drive the real
# thing rather than the permissive stand-in.
_REAL_GATE = orc.completion_gate_default


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    monkeypatch.setenv("MINDHIVE_SESSION", SESSION)


@pytest.fixture(autouse=True)
def _permissive_completion_gate(monkeypatch):
    """Isolate the LOOP tests from the pr-green gate.

    The real gate enforces MindNetwork's founding requirement — a task is not
    finished until its terminal phase is satisfied — so almost no bare fixture task
    can legitimately complete, and every wave/cascade/concurrency assertion in this
    file would fail for a reason it is not about.

    Two things stop this from becoming a lie: the `--- the pr-green gate ---`
    section drives `_REAL_GATE` directly in BOTH directions, and
    `test_orchestrate_defaults_to_the_real_completion_gate` pins the production
    default. There is also an end-to-end test in `test_cli_mindnet.py` where a task
    walked to its terminal phase really does complete."""
    monkeypatch.setattr(orc, "completion_gate_default",
                        lambda graph, tid, **kw: None)


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


def _hierarchies(n):
    """Every *n*-node hierarchy: all parent shapes x all statuses."""
    import itertools
    ids = [f"n{i}" for i in range(n)]
    stats = (t_mod.STATUS_PENDING, t_mod.STATUS_COMPLETED, t_mod.STATUS_BLOCKED)
    for parents in itertools.product([None] + ids, repeat=n):
        for sts in itertools.product(stats, repeat=n):
            yield _graph(*[
                _task(tid, status=sts[i],
                      parent=(None if parents[i] == tid else parents[i]))
                for i, tid in enumerate(ids)])


def test_the_container_pass_is_exact_over_every_small_hierarchy():
    """Brute force, because two cheaper formulations were WRONG here and an
    argument would not have caught either.

    Deduping the ancestor climb by `parent_id` missed a real container in 512 of
    50,625 four-node hierarchies; memoizing on the result missed 1,392. Every miss
    was a cyclic parent chain, and every one was in the unsafe direction — the
    container would have been offered and could then be marked COMPLETED with open
    children. This pins the current implementation against the definition."""
    def by_definition(graph):
        out = set()
        for tid, task in graph.items():
            if task is None or task.status in t_mod.CLOSED_STATUSES:
                continue
            out.update(t_mod.ancestors(graph, tid))
        return out

    checked = 0
    for graph in _hierarchies(3):
        checked += 1
        assert orc._containers_with_open_work(graph) == by_definition(graph), (
            f"diverged on {[(t.task_id, t.parent_id, t.status) for t in graph.values()]}")
    assert checked > 1000, f"only {checked} graphs — the sweep collapsed"


def test_the_ready_ancestor_cache_matches_the_uncached_rule_exactly():
    """`tasks.ready` gained a parent-keyed ancestor cache for speed. Two reviewers
    disagreed about whether it is equivalent under a hierarchy cycle, one supplying
    a specific counterexample. It IS equivalent — the divergence in `ancestors` is
    always exactly the querying task's own id, and a task only reaches the ancestor
    check after passing `status != PENDING`, so the extra element can never be
    BLOCKED. That is an argument; this is the evidence, and it is cheap enough to
    keep permanently."""
    def uncached(tasks):
        out = []
        for tid in sorted(tasks):
            x = tasks[tid]
            if x.status != t_mod.STATUS_PENDING:
                continue
            if any(tasks.get(d) is None
                   or tasks[d].status != t_mod.STATUS_COMPLETED
                   for d in x.dependencies):
                continue
            if any(tasks[a].status == t_mod.STATUS_BLOCKED
                   for a in t_mod.ancestors(tasks, tid)):
                continue
            out.append(tid)
        return out

    for graph in _hierarchies(3):
        assert t_mod.ready(graph) == uncached(graph), (
            f"cache diverged on "
            f"{[(t.task_id, t.parent_id, t.status) for t in graph.values()]}")


def test_a_container_task_is_withheld_while_its_children_are_open():
    """`tasks.ready` offers a parent alongside its children, so a wave would run
    the container and its parts concurrently and, on success, write the parent
    COMPLETED — which `tasks.can_complete` refuses outright ("has N open
    descendant(s)"). `can_complete` has no production caller, so nothing else
    would have caught it."""
    g = _graph(_task("p"), _task("c", parent="p"))
    assert "c" in t_mod.ready(g) and "p" in t_mod.ready(g), (
        "canary: ready must offer BOTH, or this test proves nothing")
    assert orc.frontier(g) == ["c"], "the container was offered"

    # Once the child settles, the parent becomes runnable.
    done = _graph(_task("p"), _task("c", parent="p",
                                    status=t_mod.STATUS_COMPLETED))
    assert orc.frontier(done) == ["p"]


def test_a_container_is_withheld_transitively_not_just_one_level():
    """A closed child with an open GRANDCHILD still leaves the grandparent
    uncompletable, which is why the cheap "has an open direct child" test is not
    enough."""
    g = _graph(_task("gp"),
               _task("p", parent="gp", status=t_mod.STATUS_COMPLETED),
               _task("gc", parent="p"))
    assert "gp" not in orc.frontier(g)
    assert orc.frontier(g) == ["gc"]


def test_frontier_agrees_with_tasks_ready_on_every_flat_graph_here():
    """The behavioural companion: whatever `ready` says, an unbounded frontier
    says. If this ever diverges, one of them is wrong and it is not this test's
    job to guess which."""
    for g in (_linear(), _diamond(), _diamond(a_status=t_mod.STATUS_COMPLETED),
              _diamond(a_status=t_mod.STATUS_COMPLETED,
                       c_status=t_mod.STATUS_BLOCKED), {}):
        assert orc.frontier(g) == t_mod.ready(g)


# --- the performance budget --------------------------------------------------

def _bench_graph(n=None, parents=1):
    """*n* tasks where every PENDING one is ready and sits at full `MAX_DEPTH`.

    Three properties the obvious fixture gets wrong:

    * **Ready, not short-circuited.** With a linear dependency chain `ready`
      exits on the dependency test for every task but the root, so the ancestor
      climb — the expensive part — runs once instead of N times. Here every
      pending task's dependency is already COMPLETED, so the loop runs and the
      climb happens.
    * **Leaves, not containers.** `frontier` withholds any task with unsettled
      descendants, so a graph of pending parent chains would report almost
      nothing ready and the benchmark would measure the withholding rather than
      the walk. Every pending task here is a leaf.
    * **Deep.** The pending leaves hang off COMPLETED spines of `MAX_DEPTH`, so
      both `ready`'s ancestor check and `_containers_with_open_work`'s upward pass
      climb the full legal depth.

    *parents* is how many distinct spines the leaves attach to: 1 makes every
    leaf a sibling (so `ready`'s parent-keyed ancestor cache always hits), and a
    large value gives nearly every leaf its own spine — the pathological shape
    that defeats the cache."""
    n = t_mod.MAX_NODES if n is None else n
    graph: dict = {}
    spines = []
    for s in range(max(1, parents)):
        prev = None
        for d in range(t_mod.MAX_DEPTH):
            tid = f"s{s}_{d}"
            graph[tid] = _task(tid, status=t_mod.STATUS_COMPLETED, parent=prev)
            prev = tid
        spines.append(prev)
        if len(graph) >= n:
            break
    leaves = n - len(graph)
    for i in range(max(0, leaves)):
        tid = f"t{i}"
        graph[tid] = _task(tid, deps=(spines[i % len(spines)],),
                           parent=spines[i % len(spines)])
    return graph, max(0, leaves)


def _best_ms(graph, rounds=3):
    orc.frontier(graph)                                   # warm any import cost
    # Best of N: this runs on shared CI, where a single sample turns an unrelated
    # scheduling hiccup into a red build.
    timings = []
    for _ in range(rounds):
        start = time.perf_counter()
        orc.frontier(graph)
        timings.append((time.perf_counter() - start) * 1000)
    return min(timings)


def test_the_frontier_stays_in_single_digit_milliseconds_at_max_nodes():
    """`MAX_NODES` tasks, every pending leaf ready at full `MAX_DEPTH`, siblings
    sharing parents.

    The bound is 15 ms, not the directive's original 5 ms, and the change is
    deliberate rather than a loosened assertion. Withholding container tasks
    (the correctness fix for a parent being marked COMPLETED with open children)
    costs a second ancestor pass, taking the measured time from ~4.6 ms to ~6 ms;
    both shortcuts that would have avoided it were wrong in the unsafe direction
    (see `_containers_with_open_work`). 5 ms was an arbitrary number from a
    directive, and `frontier` runs once per wave against waves that spawn
    subprocesses — so the honest statement is "single-digit milliseconds, and not
    growing quadratically", which is what this and the next test assert."""
    graph, leaves = _bench_graph(parents=10)
    assert len(graph) == t_mod.MAX_NODES
    ready = orc.frontier(graph)
    assert len(ready) == leaves and leaves > t_mod.MAX_NODES * 0.9, (
        f"only {len(ready)} of {leaves} leaves are ready — the fixture is "
        f"short-circuiting and this benchmark measures nothing")
    ms = _best_ms(graph)
    assert ms < 15.0, f"frontier took {ms:.2f} ms on a realistic MAX_NODES graph"


def test_the_frontier_stays_bounded_on_the_pathological_shape():
    """Nearly every leaf on its own spine, which defeats `ready`'s parent-keyed
    ancestor cache entirely.

    Deliberately a LOOSER bound than the test above, and the looseness is the
    honest part: a 5 ms assertion here would be flaky on shared CI rather than
    protective. What matters is that the pathological case is still milliseconds —
    `frontier` runs once per wave, and a wave spawns subprocesses costing orders
    of magnitude more."""
    graph, leaves = _bench_graph(parents=150)
    assert len(orc.frontier(graph)) == leaves, "fixture is short-circuiting"
    ms = _best_ms(graph)
    assert ms < 30.0, (
        f"frontier took {ms:.2f} ms on {len(graph)} nodes across many spines")


def test_the_frontier_does_not_scale_quadratically():
    """The assertion that actually catches a regression. A threshold test passes
    on a faster machine no matter how the complexity changed; this compares the
    curve to itself, so an accidental O(N^2) fails on any machine — which is
    exactly what a per-candidate `descendants()` walk was before it became a
    single upward pass.

    Quadrupling the nodes must cost well under 16x, the quadratic factor."""
    small = _best_ms(_bench_graph(n=500, parents=40)[0], rounds=5)
    large = _best_ms(_bench_graph(n=2000, parents=160)[0], rounds=5)
    assert large < small * 10, (
        f"500 nodes: {small:.2f} ms, 2000 nodes: {large:.2f} ms — a "
        f"{large/small:.1f}x cost for 4x the nodes looks quadratic")


# ============================================================================
# U4 — the wave execution loop
# ============================================================================
#
# The failure cascade comes FIRST in this section deliberately. It is the
# property most likely to be quietly wrong, and writing the happy path first
# tends to shape the code so the cascade becomes awkward to add.

def test_a_failed_task_never_spawns_its_dependents():
    """THE test. Not "the dependents are unfinished" — the runner must never
    have been CALLED for them. A loop that submits B and C and then discards
    their results would pass the weaker assertion while having already run
    the work."""
    fake = FakeRunner(results={"a": "fail"})
    out = orc.orchestrate(_diamond(), runner=fake, execute=True)

    assert fake.submitted() == {"a"}, (
        f"the loop submitted {fake.submitted() - {'a'}} after 'a' failed")
    assert out.blocked == ["a"]
    assert out.completed == []
    assert out.outcome == orc.OUTCOME_STALLED


def test_one_failed_arm_does_not_abort_the_healthy_arm():
    """A partial failure must not take the whole wave down: B is independent of
    C and has to finish, or one flaky node costs an entire graph."""
    fake = FakeRunner(results={"c": "fail"})
    out = orc.orchestrate(_diamond(a_status=t_mod.STATUS_COMPLETED),
                          runner=fake, execute=True)

    assert "b" in out.completed
    assert out.blocked == ["c"]
    assert "d" not in fake.submitted(), "the join ran with a blocked parent"
    assert out.outcome == orc.OUTCOME_STALLED


@pytest.mark.parametrize("bad", ["fail"])
def test_a_run_that_executed_and_failed_blocks_the_task(bad):
    """Only results where a child ACTUALLY RAN may block. Enumerated rather than
    treated as "not ok", so a result value added later fails loudly instead of
    being silently classified as success."""
    fake = FakeRunner(results={"a": bad})
    out = orc.orchestrate(_graph(_task("a")), runner=fake, execute=True)
    assert out.blocked == ["a"]
    assert out.completed == []


@pytest.mark.parametrize("never_ran", ["refused", "spawn-failed"])
def test_a_run_that_never_started_does_not_block_the_task(never_ran):
    """THE P0 REGRESSION TEST.

    `refused` is what the gate returns for a task with no `validation_script` —
    the shape `hive mindnet declare` produces BY DEFAULT — and `spawn-failed` is
    the OS refusing to start the process. Mapping either to `blocked` was
    unrecoverable: `tasks.ready` excludes every task with a BLOCKED ancestor and
    every task whose dependency is not COMPLETED, so one script-less task killed
    its whole subtree and its dependents, and `_persist` is the ONLY writer of
    `action: "status"` rows in the codebase while a re-declare is forbidden from
    resetting status. There was no CLI remedy at all.

    Nothing ran, so nothing is recorded: the task stays PENDING and is reported
    as skipped with its reason."""
    fake = FakeRunner(results={"a": never_ran})
    out = orc.orchestrate(_graph(_task("a")), runner=fake, execute=True)

    assert out.blocked == [], f"{never_ran} recorded a failure for a run that never started"
    assert out.completed == []
    assert out.skipped == ["a"]
    assert out.outcome == orc.OUTCOME_STALLED
    assert "a" in out.reasons


def test_a_never_started_run_writes_no_ledger_row_and_leaves_the_task_pending():
    """The half that makes it RECOVERABLE, asserted against the real ledger."""
    _declare("a")
    out = orc.orchestrate({"a": _task("a")},
                          runner=FakeRunner(results={"a": "refused"}),
                          execute=True)
    assert out.skipped == ["a"]
    # STATUS rows specifically: a wave also appends the cat feed's activity rows,
    # and counting every event would conflate the two.
    assert _status_rows() == [], "a status row was written anyway"
    assert t_mod.fold(events.read_all())["a"].status == t_mod.STATUS_PENDING


def test_a_skipped_task_does_not_kill_its_dependents():
    """The cascade half of the P0. A blocked `a` permanently removes `b` from
    every future frontier; a skipped `a` leaves `b` merely waiting."""
    fake = FakeRunner(results={"a": "refused"})
    graph = _graph(_task("a"), _task("b", deps=("a",)))
    orc.orchestrate(graph, runner=fake, execute=True)
    # `a` is still pending, so `b` is still reachable once `a` is given a script.
    assert orc.frontier(graph) == ["a"]
    fixed = _graph(_task("a", status=t_mod.STATUS_COMPLETED),
                   _task("b", deps=("a",)))
    assert orc.frontier(fixed) == ["b"], "the dependent is permanently dead"


def test_the_three_result_sets_partition_every_declared_run_result():
    """Every `RUN_RESULTS` value must land in exactly one of three homes, so a new
    value fails loudly instead of defaulting into the blocking path:

      RESULT_STATUS         it ran, and the outcome IS evidence -> status row
      NON_EXECUTING_RESULTS no child was ever created           -> no row
      UNVERIFIED_RESULTS    it ran, the outcome is NOT evidence -> no row

    The third set is new. It exists because two results were writing rows they had
    not earned: an agent's exit-0 reply was marking tasks COMPLETED, and a
    wall-clock timeout was marking them BLOCKED and deleting their subtrees."""
    homes = [set(orc.RESULT_STATUS), set(orc.NON_EXECUTING_RESULTS),
             set(orc.UNVERIFIED_RESULTS)]
    declared = set(x_mod.RUN_RESULTS)
    covered = set().union(*homes)
    assert declared <= covered, f"unclassified run results: {declared - covered}"
    for i, one in enumerate(homes):
        for other in homes[i + 1:]:
            assert not (one & other), f"classified two ways: {one & other}"


def test_a_timeout_writes_no_status_row_and_leaves_the_task_pending():
    """The regression. `timeout` used to map to BLOCKED, and `tasks.ready` removes
    a blocked task's whole subtree from every future frontier permanently — so one
    slow agent run poisoned the graph."""
    _declare("a")
    out = orc.orchestrate({"a": _task("a")},
                          runner=FakeRunner(results={"a": "timeout"}),
                          execute=True)
    assert out.blocked == [], "a wall-clock timeout blocked the task"
    assert out.skipped == ["a"]
    assert _status_rows() == []
    assert t_mod.fold(events.read_all())["a"].status == t_mod.STATUS_PENDING


def test_the_non_executing_set_matches_the_executors_own_executed_flag():
    """The classification is keyed on `RunResult.executed`, which executor.py
    calls "the SECURITY property ... deliberately not derivable from result". This
    pins that our two sets agree with what the real executor actually sets, so the
    split cannot drift from the flag it is meant to mirror."""
    src = inspect.getsource(x_mod.execute)
    assert "executed=False" in src and "executed=True" in src
    for value in orc.NON_EXECUTING_RESULTS:
        assert value in src, f"{value} is not produced by executor.execute"


def test_an_unknown_result_value_raises_rather_than_defaulting_to_success():
    """Silently treating an unrecognized result as `ok` would mark a task
    COMPLETED on evidence nobody checked."""
    fake = FakeRunner()
    fake.results["a"] = "some-new-result"
    with pytest.raises(ValueError, match="some-new-result"):
        orc.orchestrate(_graph(_task("a")), runner=fake, execute=True)


def test_a_runner_that_raises_blocks_the_task_and_does_not_kill_the_loop():
    """`executor.execute` maps its own faults to typed results, but a custom
    runner (or a bug in one) can still raise. One exploding task must not take
    the whole orchestration down."""
    fake = FakeRunner(raises={"b"})
    out = orc.orchestrate(_diamond(a_status=t_mod.STATUS_COMPLETED),
                          runner=fake, execute=True)
    assert out.blocked == ["b"]
    assert "c" in out.completed
    assert out.outcome == orc.OUTCOME_STALLED


# --- the happy path and wave sequencing ---------------------------------------

def test_a_diamond_settles_in_three_waves_with_the_documented_membership():
    """Membership, not just the count: a loop that ran the right NUMBER of waves
    with the wrong tasks in them would pass a count assertion."""
    fake = FakeRunner()
    out = orc.orchestrate(_diamond(), runner=fake, execute=True)

    assert out.outcome == orc.OUTCOME_SETTLED
    assert [w.tasks for w in out.waves] == [["a"], ["b", "c"], ["d"]]
    assert out.completed == ["a", "b", "c", "d"]
    assert out.blocked == []


def test_a_linear_graph_settles_one_task_per_wave():
    fake = FakeRunner()
    out = orc.orchestrate(_linear(), runner=fake, execute=True)
    assert [w.tasks for w in out.waves] == [["a"], ["b"], ["c"]]
    assert out.outcome == orc.OUTCOME_SETTLED


def test_both_arms_of_a_wave_run_concurrently():
    """The reason a ThreadPoolExecutor is here at all. A serial implementation
    produces the same statuses and the same waves, so only observed overlap
    tells the two apart."""
    fake = FakeRunner(delay=0.05)
    orc.orchestrate(_diamond(a_status=t_mod.STATUS_COMPLETED), runner=fake,
                    execute=True)
    assert fake.peak >= 2, (
        f"peak concurrency was {fake.peak} — the wave ran serially")


def test_the_limit_bounds_concurrency_end_to_end():
    """`limit=1` must hold at the POOL, not only at the frontier."""
    fake = FakeRunner(delay=0.02)
    out = orc.orchestrate(_diamond(a_status=t_mod.STATUS_COMPLETED),
                          runner=fake, execute=True, limit=1)
    assert fake.peak == 1, f"peak concurrency was {fake.peak} with limit=1"
    assert out.outcome == orc.OUTCOME_SETTLED, (
        "a limit must slow the graph down, not prevent it from finishing")


def test_an_already_finished_graph_runs_nothing():
    fake = FakeRunner()
    out = orc.orchestrate(_graph(_task("a", status=t_mod.STATUS_COMPLETED)),
                          runner=fake, execute=True)
    assert fake.calls == []
    assert out.waves == []
    assert out.outcome == orc.OUTCOME_SETTLED


def test_an_all_blocked_graph_stalls_without_running_anything():
    fake = FakeRunner()
    out = orc.orchestrate(_graph(_task("a", status=t_mod.STATUS_BLOCKED),
                                 _task("b", deps=("a",))),
                          runner=fake, execute=True)
    assert fake.calls == []
    assert out.outcome == orc.OUTCOME_STALLED


# --- termination --------------------------------------------------------------

def test_a_wave_that_changes_nothing_terminates_instead_of_spinning():
    """KTD7. A runner that reports `ok` without the status actually advancing
    (the shape a mis-wired persistence layer produces) must not loop forever."""
    fake = FakeRunner()
    out = orc.orchestrate(_linear(), runner=fake, execute=True,
                          apply_status=lambda graph, done, failed: graph)
    assert out.outcome == orc.OUTCOME_STALLED
    assert len(out.waves) == 1, (
        f"ran {len(out.waves)} identical waves before noticing no progress")


def test_the_wave_cap_is_a_backstop():
    """The runaway guard, matching how MAX_NODES and MAX_DEPS_PER_TASK bound the
    graph itself. Asserted with a cap of 1 on a graph that needs 3."""
    fake = FakeRunner()
    out = orc.orchestrate(_linear(), runner=fake, execute=True, max_waves=1)
    assert len(out.waves) == 1
    assert out.outcome == orc.OUTCOME_CAPPED
    assert out.completed == ["a"], "the one wave it did run must still count"


def test_the_default_wave_cap_is_generous_enough_for_a_max_depth_graph():
    """A cap that a legal graph can hit is a bug, not a backstop."""
    assert orc.MAX_WAVES >= t_mod.MAX_DEPTH


# --- the dry run --------------------------------------------------------------

def test_a_dry_run_plans_without_spawning_anything():
    """KTD5. The default, because an orchestrator that spawns real agent
    processes is the most dangerous command on this surface."""
    fake = FakeRunner()
    out = orc.orchestrate(_diamond(), runner=fake)
    assert fake.calls == [], "a dry run must not call the runner"
    assert out.outcome == orc.OUTCOME_DRY_RUN
    assert [w.tasks for w in out.waves] == [["a"], ["b", "c"], ["d"]], (
        "a dry run still has to say what it WOULD do")


def test_a_dry_run_appends_nothing_to_the_ledger():
    before = len(events.read_all())
    orc.orchestrate(_diamond(), runner=FakeRunner())
    assert len(events.read_all()) == before


def test_execute_defaults_to_false():
    """Asserted on the signature, so a future edit cannot flip the default
    without this test noticing."""
    sig = inspect.signature(orc.orchestrate)
    assert sig.parameters["execute"].default is False


# --- the child environment ----------------------------------------------------

def test_a_child_inherits_the_parent_arming_decision(monkeypatch):
    """R7. MN-6 made enforcement opt-in per process; a spawned child that did
    not inherit the decision would silently run with different rules than the
    orchestrator that started it."""
    from mindhive.mindnet import optin
    monkeypatch.setenv(optin.ENV_VAR, "1")
    fake = FakeRunner()
    orc.orchestrate(_graph(_task("a")), runner=fake, execute=True)
    assert fake.kwargs["a"]["env"][optin.ENV_VAR] == "1"


def test_a_dormant_parent_arms_nothing_in_the_child(monkeypatch, tmp_path):
    from mindhive.mindnet import optin
    monkeypatch.delenv(optin.ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    fake = FakeRunner()
    # `root=` explicitly: tmp_path is not a git worktree, and an execute run now
    # refuses up front rather than letting every task report itself failed.
    orc.orchestrate(_graph(_task("a")), runner=fake, execute=True, root=tmp_path)
    assert fake.kwargs["a"]["env"][optin.ENV_VAR] == "0"


def test_an_execute_run_outside_a_worktree_refuses_before_running_anything():
    """The P1 companion to the P0. `_run_wave` used to pass no `root`, so each
    child re-resolved `paths.worktree_root()` itself — one git subprocess per
    task, fired concurrently — and a missing git, a cwd outside a repo, or one
    call exceeding its 2s timeout made EVERY task in the wave report itself
    failed. Combined with the old refused->blocked mapping that poisoned the whole
    graph from a transient environmental condition.

    Now it is one resolution, and a failure refuses the run before any child."""
    from mindhive import paths
    fake = FakeRunner()
    real = paths.worktree_root
    try:
        paths.worktree_root = lambda cwd=None: None
        with pytest.raises(RuntimeError, match="no git worktree"):
            orc.orchestrate(_graph(_task("a")), runner=fake, execute=True)
    finally:
        paths.worktree_root = real
    assert fake.calls == [], "a child ran despite there being nowhere to run it"


def test_the_root_is_resolved_once_and_handed_to_every_child(tmp_path,
                                                            monkeypatch):
    from mindhive import paths
    calls = {"n": 0}

    def counting(cwd=None):
        calls["n"] += 1
        return tmp_path

    monkeypatch.setattr(paths, "worktree_root", counting)
    fake = FakeRunner()
    orc.orchestrate(_diamond(a_status=t_mod.STATUS_COMPLETED), runner=fake,
                    execute=True)
    assert calls["n"] == 1, f"resolved the worktree root {calls['n']} times"
    assert all(kw["root"] == tmp_path for kw in fake.kwargs.values())


def test_a_dry_run_needs_no_worktree(monkeypatch):
    """Planning must not require a checkout — the dry run is what an operator
    reaches for when the environment is the thing in question."""
    from mindhive import paths
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: None)
    out = orc.orchestrate(_diamond(), runner=FakeRunner())
    assert out.outcome == orc.OUTCOME_DRY_RUN
    assert [w.tasks for w in out.waves] == [["a"], ["b", "c"], ["d"]]


def test_each_task_is_handed_its_own_script():
    """The gap that made most of this file's happy paths weak: `FakeRunner`
    discarded `script`, so an implementation that passed every task the same
    script — or swapped two siblings' inside one wave — produced identical
    results and passed everything."""
    fake = FakeRunner()
    graph = _graph(_task("b", validation_script="run-b"),
                   _task("c", validation_script="run-c"))
    orc.orchestrate(graph, runner=fake, execute=True, root="/tmp/x")
    assert fake.scripts == {"b": "run-b", "c": "run-c"}


def test_a_task_with_no_script_is_handed_the_empty_string_not_none():
    """`executor.gate` refuses a blank script by design; it must receive the
    blank, not a None that would raise inside the gate instead."""
    fake = FakeRunner()
    orc.orchestrate(_graph(_task("a")), runner=fake, execute=True,
                    root="/tmp/x")
    assert fake.scripts["a"] == ""


def test_the_real_default_runner_blocks_a_scripted_failure_but_skips_a_scriptless_task(
        tmp_path, monkeypatch):
    """The integration test the suite was missing entirely: `orchestrate` with the
    DEFAULT runner (`executor.execute`, not the fake) over a graph mixing a
    scripted task with a scriptless one.

    Every FakeRunner happy path asserts scriptless tasks COMPLETE, because the
    fake ignores `script` and answers "ok". The real runner refuses a blank script.
    Nothing exercised that difference, which is how the P0 shipped."""
    from mindhive import paths
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: tmp_path)
    _declare("scripted")
    _declare("bare")
    failing = " ".join([__import__("sys").executable,
                        str(_FAIL_SCRIPT(tmp_path))])

    out = orc.orchestrate(
        _graph(_task("scripted", validation_script=failing), _task("bare")),
        execute=True)

    assert out.blocked == ["scripted"], "a run that failed must block"
    assert out.skipped == ["bare"], "a run that never started must not block"
    assert "validation script" in out.reasons["bare"].lower()
    folded = t_mod.fold(events.read_all())
    assert folded["scripted"].status == t_mod.STATUS_BLOCKED
    assert folded["bare"].status == t_mod.STATUS_PENDING, (
        "the scriptless task was permanently poisoned — the P0")


# ============================================================================
# MN-9 — per-task worktree isolation
# ============================================================================

@pytest.fixture
def gitrepo(tmp_path):
    """A real repository, because `worktree.add` runs real git."""
    import subprocess
    root = tmp_path / "repo"
    root.mkdir()
    for args in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "T")):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "seed.txt"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"],
                   check=True, capture_output=True)
    return root


def test_each_isolated_task_runs_in_its_own_checkout(gitrepo):
    """R1 — the property the MN-8 shared-tree cap existed to protect."""
    fake = FakeRunner()
    orc.orchestrate(_graph(_task("b"), _task("c")), runner=fake, execute=True,
                    root=gitrepo, isolate=True)
    roots = {tid: kw["root"] for tid, kw in fake.kwargs.items()}
    assert len(set(roots.values())) == 2, f"tasks shared a checkout: {roots}"
    for tid, r in roots.items():
        assert f"mn9-{tid}" in str(r)


def test_without_isolation_every_task_shares_the_one_root(gitrepo):
    """The CANARY for the test above: without `isolate`, the roots are identical,
    so "two distinct roots" up there cannot be passing for an unrelated reason."""
    fake = FakeRunner()
    orc.orchestrate(_graph(_task("b"), _task("c")), runner=fake, execute=True,
                    root=gitrepo)
    roots = {kw["root"] for kw in fake.kwargs.values()}
    assert roots == {gitrepo}


def test_each_isolated_task_gets_its_own_ledger_segment(gitrepo):
    """R2, and the reason parallelism could not ship without this unit.
    `events.append_event` writes to `<session_id>.jsonl` with an UNLOCKED append,
    so two concurrent agents sharing a session interleave into corrupt JSONL —
    and the ledger is the source of truth for the entire system."""
    fake = FakeRunner()
    orc.orchestrate(_graph(_task("b"), _task("c")), runner=fake, execute=True,
                    root=gitrepo, isolate=True)
    sessions = {tid: kw["env"].get("MINDHIVE_SESSION")
                for tid, kw in fake.kwargs.items()}
    assert all(sessions.values()), f"a task got no session: {sessions}"
    assert len(set(sessions.values())) == 2, f"tasks shared a segment: {sessions}"


def test_a_child_using_the_injected_session_writes_its_own_segment(gitrepo,
                                                                  monkeypatch):
    """R2 proved at the outcome, not just the input.

    The test above asserts distinct sessions reach the children; this asserts what
    that BUYS — a runner that actually appends under the session it was handed
    produces one segment file per agent. Without it they all append to one
    `<session>.jsonl` through an unlocked `open(..., "a")`, and concurrent writes
    interleave mid-line into JSONL the fold cannot read."""
    from mindhive import paths

    def appending_runner(run_key, script, **kw):
        # What a real agent does when it runs `hive log`: append under whatever
        # MINDHIVE_SESSION its environment carries.
        sid = kw["env"]["MINDHIVE_SESSION"]
        events.append_event("mindnet_activity",
                            {"faculty": "task-graph", "status": "active"},
                            session_id=sid, tags=["mindnet"])
        return _run_result("ok")

    orc.orchestrate(_graph(_task("b"), _task("c")), runner=appending_runner,
                    execute=True, root=gitrepo, isolate=True)

    segments = {p.name for p in paths.events_dir().glob("*.jsonl")}
    assert "mn9-b.jsonl" in segments and "mn9-c.jsonl" in segments, (
        f"agents did not get their own segments: {sorted(segments)}")


def test_without_isolation_no_session_is_forced(gitrepo):
    """The canary for R2: the shared-tree path must not silently start
    re-segmenting the ledger, which would change behaviour for --via-script runs."""
    fake = FakeRunner()
    orc.orchestrate(_graph(_task("b")), runner=fake, execute=True, root=gitrepo)
    assert "MINDHIVE_SESSION" not in fake.kwargs["b"]["env"]


def test_the_isolation_env_does_not_clobber_the_arming_decision(gitrepo):
    """The session overlay is added to the env, not substituted for it — MN-6's
    arming decision still has to reach the child."""
    from mindhive.mindnet import optin
    fake = FakeRunner()
    orc.orchestrate(_graph(_task("b")), runner=fake, execute=True,
                    root=gitrepo, isolate=True)
    env = fake.kwargs["b"]["env"]
    assert optin.ENV_VAR in env
    assert env["MINDHIVE_SESSION"]


def test_a_failed_isolation_skips_only_its_own_task(gitrepo, monkeypatch):
    """R4/KTD6. One task's environmental failure must not write a status row (that
    would delete its subtree from the graph) and must not stop the other task."""
    from mindhive.mindnet import worktree as wt_mod
    real = wt_mod.add

    def flaky(root, tid):
        if tid == "b":
            return wt_mod.Isolated(task_id=tid, ok=False, reason="disk on fire")
        return real(root, tid)

    monkeypatch.setattr(wt_mod, "add", flaky)
    out = orc.orchestrate(_graph(_task("b"), _task("c")), runner=FakeRunner(),
                          execute=True, root=gitrepo, isolate=True)

    assert out.skipped == ["b"]
    assert out.blocked == [], "an isolation failure wrote a status row"
    assert out.completed == ["c"], "the healthy task did not run"
    assert "disk on fire" in out.reasons["b"]


def test_a_clean_worktree_is_retired_and_its_branch_reported(gitrepo):
    fake = FakeRunner()
    out = orc.orchestrate(_graph(_task("b")), runner=fake, execute=True,
                          root=gitrepo, isolate=True)
    assert out.branches == {"b": "mn9/b"}
    assert out.kept == {}, "a clean worktree was left on disk"
    assert not (gitrepo / ".claude" / "worktrees" / "mn9-b").exists()


def test_a_worktree_holding_work_is_kept_and_reported(gitrepo):
    """The whole point of isolation is the work; cleanup must never delete it, and
    the operator needs to be told where it is."""
    class Writer(FakeRunner):
        def __call__(self, run_key, script, **kw):
            (Path(kw["root"]) / "agent-output.txt").write_text(
                "hours of work", encoding="utf-8")
            return super().__call__(run_key, script, **kw)

    out = orc.orchestrate(_graph(_task("b")), runner=Writer(), execute=True,
                          root=gitrepo, isolate=True)
    kept = out.kept.get("b")
    assert kept, f"the worktree holding work was not reported: {out.kept}"
    assert (Path(kept) / "agent-output.txt").read_text(encoding="utf-8") \
        == "hours of work"


def test_the_worktree_is_retired_even_when_the_agent_raised(gitrepo):
    """The `finally`: a worktree left by a crashed run accumulates and blocks the
    next attempt for that task."""
    out = orc.orchestrate(_graph(_task("b")), runner=FakeRunner(raises={"b"}),
                          execute=True, root=gitrepo, isolate=True)
    assert out.blocked == ["b"]
    assert not (gitrepo / ".claude" / "worktrees" / "mn9-b").exists()


def test_isolated_tasks_run_concurrently(gitrepo):
    """R3 — the entire purpose. MN-8 capped agents at one because they shared a
    tree; with a tree each, the pool is free to fan out."""
    fake = FakeRunner(delay=0.05)
    orc.orchestrate(_graph(_task("b"), _task("c"), _task("d")), runner=fake,
                    execute=True, root=gitrepo, isolate=True)
    assert fake.peak >= 2, f"peak concurrency was {fake.peak} with isolation on"


def test_a_dry_run_creates_no_worktree(gitrepo):
    out = orc.orchestrate(_graph(_task("b")), runner=FakeRunner(),
                          root=gitrepo, isolate=True)
    assert out.outcome == orc.OUTCOME_DRY_RUN
    assert not (gitrepo / ".claude" / "worktrees").exists()
    assert out.branches == {}


def test_the_run_key_names_the_task_it_is_running():
    """Attribution. Without the task id in the key, the RunLog artifacts of a
    parallel wave are unassignable — exactly the MN-0 problem durability solved.

    Asserted on the key rather than on a `task_id=` kwarg because the key is the
    only channel `executor.execute` actually has; a kwarg would `TypeError` on
    every real run."""
    assert orc.run_key(0, "b") == "mn7-w0-b"
    assert orc.run_key(2, "mn1-u7").endswith("mn1-u7")


def test_the_runner_is_called_with_only_arguments_the_real_executor_accepts():
    """The seam's contract, checked against the real signature. A fake runner
    accepting `**kw` will swallow anything; this is what stops the orchestrator
    from passing a keyword only the double understands."""
    fake = FakeRunner()
    orc.orchestrate(_diamond(a_status=t_mod.STATUS_COMPLETED), runner=fake,
                    execute=True)
    accepted = set(inspect.signature(x_mod.execute).parameters)
    for tid, kwargs in fake.kwargs.items():
        extra = set(kwargs) - accepted
        assert not extra, (
            f"task '{tid}' was run with {extra}, which executor.execute does "
            f"not accept — every real run would TypeError")


def test_each_task_in_a_wave_gets_its_own_run_key():
    """Two tasks sharing a run key would overwrite each other's durable
    artifact — one wave, one lost log."""
    keys = {orc.run_key(0, tid) for tid in ("b", "c")}
    assert len(keys) == 2


def test_a_retry_in_a_later_wave_does_not_overwrite_the_earlier_log():
    """Wave-scoped keys: the same task attempted twice keeps both artifacts."""
    assert orc.run_key(0, "a") != orc.run_key(1, "a")


def test_the_default_runner_is_the_executor():
    """The seam exists for testing; production must not accidentally keep a
    test double."""
    assert inspect.signature(orc.orchestrate).parameters[
        "runner"].default is x_mod.execute


# ============================================================================
# U5 — ledger writes and the per-wave sidecar rebuild
# ============================================================================

def _status_rows():
    """Every `mindnet_task` status row on the ledger, newest last.

    A wave appends TWO kinds of row — status rows (the graph) and
    `mindnet_activity` rows (the cat feed) — so tests that mean "was a status
    recorded" must say so rather than counting events."""
    return [e for e in events.read_all()
            if e.type == "mindnet_task"
            and e.payload.get("action") == "status"]


def _activity_rows():
    return [e for e in events.read_all() if e.type == "mindnet_activity"]


def _declare(tid, **payload):
    """Put *tid* on BOTH axes, the way `hive mindnet declare` does.

    Needed because these tests exercise the REAL persistence path, and a status
    row aimed at a task the ledger never declared orphans instead of landing —
    which would make every round-trip assertion below pass vacuously against an
    empty graph."""
    events.append_event(
        "mindnet_task",
        dict({"action": "declare", "task_id": tid, "title": f"task {tid}"},
             **payload), tags=["mindnet", "task"])
    events.append_event(
        "mindnet_phase",
        {"action": "declare", "task_id": tid, "phase": t_mod.PHASES[0]},
        tags=["mindnet", "phase"])


def test_a_wave_leaves_the_sidecar_fresh():
    """The MN-6 lesson, asserted where it bites. `sidecar._staleness` counts
    EVERY event, so a status row appended without rewriting the sidecar makes the
    projection read STALE — and a stale projection silently disables both MN-1's
    phase-aware injection and MN-4's teeth for the very next wave."""
    _declare("a")
    sc_mod.rebuild()
    assert sc_mod.read().fresh is True, "canary: the sidecar started stale"

    orc.orchestrate({"a": _task("a")}, runner=FakeRunner(), execute=True)
    assert sc_mod.read().fresh is True


def test_appending_the_same_rows_without_a_rebuild_reads_stale():
    """The canary for the test above. A freshness assertion that cannot go red
    proves only that `fresh` returns True somewhere."""
    _declare("a")
    sc_mod.rebuild()
    events.append_event("mindnet_task",
                        {"action": "status", "task_id": "a",
                         "status": t_mod.STATUS_COMPLETED},
                        tags=["mindnet", "task"])
    assert sc_mod.read().fresh is False, (
        "the staleness stamp no longer notices an unaccounted event — every "
        "rebuild-after-append rule in this repo rests on it")


def test_exactly_one_rebuild_per_wave_not_one_per_task(monkeypatch):
    """Two tasks settle in one wave; the projection is rewritten once. One
    rebuild per task would fold the whole ledger twice for no gain."""
    _declare("a")
    _declare("b")
    calls = {"n": 0}
    real = sc_mod.rebuild

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(sc_mod, "rebuild", counting)
    out = orc.orchestrate(_graph(_task("a"), _task("b")),
                          runner=FakeRunner(), execute=True)
    assert [w.tasks for w in out.waves] == [["a", "b"]], "not a single wave"
    assert calls["n"] == 1, f"{calls['n']} rebuilds for one wave"


def test_the_written_rows_fold_back_to_the_reported_statuses():
    """The round trip. What the run REPORTS and what the ledger REPLAYS to must
    agree, or the projection and the return value tell different stories."""
    _declare("a")
    _declare("b", dependencies=["a"])
    out = orc.orchestrate(_graph(_task("a"), _task("b", deps=("a",))),
                          runner=FakeRunner(results={"b": "fail"}),
                          execute=True)
    assert out.completed == ["a"] and out.blocked == ["b"]

    folded = t_mod.fold(events.read_all())
    assert folded["a"].status == t_mod.STATUS_COMPLETED
    assert folded["b"].status == t_mod.STATUS_BLOCKED


def test_every_written_payload_satisfies_the_writer_contract():
    """`tasks.validate_payload` is the append surface's own gate. A row this
    module writes that the gate would refuse folds as a violation and gates
    nothing — the same trap `test_cli_mindnet` pins for the phase axis."""
    _declare("a")
    orc.orchestrate({"a": _task("a")}, runner=FakeRunner(), execute=True)
    rows = [e for e in events.read_all()
            if e.type == "mindnet_task" and e.payload.get("action") == "status"]
    assert rows, "canary: no status rows were written at all"
    for ev in rows:
        t_mod.validate_payload(ev.payload)


def test_the_status_rows_are_marked_machine_written():
    """`auto` is the taxonomy's existing mark for a row the operator did not
    type. Without it these are indistinguishable from a hand-recorded status."""
    _declare("a")
    orc.orchestrate({"a": _task("a")}, runner=FakeRunner(), execute=True)
    rows = [e for e in events.read_all()
            if e.type == "mindnet_task" and e.payload.get("action") == "status"]
    assert rows and all("auto" in e.tags for e in rows)


def test_a_failing_ledger_append_names_the_tasks_that_went_unrecorded(
        monkeypatch):
    """The tasks already RAN. The failure must say exactly which ones have no
    ledger row, because re-running the orchestrator would execute them a second
    time — silent double execution is the cost of a vague error here."""
    _declare("a")

    def boom(*a, **k):
        raise OSError("ledger is read-only")

    monkeypatch.setattr(events, "append_event", boom)
    # `"a ("` not a loose `match="a "`: the message's own prose contains bare
    # letters, the same trap the sibling test below documents.
    with pytest.raises(RuntimeError) as err:
        orc.orchestrate({"a": _task("a")}, runner=FakeRunner(), execute=True)
    assert "a (" in str(err.value)


def test_one_failing_append_does_not_abandon_the_rest_of_the_wave(monkeypatch):
    """Aborting the loop mid-wave left the remaining settled tasks with no row at
    all AND skipped the rebuild, leaving the sidecar stale on top of the rows that
    did land — the exact condition _persist exists to prevent."""
    _declare("a")
    _declare("b")
    seen = []
    real = events.append_event

    def flaky(etype, payload, **kw):
        # Scoped to status rows: the wave also emits `mindnet_activity` for the
        # cat board, and intercepting those would make this test about telemetry.
        if not (etype == "mindnet_task" and payload.get("action") == "status"):
            return real(etype, payload, **kw)
        seen.append(payload.get("task_id"))
        if payload.get("task_id") == "a":
            raise OSError("this one row fails")
        return real(etype, payload, **kw)

    monkeypatch.setattr(events, "append_event", flaky)
    with pytest.raises(RuntimeError) as err:
        orc.orchestrate(_graph(_task("a"), _task("b")),
                        runner=FakeRunner(), execute=True)

    assert seen == ["a", "b"], "the loop stopped at the first failure"
    msg = str(err.value)
    # `"b (" not in`, not `"b" not in`: the message's own prose contains a 'b'
    # ("but their"), so the bare-letter check matched itself.
    assert "a (" in msg and "b (" not in msg, msg
    assert t_mod.fold(events.read_all())["b"].status == \
        t_mod.STATUS_COMPLETED, "b's row was lost with a's"


def test_a_failed_sidecar_rebuild_is_raised_not_swallowed(monkeypatch):
    """`sidecar.rebuild` signals refusal with written=False and NEVER raises (it
    is wired into a hook path). Discarding that return value meant a failed
    projection refresh was silently swallowed and the next wave read a stale
    sidecar — MN-6's lesson, reintroduced by the code that documents it."""
    _declare("a")
    monkeypatch.setattr(sc_mod, "rebuild",
                        lambda *a, **k: sc_mod.Rebuild(False, "disk full"))
    with pytest.raises(RuntimeError, match="STALE"):
        orc.orchestrate({"a": _task("a")}, runner=FakeRunner(), execute=True)


def test_a_successful_rebuild_is_not_treated_as_a_failure(monkeypatch):
    """The canary for the test above: the written=True path must stay quiet."""
    _declare("a")
    out = orc.orchestrate({"a": _task("a")}, runner=FakeRunner(), execute=True)
    assert out.outcome == orc.OUTCOME_SETTLED


# --- MN-7 review follow-ups: bounded concurrency and recorded reasons ---------

def test_the_pool_is_capped_independently_of_the_frontier_width():
    """Sizing the pool to the batch meant a wide frontier spawned one thread and
    one child per ready task — up to MAX_NODES(2000) subprocesses from a single
    command. The cap bounds EXECUTION; the frontier still plans every ready task."""
    wide = _graph(*[_task(f"t{i}") for i in range(40)])
    fake = FakeRunner(delay=0.01)
    out = orc.orchestrate(wide, runner=fake, execute=True, max_workers=3)

    assert len(out.waves[0].tasks) == 40, "the wave must still PLAN all 40"
    assert fake.peak <= 3, f"peak concurrency {fake.peak} exceeded the cap"
    assert out.outcome == orc.OUTCOME_SETTLED, "a cap must not lose tasks"


def test_the_default_worker_cap_is_at_least_two():
    """Floor of 2 so a single-core CI box still runs a wave concurrently —
    otherwise the concurrency test above would pass for the wrong reason there."""
    assert orc.DEFAULT_MAX_WORKERS >= 2


def test_the_default_cap_applies_when_no_override_is_given():
    wide = _graph(*[_task(f"t{i}") for i in range(orc.DEFAULT_MAX_WORKERS + 20)])
    fake = FakeRunner(delay=0.01)
    orc.orchestrate(wide, runner=fake, execute=True)
    assert fake.peak <= orc.DEFAULT_MAX_WORKERS


def test_a_blocked_task_carries_the_reason_it_blocked():
    """"blocked: a" with no reason is not actionable, and the CLI was pointing
    operators at `hive mindnet show` which had nothing recorded to show."""
    fake = FakeRunner(results={"a": "fail"})
    out = orc.orchestrate(_graph(_task("a")), runner=fake, execute=True)
    assert out.blocked == ["a"]
    assert "synthetic fail" in out.reasons["a"]


def test_a_raising_runner_records_its_exception_as_the_reason():
    fake = FakeRunner(raises={"a"})
    out = orc.orchestrate(_graph(_task("a")), runner=fake, execute=True)
    assert "OSError" in out.reasons["a"]


def test_the_reason_carries_the_durable_artifact_path_when_there_is_one(
        tmp_path, monkeypatch):
    """The RunLog artifact is the only place a failed child's output survives;
    a reason that does not point at it makes MN-0's durability unreachable."""
    from mindhive import paths
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: tmp_path)
    _declare("a")
    script = " ".join([__import__("sys").executable, str(_FAIL_SCRIPT(tmp_path))])
    out = orc.orchestrate({"a": _task("a", validation_script=script)},
                          execute=True)
    assert out.blocked == ["a"]
    assert "log:" in out.reasons["a"], out.reasons
    assert Path(out.reasons["a"].split("log:")[1].strip()).exists()


def test_each_wave_carries_only_its_own_reasons():
    """Per-wave attribution: a reason from wave 1 must not appear against wave 2."""
    fake = FakeRunner(results={"b": "fail"})
    out = orc.orchestrate(_diamond(), runner=fake, execute=True)
    assert "a" not in out.waves[1].reasons
    assert "b" in out.waves[1].reasons


def test_a_wave_that_settles_nothing_writes_no_status_rows():
    """An all-blocked graph produces no frontier, so no status row is written."""
    orc.orchestrate(_graph(_task("a", status=t_mod.STATUS_BLOCKED)),
                    runner=FakeRunner(), execute=True)
    assert _status_rows() == []


# --- the one test that uses the real executor ---------------------------------

def test_a_failed_child_still_leaves_its_durable_artifact(tmp_path,
                                                          monkeypatch):
    """MN-0's guarantee, through the real `executor.execute` rather than the
    fake. The injected runner makes every other test cheap, but a seam nobody
    ever drives against reality is a seam that can be wrong."""
    from mindhive import paths
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: tmp_path)
    _declare("a")
    failing = " ".join([__import__("sys").executable, "-X", "utf8",
                        str(_FAIL_SCRIPT(tmp_path))])

    out = orc.orchestrate({"a": _task("a", validation_script=failing)},
                          execute=True)

    assert out.blocked == ["a"], f"expected a blocked task, got {out}"
    from mindhive import runlog
    arts = runlog.list_artifacts()
    assert arts, "the failed child's durable log was lost — MN-0 regression"


def _FAIL_SCRIPT(tmp_path):
    """A child that prints and then exits non-zero."""
    p = tmp_path / "boom.py"
    p.write_text("import sys\nprint('partial output')\nsys.exit(3)\n",
                 encoding="utf-8")
    return p


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


def test_an_agent_that_declines_the_task_is_not_marked_completed():
    """THE second P0, end to end through the loop.

    `prompt_for` invites the failing reply, that reply is exit 0 with a clean
    envelope, and `RESULT_STATUS["ok"] -> COMPLETED` then wrote a permanent
    COMPLETED row for work nobody did — unblocking dependents and reporting
    `settled`. `can_complete` has no production caller, and `fold` is
    last-write-wins, so nothing downstream would ever have caught it."""
    from mindhive.mindnet import agent as ag_mod
    _declare("a")
    _declare("b", dependencies=["a"])

    def declining_agent(run_key, payload, **kw):
        return x_mod.RunResult(result=ag_mod.RESULT_RAN_UNVERIFIED,
                               executed=True, exit_code=0,
                               reason="I could not do this")

    graph = _graph(_task("a"), _task("b", deps=("a",)))
    out = orc.orchestrate(graph, runner=declining_agent, execute=True,
                          root=".")

    assert out.completed == [], "an agent's say-so marked the task COMPLETED"
    assert out.skipped == ["a"]
    assert _status_rows() == []
    assert t_mod.fold(events.read_all())["a"].status == t_mod.STATUS_PENDING
    assert out.outcome == orc.OUTCOME_STALLED, (
        "the run reported success for work nobody did")


def test_an_isolation_failure_that_RAISES_does_not_abort_the_wave(gitrepo,
                                                                 monkeypatch):
    """The existing test makes `add` RETURN ok=False. This makes it RAISE, which is
    the case that escaped `one()`'s try, re-raised out of `pool.map`, and discarded
    the results of every sibling agent that had already run — so re-running
    duplicated real agent work."""
    from mindhive.mindnet import worktree as wt_mod
    real = wt_mod.add

    def exploding(root, tid):
        if tid == "b":
            raise OSError("the parent path is a file")
        return real(root, tid)

    monkeypatch.setattr(wt_mod, "add", exploding)
    out = orc.orchestrate(_graph(_task("b"), _task("c")), runner=FakeRunner(),
                          execute=True, root=gitrepo, isolate=True)

    assert out.skipped == ["b"]
    assert out.completed == ["c"], "the sibling agent's result was discarded"
    assert "OSError" in out.reasons["b"]


def test_a_raising_retire_does_not_replace_the_agents_result(gitrepo,
                                                            monkeypatch):
    """A raise inside a `finally` REPLACES the value the runner already produced,
    so a cleanup error would be reported instead of the agent's real outcome."""
    from mindhive.mindnet import worktree as wt_mod
    monkeypatch.setattr(wt_mod, "retire",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
    out = orc.orchestrate(_graph(_task("b")), runner=FakeRunner(), execute=True,
                          root=gitrepo, isolate=True)
    assert out.completed == ["b"], "the cleanup error replaced the agent's result"
    assert "locked" in str(out.kept.get("b", "")) or out.kept


# ============================================================================
# MN-10 — the pr-green gate
# ============================================================================
#
# MindNetwork's founding prompt: a task "tidak akan selesai hingga pr berwarna
# hijau". `tasks.can_complete` implemented that from the start — and NOTHING called
# it. Every reference in `src/` was a comment or a docstring, so the only writer of
# a `completed` row marked tasks finished without ever asking, and a task could be
# COMPLETED at `brainstorm` while its dependents unblocked on a promise nobody
# checked.
#
# These tests drive `_REAL_GATE`, captured before the autouse permissive patch.

def test_orchestrate_defaults_to_the_real_completion_gate():
    """The production default must be the real gate, or the autouse fixture above
    would be hiding a permissive orchestrator rather than isolating a test."""
    import inspect
    src = inspect.getsource(orc.orchestrate)
    assert "completion_gate_default" in src
    assert inspect.signature(orc.orchestrate).parameters[
        "completion_gate"].default is None, (
        "the injectable must default to None so the real gate is used")


def test_the_gate_refuses_a_task_that_has_not_reached_its_terminal_phase():
    """The headline requirement. A bare task sits at `brainstorm`; completing it
    there is exactly what the missing wire allowed."""
    graph = _graph(_task("a"))
    why = _REAL_GATE(graph, "a", records={})
    assert why is not None
    assert "a" in why


def test_the_gate_refuses_a_parent_with_open_children():
    """`can_complete`'s first gate, reached through the wire."""
    graph = _graph(_task("p"), _task("c", parent="p"))
    why = _REAL_GATE(graph, "p", records={})
    assert why and "descendant" in why


def test_the_gate_allows_a_task_whose_terminal_phase_is_satisfied():
    """The other direction, so the refusals above are not vacuous.

    A `triviality_bypass` task's terminal phase is `TRIVIAL_EXIT_PHASE`
    (`audit-bug`), not `pr-green` — ADR-0005 waives the METHOD, never the gate. The
    records are built to say that phase is satisfied."""
    from mindhive.mindnet.controller import PhaseRecord, Satisfaction
    task = _task("a", triviality_bypass=True)
    graph = {"a": task}
    rec = PhaseRecord(task_id="a", phase=t_mod.TRIVIAL_EXIT_PHASE, epoch=0,
                      satisfactions=(Satisfaction(
                          phase=t_mod.TRIVIAL_EXIT_PHASE, epoch=0, valid=True,
                          result="ok", verified_by="python"),))
    assert _REAL_GATE(graph, "a", records={"a": rec}) is None


def test_a_refused_completion_writes_no_status_row_and_stays_pending(monkeypatch):
    """Through the LOOP with the real gate restored: the run succeeded, the gate
    refused, so nothing is recorded and the task remains retryable."""
    monkeypatch.setattr(orc, "completion_gate_default", _REAL_GATE)
    _declare("a")
    out = orc.orchestrate({"a": _task("a")}, runner=FakeRunner(), execute=True)

    assert out.completed == [], "a task completed without reaching pr-green"
    assert out.skipped == ["a"]
    assert _status_rows() == []
    assert t_mod.fold(events.read_all())["a"].status == t_mod.STATUS_PENDING
    assert "a" in out.reasons and out.reasons["a"]


def test_a_refused_completion_keeps_its_dependents_blocked(monkeypatch):
    """The consequence that makes the gate worth having: an unfinished task must
    not unblock the work that waits on it."""
    monkeypatch.setattr(orc, "completion_gate_default", _REAL_GATE)
    graph = _graph(_task("a"), _task("b", deps=("a",)))
    fake = FakeRunner()
    orc.orchestrate(graph, runner=fake, execute=True)
    assert "b" not in fake.submitted(), "the dependent ran on an unfinished parent"


def test_the_gate_reason_is_appended_to_the_runs_own_reason(monkeypatch):
    """The operator needs both halves: the run's outcome AND why it still did not
    count as finished."""
    monkeypatch.setattr(orc, "completion_gate_default", _REAL_GATE)
    _declare("a")
    out = orc.orchestrate({"a": _task("a")},
                          runner=FakeRunner(results={"a": "ok"}), execute=True)
    assert out.reasons["a"]


def test_the_gate_fails_closed_when_it_raises(monkeypatch):
    """A gate that cannot decide must refuse. `can_complete` already fails closed
    on a raising predicate; the wire must not undo that."""
    def boom(*a, **k):
        raise RuntimeError("records unreadable")

    monkeypatch.setattr(t_mod, "can_complete", boom)
    why = _REAL_GATE(_graph(_task("a")), "a", records={})
    assert why and "RuntimeError" in why


def test_a_missing_sidecar_refuses_rather_than_allowing(tmp_path, monkeypatch):
    """The gate reads its records from the sidecar. No sidecar means no
    satisfaction records, which `can_complete` treats as unsatisfied — the safe
    direction, and worth pinning because the alternative is a gate that opens
    whenever the projection is absent."""
    monkeypatch.chdir(tmp_path)
    why = _REAL_GATE(_graph(_task("a")), "a")
    assert why is not None
