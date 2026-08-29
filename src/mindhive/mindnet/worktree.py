# src/mindhive/mindnet/worktree.py
"""MN-9 — one isolated checkout per orchestrated task.

MN-8 shipped agent execution capped at ONE agent at a time, for a real reason:
every agent ran with ``cwd=root``, the same working tree, and two agents editing
one tree concurrently corrupt each other's edits and the git index. All of MN-7's
parallel machinery — the frontier, the thread pool, `DEFAULT_MAX_WORKERS` — was
built and unused. This module is what lifts that cap.

## Cleanup never discards work

`retire` removes a worktree **only** when it is clean and not ahead of its base.
Anything else is left on disk and reported, because the alternative is deleting an
agent's output on the operator's behalf — and the whole point of running agents is
the work they produce. The branch survives worktree removal either way, so
committed work is safe in both directions; it is the *uncommitted* case that would
be lost, and that is exactly the case this refuses to touch.

`remove` is always called with ``--force``. That is this repository's own recorded
lesson, and `enforce.py` carries a learned rule about it: on Windows a removal
fails with "Permission denied" whenever any process, editor or AV still holds a
handle inside the tree, which for a directory an agent just finished writing is
the normal case rather than the exception. `prune` follows so the admin records
never drift from the directories.

## Total, because it gates real subprocesses

Every function answers a value; none raises. A `git worktree add` that fails must
become one task reporting `skipped`, not an exception that takes down a wave —
MN-7's P0 taught that an environmental failure which writes a status row removes
the task's whole subtree from the graph permanently.
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .. import paths

# One lock for every git command this module runs, held across the WHOLE of `add`
# and the whole of `retire`.
#
# The orchestrator calls both from inside its thread pool (`orchestrator.py:413`
# and `:458`), and every lane points at the SAME `.git`. `retire` ends with
# `git worktree prune` (below), which is global over `.git/worktrees/` — so
# without this it can sweep an admin entry another lane is in the middle of
# creating, and the loss reports as a task that mysteriously has no checkout.
#
# COARSE on purpose. Locking only the mutations would let the read-only queries
# run free and measured 2.2 s per wave faster at 12 lanes — against an agent
# timeout of 1800 s. That trade buys a rule ("is this call inside or outside the
# lock?") that has to be re-decided every time a git command is added here, and
# nothing fails when it is decided wrong. The whole function is the critical
# section; there is no second rule to remember.
#
# Re-entrant because `retire` calls `state`, which runs git of its own. Today
# `state` does not lock, so a plain `Lock` would also work — an `RLock` means a
# future edit that gives `state` the same protection cannot hang the pool. A
# deadlock inside an orchestrator is a far worse failure than a muddier boundary.
_GIT_LOCK = threading.RLock()

# Where isolated checkouts live, relative to the repo root. `.claude/worktrees/` is
# this repo's existing convention — the session that wrote this module is running
# from one — and the path is ignored by the committed `.gitignore` so an agent's
# tree never shows up as untracked changes in the parent.
WORKTREE_DIR = (".claude", "worktrees")

# One prefix for the directory and the branch, so a stray worktree can be traced
# back to the feature that made it by name alone.
PREFIX = "mn9"

_TIMEOUT_S = 60


@dataclass(frozen=True)
class Isolated:
    """One task's checkout. ``ok=False`` carries git's own reason.

    ``retryable`` separates the two kinds of failure, and the separation is the
    whole point: a contended `.git` produces a transient git error that trying
    again fixes, while every refusal this module makes on purpose — an existing
    directory, a branch holding unmerged commits, an unusable parent — is
    PERMANENT and describes something an operator has to look at. Retrying those
    is not resilience; it spends time pretending a settled answer is a flaky one,
    and the last attempt's reason then reads as if git were unreliable when
    nothing is. Only the `git worktree add` failure path sets this."""

    task_id: str
    path: str = ""
    branch: str = ""
    base: str = ""          # the commit the branch was created from
    ok: bool = False
    reason: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class Retired:
    """What `retire` did. ``kept=True`` means the directory is still on disk and
    holds work nobody has looked at yet."""

    removed: bool = False
    kept: bool = False
    reason: str = ""


def _git(root, *args):
    """Run one git command in *root*. Answers ``(ok, stdout, stderr)``.

    ``shell=False`` always (U6's standing rule), and `paths.NO_WINDOW` so a
    console child spawned from the window-less dashboard process does not flash a
    console window."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *[str(a) for a in args]],
            capture_output=True, text=True, shell=False, timeout=_TIMEOUT_S,
            creationflags=paths.NO_WINDOW)
        return (proc.returncode == 0, (proc.stdout or "").strip(),
                (proc.stderr or "").strip())
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", f"{type(exc).__name__}: {exc}"


def branch_for(task_id) -> str:
    return f"{PREFIX}/{task_id}"


def path_for(root, task_id) -> Path:
    return Path(root).joinpath(*WORKTREE_DIR) / f"{PREFIX}-{task_id}"


def add(root, task_id) -> Isolated:
    """Create an isolated checkout for *task_id*, or answer why not.

    Idempotent in the direction that matters: an existing directory is REFUSED
    rather than reused or clobbered. Reusing it would hand an agent a tree holding
    a previous run's uncommitted work and call it clean; clobbering it would delete
    that work. Refusing makes the operator look, which is the only safe option
    when the alternatives are "silently wrong" and "silently destructive".

    Serialised against every other call into this module — see `_GIT_LOCK`."""
    with _GIT_LOCK:
        return _add(root, task_id)


def _add(root, task_id) -> Isolated:
    """`add`'s body. Split out only so the lock's extent is one readable line
    rather than an indent wrapping fifty."""
    tid = str(task_id)
    target = path_for(root, tid)
    branch = branch_for(tid)

    if target.exists():
        return Isolated(task_id=tid, path=str(target), branch=branch, ok=False,
                        reason=(f"{target} already exists — a previous run left "
                                f"it behind. Inspect it, then remove it with "
                                f"`git worktree remove --force {target}`"))

    # The base is captured BEFORE the worktree exists, because "ahead" is only
    # meaningful relative to where the branch started. Deriving it later is not
    # possible without guessing which branch was the parent.
    base_ok, base, base_err = _git(root, "rev-parse", "HEAD")
    if not base_ok:
        return Isolated(task_id=tid, path=str(target), branch=branch, ok=False,
                        reason=base_err or "cannot resolve HEAD to branch from")

    # A leftover branch from an earlier retired run must not make every future run
    # of this task fail — but resetting one that HOLDS COMMITS destroys them.
    # `-B` did exactly that: an agent commits, `retire` keeps the tree and reports
    # it, the operator removes the directory as this function's own message tells
    # them to, and the next run force-moved the branch back to base and made the
    # commit unreachable. Silently, exit 0. The old docstring claimed "committed
    # work survives whichever way this goes"; it did not.
    #
    # So: reset only a branch with nothing on it, and refuse otherwise.
    has_branch, _o, _e = _git(root, "rev-parse", "--verify", "--quiet", branch)
    if has_branch:
        _ok2, count, _e2 = _git(root, "rev-list", "--count", f"HEAD..{branch}")
        try:
            unmerged = int((count or "0").strip() or 0)
        except ValueError:
            unmerged = 1                    # unknown reads as "there is work"
        if unmerged:
            return Isolated(
                task_id=tid, path=str(target), branch=branch, base=base,
                ok=False,
                reason=(f"branch {branch} already holds {unmerged} commit(s) "
                        f"that {'HEAD'} does not. Resetting it would make them "
                        f"unreachable — merge or rename it first "
                        f"(`git log {branch}`), then re-run"))

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # `exist_ok=True` still raises when the path exists as a FILE, and a
        # read-only or ACL-blocked parent raises too. This module's contract is
        # "answers a value, never raises" — and a raise here escapes `one()`'s
        # try in the orchestrator and takes down the whole wave.
        return Isolated(task_id=tid, path=str(target), branch=branch, base=base,
                        ok=False,
                        reason=f"cannot create {target.parent}: "
                               f"{type(exc).__name__}: {exc}")

    ok, _out, err = _git(root, "worktree", "add", "-B", branch, str(target))
    if not ok:
        # The ONLY retryable path. Everything above is a decision this module
        # made deliberately; this is git reporting that it could not do the work,
        # which on a contended `.git` is exactly the transient the caller's
        # bounded retry exists for.
        return Isolated(task_id=tid, path=str(target), branch=branch, base=base,
                        ok=False, retryable=True,
                        reason=err or "git worktree add failed")
    return Isolated(task_id=tid, path=str(target), branch=branch, base=base,
                    ok=True)


def state(path, base="") -> tuple:
    """``(dirty, ahead)`` for the worktree at *path*.

    ``dirty`` counts untracked AND IGNORED files. Untracked because an agent that
    created a file without committing it has produced work; ignored because plain
    `--porcelain` omits those, so output written under `dist/`, `build/`,
    `.pytest_cache/` or any other ignored path read as "clean" and was deleted by
    `remove --force`. A task like "build the desktop artifact" lands entirely in
    ignored paths.

    ``ahead`` is commits on this worktree that *base* does not have, and it
    REQUIRES *base* — `add` captures it for exactly this call. The first version
    tried to infer it with ``rev-list --not --remotes``, which in a repo with no
    remotes excludes nothing and therefore counts the entire history: every fresh
    worktree read as "ahead" and `retire` never removed anything. Without a base
    this answers 0 and the dirty check carries the guard alone.

    Answers ``(True, 0)`` when it cannot tell at all. Unknown must read as "there
    may be work here", because the only consumer is the decision to delete."""
    ok, out, _err = _git(path, "status", "--porcelain", "--ignored=matching")
    if not ok:
        return True, 0
    dirty = bool(out.strip())
    ahead = 0
    if base:
        ok2, out2, _err2 = _git(path, "rev-list", "--count", f"{base}..HEAD")
        if ok2:
            try:
                ahead = int(out2.strip() or 0)
            except ValueError:
                ahead = 0
    return dirty, ahead


def retire(root, path, branch="", base="") -> Retired:
    """Remove the worktree at *path* — but only if nothing would be lost.

    The branch is never deleted here, and `add` refuses to reset a branch that
    holds commits — those two together are what make committed work survive. The
    first version of this docstring claimed the removal alone was enough; it was
    not, because `add`'s `-B` then force-moved the branch back to base and made
    those commits unreachable.

    Ignored files count as work. `git status --porcelain` omits them, so a plain
    `--porcelain` read called an agent's output under `dist/`, `build/` or any
    other ignored path "clean" and `remove --force` erased it.

    Serialised against every other call into this module — see `_GIT_LOCK`. The
    `prune` below is why that matters: it is global over `.git/worktrees/`, so it
    must never run while another lane's `add` is half-finished."""
    with _GIT_LOCK:
        return _retire(root, path, branch, base)


def _retire(root, path, branch="", base="") -> Retired:
    """`retire`'s body — see `_add` for why this is split."""
    target = Path(path)
    if not target.exists():
        return Retired(removed=False, kept=False,
                       reason="already gone")

    dirty, ahead = state(target, base)
    if dirty or ahead:
        what = []
        if dirty:
            what.append("uncommitted changes")
        if ahead:
            what.append(f"{ahead} unmerged commit(s)")
        return Retired(
            removed=False, kept=True,
            reason=(f"kept {target}: it holds " + " and ".join(what)
                    + (f" on branch {branch}" if branch else "")))

    # `--force` unconditionally: on Windows a removal fails with "Permission
    # denied" whenever any handle is still open inside the tree, which right after
    # an agent finishes writing is the normal case.
    ok, _out, err = _git(root, "worktree", "remove", "--force", str(target))
    _git(root, "worktree", "prune")
    if not ok:
        return Retired(removed=False, kept=True,
                       reason=f"could not remove {target}: "
                              f"{err or 'git worktree remove failed'}")
    return Retired(removed=True, kept=False, reason="")
