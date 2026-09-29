# tests/test_core_recommend.py
"""Blueprint P1j, MEB section 2.3 (plan decision K2): `mindhive.core.recommend`
-- `Score = ((ok + 1) / (calls + 2)) * e^(-s_f / 2)` and the three-way
promotion rule, with the blueprint's worked numbers pinned and the one gap
in its PROBATION wording read as the residual status.
"""
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import models, recommend as ledger_recommend
from mindhive.core import recommend as core

REPO = Path(__file__).resolve().parents[1]


def test_the_constants_are_the_blueprints():
    assert (core.QUALIFIED_MIN_SCORE, core.QUALIFIED_MIN_CALLS, core.PROBATION_MIN_SCORE,
            core.DISCONTINUE_STREAK, core.DISCONTINUE_MIN_CALLS) == (0.80, 4, 0.50, 3, 5)
    assert core.STATUSES == ("QUALIFIED", "PROBATION", "DISCONTINUED")
    # the runtime threshold already agrees; the PDF's BAB 3 figure is a different number
    assert ledger_recommend.AUTO_MIN_RATE == core.QUALIFIED_MIN_SCORE == 0.8
    assert models.AUTO_MIN_RATE == 0.75 != core.QUALIFIED_MIN_SCORE
    assert ledger_recommend.DISCONTINUE_STREAK == core.DISCONTINUE_STREAK == models.STREAK_DISCONTINUE


def test_one_formula_in_the_repository():
    assert core.bayesian_laplace_rate is models.bayesian_laplace_rate
    assert core.streak_penalty is models.streak_penalty


@pytest.mark.parametrize("streak, penalty", [(0, 0.0), (1, 0.3935), (2, 0.6321), (3, 0.7769)])
def test_the_streak_penalty_table(streak, penalty):
    assert core.streak_penalty(streak) == pytest.approx(penalty, abs=1e-4)


@pytest.mark.parametrize("ok, calls, streak, score", [
    (4, 4, 0, 5 / 6),                       # 0.8333, the blueprint's worked case
    (1, 1, 0, 2 / 3),                       # 0.6667, its zero-frequency case
    (3, 3, 0, 0.8),
    (2, 10, 0, 0.25),
    (0, 0, 0, 0.5),                         # the flat prior alone
    (7, 10, 1, (8 / 12) * math.exp(-0.5)),  # 0.4043
])
def test_the_score_is_laplace_times_the_streak_complement(ok, calls, streak, score):
    assert core.skill_score(ok, calls, streak) == pytest.approx(score, abs=1e-4)


def test_the_streak_multiplies_by_e_to_the_minus_half_per_failure():
    base = core.skill_score(8, 10, 0)
    assert core.skill_score(8, 10, 1) == pytest.approx(base * math.exp(-0.5))
    assert core.skill_score(8, 10, 2) == pytest.approx(base * math.exp(-1.0))


@pytest.mark.parametrize("ok, calls, streak, incidents, status", [
    (4, 4, 0, 0, "QUALIFIED"),              # 0.8333, four calls, clean
    (10, 10, 0, 0, "QUALIFIED"),
    (1, 1, 0, 0, "PROBATION"),              # 0.6667
    (3, 3, 0, 0, "PROBATION"),              # 0.80 exactly, but calls < 4
    (4, 4, 0, 1, "PROBATION"),              # an open incident blocks QUALIFIED
    (7, 10, 1, 0, "DISCONTINUED"),          # 0.404 < 0.50 with calls >= 5
    (0, 3, 3, 0, "DISCONTINUED"),           # a streak of three, whatever the count
    (2, 10, 0, 0, "DISCONTINUED"),          # 0.25 < 0.50 and calls >= 5
    (1, 4, 2, 0, "PROBATION"),              # THE GAP: calls == 4, score 0.123, streak 2
    (0, 4, 2, 0, "PROBATION"),              # 0.167 * e^-1, calls == 4, streak 2
    (2, 4, 0, 0, "PROBATION"),              # 0.50 exactly: the PROBATION floor is inclusive
])
def test_promotion_status(ok, calls, streak, incidents, status):
    assert core.promotion_status(ok, calls, streak, incidents) == status


def test_a_streak_walks_a_strong_history_down_the_three_statuses():
    """97 of 100: 0.9608 clean -> QUALIFIED; one fresh failure 0.5827 ->
    PROBATION; two 0.3535 (< 0.50 at 100 calls) -> DISCONTINUED; three ->
    DISCONTINUED by the streak rule alone."""
    assert core.promotion_status(97, 100, 0) == "QUALIFIED"
    assert core.promotion_status(97, 100, 1) == "PROBATION"
    assert core.promotion_status(97, 100, 2) == "DISCONTINUED"
    assert core.promotion_status(97, 100, 3) == "DISCONTINUED"


@pytest.mark.parametrize("args", [
    (-1, 4, 0), (5, 4, 0), (1, 4, 4), (1.5, 4, 0), (True, 4, 0), (1, 4, -1), (1, 4, 1.0)])
def test_counts_are_validated(args):
    with pytest.raises(ValueError):
        core.skill_score(*args)


def test_open_incidents_is_validated():
    with pytest.raises(ValueError):
        core.promotion_status(4, 4, 0, -1)
    with pytest.raises(ValueError):
        core.promotion_status(4, 4, 0, True)


def test_core_recommend_imports_only_the_bab3_leaf():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-W", "error", "-c",
         "import sys, mindhive.core.recommend; "
         "print(sorted(m for m in sys.modules if m.startswith('mindhive')))"],
        capture_output=True, text=True, env=env, cwd=str(REPO))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "['mindhive', 'mindhive.core', 'mindhive.core.recommend', 'mindhive.models']"
