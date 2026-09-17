# SPDX-License-Identifier: Apache-2.0
"""BAB 3 of the blueprint -- "Model Matematika, Analisis Probabilitas, dan
Risiko Kegagalan Sistemik" -- as pure functions.

Six sections, in the document's order, each with its formula written the way
the text writes it:

* **3.1** tool-failure arrivals as a homogeneous Poisson process over units of
  `ACTIONS_PER_UNIT` tool actions, exponential inter-arrival times, MTBF as
  `1/λ`, and the MTBF efficiency ratio (`+525%` from λ=5.0 to λ=0.8).
* **3.2** the circuit breaker as a discrete-time Markov chain over
  `CLOSED, HALF_OPEN, OPEN, QUARANTINED`: the 4x4 transition matrix in
  `p_f, p_cd, p_s, p_q, p_rel`, the event-level transition table the
  integration breaker (`orchestrator/breaker.py`) steps with, the stationary
  distribution `πP = π, Σπ = 1`, and lane availability `π_CLOSED + π_HALF_OPEN`
  against the 99.2% target.
* **3.3** Spearman's ρ over tie-averaged ranks, the scale-distortion factor
  (`1547.0 / 2.34 = 661.1x`), and the adaptive log-linear estimate that
  replaces the linear weight (`payoff.compute_calibrated_weight` builds on it).
* **3.4** context-window depletion: the additive accumulation `C(m)`, turns
  remaining, occupancy, the `0.04 -> 0.42` degradation step at 50% that is the
  reason for `MINDHIVE_ROTATION_RATIO = 0.5`, and the truncation probability
  `1 - Π(1 - P(ΔT_i >= C_limit - C(i-1)))`.
* **3.5** Laplace's rule of succession with a `Beta(1, 1)` prior for a skill's
  completion rate, the qualification thresholds `AUTO_MIN_RATE = 0.75` and
  `MEASURED_MIN_CALLS = 3`, and the exponential streak penalty
  `γ·(1 - e^(-s/2))` with discontinuation at three consecutive failures.
* **3.6** the systemic risk matrix: `R = P × I`, the priority band by impact,
  and the sixteen audited rows verbatim.

Derived conveniences, NOT stated in BAB 3 and kept for their callers:
`poisson_failure_probability`, `context_depletion`, `occupancy`,
`should_rotate`, `meets_availability_target`, `should_discontinue`,
`prioritised_risks`, and `classify_priority`'s impact -> band rule (inferred
from the sixteen rows). A verbatim audit should read them as such.

This module is a LEAF: it imports nothing from `mindhive` at module level.
`spearman_rho` reuses `orchestrator.payoff._ranks` (ties share the average of
the places they cover -- the reason that helper exists) but imports it at
call time, because `mindhive.orchestrator`'s `__init__` loads the whole
mission pipeline and a pure-mathematics module must stay cheap to import.
"""
from __future__ import annotations

import math
from typing import NamedTuple

# --- 3.1 Poisson process & MTBF ---------------------------------------------------

ACTIONS_PER_UNIT = 100
"""One observation unit is 100 tool actions (BAB 3.1)."""

LAMBDA_UNENFORCED_RANGE = (4.5, 6.2)
"""Empirical failure intensity, failures per unit, in sessions without rule
enforcement -- the band `incidents.py` telemetry reported."""

LAMBDA_TARGET = 0.8
"""The reliability target after `hive rules reflect` + `sopgate` enforcement."""


def _check_finite(name: str, value) -> None:
    """Every real-valued input is a finite number. A NaN passes `< 0` and
    `> calls` alike -- both comparisons are False -- so without this gate
    `poisson_pmf(nan, ...)`, `streak_penalty(nan)` and `spearman_rho` with a
    NaN rank returned NaN (or 1.0) silently instead of refusing (refute round
    2, finding 4). `bool` is refused too: `True` is a valid int elsewhere but
    never a rate."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} harus bilangan: {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} harus bilangan hingga (bukan NaN/inf): {value!r}")


def _check_rate(lam: float, t: float) -> None:
    _check_finite("laju kegagalan λ", lam)
    _check_finite("interval t", t)
    if lam < 0:
        raise ValueError(f"laju kegagalan λ tidak boleh negatif: {lam!r}")
    if t < 0:
        raise ValueError(f"interval t tidak boleh negatif: {t!r}")


def poisson_pmf(lam: float, t: float, k: int) -> float:
    """`P(N(t) = k) = ((λt)^k · e^(-λt)) / k!`."""
    _check_rate(lam, t)
    if isinstance(k, bool) or not isinstance(k, int) or k < 0:
        raise ValueError(f"jumlah insiden k harus bilangan bulat >= 0: {k!r}")
    mu = lam * t
    return (mu ** k) * math.exp(-mu) / math.factorial(k)


def poisson_failure_probability(lam: float, t: float = 1.0) -> float:
    """`P(N(t) >= 1) = 1 - e^(-λt)`: the chance a unit of work sees at least
    one tool failure."""
    _check_rate(lam, t)
    return 1.0 - math.exp(-lam * t)


def exponential_pdf(lam: float, t: float) -> float:
    """Inter-arrival density `f(t) = λ · e^(-λt)` for `t >= 0`, else 0."""
    _check_finite("laju kegagalan λ", lam)
    _check_finite("interval t", t)
    if lam < 0:
        raise ValueError(f"laju kegagalan λ tidak boleh negatif: {lam!r}")
    if t < 0:
        return 0.0
    return lam * math.exp(-lam * t)


def mtbf(failures: int, actions: int = ACTIONS_PER_UNIT) -> float:
    """`MTBF = E[T] = 1/λ` with `λ = failures / actions`, in actions. Zero
    failures is an infinite MTBF, not a division error."""
    if failures < 0 or actions <= 0:
        raise ValueError(f"MTBF butuh failures >= 0 dan actions > 0: {failures!r}, {actions!r}")
    if failures == 0:
        return math.inf
    return actions / failures


def mtbf_from_rate(lam: float) -> float:
    """`1/λ`; `λ = 0` is an infinite MTBF."""
    _check_finite("laju kegagalan λ", lam)
    if lam < 0:
        raise ValueError(f"laju kegagalan λ tidak boleh negatif: {lam!r}")
    return math.inf if lam == 0 else 1.0 / lam


def mtbf_efficiency(lam_initial: float, lam_target: float) -> float:
    """`Efisiensi (ΔMTBF) = ((1/λ_target) - (1/λ_awal)) / (1/λ_awal) × 100%`.
    λ_awal = 5.0 and λ_target = 0.8 give +525%."""
    _check_finite("λ_awal", lam_initial)
    _check_finite("λ_target", lam_target)
    if lam_initial <= 0 or lam_target <= 0:
        raise ValueError("efisiensi MTBF butuh kedua λ > 0")
    initial = 1.0 / lam_initial
    target = 1.0 / lam_target
    return (target - initial) / initial * 100.0


# --- 3.2 Discrete-time Markov chain of the circuit breaker --------------------------

BREAKER_STATES = ("CLOSED", "HALF_OPEN", "OPEN", "QUARANTINED")
CLOSED, HALF_OPEN, OPEN, QUARANTINED = BREAKER_STATES

BREAKER_PARAMS = {"same_error": 2, "no_progress": 3, "cooldown_s": 10.0,
                  "max_attempts": 2}
"""The DTMC's named parameters: `p_f` fires at `same_error >= 2` or
`no_progress >= 3`, `p_cd` when the 10.0 s cooldown has elapsed, and `p_q`
when a failed probe exceeds `max_attempts`. The text names `max_attempts`
without valuing it; the value here is the live one,
`orchestrator.resilience.DEFAULT_MAX_ATTEMPTS`, pinned by the tests."""

LANE_AVAILABILITY_TARGET = 0.992
"""`π_OPEN + π_QUARANTINED -> 0` so lane availability stays above 99.2%."""

BREAKER_EVENTS = (
    "success", "failure", "failure_threshold", "cooldown_elapsed",
    "probe_ok", "probe_fail", "probe_fail_max_attempts",
    "manual_reset", "rule_resolved",
)

# The non-zero, non-diagonal entries of the matrix, as (state, event) -> state.
# Every other pair either stays put (the states with a self-loop) or is
# undefined (HALF_OPEN, whose diagonal is 0: a probe step always leaves it).
BREAKER_TRANSITIONS = {
    (CLOSED, "failure_threshold"): OPEN,                    # p_f
    (OPEN, "cooldown_elapsed"): HALF_OPEN,                  # p_cd
    (HALF_OPEN, "probe_ok"): CLOSED,                        # p_s   (on_success)
    (HALF_OPEN, "probe_fail"): OPEN,                        # 1 - p_s - p_q
    (HALF_OPEN, "probe_fail_max_attempts"): QUARANTINED,    # p_q   (max_attempts)
    (QUARANTINED, "manual_reset"): CLOSED,                  # p_rel (operator)
    (QUARANTINED, "rule_resolved"): CLOSED,                 # p_rel (hive rules resolve)
}
_SELF_LOOP = frozenset({CLOSED, OPEN, QUARANTINED})
_HALF_OPEN_ALIASES = {"success": "probe_ok", "failure": "probe_fail"}


def circuit_breaker_transition(state: str, event: str) -> str:
    """The next state for one observed event, straight from the transition
    matrix. Unknown states and events raise; so does an event with no defined
    row entry in `HALF_OPEN`, whose diagonal is zero."""
    if state not in BREAKER_STATES:
        raise ValueError(f"status breaker tidak dikenal: {state!r}")
    if event not in BREAKER_EVENTS:
        raise ValueError(f"peristiwa breaker tidak dikenal: {event!r}")
    if state == HALF_OPEN:
        # The text ties `p_s` to the `on_success` hook: a plain success or
        # failure observed while HALF_OPEN IS the probe's outcome.
        event = _HALF_OPEN_ALIASES.get(event, event)
    nxt = BREAKER_TRANSITIONS.get((state, event))
    if nxt is not None:
        return nxt
    if state in _SELF_LOOP:
        return state
    raise ValueError(
        f"HALF_OPEN tidak punya transisi diri: peristiwa {event!r} harus berupa "
        "probe_ok, probe_fail, atau probe_fail_max_attempts")


def _check_prob(name: str, p: float) -> None:
    _check_finite(name, p)
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"{name} harus di [0, 1]: {p!r}")


def breaker_transition_matrix(p_f: float, p_cd: float, p_s: float, p_q: float,
                              p_rel: float) -> list[list[float]]:
    """The 4x4 matrix `P`, rows and columns in `BREAKER_STATES` order:

        CLOSED       [1 - p_f,  0,     p_f,             0      ]
        HALF_OPEN    [p_s,      0,     1 - p_s - p_q,   p_q    ]
        OPEN         [0,        p_cd,  1 - p_cd,        0      ]
        QUARANTINED  [p_rel,    0,     0,               1 - p_rel]
    """
    for name, p in (("p_f", p_f), ("p_cd", p_cd), ("p_s", p_s), ("p_q", p_q),
                    ("p_rel", p_rel)):
        _check_prob(name, p)
    if p_s + p_q > 1.0 + 1e-12:
        raise ValueError(f"p_s + p_q harus <= 1 agar baris HALF_OPEN berjumlah 1: {p_s + p_q!r}")
    return [
        [1.0 - p_f, 0.0, p_f, 0.0],
        [p_s, 0.0, 1.0 - p_s - p_q, p_q],
        [0.0, p_cd, 1.0 - p_cd, 0.0],
        [p_rel, 0.0, 0.0, 1.0 - p_rel],
    ]


def steady_state(matrix: list[list[float]]) -> list[float]:
    """The stationary distribution: solve `πP = π` with `Σπ = 1` by Gaussian
    elimination on `(Pᵀ - I)π = 0`, one equation replaced by the
    normalisation. Pure Python, no numpy.

    Unique only for an irreducible chain, which every breaker with `p_f > 0`,
    `p_cd > 0` and `p_rel > 0` is. A REDUCIBLE chain has infinitely many
    stationary distributions and this returns ONE of them: with an absorbing
    QUARANTINED (`p_rel = 0`) the mass ends there, `[0, 0, 0, 1]`; with two
    absorbing states (e.g. the identity matrix) the pivot-free column is
    zeroed and the first absorbing state carries everything. Pinned by the
    tests as documented behaviour, not as a claim of uniqueness."""
    n = len(matrix)
    if n == 0 or any(len(row) != n for row in matrix):
        raise ValueError("matriks transisi harus persegi dan tidak kosong")
    for row in matrix:
        if abs(sum(row) - 1.0) > 1e-9 or any(p < -1e-12 for p in row):
            raise ValueError(f"setiap baris matriks transisi harus berjumlah 1: {row!r}")
    # A π = b with A = (Pᵀ - I), last row replaced by ones (Σπ = 1).
    a = [[matrix[j][i] - (1.0 if i == j else 0.0) for j in range(n)] for i in range(n)]
    b = [0.0] * n
    a[n - 1] = [1.0] * n
    b[n - 1] = 1.0
    # forward elimination with partial pivoting
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-15:
            continue
        a[col], a[pivot] = a[pivot], a[col]
        b[col], b[pivot] = b[pivot], b[col]
        for r in range(col + 1, n):
            f = a[r][col] / a[col][col]
            if f:
                for c in range(col, n):
                    a[r][c] -= f * a[col][c]
                b[r] -= f * b[col]
    pi = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = b[r] - sum(a[r][c] * pi[c] for c in range(r + 1, n))
        pi[r] = s / a[r][r] if abs(a[r][r]) > 1e-15 else 0.0
    pi = [max(0.0, p) for p in pi]
    total = sum(pi)
    if total <= 0:
        raise ValueError("distribusi stasioner tidak ternormalisasi")
    return [p / total for p in pi]


def lane_availability(pi: list[float]) -> float:
    """`π_CLOSED + π_HALF_OPEN`: the share of time a lane can accept work.

    `pi` must be a distribution -- every entry in [0, 1] and the entries
    summing to 1 (within 1e-6, the slack `steady_state`'s elimination leaves)
    -- or `[2.0, 0, -1.0, 0]` would report 200% availability."""
    if len(pi) != len(BREAKER_STATES):
        raise ValueError(f"vektor π harus punya {len(BREAKER_STATES)} entri: {pi!r}")
    for state, p in zip(BREAKER_STATES, pi):
        _check_prob(f"π_{state}", p)
    if abs(sum(pi) - 1.0) > 1e-6:
        raise ValueError(f"vektor π harus berjumlah 1: {pi!r}")
    return pi[0] + pi[1]


def meets_availability_target(pi: list[float]) -> bool:
    """The text's condition is strict: availability must stay ABOVE 99.2%,
    so exactly `LANE_AVAILABILITY_TARGET` does not meet it."""
    return lane_availability(pi) > LANE_AVAILABILITY_TARGET


# --- 3.3 Spearman rank correlation & scale calibration ------------------------------

def spearman_rho(xs, ys) -> float | None:
    """`ρ = 1 - (6 · Σ d_i²) / (n · (n² - 1))`, `d_i = R(X_i) - R(Y_i)`, over
    tie-averaged ranks. `None` under two samples (the text: `n >= 2`); at
    exactly two, any non-inverted pair is trivially ±1."""
    xs, ys = list(xs), list(ys)
    if len(xs) != len(ys):
        raise ValueError(f"deret peringkat tidak sama panjang: {len(xs)} vs {len(ys)}")
    for i, (x, y) in enumerate(zip(xs, ys)):
        _check_finite(f"xs[{i}]", x)
        _check_finite(f"ys[{i}]", y)
    n = len(xs)
    if n < 2:
        return None
    from .orchestrator import payoff   # call-time: keeps this module a leaf
    rx, ry = payoff._ranks(xs), payoff._ranks(ys)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return 1.0 - (6.0 * d2) / (n * (n * n - 1))


def scale_distortion(estimate_ratio: float, actual_ratio: float) -> float:
    """`Faktor Distorsi Skala = (W_max/W_min) / (T_max/T_min)`; the audit's
    `1547.0 / 2.34 = 661.1x`."""
    _check_finite("rasio estimasi", estimate_ratio)
    _check_finite("rasio durasi", actual_ratio)
    if estimate_ratio <= 0 or actual_ratio <= 0:
        raise ValueError("rasio estimasi dan durasi harus > 0")
    return estimate_ratio / actual_ratio


def log_linear_estimate(complexity: float, churn: float, alpha: float,
                        beta_complexity: float, beta_churn: float,
                        epsilon: float = 0.0, *, floor: float | None = None) -> float:
    """`ln(T_est) = α + β1·ln(Complexity) + β2·ln(Churn) + ε`, returned as
    `T_est` -- the bare formula: a non-positive input has no logarithm and
    raises. `floor` is the caller's guard, not the text's: `floor=1.0` clamps
    both inputs up to 1 so a zero-churn file estimates as `exp(α + β1·ln C)`
    (what `payoff.compute_calibrated_weight` needs for real repositories)."""
    for name, v in (("complexity", complexity), ("churn", churn), ("alpha", alpha),
                    ("beta_complexity", beta_complexity), ("beta_churn", beta_churn),
                    ("epsilon", epsilon)):
        _check_finite(name, v)
    c, h = float(complexity), float(churn)
    if floor is not None:
        c, h = max(float(floor), c), max(float(floor), h)
    if c <= 0 or h <= 0:
        raise ValueError(
            f"ln tidak terdefinisi untuk masukan <= 0: complexity={complexity!r}, "
            f"churn={churn!r} (beri floor=1.0 untuk menjepit)")
    return math.exp(alpha + beta_complexity * math.log(c)
                    + beta_churn * math.log(h) + epsilon)


# --- 3.4 Context-window depletion ---------------------------------------------------

CONTEXT_LIMIT_TOKENS = 200_000
C0_RANGE = (4_500, 8_000)
"""Typical initial context: system prompt, canonical directives, the
MEMORY.md digest and the opening state."""
ROTATION_RATIO = 0.5
"""`MINDHIVE_ROTATION_RATIO`: rotate at 50% occupancy."""
ATTENTION_DEGRADATION_P = (0.04, 0.42)
"""Empirical probability of attention dilution / syntax hallucination below
and at-or-above 50% occupancy."""


def context_size(c0: int, deltas) -> int:
    """`C(m) = C0 + Σ_{i=1..m} (ΔU_i + ΔA_i + ΔT_i)`; `deltas` is an iterable
    of `(ΔU, ΔA, ΔT)` triples."""
    total = int(c0)
    for du, da, dt in deltas:
        total += int(du) + int(da) + int(dt)
    return total


def context_depletion(current: int, limit: int, per_turn: int) -> int:
    """Whole turns left before `limit` at `per_turn` tokens a turn; 0 once the
    window is at or over budget."""
    if per_turn <= 0:
        raise ValueError(f"konsumsi per putaran harus > 0: {per_turn!r}")
    if limit <= 0:
        raise ValueError(f"batas konteks harus > 0: {limit!r}")
    remaining = limit - current
    return max(0, remaining // per_turn)


def occupancy(current: int, limit: int) -> float:
    """`C(m) / C_limit`."""
    if limit <= 0:
        raise ValueError(f"batas konteks harus > 0: {limit!r}")
    return current / limit


def attention_degradation_probability(occ: float) -> float:
    """0.04 up to 50% occupancy, 0.42 strictly above it -- the text's
    `C(m) > 0.5 · C_limit`, the step that justifies Smart-Zone rotation AT
    `ROTATION_RATIO` (`should_rotate` fires at 50% so the session never
    enters the degraded half)."""
    _check_finite("okupansi", occ)
    low, high = ATTENTION_DEGRADATION_P
    return high if occ > ROTATION_RATIO else low


def should_rotate(current: int, limit: int, ratio: float = ROTATION_RATIO) -> bool:
    _check_finite("rasio rotasi", ratio)
    return occupancy(current, limit) >= ratio


def truncation_probability(tail_probabilities) -> float:
    """`P(C(m) >= C_limit) = 1 - Π_i (1 - P(ΔT_i >= C_limit - C(i-1)))` for the
    per-turn tail probabilities of a fat-tailed `ΔT`."""
    survive = 1.0
    for p in tail_probabilities:
        _check_prob("P(ΔT_i >= sisa)", p)
        survive *= 1.0 - p
    return 1.0 - survive


# --- 3.5 Bayesian inference for skill recommendation ---------------------------------

LAPLACE_ALPHA = 1.0
LAPLACE_BETA = 1.0
AUTO_MIN_RATE = 0.75
MEASURED_MIN_CALLS = 3
STREAK_DISCONTINUE = 3
"""Three consecutive failures with an active prevention rule discontinue the
skill deterministically."""


def bayesian_laplace_rate(ok: int, calls: int, alpha: float = LAPLACE_ALPHA,
                          beta: float = LAPLACE_BETA) -> float:
    """`E[θ | ok, calls] = (ok + α) / (calls + α + β)` with a `Beta(1, 1)` prior:
    (0,0) -> 0.50, (1,1) -> 0.67, (4,4) -> 0.833.

    A prior with `α + β = 0` and no observations has no mean: refused as a
    `ValueError`, not surfaced as a `ZeroDivisionError` from the arithmetic."""
    for name, v in (("ok", ok), ("calls", calls), ("alpha", alpha), ("beta", beta)):
        _check_finite(name, v)
    if calls < 0 or ok < 0 or ok > calls:
        raise ValueError(f"butuh 0 <= ok <= calls: ok={ok!r}, calls={calls!r}")
    if alpha < 0 or beta < 0:
        raise ValueError(f"prior Beta butuh α, β >= 0: alpha={alpha!r}, beta={beta!r}")
    if calls + alpha + beta <= 0:
        raise ValueError(
            f"laju Laplace tidak terdefinisi tanpa prior dan tanpa observasi: "
            f"calls={calls!r}, alpha={alpha!r}, beta={beta!r}")
    return (ok + alpha) / (calls + alpha + beta)


def laplace_qualifies(ok: int, calls: int) -> bool:
    """Promotion to auto / MUST USE needs the Laplace rate at or over
    `AUTO_MIN_RATE` AND at least `MEASURED_MIN_CALLS` calls."""
    return calls >= MEASURED_MIN_CALLS and bayesian_laplace_rate(ok, calls) >= AUTO_MIN_RATE


def streak_penalty(streak: int, gamma: float = 1.0) -> float:
    """`P_streak = γ · (1 - e^(-s_f / 2))`."""
    _check_finite("fail streak", streak)
    _check_finite("γ", gamma)
    if streak < 0:
        raise ValueError(f"fail streak tidak boleh negatif: {streak!r}")
    return gamma * (1.0 - math.exp(-streak / 2.0))


def should_discontinue(fail_streak: int, active_rule: bool) -> bool:
    """`s_f >= 3` consecutive failures WITH an active incident-prevention rule
    move the skill to `discontinued` deterministically -- both conjuncts, as
    the text states them; a streak alone only pays `streak_penalty`."""
    _check_finite("fail streak", fail_streak)
    if fail_streak < 0:
        raise ValueError(f"fail streak tidak boleh negatif: {fail_streak!r}")
    return bool(active_rule) and fail_streak >= STREAK_DISCONTINUE


# --- 3.6 Systemic risk matrix ----------------------------------------------------------

class RiskRow(NamedTuple):
    id: str
    component: str
    module: str
    probability: float
    probability_label: str
    impact: int
    impact_label: str
    score: float
    priority: str
    classification: str


def risk_score(probability: float, impact: int) -> float:
    """`R = P × I`, rounded to three places."""
    _check_prob("probabilitas", probability)
    if isinstance(impact, bool) or not isinstance(impact, int) or not 1 <= impact <= 5:
        raise ValueError(f"dampak harus bilangan bulat 1..5: {impact!r}")
    return round(probability * impact, 3)


def classify_priority(impact: int) -> str:
    """The band the matrix uses: impact 5 -> P0, 4 or 3 -> P1, 2 -> P2, 1 -> P3.
    INFERRED from the sixteen rows, not stated by the text as a rule: the band
    follows impact, not score (M-07 at 1.75 is P0 while M-04 at 3.40 is P1)."""
    if isinstance(impact, bool) or not isinstance(impact, int) or not 1 <= impact <= 5:
        raise ValueError(f"dampak harus bilangan bulat 1..5: {impact!r}")
    return {5: "P0", 4: "P1", 3: "P1", 2: "P2", 1: "P3"}[impact]


def _row(id_, component, module, p, p_label, impact, i_label, classification) -> RiskRow:
    return RiskRow(id_, component, module, p, p_label, impact, i_label,
                   risk_score(p, impact), classify_priority(impact), classification)


RISK_MATRIX = (
    _row("M-01", "Hardcoded Paths di Hooks", "hooks/*.ps1", 1.00, "Pasti", 5,
         "Kritis / Outage", "Pemblokir Fondasi Mutlak"),
    _row("M-03", "Prompt Injection via Ledger SOP", "advice.py", 0.70, "Sering", 5,
         "Kritis / Hijack", "Celah Integritas Kognitif"),
    _row("M-07", "Locking Race on Delete-Pending", "tasktool.py", 0.35, "Sedang", 5,
         "Kritis / Data Loss", "Kerusakan Integritas Mutasi"),
    _row("M-04", "Vacuous Pass di SuiteGate", "gate.py", 0.85, "Sangat Sering", 4,
         "Tinggi / False Green", "Kelemahan Verifikasi Mutu"),
    _row("M-02", "Path Traversal via session_id", "events.py", 0.45, "Sedang", 4,
         "Tinggi / File Leak", "Kerentanan File I/O"),
    _row("M-16", "Orphan Grandchildren Processes", "verify.py", 0.70, "Sering", 4,
         "Tinggi / OOM Crash", "Kebocoran Resource OS"),
    _row("M-06", "Kalibrasi Skala Bobot Tugas", "payoff.py", 1.00, "Pasti", 3,
         "Sedang / Suboptimal", "Distorsi Penjadwalan Kerja"),
    _row("M-08", "Cache Plugin Skill Usang", ".claude-plugin/", 1.00, "Pasti", 3,
         "Sedang / Logic Drift", "Divergensi Konteks Model"),
    _row("M-05", "Utang Teknis Monolitik", "__main__.py", 1.00, "Pasti", 2,
         "Rendah-Sedang / Debt", "Pemeliharaan & Higienitas"),
    _row("M-09", "Fitur Fabricate Phantom", "fabricate.py", 1.00, "Pasti", 2,
         "Rendah / Dead Code", "Rekonsiliasi Branch Git"),
    _row("M-10", "Ketergantungan Utilitas jq", "autoplan", 0.95, "Sangat Sering", 2,
         "Rendah / Missing CLI", "Portabilitas Subproses"),
    _row("M-11", "TUI Concurrency Load Glitch", "dashboard.py", 0.75, "Tinggi", 2,
         "Rendah / UI Glitch", "Observabilitas Terminal"),
    _row("M-13", "Stale Render Clobbering", "dashboard_page.py", 0.80, "Tinggi", 2,
         "Rendah / UI Flicker", "Sinkronisasi Frontend"),
    _row("M-15", "Disfungsi Validasi Empiris A/B", "ab.py", 1.00, "Pasti", 2,
         "Rendah / Unchecked", "Bukti Kinerja Arsitektur"),
    _row("M-12", "Rasio Kontras WCAG AA", "dashboard_css.py", 1.00, "Pasti", 1,
         "Rendah / Kosmetik", "Aksesibilitas Desain"),
    _row("M-14", "Kekosongan Lisensi Resmi", "LICENSE", 1.00, "Pasti", 1,
         "Legal Compliance", "Perlindungan Hukum Open Source"),
)
"""The sixteen audited foundation problems, in the document's order."""


def prioritised_risks() -> list[RiskRow]:
    """The matrix ordered for treatment: by priority band, then descending
    score, then id -- the order the triage tiers of BAB 10.12 walk. Derived
    convenience: BAB 3 prints the rows in its own order and states no sort."""
    return sorted(RISK_MATRIX, key=lambda r: (r.priority, -r.score, r.id))


__all__ = [
    "ACTIONS_PER_UNIT", "LAMBDA_UNENFORCED_RANGE", "LAMBDA_TARGET",
    "poisson_pmf", "poisson_failure_probability", "exponential_pdf",
    "mtbf", "mtbf_from_rate", "mtbf_efficiency",
    "BREAKER_STATES", "CLOSED", "HALF_OPEN", "OPEN", "QUARANTINED",
    "BREAKER_PARAMS", "LANE_AVAILABILITY_TARGET", "BREAKER_EVENTS",
    "BREAKER_TRANSITIONS", "circuit_breaker_transition",
    "breaker_transition_matrix", "steady_state", "lane_availability",
    "meets_availability_target",
    "spearman_rho", "scale_distortion", "log_linear_estimate",
    "CONTEXT_LIMIT_TOKENS", "C0_RANGE", "ROTATION_RATIO", "ATTENTION_DEGRADATION_P",
    "context_size", "context_depletion", "occupancy",
    "attention_degradation_probability", "should_rotate", "truncation_probability",
    "LAPLACE_ALPHA", "LAPLACE_BETA", "AUTO_MIN_RATE", "MEASURED_MIN_CALLS",
    "STREAK_DISCONTINUE", "bayesian_laplace_rate", "laplace_qualifies",
    "streak_penalty", "should_discontinue",
    "RiskRow", "risk_score", "classify_priority", "RISK_MATRIX", "prioritised_risks",
]
