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


# --- is the weight any good? ---------------------------------------------------
#
# Everything above scores a POLICY given an estimate. This half scores the
# ESTIMATE, which every number above inherits: `simulate_lanes` packs by
# `metrics.calculate_workload_weight` and then runs for `actual`, and if those
# two are unrelated the packing is arbitrary and its `4/3` bound buys nothing.
#
# `test_a_backwards_estimate_is_reported_as_backwards` is the one that matters
# most here, for the same reason `test_the_harness_reports_the_incumbent_winning`
# matters above: a calibration that can only report "the weight is fine" is
# unfalsifiable and measures nothing.


def _samples(pairs):
    """`{unit: weight}` and `{unit: ms}` from ``[(unit, weight, ms), ...]``."""
    return ({u: w for u, w, _ in pairs}, {u: ms for u, _, ms in pairs})


def test_a_weight_that_orders_the_work_is_reported_as_ordering_it():
    weights, durations = _samples([("a", 1, 10.0), ("b", 2, 20.0),
                                   ("c", 3, 30.0)])
    got = pf.calibrate(weights, durations)

    assert got.units == 3
    assert got.rho == pytest.approx(1.0)
    assert "the weight orders the work" in got.verdict


def test_a_backwards_estimate_is_reported_as_backwards():
    """The falsifying case. Without it every other assertion here passes for a
    function that returns "looks good" whatever it is handed."""
    weights, durations = _samples([("a", 1, 30.0), ("b", 2, 20.0),
                                   ("c", 3, 10.0)])
    got = pf.calibrate(weights, durations)

    assert got.rho == pytest.approx(-1.0)
    assert "BACKWARDS" in got.verdict


def test_an_estimate_that_predicts_nothing_says_so_rather_than_hedging():
    weights, durations = _samples([("a", 1, 20.0), ("b", 2, 10.0),
                                   ("c", 3, 30.0), ("d", 4, 15.0)])
    got = pf.calibrate(weights, durations)

    assert -0.3 < got.rho < 0.3, got.rho
    assert "does not predict the order" in got.verdict


def test_a_sub_millisecond_unit_is_a_sample_rather_than_a_unit_that_never_ran():
    """The regression that made this whole half report nothing. `elapsed_ms`
    was an integer, so a unit faster than a millisecond floored to zero and was
    dropped here as unmeasured -- leaving the estimate scored only on the units
    slow enough to survive the flooring."""
    weights, durations = _samples([("fast", 1, 0.25), ("slow", 4, 4.0)])
    got = pf.calibrate(weights, durations)

    assert got.units == 2, got.verdict
    assert [s.ms for s in got.samples] == [0.25, 4.0]


def test_a_unit_that_reported_no_duration_is_not_a_sample():
    """A unit that never ran carried as a zero would drag the correlation
    toward whatever the unrun units happened to be estimated at."""
    weights, durations = _samples([("ran", 1, 5.0), ("never", 9, 0.0)])
    got = pf.calibrate(weights, durations)

    assert [s.unit for s in got.samples] == ["ran"]
    assert got.rho is None
    assert "one measured unit cannot show" in got.verdict


def test_a_duration_for_a_unit_nobody_estimated_is_ignored():
    got = pf.calibrate({"a": 1}, {"a": 5.0, "ghost": 9.0})
    assert [s.unit for s in got.samples] == ["a"]


def test_no_measured_unit_at_all_is_reported_as_unchecked_not_as_flat():
    got = pf.calibrate({"a": 1, "b": 2}, {})
    assert got.units == 0
    assert got.rho is None
    assert "still unchecked" in got.verdict


def test_a_correlation_that_cannot_be_computed_is_none_not_zero():
    """``0.0`` would say "the weight predicts nothing", which is a finding.
    Every weight being the same is not that finding -- it is silence."""
    weights, durations = _samples([("a", 2, 10.0), ("b", 2, 20.0)])
    got = pf.calibrate(weights, durations)

    assert got.rho is None
    assert "nothing about the order can be read" in got.verdict


def test_tied_weights_share_a_rank_rather_than_taking_dict_order():
    """Two units estimated the same is the common case. Breaking that tie by
    position would let the order the dict arrived in show up as correlation."""
    assert pf._ranks([5, 5, 9]) == [1.5, 1.5, 3.0]
    assert pf._ranks([9, 5, 5]) == [3.0, 1.5, 1.5]


def test_the_spread_is_reported_even_when_the_order_cannot_be():
    """Two units estimated at the SAME weight that cost four times as much as
    each other say the estimate is wrong about magnitude, and that is a finding
    even though nothing can be said about order. It used to be computed and
    then left out of the sentence anybody reads."""
    weights, durations = _samples([("a", 1, 10.0), ("b", 1, 40.0)])
    got = pf.calibrate(weights, durations)

    assert got.rho is None
    assert got.spread == pytest.approx(4.0)
    assert "nothing about the order can be read" in got.verdict
    assert "4.0x the cheapest" in got.verdict


def test_the_spread_rides_a_readable_correlation_too():
    weights, durations = _samples([("a", 1, 10.0), ("b", 2, 80.0)])
    got = pf.calibrate(weights, durations)

    assert got.spread == pytest.approx(4.0)
    assert "4.0x the cheapest" in got.verdict


def test_one_rate_alone_reports_no_spread_rather_than_a_ratio_of_one():
    """A single rate has nothing to be a ratio against, and 1.0 would read as
    a measured finding that the estimate is uniform."""
    got = pf.calibrate({"a": 1}, {"a": 10.0, "b": 20.0})

    assert got.spread == 0.0
    assert "the cheapest" not in got.verdict


def test_an_unweighted_sample_has_no_rate_rather_than_a_zero_divide():
    assert pf.Sample(unit="a", weight=0, ms=10.0).ms_per_weight == 0.0


@pytest.mark.parametrize("bad", [None, "", "abc", object()])
def test_a_duration_that_is_not_a_number_does_not_raise(bad):
    got = pf.calibrate({"a": 1, "b": 2}, {"a": bad, "b": 4.0})
    assert [s.unit for s in got.samples] == ["b"]


def test_the_calibration_serialises_to_json_able_primitives():
    import json

    weights, durations = _samples([("a", 1, 0.25), ("b", 2, 4.5)])
    got = pf.calibrate(weights, durations).as_dict()

    assert json.loads(json.dumps(got))["samples"][0]["ms"] == 0.25


# --- the samples outlive the mission -------------------------------------------

def test_samples_round_trip_through_the_record():
    pf.record_samples([pf.Sample(unit="a", weight=2, ms=0.25)],
                      mission_id="m1", project="test")
    got = pf.history("test")

    assert [(s.unit, s.weight, s.ms) for s in got] == [("a", 2, 0.25)]


def test_a_second_mission_appends_rather_than_replacing():
    """One mission's units answer nothing about an estimator; forty units over
    ten missions begin to."""
    pf.record_samples([pf.Sample(unit="a", weight=1, ms=1.0)], project="test")
    pf.record_samples([pf.Sample(unit="a", weight=1, ms=2.0)], project="test")

    assert [s.ms for s in pf.history("test")] == [1.0, 2.0]


def test_the_same_unit_measured_twice_is_two_samples_not_one():
    """`folded` scores a sequence. Folded through a dict keyed by unit id it
    would keep only the last measurement and quietly turn a history of forty
    into a history of one."""
    pf.record_samples([pf.Sample(unit="a", weight=1, ms=1.0)], project="test")
    pf.record_samples([pf.Sample(unit="a", weight=9, ms=9.0)], project="test")

    assert pf.folded("test").units == 2
    assert [s.ms for s in pf.folded("test").samples] == [1.0, 9.0]


def test_the_folded_history_carries_the_unit_name_the_operator_recorded():
    pf.record_samples([pf.Sample(unit="writer", weight=1, ms=1.0),
                       pf.Sample(unit="reader", weight=2, ms=2.0)],
                      project="test")

    assert [s.unit for s in pf.folded("test").samples] == ["writer", "reader"]


def test_the_history_keeps_the_order_it_was_recorded_in():
    """`--calibration` prints the TAIL of this list as the most recent samples.
    Ordered by anything else, that tail is a different dozen — an earlier cut
    sorted composite `"{i}:{unit}"` keys, which put "9..." last whatever the
    real order was."""
    for i in range(12):
        pf.record_samples([pf.Sample(unit=f"u{i}", weight=1, ms=float(i + 1))],
                          project="test")

    assert [s.unit for s in pf.folded("test").samples[-3:]] == ["u9", "u10",
                                                               "u11"]


def test_a_sample_with_no_measured_time_is_not_recorded():
    kept = pf.record_samples([pf.Sample(unit="a", weight=1, ms=0.0)],
                             project="test")
    assert kept == 0
    assert pf.history("test") == ()


def test_the_record_is_capped_so_it_stays_a_file_a_person_can_read():
    pf.record_samples([pf.Sample(unit=f"u{i}", weight=1, ms=1.0)
                       for i in range(pf.MAX_SAMPLES + 25)], project="test")
    got = pf.history("test")

    assert len(got) == pf.MAX_SAMPLES
    assert got[-1].unit == f"u{pf.MAX_SAMPLES + 24}", "the OLDEST must go first"


def test_a_record_that_does_not_exist_reads_as_no_samples():
    assert pf.history("test") == ()
    assert pf.folded("test").units == 0


def test_a_corrupt_record_reads_as_no_samples_rather_than_raising():
    target = pf.samples_path("test")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")

    assert pf.history("test") == ()


def test_a_row_that_is_not_a_sample_is_skipped_not_guessed_at():
    import json

    target = pf.samples_path("test")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"samples": [
        "not a dict", {"unit": "a", "weight": 1, "ms": 3.0},
        {"unit": "b", "weight": "x", "ms": 1.0}]}), encoding="utf-8")

    assert [s.unit for s in pf.history("test")] == ["a"]


def test_a_store_that_cannot_be_written_does_not_fail_the_mission(monkeypatch):
    from mindhive import paths

    def _boom(*_a, **_kw):
        raise OSError("nope")

    monkeypatch.setattr(paths, "write_text_atomic", _boom)
    assert pf.record_samples([pf.Sample(unit="a", weight=1, ms=1.0)],
                             project="test") == 1


# --- the durable finding -------------------------------------------------------

def _experiment_rows():
    from mindhive import events as ev_mod
    return [e for e in ev_mod.read_all("test") if e.type == "experiment_result"]


def test_a_scored_calibration_lands_in_the_ledger_as_one_row():
    weights, durations = _samples([("a", 1, 10.0), ("b", 2, 20.0)])
    cal = pf.calibrate(weights, durations)

    assert pf.record_result(cal, mission_id="m1", project="test") is True
    rows = _experiment_rows()
    assert len(rows) == 1, "one row per mission, never one per unit"
    assert "m1" in rows[0].payload["text"]
    assert rows[0].payload["units"] == 2


def test_a_calibration_with_nothing_to_say_records_nothing():
    """The negative half. A row saying "one unit, no correlation" is noise in
    the memo stream it lands in."""
    cal = pf.calibrate({"a": 1}, {"a": 5.0})

    assert pf.record_result(cal, mission_id="m1", project="test") is False
    assert pf.record_result(None, project="test") is False
    assert _experiment_rows() == []


# --- the calibration reaches the operator --------------------------------------

def test_the_calibration_is_readable_from_the_command_line(capsys):
    """Printed, not merely recorded. A measurement nobody can read is the same
    as no measurement."""
    from mindhive import __main__ as cli_mod

    pf.record_samples([pf.Sample(unit="a", weight=1, ms=10.0),
                       pf.Sample(unit="b", weight=4, ms=40.0)],
                      mission_id="m1", project="test")

    assert cli_mod.main(["lintester", "--calibration"]) == 0
    out = capsys.readouterr().out
    assert "weight calibration over 2 recorded sample(s)" in out
    assert "the weight orders the work" in out
    assert "a: weight 1 -> 10.000 ms" in out


def test_the_calibration_is_asked_of_the_record_without_planning_a_mission():
    """It is a question about history, not a mission. Demanding an intent to
    ask it would mean planning a mission nobody wanted to read one line."""
    from mindhive.orchestrator import cli as cli_mod

    started = []

    class _Args:
        calibration = True
        project = "test"
        intent = ""

    from mindhive.orchestrator import lintester as lint_mod
    real = lint_mod.LintesterOrchestrator.execute_mission
    try:
        lint_mod.LintesterOrchestrator.execute_mission = (
            lambda self, *a, **kw: started.append(1))
        assert cli_mod.cmd_lintester(_Args()) == 0
    finally:
        lint_mod.LintesterOrchestrator.execute_mission = real
    assert started == [], "asking for the record started a mission"


def test_an_empty_record_says_how_to_produce_one(capsys):
    from mindhive import __main__ as cli_mod

    assert cli_mod.main(["lintester", "--calibration"]) == 0
    out = capsys.readouterr().out
    assert "no mission has recorded a measured duration yet" in out
    assert "--execute" in out


def test_a_sub_millisecond_sample_is_printed_as_the_duration_it_was(capsys):
    """Printed at `%d` it read as `0 ms` — a measurement rendered as its own
    absence, in the one place an operator would look for it."""
    from mindhive.orchestrator import cli as cli_mod

    pf.record_samples([pf.Sample(unit="fast", weight=1, ms=0.25),
                       pf.Sample(unit="slow", weight=4, ms=4.0)],
                      project="test")
    cli_mod._print_calibration("test")
    out = capsys.readouterr().out

    assert "  fast: weight 1 -> 0.250 ms" in out
    assert "-> 0 ms" not in out, "a measurement was rendered as its own absence"
    assert "0:fast" not in out, (
        "an internal key reached the operator instead of the unit name")
