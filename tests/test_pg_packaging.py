# tests/test_pg_packaging.py
"""The migrations have to survive packaging, and the extras have to stay extra.

Two failures this file exists to prevent, both of which are invisible in a
checkout and only appear where they are hardest to debug:

**The `.sql` files disappearing from a build.** `[tool.setuptools.packages.find]`
finds *packages*, not data — it ships `.py` files and nothing else. Without an
explicit `package-data` entry the three migrations are absent from any wheel and
from the frozen exe, and `apply_migrations` then finds **zero migrations and
reports success**, because "no pending migrations" and "no migrations at all"
look identical to it. The operator gets an empty database and a green command.

**The optional extras stopping being optional.** `dependencies = []` is what lets
`hive`, the dashboard, the hooks and the whole default test tier run on a machine
with neither `psycopg` nor the MCP SDK — which is this machine, and CI. A driver
that drifted into the base dependency list would be discovered as a broken
install, not as a failing test.

The reachability half is asserted through `importlib.resources`, never through a
path computed from `__file__`: the frozen build has no source directory to walk,
and a migration runner that works only from a checkout fails exactly where an
operator is least able to fix it.
"""
import tomllib
from importlib import resources
from pathlib import Path

import pytest

from mindhive import pgmirror

REPO = Path(__file__).resolve().parents[1]
PKG = "mindhive.pgmigrations"


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# reachable through the package, not through a relative path
# --------------------------------------------------------------------------

def test_the_migrations_are_reachable_through_the_package():
    """`importlib.resources`, so this keeps working in a wheel and in the frozen
    build where `__file__` points inside a bundle."""
    names = sorted(p.name for p in resources.files(PKG).iterdir()
                   if p.name.endswith(".sql"))
    assert names == [
        "001_schema.sql", "002_indexes.sql", "003_roles_and_views.sql",
        "004_mirror_state_view_and_ownership.sql",
        "005_drop_unmatchable_order_index.sql",
        "006_event_embeddings.sql",
        "007_event_embeddings_hashes.sql"]


def test_every_migration_has_readable_sql_behind_it():
    """A discoverable name with unreadable bytes is the same outage as a missing
    file, one step later."""
    for name in pgmirror.migration_files():
        text = pgmirror.migration_text(name)
        assert text.strip(), name
        assert len(pgmirror.migration_sha256(name)) == 64, name


def test_the_inventory_is_not_empty_and_the_emptiness_would_be_noticed():
    """CANARY for the whole file. Every assertion here is about a list, and an
    empty list satisfies most of them — which is precisely the packaging failure
    being guarded against: `apply_migrations` over zero files reports success."""
    assert len(pgmirror.migration_files()) == 7
    assert pgmirror.migration_files() == sorted(pgmirror.migration_files()), \
        "lexicographic filename order IS the application order"


def test_the_loader_does_not_reach_the_files_through_dunder_file():
    """The specific mechanism, asserted structurally: a `Path(__file__).parent`
    lookup passes every test above in a checkout and ships a build that cannot
    find its own schema."""
    import ast
    src = (REPO / "src" / "mindhive" / "pgmirror.py").read_text(encoding="utf-8")
    # Over the AST, not the text: the module's own docstring names `__file__` to
    # explain why it does not use it, and a substring scan would forbid the
    # explanation along with the mistake.
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert "__file__" not in names
    assert "resources.files" in src


# --------------------------------------------------------------------------
# declared as package data, so a build carries them
# --------------------------------------------------------------------------

def test_the_sql_files_are_declared_as_package_data():
    """The gap this test was written for: without this entry the migrations are
    in the repo and absent from every artefact built from it."""
    data = _pyproject()["tool"]["setuptools"].get("package-data", {})
    assert data, "no [tool.setuptools.package-data]: a wheel would ship no SQL"
    patterns = data.get("mindhive.pgmigrations") or data.get("*") or []
    assert any(p.endswith(".sql") for p in patterns), patterns


def test_every_migration_on_disk_matches_a_declared_pattern():
    """Closes the loop between the declaration and the actual files: a pattern
    of `*.psql`, or a package name with a typo, would satisfy the test above
    while shipping nothing."""
    import fnmatch
    data = _pyproject()["tool"]["setuptools"]["package-data"]
    patterns = data.get("mindhive.pgmigrations", []) + data.get("*", [])
    for name in pgmirror.migration_files():
        assert any(fnmatch.fnmatch(name, p) for p in patterns), \
            f"{name} is shipped by no declared pattern: {patterns}"


def test_the_migration_package_is_found_by_the_package_finder():
    """`packages.find` must actually see `mindhive.pgmigrations` — package-data
    for a package that is not packaged ships nothing at all."""
    assert (REPO / "src" / "mindhive" / "pgmigrations" / "__init__.py").is_file(), \
        "without __init__.py setuptools does not treat this as a package"
    assert _pyproject()["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]


# --------------------------------------------------------------------------
# the extras stay extra
# --------------------------------------------------------------------------

def test_the_base_install_still_declares_no_runtime_dependencies():
    """The property the whole default test tier rests on, and the one a careless
    `pip install` of a driver into the wrong list would end."""
    assert _pyproject()["project"]["dependencies"] == []


def test_the_two_new_extras_exist_and_are_separate():
    """Two rather than one, because they fail at different times and want
    different messages — a machine can legitimately have the driver and not the
    SDK, and telling it to install the wrong one costs a debugging session."""
    extras = _pyproject()["project"]["optional-dependencies"]
    assert "pg" in extras and "mcp" in extras
    assert any("psycopg" in d for d in extras["pg"])
    assert any(d.split(">")[0].split("[")[0].strip() == "mcp"
               for d in extras["mcp"])
    assert not any("psycopg" in d for d in extras["mcp"]), \
        "the extras must be installable independently"


def test_the_pg_extra_pins_a_version_range_rather_than_floating():
    """`psycopg[binary]` is the wheel that has to exist for cp314 on Windows. An
    unpinned major would let a future 4.x land silently on the one platform this
    repo runs on."""
    pg = _pyproject()["project"]["optional-dependencies"]["pg"]
    psycopg = next(d for d in pg if "psycopg" in d and "pool" not in d)
    assert ">=" in psycopg and "<" in psycopg, psycopg


@pytest.mark.parametrize("extra", ["vectors", "dev", "build", "browser"])
def test_the_existing_extras_were_not_disturbed(extra):
    assert extra in _pyproject()["project"]["optional-dependencies"]
