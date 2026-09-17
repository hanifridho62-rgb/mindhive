# tests/test_models.py
"""BAB 3 of the blueprint (`models.py`): the mathematics behind the resilience
work, as pure functions with the document's own worked examples pinned.

Every number asserted here is one the blueprint states -- `+525%` for the MTBF
gain from λ=5.0 to λ=0.8, `0.50 / 0.67 / 0.833` for the Laplace rate at
(0,0) / (1,1) / (4,4), `661.1x` for the scale distortion `1547.0 / 2.34`, the
`0.04 -> 0.42` degradation step at 50% occupancy, and the sixteen rows of the
systemic risk matrix -- so a formula that drifts from the text fails by the
document's figure, not by a number this file made up.

The module is a leaf: `import mindhive.models` must not import the
orchestrator package (whose `__init__` pulls in the whole mission pipeline).
`spearman_rho` still reuses `payoff._ranks` for tie-averaged ranks, but only
at call time, and the reuse is proved with a spy rather than assumed.
"""
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import models

REPO = Path(__file__).resolve().parents[1]


# --- 3.1 Poisson / MTBF --------------------------------------------------------

def test_poisson_pmf_is_the_textbook_formula():
    lam, t = 5.0, 1.0
    for k in range(0, 12):
        expect = (lam * t) ** k * math.exp(-lam * t) / math.factorial(k)
        assert models.poisson_pmf(lam, t, k) == pytest.approx(expect)


def test_poisson_pmf_matches_values_not_derived_from_its_own_body():
    """The loop above recomputes the implementation; these are tabulated
    Poisson(5) values and the distribution's mean, which no transcription
    error shared by test and code can reproduce."""
    assert models.poisson_pmf(5.0, 1.0, 0) == pytest.approx(0.006737947, abs=1e-9)
    assert models.poisson_pmf(5.0, 1.0, 5) == pytest.approx(0.17546737, abs=1e-8)
    mean = sum(k * models.poisson_pmf(0.8, 1.0, k) for k in range(60))
    assert mean == pytest.approx(0.8, abs=1e-12)


def test_poisson_pmf_sums_to_one_over_k():
    total = sum(models.poisson_pmf(4.5, 1.0, k) for k in range(0, 60))
    assert total == pytest.approx(1.0, abs=1e-9)


def test_at_least_one_failure_is_one_minus_the_zero_term():
    lam = 5.0
    assert models.poisson_failure_probability(lam, 1.0) == pytest.approx(
        1.0 - models.poisson_pmf(lam, 1.0, 0))
    # the unenforced band the text reports: 4.5..6.2 per 100 actions -- at
    # either edge a unit of 100 actions almost surely sees a failure
    lo, hi = models.LAMBDA_UNENFORCED_RANGE
    assert lo == 4.5 and hi == 6.2
    assert models.poisson_failure_probability(lo) > 0.98
    assert models.poisson_failure_probability(hi) > 0.99
    # at the enforced target 0.8 a unit of 100 actions still fails more often
    # than not: 1 - e^(-0.8) = 0.5507. Pinned exactly, so the reading that the
    # target makes a unit "more likely clean" cannot be re-asserted by mistake.
    assert models.LAMBDA_TARGET == 0.8
    assert models.poisson_failure_probability(models.LAMBDA_TARGET) == pytest.approx(
        1.0 - math.exp(-0.8), abs=1e-12)
    assert models.poisson_failure_probability(models.LAMBDA_TARGET) > 0.5


@pytest.mark.parametrize("bad", [(-1.0, 1.0, 0), (1.0, -1.0, 0), (1.0, 1.0, -1),
                                 (1.0, 1.0, 1.5)])
def test_poisson_rejects_a_negative_rate_interval_or_count(bad):
    with pytest.raises(ValueError):
        models.poisson_pmf(*bad)


def test_exponential_inter_arrival_density():
    lam = 2.0
    assert models.exponential_pdf(lam, 0.0) == pytest.approx(lam)
    assert models.exponential_pdf(lam, 1.0) == pytest.approx(lam * math.exp(-lam))
    assert models.exponential_pdf(lam, -0.1) == 0.0     # t >= 0 only


def test_mtbf_is_actions_per_failure():
    assert models.ACTIONS_PER_UNIT == 100
    assert models.mtbf(4, 100) == 25.0
    assert models.mtbf(4) == 25.0                     # one observation unit
    assert models.mtbf(0, 100) is math.inf
    assert models.mtbf_from_rate(0.8) == pytest.approx(1.25)
    assert models.mtbf_from_rate(0.0) is math.inf


def test_mtbf_efficiency_matches_the_documents_525_percent():
    """`Efisiensi (ΔMTBF) = ((1/λ_target) - (1/λ_awal)) / (1/λ_awal) * 100%`
    with λ_awal = 5.0 and λ_target = 0.8 -> +525%."""
    assert models.mtbf_efficiency(5.0, 0.8) == pytest.approx(525.0)
    assert models.mtbf_efficiency(5.0, 5.0) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        models.mtbf_efficiency(0.0, 0.8)


# --- 3.2 DTMC circuit breaker --------------------------------------------------

def test_the_four_states_are_the_documents_in_the_documents_order():
    assert models.BREAKER_STATES == ("CLOSED", "HALF_OPEN", "OPEN", "QUARANTINED")
    assert (models.CLOSED, models.HALF_OPEN, models.OPEN,
            models.QUARANTINED) == models.BREAKER_STATES


@pytest.mark.parametrize("state, event, expect", [
    ("CLOSED", "failure", "CLOSED"),            # below the threshold: 1 - p_f
    ("CLOSED", "success", "CLOSED"),
    ("CLOSED", "failure_threshold", "OPEN"),    # p_f
    ("OPEN", "failure", "OPEN"),                # 1 - p_cd
    ("OPEN", "cooldown_elapsed", "HALF_OPEN"),  # p_cd
    ("HALF_OPEN", "probe_ok", "CLOSED"),        # p_s
    ("HALF_OPEN", "probe_fail", "OPEN"),        # 1 - p_s - p_q
    ("HALF_OPEN", "probe_fail_max_attempts", "QUARANTINED"),   # p_q
    ("QUARANTINED", "failure", "QUARANTINED"),  # 1 - p_rel
    ("QUARANTINED", "probe_ok", "QUARANTINED"),
    ("QUARANTINED", "manual_reset", "CLOSED"),  # p_rel: operator release
    ("QUARANTINED", "rule_resolved", "CLOSED"), # p_rel: `hive rules resolve`
])
def test_circuit_breaker_transition_table(state, event, expect):
    assert models.circuit_breaker_transition(state, event) == expect


def test_half_open_has_no_self_loop_and_open_cannot_jump_to_quarantine():
    """The matrix's HALF_OPEN row is [p_s, 0, 1-p_s-p_q, p_q] and its OPEN row
    [0, p_cd, 1-p_cd, 0]: a probe step always leaves HALF_OPEN, and OPEN only
    ever moves to HALF_OPEN. Both zeros are asserted, not merely absent."""
    with pytest.raises(ValueError, match="HALF_OPEN"):
        models.circuit_breaker_transition("HALF_OPEN", "cooldown_elapsed")
    assert models.circuit_breaker_transition("OPEN", "probe_fail_max_attempts") == "OPEN"
    assert models.circuit_breaker_transition("OPEN", "manual_reset") == "OPEN"


def test_half_open_reads_a_plain_success_or_failure_as_the_probe_outcome():
    """`p_s` is tied to the `on_success` hook in the text, so a success seen
    while HALF_OPEN closes the circuit; a failure re-opens it."""
    assert models.circuit_breaker_transition("HALF_OPEN", "success") == "CLOSED"
    assert models.circuit_breaker_transition("HALF_OPEN", "failure") == "OPEN"
    assert models.circuit_breaker_transition("HALF_OPEN", "probe_fail_max_attempts") == "QUARANTINED"
    with pytest.raises(ValueError):
        models.circuit_breaker_transition("HALF_OPEN", "cooldown_elapsed")


@pytest.mark.parametrize("state, event", [("BROKEN", "failure"), ("CLOSED", "explode"),
                                          ("", "failure"), ("CLOSED", "")])
def test_an_unknown_state_or_event_is_a_valueerror(state, event):
    with pytest.raises(ValueError):
        models.circuit_breaker_transition(state, event)


def test_the_transition_matrix_is_the_documents_rows():
    p_f, p_cd, p_s, p_q, p_rel = 0.2, 0.6, 0.5, 0.1, 0.3
    P = models.breaker_transition_matrix(p_f, p_cd, p_s, p_q, p_rel)
    assert P == [
        [1 - p_f, 0.0, p_f, 0.0],
        [p_s, 0.0, 1 - p_s - p_q, p_q],
        [0.0, p_cd, 1 - p_cd, 0.0],
        [p_rel, 0.0, 0.0, 1 - p_rel],
    ]
    for row in P:
        assert sum(row) == pytest.approx(1.0)


@pytest.mark.parametrize("args", [(1.2, 0.5, 0.5, 0.1, 0.1), (0.2, 0.5, 0.7, 0.4, 0.1),
                                  (-0.1, 0.5, 0.5, 0.1, 0.1)])
def test_the_matrix_refuses_probabilities_that_do_not_form_rows(args):
    with pytest.raises(ValueError):
        models.breaker_transition_matrix(*args)


def test_steady_state_solves_pi_p_equals_pi():
    P = models.breaker_transition_matrix(0.05, 0.8, 0.7, 0.1, 0.5)
    pi = models.steady_state(P)
    assert len(pi) == 4
    assert sum(pi) == pytest.approx(1.0)
    assert all(p >= 0 for p in pi)
    for j in range(4):
        assert sum(pi[i] * P[i][j] for i in range(4)) == pytest.approx(pi[j], abs=1e-9)


def test_steady_state_of_a_never_failing_breaker_is_all_closed():
    pi = models.steady_state(models.breaker_transition_matrix(0.0, 1.0, 1.0, 0.0, 1.0))
    assert pi == pytest.approx([1.0, 0.0, 0.0, 0.0])


def test_steady_state_against_a_closed_form_oracle():
    """`p_f = 0.1`, `p_cd = p_s = 1`, `p_q = 0`: CLOSED -> OPEN -> HALF_OPEN ->
    CLOSED is a cycle whose stationary mass is `[1/(1+2p_f), p_f/(1+2p_f),
    p_f/(1+2p_f), 0] = [5/6, 1/12, 1/12, 0]` by hand, not by the solver."""
    pi = models.steady_state(models.breaker_transition_matrix(0.1, 1.0, 1.0, 0.0, 1.0))
    assert pi == pytest.approx([5 / 6, 1 / 12, 1 / 12, 0.0], abs=1e-12)


def test_steady_state_of_a_reducible_chain_is_the_documented_solution():
    """Not unique, and the docstring says which one comes back: an absorbing
    QUARANTINED (`p_rel = 0`) ends with all the mass there; two absorbing
    states (the identity) put it on the first."""
    absorbing = models.steady_state(models.breaker_transition_matrix(0.1, 1.0, 0.5, 0.5, 0.0))
    assert absorbing == pytest.approx([0.0, 0.0, 0.0, 1.0])
    identity = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
    assert models.steady_state(identity) == pytest.approx([1.0, 0.0, 0.0, 0.0])


def test_lane_availability_is_closed_plus_half_open_against_the_target():
    assert models.LANE_AVAILABILITY_TARGET == 0.992
    pi = [0.9, 0.05, 0.03, 0.02]
    assert models.lane_availability(pi) == pytest.approx(0.95)
    assert models.lane_availability([0.995, 0.0, 0.004, 0.001]) > models.LANE_AVAILABILITY_TARGET
    assert models.lane_availability(pi) < models.LANE_AVAILABILITY_TARGET
    # "tetap berada di atas 99.2%": strictly above, so the boundary itself fails
    assert models.meets_availability_target([0.995, 0.0, 0.004, 0.001]) is True
    assert models.meets_availability_target([0.992, 0.0, 0.008, 0.0]) is False
    assert models.meets_availability_target(pi) is False
    # only a distribution: an entry outside [0, 1] or a sum away from 1 raises
    # (`[2.0, 0, -1.0, 0]` used to report 200% availability)
    with pytest.raises(ValueError):
        models.lane_availability([2.0, 0.0, -1.0, 0.0])
    with pytest.raises(ValueError):
        models.lane_availability([0.5, 0.5, 0.5, 0.0])
    with pytest.raises(ValueError):
        models.lane_availability([0.9, 0.05, 0.03])


def test_the_dtmc_parameters_are_the_ones_the_text_names():
    """`same_error >= 2`, `no_progress >= 3`, `cooldown_s = 10.0`. The first
    two are the live tool-breaker defaults; the cooldown is the DTMC's stated
    figure, and `resilience.DEFAULT_COOLDOWN_S` is a different, larger number
    on purpose (the tool breaker waits longer than the model's probe timer)."""
    from mindhive.orchestrator import resilience
    assert models.BREAKER_PARAMS == {"same_error": 2, "no_progress": 3, "cooldown_s": 10.0,
                                     "max_attempts": 2}
    # `p_q`'s gate: the text names max_attempts without valuing it; the value
    # is the live probe limit.
    assert models.BREAKER_PARAMS["max_attempts"] == resilience.DEFAULT_MAX_ATTEMPTS
    assert models.BREAKER_PARAMS["same_error"] == resilience.DEFAULT_SAME_ERROR
    assert models.BREAKER_PARAMS["no_progress"] == resilience.DEFAULT_NO_PROGRESS
    # The live tool-breaker wait is 30.0 s (`resilience.DEFAULT_COOLDOWN_S`);
    # the DTMC's probe timer is 10.0 s. Both values pinned, so neither can
    # drift toward the other -- or anywhere else -- unnoticed.
    assert resilience.DEFAULT_COOLDOWN_S == 30.0
    assert models.BREAKER_PARAMS["cooldown_s"] != resilience.DEFAULT_COOLDOWN_S


def test_the_transition_table_lists_exactly_the_matrix_off_diagonal_entries():
    """Seven `(state, event) -> state` rows: the non-zero, non-diagonal
    entries of the 4x4 matrix, plus the two operator/rule exits from
    QUARANTINED that share `p_rel`. And the event vocabulary is closed."""
    C, H, O, Q = models.CLOSED, models.HALF_OPEN, models.OPEN, models.QUARANTINED
    assert models.BREAKER_EVENTS == (
        "success", "failure", "failure_threshold", "cooldown_elapsed",
        "probe_ok", "probe_fail", "probe_fail_max_attempts",
        "manual_reset", "rule_resolved")
    assert models.BREAKER_TRANSITIONS == {
        (C, "failure_threshold"): O,
        (O, "cooldown_elapsed"): H,
        (H, "probe_ok"): C,
        (H, "probe_fail"): O,
        (H, "probe_fail_max_attempts"): Q,
        (Q, "manual_reset"): C,
        (Q, "rule_resolved"): C,
    }
    for (state, event), target in models.BREAKER_TRANSITIONS.items():
        assert event in models.BREAKER_EVENTS
        assert models.circuit_breaker_transition(state, event) == target


# --- 3.3 Spearman ---------------------------------------------------------------

def test_spearman_rho_on_aligned_and_reversed_ranks():
    assert models.spearman_rho([1, 2, 3], [1, 2, 3]) == 1.0
    assert models.spearman_rho([1, 2, 3], [3, 2, 1]) == -1.0
    assert models.spearman_rho([1, 2, 3, 4], [10, 30, 20, 40]) == pytest.approx(0.8)


def test_spearman_needs_two_samples_and_two_is_trivially_aligned():
    """The text's own caveat: at n = 2 any non-inverted pair gives |ρ| = 1.00."""
    assert models.spearman_rho([1], [1]) is None
    assert models.spearman_rho([], []) is None
    assert models.spearman_rho([1, 1547], [198.60, 465.48]) == 1.0
    with pytest.raises(ValueError):
        models.spearman_rho([1, 2], [1])


def test_spearman_reuses_payoffs_tie_averaged_ranks(monkeypatch):
    from mindhive.orchestrator import payoff
    calls = []
    real = payoff._ranks

    def spy(values):
        calls.append(list(values))
        return real(values)

    monkeypatch.setattr(payoff, "_ranks", spy)
    rho = models.spearman_rho([2, 2, 5], [1, 1, 9])
    assert calls == [[2, 2, 5], [1, 1, 9]]
    assert rho == 1.0


def test_scale_distortion_is_the_documents_661x():
    assert models.scale_distortion(1547.0, 2.34) == pytest.approx(661.1, abs=0.05)
    assert models.scale_distortion(1547 / 1, 465.48 / 198.60) == pytest.approx(660.0, abs=0.5)
    with pytest.raises(ValueError):
        models.scale_distortion(1.0, 0.0)


def test_log_linear_estimate_damps_the_marginal_value_of_complexity():
    """`ln(T_est) = α + β1·ln(Complexity) + β2·ln(Churn) + ε`."""
    alpha, b1, b2 = 1.0, 0.65, 0.45
    t = models.log_linear_estimate(20, 5, alpha, b1, b2)
    assert t == pytest.approx(math.exp(alpha + b1 * math.log(20) + b2 * math.log(5)))
    # doubling complexity multiplies the estimate by 2**β1, not by 2
    assert models.log_linear_estimate(40, 5, alpha, b1, b2) / t == pytest.approx(2 ** b1)
    # the bare formula has no clamp: ln(0) is undefined, so 0 raises ...
    with pytest.raises(ValueError):
        models.log_linear_estimate(0, 0, alpha, b1, b2)
    # ... and inputs in (0, 1) are computed, not rounded up (exp(ln 0.5) = 0.5)
    assert models.log_linear_estimate(0.5, 1, 0.0, 1.0, 0.0) == pytest.approx(0.5)
    # `floor=1.0` is the caller's guard: zero churn estimates as exp(α)
    assert models.log_linear_estimate(0, 0, alpha, b1, b2, floor=1.0) == pytest.approx(
        math.exp(alpha))
    assert models.log_linear_estimate(1, 1, alpha, b1, b2, epsilon=0.5) == pytest.approx(
        math.exp(alpha + 0.5))


# --- 3.4 Context depletion --------------------------------------------------------

def test_context_size_is_the_additive_accumulation():
    """`C(m) = C0 + Σ (ΔU_i + ΔA_i + ΔT_i)`."""
    assert models.context_size(5000, []) == 5000
    assert models.context_size(5000, [(100, 200, 3000), (50, 150, 0)]) == 8500
    assert models.CONTEXT_LIMIT_TOKENS == 200_000
    assert models.C0_RANGE == (4_500, 8_000)


def test_context_depletion_counts_whole_turns_remaining():
    assert models.context_depletion(80_000, 100_000, 2_500) == 8
    assert models.context_depletion(99_000, 100_000, 2_500) == 0
    assert models.context_depletion(120_000, 100_000, 2_500) == 0     # over budget
    with pytest.raises(ValueError):
        models.context_depletion(0, 100_000, 0)


def test_occupancy_and_the_degradation_step_at_fifty_percent():
    """The text: "pada C(m) > 0.5 * C_limit (okupansi 50% ...) ... meningkat
    tajam dari P = 0.04 menjadi P = 0.42" -- a STRICT inequality, so exactly
    50% is still 0.04 and rotation fires AT 50% to stay out of that half."""
    assert models.ROTATION_RATIO == 0.5
    # The live counterpart: `MINDHIVE_ROTATION_RATIO`'s default is
    # `LoopConfig.smart_zone_ratio` (hook.py falls back to it).
    from mindhive.loop.config import LoopConfig
    assert models.ROTATION_RATIO == LoopConfig().smart_zone_ratio == 0.5
    assert models.ATTENTION_DEGRADATION_P == (0.04, 0.42)
    assert models.occupancy(100_000, 200_000) == pytest.approx(0.5)
    assert models.attention_degradation_probability(0.49) == 0.04
    assert models.attention_degradation_probability(0.5) == 0.04
    assert models.attention_degradation_probability(0.5000001) == 0.42
    assert models.attention_degradation_probability(0.9) == 0.42
    assert models.should_rotate(99_999, 200_000) is False
    assert models.should_rotate(100_000, 200_000) is True
    with pytest.raises(ValueError):
        models.occupancy(1, 0)


def test_truncation_probability_is_one_minus_the_product_of_survivals():
    """`P(C(m) >= C_limit) = 1 - Π (1 - P(ΔT_i >= C_limit - C(i-1)))`."""
    assert models.truncation_probability([]) == 0.0
    assert models.truncation_probability([0.1, 0.2]) == pytest.approx(1 - 0.9 * 0.8)
    assert models.truncation_probability([0.0, 1.0]) == 1.0
    with pytest.raises(ValueError):
        models.truncation_probability([1.5])


# --- 3.5 Bayesian skill routing ---------------------------------------------------

def test_laplace_rate_reproduces_the_documents_three_cases():
    assert (models.LAPLACE_ALPHA, models.LAPLACE_BETA) == (1.0, 1.0)
    assert models.bayesian_laplace_rate(0, 0) == pytest.approx(0.5)
    assert models.bayesian_laplace_rate(1, 1) == pytest.approx(0.667, abs=5e-4)
    assert models.bayesian_laplace_rate(4, 4) == pytest.approx(0.833, abs=5e-4)
    with pytest.raises(ValueError):
        models.bayesian_laplace_rate(3, 2)
    with pytest.raises(ValueError):
        models.bayesian_laplace_rate(-1, 2)
    # no prior and no observations: refused, not a ZeroDivisionError
    with pytest.raises(ValueError):
        models.bayesian_laplace_rate(0, 0, alpha=0.0, beta=0.0)
    with pytest.raises(ValueError):
        models.bayesian_laplace_rate(1, 2, alpha=-1.0)


def test_laplace_qualification_needs_the_rate_and_the_calls():
    """`AUTO_MIN_RATE = 0.75` and `MEASURED_MIN_CALLS >= 3`: (1,1) is 0.67 and
    does not qualify; (4,4) is 0.833 over three calls and does."""
    from mindhive import recommend
    assert models.AUTO_MIN_RATE == 0.75
    # The live recommender (`recommend.score_skill`) uses a plain ok/calls
    # rate at 0.8 and contains no Laplace code; the text's 0.75 is the BAB 3
    # model's threshold and has no production caller yet. Pinned so the
    # divergence is visible, the way `cooldown_s` is pinned above.
    assert recommend.AUTO_MIN_RATE == 0.8
    assert models.AUTO_MIN_RATE != recommend.AUTO_MIN_RATE
    assert models.MEASURED_MIN_CALLS == 3 == recommend.MEASURED_MIN_CALLS
    assert models.laplace_qualifies(1, 1) is False
    assert models.laplace_qualifies(4, 4) is True
    assert models.laplace_qualifies(2, 2) is False      # 0.75 but under three calls
    assert models.laplace_qualifies(5, 8) is False      # enough calls, 0.6 rate
    # both boundaries are inclusive: rate == AUTO_MIN_RATE, calls == MEASURED_MIN_CALLS
    assert models.bayesian_laplace_rate(5, 6) == pytest.approx(0.75)
    assert models.laplace_qualifies(5, 6) is True       # rate exactly 0.75
    assert models.laplace_qualifies(3, 3) is True       # exactly three calls


def test_streak_penalty_is_gamma_times_one_minus_exp_minus_half_streak():
    assert models.streak_penalty(0) == 0.0
    assert models.streak_penalty(2) == pytest.approx(1 - math.exp(-1.0))
    assert models.streak_penalty(3, gamma=0.5) == pytest.approx(0.5 * (1 - math.exp(-1.5)))
    assert models.streak_penalty(1) < models.streak_penalty(2) < models.streak_penalty(3) < 1.0
    assert models.STREAK_DISCONTINUE == 3
    with pytest.raises(ValueError):
        models.streak_penalty(-1)


def test_discontinue_needs_three_failures_and_an_active_rule():
    """Both conjuncts of the text's sentence: `s_f >= 3` AND an active
    incident-prevention rule; the live recommender uses the same figure."""
    from mindhive import recommend
    assert models.STREAK_DISCONTINUE == recommend.DISCONTINUE_STREAK
    assert models.should_discontinue(3, True) is True
    assert models.should_discontinue(7, True) is True
    assert models.should_discontinue(3, False) is False
    assert models.should_discontinue(2, True) is False
    assert models.should_discontinue(0, True) is False
    with pytest.raises(ValueError):
        models.should_discontinue(-1, True)


# --- 3.6 Systemic risk matrix -----------------------------------------------------

def test_risk_score_is_probability_times_impact():
    assert models.risk_score(0.986, 3) == round(2.958, 3)
    assert models.risk_score(1.0, 5) == 5.0
    assert models.risk_score(0.7, 5) == 3.5
    for bad in [(1.1, 3), (-0.1, 3), (0.5, 0), (0.5, 6)]:
        with pytest.raises(ValueError):
            models.risk_score(*bad)


def test_priority_follows_the_impact_band():
    assert [models.classify_priority(i) for i in (5, 4, 3, 2, 1)] == ["P0", "P1", "P1", "P2", "P3"]
    with pytest.raises(ValueError):
        models.classify_priority(0)


def test_the_risk_matrix_holds_the_sixteen_audited_problems():
    rows = models.RISK_MATRIX
    assert len(rows) == 16
    ids = [r.id for r in rows]
    assert len(set(ids)) == 16
    assert set(ids) == {f"M-{n:02d}" for n in range(1, 17)}
    assert all(isinstance(r, models.RiskRow) for r in rows)
    by_id = {r.id: r for r in rows}
    assert by_id["M-01"].component.startswith("Hardcoded Paths")
    assert by_id["M-01"].module == "hooks/*.ps1"
    assert (by_id["M-01"].probability, by_id["M-01"].impact, by_id["M-01"].score) == (1.0, 5, 5.0)
    assert by_id["M-03"].score == 3.5 and by_id["M-03"].module == "advice.py"
    assert by_id["M-07"].score == 1.75 and by_id["M-07"].priority == "P0"
    assert by_id["M-04"].score == 3.4 and by_id["M-04"].priority == "P1"
    assert by_id["M-05"].module == "__main__.py" and by_id["M-05"].priority == "P2"
    assert by_id["M-12"].score == 1.0 and by_id["M-12"].priority == "P3"
    assert by_id["M-14"].module == "LICENSE" and by_id["M-14"].priority == "P3"


# (id, component, module, P, P label, I, I label, R, priority, classification),
# transcribed from the matrix on pp. 28-31 in the document's row order.
EXPECTED_ROWS = [
    ("M-01", "Hardcoded Paths di Hooks", "hooks/*.ps1", 1.00, "Pasti", 5,
     "Kritis / Outage", 5.00, "P0", "Pemblokir Fondasi Mutlak"),
    ("M-03", "Prompt Injection via Ledger SOP", "advice.py", 0.70, "Sering", 5,
     "Kritis / Hijack", 3.50, "P0", "Celah Integritas Kognitif"),
    ("M-07", "Locking Race on Delete-Pending", "tasktool.py", 0.35, "Sedang", 5,
     "Kritis / Data Loss", 1.75, "P0", "Kerusakan Integritas Mutasi"),
    ("M-04", "Vacuous Pass di SuiteGate", "gate.py", 0.85, "Sangat Sering", 4,
     "Tinggi / False Green", 3.40, "P1", "Kelemahan Verifikasi Mutu"),
    ("M-02", "Path Traversal via session_id", "events.py", 0.45, "Sedang", 4,
     "Tinggi / File Leak", 1.80, "P1", "Kerentanan File I/O"),
    ("M-16", "Orphan Grandchildren Processes", "verify.py", 0.70, "Sering", 4,
     "Tinggi / OOM Crash", 2.80, "P1", "Kebocoran Resource OS"),
    ("M-06", "Kalibrasi Skala Bobot Tugas", "payoff.py", 1.00, "Pasti", 3,
     "Sedang / Suboptimal", 3.00, "P1", "Distorsi Penjadwalan Kerja"),
    ("M-08", "Cache Plugin Skill Usang", ".claude-plugin/", 1.00, "Pasti", 3,
     "Sedang / Logic Drift", 3.00, "P1", "Divergensi Konteks Model"),
    ("M-05", "Utang Teknis Monolitik", "__main__.py", 1.00, "Pasti", 2,
     "Rendah-Sedang / Debt", 2.00, "P2", "Pemeliharaan & Higienitas"),
    ("M-09", "Fitur Fabricate Phantom", "fabricate.py", 1.00, "Pasti", 2,
     "Rendah / Dead Code", 2.00, "P2", "Rekonsiliasi Branch Git"),
    ("M-10", "Ketergantungan Utilitas jq", "autoplan", 0.95, "Sangat Sering", 2,
     "Rendah / Missing CLI", 1.90, "P2", "Portabilitas Subproses"),
    ("M-11", "TUI Concurrency Load Glitch", "dashboard.py", 0.75, "Tinggi", 2,
     "Rendah / UI Glitch", 1.50, "P2", "Observabilitas Terminal"),
    ("M-13", "Stale Render Clobbering", "dashboard_page.py", 0.80, "Tinggi", 2,
     "Rendah / UI Flicker", 1.60, "P2", "Sinkronisasi Frontend"),
    ("M-15", "Disfungsi Validasi Empiris A/B", "ab.py", 1.00, "Pasti", 2,
     "Rendah / Unchecked", 2.00, "P2", "Bukti Kinerja Arsitektur"),
    ("M-12", "Rasio Kontras WCAG AA", "dashboard_css.py", 1.00, "Pasti", 1,
     "Rendah / Kosmetik", 1.00, "P3", "Aksesibilitas Desain"),
    ("M-14", "Kekosongan Lisensi Resmi", "LICENSE", 1.00, "Pasti", 1,
     "Legal Compliance", 1.00, "P3", "Perlindungan Hukum Open Source"),
]


def test_every_risk_row_is_the_documents_row_verbatim_and_in_its_order():
    assert len(EXPECTED_ROWS) == 16
    assert [tuple(r) for r in models.RISK_MATRIX] == EXPECTED_ROWS


@pytest.mark.parametrize("expected", EXPECTED_ROWS, ids=[r[0] for r in EXPECTED_ROWS])
def test_risk_row_fields_by_name(expected):
    row = {r.id: r for r in models.RISK_MATRIX}[expected[0]]
    (id_, component, module, p, p_label, impact, i_label, score, priority, cls) = expected
    assert (row.component, row.module) == (component, module)
    assert (row.probability, row.probability_label) == (p, p_label)
    assert (row.impact, row.impact_label) == (impact, i_label)
    assert (row.score, row.priority, row.classification) == (score, priority, cls)
    assert row.score == round(p * impact, 3)


def test_prioritised_risks_orders_by_priority_then_score():
    ordered = models.prioritised_risks()
    assert [r.id for r in ordered[:3]] == ["M-01", "M-03", "M-07"]
    # the whole order, ties broken by id (M-06 before M-08 at 3.00; M-05,
    # M-09, M-15 at 2.00), so a reordering of equal scores is not silent
    assert [r.id for r in ordered] == [
        "M-01", "M-03", "M-07", "M-04", "M-06", "M-08", "M-16", "M-02",
        "M-05", "M-09", "M-15", "M-10", "M-13", "M-11", "M-12", "M-14"]
    assert [r.priority for r in ordered] == sorted(r.priority for r in ordered)
    for a, b in zip(ordered, ordered[1:]):
        if a.priority == b.priority:
            assert a.score >= b.score


# --- NaN never passes a comparison, so it is refused at the door ----------------------

NAN = float("nan")
INF = float("inf")


@pytest.mark.parametrize("call", [
    lambda: models.poisson_pmf(NAN, 1.0, 0),
    lambda: models.poisson_pmf(1.0, NAN, 0),
    lambda: models.poisson_failure_probability(NAN),
    lambda: models.exponential_pdf(NAN, 1.0),
    lambda: models.mtbf_from_rate(NAN),
    lambda: models.mtbf_efficiency(NAN, 0.8),
    lambda: models.breaker_transition_matrix(NAN, 1.0, 1.0, 0.0, 1.0),
    lambda: models.lane_availability([NAN, 0.0, 0.0, 0.0]),
    lambda: models.spearman_rho([1, NAN, 3], [1, 2, 3]),
    lambda: models.scale_distortion(NAN, 1.0),
    lambda: models.log_linear_estimate(NAN, 1.0, 1.0, 1.0, 1.0),
    lambda: models.attention_degradation_probability(NAN),
    lambda: models.should_rotate(1, 100, ratio=NAN),
    lambda: models.truncation_probability([NAN]),
    lambda: models.bayesian_laplace_rate(NAN, 2),
    lambda: models.streak_penalty(NAN),
    lambda: models.should_discontinue(NAN, True),
    lambda: models.poisson_pmf(INF, 1.0, 0),
    lambda: models.streak_penalty(-INF),
], ids=[
    "pmf-lam", "pmf-t", "failure-prob", "exp-pdf", "mtbf-from-rate", "mtbf-eff",
    "matrix", "availability", "spearman", "distortion", "log-linear", "degradation",
    "should-rotate", "truncation", "laplace", "streak", "discontinue", "pmf-inf",
    "streak-neg-inf",
])
def test_a_nan_or_infinite_input_is_refused_not_propagated(call):
    """`nan < 0` and `nan > calls` are both False, so before the finiteness
    gate every one of these returned NaN (or, worse, a plausible number:
    `spearman_rho([1, nan, 3], [1, 2, 3])` was 1.0 and
    `attention_degradation_probability(nan)` was 0.04)."""
    with pytest.raises(ValueError):
        call()


# --- the module is a leaf ------------------------------------------------------------

def test_importing_models_does_not_import_the_orchestrator_package():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-W", "error", "-c",
         "import sys, mindhive.models; "
         "print('mindhive.orchestrator' in sys.modules, "
         "'mindhive.orchestrator.payoff' in sys.modules)"],
        capture_output=True, text=True, env=env, cwd=str(REPO))
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False", "False"]


# =====================================================================================
# MEB section 2 (blueprint P1a'): the MASTER ENGINEERING BLUEPRINT's audit numbers
# =====================================================================================

# --- MEB 2.1 Poisson baseline vs. mitigated -----------------------------------------

def test_meb_rates_mtbf_and_the_two_session_reliabilities():
    assert (models.LAMBDA_BASELINE, models.LAMBDA_MITIGATED, models.SESSION_ACTIONS) == (0.05, 0.008, 100)
    assert models.mtbf_from_rate(models.LAMBDA_BASELINE) == pytest.approx(20.0)
    assert models.mtbf_from_rate(models.LAMBDA_MITIGATED) == pytest.approx(125.0)
    r0 = models.poisson_reliability(models.LAMBDA_BASELINE, 100)
    r1 = models.poisson_reliability(models.LAMBDA_MITIGATED, 100)
    assert r0 == pytest.approx(0.006737947, abs=1e-9)          # 0.6738 %
    assert r1 == pytest.approx(0.449328964, abs=1e-9)          # 44.9329 %
    assert r0 * 100 == pytest.approx(0.6738, abs=1e-4)
    assert r1 * 100 == pytest.approx(44.9329, abs=1e-4)
    # R(t) is the k = 0 Poisson term, not a second formula
    assert r0 == pytest.approx(models.poisson_pmf(models.LAMBDA_BASELINE, 100, 0))
    assert models.poisson_reliability(0.0, 100) == 1.0 and models.poisson_reliability(0.05, 0) == 1.0


def test_meb_reliability_gain_is_e_to_the_four_point_two():
    gain = models.reliability_gain(0.05, 0.008, 100)
    assert gain == pytest.approx(math.exp(4.2))
    assert gain == pytest.approx(66.686, abs=1e-3)
    assert gain == pytest.approx(models.poisson_reliability(0.008, 100)
                                 / models.poisson_reliability(0.05, 100))
    assert models.reliability_gain(0.05, 0.05, 100) == 1.0


def test_meb_session_failure_reduction_is_the_relative_change_of_one_minus_r():
    red = models.session_failure_reduction(0.05, 0.008, 100)
    f0, f1 = 1 - math.exp(-5.0), 1 - math.exp(-0.8)
    assert red == pytest.approx((f1 - f0) / f0)
    assert red * 100 == pytest.approx(-44.56, abs=0.01)
    with pytest.raises(ValueError):
        models.session_failure_reduction(0.0, 0.008, 100)      # no baseline failure to reduce
    with pytest.raises(ValueError):
        models.session_failure_reduction(0.05, 0.008, 0)


@pytest.mark.parametrize("row", models.RELIABILITY_SENSITIVITY, ids=lambda r: f"t={r.actions}")
def test_meb_sensitivity_table_recomputes_from_the_two_rates(row):
    """Every printed cell within 0.02 of the recomputation (the MEB rounds its
    intermediates: -80.45 printed against -80.457 recomputed at t = 10)."""
    r0 = models.poisson_reliability(models.LAMBDA_BASELINE, row.actions) * 100
    r1 = models.poisson_reliability(models.LAMBDA_MITIGATED, row.actions) * 100
    assert r0 == pytest.approx(row.baseline_pct, abs=0.02)
    assert r1 == pytest.approx(row.mitigated_pct, abs=0.02)
    if row.baseline_pct < 0.1:                                  # 0.055 % and 0.0045 %: relative too
        assert r0 == pytest.approx(row.baseline_pct, rel=0.02)
    gain = models.reliability_gain(models.LAMBDA_BASELINE, models.LAMBDA_MITIGATED, row.actions)
    assert gain == pytest.approx(row.gain, rel=1e-3, abs=0.006)   # two printed decimals
    red = models.session_failure_reduction(models.LAMBDA_BASELINE, models.LAMBDA_MITIGATED,
                                           row.actions) * 100
    assert red == pytest.approx(row.failure_reduction_pct, abs=0.02)


def test_meb_sensitivity_table_is_the_documents_seven_rows_verbatim():
    assert [r.actions for r in models.RELIABILITY_SENSITIVITY] == [10, 25, 50, 75, 100, 150, 200]
    assert models.RELIABILITY_SENSITIVITY[4] == (100, 0.67, 44.93, 66.69, -44.56)
    assert models.RELIABILITY_SENSITIVITY[-1] == (200, 0.0045, 20.19, 4447.07, -20.18)


# --- MEB 2.2 the integration breaker's DTMC -----------------------------------------

def test_meb_integration_breaker_matrix_rows_are_the_documents():
    p_f, p_s, p_rec, p_q, p_reset = 0.12, 0.50, 0.30, 0.10, 0.05
    m = models.integration_breaker_matrix(p_f, p_s, p_rec, p_q, p_reset)
    assert m == [[1 - p_f, 0.0, p_f, 0.0],
                 [p_s, 0.0, 1 - p_s, 0.0],
                 [0.0, p_rec, 1 - p_rec - p_q, p_q],
                 [p_reset, 0.0, 0.0, 1 - p_reset]]
    assert all(sum(row) == pytest.approx(1.0) for row in m)
    # QUARANTINED is entered from OPEN here (MEB) and from HALF_OPEN in BAB 3
    bab3 = models.breaker_transition_matrix(p_f, p_rec, p_s, p_q, p_reset)
    assert m[2][3] == p_q and bab3[2][3] == 0.0
    assert m[1][3] == 0.0 and bab3[1][3] == p_q
    assert models.INTEGRATION_BREAKER_PARAMS == {"failure_threshold": 3, "cooldown_period_s": 300.0}
    assert models.BREAKER_PARAMS["cooldown_s"] == 10.0            # the TOOL breaker keeps BAB 3's
    with pytest.raises(ValueError):
        models.integration_breaker_matrix(0.1, 0.5, 0.7, 0.4, 0.1)   # p_rec + p_q > 1
    with pytest.raises(ValueError):
        models.integration_breaker_matrix(1.2, 0.5, 0.3, 0.1, 0.1)


@pytest.mark.parametrize("params", [
    (0.12, 0.50, 0.30, 0.10, 0.05), (0.02, 0.85, 0.50, 0.02, 0.20),
    (0.5, 0.5, 0.5, 0.25, 0.5), (0.01, 0.99, 0.9, 0.05, 0.01), (1.0, 0.0, 0.1, 0.9, 1.0)])
def test_meb_closed_form_stationary_agrees_with_the_solver(params):
    closed = models.integration_breaker_stationary(*params)
    solved = models.stationary_distribution(models.integration_breaker_matrix(*params))
    assert closed == pytest.approx(solved, abs=1e-9)
    assert sum(closed) == pytest.approx(1.0)
    # π·P = π for the closed form too
    m = models.integration_breaker_matrix(*params)
    again = [sum(closed[i] * m[i][j] for i in range(4)) for j in range(4)]
    assert again == pytest.approx(closed, abs=1e-12)


def test_meb_names_are_bab3s_objects_where_the_formula_is_the_same():
    assert models.stationary_distribution is models.steady_state
    assert models.availability is models.lane_availability
    pi = [0.7, 0.1, 0.15, 0.05]
    assert models.downtime(pi) == pytest.approx(0.2)
    assert models.availability(pi) + models.downtime(pi) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        models.integration_breaker_stationary(0.1, 0.0, 0.5, 0.0, 0.1)   # p_s·p_rec + p_q == 0
    with pytest.raises(ValueError):
        models.integration_breaker_stationary(0.1, 0.5, 0.5, 0.1, 0.0)   # absorbing QUARANTINED


def test_meb_calibration_what_the_stated_parameters_actually_give():
    """The MEB's stated parameters, through the MEB's own matrix. These are
    the numbers, not the ones the document prints (next test)."""
    base = models.MEB_BREAKER_CALIBRATION["baseline"]
    mit = models.MEB_BREAKER_CALIBRATION["mitigated"]
    pi_b = models.integration_breaker_stationary(*base["params"])
    pi_m = models.integration_breaker_stationary(*mit["params"])
    assert [round(p * 100, 2) for p in pi_b] == [38.70, 5.57, 18.58, 37.15]
    assert [round(p * 100, 2) for p in pi_m] == [93.29, 2.10, 4.19, 0.42]
    assert models.availability(pi_b) * 100 == pytest.approx(44.27, abs=0.01)
    assert models.availability(pi_m) * 100 == pytest.approx(95.39, abs=0.01)
    red = models.downtime_reduction(models.downtime(pi_b), models.downtime(pi_m))
    assert red * 100 == pytest.approx(91.72, abs=0.01)


def test_meb_calibration_the_printed_vectors_come_from_other_parameters():
    """Documented divergence (plan K1): the printed π vectors are not the
    stationary distribution of the printed parameters -- the CLOSED balance
    fails by a factor of three -- and the printed baseline vector IS the
    stationary distribution at (p_f, p_q) = (0.0375, 0.03)."""
    base = models.MEB_BREAKER_CALIBRATION["baseline"]
    mit = models.MEB_BREAKER_CALIBRATION["mitigated"]
    p_f, p_s, p_rec, p_q, p_reset = base["params"]
    pi = base["printed_pi"]
    assert p_f * pi[0] == pytest.approx(0.0860, abs=1e-4)
    assert p_s * pi[1] + p_reset * pi[3] == pytest.approx(0.0269, abs=1e-4)      # != 0.0860
    computed = models.integration_breaker_stationary(*base["params"])
    assert abs(computed[0] - pi[0]) > 0.30                       # 38.70 % against 71.64 %
    fitted = models.integration_breaker_stationary(0.0375, p_s, p_rec, 0.03, p_reset)
    assert [round(p, 4) for p in fitted] == list(pi)             # reproduced exactly
    assert sum(pi[:2]) == pytest.approx(base["printed_availability"], abs=1e-4)
    assert sum(mit["printed_pi"][:2]) == pytest.approx(mit["printed_availability"], abs=1e-4)
    # the printed reduction follows from the printed downtimes, and agrees with
    # the stated parameters' reduction to 0.03 pp
    printed = models.downtime_reduction(base["printed_downtime"], mit["printed_downtime"])
    assert printed == pytest.approx(models.MEB_BREAKER_CALIBRATION["printed_downtime_reduction"], abs=5e-5)
    assert printed * 100 == pytest.approx(91.75, abs=0.01)
    with pytest.raises(ValueError):
        models.downtime_reduction(0.0, 0.0)


# --- MEB 2.5 context depletion and CLT overflow ---------------------------------------

def test_meb_context_budget_constants_and_safe_turns():
    assert models.CONTEXT_WINDOW_TOKENS == models.CONTEXT_LIMIT_TOKENS == 200_000
    assert models.SAFE_CONTEXT_BUDGET_TOKENS == 160_000 == 0.8 * models.CONTEXT_WINDOW_TOKENS
    assert models.OVERFLOW_TRIGGER_P == 0.05
    assert models.safe_turns_remaining(140_000, 3_000) == 6                  # ⌊20 000 / 3 000⌋
    assert models.safe_turns_remaining(140_000, 3_000, budget=200_000) == 20
    assert models.safe_turns_remaining(170_000, 3_000) == 0
    assert models.safe_turns_remaining(140_000, 3_000) == models.context_depletion(140_000, 160_000, 3_000)


def test_meb_overflow_probability_worked_example_and_shape():
    p = models.overflow_probability(140_000, 160_000, 5, 3_000, 800)
    z = (160_000 - 140_000 - 5 * 3_000) / (800 * math.sqrt(5))
    assert z == pytest.approx(2.7951, abs=1e-4)
    assert p == pytest.approx(1 - models.normal_cdf(z))
    assert p == pytest.approx(0.0026, abs=1e-4)
    assert not models.should_trigger_adaptive_tail(p)
    # more turns, more risk; a mean that already exceeds the head-room is past 1/2
    assert models.overflow_probability(140_000, 160_000, 6, 3_000, 800) > p
    assert models.overflow_probability(140_000, 160_000, 7, 3_000, 800) > 0.5
    assert models.should_trigger_adaptive_tail(models.overflow_probability(140_000, 160_000, 7, 3_000, 800))
    assert models.should_trigger_adaptive_tail(0.05) is False and models.should_trigger_adaptive_tail(0.0501)
    # σ = 0: the step
    assert models.overflow_probability(140_000, 160_000, 6, 3_000, 0) == 0.0
    assert models.overflow_probability(140_000, 160_000, 7, 3_000, 0) == 1.0
    assert models.normal_cdf(0.0) == 0.5
    assert models.normal_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
    assert models.normal_cdf(-1.0) + models.normal_cdf(1.0) == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [
    (-1, 160_000, 5, 3_000, 800), (140_000, 0, 5, 3_000, 800), (140_000, 160_000, 0, 3_000, 800),
    (140_000, 160_000, 5, -1.0, 800), (140_000, 160_000, 5, 3_000, -1.0),
    (140_000, 160_000, 5, float("nan"), 800), (140_000.0, 160_000, 5, 3_000, 800),
    (140_000, 160_000, True, 3_000, 800)])
def test_meb_overflow_probability_validates(bad):
    with pytest.raises(ValueError):
        models.overflow_probability(*bad)


# --- MEB 2.6 the operational risk matrix ----------------------------------------------

def test_meb_operational_risk_matrix_scores_and_order():
    rows = models.OPERATIONAL_RISK_MATRIX
    assert len(rows) == 8
    assert [r.score for r in rows] == [2.40, 0.75, 1.65, 2.80, 1.20, 2.60, 1.50, 1.40]
    for r in rows:
        assert r.score == models.risk_score(r.probability, r.impact)
        assert 1 <= r.impact <= 5 and 0 < r.probability < 1
    assert [r.mode for r in rows][:2] == ["NTFS File Lock Collision (PermissionError pada open)",
                                         "Path Traversal Injection (Windows Reserved Names)"]
    assert rows[3] == ("Flaky Test Assertion Manipulation (Agent Cheat)", 0.70, "High", 4,
                       "High - False Quality", 2.80,
                       "Immutable Test Gates (penguncian read-only pada berkas uji selama implementasi).")
    assert rows[6].mitigation == "Penegakan PYTHONDONTWRITEBYTECODE=1 pada seluruh runner subprocess."
    ordered = models.prioritised_operational_risks()
    assert [r.score for r in ordered] == [2.80, 2.60, 2.40, 1.65, 1.50, 1.40, 1.20, 0.75]
    assert ordered[0].mode.startswith("Flaky Test") and ordered[-1].mode.startswith("Path Traversal")
    assert sorted(models.OPERATIONAL_RISK_MATRIX, key=lambda r: r.mode) != list(ordered)


# --- MEB 7.3 the birthday bound --------------------------------------------------------

def test_meb_hash_collision_probability_is_the_birthday_bound():
    p = models.hash_collision_probability(10 ** 9)
    assert p == pytest.approx(4.318e-60, rel=1e-3)
    assert p == pytest.approx((10 ** 9) ** 2 / 2 ** 257, rel=1e-6)          # the ≈ N²/2^257 form
    assert models.HASH_BITS == 256
    assert models.hash_collision_probability(0) == 0.0 == models.hash_collision_probability(1)
    assert models.hash_collision_probability(10 ** 12) > p
    # a 64-bit space at 2^32 records: N(N-1)/2^65 ≈ 0.5 -> 1 - e^-0.5
    assert models.hash_collision_probability(2 ** 32, bits=64) == pytest.approx(1 - math.exp(-0.5), rel=1e-6)
    for bad in ((-1,), (1.5,), (True,), (10, 0)):
        with pytest.raises(ValueError):
            models.hash_collision_probability(*bad)


# --- MEB 8 HNSW ----------------------------------------------------------------------

def test_meb_hnsw_parameters_and_memory_estimate():
    assert models.HNSW_PARAMS == {"space": "cosine", "M": 16, "ef_construction": 100, "ef_search": 32}
    assert (models.EMBEDDING_DIM, models.HNSW_NODE_OVERHEAD_BYTES) == (384, 208)
    assert models.estimate_hnsw_memory_bytes(1) == 1_536 + 256 + 208 == 2_000
    assert models.estimate_hnsw_memory_bytes(50_000) == 100_000_000
    assert models.estimate_hnsw_memory_bytes(50_000) / 2 ** 20 == pytest.approx(95.37, abs=0.01)
    assert models.estimate_hnsw_memory_bytes(0) == 0
    assert models.estimate_hnsw_memory_bytes(10, dim=3, m=1, overhead=0) == 10 * (12 + 16)
    with pytest.raises(ValueError):
        models.estimate_hnsw_memory_bytes(-1)
    with pytest.raises(ValueError):
        models.estimate_hnsw_memory_bytes(1.0)


def test_meb_additions_are_exported_and_the_module_stays_a_leaf():
    for name in ("LAMBDA_BASELINE", "LAMBDA_MITIGATED", "SESSION_ACTIONS", "poisson_reliability",
                 "reliability_gain", "session_failure_reduction", "ReliabilityRow",
                 "RELIABILITY_SENSITIVITY", "INTEGRATION_BREAKER_PARAMS", "integration_breaker_matrix",
                 "integration_breaker_stationary", "stationary_distribution", "availability",
                 "downtime", "downtime_reduction", "MEB_BREAKER_CALIBRATION", "CONTEXT_WINDOW_TOKENS",
                 "SAFE_CONTEXT_BUDGET_TOKENS", "OVERFLOW_TRIGGER_P", "safe_turns_remaining",
                 "normal_cdf", "overflow_probability", "should_trigger_adaptive_tail",
                 "OperationalRisk", "OPERATIONAL_RISK_MATRIX", "prioritised_operational_risks",
                 "HASH_BITS", "hash_collision_probability", "HNSW_PARAMS", "EMBEDDING_DIM",
                 "HNSW_NODE_OVERHEAD_BYTES", "estimate_hnsw_memory_bytes"):
        assert name in models.__all__, name
    assert len(models.__all__) == len(set(models.__all__))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-W", "error", "-c",
         "import sys, mindhive.models; print(sorted(m for m in sys.modules if m.startswith('mindhive')))"],
        capture_output=True, text=True, env=env, cwd=str(REPO))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "['mindhive', 'mindhive.models']"
