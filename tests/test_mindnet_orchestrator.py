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

def _bench_graph(n=None, fanout=1):
    """*n* tasks that are ALL ready, each at full `MAX_DEPTH`.

    The shape matters more than the size, and the obvious fixture measures
    nothing: with a linear dependency chain, `ready` short-circuits on the
    dependency test for every task but the root, so the ancestor climb — the
    expensive part — runs once instead of N times. Here every task is ready AND
    deep, forcing the climb per chain, and dependencies are satisfied rather than
    absent so the dependency loop runs too instead of skipping an empty tuple.

    *fanout* controls how many siblings share a parent. ``1`` gives every task
    its own parent, which defeats `ready`'s parent-keyed ancestor cache — the
    pathological shape. A larger fanout is what real graphs look like."""
    n = t_mod.MAX_NODES if n is None else n
    graph = {"done": _task("done", status=t_mod.STATUS_COMPLETED)}
    chain = ["done"]
    for i in range(n - 1):
        # Restart the parent chain at MAX_DEPTH: past the cap the fold reports a
        # violation and the shape stops being the legal worst case.
        if len(chain) > t_mod.MAX_DEPTH:
            chain = ["done"]
        tid = f"t{i}"
        graph[tid] = _task(tid, deps=("done",), parent=chain[-1])
        if i % fanout == 0:
            chain.append(tid)
    return graph


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


def test_the_frontier_is_under_five_milliseconds_on_a_realistic_max_size_graph():
    """The plan's R8, on a graph shaped like a real one: `MAX_NODES` tasks, all
    ready, at full depth, with siblings sharing parents.

    Measured ~0.8 ms — a 6x margin. The bound is meaningful here rather than
    arbitrary: `ready`'s ancestor answer is cached by `parent_id`, so a branching
    graph climbs each chain once."""
    graph = _bench_graph(fanout=200)
    assert len(graph) == t_mod.MAX_NODES
    ready = orc.frontier(graph)
    assert len(ready) == t_mod.MAX_NODES - 1, (
        f"only {len(ready)} of {t_mod.MAX_NODES} are ready — the fixture is "
        f"short-circuiting and this benchmark measures nothing")
    ms = _best_ms(graph)
    assert ms < 5.0, f"frontier took {ms:.2f} ms on a realistic MAX_NODES graph"


def test_the_frontier_stays_bounded_on_the_pathological_shape():
    """Every task with its own parent, which defeats the ancestor cache entirely.

    Deliberately a LOOSER bound than the test above, and the looseness is the
    honest part: measured ~4.6 ms, so a 5 ms assertion here would be flaky on
    shared CI rather than protective. What matters is that the pathological case
    is still milliseconds — `frontier` runs once per wave, and a wave spawns
    subprocesses costing orders of magnitude more."""
    ms = _best_ms(_bench_graph(fanout=1))
    assert ms < 25.0, (
        f"frontier took {ms:.2f} ms on {t_mod.MAX_NODES} unique-parent nodes")


def test_the_frontier_does_not_scale_quadratically():
    """The assertion that actually catches a regression. A threshold test passes
    on a faster machine no matter how the complexity changed; this compares the
    curve to itself, so an accidental O(N^2) fails on any machine.

    Quadrupling the nodes must cost well under 16x — the quadratic factor —
    leaving generous room for the sort's N log N and for CI noise."""
    small = _best_ms(_bench_graph(n=500, fanout=1), rounds=5)
    large = _best_ms(_bench_graph(n=2000, fanout=1), rounds=5)
    assert large < small * 10, (
        f"500 nodes: {small:.2f} ms, 2000 nodes: {large:.2f} ms — a {large/small:.1f}x "
        f"cost for 4x the nodes looks quadratic")


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


@pytest.mark.parametrize("bad", ["fail", "timeout", "refused", "spawn-failed"])
def test_every_non_ok_result_blocks_the_task(bad):
    """Enumerated against `RUN_RESULTS` rather than treated as "not ok", so a
    result value added later fails loudly instead of being silently classified
    as success."""
    fake = FakeRunner(results={"a": bad})
    out = orc.orchestrate(_graph(_task("a")), runner=fake, execute=True)
    assert out.blocked == ["a"]
    assert out.completed == []


def test_the_result_mapping_covers_every_declared_run_result():
    """The canary for the parametrize above: if `RUN_RESULTS` grows, this fails
    and the new value gets a deliberate classification."""
    assert set(orc.RESULT_STATUS) == set(x_mod.RUN_RESULTS), (
        "orchestrator.RESULT_STATUS and executor.RUN_RESULTS disagree — one of "
        "them grew and the other did not")


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
    orc.orchestrate(_graph(_task("a")), runner=fake, execute=True)
    assert fake.kwargs["a"]["env"][optin.ENV_VAR] == "0"


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
    with pytest.raises(RuntimeError, match="a "):
        orc.orchestrate({"a": _task("a")}, runner=FakeRunner(), execute=True)


def test_one_failing_append_does_not_abandon_the_rest_of_the_wave(monkeypatch):
    """Aborting the loop mid-wave left the remaining settled tasks with no row at
    all AND skipped the rebuild, leaving the sidecar stale on top of the rows that
    did land — the exact condition _persist exists to prevent."""
    _declare("a")
    _declare("b")
    seen = []
    real = events.append_event

    def flaky(etype, payload, **kw):
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


def test_no_rows_are_written_for_a_wave_that_settled_nothing(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(sc_mod, "rebuild",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    before = len(events.read_all())
    orc.orchestrate(_graph(_task("a", status=t_mod.STATUS_BLOCKED)),
                    runner=FakeRunner(), execute=True)
    assert len(events.read_all()) == before
    assert calls["n"] == 0, "a wave that did nothing must not rewrite anything"


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
