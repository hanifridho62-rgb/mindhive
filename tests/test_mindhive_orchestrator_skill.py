# tests/test_mindhive_orchestrator_skill.py
"""/mindhive ships as part of the mindhive Claude Code PLUGIN: a typeable slash
COMMAND (commands/mindhive.md) + a model-invoked SKILL
(skills/mindhive-orchestrator/) with two on-demand reference files, declared by
the plugin manifest (.claude-plugin/plugin.json). Guard the shape so a
rename/delete can't silently break activation, and so the manifest keeps
advertising the command."""
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = _ROOT / "skills" / "mindhive-orchestrator" / "SKILL.md"
_MATRIX = _ROOT / "skills" / "mindhive-orchestrator" / "references" / "recommendation-matrix.md"
_HANDOFF = _ROOT / "skills" / "mindhive-orchestrator" / "references" / "handoff-protocol.md"
_COMMAND = _ROOT / "commands" / "mindhive.md"
_PLUGIN = _ROOT / ".claude-plugin" / "plugin.json"
_MARKETPLACE = _ROOT / ".claude-plugin" / "marketplace.json"


def test_orchestrator_skill_exists():
    assert _SKILL.exists(), "MindHive orchestrator skill is missing"
    text = _SKILL.read_text(encoding="utf-8")
    # Valid frontmatter with the expected skill name.
    assert text.startswith("---") and "name: mindhive-orchestrator" in text.split("---", 2)[1]
    # Recall-before-asking is the prime directive — guard that it survives edits.
    assert "mindhive recall" in text


def test_orchestrator_reference_files_exist():
    assert _MATRIX.exists(), "recommendation-matrix reference is missing"
    assert _HANDOFF.exists(), "handoff-protocol reference is missing"
    # The matrix must keep enforcing the verbatim available-skills rule.
    assert "available-skills" in _MATRIX.read_text(encoding="utf-8")


def test_mindhive_slash_command_exists_and_drives_orchestrator():
    # The typeable `/mindhive` entry point — what the user literally types.
    assert _COMMAND.exists(), "/mindhive slash command is missing"
    text = _COMMAND.read_text(encoding="utf-8")
    assert text.startswith("---")                # frontmatter
    assert "$ARGUMENTS" in text                  # forwards the idea to the skill
    assert "mindhive-orchestrator" in text       # points at the skill


def test_plugin_manifest_advertises_mindhive():
    data = json.loads(_PLUGIN.read_text(encoding="utf-8"))
    assert data["name"] == "mindhive"
    # Bumped when /mindhive shipped; never regress below it. Compare as a
    # numeric tuple, not a string — lexicographic ">=" would rank "0.10.0"
    # below "0.2.0" and fail the guard on a future minor bump.
    version = tuple(int(part) for part in data["version"].split("."))
    assert version >= (0, 2, 0)
    assert "/mindhive" in data["description"]


def test_marketplace_version_matches_plugin():
    # The installer trusts marketplace.json's version, not plugin.json's —
    # if they drift, `claude plugin install` silently serves a stale-labeled
    # build (this bit us: plugin.json said 0.2.0 while marketplace pinned 0.1.0).
    plugin = json.loads(_PLUGIN.read_text(encoding="utf-8"))
    market = json.loads(_MARKETPLACE.read_text(encoding="utf-8"))
    entry = next(p for p in market["plugins"] if p["name"] == "mindhive")
    assert entry["version"] == plugin["version"], (
        f"marketplace.json pins {entry['version']} but plugin.json is "
        f"{plugin['version']} — bump both together"
    )


def test_skill_consumes_learning_gated_execution_modes():
    text = _SKILL.read_text(encoding="utf-8")
    assert "execution_mode" in text
    assert "`auto`" in text and "`suggest`" in text
    assert "discontinued" in text
    assert "Never auto-apply" in text


def test_skill_documents_parallel_dispatch_and_checkpoints():
    text = _SKILL.read_text(encoding="utf-8")
    assert "Parallel dispatch" in text
    assert "evaluation checkpoint" in text.lower()
    assert "Oversee" in text
    assert "stack trace" in text        # read failure logs, fix without new instructions
