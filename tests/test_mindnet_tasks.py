# tests/test_mindnet_tasks.py
"""MN-2 hierarchical task graph: the event contract, the deterministic fold,
defensive bounds, triple-threat cycle detection, and the readiness/completion
predicates.

Deliberately PHASE-COUNT AGNOSTIC. The 6-vs-8 phase legend is still unpinned,
so every assertion here is written against ``tasks.PHASES`` positionally
(``PHASES[0]``, ``PHASES[-1]``) rather than against a hardcoded length. Pinning
the legend must not require rewriting this file — only ``test_legend_shape``
below encodes what is agreed regardless of count.

The graph is built dynamically by an agent, so it is assumed hostile: the tests
below assert that hallucinated cycles, absurd depth, and dependency floods are
REFUSED rather than folded, and that traversal never recurses (a 5000-node
chain would blow Python's ~1000-frame stack, so a passing depth test on one is
the real proof that the implementation is iterative).
"""
import dataclasses

import pytest

from mindhive import events
from mindhive.mindnet import tasks as t_mod


# --- helpers ------------------------------------------------------------------

def _t(task_id, **kw):
    """A Task with sane defaults; `frozen=True` so overrides go through kw."""
    return t_mod.Task(task_id=task_id, title=kw.pop("title", f"task {task_id}"),
                      **kw)


def _graph(*tasks):
    """{task_id: Task} in insertion order — the shape every predicate takes."""
    return {task.task_id: task for task in tasks}


def _chain(n, prefix="c"):
    """A parent->child chain of *n* tasks: c0 (root) -> c1 -> ... -> c(n-1)."""
    out = {}
    for i in range(n):
        tid = f"{prefix}{i}"
        out[tid] = _t(tid, parent_id=(f"{prefix}{i-1}" if i else None))
    return out


def _ev(action, task_id="root-1", ts=None, session="s1", **kw):
    return events.append_event(
        "mindnet_task", {"action": action, "task_id": task_id, **kw},
        session_id=session, ts=ts, tags=["mindnet", "task"])


def _declare(task_id="root-1", title="build the thing", ts=None, **kw):
    return _ev("declare", task_id=task_id, title=title, ts=ts, **kw)


# --- the legend, and the bounds ------------------------------------------------

def test_legend_shape():
    """What holds under BOTH candidate legends: brainstorm is the entry gate,
    PR-green is terminal, names are unique, and index lookup is total."""
    assert t_mod.PHASES[0] == "brainstorm"
    assert t_mod.PHASES[-1] in ("pr-green", "pr_green")
    assert len(set(t_mod.PHASES)) == len(t_mod.PHASES), "duplicate phase name"
    assert t_mod.phase_index(t_mod.PHASES[0]) == 0
    assert t_mod.phase_index("no-such-phase") is None


def test_every_task_starts_at_the_gate():
    """A task may not be born mid-pipeline — the entry phase is not optional."""
    assert _t("x").phase == t_mod.PHASES[0]
    assert _t("x").status == t_mod.STATUS_PENDING
    assert _t("x").depth == 0
    assert _t("x").parent_id is None


def test_bounds_are_declared():
    assert t_mod.MAX_NODES == 2000
    assert t_mod.MAX_DEPTH == 12
    assert t_mod.MAX_DEPS_PER_TASK == 32


def test_task_is_immutable():
    """frozen=True: a fold must rebuild records, never mutate them in place, so
    two folds of the same events can never alias each other's state."""
    task = _t("x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.status = t_mod.STATUS_COMPLETED


# --- roots and hierarchy -------------------------------------------------------

def test_multiple_roots_coexist():
    g = _graph(_t("a"), _t("b"), _t("a1", parent_id="a"))
    assert sorted(t_mod.roots(g)) == ["a", "b"]
    assert t_mod.depth_of(g, "a1") == 1


def test_ancestors_and_descendants_are_ordered_and_total():
    g = _chain(4)
    assert t_mod.ancestors(g, "c3") == ("c2", "c1", "c0")   # nearest first
    assert t_mod.descendants(g, "c0") == {"c1", "c2", "c3"}
    assert t_mod.ancestors(g, "c0") == ()
    assert t_mod.descendants(g, "c3") == set()
    # A dangling parent reference must not raise — reads stay total.
    assert t_mod.ancestors(_graph(_t("x", parent_id="ghost")), "x") == ()
    assert t_mod.ancestors(g, "absent") == ()


def test_depth_violation_is_reported():
    g = _chain(t_mod.MAX_DEPTH + 2)
    assert t_mod.depth_of(g, f"c{t_mod.MAX_DEPTH + 1}") == t_mod.MAX_DEPTH + 1
    violations = t_mod.check_graph(g)
    assert any("depth" in v for v in violations), violations


def test_depth_at_the_cap_is_allowed():
    """Off-by-one guard: MAX_DEPTH is a permitted depth, not a rejected one."""
    g = _chain(t_mod.MAX_DEPTH + 1)                 # deepest node depth == MAX_DEPTH
    assert t_mod.depth_of(g, f"c{t_mod.MAX_DEPTH}") == t_mod.MAX_DEPTH
    assert not [v for v in t_mod.check_graph(g) if "depth" in v]


def test_traversal_is_iterative_not_recursive():
    """The mandate's real teeth: a 5000-deep chain exceeds Python's default
    ~1000-frame limit, so any recursive implementation raises RecursionError
    here instead of reporting the violation."""
    g = _chain(5000)
    assert t_mod.depth_of(g, "c4999") == 4999          # must not raise
    assert t_mod.descendants(g, "c0") == {f"c{i}" for i in range(1, 5000)}
    assert any("depth" in v for v in t_mod.check_graph(g))


def test_node_count_cap():
    g = {f"n{i}": _t(f"n{i}") for i in range(t_mod.MAX_NODES + 1)}
    assert any("nodes" in v for v in t_mod.check_graph(g))


def test_dependency_flood_cap():
    deps = tuple(f"d{i}" for i in range(t_mod.MAX_DEPS_PER_TASK + 1))
    g = _graph(_t("hub", dependencies=deps), *[_t(d) for d in deps])
    assert any("dependencies" in v for v in t_mod.check_graph(g))


# --- triple-threat cycle detection --------------------------------------------
# Three INDEPENDENT checks: a deadlock can exist without forming a simple cycle
# in either the parent graph or the dependency graph alone.

def test_threat_1_dependency_cycle():
    """A waits on B, B waits on C, C waits on A."""
    g = _graph(_t("a", dependencies=("b",)),
               _t("b", dependencies=("c",)),
               _t("c", dependencies=("a",)))
    cycle = t_mod.dependency_cycle(g)
    assert cycle is not None
    assert set(cycle) == {"a", "b", "c"}
    assert any("dependency cycle" in v for v in t_mod.check_graph(g))


def test_threat_1_self_dependency():
    g = _graph(_t("a", dependencies=("a",)))
    assert t_mod.dependency_cycle(g) == ("a",)


def test_threat_1_diamond_is_not_a_cycle():
    """Shared ancestry is legal: a diamond must NOT be reported, or every
    realistic fan-in graph would be refused."""
    g = _graph(_t("top"),
               _t("left", dependencies=("top",)),
               _t("right", dependencies=("top",)),
               _t("join", dependencies=("left", "right")))
    assert t_mod.dependency_cycle(g) is None
    assert t_mod.check_graph(g) == []


def test_threat_2_hierarchy_cycle_reparent_under_descendant():
    g = _chain(3)                                   # c0 -> c1 -> c2
    assert t_mod.hierarchy_cycle(g, "c0", "c2") is not None
    assert t_mod.hierarchy_cycle(g, "c0", "c0") is not None      # self-parent
    # Legal moves stay legal.
    g2 = _graph(_t("a"), _t("b"), _t("a1", parent_id="a"))
    assert t_mod.hierarchy_cycle(g2, "a1", "b") is None
    assert t_mod.hierarchy_cycle(g2, "a1", None) is None         # promote to root


def test_threat_2_existing_parent_loop_is_reported():
    """A loop already present in folded data (two tasks parenting each other)
    must be reported, not spun on."""
    g = _graph(_t("a", parent_id="b"), _t("b", parent_id="a"))
    assert t_mod.depth_of(g, "a") == -1              # unresolvable chain sentinel
    assert any("hierarchy cycle" in v for v in t_mod.check_graph(g))


def test_threat_3_parent_depending_on_own_child_is_a_deadlock():
    """P cannot start until C completes, but C's work is part of P."""
    g = _graph(_t("p", dependencies=("c",)), _t("c", parent_id="p"))
    assert t_mod.cross_graph_conflict(g) == ("p", "c")
    assert any("deadlock" in v for v in t_mod.check_graph(g))


def test_threat_3_child_depending_on_own_ancestor_is_a_deadlock():
    """C cannot start until P completes, but P cannot complete while C is open
    — a hard deadlock that neither single-graph check would catch."""
    g = _graph(_t("p"), _t("c", parent_id="p", dependencies=("p",)))
    assert t_mod.cross_graph_conflict(g) is not None
    assert any("deadlock" in v for v in t_mod.check_graph(g))


def test_threat_3_deep_ancestor_dependency():
    g = _chain(4)
    g["c3"] = dataclasses.replace(g["c3"], dependencies=("c0",))
    assert t_mod.cross_graph_conflict(g) is not None


def test_threat_3_sibling_dependency_is_legal():
    """The common, correct case: siblings ordered under one parent."""
    g = _graph(_t("p"), _t("a", parent_id="p"),
               _t("b", parent_id="p", dependencies=("a",)))
    assert t_mod.cross_graph_conflict(g) is None
    assert t_mod.check_graph(g) == []


def test_dangling_dependency_is_reported_not_crashed():
    g = _graph(_t("a", dependencies=("ghost",)))
    assert any("unknown" in v for v in t_mod.check_graph(g))
    assert t_mod.dependency_cycle(g) is None


# --- readiness ----------------------------------------------------------------

def test_ready_requires_pending_and_completed_dependencies():
    g = _graph(_t("done", status=t_mod.STATUS_COMPLETED),
               _t("open", status=t_mod.STATUS_IN_PROGRESS),
               _t("a", dependencies=("done",)),
               _t("b", dependencies=("done", "open")))
    ready = t_mod.ready(g)
    assert "a" in ready
    assert "b" not in ready                 # 'open' is not COMPLETED
    assert "done" not in ready              # already completed
    assert "open" not in ready              # only PENDING becomes READY


def test_blocked_parent_blocks_children():
    g = _graph(_t("p", status=t_mod.STATUS_BLOCKED), _t("c", parent_id="p"))
    assert t_mod.ready(g) == []


def test_blocked_ancestor_blocks_transitively():
    """Not just the immediate parent — a blocked grandparent must block down
    the whole subtree, or blocking is trivially escaped by nesting deeper."""
    g = _chain(3)
    g["c0"] = dataclasses.replace(g["c0"], status=t_mod.STATUS_BLOCKED)
    assert "c2" not in t_mod.ready(g)
    assert "c1" not in t_mod.ready(g)


def test_abandoned_dependency_never_satisfies():
    g = _graph(_t("x", status=t_mod.STATUS_ABANDONED),
               _t("a", dependencies=("x",)))
    assert t_mod.ready(g) == []


def test_ready_is_deterministic_and_sorted():
    g = _graph(_t("z"), _t("m"), _t("a"))
    assert t_mod.ready(g) == ["a", "m", "z"] == t_mod.ready(g)


# --- completion ---------------------------------------------------------------

def test_parent_cannot_complete_while_a_descendant_is_open():
    g = _graph(_t("p", evidence=("runs/p.txt",)),
               _t("c", parent_id="p", status=t_mod.STATUS_PENDING))
    reason = t_mod.can_complete(g, "p")
    assert reason is not None and "descendant" in reason


def test_parent_completes_once_subtree_is_closed():
    g = _graph(_t("p", evidence=("runs/p.txt",)),
               _t("c", parent_id="p", status=t_mod.STATUS_COMPLETED),
               _t("d", parent_id="p", status=t_mod.STATUS_ABANDONED))
    assert t_mod.can_complete(g, "p") is None


def test_completion_requires_evidence():
    g = _graph(_t("leaf"))
    reason = t_mod.can_complete(g, "leaf")
    assert reason is not None and "evidence" in reason


def test_triviality_bypass_waives_evidence_only():
    """ADR-0005's bypass covers the METHOD, not the graph: a trivial task still
    cannot complete over an open descendant."""
    g = _graph(_t("leaf", triviality_bypass=True))
    assert t_mod.can_complete(g, "leaf") is None
    g2 = _graph(_t("p", triviality_bypass=True),
                _t("c", parent_id="p", status=t_mod.STATUS_PENDING))
    assert t_mod.can_complete(g2, "p") is not None


def test_can_complete_on_unknown_task_is_total():
    assert t_mod.can_complete(_graph(), "nope") is not None


# --- the event contract -------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "free text",                                             # not a dict
    {"action": "declare"},                                   # no task_id
    {"action": "teleport", "task_id": "a-1"},                # unknown action
    {"action": "declare", "task_id": "Not Kebab"},           # bad id
    {"action": "declare", "task_id": "a-1"},                 # declare needs title
    {"action": "phase", "task_id": "a-1", "phase": "nope"},  # unknown phase
    {"action": "status", "task_id": "a-1", "status": "nope"},  # unknown status
    {"action": "depend", "task_id": "a-1"},                  # depend needs on
    {"action": "declare", "task_id": "a-1", "title": "x",
     "dependencies": [f"d{i}" for i in range(t_mod.MAX_DEPS_PER_TASK + 1)]},
    {"action": "declare", "task_id": "a-1", "title": "t" * 500},   # title cap
])
def test_validate_payload_rejects(payload):
    with pytest.raises(ValueError):
        t_mod.validate_payload(payload)


@pytest.mark.parametrize("payload", [
    {"action": "declare", "task_id": "a-1", "title": "build it"},
    {"action": "declare", "task_id": "a-1", "title": "t", "parent_id": "root-1"},
    {"action": "status", "task_id": "a-1", "status": t_mod.STATUS_BLOCKED},
    {"action": "phase", "task_id": "a-1", "phase": t_mod.PHASES[-1]},
    {"action": "depend", "task_id": "a-1", "on": "b-1"},
    {"action": "reparent", "task_id": "a-1", "parent_id": None},
    {"action": "evidence", "task_id": "a-1", "ref": "runs/a-1.txt"},
])
def test_validate_payload_accepts(payload):
    t_mod.validate_payload(payload)


# --- the fold -----------------------------------------------------------------

def test_fold_builds_the_graph():
    _declare("root-1", title="ship MN-2")
    _declare("sub-1", title="write the fold", parent_id="root-1")
    _ev("status", task_id="sub-1", status=t_mod.STATUS_IN_PROGRESS)
    _ev("phase", task_id="sub-1", phase=t_mod.PHASES[1])
    g = t_mod.fold(events.read_all())
    assert set(g) == {"root-1", "sub-1"}
    assert g["sub-1"].parent_id == "root-1"
    assert g["sub-1"].status == t_mod.STATUS_IN_PROGRESS
    assert g["sub-1"].phase == t_mod.PHASES[1]
    assert g["sub-1"].depth == 1


def test_fold_accumulates_dependencies_and_evidence():
    _declare("a-1")
    _declare("b-1")
    _ev("depend", task_id="b-1", on="a-1")
    _ev("evidence", task_id="b-1", ref="runs/b-1.txt")
    _ev("evidence", task_id="b-1", ref="runs/b-1.txt")     # idempotent
    g = t_mod.fold(events.read_all())
    assert g["b-1"].dependencies == ("a-1",)
    assert g["b-1"].evidence == ("runs/b-1.txt",)


def test_fold_refuses_a_dependency_that_would_cycle():
    """The fold is the last line of defence: an agent that appends a cycling
    `depend` must not corrupt the graph. The event stays on the ledger (append
    only), but the fold declines to apply it."""
    _declare("a-1")
    _declare("b-1")
    _ev("depend", task_id="b-1", on="a-1")
    _ev("depend", task_id="a-1", on="b-1")                  # would close a cycle
    g = t_mod.fold(events.read_all())
    assert g["a-1"].dependencies == ()
    assert t_mod.dependency_cycle(g) is None


def test_fold_refuses_a_reparent_that_would_cycle():
    _declare("c0")
    _declare("c1", parent_id="c0")
    _ev("reparent", task_id="c0", parent_id="c1")           # under own child
    g = t_mod.fold(events.read_all())
    assert g["c0"].parent_id is None


@pytest.mark.parametrize("payload", [
    "not a dict",
    {"action": "declare"},                                   # no task_id
    {"action": "teleport", "task_id": "a-1"},                # unknown action
    {"action": "status", "task_id": "ghost", "status": t_mod.STATUS_BLOCKED},
    {"action": "status", "task_id": "a-1", "status": "invented"},
    {"action": "phase", "task_id": "a-1", "phase": "invented"},
])
def test_fold_is_total(payload):
    """Invariant 3: the ledger holds history written by every past version, so
    the fold skips junk and never raises. Nothing here may throw."""
    _declare("a-1")
    events.append_event("mindnet_task", payload, session_id="s1",
                        tags=["mindnet"])
    g = t_mod.fold(events.read_all())
    assert set(g) == {"a-1"}
    assert g["a-1"].status == t_mod.STATUS_PENDING


def test_fold_ignores_foreign_event_types():
    _declare("a-1")
    events.append_event("decision", {"text": "unrelated"}, session_id="s1")
    assert set(t_mod.fold(events.read_all())) == {"a-1"}


def test_fold_is_deterministic():
    """Invariant 2: same events in, same records out. No clock, no RNG — the
    task_id is caller-supplied precisely so the fold stays pure."""
    _declare("a-1", ts="2026-07-25T00:00:01Z")
    _declare("b-1", ts="2026-07-25T00:00:02Z", parent_id="a-1")
    _ev("status", task_id="b-1", status=t_mod.STATUS_BLOCKED,
        ts="2026-07-25T00:00:03Z")
    evs = events.read_all()
    assert t_mod.fold(evs) == t_mod.fold(evs)


def test_fold_rejects_a_declare_past_the_node_cap():
    """A runaway agent loop must hit a wall in the fold, not exhaust memory."""
    for i in range(t_mod.MAX_NODES + 5):
        _declare(f"n-{i}", ts="2026-07-25T00:00:01Z")
    g = t_mod.fold(events.read_all())
    assert len(g) == t_mod.MAX_NODES


def test_redeclare_updates_without_losing_progress():
    """`declare` upserts (roadmap's `set` precedent) but must not silently
    reset a task's accumulated status/evidence."""
    _declare("a-1", title="first")
    _ev("status", task_id="a-1", status=t_mod.STATUS_IN_PROGRESS)
    _ev("evidence", task_id="a-1", ref="runs/a-1.txt")
    _declare("a-1", title="sharpened title")
    g = t_mod.fold(events.read_all())
    assert g["a-1"].title == "sharpened title"
    assert g["a-1"].status == t_mod.STATUS_IN_PROGRESS
    assert g["a-1"].evidence == ("runs/a-1.txt",)
