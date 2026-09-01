# tests/test_orchestrator_worktree.py
"""WB4 — lane-scoped physical isolation at `.worktrees/lane-N`.

Written against
``docs/plans/2026-09-01-001-feat-lintester-orchestrator-full-prd-plan.md`` U5.

Real git throughout, matching `tests/test_mindnet_worktree.py`: the behaviour
under test IS git's, and a patched `subprocess.run` would encode a guess about
argv for the exact commands whose semantics are the question.

The assertion that matters most is `test_release_keeps_a_lane_that_holds_work`.
Every other failure here costs a rerun; that one silently deletes an agent's
output, which is the entire reason for running agents.
"""
import subprocess

import pytest

from mindhive.orchestrator import worktree as lw


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *[str(a) for a in args]],
                       capture_output=True, text=True, shell=False)
    if check:
        assert r.returncode == 0, f"git {' '.join(map(str, args))}: {r.stderr}"
    return r


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-qm", "seed")
    return root


# --- naming -------------------------------------------------------------------

def test_a_lane_resolves_to_the_path_the_prd_names(tmp_path):
    got = lw.lane_path_for(tmp_path, 2)
    assert got.as_posix().endswith(".worktrees/lane-2")
    assert got.parent.name == ".worktrees"


def test_two_lanes_never_resolve_to_one_path(tmp_path):
    paths = {lw.lane_path_for(tmp_path, i) for i in range(4)}
    assert len(paths) == 4


def test_the_lane_branch_namespace_is_distinct_from_the_incumbents():
    from mindhive.mindnet import worktree as mn_wt

    assert lw.lane_branch_for(0, "m1").startswith(f"{lw.BRANCH_PREFIX}/")
    assert not lw.lane_branch_for(0, "m1").startswith(f"{mn_wt.PREFIX}/")


# --- acquire / release --------------------------------------------------------

def test_acquire_creates_the_lane_checkout(repo):
    got = lw.acquire(repo, 0, "m1")
    assert got.ok, got.reason
    assert (repo / ".worktrees" / "lane-0").is_dir()
    assert got.branch == lw.lane_branch_for(0, "m1")
    assert got.base, "the base commit must be captured for the ahead check"


def test_acquire_then_release_leaves_the_origin_index_clean(repo):
    got = lw.acquire(repo, 0, "m1")
    assert got.ok, got.reason
    released = lw.release(repo, got)
    assert released.removed, released.reason
    assert not (repo / ".worktrees" / "lane-0").exists()
    porcelain = _git(repo, "status", "--porcelain").stdout.strip()
    assert porcelain == "", f"the parent repo was left dirty: {porcelain!r}"


def test_release_keeps_a_lane_that_holds_work(repo):
    """The one whose failure destroys an agent's output."""
    got = lw.acquire(repo, 1, "m1")
    assert got.ok, got.reason
    (repo / ".worktrees" / "lane-1" / "agent-output.txt").write_text(
        "work nobody has looked at\n", encoding="utf-8")

    released = lw.release(repo, got)
    assert released.kept and not released.removed
    assert (repo / ".worktrees" / "lane-1" / "agent-output.txt").exists()
    assert "lane-1" in released.reason


def test_releasing_twice_is_a_no_op_not_an_error(repo):
    got = lw.acquire(repo, 0, "m1")
    assert lw.release(repo, got).removed
    again = lw.release(repo, got)
    assert not again.removed and not again.kept
    assert "already gone" in again.reason


def test_a_failed_acquire_answers_a_reason_and_raises_nothing(tmp_path):
    got = lw.acquire(tmp_path / "not-a-repo", 0, "m1")
    assert not got.ok
    assert got.reason, "a refusal with no reason cannot be acted on"


def test_release_without_a_path_is_refused_rather_than_guessed(repo):
    got = lw.release(repo, lw.LaneWorktree(lane_id=3))
    assert not got.removed and "no path" in got.reason


def test_acquire_lanes_returns_one_entry_per_lane_including_failures(repo):
    trees = lw.acquire_lanes(repo, 3, "m1")
    assert [t.lane_id for t in trees] == [0, 1, 2]
    assert all(t.ok for t in trees), [t.reason for t in trees]
    assert len({t.path for t in trees}) == 3
    lw.release_lanes(repo, trees)


def test_release_lanes_skips_the_ones_that_were_never_acquired(repo):
    trees = [lw.acquire(repo, 0, "m1"), lw.LaneWorktree(lane_id=1, ok=False)]
    released = lw.release_lanes(repo, trees)
    assert [r.lane_id for r in released] == [0]


def test_acquire_lanes_of_zero_is_empty_not_an_error(repo):
    assert lw.acquire_lanes(repo, 0, "m1") == []


# --- delegation ---------------------------------------------------------------

def test_the_git_calls_belong_to_the_incumbent_not_this_module(monkeypatch, repo):
    """A second git driver is the regression the design's rejection guarded
    against. If this module ever stops delegating, patching the incumbent stops
    changing the answer — and that is what this asserts."""
    from mindhive.mindnet import worktree as mn_wt

    seen = {}

    def fake_add(root, task_id, path=None, branch=None):
        seen["args"] = (str(task_id), str(path), str(branch))
        return mn_wt.Isolated(task_id=str(task_id), path=str(path),
                              branch=str(branch), ok=False, reason="patched")

    monkeypatch.setattr(mn_wt, "add", fake_add)
    got = lw.acquire(repo, 2, "m9")
    assert got.reason == "patched", "acquire issued its own git command"
    assert seen["args"][0] == "lane-2"
    assert seen["args"][1].endswith("lane-2")
    assert seen["args"][2] == "lint/m9/lane-2"


def test_the_incumbent_default_naming_is_untouched(repo):
    """The optional path/branch must not have moved the task-keyed defaults."""
    from mindhive.mindnet import worktree as mn_wt

    got = mn_wt.add(repo, "t1")
    assert got.ok, got.reason
    assert got.path == str(mn_wt.path_for(repo, "t1"))
    assert got.branch == mn_wt.branch_for("t1")
    mn_wt.retire(repo, got.path, got.branch, got.base)
