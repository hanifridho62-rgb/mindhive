# SPDX-License-Identifier: Apache-2.0
"""The integration breaker and the per-lane quota (blueprint P1f; MEB ORCH-03,
risk 6 / K21).

Two controls that both answer `core.models.BreakerVerdict`, and are kept apart
on purpose:

* `IntegrationBreaker` is a CLOSED -> OPEN -> HALF_OPEN -> QUARANTINED state
  machine over MEB 2.2's parameters (`FAILURE_THRESHOLD = 3`,
  `COOLDOWN_PERIOD_S = 300`), persisted atomically to
  `state/integration_breaker.json`. It guards `integrate_lanes`: a run that
  keeps failing to merge stops merging rather than grinding the integration
  branch against the same conflict forever.
* `LaneQuota` is a budget, not a circuit: it never trips, it just answers
  whether one lane's accumulated usage is still inside its ceiling.

The tool breaker (`orchestrator/resilience.ToolCircuitBreaker`, BAB 3's
`BREAKER_PARAMS`) is a different object with different numbers and is NOT
touched here -- decision K1 keeps both, and a test below pins them apart so a
later edit cannot quietly unify them.
"""
import json
import time

import pytest

from mindhive import events, models, paths
from mindhive.core import models as core_models
from mindhive.orchestrator import breaker as brk
from mindhive.orchestrator import resilience


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MINDHIVE_EVENT_BACKEND", raising=False)
    return tmp_path


def _clock(values):
    """A monotonic seam that walks *values* and then holds the last one."""
    seq = list(values)

    def now():
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return now


# --- the parameters are MEB's, and the tool breaker keeps BAB 3's -----------------

def test_the_integration_breaker_runs_on_the_meb_parameters():
    """MEB 2.2 A verbatim: three consecutive failures trip, and OPEN lasts
    300 s before a probe is admitted."""
    assert brk.IntegrationBreaker.FAILURE_THRESHOLD == 3
    assert brk.IntegrationBreaker.COOLDOWN_PERIOD_S == 300.0
    assert models.INTEGRATION_BREAKER_PARAMS == {
        "failure_threshold": brk.IntegrationBreaker.FAILURE_THRESHOLD,
        "cooldown_period_s": brk.IntegrationBreaker.COOLDOWN_PERIOD_S}


def test_the_quarantine_window_is_this_modules_own_decision():
    """MEB gives `p_q` but no window, so the two numbers that close it are a
    decision recorded HERE and in the docstring -- not a silent invention."""
    assert brk.IntegrationBreaker.MAX_TRIPS_IN_WINDOW == 3
    assert brk.IntegrationBreaker.TRIP_WINDOW_S == 3600.0
    doc = brk.IntegrationBreaker.__doc__ or ""
    assert "MAX_TRIPS_IN_WINDOW" in doc and "TRIP_WINDOW_S" in doc


def test_the_tool_breaker_keeps_its_own_numbers(store):
    """DISCRIMINATING. K1 keeps two breakers. If a later edit points the
    integration breaker at `BREAKER_PARAMS`, or the tool breaker at the MEB
    numbers, this fails."""
    assert resilience.DEFAULT_SAME_ERROR == 2
    assert resilience.DEFAULT_NO_PROGRESS == 3
    assert resilience.DEFAULT_COOLDOWN_S == 30.0
    assert brk.IntegrationBreaker.COOLDOWN_PERIOD_S != resilience.DEFAULT_COOLDOWN_S
    assert models.BREAKER_PARAMS["cooldown_s"] != brk.IntegrationBreaker.COOLDOWN_PERIOD_S


def test_the_transition_table_gained_the_repeat_trip_edge():
    """K1: OPEN -> QUARANTINED on a repeated trip is the MEB matrix's `p_q`,
    which BAB 3's table (QUARANTINED from HALF_OPEN) does not have. Both edges
    exist afterwards; the older one is not replaced."""
    assert "trip_repeat" in models.BREAKER_EVENTS
    assert models.circuit_breaker_transition(models.OPEN, "trip_repeat") \
        == models.QUARANTINED
    assert models.circuit_breaker_transition(
        models.HALF_OPEN, "probe_fail_max_attempts") == models.QUARANTINED


# --- the state machine ------------------------------------------------------------

def test_a_fresh_breaker_is_closed_and_allows(store):
    b = brk.IntegrationBreaker(project="p")
    v = b.check()
    assert isinstance(v, core_models.BreakerVerdict)
    assert v.allowed is True and v.state == models.CLOSED
    assert v.reason


def test_two_failures_do_not_trip_and_a_success_clears_the_count(store):
    """The threshold counts CONSECUTIVE failures: a success in between resets
    it, or a breaker on a flaky-but-recovering lane would trip on noise."""
    b = brk.IntegrationBreaker(project="p")
    b.record_failure("conflict")
    b.record_failure("conflict")
    assert b.check().allowed is True
    b.record_success()
    assert b.state().failure_count == 0
    b.record_failure("conflict")
    b.record_failure("conflict")
    assert b.check().allowed is True


def test_the_third_consecutive_failure_trips_it_open(store):
    b = brk.IntegrationBreaker(project="p", clock=_clock([100.0]))
    for _ in range(3):
        b.record_failure("conflicts with the integration tree")
    v = b.check()
    assert v.allowed is False and v.state == models.OPEN
    assert "conflicts with the integration tree" in v.reason
    st = b.state()
    assert st.state == models.OPEN and st.failure_count == 3
    assert st.last_trip_ts


def test_open_refuses_until_the_cooldown_elapses_then_admits_one_probe(store):
    """Exactly MEB's 300 s, and the boundary is STRICT: at the cooldown it is
    still OPEN, past it the breaker is HALF_OPEN and admits one call."""
    now = {"t": 1000.0}
    b = brk.IntegrationBreaker(project="p", clock=lambda: now["t"])
    for _ in range(3):
        b.record_failure("boom")
    now["t"] = 1000.0 + 300.0
    assert b.check().allowed is False
    now["t"] = 1000.0 + 300.0 + 0.001
    v = b.check()
    assert v.allowed is True and v.state == models.HALF_OPEN


def test_a_probe_that_succeeds_closes_it(store):
    now = {"t": 0.0}
    b = brk.IntegrationBreaker(project="p", clock=lambda: now["t"])
    for _ in range(3):
        b.record_failure("boom")
    now["t"] = 400.0
    assert b.check().allowed is True
    b.record_success()
    st = b.state()
    assert st.state == models.CLOSED and st.failure_count == 0
    assert b.check().allowed is True


def test_a_probe_that_fails_reopens_it_and_restarts_the_cooldown(store):
    now = {"t": 0.0}
    b = brk.IntegrationBreaker(project="p", clock=lambda: now["t"])
    for _ in range(3):
        b.record_failure("boom")
    now["t"] = 400.0
    assert b.check().allowed is True            # HALF_OPEN, one probe
    b.record_failure("still broken")
    assert b.state().state == models.OPEN
    now["t"] = 400.0 + 299.0
    assert b.check().allowed is False           # the cooldown restarted at 400
    now["t"] = 400.0 + 301.0
    assert b.check().allowed is True


def test_three_trips_inside_the_window_quarantine_it(store):
    """MEB's `p_q` edge: repeated trips are not one bad merge, they are a lane
    that must stop being retried until a person looks."""
    now = {"t": 0.0}
    b = brk.IntegrationBreaker(project="p", clock=lambda: now["t"])
    for trip in range(3):
        for _ in range(3):
            b.record_failure(f"trip {trip}")
        now["t"] += 301.0
        b.check()                                # cooldown -> HALF_OPEN probe
    st = b.state()
    assert st.state == models.QUARANTINED
    assert st.quarantine_reason
    v = b.check()
    assert v.allowed is False and v.state == models.QUARANTINED


def test_trips_that_age_out_of_the_window_do_not_quarantine(store):
    """DISCRIMINATING. The window is a window: three trips an hour apart are
    three separate incidents, not an escalation."""
    now = {"t": 0.0}
    b = brk.IntegrationBreaker(project="p", clock=lambda: now["t"])
    for _ in range(3):
        for _ in range(3):
            b.record_failure("boom")
        now["t"] += 3601.0
        b.check()
    assert b.state().state != models.QUARANTINED


def test_three_trips_spanning_more_than_the_window_do_not_quarantine(store):
    """DISCRIMINATING for the window's VALUE. Trips at 0, 2000 and 4000 are
    each within an hour of the NEXT one, so a pairwise reading keeps all three
    -- but they SPAN 4000 s, and only two are ever inside one 3600 s window at
    a time. Widen `TRIP_WINDOW_S` in `_recent_trips` and all three are retained
    and the circuit quarantines, which only `hive ci breaker reset` can leave:
    exactly the "three unrelated bad days do not add up to a quarantine"
    outcome the number was chosen to prevent."""
    now = {"t": 0.0}
    b = brk.IntegrationBreaker(project="p", clock=lambda: now["t"])
    for _ in range(3):
        b.record_failure("boom")
    assert b.state().state == models.OPEN
    for stamp in (2000.0, 4000.0):
        now["t"] = stamp
        assert b.check().state == models.HALF_OPEN
        b.record_failure("boom")                # a HALF_OPEN failure trips at once
    assert len(b.state().trips_in_window) == 2
    assert b.state().state != models.QUARANTINED


def test_the_window_is_inclusive_at_its_own_edge(store):
    """The boundary itself, by value: a trip exactly `TRIP_WINDOW_S` old is
    still inside, one moment older is not."""
    b = brk.IntegrationBreaker(project="q", clock=lambda: 3600.0)
    b._state = brk.CircuitBreakerState(state=models.CLOSED,
                                       trips_in_window=("t0.000000",))
    assert b._recent_trips(brk.IntegrationBreaker.TRIP_WINDOW_S) == ("t0.000000",)
    assert b._recent_trips(brk.IntegrationBreaker.TRIP_WINDOW_S + 0.1) == ()


def test_only_a_manual_reset_leaves_quarantine(store):
    now = {"t": 0.0}
    b = brk.IntegrationBreaker(project="p", clock=lambda: now["t"])
    for _ in range(3):
        for _ in range(3):
            b.record_failure("boom")
        now["t"] += 301.0
        b.check()
    assert b.state().state == models.QUARANTINED
    now["t"] += 100_000.0
    assert b.check().allowed is False            # no cooldown out of quarantine
    b.manual_reset()
    st = b.state()
    assert st.state == models.CLOSED and st.failure_count == 0
    assert st.quarantine_reason is None and st.trips_in_window == ()
    assert b.check().allowed is True


# --- persistence ------------------------------------------------------------------

def test_the_state_is_persisted_where_the_plan_says(store):
    b = brk.IntegrationBreaker(project="p")
    b.record_failure("boom")
    path = paths.state_dir("p") / "integration_breaker.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["failure_count"] == 1


def test_a_second_breaker_reads_what_the_first_wrote(store):
    """The breaker is per-PROCESS but the state is per-PROJECT: a mission in a
    new process must not start from a clean circuit."""
    a = brk.IntegrationBreaker(project="p", clock=_clock([10.0]))
    for _ in range(3):
        a.record_failure("boom")
    b = brk.IntegrationBreaker(project="p", clock=_clock([20.0]))
    assert b.state().state == models.OPEN
    assert b.check().allowed is False


def test_the_write_is_atomic(store, monkeypatch):
    """Through `paths.write_text_atomic`, so a reader never sees a torn file."""
    seen = []
    real = paths.write_text_atomic
    monkeypatch.setattr(paths, "write_text_atomic",
                        lambda p, t: (seen.append(str(p)), real(p, t))[1])
    brk.IntegrationBreaker(project="p").record_failure("boom")
    assert any(s.endswith("integration_breaker.json") for s in seen)


def test_a_corrupt_state_file_reads_as_closed_with_a_warning(store):
    """Fail-OPEN on unreadable state: a breaker that refuses every merge
    because its own sidecar is malformed is a worse outage than the one it
    guards against. It says so rather than pretending the file was fine."""
    path = paths.state_dir("p") / "integration_breaker.json"
    path.write_text("{not json", encoding="utf-8")
    b = brk.IntegrationBreaker(project="p")
    st = b.state()
    assert st.state == models.CLOSED and st.failure_count == 0
    assert b.warnings and "integration_breaker.json" in b.warnings[0]
    assert b.check().allowed is True


@pytest.mark.parametrize("junk", [
    '{"state": "NOT_A_STATE"}',
    '{"state": "OPEN", "failure_count": -3}',
    '{"state": "OPEN", "surprise": 1}',
    '[]',
    '"closed"',
])
def test_every_shape_the_dataclass_refuses_reads_as_closed(store, junk):
    """Total over the file: `CircuitBreakerState.from_dict` refuses unknown
    keys and bad values, and each refusal is a stale sidecar, not a crash."""
    (paths.state_dir("p") / "integration_breaker.json").write_text(
        junk, encoding="utf-8")
    b = brk.IntegrationBreaker(project="p")
    assert b.state().state == models.CLOSED
    assert b.warnings


def test_the_persisted_shape_is_the_core_dataclasss_own(store):
    """K6: `core.models.CircuitBreakerState` IS the persistence format -- the
    breaker does not keep a second, hand-rolled dict shape beside it."""
    b = brk.IntegrationBreaker(project="p")
    for _ in range(3):
        b.record_failure("boom")
    raw = json.loads((paths.state_dir("p") / "integration_breaker.json")
                     .read_text(encoding="utf-8"))
    assert set(raw) == set(core_models.CircuitBreakerState._FIELDS)
    assert core_models.CircuitBreakerState.from_dict(raw) == b.state()


# --- the lane quota ---------------------------------------------------------------

def test_a_quota_with_no_ceilings_allows_anything(store):
    q = brk.LaneQuota()
    v = q.check(brk.LaneUsage(tokens=10 ** 9, cost_usd=10 ** 6))
    assert isinstance(v, core_models.BreakerVerdict)
    assert v.allowed is True and v.state == models.CLOSED


def test_a_quota_refuses_past_its_token_ceiling(store):
    q = brk.LaneQuota(max_tokens=1000)
    assert q.check(brk.LaneUsage(tokens=1000)).allowed is True   # AT is inside
    v = q.check(brk.LaneUsage(tokens=1001))
    assert v.allowed is False and "1001" in v.reason and "1000" in v.reason


def test_a_quota_refuses_past_its_cost_ceiling(store):
    q = brk.LaneQuota(max_cost_usd=2.50)
    assert q.check(brk.LaneUsage(cost_usd=2.50)).allowed is True
    v = q.check(brk.LaneUsage(cost_usd=2.51))
    assert v.allowed is False and "2.5" in v.reason


def test_a_quota_is_not_a_circuit_and_keeps_no_state(store):
    """DISCRIMINATING. Asking twice with the same usage answers the same
    thing: a refusal does not latch, and nothing is persisted."""
    q = brk.LaneQuota(max_tokens=10)
    assert q.check(brk.LaneUsage(tokens=50)).allowed is False
    assert q.check(brk.LaneUsage(tokens=5)).allowed is True
    assert not (paths.state_dir("p") / "lane_quota.json").exists()


def test_usage_accumulates_by_addition(store):
    a = brk.LaneUsage(tokens=10, cost_usd=0.5)
    b = brk.LaneUsage(tokens=5, cost_usd=0.25)
    assert a.plus(b) == brk.LaneUsage(tokens=15, cost_usd=0.75)
    assert a == brk.LaneUsage(tokens=10, cost_usd=0.5)   # frozen, not mutated


@pytest.mark.parametrize("kwargs", [
    {"max_tokens": -1}, {"max_cost_usd": -0.01},
    {"max_tokens": "lots"}, {"max_cost_usd": float("nan")},
])
def test_a_nonsense_ceiling_is_refused_at_construction(store, kwargs):
    with pytest.raises(ValueError):
        brk.LaneQuota(**kwargs)


def test_unavailable_usage_is_zero_not_a_refusal(store):
    """The runner may not report usage at all (K21): absent usage must read as
    "nothing spent", never as "over budget" -- a quota that refuses because it
    cannot measure would stop every mission on a runner without token
    reporting."""
    q = brk.LaneQuota(max_tokens=10)
    assert q.check(brk.LaneUsage()).allowed is True
    assert brk.LaneUsage().tokens == 0 and brk.LaneUsage().cost_usd == 0.0


# --- integrate_lanes refuses under an open circuit --------------------------------

class _Tree:
    def __init__(self, lane_id, path, branch, base="base-sha"):
        self.ok, self.lane_id, self.path = True, lane_id, path
        self.branch, self.base = branch, base


def test_integrate_lanes_refuses_while_the_breaker_is_open(store, monkeypatch):
    """The refusal is total: no integration checkout is made, no lane is
    merged, and every candidate is reported as skipped with the reason."""
    from mindhive.orchestrator import integrate

    b = brk.IntegrationBreaker(project="p", clock=_clock([1.0]))
    for _ in range(3):
        b.record_failure("boom")
    monkeypatch.setattr(integrate.mn_wt, "state", lambda p, base: (False, 2))
    monkeypatch.setattr(integrate.mn_wt, "add", _never)
    said = []
    got = integrate.integrate_lanes(
        "root", [_Tree(0, "lane0", "lint/m/0")], "m",
        emit=said.append, breaker=b)
    assert got.ok is False
    assert "breaker" in got.reason.lower() or "OPEN" in got.reason
    assert len(got.skipped) == 1 and got.merged == ()
    assert any("refused" in line for line in said)


def _never(*a, **k):
    raise AssertionError("integrate_lanes must not touch git while refusing")


def test_integrate_lanes_refuses_while_quarantined(store, monkeypatch):
    from mindhive.orchestrator import integrate

    now = {"t": 0.0}
    b = brk.IntegrationBreaker(project="p", clock=lambda: now["t"])
    for _ in range(3):
        for _ in range(3):
            b.record_failure("boom")
        now["t"] += 301.0
        b.check()
    assert b.state().state == models.QUARANTINED
    monkeypatch.setattr(integrate.mn_wt, "state", lambda p, base: (False, 2))
    monkeypatch.setattr(integrate.mn_wt, "add", _never)
    got = integrate.integrate_lanes("root", [_Tree(0, "lane0", "lint/m/0")],
                                    "m", breaker=b)
    assert got.ok is False and got.merged == ()


def test_a_closed_breaker_does_not_change_the_old_behaviour(store, monkeypatch):
    """CANARY. With the circuit closed, `integrate_lanes` runs exactly as it
    did before P1f -- the no-lane-committed short circuit included."""
    from mindhive.orchestrator import integrate

    monkeypatch.setattr(integrate.mn_wt, "state", lambda p, base: (False, 0))
    got = integrate.integrate_lanes("root", [_Tree(0, "lane0", "lint/m/0")],
                                    "m", breaker=brk.IntegrationBreaker(project="p"))
    assert got.ok is True and got.reason == "no lane committed anything"


def test_integrate_lanes_still_takes_its_four_positional_arguments(store, monkeypatch):
    """`lintester.py` is ZERO-EDIT (RP GAP 4): it calls
    `integrate_lanes(root, trees, mission_id, emit=...)` and that call must
    keep working with no breaker passed at all."""
    from mindhive.orchestrator import integrate

    monkeypatch.setattr(integrate.mn_wt, "state", lambda p, base: (False, 0))
    got = integrate.integrate_lanes("root", [_Tree(0, "lane0", "lint/m/0")],
                                    "m", emit=None)
    assert got.ok is True


def test_the_switch_command_the_refusal_names_is_the_integration_branch(store, monkeypatch):
    """The message has to be actionable: it names the branch a person would
    check out to look at the wreckage, spelled the way `integrate` spells it."""
    from mindhive.orchestrator import integrate

    b = brk.IntegrationBreaker(project="p", clock=_clock([1.0]))
    for _ in range(3):
        b.record_failure("boom")
    monkeypatch.setattr(integrate.mn_wt, "state", lambda p, base: (False, 2))
    monkeypatch.setattr(integrate.mn_wt, "add", _never)
    said = []
    integrate.integrate_lanes("root", [_Tree(0, "lane0", "lint/m/0")], "m",
                              emit=said.append, breaker=b)
    branch = integrate.integration_branch_for("m")
    assert branch == "lint/m/integration"
    assert any(f"git switch {branch}" in line for line in said)


def _stamped(root, base, ref, trailer):
    """A lane whose one commit past the base is one `commit_lane` stamped, so
    the provenance check passes and the failure under test is `add`'s."""
    from mindhive.mindnet import worktree as mn_wt
    return (mn_wt.LogEntry("a" * 40, ("t-1",)),)


def test_a_merge_failure_is_what_feeds_the_breaker(store, monkeypatch):
    """The wiring that matters: a lane that could not be merged records a
    failure, a clean run records a success. Without this the breaker is a
    state machine nothing ever drives."""
    from mindhive.orchestrator import integrate

    b = brk.IntegrationBreaker(project="p")
    monkeypatch.setattr(integrate.mn_wt, "state", lambda p, base: (False, 2))
    monkeypatch.setattr(integrate.mn_wt, "commits_since", _stamped)
    monkeypatch.setattr(integrate.mn_wt, "add", lambda *a, **k: integrate.mn_wt.Isolated(
        task_id="integration", path=str(store / "iso"), branch="lint/m/integration",
        ok=False, reason="no checkout"))
    integrate.integrate_lanes("root", [_Tree(0, "lane0", "lint/m/0")], "m",
                              breaker=b)
    assert b.state().failure_count == 1


# --- the mission status a refusal leaves ------------------------------------------

def test_a_refusal_is_partial_not_a_crash(store, monkeypatch):
    """A refused integration is a mission that did SOME work: the lanes ran,
    they just did not land. `ok=False` with every lane in `skipped` is what
    the caller already knows how to render."""
    from mindhive.orchestrator import integrate

    b = brk.IntegrationBreaker(project="p", clock=_clock([1.0]))
    for _ in range(3):
        b.record_failure("boom")
    monkeypatch.setattr(integrate.mn_wt, "state", lambda p, base: (False, 2))
    monkeypatch.setattr(integrate.mn_wt, "add", _never)
    got = integrate.integrate_lanes(
        "root", [_Tree(0, "lane0", "lint/m/0"), _Tree(1, "lane1", "lint/m/1")],
        "m", breaker=b)
    assert got.ok is False
    assert {m.lane_id for m in got.skipped} == {0, 1}
    assert all(not m.merged and m.reason for m in got.skipped)
    assert got.as_dict()["skipped"]


def test_the_default_circuit_is_the_missions_own_and_really_refuses(store,
                                                                   monkeypatch):
    """`integrate_lanes` has no `project` parameter (its caller cannot be
    edited), so with no breaker passed it builds one ITSELF -- and that circuit
    must key on the MISSION's checkout, not on whatever directory the
    orchestrator process happens to be sitting in.

    Asserted through the refusal rather than through the constructor's kwargs:
    a `_breaker_of` that built a breaker and returned None would satisfy a
    kwargs assertion while merging every lane uncircuited."""
    from mindhive.orchestrator import integrate

    # `tests/conftest.py` pins MINDHIVE_PROJECT for store isolation, and an
    # explicit override rightly beats any directory -- which would collapse
    # both namespaces here and make the assertion vacuous.
    monkeypatch.delenv("MINDHIVE_PROJECT", raising=False)
    monkeypatch.setattr(integrate.mn_wt, "state", lambda p, base: (False, 1))
    root = store / "mission-repo"
    root.mkdir()
    b = brk.IntegrationBreaker(project=paths.detect_project(root))
    for _ in range(3):
        b.record_failure("boom")
    assert b.state().state == models.OPEN

    got = integrate.integrate_lanes(str(root),
                                    [_Tree(0, "lane0", "lint/m/0")], "m")
    assert got.ok is False
    assert "breaker" in got.reason, got.reason
    assert {m.lane_id for m in got.skipped} == {0}


def test_the_default_circuit_does_not_key_on_the_process_cwd(store, monkeypatch):
    """DISCRIMINATING. A circuit tripped for a DIFFERENT checkout must not
    refuse this mission. With `project=None` the namespace comes from
    `detect_project()` over the process's cwd, so two missions in two
    checkouts shared one circuit and either could stop the other."""
    from mindhive.orchestrator import integrate

    monkeypatch.delenv("MINDHIVE_PROJECT", raising=False)
    monkeypatch.setattr(integrate.mn_wt, "state", lambda p, base: (False, 1))
    monkeypatch.setattr(integrate.mn_wt, "commits_since", _stamped)
    other = store / "someone-elses-repo"
    other.mkdir()
    root = store / "mission-repo"
    root.mkdir()
    b = brk.IntegrationBreaker(project=paths.detect_project(other))
    for _ in range(3):
        b.record_failure("boom")
    assert b.state().state == models.OPEN

    # `root` is not a git checkout, so the integration fails at `mn_wt.add`
    # -- which is exactly the point: the reason must be THAT, not a circuit
    # that belongs to a different repository.
    got = integrate.integrate_lanes(str(root),
                                    [_Tree(0, "lane0", "lint/m/0")], "m")
    assert "breaker" not in (got.reason or ""), got.reason


# --- the events ------------------------------------------------------------------

def test_a_trip_is_recorded_where_an_operator_would_look(store):
    """An anomaly, the way `resilience.record_anomaly` records one -- not a new
    event type. The breaker's own state file is the machine-readable half; this
    is the half a person reads in Oversee."""
    b = brk.IntegrationBreaker(project="p", clock=_clock([1.0]))
    for _ in range(3):
        b.record_failure("conflicts everywhere")
    rows = [e for e in events.read_all("p") if e.type == "tool_failed"]
    assert rows, "the trip left no trace an operator could find"
    assert any("breaker" in json.dumps(e.payload).lower() for e in rows)
    assert rows[0].payload["action"] == "lintester:integration_breaker"


def test_only_a_trip_is_recorded_not_every_failure(store):
    """DISCRIMINATING. Two failures are not an incident; the third one is.
    An anomaly per failed merge would bury the trip it exists to surface."""
    b = brk.IntegrationBreaker(project="p", clock=_clock([1.0]))
    b.record_failure("once")
    b.record_failure("twice")
    assert [e for e in events.read_all("p") if e.type == "tool_failed"] == []
    b.record_failure("thrice")
    assert len([e for e in events.read_all("p") if e.type == "tool_failed"]) == 1


# --- the clock the state file can actually carry ------------------------------------
# Round 2. The circuit is persisted BECAUSE the process that tripped it is not the
# process that would trip it again -- which makes the stamp in that file a value a
# LATER process has to be able to read. `time.monotonic()` is not one: it is an
# offset from an arbitrary origin that restarts at every boot, so an OPEN breaker
# written before a reboot reads back as a trip in the far future and never cools
# down again. The clock seam stays; only its default changes.

def test_the_default_clock_is_one_a_later_process_can_read(store):
    """The stamp in `integration_breaker.json` must be wall-clock seconds.

    DISCRIMINATING: with `time.monotonic` as the default this stamp is an
    uptime offset -- on a machine up for a day it reads as 1970 -- and the
    assertion below fails by six orders of magnitude."""
    b = brk.IntegrationBreaker(project="p")
    for _ in range(3):
        b.record_failure("boom")
    stamp = float(str(b.state().last_trip_ts).lstrip("t"))
    assert abs(stamp - time.time()) < 60.0
    assert b.check().allowed is False


def test_a_trip_written_before_a_reboot_still_cools_down(store):
    """The failure this exists to prevent: after a reboot `time.monotonic()`
    restarts near zero, so a stamp written by the old process is in the FUTURE
    and `now - trip_at` is a large negative number. Refusing every integration
    until a person runs `hive ci breaker reset` is exactly the outage the
    module's own fail-open rule forbids."""
    b = brk.IntegrationBreaker(project="p", clock=lambda: 900_000.0)
    for _ in range(3):
        b.record_failure("boom")
    assert b.state().state == models.OPEN
    # A second process, on a machine that has just rebooted.
    after = brk.IntegrationBreaker(project="p", clock=lambda: 42.0)
    v = after.check()
    assert v.allowed is True, v.reason
    assert v.state == models.HALF_OPEN


def test_a_stamp_from_the_future_is_not_a_recent_trip(store):
    """The same backwards jump, on the quarantine window. Kept stale trips
    would quarantine the circuit on its very next trip -- `now - ts` is
    negative, which is <= the window, so nothing ever ages out."""
    b = brk.IntegrationBreaker(project="p", clock=lambda: 100.0)
    assert b._recent_trips(100.0) == ()
    b._state = brk.CircuitBreakerState(
        state=models.CLOSED,
        trips_in_window=("t900000.000000", "t50.000000"))
    assert b._recent_trips(100.0) == ("t50.000000",)


def test_the_cooldown_boundary_is_unchanged_by_the_backwards_guard(store):
    """CANARY. The guard must only fire for a stamp in the future: a normal
    OPEN breaker one second short of its cooldown still refuses."""
    now = {"t": 1_000_000.0}
    b = brk.IntegrationBreaker(project="p", clock=lambda: now["t"])
    for _ in range(3):
        b.record_failure("boom")
    now["t"] += brk.IntegrationBreaker.COOLDOWN_PERIOD_S - 1.0
    assert b.check().allowed is False
    now["t"] += 2.0
    assert b.check().allowed is True


# --- the sidecar is shared, so every mutation is a read-modify-write ---------------

def test_a_concurrent_success_cannot_erase_another_runs_trip(store):
    """Two missions integrating at once share ONE `integration_breaker.json`.
    Each `IntegrationBreaker` loads a snapshot when it is built and writes the
    whole record back on every mutation, so the second writer publishes a state
    that no longer exists: here a clean merge erases a trip made moments
    earlier, taking the cooldown and the quarantine window with it. The circuit
    then admits the very merges it had just decided to stop."""
    stale = brk.IntegrationBreaker(project="p", clock=lambda: 1000.0)
    other = brk.IntegrationBreaker(project="p", clock=lambda: 1000.0)
    for _ in range(3):
        other.record_failure("boom")
    assert other.state().state == models.OPEN

    stale.record_success()          # holding the CLOSED snapshot from before
    assert brk.IntegrationBreaker(project="p").state().state == models.OPEN


def test_a_concurrent_failure_cannot_erase_another_runs_failure_count(store):
    """The same race one step earlier: two runs each report their first
    failure, and the count must reach two rather than staying at one."""
    a = brk.IntegrationBreaker(project="p", clock=lambda: 1000.0)
    b = brk.IntegrationBreaker(project="p", clock=lambda: 1000.0)
    a.record_failure("one")
    b.record_failure("two")
    assert brk.IntegrationBreaker(project="p").state().failure_count == 2


def test_the_sidecar_lock_lives_beside_it_and_is_released(store):
    """Under `paths.state_dir`, like every other sidecar in the store -- never
    at the store root -- and gone once the mutation is done."""
    b = brk.IntegrationBreaker(project="p", clock=lambda: 1000.0)
    b.record_failure("boom")
    lock = paths.state_dir("p") / "integration_breaker.lock"
    assert not lock.exists(), "the lock was left behind"
    assert (paths.state_dir("p") / brk.STATE_FILENAME).exists()


def test_a_lock_that_cannot_be_taken_does_not_become_the_outage(store,
                                                                monkeypatch):
    """FAIL-OPEN, like every other total in this module. A breaker that cannot
    take its own lock must still record the failure: the circuit exists to
    prevent an outage, not to become one."""
    from mindhive.core import locks

    monkeypatch.setattr(locks, "acquire", lambda *a, **k: None)
    b = brk.IntegrationBreaker(project="p", clock=lambda: 1000.0)
    b.record_failure("boom")
    assert brk.IntegrationBreaker(project="p").state().failure_count == 1
