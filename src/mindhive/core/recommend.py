# SPDX-License-Identifier: Apache-2.0
# src/mindhive/core/recommend.py
"""Skill promotion by Laplace succession and the exponential failure-streak
penalty (blueprint P1j; MEB section 2.3; plan decision K2).

The score is `Score = E[theta] * (1 - P_streak(s_f))` with
`E[theta] = (ok + 1) / (calls + 2)` (a Beta(1, 1) prior) and
`P_streak(s_f) = 1 - e^(-s_f / 2)`, so `Score = ((ok + 1) / (calls + 2)) * e^(-s_f / 2)`.
Both factors come from `mindhive.models` BY IDENTITY -- one formula in the
repository, not two -- and this module adds the decision the blueprint
puts on top of it:

    QUALIFIED     Score >= 0.80 AND calls >= 4 AND no open incident
    DISCONTINUED  s_f >= 3 OR (Score < 0.50 AND calls >= 5)
    PROBATION     everything else

The blueprint states PROBATION as `calls < 4 OR 0.50 <= Score < 0.80`,
which leaves a gap it does not name (calls == 4, Score < 0.50, streak
under 3: e.g. one success in four with the last two failed). PROBATION is
read here as the residual status -- "may still be called, under strict
metric watch" is what a skill in that gap deserves -- and the gap case is
pinned in `tests/test_core_recommend.py` so the reading is explicit.

DISCONTINUED is checked first: a streak of three discontinues a skill
whatever its history says, which is the point of the streak term. The
runtime consumer is `recommend.should_promote_skill` (P4); until then the
ledger's `recommend.AUTO_MIN_RATE` (0.8) already equals `QUALIFIED_MIN_SCORE`,
while `models.AUTO_MIN_RATE` (0.75) stays the PDF's BAB 3 figure, a
divergence `tests/test_models.py` pins on purpose.
"""
from ..models import bayesian_laplace_rate, streak_penalty

__all__ = ["QUALIFIED_MIN_SCORE", "QUALIFIED_MIN_CALLS", "PROBATION_MIN_SCORE",
           "DISCONTINUE_STREAK", "DISCONTINUE_MIN_CALLS",
           "QUALIFIED", "PROBATION", "DISCONTINUED", "STATUSES",
           "bayesian_laplace_rate", "streak_penalty", "skill_score", "promotion_status"]

QUALIFIED_MIN_SCORE = 0.80
QUALIFIED_MIN_CALLS = 4
PROBATION_MIN_SCORE = 0.50
DISCONTINUE_STREAK = 3
DISCONTINUE_MIN_CALLS = 5

QUALIFIED = "QUALIFIED"
PROBATION = "PROBATION"
DISCONTINUED = "DISCONTINUED"
STATUSES = (QUALIFIED, PROBATION, DISCONTINUED)


def _count(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} harus bilangan bulat >= 0: {value!r}")
    return value


def skill_score(ok: int, calls: int, fail_streak: int) -> float:
    """`((ok + 1) / (calls + 2)) * e^(-fail_streak / 2)`. Counts are
    non-negative ints with `ok <= calls` and `fail_streak <= calls - ok`
    (a run of consecutive failures cannot exceed the failures there were)."""
    _count("ok", ok)
    _count("calls", calls)
    _count("fail_streak", fail_streak)
    if ok > calls:
        raise ValueError(f"butuh ok <= calls: ok={ok!r}, calls={calls!r}")
    if fail_streak > calls - ok:
        raise ValueError(f"fail_streak melebihi jumlah kegagalan: "
                         f"fail_streak={fail_streak!r}, calls-ok={calls - ok!r}")
    return bayesian_laplace_rate(ok, calls) * (1.0 - streak_penalty(fail_streak))


def promotion_status(ok: int, calls: int, fail_streak: int, open_incidents: int = 0) -> str:
    """`QUALIFIED`, `PROBATION` or `DISCONTINUED` for the counts, by the
    rules in the module docstring; *open_incidents* only blocks QUALIFIED."""
    _count("open_incidents", open_incidents)
    score = skill_score(ok, calls, fail_streak)
    if fail_streak >= DISCONTINUE_STREAK or (
            score < PROBATION_MIN_SCORE and calls >= DISCONTINUE_MIN_CALLS):
        return DISCONTINUED
    if score >= QUALIFIED_MIN_SCORE and calls >= QUALIFIED_MIN_CALLS and open_incidents == 0:
        return QUALIFIED
    return PROBATION
