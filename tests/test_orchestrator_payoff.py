# tests/test_orchestrator_payoff.py
"""TODOS 27b — the harness that scores weighted lanes against static waves.

The point of this module is that the harness must be able to DISAGREE with the
design it was built to check. `test_the_harness_reports_the_incumbent_winning`
is the test that proves it can: it pins a real unit set, found by sweeping the
harness itself, on which the incumbent finishes first. Without that test the
whole comparison is unfalsifiable and therefore worth nothing.

The other load-bearing one is `test_a_perfect_estimate_leaves_nothing_to_steal`.
It pins the finding, not an opinion: when the weight a lane is packed by is also
the duration it runs for, `shard_plan`'s longest-first packing balances the
lanes exactly and work stealing never fires once. That is the reason `actual`
exists, and a change that quietly makes the two the same again would silently
turn every steal-related number in this repo into a tautology.
"""
import random

import pytest

from mindhive.orchestrator import payoff as pf


# --- the two policies on identical work ---------------------------------------

def test_the_incumbents_sort_order_is_what_the_lanes_beat():
    """The lane side's edge is ORDERING, not stealing. `frontier` sorts for
    determinism, so a heavy unit late in the sort tails the wave; `shard_plan`
    places the heaviest first and does not have that tail."""
    got = pf.compare({"a": 1, "b": 1, "z": 10}, lanes=2)

    assert got.comparable
    assert got.waves.makespan == 11
    assert got.lanes.makespan == 10
    assert got.delta == 1
    assert got.lanes.steals == 0, (
        "the lanes won without stealing anything, so a report crediting the "
        "steal would be crediting the wrong mechanism")


def test_the_harness_reports_the_incumbent_winning():
    """A harness that can only confirm its own design measures nothing.

    This unit set was found by sweeping the harness over 4000 random sets; it is
    the one on which the incumbent came out ahead. If a change to `shard_plan`
    or to the simulation makes this pass by winning instead, that is a result to
    look at, not a test to update."""
    got = pf.compare({"a": 8, "b": 1, "c": 5, "d": 8,
                      "e": 3, "f": 3, "g": 5, "h": 3}, lanes=4)

    assert got.comparable
    assert got.delta == -1, got.as_dict()
    assert got.waves.makespan == 10
    assert got.lanes.makespan == 11
    assert "incumbent finishes" in got.verdict


def test_both_policies_run_every_unit_exactly_once():
    """The partition invariant. A schedule that drops or duplicates a unit can
    post any makespan it likes, so this is checked before any of them mean
    anything."""
    weights = {"a": 5, "b": 13, "c": 2, "d": 8, "e": 21}
    total = sum(weights.values())
    got = pf.compare(weights, lanes=3)

    assert got.waves.busy == total
    assert got.lanes.busy == total


# --- the estimate is not the duration -----------------------------------------

def test_a_perfect_estimate_leaves_nothing_to_steal():
    """The finding, pinned. Packed by the same number it is then run for, every
    lane finishes with its siblings and no lane is ever idle beside a loaded
    one. Stealing is not dead code — it is code that only a WRONG estimate can
    pay for, which is the whole reason `actual` is a separate argument."""
    rng = random.Random(11)
    stolen = 0
    for _ in range(200):
        weights = {chr(97 + i): rng.choice([1, 2, 3, 5, 8, 13, 21, 34, 55])
                   for i in range(rng.randint(2, 8))}
        stolen += pf.simulate_lanes(weights, rng.randint(2, 4)).steals
    assert stolen == 0, (
        f"{stolen} steal(s) fired on a perfect estimate; either the packer no "
        f"longer balances by weight or the simulation is running a duration "
        f"that is not the weight it packed by")


def test_a_wrong_estimate_is_what_a_steal_recovers():
    """The other half. Six units the packer believes are identical, three of
    which actually run twenty times longer: one lane drains, finds a sibling
    still loaded, and takes from it."""
    weights = {k: 10 for k in "abcdef"}
    actual = {"a": 1, "c": 1, "e": 1, "b": 20, "d": 20, "f": 20}
    got = pf.compare(weights, lanes=2, actual=actual)

    assert got.lanes.steals == 1, got.as_dict()
    assert got.lanes.makespan == 40
    assert got.waves.makespan == 41


def test_the_thief_takes_the_victims_oldest_entry_not_its_newest():
    """Fidelity to `queue.WorkStealingScheduler`, not to whichever end happens
    to schedule better. `LaneWorktree.steal` is `popleft` while `take` is `pop`,
    and this unit set is one where the difference shows: the shipped FIFO end
    lands at 26, taking the newest entry instead would land at 24.

    So the harness records the shipped policy being TWO units worse here. That
    is the finding to keep — a harness silently modelling the better end would
    report a payoff the running code does not deliver."""
    weights = {"a": 1, "b": 1, "c": 8, "d": 8, "e": 1, "f": 2, "g": 5}
    actual = {"a": 8, "b": 8, "c": 3, "d": 1, "e": 13, "f": 13, "g": 13}
    got = pf.simulate_lanes(weights, lanes=3, actual=actual)

    assert got.steals == 1
    assert got.makespan == 26, (
        f"{got.makespan} is not the shipped FIFO-steal schedule; 24 is what "
        f"stealing the victim's newest entry would give")


def test_the_victim_is_the_deepest_sibling_not_the_lightest():
    """`_steal_for` orders victims by `-queued_depth()` — a COUNT, so a lane
    holding one enormous unit is never the one robbed. Nine units over four
    lanes is a set where robbing the shallowest instead changes the answer."""
    weights = {"a": 5, "b": 3, "c": 5, "d": 3, "e": 5,
               "f": 8, "g": 5, "h": 1, "i": 5}
    actual = {"a": 3, "b": 5, "c": 1, "d": 34, "e": 21,
              "f": 5, "g": 1, "h": 34, "i": 8}
    got = pf.simulate_lanes(weights, lanes=4, actual=actual)

    assert got.steals == 2
    assert got.makespan == 39, (
        f"{got.makespan} is not the deepest-sibling schedule; 38 is what "
        f"robbing the shallowest loaded lane would give")


def test_an_actual_duration_never_re_packs_the_lanes():
    """`actual` is what happens, not what the planner knew. A packer that could
    see it would be a planner with the future in hand, and every number this
    harness produces would be measuring that instead."""
    weights = {"a": 100, "b": 1}
    honest = pf.simulate_lanes(weights, lanes=2)
    lied_to = pf.simulate_lanes(weights, lanes=2, actual={"a": 1, "b": 100})

    assert honest.workers == lied_to.workers == 2
    assert honest.makespan == 100 and lied_to.makespan == 100, (
        "the packing followed the duration rather than the estimate")


def test_a_duration_for_a_unit_that_is_not_in_the_set_is_ignored():
    got = pf.simulate_lanes({"a": 5}, lanes=1, actual={"a": 7, "ghost": 900})
    assert got.makespan == 7


# --- dependencies -------------------------------------------------------------

def test_a_graph_with_dependencies_is_reported_as_not_comparable():
    """The lane scheduler has no dependency model, so a linked graph is not a
    comparison — and saying so is the honest answer, not a fallback."""
    got = pf.compare({"a": 10, "b": 5}, deps={"b": ["a"]}, lanes=2)

    assert got.comparable is False
    assert "no dependency model" in got.verdict


def test_the_barrier_between_waves_is_what_the_incumbent_pays():
    """Two waves, and the second cannot start until the first has fully drained.
    `a` takes 10 while `b` takes 1, so a worker sits idle for 9 before the
    barrier lifts — that idle time is the incumbent's whole cost."""
    got = pf.simulate_waves({"a": 10, "b": 1, "c": 10, "d": 1},
                            deps={"c": ["a"], "d": ["a"]}, workers=2)

    assert got.waves == 2
    assert got.makespan == 20
    assert got.busy == 22
    assert got.idle == 18


def test_an_edge_to_a_unit_outside_the_set_is_dropped_not_treated_as_unmet():
    """Otherwise a mission naming one task outside its own unit set would
    schedule nothing at all and report a makespan of zero as if it were fast."""
    got = pf.simulate_waves({"a": 4}, deps={"a": ["somebody-elses-task"]},
                            workers=2)
    assert got.makespan == 4
    assert got.waves == 1


def test_a_cycle_stops_the_fold_rather_than_spinning():
    got = pf.simulate_waves({"a": 5, "b": 5}, deps={"a": ["b"], "b": ["a"]},
                            workers=2)
    assert got.waves == 0
    assert got.makespan == 0


# --- degenerate input ---------------------------------------------------------

def test_an_empty_unit_set_compares_to_nothing_and_says_so():
    got = pf.compare({}, lanes=4)
    assert got.comparable is False
    assert got.units == 0
    assert "nothing to compare" in got.verdict


def test_a_zero_weight_unit_is_scheduled_rather_than_dropped():
    got = pf.compare({"a": 0, "b": 4}, lanes=2)
    assert got.units == 2
    assert got.waves.busy == 4 and got.lanes.busy == 4


@pytest.mark.parametrize("bad", [None, -3, "x"])
def test_a_weight_that_is_not_a_positive_number_does_not_raise(bad):
    """Weights arrive from `metrics.calculate_workload_weight` and, in a report
    round-tripped through JSON, from anywhere at all."""
    weights = {"a": bad, "b": 6}
    if bad == "x":
        with pytest.raises((TypeError, ValueError)):
            pf.compare(weights, lanes=2)
        return
    got = pf.compare(weights, lanes=2)
    assert got.lanes.busy == 6 and got.waves.busy == 6


def test_utilisation_of_an_empty_schedule_is_zero_rather_than_a_zero_divide():
    assert pf.Schedule().utilisation == 0.0


# --- the report ---------------------------------------------------------------

def test_the_report_serialises_to_json_able_primitives():
    import json

    got = pf.compare({"a": 3, "b": 9}, lanes=2)
    round_tripped = json.loads(json.dumps(got.as_dict()))

    assert round_tripped["units"] == 2
    assert round_tripped["delta"] == got.delta
    assert round_tripped["lanes"]["steals"] == got.lanes.steals
    assert round_tripped["verdict"] == got.verdict


def test_the_payoff_reaches_the_operator_through_the_command(capsys):
    """Printed, not merely computed. A measurement nobody can read is the same
    as no measurement."""
    from mindhive.orchestrator import cli as cli_mod

    cli_mod._print_payoff(pf.compare({"a": 1, "b": 1, "z": 10}, lanes=2))
    out = capsys.readouterr().out

    assert "payoff over 3 unit(s)" in out
    assert "static waves: makespan 11" in out
    assert "weighted lanes: makespan 10" in out


def test_a_report_that_is_not_a_like_for_like_comparison_says_so_when_printed():
    from mindhive.orchestrator import cli as cli_mod
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli_mod._print_payoff(pf.compare({"a": 2, "b": 2}, deps={"b": ["a"]},
                                         lanes=2))
    assert "not a like-for-like comparison" in buf.getvalue()
