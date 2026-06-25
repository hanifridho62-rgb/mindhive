# tests/test_circle_skill.py
"""The /Circle skill is the human-facing entry point to `hive loop`. Guard its
shape so a rename/delete can't silently break activation."""
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "circle" / "SKILL.md"


def test_circle_skill_exists():
    assert _SKILL.exists(), "Circle activation skill is missing"


def test_circle_skill_frontmatter_and_loop_wiring():
    text = _SKILL.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md must open with YAML frontmatter"
    head = text.split("---", 2)[1]
    assert "name: circle" in head
    assert "description:" in head
    # it must actually drive the loop CLI, not reinvent it
    assert "mindhive loop" in text
    assert "loop init" in text
