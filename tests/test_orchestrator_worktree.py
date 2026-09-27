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
import os
import subprocess
from pathlib import Path

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


# --- K13: the lane's tests are read-only while a child works in it ------------

def _mode(path):
    import stat
    return bool(path.stat().st_mode & stat.S_IWRITE)


def test_the_lock_clears_the_write_bit_on_the_lanes_tests(tmp_path):
    lane = tmp_path / "lane"
    (lane / "tests" / "sub").mkdir(parents=True)
    keep = lane / "tests" / "test_a.py"
    deep = lane / "tests" / "sub" / "test_b.py"
    for target in (keep, deep):
        target.write_text("x = 1\n", encoding="utf-8")

    assert lw.lock_tests_readonly(lane) == ("tests/sub/test_b.py",
                                            "tests/test_a.py")
    assert not _mode(keep) and not _mode(deep)
    assert lw.unlock_tests(lane)
    assert _mode(keep) and _mode(deep)


def test_the_lock_leaves_the_directory_itself_writable(tmp_path):
    """Only the files. A read-only `tests/` stops pytest writing
    `__pycache__` there, which breaks the suite the lane is judged by rather
    than slowing down the child editing it."""
    lane = tmp_path / "lane"
    (lane / "tests").mkdir(parents=True)
    (lane / "tests" / "test_a.py").write_text("x = 1\n", encoding="utf-8")

    lw.lock_tests_readonly(lane)
    try:
        (lane / "tests" / "__pycache__").mkdir()
    finally:
        lw.unlock_tests(lane)


def test_locking_a_tree_with_no_tests_is_not_an_error(tmp_path):
    assert lw.lock_tests_readonly(tmp_path / "nowhere") == ()
    assert lw.unlock_tests(tmp_path / "nowhere") == ()


def test_the_lock_is_off_unless_the_front_door_switches_it_on(repo):
    """`LOCK_TESTS` defaults off: `lintester.py` is the only caller between
    the CLI and `acquire_lanes` and this blueprint does not edit it, so a
    default-on flag would apply to every library caller too."""
    assert lw.LOCK_TESTS is False


def test_acquire_lanes_locks_each_new_lane_when_asked(repo, monkeypatch):
    seen = []
    monkeypatch.setattr(lw, "lock_tests_readonly", lambda p: seen.append(str(p)))
    lanes = lw.acquire_lanes(repo, 2, "m-1", lock_tests=True)
    try:
        assert all(l.ok for l in lanes), [l.reason for l in lanes]
        assert seen == [l.path for l in lanes]
    finally:
        lw.release_lanes(repo, lanes)


def test_acquire_lanes_locks_nothing_by_default(repo, monkeypatch):
    seen = []
    monkeypatch.setattr(lw, "lock_tests_readonly", lambda p: seen.append(str(p)))
    lanes = lw.acquire_lanes(repo, 1, "m-1")
    try:
        assert seen == []
    finally:
        lw.release_lanes(repo, lanes)


def test_release_always_unlocks_whatever_the_policy_was(repo, monkeypatch):
    """Unconditionally, because a read-only file is one more thing that can
    make a removal fail on Windows and re-granting a bit that is already
    granted costs nothing."""
    seen = []
    monkeypatch.setattr(lw, "unlock_tests", lambda p: seen.append(str(p)))
    lanes = lw.acquire_lanes(repo, 1, "m-1")
    lw.release_lanes(repo, lanes)
    assert seen == [lanes[0].path]


def test_a_locked_lane_can_still_be_torn_down(repo):
    """The whole point of unlocking on release, asserted against real git and
    a real read-only file rather than against the call.

    The tests are committed to the REPO before the lane exists, which is both
    the realistic case — a lane inherits the suite it is judged by — and the
    only one that can answer this question: `retire` keeps any lane holding
    work, so a lane that had to create the file itself would be kept for
    holding it and the read-only bit would never be reached."""
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tests")

    lanes = lw.acquire_lanes(repo, 1, "m-1", lock_tests=True)
    assert lanes[0].ok, lanes[0].reason
    lane = Path(lanes[0].path)
    assert not _mode(lane / "tests" / "test_a.py"), "the lane was not locked"

    released, = lw.release_lanes(repo, lanes)
    assert released.removed, released.reason
    assert not lane.exists()


def test_the_lock_covers_exactly_the_judge(tmp_path):
    """What the gate compares is what the lock protects: one list,
    `judge.judge_files`. The shared config files are not on it -- a child
    may bump a dependency there, and only their pytest section is the judge,
    which no file mode can express."""
    from mindhive.orchestrator import judge

    lane = tmp_path / "lane"
    files = {
        "tests/test_a.py": "x = 1\n",
        "tests/conftest.py": "import pytest\n",
        "tests/data/expected.json": "{}\n",
        "conftest.py": "# the root plugin\n",
        "pytest.ini": "[pytest]\n",
        "pyproject.toml": "[project]\nname = 'p'\n",
        "src/pkg/mod.py": "y = 2\n",
        "tests/__pycache__/test_a.cpython-314.pyc": "",
    }
    for rel, text in files.items():
        (lane / rel).parent.mkdir(parents=True, exist_ok=True)
        (lane / rel).write_text(text, encoding="utf-8")

    locked = lw.lock_tests_readonly(lane)
    try:
        assert locked == judge.judge_files(lane) == (
            "conftest.py", "pytest.ini", "tests/conftest.py",
            "tests/data/expected.json", "tests/test_a.py")
        for rel in files:
            assert _mode(lane / rel) is (rel not in locked), rel
    finally:
        lw.unlock_tests(lane)
    assert all(_mode(lane / rel) for rel in files)


@pytest.mark.skipif(os.name == "nt", reason="no execute bit to keep on Windows")
def test_the_lock_moves_only_the_write_bits(tmp_path):
    """A fixed 0o444 would drop an executable fixture's `x` bit, and git
    records that bit: the lock itself would then read as a test edit."""
    import stat

    lane = tmp_path / "lane"
    (lane / "tests").mkdir(parents=True)
    script = lane / "tests" / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(script, 0o755)

    lw.lock_tests_readonly(lane)
    assert stat.S_IMODE(script.stat().st_mode) == 0o555
    lw.unlock_tests(lane)
    assert stat.S_IMODE(script.stat().st_mode) == 0o755


def test_locking_a_lane_is_not_itself_a_test_edit(repo):
    """The deterrent must not trip the gate it stands beside. Git sees a mode
    change as a change, so a lock that moved more than it should would make
    every locked lane read as a child that edited its tests."""
    from mindhive.orchestrator import judge

    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "conftest.py").write_text("# the root plugin\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tests")

    lanes = lw.acquire_lanes(repo, 1, "m-1", lock_tests=True)
    try:
        assert lanes[0].ok, lanes[0].reason
        lane = Path(lanes[0].path)
        assert not _mode(lane / "tests" / "test_a.py"), "canary: not locked"
        assert not _mode(lane / "conftest.py"), "canary: not locked"
        assert judge.tests_mutated(lane) == ()
        assert _git(lane, "status", "--porcelain").stdout == ""
    finally:
        lw.release_lanes(repo, lanes)


def test_the_lock_never_follows_a_symlink(tmp_path):
    """A symlink's mode is its target's, and the target can be anywhere --
    outside the lane, in the operator's own checkout."""
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    lane = tmp_path / "lane"
    (lane / "tests").mkdir(parents=True)
    link = lane / "tests" / "test_link.py"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("this platform or user cannot create a symlink")
    try:
        assert "tests/test_link.py" not in lw.lock_tests_readonly(lane)
        assert _mode(outside), "the lock followed the link out of the lane"
    finally:
        lw.unlock_tests(lane)


def test_an_empty_path_locks_nothing_rather_than_the_working_directory(
        tmp_path, monkeypatch):
    """`Path("")` is `.`. An empty lane path read that way would make the
    checkout the orchestrator runs FROM read-only."""
    (tmp_path / "tests").mkdir()
    mine = tmp_path / "tests" / "test_mine.py"
    mine.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for empty in ("", None):
        assert lw.lock_tests_readonly(empty) == ()
        assert lw.unlock_tests(empty) == ()
    assert _mode(mine)


# --- P2 round 2: a lane git no longer answers for is not a lane ----------------

def test_a_lane_whose_link_to_its_repository_is_gone_is_refused_not_emptied(
        repo):
    """`is_lane_path` is a test of the PATH. With the lane's `.git` file gone,
    git asked there walks up and answers for the main checkout, from where
    every file in the lane is untracked -- and `clean -fd` then empties it.
    The guard behind the path guard is git's own answer about the top level."""
    lane = _lane(repo)
    path = Path(lane.path)
    (path / "half-written.txt").write_text("partial\n", encoding="utf-8")
    (path / ".git").unlink()

    got = lw.rollback(repo, lane)

    assert not got.ok and not got.changed, got.reason
    assert "discarding nothing" in got.reason
    assert (path / "half-written.txt").exists()
    assert (path / "seed.txt").exists()


# --- P2 round 2, review fixes: the guard under the lock, the walk at a link -------

def _link_dir(target, link):
    """A directory link at *link* naming *target*: a junction on Windows
    (`_winapi.CreateJunction` needs no privilege), a symlink elsewhere. Skips
    when the platform refuses either."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, ImportError, AttributeError, NotImplementedError) as exc:
        pytest.skip(f"cannot link a directory here: {exc}")


def test_the_top_level_guard_is_a_seam_taken_inside_the_try(monkeypatch, repo):
    """`rollback` reaches for `mn_wt.is_toplevel` the way it reaches for
    `_git` and `_GIT_LOCK`: inside the guard, so a rename on the incumbent's
    side is reported, not raised out of a repair loop."""
    from mindhive.mindnet import worktree as mn_wt

    lane = _lane(repo)
    monkeypatch.delattr(mn_wt, "is_toplevel")
    got = lw.rollback(repo, lane)

    assert not got.ok and not got.changed
    assert "cannot load the worktree driver" in got.reason
    assert "AttributeError" in got.reason


def test_the_top_level_guard_holds_the_git_lock_while_it_asks(monkeypatch,
                                                              repo):
    """Between "this is a checkout of its own" and `checkout -- .` nothing may
    change what git answers for the path: a prune on another thread takes the
    same lock, so the guard is asked under it, not before it. Proven by a
    second thread that tries the lock while the guard runs and must fail."""
    import threading

    from mindhive.mindnet import worktree as mn_wt

    real, held = mn_wt.is_toplevel, []

    def spy(path):
        taken = []

        def probe():
            ok = mn_wt._GIT_LOCK.acquire(blocking=False)
            if ok:
                mn_wt._GIT_LOCK.release()
            taken.append(ok)

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        held.append(taken == [False])
        return real(path)

    lane = _lane(repo)
    monkeypatch.setattr(mn_wt, "is_toplevel", spy)
    got = lw.rollback(repo, lane)

    assert got.ok, got.reason
    assert held == [True], "the guard was asked outside the lock"


def test_the_lock_never_follows_a_directory_link(tmp_path):
    """The symlink case, and the junction: `os.walk` walks straight into one,
    and the lock would then clear the write bit on files that belong to
    whatever tree the link names."""
    lane = tmp_path / "lane"
    outside = tmp_path / "outside"
    (lane / "tests").mkdir(parents=True)
    outside.mkdir()
    keep = lane / "tests" / "test_a.py"
    theirs = outside / "test_theirs.py"
    for target in (keep, theirs):
        target.write_text("x = 1\n", encoding="utf-8")
    _link_dir(outside, lane / "tests" / "linked")

    assert lw.lock_tests_readonly(lane) == ("tests/test_a.py",)
    try:
        assert not _mode(keep)
        assert _mode(theirs), "a file behind the link lost its write bit"
    finally:
        lw.unlock_tests(lane)
