# tests/test_tier2_stability.py
"""tests/test_tier2_stability.py -- Deterministic Verification for Tier 2
Stability (blueprint TDD-02, landed with P4).

## What a Tier-2 invariant is

A Tier-1 invariant (`tests/test_tier1_invariants.py`) is a property of the
FOUNDATION. A Tier-2 invariant is a property of the NUMBERS the system steers
by -- the weight a lane is packed by, the score a skill is promoted on, the
patience of a lock, the thresholds of a circuit -- and of the shapes those
numbers come in. None of them is derived from data at runtime; each is a
decision (K1-K4 in the plan) that a later refactor would most plausibly
"tidy" into something else. This file is where such a tidying trips.

Every test here is deterministic and isolated: no network, no clock, no git,
a throwaway store per test (`tests/conftest.py`'s autouse `isolate_store`).

## The four families

* **Weights (K3).** `payoff.compute_calibrated_weight(cc, churn)` is the MEB's
  `clamp(1, 50, 1 + 0.65 ln(1 + cc) + 0.45 ln(1 + churn))`: `(0, 0)` is
  exactly `1.0`, each term is strictly monotone, the K3 grid (cc in [1, 500],
  churn in [0, 300]) has a max/min ratio of about 5.2 against the 18.4 the
  decision allows, and `metrics.calculate_workload_weight` uses it by default
  (`DEFAULT_WEIGHT_MODEL`, env `MINDHIVE_WEIGHT_MODEL`) with the original
  three-term sum still there as `model="linear"`. A file with nothing to
  weigh is 0 under BOTH models -- the metrics contract, not the formula's
  floor.
* **Promotion (K2).** `recommend.should_promote_skill` is the MEB's
  three-way rule over `core.recommend`: PROBATION below four calls (the RP's
  INSUFFICIENT does not exist), QUALIFIED at score >= 0.80 with four calls
  and no open incident, DISCONTINUED on a three-streak or a score below 0.50
  after five calls -- and it is TOTAL over the contradictory rows a folded
  telemetry can carry. `MEASURED_MIN_CALLS` (3, the provenance label) and
  `QUALIFIED_MIN_CALLS` (4, the promotion) are two bars and stay two.
* **Locks (K4).** `tasktool.task_lock`'s five verbatim constants and
  `core.locks.DEFAULT_POLICY` are pinned APART: one file, one policy.
* **Breakers (K1).** The integration circuit runs on MEB 2.2's numbers, the
  tool breaker on `resilience`'s own, and BAB 3's DTMC keeps the PDF's.
"""
import functools
import math

import pytest

from mindhive import models, recommend, tasktool
from mindhive.core import locks
from mindhive.core import recommend as core_recommend
from mindhive.orchestrator import breaker, metrics, payoff, resilience

# Three decisions (if / for / if) against none: `metrics.cyclomatic` counts 3
# and 0, and no git repository around `tmp_path` means churn 0 for both.
BRANCHY = ("def f(a, b):\n    if a:\n        return 1\n    for x in b:\n"
           "        if x:\n            return x\n    return 0\n")
FLAT = "X = 1\nY = 2\n"


def _two_files(tmp_path):
    heavy, light = tmp_path / "heavy.py", tmp_path / "light.py"
    heavy.write_text(BRANCHY, encoding="utf-8")
    light.write_text(FLAT, encoding="utf-8")
    return heavy, light


@functools.lru_cache(maxsize=1)
def _k3_grid():
    """Every weight on K3's grid: cc in [1, 500], churn in [0, 300]."""
    return tuple(payoff.compute_calibrated_weight(cc, ch)
                 for cc in range(1, 501) for ch in range(0, 301))


# --- weights (K3): the calibrated weight itself -------------------------------

def test_the_calibrated_weight_constants_are_the_mebs():
    assert (payoff._ALPHA_BASE, payoff._BETA_COMPLEXITY, payoff._BETA_CHURN) == (1.0, 0.65, 0.45)
    assert (payoff.WEIGHT_MIN, payoff.WEIGHT_MAX) == (1.0, 50.0)


def test_a_file_with_nothing_to_weigh_weighs_exactly_one():
    got = payoff.compute_calibrated_weight(0, 0)
    assert got == 1.0 and isinstance(got, float)


@pytest.mark.parametrize("cc, churn", [(3, 5), (1, 0), (0, 300), (500, 300), (12, 7)])
def test_the_weight_is_the_meb_formula(cc, churn):
    want = 1.0 + 0.65 * math.log1p(cc) + 0.45 * math.log1p(churn)
    assert payoff.compute_calibrated_weight(cc, churn) == pytest.approx(want)


def test_the_weight_is_strictly_monotone_in_each_term():
    along_cc = [payoff.compute_calibrated_weight(cc, 7) for cc in (0, 1, 2, 5, 50, 500)]
    along_churn = [payoff.compute_calibrated_weight(7, ch) for ch in (0, 1, 2, 5, 50, 300)]
    assert along_cc == sorted(along_cc) and len(set(along_cc)) == len(along_cc)
    assert along_churn == sorted(along_churn) and len(set(along_churn)) == len(along_churn)


def test_the_weight_stays_inside_the_clamp_and_both_ends_of_it_are_real():
    grid = _k3_grid()
    assert min(grid) >= payoff.WEIGHT_MIN and max(grid) <= payoff.WEIGHT_MAX
    assert payoff.compute_calibrated_weight(0, 0) == payoff.WEIGHT_MIN
    # The ceiling takes counts near 2e19 (both terms) -- unreachable for a
    # repository, kept because the MEB states it. The clamp must really clamp.
    assert payoff.compute_calibrated_weight(10 ** 40, 10 ** 40) == payoff.WEIGHT_MAX


def test_a_count_past_float_range_is_still_a_count_and_hits_the_ceiling():
    """`float(10 ** 400)` raises OverflowError. No numstat total gets there,
    but the contract is a real number for EVERY count, and the honest answer
    for one that large is the ceiling -- not a raise the scheduler above never
    catches (the module doctrine: a weighting function that raises takes the
    mission down)."""
    assert payoff.compute_calibrated_weight(10 ** 400, 0) == payoff.WEIGHT_MAX
    assert payoff.compute_calibrated_weight(0, 10 ** 400) == payoff.WEIGHT_MAX


def test_the_range_ratio_over_the_k3_grid_is_at_most_18_4():
    """K3's compression claim. The bound is 18.4; the formula's real ratio is
    about 5.25 and is pinned too, so a coefficient change is caught rather
    than only a blow-up."""
    grid = _k3_grid()
    ratio = max(grid) / min(grid)
    assert ratio <= 18.4
    assert ratio == pytest.approx(5.246, abs=0.01)


@pytest.mark.parametrize("bad", [(-1, 0), (0, -1), (float("nan"), 0), (0, float("inf")),
                                 ("3", 0), (None, 0), (True, 0)])
def test_a_count_that_is_not_a_count_is_refused(bad):
    """The callers hand in AST decisions and numstat line totals, never any of
    these; one here is a bug upstream, not a term to be zeroed."""
    with pytest.raises(ValueError):
        payoff.compute_calibrated_weight(*bad)


# --- weights (K3): the model switch in metrics --------------------------------

def test_the_default_weight_model_is_log_linear():
    assert metrics.DEFAULT_WEIGHT_MODEL == "log_linear"
    assert metrics.WEIGHT_MODELS == ("log_linear", "linear")
    assert metrics.WEIGHT_MODEL_ENV == "MINDHIVE_WEIGHT_MODEL"


def test_the_weight_record_names_its_model_and_a_bare_record_says_linear():
    assert metrics.Weight().model == "linear"
    assert metrics.Weight().total == 0


def test_the_default_call_fuses_by_the_calibrated_weight(tmp_path, monkeypatch):
    monkeypatch.delenv("MINDHIVE_WEIGHT_MODEL", raising=False)
    heavy, _ = _two_files(tmp_path)
    got = metrics.calculate_workload_weight(heavy, root=tmp_path)
    assert got.model == "log_linear"
    assert (got.complexity, got.churn, got.usage) == (3, 0, 0)
    assert got.total == pytest.approx(payoff.compute_calibrated_weight(3, 0))
    assert got.reason == ""


def test_model_linear_is_the_old_formula_and_still_available(tmp_path):
    heavy, _ = _two_files(tmp_path)
    got = metrics.calculate_workload_weight(heavy, root=tmp_path, model="linear")
    assert got.model == "linear"
    assert got.total == metrics.W_COMPLEXITY * 3 + metrics.W_CHURN * 0 + metrics.W_USAGE * 0


def test_the_environment_picks_the_model_and_an_explicit_argument_beats_it(tmp_path, monkeypatch):
    heavy, _ = _two_files(tmp_path)
    monkeypatch.setenv("MINDHIVE_WEIGHT_MODEL", "linear")
    assert metrics.calculate_workload_weight(heavy, root=tmp_path).model == "linear"
    assert metrics.calculate_workload_weight(heavy, root=tmp_path,
                                             model="log_linear").model == "log_linear"
    monkeypatch.setenv("MINDHIVE_WEIGHT_MODEL", "")            # unset-shaped: the default
    assert metrics.calculate_workload_weight(heavy, root=tmp_path).model == "log_linear"


def test_an_unknown_model_falls_back_to_the_default_and_says_so(tmp_path, monkeypatch):
    """The module's doctrine, not a convenience: a weighting function that
    raises takes a whole mission down to improve an estimate. The Weight
    says which model it really used and where the bad name came from."""
    monkeypatch.delenv("MINDHIVE_WEIGHT_MODEL", raising=False)
    heavy, _ = _two_files(tmp_path)
    got = metrics.calculate_workload_weight(heavy, root=tmp_path, model="quadratic")
    assert got.model == "log_linear"
    assert "quadratic" in got.reason and "log_linear" in got.reason
    assert got.total == pytest.approx(payoff.compute_calibrated_weight(3, 0))
    monkeypatch.setenv("MINDHIVE_WEIGHT_MODEL", "quadratic")
    got = metrics.calculate_workload_weight(heavy, root=tmp_path)
    assert got.model == "log_linear" and "MINDHIVE_WEIGHT_MODEL" in got.reason


def test_a_missing_path_weighs_zero_in_both_models(tmp_path):
    for model in metrics.WEIGHT_MODELS:
        got = metrics.calculate_workload_weight(tmp_path / "nope.py", model=model)
        assert got.total == 0 and got.model == model and "no such file" in got.reason


def test_a_file_with_nothing_to_weigh_is_zero_in_both_models_not_the_floor(tmp_path):
    """`compute_calibrated_weight(0, 0)` is 1.0 -- the formula's floor -- but
    metrics keeps its own contract: every term resolved 0 is a file this could
    not weigh, and it says so with a 0 (`tests/test_orchestrator_metrics.py`
    pins the unparseable case the same way)."""
    _, light = _two_files(tmp_path)
    for model in metrics.WEIGHT_MODELS:
        got = metrics.calculate_workload_weight(light, root=tmp_path, model=model)
        assert got.total == 0 and got.reason == "every term resolved 0", model


def test_heavy_outweighs_light_in_both_models(tmp_path):
    heavy, light = _two_files(tmp_path)
    for model in metrics.WEIGHT_MODELS:
        a = metrics.calculate_workload_weight(heavy, root=tmp_path, model=model)
        b = metrics.calculate_workload_weight(light, root=tmp_path, model=model)
        assert a.total > b.total, model


def test_weigh_files_carries_the_model_through(tmp_path, monkeypatch):
    monkeypatch.delenv("MINDHIVE_WEIGHT_MODEL", raising=False)
    _two_files(tmp_path)
    assert metrics.weigh_files(["heavy.py"], root=tmp_path, model="linear") == {"heavy.py": 9}
    got = metrics.weigh_files(["heavy.py", "light.py"], root=tmp_path)
    assert got["heavy.py"] == pytest.approx(payoff.compute_calibrated_weight(3, 0))
    assert got["light.py"] == 0


# --- promotion (K2): Laplace, the penalty and the three statuses --------------

def test_the_laplace_rate_pins():
    assert core_recommend.bayesian_laplace_rate(1, 1) == pytest.approx(2 / 3, abs=1e-4)   # 0.667
    assert core_recommend.bayesian_laplace_rate(4, 4) == pytest.approx(5 / 6, abs=1e-4)   # 0.833
    assert core_recommend.skill_score(4, 4, 0) == pytest.approx(5 / 6, abs=1e-4)
    assert core_recommend.skill_score(7, 10, 1) == pytest.approx((8 / 12) * math.exp(-0.5), abs=1e-4)


@pytest.mark.parametrize("ok, calls, streak, want", [
    (1, 1, 0, (False, "PROBATION")),
    (2, 2, 0, (False, "PROBATION")),          # the RP's INSUFFICIENT is gone: calls < 4 is PROBATION
    (3, 3, 0, (False, "PROBATION")),          # 0.80 exactly, but three calls
    (4, 4, 0, (True, "QUALIFIED")),
    (7, 10, 3, (False, "DISCONTINUED")),
    (2, 10, 0, (False, "DISCONTINUED")),
    (2, 4, 0, (False, "PROBATION")),          # 0.50 exactly: the floor is inclusive
])
def test_should_promote_skill_answers_the_meb_statuses(ok, calls, streak, want):
    assert recommend.should_promote_skill(ok, calls, streak) == want


def test_an_open_incident_blocks_qualified_only():
    assert recommend.should_promote_skill(4, 4, 0, open_incidents=1) == (False, "PROBATION")
    assert recommend.should_promote_skill(2, 10, 0, open_incidents=1) == (False, "DISCONTINUED")


def test_should_promote_skill_is_total_over_bad_bookkeeping():
    """Telemetry is folded from events by several hands and a row can
    contradict itself. The adapter never raises on the recommendation path:
    a reported streak of three is a fact on its own (DISCONTINUED); any other
    contradiction is the residual status and never a promotion."""
    assert recommend.should_promote_skill(9, 10, 3) == (False, "DISCONTINUED")   # streak > failures
    assert recommend.should_promote_skill(7, 5, 0) == (False, "PROBATION")       # ok > calls
    assert recommend.should_promote_skill(4, 5, 2) == (False, "PROBATION")       # streak > failures
    assert recommend.should_promote_skill(None, "x", -2) == (False, "PROBATION")
    assert recommend.should_promote_skill(True, 4, 0) == (False, "PROBATION")


def test_a_field_that_is_not_a_count_never_promotes_and_never_raises():
    """`None` is an absent field and reads as 0 (a row without `fail_streak`
    is normal). A value that is not a count at all -- a bool, NaN, an
    infinity (which overflows `int()`), a non-numeric string, a negative --
    makes the row incoherent: PROBATION and never `auto`, unless the streak
    alone already says DISCONTINUED. Reading garbage as 0 would promote a row
    whose streak field happens to be broken."""
    assert recommend.should_promote_skill(4, 4, None) == (True, "QUALIFIED")
    for garbage in (float("inf"), float("nan"), "lots", -1, True):
        assert recommend.should_promote_skill(4, 4, garbage) == (False, "PROBATION"), garbage
        assert recommend.should_promote_skill(garbage, 4, 0) == (False, "PROBATION"), garbage
        assert recommend.should_promote_skill(4, garbage, 0) == (False, "PROBATION"), garbage
        assert recommend.should_promote_skill(4, 4, 0, open_incidents=garbage) == (False, "PROBATION"), garbage
    assert recommend.should_promote_skill("lots", 4, 3) == (False, "DISCONTINUED")


def test_score_skill_survives_a_row_whose_streak_is_not_a_count():
    """The same garbage through the scoring path: no raise, no `auto`."""
    skill = {"name": "x", "plugin": "p", "description": "x does one thing"}
    for garbage in (float("inf"), float("nan"), "lots"):
        row = {"ok": 4, "calls": 4, "completion_rate": 1.0, "fail_streak": garbage}
        out = recommend.score_skill(skill, {"x": row})
        assert out["execution_mode"] == "suggest" and out["promotion"] == "PROBATION", garbage
        assert 0.0 <= out["skill_score"] <= 1.0 and out["fail_streak"] == 0


def test_measured_and_qualified_are_two_different_bars():
    """`MEASURED_MIN_CALLS` (3) is the provenance label; `QUALIFIED_MIN_CALLS`
    (4) is the promotion. A skill can be measured and still on probation."""
    assert recommend.MEASURED_MIN_CALLS == 3 and core_recommend.QUALIFIED_MIN_CALLS == 4
    assert recommend.AUTO_MIN_RATE == core_recommend.QUALIFIED_MIN_SCORE == 0.8
    assert recommend.should_promote_skill(3, 3, 0) == (False, "PROBATION")


# --- locks (K4): two policies, pinned apart -----------------------------------

def test_the_two_lock_policies_are_pinned_apart():
    """CONC-01's generic primitive and CONC-02's verbatim task lock share a
    shape and NOT their numbers; a future 'unification' has to move these on
    purpose, and `core/locks.py` says in its header why it must not."""
    assert (tasktool._LOCK_TIMEOUT_SECS, tasktool._LOCK_RETRY_BASE_MS,
            tasktool._LOCK_RETRY_MAX_MS, tasktool._STALE_LOCK_AGE_SECS,
            tasktool._TASK_LOCK_DENIED_SPINS) == (15.0, 0.015, 0.200, 60.0, 20)
    p = locks.DEFAULT_POLICY
    assert (p.timeout_s, p.base_s, p.max_s, p.stale_s, p.denied_spins,
            p.fail_open) == (2.0, 0.010, 0.050, 30.0, 5, False)
    assert issubclass(tasktool.LockTimeoutError, locks.LockTimeoutError)
    assert (tasktool._LOCK_WAIT_S, tasktool._LOCK_DENIED_SPINS) == (2.0, 5)


# --- breakers (K1): three sets of numbers, none shared ------------------------

def test_the_three_breakers_keep_their_own_numbers():
    ib = breaker.IntegrationBreaker
    assert (ib.FAILURE_THRESHOLD, ib.COOLDOWN_PERIOD_S, ib.MAX_TRIPS_IN_WINDOW,
            ib.TRIP_WINDOW_S) == (3, 300.0, 3, 3600.0)
    assert (resilience.DEFAULT_SAME_ERROR, resilience.DEFAULT_NO_PROGRESS,
            resilience.DEFAULT_COOLDOWN_S) == (2, 3, 30.0)
    assert models.BREAKER_PARAMS == {"same_error": 2, "no_progress": 3,
                                     "cooldown_s": 10.0, "max_attempts": 2}
    assert breaker._LOCK_POLICY == locks.LockPolicy(timeout_s=5.0, stale_s=60.0, fail_open=True)
