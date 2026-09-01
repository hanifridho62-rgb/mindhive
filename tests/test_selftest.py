# tests/test_selftest.py
"""`hive selftest` — and WHICH checkout it actually tests.

The bug this file exists for is a false green, which is the worst kind: the
command reported a passing suite and the suite it ran was somebody else's.

`mindhive` is installed EDITABLE against the main checkout, so
``Path(__file__).resolve().parents[2]`` resolves there no matter which worktree
the operator is standing in. `hive selftest` run from a feature branch collected
the main checkout's tests, passed, and said so — with the branch's own tests
never executed and its own source never imported.

Both halves have to move together. Collecting the branch's tests while the child
still imports the installed package is the same failure one layer down: the
branch's assertions run against the main checkout's code.
"""
import os
import sys
from pathlib import Path

import pytest

from mindhive import paths
from mindhive import selftest


def _checkout(root: Path) -> Path:
    """The minimum `suite_root` accepts as a checkout: a suite and a package."""
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "src" / "mindhive").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def captured(monkeypatch):
    """`run_and_record`'s arguments, without running a suite."""
    seen = {}

    def _fake(argv=None, project=None, root=None, echo=True, emit=None,
              env=None):
        seen.update(argv=list(argv or ()), env=env, emit=emit, echo=echo)
        return {"exit_code": 0, "counts": {}, "error": "", "artifact": None,
                "excerpt": ""}

    from mindhive import verify
    monkeypatch.setattr(verify, "run_and_record", _fake)
    return seen


# --- which checkout ------------------------------------------------------------

def test_the_suite_that_runs_is_the_checkout_the_operator_is_standing_in(
        tmp_path, monkeypatch, captured):
    """The whole bug. A worktree's `hive selftest` must collect the worktree's
    tests, not the ones the editable install happens to point at."""
    branch = _checkout(tmp_path / "worktree")
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: branch)

    selftest.run(echo=False)

    assert captured["argv"][-1] == str(branch / "tests")


def test_the_installed_suite_is_used_outside_a_git_worktree(monkeypatch,
                                                            captured):
    """`worktree_root` answers None outside a repo, and a selftest that then
    ran nothing would be worse than one that runs the packaged suite."""
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: None)

    selftest.run(echo=False)

    assert captured["argv"][-1] == str(selftest.packaged_root() / "tests")


def test_a_git_repository_that_is_not_a_mindhive_checkout_is_not_run(
        tmp_path, monkeypatch, captured):
    """`hive` is on PATH everywhere. Run from an unrelated repository it must
    not point pytest at that repository's tests."""
    stranger = tmp_path / "someone-elses-repo"
    (stranger / "tests").mkdir(parents=True)            # a suite, but not ours
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: stranger)

    selftest.run(echo=False)

    assert captured["argv"][-1] == str(selftest.packaged_root() / "tests")


def test_a_checkout_with_no_suite_falls_back_rather_than_reporting_zero_tests(
        tmp_path, monkeypatch, captured):
    bare = tmp_path / "no-tests"
    (bare / "src" / "mindhive").mkdir(parents=True)
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: bare)

    assert selftest.suite_root() == selftest.packaged_root()


def test_the_packaged_root_is_the_one_this_module_was_installed_from():
    """The fall-back's own definition, pinned. If it ever stops being the
    package's own parent the fall-back silently changes meaning."""
    assert selftest.packaged_root() == Path(
        selftest.__file__).resolve().parents[2]


# --- which SOURCE the suite imports ---------------------------------------------

def test_the_child_imports_the_checkout_under_test_not_the_installed_package(
        tmp_path, monkeypatch, captured):
    """The second half of the same false green. Collecting the branch's tests
    while the child imports the editable install runs the branch's assertions
    against the main checkout's code."""
    branch = _checkout(tmp_path / "worktree")
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: branch)

    selftest.run(echo=False)

    first = captured["env"]["PYTHONPATH"].split(os.pathsep)[0]
    assert first == str(branch / "src")


def test_an_existing_pythonpath_is_prepended_to_rather_than_replaced(
        tmp_path, monkeypatch, captured):
    """Whatever the operator set is still theirs; the checkout only has to win
    the lookup, not evict everything else."""
    branch = _checkout(tmp_path / "worktree")
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: branch)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "theirs"))

    selftest.run(echo=False)

    assert captured["env"]["PYTHONPATH"].split(os.pathsep) == [
        str(branch / "src"), str(tmp_path / "theirs")]


def test_the_rest_of_the_environment_reaches_the_child_unchanged(
        tmp_path, monkeypatch, captured):
    branch = _checkout(tmp_path / "worktree")
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: branch)
    monkeypatch.setenv("MINDHIVE_MARKER", "kept")

    selftest.run(echo=False)

    assert captured["env"]["MINDHIVE_MARKER"] == "kept"


def test_a_checkout_with_no_src_directory_leaves_the_environment_alone(
        tmp_path, monkeypatch, captured):
    """`None` means "inherit", which is what a wheel install wants: there is no
    `<root>/src` to point at and inventing one would break the import it was
    meant to fix."""
    monkeypatch.setattr(selftest, "packaged_root", lambda: tmp_path / "wheel")
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: None)

    selftest.run(echo=False)

    assert captured["env"] is None


# --- the seam is still the one the callers use ----------------------------------

def test_the_streamed_output_still_reaches_both_readers(tmp_path, monkeypatch,
                                                        captured):
    """`echo` is the terminal, `emit` is the dashboard's job pane. A root fix
    that dropped either would empty a pane nobody is watching a log in."""
    monkeypatch.setattr(paths, "worktree_root",
                        lambda cwd=None: _checkout(tmp_path / "wt"))

    def sink(line):                                     # pragma: no cover - identity
        pass

    selftest.run(emit=sink, echo=False)

    assert captured["emit"] is sink
    assert captured["echo"] is False


def test_the_child_is_the_running_interpreter_not_a_bare_python(tmp_path,
                                                                monkeypatch,
                                                                captured):
    """`python` is not on PATH in this environment's Git Bash, and the venv
    interpreter is the one with the dependencies."""
    monkeypatch.setattr(paths, "worktree_root",
                        lambda cwd=None: _checkout(tmp_path / "wt"))

    selftest.run(echo=False)

    assert captured["argv"][:3] == [sys.executable, "-m", "pytest"]
