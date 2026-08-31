# tests/test_mindnet_ab.py
"""TODOS 23 — the harness that runs both arms, and the decision it encodes.

The composer ships default ON and nobody has shown it helps; the owner made that
default conditional on the gate being written down, and the gate says Branch 2
does not open until this measurement runs. So the thing under test here is not
really a feature, it is an instrument — and the failure mode of an instrument is
reporting a confident number it could not have measured.

Two of these tests exist for that specifically: the dead-metric guard, and the
independent-graph property. Both describe ways this module could have printed
`neutral` from data that carried no signal.
"""
import os

import pytest

from mindhive.mindnet import ab
from mindhive.mindnet import brief as br_mod
from mindhive.mindnet import tasks as t_mod


def _task(tid, **kw):
    return t_mod.Task(task_id=tid, title=f"task {tid}", **kw)


def _graph(*tids):
    return {t: _task(t) for t in tids}


def _arm(name, score=0, outcome="settled"):
    return ab.Arm(name=name, brief_on=name == "on", outcome=outcome,
                  completed=tuple(f"{name}{i}" for i in range(score)))


# --- the switch ---------------------------------------------------------------

def test_the_switch_restores_the_variables_absence_not_its_default(monkeypatch):
    """Restoring "unset" is not the same as setting it to the default value.

    `brief.enabled()` defaults ON when the variable is ABSENT, so a harness that
    tidied up by writing "1" would leave the process in a state that looks
    identical and is not: the next reader cannot tell a deliberate ON from a
    leftover. The ON arm runs second, so the leak would always be the ON one."""
    # Through monkeypatch, not `os.environ.pop`: this test asserts on process
    # state, and a raw pop leaks the deletion into every test that runs after it.
    monkeypatch.delenv(br_mod.ENV_SWITCH, raising=False)
    with ab.switched(False):
        assert os.environ[br_mod.ENV_SWITCH] == "0"
    assert br_mod.ENV_SWITCH not in os.environ


def test_the_switch_restores_a_preexisting_value(monkeypatch):
    """An operator who had already set it gets their setting back, not the
    default. Otherwise running the harness silently reconfigures the shell it
    was run from."""
    monkeypatch.setenv(br_mod.ENV_SWITCH, "0")
    with ab.switched(True):
        assert os.environ[br_mod.ENV_SWITCH] == "1"
    assert os.environ[br_mod.ENV_SWITCH] == "0"


def test_each_arm_actually_runs_under_its_own_switch():
    """The whole experiment is the switch being different between the arms.

    Observed at the seam a real agent would observe it from — `brief.enabled()`
    read during the run — rather than by trusting that the context manager was
    entered."""
    seen = []

    def runner(run_key, script, **kw):
        seen.append(br_mod.enabled())
        from mindhive.mindnet import executor as x_mod
        return x_mod.RunResult(result="ok", executed=True, run_key=run_key)

    ab.run(_graph("a"), runner=runner, execute=True)
    assert seen == [False, True], (
        f"the arms did not run under different switches: {seen}")


# --- the property that would produce a confident wrong number -----------------

def test_each_arm_starts_from_the_same_graph():
    """Statuses are applied to the graph IN PLACE.

    Without a per-arm copy the second arm inherits a board the first arm already
    advanced, so it is graded on work it did not do — and the result looks
    entirely plausible, which is what makes it the dangerous one. Asserted on
    what each arm was HANDED, so it fails for the right reason."""
    handed = []

    def runner(run_key, script, **kw):
        from mindhive.mindnet import executor as x_mod
        return x_mod.RunResult(result="ok", executed=True, run_key=run_key)

    graph = _graph("a", "b")
    original = {tid: t.status for tid, t in graph.items()}

    real_orchestrate = ab.orc_mod.orchestrate

    def spy(g, **kw):
        handed.append({tid: t.status for tid, t in g.items()})
        return real_orchestrate(g, **kw)

    ab.orc_mod.orchestrate = spy
    try:
        ab.run(graph, runner=runner, execute=True)
    finally:
        ab.orc_mod.orchestrate = real_orchestrate

    assert len(handed) == 2
    assert handed[0] == handed[1] == original, (
        f"the arms started from different boards: {handed}")
    assert {tid: t.status for tid, t in graph.items()} == original, (
        "the harness mutated the caller's graph")


# --- the decision ------------------------------------------------------------

def test_zero_against_zero_is_inconclusive_and_never_neutral():
    """The dead-metric guard, and the reason this module has a fourth verdict.

    A fully successful agent wave scores ZERO validated completions in both arms:
    `agent.run` returns `ran-unverified`, `_run_wave` routes it to `skipped` with
    no status row. Grading that pair `neutral` would close the Branch 2 gate on
    data that could not have moved — the same shape as this repo's recorded
    dead-lens defect, where a threshold above the base score made three
    conditions unreachable, silently."""
    verdict, reason, delta = ab.verdict_for(_arm("off", 0), _arm("on", 0))
    assert verdict == ab.INCONCLUSIVE, (
        f"a metric that could not move was graded {verdict!r}")
    assert delta == 0
    assert "ran-unverified" in reason, (
        "the reason must name the mechanism, or the operator retries the same "
        f"unmeasurable run. Got: {reason!r}")


def test_a_dry_run_is_inconclusive():
    """`orchestrate(execute=False)` never reaches the wave. Both arms come back
    empty and identical, which is a shape check and not a measurement."""
    trial = ab.run(_graph("a", "b"))
    assert trial.verdict == ab.INCONCLUSIVE
    assert "dry run" in trial.reason


@pytest.mark.parametrize("off_score,on_score,expected", [
    (1, 3, ab.HELPS),
    (3, 1, ab.HURTS),
    (2, 2, ab.NEUTRAL),
])
def test_the_grading_moves_with_the_metric(off_score, on_score, expected):
    """`neutral` IS reachable — but only above zero. A guard that swallowed every
    equal pair into `inconclusive` would be unable to report the one result the
    owner wrote down in advance as decision-relevant."""
    verdict, _reason, delta = ab.verdict_for(_arm("off", off_score),
                                             _arm("on", on_score))
    assert verdict == expected
    assert delta == on_score - off_score


# --- the ledger row -----------------------------------------------------------

def test_the_trial_is_recorded_as_an_experiment_result(tmp_path, monkeypatch):
    """Through `events.append_event`, as the repo's own semantic type — so
    `hive recall` can find the measurement later without a bespoke reader."""
    from mindhive import events as ev_mod

    written = {}

    def fake_append(etype, payload, project=None, **kw):
        written["type"], written["payload"] = etype, payload

    monkeypatch.setattr(ev_mod, "append_event", fake_append)
    trial = ab.Trial(arms=(_arm("off", 1), _arm("on", 2)),
                     verdict=ab.HELPS, reason="r", delta=1)
    ab.record(trial, project="test")

    assert written["type"] == "experiment_result"
    assert written["payload"]["experiment"] == "mindnet-brief-ab"
    assert written["payload"]["verdict"] == ab.HELPS
    assert [a["name"] for a in written["payload"]["arms"]] == ["off", "on"]
    assert written["payload"]["arms"][1]["score"] == 2


def test_a_dry_run_is_not_recorded(monkeypatch, capsys):
    """A measurement-shaped row that measured nothing is worse than no row: it is
    indistinguishable from a real one at recall time."""
    from mindhive import events as ev_mod
    from mindhive.mindnet import tasks as tm

    calls = []
    monkeypatch.setattr(ev_mod, "append_event",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setattr(ev_mod, "read_types", lambda *a, **k: [])
    monkeypatch.setattr(tm, "fold", lambda evs: _graph("a"))

    class Args:
        execute, project, limit, record, as_json = False, None, None, True, False

    assert ab.cmd_ab(Args()) is None
    assert calls == [], "a dry run was recorded as an experiment_result"
    assert "not recorded" in capsys.readouterr().out


def test_the_command_refuses_an_empty_graph(monkeypatch, capsys):
    """`REFUSED`, on stderr, matching every sibling `mindnet` command."""
    from mindhive import events as ev_mod
    from mindhive.mindnet import tasks as tm

    monkeypatch.setattr(ev_mod, "read_types", lambda *a, **k: [])
    monkeypatch.setattr(tm, "fold", lambda evs: {})

    class Args:
        execute, project, limit, record, as_json = False, None, None, False, False

    assert ab.cmd_ab(Args()) == ab.REFUSED
    assert "no tasks on the graph" in capsys.readouterr().err
