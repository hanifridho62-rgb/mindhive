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


# --- the rollback (U10 / WB5.5) ------------------------------------------------
#
# The only function in this package that can DELETE an agent's output, which is
# the same hazard `test_release_keeps_a_lane_that_holds_work` above exists for.
# The guard tests are therefore the load-bearing ones here, not the happy path:
# every path `is_lane_path` rejects is a directory somebody would otherwise be
# about to discard the contents of.


def _lane(repo, lane_id=0, mission="m1"):
    got = lw.acquire(repo, lane_id, mission)
    assert got.ok, got.reason
    return got


def test_a_rollback_discards_the_half_written_work_a_failed_attempt_left(repo):
    """The point of the whole thing: attempt two must read a tree at its last
    commit, not one carrying whatever attempt one left behind."""
    lane = _lane(repo)
    path = repo / ".worktrees" / "lane-0"
    (path / "half-written.txt").write_text("partial\n", encoding="utf-8")
    (path / "seed.txt").write_text("clobbered\n", encoding="utf-8")

    got = lw.rollback(repo, lane)

    assert got.ok and got.changed, got.reason
    assert not (path / "half-written.txt").exists()
    assert (path / "seed.txt").read_text(encoding="utf-8") == "seed\n"
    assert "discarded" in got.reason


def test_a_rollback_keeps_every_commit_the_lane_made(repo):
    """`checkout -- .` and `clean -fd` discard what was never committed. Work
    the agent committed is work it decided to keep, and this must not be the
    thing that throws it away."""
    lane = _lane(repo)
    path = repo / ".worktrees" / "lane-0"
    (path / "kept.txt").write_text("committed work\n", encoding="utf-8")
    _git(path, "add", "kept.txt")
    _git(path, "commit", "-qm", "the agent's own commit")

    got = lw.rollback(repo, lane)

    assert got.ok and not got.changed, got.reason
    assert (path / "kept.txt").exists(), "a committed file was discarded"
    assert "already clean" in got.reason


def test_a_clean_lane_reports_that_nothing_was_discarded(repo):
    """`changed` is the field a mission report reads to say whether work was
    thrown away. A clean rollback that claimed it discarded something would put
    a loss in the report that never happened."""
    got = lw.rollback(repo, _lane(repo))
    assert got.ok and not got.changed
    assert "already clean" in got.reason


def test_the_main_checkout_is_refused_rather_than_discarded(repo):
    """The failure this guard exists for. A caller that hands the repository
    root to a function whose job is `clean -fd` deletes the developer's tree."""
    (repo / "uncommitted.txt").write_text("mine\n", encoding="utf-8")

    got = lw.rollback(repo, lw.LaneWorktree(lane_id=0, path=str(repo)))

    assert not got.ok and not got.changed
    assert "discarding nothing" in got.reason
    assert (repo / "uncommitted.txt").exists()


def test_the_incumbents_task_keyed_worktree_is_refused(repo):
    """`.claude/worktrees/<task>` is the incumbent's namespace, not a lane. A
    guard that only checked "is it under the repo" would discard those too."""
    other = repo / ".claude" / "worktrees" / "some-task"
    other.mkdir(parents=True)
    (other / "work.txt").write_text("not mine to delete\n", encoding="utf-8")

    got = lw.rollback(repo, lw.LaneWorktree(lane_id=0, path=str(other)))

    assert not got.ok
    assert (other / "work.txt").exists()


@pytest.mark.parametrize("name", ["notalane", "lane-", "lane-x", "lane-1x",
                                  "LANE-1"])
def test_a_directory_that_merely_sits_beside_the_lanes_is_refused(repo, name):
    target = repo / ".worktrees" / name
    target.mkdir(parents=True)
    assert lw.is_lane_path(repo, target) is False
    assert lw.rollback(repo, lw.LaneWorktree(lane_id=0,
                                             path=str(target))).ok is False


def test_the_lane_directory_itself_is_not_a_lane(repo):
    """`.worktrees` holds every lane. Rolling IT back would discard all of them
    at once."""
    (repo / ".worktrees").mkdir(exist_ok=True)
    assert lw.is_lane_path(repo, repo / ".worktrees") is False


def test_a_real_lane_path_is_accepted(repo):
    """The positive half. Without it every rejection above passes for a
    predicate that answers False to everything."""
    assert lw.is_lane_path(repo, lw.lane_path_for(repo, 0)) is True
    assert lw.is_lane_path(repo, lw.lane_path_for(repo, 12)) is True


def test_a_file_inside_a_lane_is_not_itself_a_lane(repo):
    assert lw.is_lane_path(repo, lw.lane_path_for(repo, 0) / "src") is False


def test_a_rollback_without_a_path_is_refused_rather_than_guessed(repo):
    got = lw.rollback(repo, lw.LaneWorktree(lane_id=3))
    assert not got.ok and got.lane_id == 3
    assert "no path" in got.reason


def test_a_rollback_of_a_lane_that_is_not_on_disk_answers_a_reason(repo):
    """Total: it answers a value, never raises. The path passes the guard and
    git then refuses, which is a different fact from a refused guard."""
    got = lw.rollback(repo, lw.LaneWorktree(lane_id=9,
                                            path=str(lw.lane_path_for(repo, 9))))
    assert not got.ok and not got.changed
    assert "git status failed" in got.reason


def test_the_git_calls_belong_to_the_incumbent_here_too(monkeypatch, repo):
    """Same rule as `acquire`: a second git driver drifting from the first is
    how an agent's work gets deleted by code that looks correct."""
    from mindhive.mindnet import worktree as mn_wt

    seen = []

    def fake_git(cwd, *args):
        seen.append(tuple(str(a) for a in args))
        return (False, "", "patched")

    lane = _lane(repo)                                  # acquired on REAL git
    monkeypatch.setattr(mn_wt, "_git", fake_git)
    got = lw.rollback(repo, lane)

    assert not got.ok and "patched" in got.reason
    assert seen == [("status", "--porcelain")], (
        "rollback issued its own git command, or ran clean after a failed status")


def test_a_failing_checkout_stops_before_the_clean(monkeypatch, repo):
    """Ordering matters: `clean -fd` after a failed `checkout` would delete
    untracked files while leaving the tracked ones half-restored."""
    from mindhive.mindnet import worktree as mn_wt

    seen = []
    real = mn_wt._git

    def fake_git(cwd, *args):
        seen.append(args[0])
        if args[0] == "checkout":
            return (False, "", "checkout refused")
        return real(cwd, *args)

    lane = _lane(repo)
    (repo / ".worktrees" / "lane-0" / "dirty.txt").write_text("x\n",
                                                             encoding="utf-8")
    monkeypatch.setattr(mn_wt, "_git", fake_git)
    got = lw.rollback(repo, lane)

    assert not got.ok
    assert "git checkout failed" in got.reason
    assert "clean" not in seen
    assert (repo / ".worktrees" / "lane-0" / "dirty.txt").exists()


def test_the_incumbent_renaming_its_private_seam_is_reported_not_raised(
        monkeypatch, repo):
    """`rollback` reaches for `mn_wt._git` and `mn_wt._GIT_LOCK`, which are
    private and may be renamed without anyone thinking of this caller. Its
    docstring promises it answers a value and never raises, and it is called
    from inside a repair loop — the place least able to absorb a surprise."""
    from mindhive.mindnet import worktree as mn_wt

    lane = _lane(repo)
    monkeypatch.delattr(mn_wt, "_git")
    got = lw.rollback(repo, lane)

    assert not got.ok and not got.changed
    assert "cannot load the worktree driver" in got.reason
    assert "AttributeError" in got.reason
