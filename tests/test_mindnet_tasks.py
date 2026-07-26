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


def _sat(*pairs):
    """A stub satisfaction predicate: ``(task_id, phase) -> bool``.

    `can_complete` takes this INJECTED rather than importing the controller —
    the whole point of the injection is that `tasks.py` never depends on
    `controller.py`, so these tests must not import it either. ``_sat()`` with
    no arguments is the honest "nothing has been satisfied" world."""
    allowed = set(pairs)
    return lambda task_id, phase: (task_id, phase) in allowed


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
    """NEW RULE: completion now also needs the terminal phase satisfied, so the
    stub predicate is no longer optional — a closed subtree alone is not a
    finished task, it is a task with nothing left blocking it."""
    g = _graph(_t("p", evidence=("runs/p.txt",)),
               _t("c", parent_id="p", status=t_mod.STATUS_COMPLETED),
               _t("d", parent_id="p", status=t_mod.STATUS_ABANDONED))
    assert t_mod.can_complete(g, "p", satisfied=_sat(("p", t_mod.PHASES[-1]))) \
        is None


def test_completion_requires_evidence():
    """NEW RULE: the artifact is still required, and it is now an INDEPENDENT
    gate — the predicate says pr-green is cleared and completion is still
    refused, so a satisfied phase cannot stand in for a missing artifact."""
    g = _graph(_t("leaf"))
    reason = t_mod.can_complete(g, "leaf",
                                satisfied=_sat(("leaf", t_mod.PHASES[-1])))
    assert reason is not None and "evidence" in reason


def test_triviality_bypass_waives_the_method_not_the_graph():
    """REPLACES test_triviality_bypass_waives_evidence_only. NEW RULE: the
    bypass no longer means "completes on nothing" — it swaps the `pr-green`
    gate and the artifact for the bug audit (TRIVIAL_EXIT_PHASE). What is
    unchanged is the part that mattered: the bypass covers the METHOD, not the
    graph, so a trivial task still cannot complete over an open descendant."""
    ok = _sat(("leaf", t_mod.TRIVIAL_EXIT_PHASE), ("p", t_mod.TRIVIAL_EXIT_PHASE))
    g = _graph(_t("leaf", triviality_bypass=True))
    assert t_mod.can_complete(g, "leaf", satisfied=ok) is None
    g2 = _graph(_t("p", triviality_bypass=True),
                _t("c", parent_id="p", status=t_mod.STATUS_PENDING))
    assert t_mod.can_complete(g2, "p", satisfied=ok) is not None


def test_can_complete_on_unknown_task_is_total():
    assert t_mod.can_complete(_graph(), "nope") is not None
    assert t_mod.can_complete(_graph(), "nope", satisfied=_sat()) is not None


# --- the event contract -------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "free text",                                             # not a dict
    {"action": "declare"},                                   # no task_id
    {"action": "teleport", "task_id": "a-1"},                # unknown action
    {"action": "declare", "task_id": "Not Kebab"},           # bad id
    {"action": "declare", "task_id": "a-1"},                 # declare needs title
    # NEW RULE (KTD1): rejected for the ACTION, not the phase value — `phase`
    # left ACTIONS entirely, so even a perfectly well-formed one is refused.
    {"action": "phase", "task_id": "a-1", "phase": "nope"},
    {"action": "phase", "task_id": "a-1", "phase": t_mod.PHASES[-1]},
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
    # NEW RULE (KTD1): the `phase` row moved to test_validate_payload_rejects —
    # this module no longer writes the phase axis at all.
    {"action": "depend", "task_id": "a-1", "on": "b-1"},
    {"action": "reparent", "task_id": "a-1", "parent_id": None},
    {"action": "evidence", "task_id": "a-1", "ref": "runs/a-1.txt"},
])
def test_validate_payload_accepts(payload):
    t_mod.validate_payload(payload)


# --- the fold -----------------------------------------------------------------

def test_fold_builds_the_graph():
    """NEW RULE (KTD1): the `phase` event is gone from this test — the fold no
    longer writes that axis, so `phase` here is the declared entry gate and
    stays there for the task's whole life in this projection. The refusal
    itself is proved by test_raw_phase_append_cannot_reach_the_terminal_phase."""
    _declare("root-1", title="ship MN-2")
    _declare("sub-1", title="write the fold", parent_id="root-1")
    _ev("status", task_id="sub-1", status=t_mod.STATUS_IN_PROGRESS)
    g = t_mod.fold(events.read_all())
    assert set(g) == {"root-1", "sub-1"}
    assert g["sub-1"].parent_id == "root-1"
    assert g["sub-1"].status == t_mod.STATUS_IN_PROGRESS
    assert g["sub-1"].phase == t_mod.PHASES[0]
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
    # NEW RULE (KTD1): a WELL-FORMED phase row, not an invented one. The old
    # row was skipped for its bad value; this one is skipped because the action
    # itself no longer exists — the stronger property, and the shape every
    # historical mindnet_task event written this morning actually has.
    {"action": "phase", "task_id": "a-1", "phase": t_mod.PHASES[-1]},
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
    assert g["a-1"].phase == t_mod.PHASES[0]


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


# --- KTD1: one writer on the phase axis ---------------------------------------
# `mindnet_phase`, gated by the controller, is the ONLY phase writer. Every test
# below was written against the SHIPPED code first and observed to fail there —
# each one is a live exploit, not a hypothetical.

def test_phase_is_not_a_task_action():
    """The old contract accepted `action="phase"` and the old fold applied it
    whenever the value was a member of PHASES — no transition check at all
    (`phase_index` was defined and never called). It is now an unknown action,
    rejected even when perfectly well-formed."""
    assert "phase" not in t_mod.ACTIONS
    with pytest.raises(ValueError):
        t_mod.validate_payload({"action": "phase", "task_id": "a-1",
                                "phase": t_mod.PHASES[-1]})


def test_raw_phase_append_cannot_reach_the_terminal_phase():
    """The exploit, end to end: two raw `hive append mindnet_task` calls —
    declare, then phase="pr-green" — parked a task at the terminal gate with
    zero work done. The fold now ignores the action entirely."""
    _declare("a-1")
    _ev("phase", task_id="a-1", phase=t_mod.PHASES[-1])
    g = t_mod.fold(events.read_all())
    assert g["a-1"].phase == t_mod.PHASES[0]


def test_declared_entry_phase_survives_as_a_field():
    """`Task.phase` is KEPT — U4 and the CLI read it for the entry point — but
    its meaning narrows to the DECLARED ENTRY phase. Nothing in this module
    writes it after the declare."""
    assert "phase" in {f.name for f in dataclasses.fields(t_mod.Task)}
    _declare("a-1")
    _ev("status", task_id="a-1", status=t_mod.STATUS_IN_PROGRESS)
    _ev("evidence", task_id="a-1", ref="runs/a-1.txt")
    g = t_mod.fold(events.read_all())
    assert g["a-1"].phase == t_mod.PHASES[0]


# --- KTD10: triviality_bypass is first-declare-only ---------------------------

def test_the_trivial_phases_are_real_phases_in_order():
    """Invariant 10 canary: both named constants must be members of PHASES, and
    a trivial task's exit gate must sit after its entry gate."""
    assert t_mod.TRIVIAL_ENTRY_PHASE in t_mod.PHASES
    assert t_mod.TRIVIAL_EXIT_PHASE in t_mod.PHASES
    assert (t_mod.phase_index(t_mod.TRIVIAL_ENTRY_PHASE)
            < t_mod.phase_index(t_mod.TRIVIAL_EXIT_PHASE)
            < t_mod.phase_index(t_mod.PHASES[-1]))


def test_late_triviality_bypass_cannot_unlock_completion():
    """The completion skeleton key. The old declare-upsert applied the flag on
    EVERY re-declare with no progress check, and `can_complete` short-circuited
    on it BEFORE the evidence test — so declaring a task non-trivial, doing
    work, then re-declaring the same task_id with triviality_bypass=true closed
    it with zero evidence. The flag is now first-declare-only."""
    _declare("a-1", title="real work")
    _ev("status", task_id="a-1", status=t_mod.STATUS_IN_PROGRESS)
    _declare("a-1", title="real work", triviality_bypass=True)
    g = t_mod.fold(events.read_all())
    assert g["a-1"].triviality_bypass is False
    assert any("triviality_bypass" in v for v in g["a-1"].violations), \
        g["a-1"].violations
    assert t_mod.can_complete(g, "a-1", satisfied=_sat()) is not None


def test_first_declare_may_set_the_bypass_and_a_repeat_is_silent():
    """First-declare-only is not first-declare-never: the flag still lands, and
    re-declaring WITHOUT the key (the common sharpen-the-title case) keeps it
    without manufacturing a violation."""
    _declare("a-1", triviality_bypass=True)
    _declare("a-1", title="sharpened")
    g = t_mod.fold(events.read_all())
    assert g["a-1"].triviality_bypass is True
    assert g["a-1"].violations == ()


def test_clearing_the_bypass_at_or_past_execution_is_refused():
    """A trivial task enters at `execution`, so clearing its bypass is a
    refusal with its own reason — the task is already past the phases the
    bypass waived, and un-waiving them retroactively has no sound meaning."""
    _declare("a-1", triviality_bypass=True)
    _declare("a-1", title="same", triviality_bypass=False)
    g = t_mod.fold(events.read_all())
    assert g["a-1"].triviality_bypass is True
    assert any("refused" in v for v in g["a-1"].violations), g["a-1"].violations


def test_trivial_task_enters_at_execution():
    """`Task.phase` defaulted to PHASES[0] for EVERY declare, trivial ones
    included, contradicting the spec's own written exception to the
    forward-skip ban. The fold now derives the trivial entry point."""
    _declare("a-1", triviality_bypass=True)
    _declare("b-1")
    g = t_mod.fold(events.read_all())
    assert g["a-1"].phase == t_mod.TRIVIAL_ENTRY_PHASE
    assert g["b-1"].phase == t_mod.PHASES[0]


def test_violations_are_bounded():
    """A runaway agent loop must not grow one record without limit — the same
    rationale as MAX_NODES, applied to the new field."""
    _declare("a-1", triviality_bypass=True)
    for _ in range(t_mod.MAX_VIOLATIONS + 5):
        _declare("a-1", title="same", triviality_bypass=False)
    g = t_mod.fold(events.read_all())
    assert len(g["a-1"].violations) == t_mod.MAX_VIOLATIONS


# --- one placement gate for all THREE writers of parent_id --------------------
# `reparent`, the re-declare upsert, and the FIRST declare all write parent_id.
# They must refuse the same placements for the same reason with the same
# message, or the strictest path is bypassed simply by spelling the move
# differently. Deliberately NO phase-coupling check in any of them: KTD1 leaves
# `Task.phase` with no writer in this module, so a phase comparison would
# compare declared-entry defaults and pass vacuously — that belongs to U4.

def _deep_chain_events(n, prefix="c"):
    for i in range(n):
        _declare(f"{prefix}{i}", parent_id=(f"{prefix}{i-1}" if i else None))


def test_reparent_past_max_depth_is_refused():
    """`reparent` shipped with exactly two gates — the task exists, and no
    hierarchy cycle — so hanging a loose node off a MAX_DEPTH chain produced
    depth MAX_DEPTH+1 with no refusal, bypassing the bound entirely."""
    _deep_chain_events(t_mod.MAX_DEPTH + 1)         # deepest node at MAX_DEPTH
    _declare("loose")
    _ev("reparent", task_id="loose", parent_id=f"c{t_mod.MAX_DEPTH}")
    g = t_mod.fold(events.read_all())
    assert g["loose"].parent_id is None
    assert g["loose"].depth == 0
    assert not [v for v in t_mod.check_graph(g) if "depth" in v]
    assert any("depth" in v for v in g["loose"].violations), g["loose"].violations


def test_reparent_refuses_when_a_DESCENDANT_would_overflow():
    """The bound applies to the whole moved subtree, not just the moved node —
    checking only the node itself is how a two-deep subtree slips one past."""
    _deep_chain_events(t_mod.MAX_DEPTH)             # deepest node at MAX_DEPTH-1
    _declare("m0")
    _declare("m1", parent_id="m0")                  # m1 would land at MAX_DEPTH+1
    _ev("reparent", task_id="m0", parent_id=f"c{t_mod.MAX_DEPTH - 1}")
    g = t_mod.fold(events.read_all())
    assert g["m0"].parent_id is None


def test_reparent_within_the_depth_bound_still_works():
    """Canary against a vacuous refusal: the legal move must stay legal, or the
    check above would pass for the wrong reason."""
    _declare("a")
    _declare("b")
    _declare("a1", parent_id="a")
    _ev("reparent", task_id="a1", parent_id="b")
    g = t_mod.fold(events.read_all())
    assert g["a1"].parent_id == "b"
    assert g["a1"].violations == ()


def test_reparent_under_a_closed_parent_is_refused():
    """`can_complete` already certified that parent's subtree was closed;
    hanging work under it afterwards makes that certificate retroactively
    false without a single event recording the change of mind."""
    _declare("p")
    _declare("loose")
    _ev("status", task_id="p", status=t_mod.STATUS_COMPLETED)
    _ev("reparent", task_id="loose", parent_id="p")
    g = t_mod.fold(events.read_all())
    assert g["loose"].parent_id is None
    assert any("completed" in v for v in g["loose"].violations), \
        g["loose"].violations


def test_first_declare_past_max_depth_is_refused():
    """The THIRD writer of parent_id, and the last one to be gated. `reparent`
    and the re-declare upsert both refuse an over-deep placement; the FIRST
    declare accepted it outright, so the identical bad edge landed simply by
    being declared in the right order — and this module's stated contract is
    that the fold REFUSES the edge, not that `check_graph` reports it later."""
    _deep_chain_events(t_mod.MAX_DEPTH + 1)         # deepest node at MAX_DEPTH
    _declare("loose", parent_id=f"c{t_mod.MAX_DEPTH}")
    g = t_mod.fold(events.read_all())
    assert "loose" in g                     # the NODE still lands...
    assert g["loose"].parent_id is None     # ...only its placement is refused
    assert g["loose"].depth == 0
    assert not [v for v in t_mod.check_graph(g) if "depth" in v]
    assert any("depth" in v for v in g["loose"].violations), g["loose"].violations


def test_a_forward_reference_parent_still_declares():
    """Ledger order is not declaration order: a child may legally be declared
    before its parent exists. Depth is unresolvable at that moment, so there is
    nothing to refuse — the reference is KEPT and resolves itself when the
    parent lands. Refusing here would break out-of-order decomposition, which
    is the normal way an agent builds this graph."""
    _declare("kid", parent_id="p")                  # 'p' does not exist yet
    g1 = t_mod.fold(events.read_all())
    assert g1["kid"].parent_id == "p"
    assert g1["kid"].violations == ()
    assert g1["kid"].depth == 0                     # dangling parent => a root
    _declare("p")
    g2 = t_mod.fold(events.read_all())
    assert g2["kid"].parent_id == "p" and g2["kid"].depth == 1
    assert t_mod.check_graph(g2) == []


def test_a_forward_reference_cannot_smuggle_a_subtree_past_the_bound():
    """The boundary the forward-reference allowance creates, pinned so nobody
    'simplifies' the moved-subtree check back to the moved node alone. A child
    declared early against a not-yet-existing parent becomes part of that
    parent's subtree the moment it lands — so the parent's own placement is
    what gets refused, and the chain splits rather than overflowing."""
    _declare("kid", parent_id=f"c{t_mod.MAX_DEPTH}")    # forward reference
    _deep_chain_events(t_mod.MAX_DEPTH + 1)             # ...c12 lands at depth 12
    g = t_mod.fold(events.read_all())
    assert g["kid"].parent_id == f"c{t_mod.MAX_DEPTH}"
    assert not [v for v in t_mod.check_graph(g) if "depth" in v]
    assert g[f"c{t_mod.MAX_DEPTH}"].parent_id is None   # refused, recorded below
    assert any("depth" in v
               for v in g[f"c{t_mod.MAX_DEPTH}"].violations)


def test_a_redeclare_is_held_to_the_same_reparent_gates():
    """A re-declare carrying `parent_id` IS a reparent — otherwise the hardened
    action is routed around by spelling the move as a declare. The unchanged
    fields still land: the move is refused, the title is not."""
    _deep_chain_events(t_mod.MAX_DEPTH + 1)
    _declare("loose", title="first")
    _declare("loose", title="sharpened", parent_id=f"c{t_mod.MAX_DEPTH}")
    g = t_mod.fold(events.read_all())
    assert g["loose"].parent_id is None
    assert g["loose"].title == "sharpened"
    assert any("depth" in v for v in g["loose"].violations), g["loose"].violations


def test_a_noop_reparent_manufactures_no_violation():
    """Echoing the parent a task already has is not a move. Without this guard
    every re-declare of a closed task would accrue a spurious refusal."""
    _declare("p")
    _declare("kid", parent_id="p")
    _ev("status", task_id="kid", status=t_mod.STATUS_COMPLETED)
    _declare("kid", title="sharpened", parent_id="p")
    _ev("reparent", task_id="kid", parent_id="p")
    g = t_mod.fold(events.read_all())
    assert g["kid"].parent_id == "p"
    assert g["kid"].title == "sharpened"
    assert g["kid"].violations == ()


def test_reparent_of_a_closed_task_is_refused():
    """Settled work is not restructured: its subtree membership is part of the
    completion record `can_complete` already accepted."""
    _declare("p")
    _declare("done")
    _ev("status", task_id="done", status=t_mod.STATUS_ABANDONED)
    _ev("reparent", task_id="done", parent_id="p")
    g = t_mod.fold(events.read_all())
    assert g["done"].parent_id is None
    assert g["done"].violations != ()


# --- KTD11: can_complete requires the terminal phase --------------------------

def test_can_complete_refuses_without_a_satisfaction_predicate():
    """Fail-safe. U2 ships before the controller exists, and a missing
    predicate must never LOOSEN the gate it was added to tighten."""
    g = _graph(_t("leaf", evidence=("runs/leaf.txt",)))
    reason = t_mod.can_complete(g, "leaf")
    assert reason is not None and "predicate" in reason


def test_can_complete_requires_the_terminal_phase_satisfied():
    """The exploit: declare -> evidence -> status(completed) closed a task that
    never left the entry phase, because `can_complete` read four things and
    `phase` was not one of them. `pr-green` gated nothing."""
    g = _graph(_t("leaf", evidence=("runs/leaf.txt",)))
    reason = t_mod.can_complete(g, "leaf", satisfied=_sat())
    assert reason is not None and t_mod.PHASES[-1] in reason


def test_can_complete_accepts_a_satisfied_terminal_phase():
    g = _graph(_t("leaf", evidence=("runs/leaf.txt",)))
    ok = _sat(("leaf", t_mod.PHASES[-1]))
    assert t_mod.can_complete(g, "leaf", satisfied=ok) is None


def test_trivial_completion_gates_on_the_bug_audit():
    """ADR-0005's bypass waives brainstorm/plan/PRD. It does not waive the bug
    audit, and it never meant "completes on nothing"."""
    g = _graph(_t("leaf", triviality_bypass=True))
    assert t_mod.can_complete(g, "leaf", satisfied=_sat()) is not None
    ok = _sat(("leaf", t_mod.TRIVIAL_EXIT_PHASE))
    assert t_mod.can_complete(g, "leaf", satisfied=ok) is None
    # A trivial task is NOT held to `pr-green` — that is the whole bypass.
    only_prgreen = _sat(("leaf", t_mod.PHASES[-1]))
    assert t_mod.can_complete(g, "leaf", satisfied=only_prgreen) is not None


def test_can_complete_refuses_when_the_predicate_raises():
    """The predicate comes from another module; an exception inside it is a
    fault, and a completion gate must fail CLOSED on a fault."""
    def boom(task_id, phase):
        raise RuntimeError("controller exploded")
    g = _graph(_t("leaf", evidence=("runs/leaf.txt",)))
    assert t_mod.can_complete(g, "leaf", satisfied=boom) is not None


def test_open_descendants_outrank_every_other_reason():
    """Order is load-bearing: the subtree check runs FIRST and applies to
    trivial tasks too, so a fully satisfied parent still cannot close over open
    work."""
    g = _graph(_t("p", triviality_bypass=True),
               _t("c", parent_id="p", status=t_mod.STATUS_PENDING))
    ok = _sat(("p", t_mod.TRIVIAL_EXIT_PHASE), ("p", t_mod.PHASES[-1]))
    reason = t_mod.can_complete(g, "p", satisfied=ok)
    assert reason is not None and "descendant" in reason
