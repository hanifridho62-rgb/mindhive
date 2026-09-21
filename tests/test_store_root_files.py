# tests/test_store_root_files.py
"""The store root's own FILES, and the project names that used to stand on them.

A namespace is a directory directly under `paths.store_home()`, and so is every
file the store keeps for itself: `LIVE_LOCK`, the `autosync.json` registry, the
dashboard token, the global mandatory-SOP sidecar, ... Before
`paths.STORE_ROOT_FILES`, a project named after one of those files was a valid
namespace, and neither half failed loudly:

* `append_event(project="LIVE_LOCK")` minted `LIVE_LOCK/events/`, and
  `guard.live_lock_active` -- an `exists()` -- read the directory as a held lock,
  so every `hive gc --prune` and `reflect` refused from then on;
* a project named `autosync.json` made the registry a directory: `known_projects`
  read an empty allow-list and `autosync.register` raised.

Pinned in three layers, each with its canary:

1. the SINK -- `paths.namespace_name` quarantines those names in any case, on
   every platform, and leaves their neighbours alone;
2. the loud surface -- `cliparse.project_arg` refuses them by asking the sink
   (the dashboard's `is_safe_project` asks the same question; see
   `tests/test_dashboard_query.py`);
3. the WRITERS -- every root file is reached through `paths.store_file`, which
   refuses an undeclared name, and the AST guard below fails on a join onto
   `store_home()` that goes around it. That guard is what declares the next
   root file: on the first run of the suite, not after a project collides.
"""
import argparse
import ast
from pathlib import Path

import pytest

from mindhive import (autosync, cliparse, consolidate, dashboard, events, guard,
                      harvest, hookcapture, paths, research, sop, workspace)
# `tests/` is a package (`tests/__init__.py`), so the suite's own conftest has
# an importable name and its helpers can be exercised directly.
from tests import conftest

SRC = Path(__file__).resolve().parents[1] / "src" / "mindhive"

DECLARED = sorted(paths.STORE_ROOT_FILES)
PER_RUN = ["research_run_1a2b3c4d.json", "research_runs_run_learnings.md"]


# --- 1. the sink ----------------------------------------------------------------

@pytest.mark.parametrize("os_name", ["nt", "posix"])
@pytest.mark.parametrize("spell", [str, str.lower, str.upper],
                         ids=["declared", "lower", "upper"])
@pytest.mark.parametrize("name", DECLARED + PER_RUN)
def test_a_store_root_file_name_is_never_a_namespace(name, spell, os_name):
    """DISCRIMINATING: every one of these matches the project whitelist, so the
    root-file rule is the only thing quarantining it. Any case, because NTFS
    and APFS ignore case -- `Live_Lock` IS `LIVE_LOCK` there -- and on every
    platform, so a store that moves between machines means the same thing."""
    raw = spell(name)
    assert paths.PROJECT_RE.fullmatch(raw)                     # armed
    kept = paths.namespace_name(raw, os_name=os_name)
    assert kept != raw
    assert kept.startswith(paths.QUARANTINE_PREFIX)


@pytest.mark.parametrize("name", [
    "live-lock", "LIVE_LOCKS", "autosync", "autosync-json", "harvest.lock2",
    "dashboard", "research-run", "research_run", "research_runsx",
])
def test_canary_a_neighbour_of_a_root_file_name_is_still_a_namespace(name):
    """CANARY: a rule that quarantined by substring, or every name with a dot or
    an underscore, would pass the test above and break real projects."""
    for os_name in ("nt", "posix"):
        assert paths.namespace_name(name, os_name=os_name) == name


def test_a_session_may_carry_a_root_file_name():
    """Deliberately NOT part of `is_reserved_component`, which segments share: a
    session's segment lives inside `events/`, nowhere near the store root."""
    assert events.segment_name("LIVE_LOCK") == "LIVE_LOCK.jsonl"
    assert events.segment_name("autosync.json") == "autosync.json.jsonl"


# --- store_file ----------------------------------------------------------------

@pytest.mark.parametrize("name", DECLARED + PER_RUN)
def test_store_file_is_the_declared_file_at_the_store_root(name):
    assert paths.store_file(name) == paths.store_home() / name


@pytest.mark.parametrize("name", [
    "notes.json", "STATE.md", "events", "autosync.JSON", "live_lock",
    "../autosync.json", "research_run_a/b", "research_run_a\\b", ".autosync.json",
    "", None, 5,
], ids=["undeclared", "state-md", "events", "wrong-case", "wrong-case-lock",
        "traversal", "slash-in-run-id", "backslash-in-run-id", "leading-dot",
        "empty", "null", "int"])
def test_store_file_refuses_a_name_that_is_not_declared(name):
    """Spelled exactly, as one component. An undeclared root file is the
    collision this file is about, waiting for a project to be named after it."""
    with pytest.raises(ValueError):
        paths.store_file(name)


ACCESSORS = [
    ("autosync.json", autosync._registry_path),
    ("dashboard.token", dashboard._token_path),
    ("harvest.lock", harvest._harvest_lock_path),
    ("consolidate_marker.json", consolidate._marker_path),
    ("workspace_profiles.json", workspace._profiles_path),
    ("workspace_board.json", workspace._board_path),
    ("mandatory_sops.json", sop.global_sidecar_path),
    ("research_runs.json", research.leaderboard_path),
    ("research_run_1a2b3c4d.json", lambda: research.leaderboard_path("1a2b3c4d")),
]


@pytest.mark.parametrize("name, accessor", ACCESSORS,
                         ids=[name for name, _a in ACCESSORS])
def test_every_root_file_accessor_resolves_to_its_declared_name(name, accessor):
    assert accessor() == paths.store_file(name)


def test_a_hook_failure_is_logged_to_the_declared_root_file():
    hookcapture.log_hook_error("probe-stage", RuntimeError("boom"))
    text = paths.store_file("hooks_errors.log").read_text(encoding="utf-8")
    assert "probe-stage" in text and "RuntimeError: boom" in text


# --- end to end: the two collisions that shipped --------------------------------

@pytest.mark.parametrize("project", ["LIVE_LOCK", "live_lock"])
def test_a_project_named_after_the_live_lock_does_not_hold_it(project):
    """DISCRIMINATING. The directory this append used to mint stood exactly where
    `guard.live_lock_active` looks, and every prune and reflect refused."""
    events.append_event("decision", {"text": "a decision under a hostile name"},
                        session_id="probe-sess", project=project)
    assert (paths.project_dir(project) / "events").is_dir()   # nothing was lost
    assert not paths.store_file("LIVE_LOCK").exists()
    assert guard.live_lock_active() is False

    # CANARY: a real lock trips the same guard.
    paths.store_file("LIVE_LOCK").write_text("live", encoding="utf-8")
    assert guard.live_lock_active() is True


@pytest.mark.parametrize("project", ["autosync.json", "AUTOSYNC.JSON"])
def test_a_project_named_after_the_registry_leaves_it_writable(project):
    """DISCRIMINATING. The directory this append used to mint made
    `autosync.register` raise and `known_projects` read an empty allow-list."""
    events.append_event("decision", {"text": "a decision under a hostile name"},
                        session_id="probe-sess", project=project)
    assert not paths.store_file("autosync.json").is_dir()
    autosync.register("real-project", "C:/transcripts", "C:/work/real-project")
    assert paths.known_projects() == {"real-project"}


# --- 2. the loud surface --------------------------------------------------------

@pytest.mark.parametrize("name", ["LIVE_LOCK", "autosync.json",
                                  "Mandatory_Sops.JSON", "research_run_x.json"])
def test_the_cli_refuses_a_root_file_name_out_loud(name):
    with pytest.raises(argparse.ArgumentTypeError) as exc:
        cliparse.project_arg(name)
    assert "LIVE_LOCK or autosync.json" in str(exc.value)


def test_canary_the_cli_still_accepts_a_neighbouring_name():
    assert cliparse.project_arg("live-lock") == "live-lock"


# --- 3. the writers: no join onto the store root around `store_file` -------------

# The two functions allowed to join onto `store_home()`: the file accessor, and
# the namespace accessor, whose join runs through `namespace_name`.
ALLOWED = {("paths.py", "project_dir"), ("paths.py", "store_file")}

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _is_store_home_call(node) -> bool:
    """`store_home()`, bare or as an attribute (`paths.store_home()`)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return ((isinstance(func, ast.Name) and func.id == "store_home")
            or (isinstance(func, ast.Attribute) and func.attr == "store_home"))


def _own_nodes(scope):
    """Every node in *scope* that belongs to it rather than to a function or a
    class defined inside it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _root_joins(source) -> list:
    """``(scope name, line)`` for every join onto the store root in *source*:
    `/` with the root on its left, `.joinpath` on it, or `os.path.join` with it
    first -- where the root is a `store_home()` call or a name the same scope
    bound to one (plain, annotated or walrus assignment)."""
    tree = ast.parse(source)
    hits = []
    for scope in [tree] + [n for n in ast.walk(tree) if isinstance(n, _SCOPES)]:
        nodes = list(_own_nodes(scope))
        aliases = set()
        for node in nodes:
            if isinstance(node, ast.Assign) and _is_store_home_call(node.value):
                aliases.update(t.id for t in node.targets
                               if isinstance(t, ast.Name))
            elif (isinstance(node, (ast.AnnAssign, ast.NamedExpr))
                  and node.value is not None
                  and _is_store_home_call(node.value)
                  and isinstance(node.target, ast.Name)):
                aliases.add(node.target.id)

        def is_root(expr, aliases=aliases):
            return _is_store_home_call(expr) or (
                isinstance(expr, ast.Name) and expr.id in aliases)

        name = getattr(scope, "name", "<module>")
        for node in nodes:
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                    and is_root(node.left)):
                hits.append((name, node.lineno))
            elif (isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)):
                if node.func.attr == "joinpath" and is_root(node.func.value):
                    hits.append((name, node.lineno))
                elif (node.func.attr == "join" and node.args
                      and is_root(node.args[0])):
                    hits.append((name, node.lineno))
    return hits


def test_no_module_joins_onto_the_store_root_around_store_file():
    """The drift guard. A new root file written as `store_home() / "x.json"` is
    the next collision; it has to be declared in `paths.STORE_ROOT_FILES` and
    reached through `paths.store_file`, and this is where that is found out.

    ARMED by its own second assertion: the two sanctioned joins must be SEEN, so
    a scan that matched nothing cannot pass as a clean tree."""
    offenders, seen = [], set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for scope, line in _root_joins(path.read_text(encoding="utf-8")):
            if (rel, scope) in ALLOWED:
                seen.add((rel, scope))
            else:
                offenders.append(f"src/mindhive/{rel}:{line} in {scope}")
    assert not offenders, (
        "joins onto store_home() outside paths.store_file / paths.project_dir "
        "-- declare the file in paths.STORE_ROOT_FILES and reach it through "
        "paths.store_file: " + ", ".join(offenders))
    assert seen == ALLOWED


@pytest.mark.parametrize("source", [
    "def f():\n    return paths.store_home() / 'x'\n",
    "def f():\n    return store_home() / 'x'\n",
    "def f():\n    return paths.store_home().joinpath('x')\n",
    "def f():\n    return os.path.join(paths.store_home(), 'x')\n",
    "def f():\n    home = paths.store_home()\n    return home / 'x'\n",
    "def f():\n    home: Path = paths.store_home()\n    return home / 'x'\n",
    "def f():\n    if (home := paths.store_home()):\n        return home / 'x'\n",
    "HOME = store_home()\nLOCK = HOME / 'x'\n",
    "g = lambda: paths.store_home() / 'x'\n",
    "class C:\n    def m(self):\n        return paths.store_home() / 'x'\n",
], ids=["div", "bare-div", "joinpath", "os-path-join", "alias",
        "annotated-alias", "walrus", "module-level", "lambda", "method"])
def test_canary_the_guard_sees_every_spelling_of_a_root_join(source):
    assert _root_joins(source)


@pytest.mark.parametrize("source", [
    "def f():\n    return paths.store_file('LIVE_LOCK')\n",
    "def f():\n    return paths.project_dir('p') / 'events'\n",
    "def f():\n    home = paths.store_home()\n    return sorted(home.iterdir())\n",
    "def f():\n    paths.store_home().mkdir(parents=True, exist_ok=True)\n",
    "def f():\n    home = paths.store_home()\n\n\ndef g(home):\n    return home / 'x'\n",
], ids=["store-file", "project-dir", "iterdir", "mkdir",
        "alias-in-another-scope"])
def test_canary_the_guard_stays_silent_on_sanctioned_uses(source):
    assert _root_joins(source) == []


# --------------------------------------------------------------------------
# The REPO root's turn: `tests/conftest.py::repo_root_stays_clean`
# --------------------------------------------------------------------------
#
# Everything above guards the STORE root. The checkout has the same problem
# from the other direction -- pytest's working directory IS the checkout, so
# a relative path under test, or a subprocess spawned without `cwd=`, writes
# straight into someone's working copy. The session fixture watches for it;
# these canaries are what keep the watching honest, because a snapshot
# comparison that always returned the empty set would pass forever.

def test_canary_the_repo_root_guard_sees_a_new_file_and_a_new_directory(tmp_path):
    (tmp_path / "already-here.txt").write_text("x", encoding="utf-8")
    before = conftest.repo_root_entries(tmp_path)
    (tmp_path / "b.bin").write_bytes(b"\x00\x01main")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "in.txt").write_text("x\n", encoding="utf-8")
    new = sorted(conftest.repo_root_entries(tmp_path) - before)
    # `d/in.txt` is NOT listed separately: the check is top-level names, and a
    # stray nested file always arrives inside a new top-level directory.
    assert new == ["b.bin", "d"]


def test_canary_the_repo_root_guard_ignores_the_tooling_churn(tmp_path):
    """A default run writes `.pytest_cache`; `-p no:cacheprovider` does not.
    A guard that disagreed with itself between those two spellings would be
    noise, and noise is what gets a guard deleted."""
    before = conftest.repo_root_entries(tmp_path)
    for name in ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"):
        (tmp_path / name).mkdir()
    (tmp_path / ".coverage.host.12345").write_text("", encoding="utf-8")
    assert conftest.repo_root_entries(tmp_path) == before


def test_canary_the_repo_root_guard_survives_a_root_that_is_not_there(tmp_path):
    """It must never be the thing that fails a run."""
    assert conftest.repo_root_entries(tmp_path / "nope") == set()


def test_the_repo_root_guard_is_autouse_for_the_whole_session():
    """Scope is the design decision, not an accident: per-test would name the
    offending test but pay one listing per test to find a thing that has
    happened once."""
    # pytest 9 wraps a fixture in a FixtureFunctionDefinition and hangs the
    # marker off `_fixture_function_marker`; `_pytestfixturefunction` was the
    # pytest 8 spelling and is gone.
    marker = conftest.repo_root_stays_clean._fixture_function_marker
    assert marker.scope == "session"
    assert marker.autouse is True
    assert conftest.REPO_ROOT == Path(__file__).resolve().parents[1]
