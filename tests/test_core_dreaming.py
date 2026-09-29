# tests/test_core_dreaming.py
"""Blueprint P1j, MEB CTX-02: `mindhive.core.dreaming`, the Dreaming Pass --
semantic de-duplication at cosine >= 0.88 (inclusive), first-kept-wins in
the caller's order, pure and non-mutating, with a Jaccard fallback for items
that carry no vector.
"""
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from mindhive.core import dreaming

REPO = Path(__file__).resolve().parents[1]


@dataclass
class Memory:
    text: str
    vector: tuple | None = None


def _unit_at(cos: float):
    """A 2-D unit vector whose cosine against (1, 0) is *cos*."""
    return (cos, math.sqrt(1.0 - cos * cos))


# --- the similarities ----------------------------------------------------------------

def test_cosine_of_the_textbook_pairs():
    assert dreaming.cosine_similarity((1, 0), (1, 0)) == 1.0
    assert dreaming.cosine_similarity((1, 0), (0, 1)) == 0.0
    assert dreaming.cosine_similarity((1, 0), (-1, 0)) == -1.0
    assert dreaming.cosine_similarity((3, 4), (6, 8)) == pytest.approx(1.0)
    assert dreaming.cosine_similarity((1, 0), _unit_at(0.9)) == pytest.approx(0.9)
    assert dreaming.cosine_similarity((0, 0), (1, 1)) == 0.0        # nothing to be similar to
    assert dreaming.cosine_similarity([], []) == 0.0


def test_cosine_refuses_a_length_mismatch_and_a_non_finite_component():
    with pytest.raises(ValueError, match="panjang"):
        dreaming.cosine_similarity((1, 0), (1, 0, 0))
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="terhingga"):
            dreaming.cosine_similarity((1, bad), (1, 0))


def test_cosine_is_clamped_against_rounding():
    v = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7)
    assert dreaming.cosine_similarity(v, v) <= 1.0
    assert dreaming.cosine_similarity(v, tuple(-x for x in v)) >= -1.0


def test_jaccard_and_tokens():
    assert dreaming.tokens("The cat sat ON the mat, 2x!") == frozenset({"the", "cat", "sat", "on", "mat", "2x"})
    assert dreaming.jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0
    assert dreaming.jaccard_similarity({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)
    assert dreaming.jaccard_similarity(set(), set()) == 0.0
    assert dreaming.jaccard_similarity(set(), {"a"}) == 0.0


# --- the pass ------------------------------------------------------------------------

def test_the_threshold_is_the_blueprints_number():
    assert dreaming.DREAMING_THRESHOLD == 0.88


def test_a_pair_at_cosine_point_nine_is_absorbed_and_one_at_point_five_is_kept():
    a = Memory("first", (1.0, 0.0))
    near = Memory("near duplicate", _unit_at(0.9))
    far = Memory("different", _unit_at(0.5))
    kept, dropped = dreaming.dreaming_pass([a, near, far])
    assert kept == [a, far] and dropped == [near]


def test_the_threshold_is_inclusive_and_first_kept_wins():
    items = ["newest", "older", "oldest"]
    scores = {("older", "newest"): 0.88, ("oldest", "newest"): 0.8799, ("oldest", "older"): 0.1}

    def sim(candidate, kept):
        return scores[(candidate, kept)]
    kept, dropped = dreaming.dreaming_pass(items, similarity=sim)
    assert kept == ["newest", "oldest"] and dropped == ["older"]


def test_text_only_items_fall_back_to_jaccard():
    kept, dropped = dreaming.dreaming_pass([
        "the cat sat on the mat",
        "the cat sat on the mat",              # identical tokens: 1.0
        "the cat sat on a mat",                # 5/6 = 0.833 < 0.88
        "a wholly different memory"])
    assert kept == ["the cat sat on the mat", "the cat sat on a mat", "a wholly different memory"]
    assert dropped == ["the cat sat on the mat"]


def test_dict_items_and_mixed_items_choose_the_similarity_by_what_they_carry():
    """Two vectors: cosine, whatever the texts say. One vector or none: the
    texts' Jaccard, whatever the vector says."""
    with_vec = {"text": "x", "vector": (1.0, 0.0)}
    twin = {"text": "totally different words", "vector": _unit_at(0.95)}
    no_vec = {"text": "x"}
    other = {"text": "y", "vector": (0.0, 1.0)}
    assert dreaming.default_similarity(twin, with_vec) == pytest.approx(0.95)
    assert dreaming.default_similarity(no_vec, with_vec) == 1.0     # "x" vs "x"
    assert dreaming.default_similarity(no_vec, other) == 0.0        # "x" vs "y"
    assert dreaming.default_similarity(other, with_vec) == 0.0      # orthogonal vectors
    kept, dropped = dreaming.dreaming_pass([with_vec, twin, no_vec, other])
    assert kept == [with_vec, other]
    assert dropped == [twin, no_vec]


def test_the_input_is_never_mutated_and_the_output_is_in_input_order():
    items = [Memory("a", (1.0, 0.0)), Memory("b", _unit_at(0.99)), Memory("c", (0.0, 1.0)),
             Memory("d", (-1.0, 0.0))]            # -1.0 against a, 0.0 against c: kept
    snapshot = list(items)
    kept, dropped = dreaming.dreaming_pass(items)
    assert items == snapshot
    assert kept == [items[0], items[2], items[3]] and dropped == [items[1]]
    # a candidate is measured against EVERY kept item, not only the first:
    near_c = Memory("e", _unit_at(0.05))          # 0.05 against a but 0.9987 against c
    assert dreaming.dreaming_pass(items + [near_c])[1] == [items[1], near_c]
    assert dreaming.dreaming_pass([]) == ([], [])


def test_a_custom_similarity_is_called_as_candidate_then_kept():
    seen = []

    def sim(candidate, kept):
        seen.append((candidate, kept))
        return 0.0
    dreaming.dreaming_pass(["n", "o", "p"], similarity=sim)
    assert seen == [("o", "n"), ("p", "n"), ("p", "o")]


@pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan")])
def test_the_threshold_must_be_a_probability(bad):
    with pytest.raises(ValueError):
        dreaming.dreaming_pass(["a"], threshold=bad)


def test_core_dreaming_imports_cold_as_a_leaf():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-W", "error", "-c",
         "import sys, mindhive.core.dreaming; "
         "print(sorted(m for m in sys.modules if m.startswith('mindhive')))"],
        capture_output=True, text=True, env=env, cwd=str(REPO))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "['mindhive', 'mindhive.core', 'mindhive.core.dreaming']"
