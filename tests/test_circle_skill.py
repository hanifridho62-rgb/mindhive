# tests/test_circle_skill.py
"""/Circle has two entry points to `hive loop`: a typeable slash COMMAND
(.claude/commands/circle.md) and a model-invoked SKILL (.claude/skills/circle/).
Guard both so a rename/delete can't silently break activation."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = _ROOT / ".claude" / "skills" / "circle" / "SKILL.md"
_COMMAND = _ROOT / ".claude" / "commands" / "circle.md"


def test_circle_skill_exists():
    assert _SKILL.exists(), "Circle activation skill is missing"


def test_circle_slash_command_exists_and_drives_loop():
    # The typeable `/circle` entry point — what the user literally types.
    assert _COMMAND.exists(), "Circle slash command is missing"
    text = _COMMAND.read_text(encoding="utf-8")
    assert text.startswith("---")               # frontmatter
    assert "mindhive loop" in text


def test_circle_skill_frontmatter_and_loop_wiring():
    text = _SKILL.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md must open with YAML frontmatter"
    head = text.split("---", 2)[1]
    assert "name: circle" in head
    assert "description:" in head
    # it must actually drive the loop CLI, not reinvent it
    assert "mindhive loop" in text
    assert "loop init" in text
