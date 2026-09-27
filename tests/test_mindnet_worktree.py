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
import concurrent.futures as cf
import contextlib
import subprocess
import threading
import time
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


def test_changed_files_lists_tracked_changes_and_untracked_files(repo):
    """The per-unit gate's question: what did the child touch? Modified,
    staged-new and brand-new files all count; ignored ones do not, because a
    `.pytest_cache/` is not a change."""
    assert wt.changed_files(repo) == (), "canary: a clean tree answers empty"
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "new.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "staged.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "staged.py")
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "junk.py").write_text("z = 3\n", encoding="utf-8")

    got = wt.changed_files(repo)

    assert set(got) == {"seed.txt", "staged.py", "sub/new.py", ".gitignore"}
    assert all("\\" not in p for p in got), got


def test_changed_files_is_none_where_git_cannot_answer(tmp_path):
    assert wt.changed_files(tmp_path / "nowhere") is None


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


# --- E1: two lanes, one .git --------------------------------------------------
#
# `add` and `retire` both run from inside the orchestrator's thread pool
# (`orchestrator.py:413` and `:458`), against one shared `.git`. `retire` ends with
# `git worktree prune`, which is GLOBAL over `.git/worktrees/` — so it can sweep an
# admin entry another lane is in the middle of creating.
#
# These assertions are about MUTUAL EXCLUSION, not about speed: with the lock in
# place two `add` calls cannot overlap, whatever the machine is doing. The delay is
# injected only so the CANARY's positive assertion is robust — without it, an
# unserialised run could finish each call before the next began and the instrument
# would report "no overlap" for the wrong reason.

def _recording_git(monkeypatch, seconds=0.05):
    """Record every git call's interval, and slow each one so overlap is visible.

    The interval measured is the GIT CALL, not the `add()` call. That distinction
    is the whole instrument: a lane blocked on the lock has not entered `.git`
    yet, so timing `add()` from the outside reports every waiter as an overlap and
    the test can never pass, lock or no lock."""
    real = wt._git
    calls, guard = [], threading.Lock()

    def rec(root, *args):
        t0 = time.perf_counter()
        out = real(root, *args)
        time.sleep(seconds)
        t1 = time.perf_counter()
        with guard:
            calls.append((t0, t1, threading.current_thread().name,
                          " ".join(str(a) for a in args[:2])))
        return out

    monkeypatch.setattr(wt, "_git", rec)
    return calls


def _concurrent(ops):
    """Run every ``(label, fn)`` at once; answer ``{label: result}``."""
    results, guard = {}, threading.Lock()

    def one(item):
        label, fn = item
        out = fn()
        with guard:
            results[label] = out

    with cf.ThreadPoolExecutor(max_workers=len(ops)) as pool:
        list(pool.map(one, ops))
    return results


def _cross_thread_overlaps(calls):
    """Git calls from DIFFERENT threads that were inside `.git` together.

    Adjacent pairs suffice: under a lock the sorted intervals are disjoint, so any
    violation shows up between neighbours."""
    ordered = sorted(calls)
    return [(a[2], a[3], b[2], b[3])
            for a, b in zip(ordered, ordered[1:])
            if b[0] < a[1] and a[2] != b[2]]


def test_concurrent_adds_never_overlap(repo, monkeypatch):
    """The E1 contract. Four lanes calling `add` at once must be serialised, so no
    two git invocations are ever inside one `.git` together."""
    calls = _recording_git(monkeypatch)
    results = _concurrent([(tid, (lambda t=tid: wt.add(repo, t)))
                           for tid in ("t1", "t2", "t3", "t4")])

    assert all(r.ok for r in results.values()), \
        [r.reason for r in results.values() if not r.ok]
    assert calls, "no git calls were recorded — the instrument is not wired in"
    assert _cross_thread_overlaps(calls) == [], \
        f"git ran concurrently against one .git: {_cross_thread_overlaps(calls)}"


def test_the_overlap_instrument_can_see_an_unserialised_add(repo, monkeypatch):
    """The canary. The test above asserts an ABSENCE, and an absence is evidence
    only from an instrument that can report a presence.

    Same harness, same inputs, lock replaced by a no-op — it must report overlap.
    If this goes green the test above proves nothing, and the lock could be deleted
    without either of them noticing."""
    calls = _recording_git(monkeypatch)
    monkeypatch.setattr(wt, "_GIT_LOCK", contextlib.nullcontext())
    _concurrent([(tid, (lambda t=tid: wt.add(repo, t)))
                 for tid in ("t1", "t2", "t3", "t4")])

    # Deliberately no assertion on `ok`: unserialised adds are ALLOWED to collide —
    # that collision is the defect. Only the instrument is under test here.
    assert _cross_thread_overlaps(calls), (
        "the harness saw no overlap even with the lock disabled, so it cannot "
        "distinguish serialised from concurrent and the E1 test is vacuous")


def test_retire_is_serialised_against_add(repo, monkeypatch):
    """`retire` ends with `git worktree prune`, which is global over
    `.git/worktrees/`. Locking only `add` would leave prune free to run while
    another lane's admin entry is half-written."""
    settled = wt.add(repo, "keep")
    assert settled.ok, settled.reason
    calls = _recording_git(monkeypatch)

    _concurrent([("add-t1", lambda: wt.add(repo, "t1")),
                 ("add-t2", lambda: wt.add(repo, "t2")),
                 ("retire-keep", lambda: wt.retire(repo, settled.path,
                                                   settled.branch, settled.base))])

    assert calls, "no git calls were recorded — the instrument is not wired in"
    assert _cross_thread_overlaps(calls) == [], \
        f"add and retire ran against one .git together: {_cross_thread_overlaps(calls)}"


# --- the merge-back's primitives (orchestrator/integrate.py's git driver) -------

def _argv(monkeypatch):
    """Every git argv this module runs, recorded at the primitive."""
    calls = []
    real = wt._git_rc

    def rec(root, *args):
        calls.append(tuple(str(a) for a in args))
        return real(root, *args)

    monkeypatch.setattr(wt, "_git_rc", rec)
    return calls


def _branch_with_a_commit(repo, name, rel="new.txt"):
    """*name*, one commit past `main`, with `main` checked out again after."""
    _git(repo, "checkout", "-q", "-b", name)
    (repo / rel).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-qm", name)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "main")
    return sha


def _conflict(repo):
    """`lane` and `main` both rewrite `seed.txt`, differently."""
    _git(repo, "checkout", "-q", "-b", "lane")
    (repo / "seed.txt").write_text("lane\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "lane")
    _git(repo, "checkout", "-q", "main")
    (repo / "seed.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "main moved")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_add_without_a_start_point_runs_the_argv_it_always_ran(repo, monkeypatch):
    """The pin. `start_point` is the merge-back's one addition to `add`;
    absent, every existing caller sees the same git commands, byte for byte."""
    calls = _argv(monkeypatch)

    got = wt.add(repo, "t1")

    assert got.ok, got.reason
    assert ("rev-parse", "HEAD") in calls
    adds = [c for c in calls if c[:2] == ("worktree", "add")]
    assert adds == [("worktree", "add", "-B", "mn9/t1", str(Path(got.path)))]


def test_add_with_a_start_point_branches_from_it_and_records_it_as_base(
        repo, monkeypatch):
    first = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "later")
    calls = _argv(monkeypatch)

    got = wt.add(repo, "t1", start_point=first)

    assert got.ok, got.reason
    assert got.base == first, "base must be the start point, not HEAD"
    assert _git(got.path, "rev-parse", "HEAD").stdout.strip() == first
    assert not (Path(got.path) / "later.txt").exists()
    adds = [c for c in calls if c[:2] == ("worktree", "add")]
    assert adds == [("worktree", "add", "-B", "mn9/t1", str(Path(got.path)),
                     first)]


def test_add_with_a_start_point_refuses_a_branch_holding_commits_past_it(repo):
    """The rule that keeps committed work reachable, measured against the
    start point rather than HEAD."""
    first = _git(repo, "rev-parse", "HEAD").stdout.strip()
    held = _branch_with_a_commit(repo, "mn9/t1")

    got = wt.add(repo, "t1", start_point=first)

    assert got.ok is False and got.retryable is False
    assert "already holds 1 commit" in got.reason
    assert _git(repo, "rev-parse", "mn9/t1").stdout.strip() == held
    assert not Path(got.path).exists()


def test_add_with_an_unknown_start_point_is_a_settled_refusal(repo):
    got = wt.add(repo, "t1", start_point="no-such-ref")
    assert got.ok is False and got.retryable is False
    assert got.reason
    assert not Path(got.path).exists()


def test_commit_all_on_a_clean_tree_is_a_recorded_no_op(repo):
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    got = wt.commit_all(repo, "nothing here")

    assert got.ok is True and got.changed is False
    assert got.sha == head
    assert "nothing to commit" in got.reason
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head


def test_commit_all_commits_everything_but_the_caches(repo):
    """`add -A` with the cache directories excluded: a child's `__pycache__/`
    is not its work, and a repository without a `.gitignore` -- every lane
    fixture in this suite -- would otherwise commit bytecode."""
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "new.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "sub" / "__pycache__").mkdir()
    (repo / "sub" / "__pycache__" / "new.cpython-312.pyc").write_bytes(b"\x00")
    (repo / ".pytest_cache" / "v").mkdir(parents=True)
    (repo / ".pytest_cache" / "v" / "nodeids").write_text("[]", encoding="utf-8")

    got = wt.commit_all(repo, "lint(lm-1) t-1: the work\n\nLintester-Task: t-1")

    assert got.ok is True and got.changed is True, got.reason
    assert got.sha == _git(repo, "rev-parse", "HEAD").stdout.strip()
    tree = _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "sub/new.py" in tree and "seed.txt" in tree
    assert "__pycache__" not in tree and ".pytest_cache" not in tree, tree
    body = _git(repo, "log", "-1", "--format=%B").stdout
    assert body.startswith("lint(lm-1) t-1: the work")
    assert "Lintester-Task: t-1" in body
    left = [ln.strip() for ln in _git(repo, "status", "--porcelain").stdout
            .splitlines() if ln.strip()]
    assert left and all(any(d in ln for d in wt.CACHE_DIRS) for ln in left), left


def test_commit_all_with_only_caches_to_commit_is_a_no_op(repo):
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\x00")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    got = wt.commit_all(repo, "caches only")

    assert got.ok is True and got.changed is False
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head


def test_commit_all_never_bypasses_a_hook(repo):
    """A refusing `pre-commit` hook is git's own reason, reported as one. No
    `--no-verify`, no retry -- the operator's hooks bind the orchestrator's
    commits exactly as they bind a person's."""
    import inspect
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho refused-by-hook >&2\nexit 1\n",
                    encoding="utf-8")
    hook.chmod(0o755)
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    got = wt.commit_all(repo, "blocked")

    assert got.ok is False and got.changed is False
    assert "refused-by-hook" in got.reason, got.reason
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert "--no-verify" not in inspect.getsource(wt)


def test_merge_probe_lists_the_conflicting_paths_and_writes_no_ref(repo):
    """`git merge-tree --write-tree` writes objects, never refs: HEAD, the
    index and the working tree are exactly as they were."""
    head = _conflict(repo)
    refs = _git(repo, "for-each-ref").stdout

    probe = wt.merge_probe(repo, "lane")

    assert probe.clean is False
    assert probe.conflicts == ("seed.txt",)
    assert probe.tree and len(probe.tree) == 40
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert _git(repo, "for-each-ref").stdout == refs
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_merge_probe_answers_clean_with_the_tree_it_would_make(repo):
    _branch_with_a_commit(repo, "lane")

    probe = wt.merge_probe(repo, "lane")

    assert probe.clean is True and probe.conflicts == ()
    assert _git(repo, "cat-file", "-t", probe.tree).stdout.strip() == "tree"


def test_merge_probe_that_cannot_tell_is_not_a_no(repo):
    probe = wt.merge_probe(repo, "no-such-branch")
    assert probe.clean is None
    assert probe.reason


def test_merge_into_aborts_a_failed_merge_and_leaves_the_tree_clean(repo):
    head = _conflict(repo)

    got = wt.merge_into(repo, "lane", "merge lane")

    assert got.ok is False and got.reason
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head


def test_merge_into_makes_a_merge_commit_even_when_it_could_fast_forward(repo):
    lane_sha = _branch_with_a_commit(repo, "lane")

    got = wt.merge_into(repo, "lane", "lint(lm-1): merge lane 0")

    assert got.ok, got.reason
    assert got.sha == _git(repo, "rev-parse", "HEAD").stdout.strip()
    parents = _git(repo, "log", "-1", "--format=%P").stdout.split()
    assert len(parents) == 2, "a fast-forward leaves no merge to read the lane from"
    assert lane_sha in parents
    subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "lint(lm-1): merge lane 0"


def test_is_ancestor_distinguishes_no_from_unknown(repo):
    _branch_with_a_commit(repo, "lane")
    assert wt.is_ancestor(repo, "main", "lane") is True
    assert wt.is_ancestor(repo, "lane", "main") is False
    assert wt.is_ancestor(repo, "no-such-ref", "main") is None


def test_rev_of_answers_the_commit_or_nothing(repo):
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert wt.rev_of(repo, "main") == head
    assert wt.rev_of(repo, "no-such-ref") == ""


def test_state_does_not_count_test_caches_as_work(repo):
    """A gated lane's pytest run leaves `__pycache__/` and `.pytest_cache/`
    behind; before, `state` read them as work and every gated lane was kept
    after its commits had merged. Both spellings: untracked (a fixture repo
    has no `.gitignore`) and ignored."""
    got = wt.add(repo, "t1")
    p = Path(got.path)
    (p / ".pytest_cache" / "v").mkdir(parents=True)
    (p / ".pytest_cache" / "v" / "nodeids").write_text("[]", encoding="utf-8")
    (p / "src" / "__pycache__").mkdir(parents=True)
    (p / "src" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\x00")
    assert wt.state(p, got.base) == (False, 0)

    (p / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n",
                                  encoding="utf-8")
    _git(p, "add", ".gitignore")
    _git(p, "commit", "-qm", "ignore the caches")
    assert wt.state(p, got.base) == (False, 1)
    tip = _git(p, "rev-parse", "HEAD").stdout.strip()
    assert wt.retire(repo, p, got.branch, tip).removed


def test_ignored_output_outside_a_cache_directory_is_still_work(repo):
    """The canary: the exemption is a fixed list of cache directories, not
    "anything ignored". `dist/out.bin` under an ignore rule is a build the
    operator asked for, and `retire` keeps it exactly as before."""
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore dist")
    got = wt.add(repo, "t1")
    p = Path(got.path)
    (p / "dist").mkdir()
    (p / "dist" / "out.bin").write_bytes(b"\x00")

    assert wt.state(p, got.base) == (True, 0)
    assert wt.retire(repo, p, got.branch, got.base).kept
    assert (p / "dist" / "out.bin").exists()


# --- the merge-back's provenance read ---------------------------------------------
#
# `orchestrator.integrate` delivers a lane only when every commit past the base
# is one it stamped. This is the read that tells them apart -- git's own
# trailer parser answering, not a substring search over messages.

def _commit_with(repo, rel, message):
    (repo / rel).write_text(f"{rel}\n", encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_commits_since_reads_each_commit_with_the_trailer_git_sees(repo):
    """Newest first, as `git log` prints them, each with the values of the one
    trailer asked for as `git interpret-trailers` reads a message -- the LAST
    paragraph, `Key: value` lines -- so the key mentioned in a body is not a
    trailer, and two trailer lines are two values."""
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    stamped = _commit_with(repo, "a.txt", "lint(m) t-1: title\n\n"
                           "Lintester-Task: t-1\n"
                           "Co-Authored-By: Claude <noreply@anthropic.com>")
    foreign = _commit_with(repo, "b.txt", "a child's own commit\n\n"
                           "it mentions Lintester-Task: in the body\n\n"
                           "and ends with prose")
    twice = _commit_with(repo, "c.txt", "two\n\nLintester-Task: t-2\n"
                         "Lintester-Task: t-3\n")

    got = wt.commits_since(repo, start, "HEAD", "Lintester-Task")

    assert got == (wt.LogEntry(twice, ("t-2", "t-3")),
                   wt.LogEntry(foreign, ()),
                   wt.LogEntry(stamped, ("t-1",)))
    assert wt.commits_since(repo, start, start, "Lintester-Task") == ()
    assert wt.commits_since(repo, twice, start, "Lintester-Task") == ()


@pytest.mark.parametrize("base, ref, key", [
    ("no-such-ref", "HEAD", "Lintester-Task"),
    ("HEAD", "no-such-ref", "Lintester-Task"),
    ("", "HEAD", "Lintester-Task"),
    ("HEAD", "", "Lintester-Task"),
    ("HEAD", "HEAD", ""),
    ("HEAD", "HEAD", "Bad Key"),
    ("HEAD", "HEAD", "key,valueonly"),
])
def test_commits_since_is_none_where_git_could_not_or_must_not_answer(
        repo, base, ref, key):
    """An unknown rev, and a key that is not a trailer token (git would read
    the comma as its next format option): ``None``, never ``()`` -- the
    caller reads ``()`` as "nothing foreign here" and merges."""
    assert wt.commits_since(repo, base, ref, key) is None


def test_commits_since_never_hands_git_an_option_as_a_rev(repo, monkeypatch):
    """`git log --output=x..HEAD` WRITES a file named for the range. A rev
    that reads as an option is refused before git is asked anything."""
    calls = []
    real = wt._git_rc
    monkeypatch.setattr(wt, "_git_rc",
                        lambda root, *a: calls.append(a) or real(root, *a))
    assert wt.commits_since(repo, "--output=x", "HEAD", "Lintester-Task") is None
    assert wt.commits_since(repo, "HEAD", "--all", "Lintester-Task") is None
    assert calls == []
    assert not list(Path(repo).glob("x*"))


def test_commits_since_outside_a_repository_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert wt.commits_since(tmp_path, "HEAD", "HEAD", "Lintester-Task") is None
