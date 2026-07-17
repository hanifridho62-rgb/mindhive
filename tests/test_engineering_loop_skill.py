# tests/test_engineering_loop_skill.py
"""The engineering-loop skill fuses /circle (autonomous headless loop) with the
lfg pipeline (plan → work → review → fix → verify) and closes the cycle with
MindHive's learn step. Guard its shape and its distinguishing integrations so a
rename or a gutted rewrite fails loudly."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = _ROOT / "skills" / "engineering-loop" / "SKILL.md"


def test_engineering_loop_skill_exists_and_is_named():
    assert _SKILL.exists(), "engineering-loop skill is missing"
    text = _SKILL.read_text(encoding="utf-8")
    assert text.startswith("---")
    fm = text.split("---", 2)[1]
    assert "name: engineering-loop" in fm


def test_engineering_loop_covers_all_lfg_stages():
    text = _SKILL.read_text(encoding="utf-8").lower()
    for stage in ("plan", "work", "review", "fix", "verify", "learn"):
        assert stage in text, f"engineering loop missing the {stage} stage"


def test_engineering_loop_builds_on_circle_and_is_more_detailed_than_lfg():
    text = _SKILL.read_text(encoding="utf-8").lower()
    assert "circle" in text and "lfg" in text
    # the recall/append memory spine between stages — what makes it "more detailed"
    assert "recall" in text and "append" in text


def test_engineering_loop_wires_the_invoke_and_improvement_channel():
    text = _SKILL.read_text(encoding="utf-8")
    assert "skills invoke" in text            # drives skills through MindHive
    assert "skill:" in text or "Improvements" in text   # failure→learning loop


def test_loop_is_learning_gated_and_parallel_aware():
    text = _SKILL.read_text(encoding="utf-8")
    assert "execution_mode" in text
    assert "discontinued" in text
    assert "Parallel whenever independent" in text
    assert "Oversee" in text
