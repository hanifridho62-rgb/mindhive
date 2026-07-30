# tests/test_mindnet_worktree.py
"""MN-9 U1+U3 — one isolated checkout per orchestrated task.

Written against
``docs/plans/2026-07-30-002-feat-mindnet-worktree-isolation-plan.md``.

## Real git, not a patched subprocess

Every test here builds an actual repository in `tmp_path` — `git init`, one commit —
and runs the real commands. That is this repo's own doctrine (`test_mindnet_executor`
drives real subprocesses for the same reason) and it is especially load-bearing
here: the behaviour under test IS git's, and a patched `subprocess.run` would
encode a guess about argv for the exact commands whose semantics are the question.
`git init` in tmp_path costs milliseconds.

## The assertion that matters most

`retire` must never delete an agent's work. `test_retire_keeps_a_dirty_worktree`
and `test_retire_keeps_a_worktree_that_is_ahead` are the two whose failure silently
destroys output, which is the entire reason for running agents in the first place.
"""
import subprocess
from pathlib import Path

import pytest

from mindhive.mindnet import worktree as wt

REPO = Path(__file__).resolve().parents[1]


# --- fixture builders ---------------------------------------------------------

def _git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *[str(a) for a in args]],
                       capture_output=True, text=True, shell=False)
    if check:
        assert r.returncode == 0, f"git {' '.join(map(str, args))}: {r.stderr}"
    return r


@pytest.fixture
def repo(tmp_path):
    """A real repository with one commit — the minimum a worktree can branch from."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-qm", "seed")
    return root


def _worktrees(root):
    out = _git(root, "worktree", "list", "--porcelain").stdout
    return [ln.split(" ", 1)[1] for ln in out.splitlines()
            if ln.startswith("worktree ")]


def _branches(root):
    return {ln.strip().lstrip("* ").strip()
            for ln in _git(root, "branch", "--format=%(refname:short)")
            .stdout.splitlines() if ln.strip()}


# --- U1: the ignore rule must be portable ------------------------------------

def test_the_worktree_dir_is_ignored_by_the_committed_gitignore():
    """It previously lived only in `.git/info/exclude`, which is local and does not
    travel. On any other clone an agent's worktree would show up as untracked
    changes in the parent — and this project's change-log detection reads
    `git status`.

    Asserting the SOURCE file, not just the outcome: a test that only checked
    "is it ignored" passes on this machine via the local exclude and fails on a
    fresh clone, which is the bug it is meant to prevent."""
    probe = Path(*wt.WORKTREE_DIR) / f"{wt.PREFIX}-probe"
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-v",
                        str(probe).replace("\\", "/")],
                       capture_output=True, text=True, shell=False)
    assert r.returncode == 0, f"{probe} is not ignored at all"
    source = r.stdout.split(":", 1)[0].strip()
    assert source.endswith(".gitignore"), (
        f"{probe} is ignored by {source}, not the committed .gitignore — that "
        f"rule does not travel to another clone")


@pytest.mark.parametrize("keep", [".claude/skills", ".claude/settings.json",
                                 ".claude/launch.json"])
def test_the_ignore_rule_does_not_swallow_the_rest_of_dot_claude(keep):
    """The canary for the test above: a too-broad `.claude/` rule would also pass
    it while hiding the skills and settings this repo tracks."""
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", keep],
                       capture_output=True, text=True, shell=False)
    assert r.returncode != 0, f"{keep} is ignored, which it must not be"


# --- add ----------------------------------------------------------------------

def test_add_creates_a_worktree_on_its_own_branch(repo):
    got = wt.add(repo, "t1")
    assert got.ok, got.reason
    assert Path(got.path).is_dir()
    assert got.branch == "mn9/t1"
    assert got.branch in _branches(repo)
    assert Path(got.path).name == "mn9-t1"


def test_the_new_worktree_holds_the_repo_contents(repo):
    """A checkout an agent cannot read is not a checkout."""
    got = wt.add(repo, "t1")
    assert (Path(got.path) / "seed.txt").read_text(encoding="utf-8") == "seed\n"


def test_two_tasks_get_two_independent_worktrees(repo):
    """R1. Independent means a write in one is invisible in the other — that is the
    whole property the shared-tree cap existed to protect."""
    a, b = wt.add(repo, "a"), wt.add(repo, "b")
    assert a.ok and b.ok
    assert a.path != b.path and a.branch != b.branch

    (Path(a.path) / "only-in-a.txt").write_text("x", encoding="utf-8")
    assert not (Path(b.path) / "only-in-a.txt").exists()
    assert len(_worktrees(repo)) == 3          # the main tree plus two


def test_add_refuses_an_existing_directory_rather_than_reusing_it(repo):
    """Reusing it would hand an agent a tree holding a previous run's uncommitted
    work and call it clean; clobbering it would delete that work. Refusing makes
    the operator look."""
    first = wt.add(repo, "t1")
    assert first.ok
    (Path(first.path) / "unsaved.txt").write_text("precious", encoding="utf-8")

    second = wt.add(repo, "t1")
    assert not second.ok
    assert "already exists" in second.reason
    assert (Path(first.path) / "unsaved.txt").exists(), "the work was destroyed"


def test_add_reuses_a_leftover_branch_after_its_worktree_was_retired(repo):
    """`-B` not `-b`: a branch left behind by an earlier retired run must not make
    every future run of that task fail."""
    first = wt.add(repo, "t1")
    assert wt.retire(repo, first.path, first.branch, first.base).removed
    assert "mn9/t1" in _branches(repo), "canary: the branch should outlive removal"

    again = wt.add(repo, "t1")
    assert again.ok, again.reason


def test_add_outside_a_repo_answers_a_reason_and_does_not_raise(tmp_path):
    """R4/KTD6: one task's environmental failure must be a value the wave can
    classify, never an exception that takes the wave down."""
    got = wt.add(tmp_path / "not-a-repo", "t1")
    assert got.ok is False
    assert got.reason


# --- state --------------------------------------------------------------------

def test_state_reports_a_fresh_worktree_clean(repo):
    got = wt.add(repo, "t1")
    assert wt.state(got.path, got.base) == (False, 0)


def test_state_sees_an_untracked_file_as_dirty(repo):
    """Untracked counts: an agent that created a file and did not commit it has
    produced work, and deleting the tree would destroy it."""
    got = wt.add(repo, "t1")
    (Path(got.path) / "new.txt").write_text("work", encoding="utf-8")
    dirty, _ahead = wt.state(got.path, got.base)
    assert dirty is True


def test_state_sees_a_modified_tracked_file_as_dirty(repo):
    got = wt.add(repo, "t1")
    (Path(got.path) / "seed.txt").write_text("changed\n", encoding="utf-8")
    assert wt.state(got.path, got.base)[0] is True


def test_state_counts_a_commit_as_ahead(repo):
    got = wt.add(repo, "t1")
    p = Path(got.path)
    (p / "new.txt").write_text("work", encoding="utf-8")
    _git(p, "add", "new.txt")
    _git(p, "commit", "-qm", "agent work")
    dirty, ahead = wt.state(got.path, got.base)
    assert dirty is False, "a committed tree is clean"
    assert ahead >= 1


def test_state_answers_dirty_when_it_cannot_tell(tmp_path):
    """Unknown must read as "there may be work here" — the only consumer is the
    decision to delete."""
    assert wt.state(tmp_path / "nope", "abc123") == (True, 0)


# --- retire: the two tests whose failure destroys work ------------------------

def test_retire_removes_a_clean_worktree(repo):
    got = wt.add(repo, "t1")
    out = wt.retire(repo, got.path, got.branch, got.base)
    assert out.removed is True and out.kept is False
    assert not Path(got.path).exists()
    assert got.path not in [p.replace("/", "\\") for p in _worktrees(repo)]


def test_retire_keeps_a_dirty_worktree(repo):
    """THE assertion. Removing this tree deletes an agent's uncommitted output,
    which is the entire product of running the agent."""
    got = wt.add(repo, "t1")
    (Path(got.path) / "precious.txt").write_text("hours of work", encoding="utf-8")

    out = wt.retire(repo, got.path, got.branch, got.base)
    assert out.removed is False
    assert out.kept is True
    assert "uncommitted" in out.reason
    assert (Path(got.path) / "precious.txt").read_text(encoding="utf-8") \
        == "hours of work"


def test_retire_keeps_a_worktree_that_is_ahead(repo):
    """The committed-but-unmerged case. The branch would survive removal, but the
    operator has no signal that there is anything to look at."""
    got = wt.add(repo, "t1")
    p = Path(got.path)
    (p / "new.txt").write_text("work", encoding="utf-8")
    _git(p, "add", "new.txt")
    _git(p, "commit", "-qm", "agent work")

    out = wt.retire(repo, got.path, got.branch, got.base)
    assert out.kept is True
    assert "commit" in out.reason
    assert p.exists()


def test_the_branch_survives_a_successful_retire(repo):
    """So committed work is never lost in either direction."""
    got = wt.add(repo, "t1")
    wt.retire(repo, got.path, got.branch, got.base)
    assert "mn9/t1" in _branches(repo)


def test_retire_on_an_already_gone_path_does_not_raise(repo):
    got = wt.add(repo, "t1")
    wt.retire(repo, got.path, got.branch, got.base)
    out = wt.retire(repo, got.path, got.branch, got.base)
    assert out.removed is False and out.kept is False


def test_retire_prunes_the_admin_records(repo):
    """A removed directory whose admin record survives makes the next `add` for
    that task fail with a stale-registration error."""
    got = wt.add(repo, "t1")
    wt.retire(repo, got.path, got.branch, got.base)
    listed = " ".join(_worktrees(repo))
    assert "mn9-t1" not in listed


# --- structural guards --------------------------------------------------------

def test_no_shell_is_ever_used():
    """U6's standing rule. A shell here would make every task id a command
    injection point, since ids reach the argv."""
    import inspect
    src = inspect.getsource(wt)
    assert "shell=True" not in src
    assert "shell=False" in src


def test_every_remove_passes_force():
    """This repo's recorded Windows lesson, and `enforce.py` carries a learned rule
    about it: removal fails with "Permission denied" whenever a handle is still
    open in the tree, which right after an agent finishes writing is normal."""
    import inspect
    src = inspect.getsource(wt)
    idx = src.index('"remove"')
    assert '"--force"' in src[idx:idx + 120], (
        "git worktree remove without --force fails on Windows whenever any "
        "process still holds a handle inside the tree")


def test_a_task_id_with_a_slash_cannot_escape_the_worktree_dir(repo):
    """Task ids are kebab-case by `tasks.ID_RE`, but this module takes a string
    from a caller and builds a filesystem path out of it. Anything that escaped
    `.claude/worktrees/` would land an agent's checkout somewhere unexpected."""
    got = wt.add(repo, "../../escape")
    if got.ok:
        resolved = Path(got.path).resolve()
        allowed = (Path(repo) / Path(*wt.WORKTREE_DIR)).resolve()
        assert allowed in resolved.parents, (
            f"{resolved} escaped {allowed}")
    else:
        assert got.reason


def test_add_records_the_base_commit_it_branched_from(repo):
    """`ahead` is meaningless without it. The first implementation tried to infer
    it with `rev-list --not --remotes`, which in a repo with no remotes excludes
    nothing and counts the whole history — so every fresh worktree read as ahead
    and `retire` never removed anything."""
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    got = wt.add(repo, "t1")
    assert got.base == head


def test_state_without_a_base_cannot_report_ahead(repo):
    """Documented degradation: the dirty check still guards, but the committed
    case needs the base. A caller that forgets it gets 0, not a wrong number."""
    got = wt.add(repo, "t1")
    p = Path(got.path)
    (p / "n.txt").write_text("w", encoding="utf-8")
    _git(p, "add", "n.txt")
    _git(p, "commit", "-qm", "work")
    assert wt.state(p, got.base)[1] >= 1, "canary: with the base it IS ahead"
    assert wt.state(p)[1] == 0


# --- the P0 a reviewer verified live in a throwaway repo ----------------------

def test_add_refuses_to_reset_a_branch_that_holds_commits(repo):
    """THE P0. `-B` force-moves the branch, and the sequence is entirely ordinary:

      1. an agent commits inside its isolated worktree
      2. `retire` keeps the tree (ahead=1) and reports it
      3. the operator removes the DIRECTORY, exactly as `add`'s own refusal
         message instructs (`git worktree remove --force <target>`)
      4. the next run does `worktree add -B mn9/t1` -> the branch is reset to base
         and the commit becomes unreachable. Exit 0, no warning.

    So committed agent work was destroyed by a normal retry. The old docstring
    claimed "committed work survives whichever way this goes"; it did not, and the
    only `-B` test reset a branch with ZERO commits on it — the same
    "assert a shape production does not emit" pattern as MN-4/7/8."""
    first = wt.add(repo, "t1")
    p = Path(first.path)
    (p / "agent.txt").write_text("hours of work\n", encoding="utf-8")
    _git(p, "add", "agent.txt")
    _git(p, "commit", "-qm", "agent work")
    sha = _git(p, "rev-parse", "HEAD").stdout.strip()

    # The operator follows the documented remedy: remove the directory.
    _git(repo, "worktree", "remove", "--force", str(p))

    again = wt.add(repo, "t1")
    assert not again.ok, "the branch was reset and the commit destroyed"
    assert "commit" in again.reason
    contains = _git(repo, "branch", "--contains", sha, check=False).stdout
    assert "mn9/t1" in contains, f"commit {sha[:7]} is no longer reachable"


def test_add_still_resets_a_branch_with_nothing_on_it(repo):
    """The canary: the leftover-branch case the `-B` reset exists for must keep
    working, or every task becomes single-use."""
    first = wt.add(repo, "t1")
    assert wt.retire(repo, first.path, first.branch, first.base).removed
    assert "mn9/t1" in _branches(repo)
    again = wt.add(repo, "t1")
    assert again.ok, again.reason


def test_retire_keeps_a_worktree_whose_only_output_is_gitignored(repo):
    """`git status --porcelain` omits ignored paths, so an agent's output under
    `dist/`, `build/` or `.pytest_cache/` read as clean and `remove --force` erased
    it. A task like "build the desktop artifact" lands entirely in ignored paths."""
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore dist")

    got = wt.add(repo, "t1")
    out_dir = Path(got.path) / "dist"
    out_dir.mkdir()
    (out_dir / "artifact.bin").write_text("the build output", encoding="utf-8")

    res = wt.retire(repo, got.path, got.branch, got.base)
    assert res.kept is True, "gitignored agent output was deleted"
    assert (out_dir / "artifact.bin").exists()


def test_add_does_not_raise_when_the_worktree_parent_is_a_file(repo):
    """`mkdir(exist_ok=True)` still raises when the path exists as a FILE, and that
    raise escaped `one()`'s try in the orchestrator — taking down the whole wave and
    discarding the results of agents that had already run."""
    parent = repo.joinpath(*wt.WORKTREE_DIR)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("not a directory", encoding="utf-8")

    got = wt.add(repo, "t1")
    assert got.ok is False
    assert got.reason
