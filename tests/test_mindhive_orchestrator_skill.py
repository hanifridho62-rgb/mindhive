# tests/test_mindhive_orchestrator_skill.py
"""The centralized pipeline ships as part of the mindhive Claude Code PLUGIN: a
typeable slash COMMAND (commands/mindhive-orchestrator.md) + a model-invoked SKILL
(skills/mindhive-orchestrator/) with on-demand reference files, declared by the
plugin manifest (.claude-plugin/plugin.json). The pipeline is reachable ONLY via
/mindhive-orchestrator — the old /mindhive front door was retired. Guard the shape
so a rename/delete can't silently break activation or resurrect the old trigger."""
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = _ROOT / "skills" / "mindhive-orchestrator" / "SKILL.md"
_REFS = _ROOT / "skills" / "mindhive-orchestrator" / "references"
_MATRIX = _REFS / "recommendation-matrix.md"
_HANDOFF = _REFS / "handoff-protocol.md"
_STAGES = _REFS / "pipeline-stages.md"
_PARALLEL = _REFS / "parallel-orchestration.md"
_COMMAND = _ROOT / "commands" / "mindhive-orchestrator.md"
_OLD_COMMAND = _ROOT / "commands" / "mindhive.md"
_PLUGIN = _ROOT / ".claude-plugin" / "plugin.json"
_MARKETPLACE = _ROOT / ".claude-plugin" / "marketplace.json"


def test_orchestrator_skill_exists():
    assert _SKILL.exists(), "MindHive orchestrator skill is missing"
    text = _SKILL.read_text(encoding="utf-8")
    # Valid frontmatter with the expected skill name.
    assert text.startswith("---") and "name: mindhive-orchestrator" in text.split("---", 2)[1]
    # Recall-before-asking is the prime directive — guard that it survives edits.
    assert "mindhive recall" in text


def test_orchestrator_is_the_ten_stage_dynamic_pipeline():
    text = _SKILL.read_text(encoding="utf-8").lower()
    # The ten ordered stages must all be present.
    for stage in ("brainstorm", "plan", "observation", "converge", "prd",
                  "matrix", "kanban", "execution", "quality", "learning"):
        assert stage in text, f"orchestrator missing the {stage} stage"
    # Backbone restricted to the two allowed plugins.
    assert "superpowers" in text and "compound-engineering" in text
    # Dynamic gate: automatic vs. semi-automatic advancement.
    assert "automatic" in text
    # Parallel layer is wired to the cluster CLI.
    assert "clusters" in text


def test_orchestrator_reference_files_exist():
    assert _MATRIX.exists(), "recommendation-matrix reference is missing"
    assert _HANDOFF.exists(), "handoff-protocol reference is missing"
    assert _STAGES.exists(), "pipeline-stages reference is missing"
    assert _PARALLEL.exists(), "parallel-orchestration reference is missing"
    # The matrix must keep enforcing the verbatim available-skills rule.
    assert "available-skills" in _MATRIX.read_text(encoding="utf-8")
    # Stage map must carry the backbone-restriction rule.
    stages = _STAGES.read_text(encoding="utf-8")
    assert "superpowers" in stages and "compound-engineering" in stages
    # Parallel protocol must reference the cluster CLI + file-ownership safety.
    parallel = _PARALLEL.read_text(encoding="utf-8")
    assert "clusters" in parallel and "file-ownership" in parallel.lower()


def test_orchestrator_slash_command_exists_and_drives_skill():
    # The typeable `/mindhive-orchestrator` entry point — the ONLY pipeline trigger.
    assert _COMMAND.exists(), "/mindhive-orchestrator slash command is missing"
    text = _COMMAND.read_text(encoding="utf-8")
    assert text.startswith("---")                # frontmatter
    assert "$ARGUMENTS" in text                  # forwards the idea to the skill
    assert "mindhive-orchestrator" in text       # points at the skill


def test_old_mindhive_front_door_is_retired():
    # The pipeline must live ONLY behind /mindhive-orchestrator now.
    assert not _OLD_COMMAND.exists(), "commands/mindhive.md should have been removed"


def test_plugin_manifest_advertises_the_pipeline():
    data = json.loads(_PLUGIN.read_text(encoding="utf-8"))
    assert data["name"] == "mindhive"
    # Bumped when the consolidated pipeline shipped; never regress below it.
    # Compare as a numeric tuple, not a string — lexicographic ">=" would rank
    # "0.10.0" below "0.4.0" and fail the guard on a future minor bump.
    version = tuple(int(part) for part in data["version"].split("."))
    assert version >= (0, 4, 0)
    assert "/mindhive-orchestrator" in data["description"]


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
