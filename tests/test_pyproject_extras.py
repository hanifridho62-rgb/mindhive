"""Spec section 1.3 (plan RTM rows 1.1-1.5): every optional extra the spec's
dependency table names exists in `pyproject.toml` with at least the floor the
table gives, and the runtime itself needs none of them.

The spec's premise is "Zero Mandatory Dependencies": `dependencies = []` on
purpose, so the ledger, the hash chain, the projections and the hooks run on
the standard library alone, and each heavier capability arrives through its own
extra. That premise is the first thing pinned; the floors come after it.

One row of the table is deliberately not reproduced. The spec pins
`numpy >= 1.24.0, < 2.0.0` inside `vectors`; numpy 1.x publishes no wheels for
Python 3.13 or later, and this repository is developed and tested on 3.14, so
that ceiling would make `pip install -e .[vectors]` fail on the one machine the
project runs on. numpy arrives transitively through chromadb and
sentence-transformers, whichever major they support. Pinned below: no extra
caps numpy under 2, so the deviation cannot be reverted by accident.
"""
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
EXTRAS = PROJECT["optional-dependencies"]

# Spec 1.3's table: extra -> {distribution: minimum version}.
SPEC_FLOORS = {
    "vectors": {"chromadb": "0.5.0", "sentence-transformers": "2.2.0"},
    "dev": {"pytest": "8.0.0"},
    "build": {"pyinstaller": "6.0.0"},
    "tui": {"rich": "13.0.0"},
    "browser": {"playwright": "1.40.0"},
    "pg": {"psycopg": "3.3.0", "psycopg_pool": "3.2.0"},
    "mcp": {"mcp": "1.2.0"},
}
REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")


def _version(text: str) -> tuple:
    parts = [int(p) for p in text.strip().split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def requirement_floor(requirement: str):
    """``(name, floor tuple or None, specifier text)`` of one requirement string."""
    m = REQUIREMENT.match(requirement)
    if not m:
        raise ValueError(f"not a requirement: {requirement!r}")
    name, spec = m.group(1).lower().replace("_", "-"), m.group(3)
    floors = re.findall(r">=\s*([0-9][0-9.]*)", spec)
    return name, (_version(floors[0]) if floors else None), spec


def floor_problem(extra: str, dist: str, minimum: str):
    """None when `extra` requires `dist` with a floor at or above `minimum`."""
    want = dist.lower().replace("_", "-")
    for req in EXTRAS.get(extra, []):
        name, floor, _ = requirement_floor(req)
        if name == want:
            if floor is None:
                return f"{extra}: {req!r} has no lower bound"
            if floor < _version(minimum):
                return f"{extra}: {req!r} is below the spec's floor {minimum}"
            return None
    return f"{extra} does not require {dist}"


def test_the_runtime_needs_no_extra():
    assert PROJECT["dependencies"] == []


def test_python_below_3_11_is_refused():
    assert PROJECT["requires-python"] == ">=3.11"


def test_every_extra_the_spec_names_exists():
    assert set(SPEC_FLOORS) <= set(EXTRAS), sorted(set(SPEC_FLOORS) - set(EXTRAS))


@pytest.mark.parametrize("extra, dist, minimum",
                         [(e, d, m) for e, floors in SPEC_FLOORS.items()
                          for d, m in floors.items()])
def test_each_extra_carries_the_spec_floor(extra, dist, minimum):
    assert floor_problem(extra, dist, minimum) is None


def test_the_postgres_driver_stays_below_its_next_major():
    """Spec: `psycopg[binary] >= 3.3.0, < 4.0.0` -- the binary build, bounded."""
    (req,) = [r for r in EXTRAS["pg"] if r.lower().startswith("psycopg[")]
    assert "[binary]" in req and "<4" in req.replace(" ", "")


def test_no_extra_caps_numpy_below_2():
    capped = [r for reqs in EXTRAS.values() for r in reqs
              if requirement_floor(r)[0] == "numpy" and "<2" in r.replace(" ", "")]
    assert capped == []


def test_readme_gives_the_spec_install_line():
    """Spec 1.3 closes on the one command a developer runs for v1.0 + v1.1."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "pip install -e .[vectors,dev,tui]" in readme


@pytest.mark.parametrize("extra, dist, minimum", [
    ("vectors", "chromadb", "9.0"),        # floor too low for the demand
    ("dev", "chromadb", "0.5"),            # not in that extra
    ("nonexistent", "rich", "13"),         # no such extra
])
def test_the_floor_check_rejects_what_it_should(extra, dist, minimum):
    """CANARY: the per-row checks pass only because this checker can fail."""
    assert floor_problem(extra, dist, minimum) is not None


def test_the_requirement_parser_reads_extras_markers_and_bounds():
    assert requirement_floor("psycopg[binary]>=3.3,<4")[:2] == ("psycopg", (3, 3, 0))
    assert requirement_floor("psycopg_pool>=3.2")[:2] == ("psycopg-pool", (3, 2, 0))
    assert requirement_floor("rich")[:2] == ("rich", None)
