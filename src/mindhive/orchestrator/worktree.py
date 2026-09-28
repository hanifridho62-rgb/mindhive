# SPDX-License-Identifier: Apache-2.0
# src/mindhive/orchestrator/worktree.py
"""Lane-scoped physical isolation: `.worktrees/lane-N`, one per lane, for life.

## What this adds, and what it refuses to add

`mindnet/worktree.py` already owns every git invocation, the never-raise
contract its module docstring states, the `_GIT_LOCK` that serialises `add`
against a global `prune`, and the two rules that make committed work survive a
teardown (refuse to reset a branch holding commits; count ignored files as
work). None of that is re-implemented here. A second git-worktree driver that
drifted from those rules would delete an agent's output and look correct doing
it.

What this module contributes is two things the incumbent has no opinion about:

* **the naming the PRD specifies.** `mindnet.worktree.path_for` keys by task id
  (`.claude/worktrees/mn9-<task>`); the PRD asks for `.worktrees/lane-N`. Both
  are now reachable, because `add` takes an optional explicit path and branch.
* **lifecycle binding.** A lane acquires its tree before its first dispatch and
  releases it after its last, so no task ever runs outside its own lane's tree.
  Per-task trees would mean a `git worktree add` per task rather than per lane —
  for a 12-task mission on 3 lanes that is 12 checkouts instead of 3, and each
  one costs a full working-tree materialisation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# `.worktrees` at the repo root, exactly as the PRD spells it. Not
# `.claude/worktrees`, which is the incumbent's directory and holds task-keyed
# trees a lane teardown must never walk into.
LANE_DIR = ".worktrees"
LANE_PREFIX = "lane-"

# Branch namespace for lane checkouts. Distinct from the incumbent's `mn9/` so a
# `git branch --list` says which subsystem made a leftover.
BRANCH_PREFIX = "lint"


def lane_name(lane_id) -> str:
    return f"{LANE_PREFIX}{int(lane_id)}"


def lane_path_for(root, lane_id) -> Path:
    return Path(root) / LANE_DIR / lane_name(lane_id)


def lane_branch_for(lane_id, mission_id="") -> str:
    mission = str(mission_id or "").strip()
    if mission:
        return f"{BRANCH_PREFIX}/{mission}/{lane_name(lane_id)}"
    return f"{BRANCH_PREFIX}/{lane_name(lane_id)}"


@dataclass(frozen=True)
class LaneWorktree:
    """One lane's checkout, or the reason it has none.

    Mirrors `mindnet.worktree.Isolated` field for field, including `retryable` —
    the caller's bounded retry has to be able to tell git-was-busy from
    this-is-settled, and losing that distinction here would make a permanent
    refusal look like a flake worth hammering."""

    lane_id: int = -1
    path: str = ""
    branch: str = ""
    base: str = ""
    ok: bool = False
    reason: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class Released:
    """What `release` did. `kept=True` means work is still on disk."""

    lane_id: int = -1
    removed: bool = False
    kept: bool = False
    reason: str = ""


def acquire(root, lane_id, mission_id="") -> LaneWorktree:
    """Create `.worktrees/lane-N` for *lane_id*, or answer why not.

    Delegates wholly to `mindnet.worktree.add`, passing the lane path and branch
    explicitly. Never raises — an import failure or a git fault comes back as
    `ok=False` with a reason, because the caller is a mission that has other
    lanes to run."""
    try:
        from ..mindnet import worktree as mn_wt
    except Exception as exc:                            # noqa: BLE001
        return LaneWorktree(lane_id=int(lane_id), ok=False,
                            reason=f"cannot load the worktree driver: "
                                   f"{type(exc).__name__}: {exc}")
    target = lane_path_for(root, lane_id)
    branch = lane_branch_for(lane_id, mission_id)
    try:
        iso = mn_wt.add(root, lane_name(lane_id), path=target, branch=branch)
    except Exception as exc:                            # noqa: BLE001
        return LaneWorktree(lane_id=int(lane_id), path=str(target),
                            branch=branch, ok=False,
                            reason=f"{type(exc).__name__}: {exc}")
    return LaneWorktree(lane_id=int(lane_id), path=str(getattr(iso, "path", "")
                                                       or target),
                        branch=str(getattr(iso, "branch", "") or branch),
                        base=str(getattr(iso, "base", "") or ""),
                        ok=bool(getattr(iso, "ok", False)),
                        reason=str(getattr(iso, "reason", "") or ""),
                        retryable=bool(getattr(iso, "retryable", False)))


def release(root, lane_worktree) -> Released:
    """Remove a lane's checkout — unless it holds work nobody has looked at.

    The keep-versus-remove judgement is entirely `mindnet.worktree.retire`'s,
    including its rule that ignored files count as work. Re-deriving that here is
    how an agent's output under `dist/` gets deleted."""
    lane_id = int(getattr(lane_worktree, "lane_id", -1))
    path = str(getattr(lane_worktree, "path", "") or "")
    if not path:
        return Released(lane_id=lane_id, reason="no path to release")
    try:
        from ..mindnet import worktree as mn_wt
        ret = mn_wt.retire(root, path,
                           branch=str(getattr(lane_worktree, "branch", "") or ""),
                           base=str(getattr(lane_worktree, "base", "") or ""))
    except Exception as exc:                            # noqa: BLE001
        return Released(lane_id=lane_id, kept=True,
                        reason=f"{type(exc).__name__}: {exc}")
    return Released(lane_id=lane_id, removed=bool(getattr(ret, "removed", False)),
                    kept=bool(getattr(ret, "kept", False)),
                    reason=str(getattr(ret, "reason", "") or ""))


@dataclass(frozen=True)
class RolledBack:
    """What `rollback` did to one lane. `changed` means work was discarded."""

    lane_id: int = -1
    ok: bool = False
    changed: bool = False
    reason: str = ""


def is_lane_path(root, path) -> bool:
    """Whether *path* is a lane checkout THIS module made under *root*.

    The guard `rollback` stands on, and the reason it is a separate predicate is
    that it has to be testable on its own: everything it rejects is a directory
    somebody would otherwise be about to discard the contents of."""
    try:
        base = (Path(root).resolve() / LANE_DIR).resolve()
        target = Path(path).resolve()
    except OSError:                                     # pragma: no cover - odd fs
        return False
    if target == base or base not in target.parents:
        return False
    name = target.name
    if not name.startswith(LANE_PREFIX):
        return False
    return name[len(LANE_PREFIX):].isdigit()


def rollback(root, lane_worktree) -> RolledBack:
    """Discard a lane checkout's UNCOMMITTED work, so a retry starts clean.

    The repair loop's other half. `default_fall_back` moves the task back a
    phase in the ledger, which is a record; this moves the working tree back to
    its last commit, which is the thing the next attempt actually reads. Without
    it a second attempt starts on top of whatever the first one left half-written
    and the failure it was given to repair is not the only thing wrong with the
    tree.

    This is the only function in this package that can delete an agent's output,
    so three things bound it:

    * **the path must be a lane checkout.** `is_lane_path` requires
      `<root>/.worktrees/lane-N` with a numeric N. The main checkout, the
      incumbent's task-keyed trees under `.claude/worktrees`, and anything a
      caller merely claims is a lane are all refused, with a reason.
      Behind the path, git's own answer: a lane whose `.git` link is gone is
      a directory inside the MAIN checkout, from where every file in it is
      untracked and `clean -fd` would empty it, so a directory
      `mindnet.worktree.is_toplevel` does not vouch for is refused too.
    * **commits are never touched.** `checkout -- .` and `clean -fd` discard
      what was never committed. A lane that committed its work keeps every
      commit, and `release` still refuses to reset a branch holding them.
    * **it is opt-in.** `RepairLoop` takes it as a seam and defaults to None;
      `lintester` wires it only for a real run with isolated lanes.

    Total: answers a value, never raises."""
    lane_id = int(getattr(lane_worktree, "lane_id", -1))
    path = str(getattr(lane_worktree, "path", "") or "")
    if not path:
        return RolledBack(lane_id=lane_id, reason="no path to roll back")
    if not is_lane_path(root, path):
        return RolledBack(lane_id=lane_id,
                          reason=(f"{path} is not a {LANE_DIR}/{LANE_PREFIX}N "
                                  f"checkout under {root}; discarding nothing"))
    # `mn_wt._git` rather than a second `subprocess.run` here. It is private, and
    # reaching for it is still the lesser evil: it carries the timeout, the
    # `shell=False` rule and `paths.NO_WINDOW`, and this module's whole premise
    # is that a second git driver drifting from the first is how an agent's work
    # gets deleted by code that looks correct. `_GIT_LOCK` for the same reason
    # `add` and `retire` take it — a linked worktree's checkout writes into the
    # common git directory, where a concurrent prune is waiting to race it.
    #
    # All three seams are taken INSIDE the guard, not just the import. Reaching
    # for another module's privates is a drift risk this comment already names,
    # and a rename on the incumbent's side would otherwise raise an
    # AttributeError out of a function whose docstring promises it answers a
    # value — from inside a repair loop, where the caller can least afford a
    # surprise.
    try:
        from ..mindnet import worktree as mn_wt
        git, lock, is_toplevel = mn_wt._git, mn_wt._GIT_LOCK, mn_wt.is_toplevel
    except Exception as exc:                            # noqa: BLE001
        return RolledBack(lane_id=lane_id,
                          reason=f"cannot load the worktree driver: "
                                 f"{type(exc).__name__}: {exc}")
    # `is_lane_path` is a test of the PATH. With the lane's `.git` link gone,
    # git asked there walks up and answers for the MAIN checkout, from where
    # every file in the lane is untracked -- and `clean -fd` then empties it.
    # The guard behind the path guard is git's own answer about the top level,
    # asked only of a directory that exists: a lane that is not on disk is
    # left to `status` to refuse, which is a different fact. Asked UNDER the
    # lock, because between its answer and `checkout -- .` nothing may change
    # what git answers for the path, and a prune on another thread takes the
    # same lock; the lock is re-entrant, and the guard takes it again itself.
    with lock:
        if Path(path).is_dir() and not is_toplevel(path):
            return RolledBack(lane_id=lane_id,
                              reason=(f"{path} is not a checkout of its own: "
                                      f"git there answers for another tree; "
                                      f"discarding nothing"))
        ok, out, err = git(path, "status", "--porcelain")
        if not ok:
            return RolledBack(lane_id=lane_id,
                              reason=f"git status failed: {err or 'no reason'}")
        if not out:
            return RolledBack(lane_id=lane_id, ok=True, changed=False,
                              reason="the lane was already clean")
        ok, _out, err = git(path, "checkout", "--", ".")
        if not ok:
            return RolledBack(lane_id=lane_id,
                              reason=f"git checkout failed: {err or 'no reason'}")
        ok, _out, err = git(path, "clean", "-fd")
        if not ok:
            return RolledBack(lane_id=lane_id,
                              reason=f"git clean failed: {err or 'no reason'}")
    return RolledBack(lane_id=lane_id, ok=True, changed=True,
                      reason="uncommitted work discarded; commits kept")


_WRITE_BITS = 0o222


def _chmod_tests(path, writable) -> tuple:
    """Set or clear the write bits on every file of the JUDGE under *path*:
    `judge.judge_files`, the very list the gate compares, so what is protected
    and what is checked cannot drift apart -- everything under `tests/`, every
    `conftest.py` pytest would load, the root pytest config.

    Not the shared config files (`pyproject.toml`, `setup.cfg`, `tox.ini`): a
    child may bump a dependency there, and `judge.tests_mutated` judges those
    by their pytest section alone, which no file mode can express. Not a
    symlink either, whose mode is its target's and may lie outside the lane.

    Only the write bits move, from each file's own mode. A fixed 0o444 would
    drop an executable fixture's `x` bit, git would see the mode change, and
    the lock itself would then read as a test edit.

    Sorted lane-relative paths of what was actually changed. Total: a file the
    OS will not let us touch is skipped, because this is a deterrent layer and
    the gate's own check is the one that decides."""
    import os
    import stat

    from . import judge                                 # lazy, like the rest

    text = str(path or "")
    if not text:                                        # `Path("")` is the cwd
        return ()
    root = Path(text)
    done = []
    for rel in judge.judge_files(text):
        target = root / rel
        try:
            if target.is_symlink():
                continue
            mode = stat.S_IMODE(os.stat(target).st_mode)
            os.chmod(target, (mode | stat.S_IWRITE) if writable
                     else (mode & ~_WRITE_BITS))
            done.append(rel)
        except OSError:                                 # pragma: no cover - odd fs
            continue
    return tuple(done)


def lock_tests_readonly(path) -> tuple:
    """Make the lane's judge files read-only for the child (MEB K13).

    Only the FILES, never the `tests/` directory: a read-only directory stops
    pytest writing `__pycache__` there, and the point is to slow a child down,
    not to break the suite it is judged by. A child running as the same user
    can chmod this back, which is why it is the deterrent and not the gate --
    `judge.tests_mutated` compares against a commit pinned before the child
    ran, which the orchestrator holds and the lane does not."""
    return _chmod_tests(path, writable=False)


def unlock_tests(path) -> tuple:
    """The undo. Called on teardown WHATEVER the policy was, because a
    read-only file is one more thing that can make a removal fail on Windows
    and re-granting a bit that is already granted costs nothing."""
    return _chmod_tests(path, writable=True)


#: Whether `acquire_lanes` locks each new lane's tests. Off by default and set
#: by `orchestrator/cli.py` from `--immutable-tests`, because the only caller
#: between them is `lintester.py`, which this blueprint does not touch.
LOCK_TESTS = False


def acquire_lanes(root, count, mission_id="", lock_tests=None) -> list:
    """One `LaneWorktree` per lane, in lane order.

    Failures are returned, not raised and not skipped: a mission that got three
    of four trees should run three lanes and SAY the fourth is missing, and a
    silently shortened list makes that indistinguishable from a mission that only
    ever wanted three."""
    want = LOCK_TESTS if lock_tests is None else bool(lock_tests)
    out = []
    for i in range(max(0, int(count or 0))):
        lane = acquire(root, i, mission_id)
        if want and getattr(lane, "ok", False) and getattr(lane, "path", ""):
            lock_tests_readonly(lane.path)
        out.append(lane)
    return out


def release_lanes(root, lane_worktrees) -> list:
    """Release every lane tree. Total — one failure does not skip the rest."""
    out = []
    for lw in lane_worktrees or ():
        if not getattr(lw, "ok", False):
            continue
        if getattr(lw, "path", ""):
            unlock_tests(lw.path)
        out.append(release(root, lw))
    return out
