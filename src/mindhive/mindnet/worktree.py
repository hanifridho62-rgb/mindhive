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
from dataclasses import dataclass
from pathlib import Path

from .. import paths

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
    """One task's checkout. ``ok=False`` carries git's own reason."""

    task_id: str
    path: str = ""
    branch: str = ""
    base: str = ""          # the commit the branch was created from
    ok: bool = False
    reason: str = ""


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
    when the alternatives are "silently wrong" and "silently destructive"."""
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

    target.parent.mkdir(parents=True, exist_ok=True)
    # `-B` rather than `-b`: a leftover branch from an earlier run whose worktree
    # was already retired must not make every future run of this task fail. The
    # branch is reset to the current base, which is what a fresh attempt means.
    ok, _out, err = _git(root, "worktree", "add", "-B", branch, str(target))
    if not ok:
        return Isolated(task_id=tid, path=str(target), branch=branch, base=base,
                        ok=False, reason=err or "git worktree add failed")
    return Isolated(task_id=tid, path=str(target), branch=branch, base=base,
                    ok=True)


def state(path, base="") -> tuple:
    """``(dirty, ahead)`` for the worktree at *path*.

    ``dirty`` counts untracked files too — an agent that created a file and did not
    commit it has produced work, and `--porcelain` lists it.

    ``ahead`` is commits on this worktree that *base* does not have, and it
    REQUIRES *base* — `add` captures it for exactly this call. The first version
    tried to infer it with ``rev-list --not --remotes``, which in a repo with no
    remotes excludes nothing and therefore counts the entire history: every fresh
    worktree read as "ahead" and `retire` never removed anything. Without a base
    this answers 0 and the dirty check carries the guard alone.

    Answers ``(True, 0)`` when it cannot tell at all. Unknown must read as "there
    may be work here", because the only consumer is the decision to delete."""
    ok, out, _err = _git(path, "status", "--porcelain")
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

    The branch is never deleted. Committed work therefore survives whichever way
    this goes, and the only thing a removal could destroy is uncommitted changes,
    which is precisely the case that is refused."""
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
