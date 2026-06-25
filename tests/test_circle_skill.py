# tests/test_circle_skill.py
"""/circle ships as a Claude Code PLUGIN: a typeable slash COMMAND
(commands/circle.md) + a model-invoked SKILL (skills/circle/), declared by the
plugin manifest (.claude-plugin/plugin.json) and installable via
.claude-plugin/marketplace.json. Guard the shape so a rename/delete can't
silently break activation."""
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = _ROOT / "skills" / "circle" / "SKILL.md"
_COMMAND = _ROOT / "commands" / "circle.md"
_PLUGIN = _ROOT / ".claude-plugin" / "plugin.json"
_MARKETPLACE = _ROOT / ".claude-plugin" / "marketplace.json"


def test_circle_skill_exists():
    assert _SKILL.exists(), "Circle activation skill is missing"
    text = _SKILL.read_text(encoding="utf-8")
    assert text.startswith("---") and "name: circle" in text.split("---", 2)[1]
    assert "mindhive loop" in text


def test_circle_slash_command_exists_and_drives_loop():
    # The typeable `/circle` entry point — what the user literally types.
    assert _COMMAND.exists(), "Circle slash command is missing"
    text = _COMMAND.read_text(encoding="utf-8")
    assert text.startswith("---")               # frontmatter
    assert "mindhive loop" in text


def test_plugin_manifest_valid():
    data = json.loads(_PLUGIN.read_text(encoding="utf-8"))
    assert data["name"] == "mindhive"
    assert "version" in data


def test_marketplace_lists_the_plugin():
    data = json.loads(_MARKETPLACE.read_text(encoding="utf-8"))
    names = [p["name"] for p in data["plugins"]]
    assert "mindhive" in names
    assert any(p["source"] == "./" for p in data["plugins"])
