# tests/test_readme_prerequisites.py — blueprint P11 (spec §1.1–1.5): the
# prerequisites README states are the ones the build pins.
"""Spec §1.1–1.5 asks the README to say what the runtime needs: the operating
systems, the Python floor, the extras, the Claude Code CLI and the hardware.
The extras were documented and pinned in P8 (`tests/test_pyproject_extras.py`);
the rest had no section anywhere (RTM critic, gap 6). This holds the new
`### Prerequisites` section to the two files that actually decide those
things:

* the Python floor it states is `requires-python` from `pyproject.toml`;
* every operating system the CI matrix runs is named;
* the Claude Code CLI is named as what the orchestration commands need, and
  the hardware line gives a number, not an adjective.
"""
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _section() -> str:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    m = re.search(r"^### Prerequisites\n(.*?)(?=^##)", text, flags=re.M | re.S)
    assert m, "README.md has no `### Prerequisites` section"
    return m.group(1)


def test_the_python_floor_is_the_one_pyproject_declares():
    meta = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    floor = meta["project"]["requires-python"]          # ">=3.11"
    version = re.fullmatch(r">=\s*(\d+\.\d+)", floor).group(1)
    assert f"Python {version}" in _section() or f"Python >= {version}" in _section()


def test_every_operating_system_the_ci_matrix_runs_is_named():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    runners = re.search(r"^\s*os:\s*\[(.*?)\]", ci, flags=re.M).group(1)
    families = {r.strip().split("-")[0] for r in runners.split(",")}
    section = _section().lower()
    for family in families:                             # ubuntu, windows
        name = {"ubuntu": "linux", "windows": "windows", "macos": "macos"}[family]
        assert name in section, f"the CI runs on {family}; README does not say {name}"


def test_the_claude_cli_and_a_hardware_number_are_stated():
    section = _section()
    assert "Claude Code" in section
    assert re.search(r"\b\d+\s*(GB|MB)\b", section), "no hardware figure (GB/MB)"
    assert "hive lintester" in section or "hive mindnet" in section
