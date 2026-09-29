"""Spec section 1.6 (plan RTM row 1.6): README's `## Environment variables`
section is the registry of every environment variable MIND HIVE reads, and the
registry and the code cannot drift apart in either direction.

A registry that only grows by hand rots in two ways. A variable the code stopped
reading stays documented and sends an operator chasing a knob that does nothing;
a variable the code started reading is never documented and becomes a behaviour
nobody can find. Both directions are checked here, against the real README and
the real source:

* every name the section documents is used somewhere in `src/mindhive/` or
  `hooks/` (or is a third-party variable the code demonstrably reads or sets);
* every `MINDHIVE_*` / `MINDNETWORK_*` name the code uses is documented, except
  the two the hook shims set for their own library, which are plumbing, not
  configuration -- and that exemption is itself checked (the shims assign them);
* the spec's own list (section 1.6) is all present.

"Uses" is measured by name: a string literal in the Python source (how every
`os.environ.get("MINDHIVE_…")` and every `ENV = "MINDHIVE_…"` constant spells
it), or a `$MINDHIVE_…` / `${MINDHIVE_…}` / `$env:MINDHIVE_…` reference in a hook.
`scripts/` is out of scope: each script documents its own variables next to it.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "mindhive"
HOOKS = REPO / "hooks"
SECTION = "## Environment variables"
ENV_NAME = re.compile(r"^(?:MINDHIVE|MINDNETWORK)_[A-Z0-9_]+$")
HOOK_REF = re.compile(r"\$(?:env:|\{)?((?:MINDHIVE|MINDNETWORK)_[A-Z0-9_]+)")
ROW_NAME = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]+)`")

# Spec section 1.6, verbatim list.
SPEC_1_6 = {"MINDHIVE_HOME", "MINDHIVE_PROJECT", "MINDHIVE_SESSION",
            "MINDHIVE_EVENT_BACKEND", "MINDHIVE_BRIEF", "MINDNETWORK_ENFORCE",
            "MINDHIVE_ROTATION", "MINDHIVE_ROTATION_CAP",
            "MINDHIVE_DASHBOARD_TOKEN", "MINDHIVE_SYMBOLS"}
# Set by the hook shims for `lib.sh` / `lib.ps1` in the same process; an operator
# exporting either changes nothing they could want changed.
INTERNAL = {"MINDHIVE_LIB", "MINDHIVE_HOOK_DIR"}
# Not ours, but the code reads or sets them, so the registry names them.
THIRD_PARTY = {"ANTHROPIC_API_KEY": "workspace.py",
               "OMP_NUM_THREADS": "vectors.py",
               "MKL_NUM_THREADS": "vectors.py",
               "OPENBLAS_NUM_THREADS": "vectors.py"}


def documented_names(readme_text: str) -> set:
    """The backticked first-column names of the table under SECTION."""
    lines = readme_text.splitlines()
    try:
        start = lines.index(SECTION)
    except ValueError:
        return set()
    names = set()
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        m = ROW_NAME.match(line)
        if m:
            names.add(m.group(1))
    return names


def names_used_by_code() -> dict:
    """``{name: {relative paths}}`` over the Python source and the hooks."""
    used = {}
    for p in sorted(SRC.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and ENV_NAME.match(node.value):
                used.setdefault(node.value, set()).add(p.relative_to(REPO).as_posix())
    for p in sorted(HOOKS.iterdir()):
        if p.is_file() and p.suffix in (".sh", ".ps1"):
            for name in HOOK_REF.findall(p.read_text(encoding="utf-8")):
                used.setdefault(name, set()).add(p.relative_to(REPO).as_posix())
    return used


README = (REPO / "README.md").read_text(encoding="utf-8")
DOCUMENTED = documented_names(README)
USED = names_used_by_code()


def test_the_registry_section_exists_and_is_not_a_stub():
    assert SECTION in README.splitlines(), f"README has no {SECTION!r} heading"
    assert len(DOCUMENTED) > 30, f"only {len(DOCUMENTED)} variables documented"


def test_every_documented_name_is_used_by_the_code():
    unused = sorted(n for n in DOCUMENTED if n not in USED and n not in THIRD_PARTY)
    assert unused == [], f"documented but read nowhere in src/ or hooks/: {unused}"


def test_every_name_the_code_uses_is_documented():
    missing = {n: sorted(USED[n]) for n in USED
               if n not in DOCUMENTED and n not in INTERNAL}
    assert missing == {}, f"used by the code but not in README's registry: {missing}"


def test_the_internal_exemptions_are_assigned_by_the_hooks_themselves():
    shims = "\n".join(p.read_text(encoding="utf-8") for p in HOOKS.iterdir()
                      if p.is_file() and p.suffix in (".sh", ".ps1"))
    for name in INTERNAL:
        assert re.search(rf"(?m)^\s*(?:\$env:)?{name}\s*=", shims), (
            f"{name} is exempted as shim plumbing but no hook assigns it")
        assert name not in DOCUMENTED, f"{name} is plumbing; document it or drop the exemption"


@pytest.mark.parametrize("name, module", sorted(THIRD_PARTY.items()))
def test_third_party_names_are_documented_and_really_touched(name, module):
    assert name in DOCUMENTED
    assert name in (SRC / module).read_text(encoding="utf-8")


def test_the_spec_registry_is_documented():
    assert sorted(SPEC_1_6 - DOCUMENTED) == []


def test_the_section_parser_reads_rows_and_stops_at_the_next_heading():
    """CANARY for the parser both directions lean on."""
    text = "\n".join([
        "## Something else", "| `MINDHIVE_OUTSIDE` | x |",
        SECTION, "", "| Variable | Default |", "|---|---|",
        "| `MINDHIVE_A` | 1 |", "| `OMP_NUM_THREADS` | 2 |", "text `MINDHIVE_PROSE`",
        "## Next", "| `MINDHIVE_AFTER` | 3 |"])
    assert documented_names(text) == {"MINDHIVE_A", "OMP_NUM_THREADS"}
    assert documented_names("no registry here") == set()


def test_the_code_scan_finds_the_names_it_must():
    """CANARY for the other direction: an empty scan would document nothing
    missing while checking nothing."""
    for name in ("MINDHIVE_HOME", "MINDHIVE_SESSION", "MINDNETWORK_ENFORCE",
                 "MINDHIVE_PYTHON", "MINDHIVE_PLUGIN_AUTOSYNC"):
        assert name in USED, f"the scan missed {name}"
    assert "MINDNET_FACULTY" not in USED        # a Python constant, not a variable
