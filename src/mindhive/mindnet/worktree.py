# SPDX-License-Identifier: Apache-2.0
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

## Names as git holds them, and only at the top level

`changed_files`, `modified_tracked`, `tracked_at` and `changes_between` read
git with `-z` and split on NUL (`_git_paths`): `core.quotepath=false` still
C-quotes a DEL, `str.splitlines` breaks a name at LINE SEPARATOR or NEL, and
`strip` eats a trailing NO-BREAK SPACE, and each came back as a name the gate
compared against the wrong file. `is_toplevel` tells a checkout from a
directory inside one, because git asked in a subdirectory answers for the
tree around it -- and a lane whose `.git` link is gone is that case one level
up, answering for the MAIN checkout. `commit_all` refuses such a path before
staging anything, and `orchestrator.judge` and `orchestrator.worktree.rollback`
ask the same question before they read or discard.

## The merge-back's primitives live here too

`commit_all`, `merge_probe`, `merge_into`, `is_ancestor`, `rev_of` and
`commits_since` exist for `orchestrator.integrate`, which commits each validated
lane on the child's behalf, and merges into an integration branch only the
lanes whose every commit past the base it stamped. They are in this module
because it is the ONLY git driver: the lock, the timeout, `shell=False`, and
the refusal to force anything that could lose work are decided once, here.
`add` grew `start_point` for the same caller. `CACHE_DIRS` names what a test
run leaves behind, which neither `state` nor `commit_all` treats as work.
"""
from __future__ import annotations

import os
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

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

# Directories a test run leaves behind and no task ever asks for. `state` does
# not count them as work and `commit_all` never commits them: without this,
# every lane the per-unit gate ran pytest in was kept at teardown as
# "uncommitted changes" after its work had merged, and a lane whose repository
# ignores nothing would have committed its bytecode. A fixed list on purpose --
# "anything ignored" would put `dist/` back on the deletable side, which is the
# exact rule `state`'s docstring exists to keep.
CACHE_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
              ".hypothesis")


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


def _git_bytes(root, *args):
    """Run one git command in *root*. Answers ``(exit_code, stdout, stderr)``
    as BYTES. The module's one spawn site: `_git_rc` decodes what it answers,
    `show_at` returns it as the file content it is.

    ``-1`` when the process could not run at all. ``shell=False`` always (U6's
    standing rule), and `paths.NO_WINDOW` so a console child spawned from the
    window-less dashboard process does not flash a console window.

    Bytes, because decoding inside `subprocess.run` is where this went wrong.
    ``text=True`` decodes the pipe with the locale's codec -- cp1252 on
    Windows, which leaves five bytes undefined -- and git prints paths as UTF-8
    under `core.quotepath=false`. One such byte in a file NAME (`ā` is
    `C4 81`) killed the pipe's reader thread, and `run` then answered an EMPTY
    stdout with exit 0. No exception ever reached this function; the checkout
    was simply reported clean."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *[str(a) for a in args]],
            capture_output=True, shell=False, timeout=_TIMEOUT_S,
            creationflags=paths.NO_WINDOW)
        return proc.returncode, proc.stdout or b"", proc.stderr or b""
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, b"", f"{type(exc).__name__}: {exc}".encode("utf-8",
                                                               "replace")


def _decoded(data) -> str:
    """Git's output as text: UTF-8 with replacement, universal newlines,
    stripped -- what ``text=True`` gave, without the codec that could fail. A
    replacement character costs one path a wrong byte; the empty answer the
    locale codec produced lost every path at once."""
    text = bytes(data or b"").decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _git_rc(root, *args):
    """Run one git command in *root*. Answers ``(exit_code, stdout, stderr)``.

    The primitive under `_git`, for the callers that read git's exit code as
    an answer rather than a verdict: `merge-tree` and `merge-base
    --is-ancestor` say "no" with 1 and "could not tell" with anything else.
    ``-1`` when the process could not run at all."""
    rc, out, err = _git_bytes(root, *args)
    return rc, _decoded(out), _decoded(err)


def _git(root, *args):
    """Run one git command in *root*. Answers ``(ok, stdout, stderr)``."""
    rc, out, err = _git_rc(root, *args)
    return rc == 0, out, err


def _git_paths(root, *args):
    """Run one git LISTING in *root* -- a command given `-z` -- and answer the
    paths it printed, in git's order, or ``None`` when it failed.

    Split on NUL and decoded with replacement, never stripped, never
    line-split: with `-z` a path is printed verbatim, so a DEL, a LINE
    SEPARATOR, a NEL or a trailing NO-BREAK SPACE in a name comes back as the
    name, where the text reading before this C-quoted the first, broke at the
    next two and ate the last."""
    rc, out, _err = _git_bytes(root, *args)
    if rc != 0:
        return None
    return [chunk.decode("utf-8", errors="replace")
            for chunk in out.split(b"\x00") if chunk]


def branch_for(task_id) -> str:
    return f"{PREFIX}/{task_id}"


def path_for(root, task_id) -> Path:
    return Path(root).joinpath(*WORKTREE_DIR) / f"{PREFIX}-{task_id}"


def add(root, task_id, path=None, branch=None, start_point=None) -> Isolated:
    """Create an isolated checkout for *task_id*, or answer why not.

    Idempotent in the direction that matters: an existing directory is REFUSED
    rather than reused or clobbered. Reusing it would hand an agent a tree holding
    a previous run's uncommitted work and call it clean; clobbering it would delete
    that work. Refusing makes the operator look, which is the only safe option
    when the alternatives are "silently wrong" and "silently destructive".

    *path* and *branch* override the task-keyed defaults, and exist for exactly
    one caller: `orchestrator.worktree`, whose PRD spells its checkouts
    `.worktrees/lane-N` rather than `.claude/worktrees/mn9-<task>`. They are
    optional, they default to the derivations this module has always used, and
    every existing caller passes positionally — so nothing about the incumbent
    behaviour moves. The alternative was a second module issuing its own `git
    worktree add`, which would have to re-derive the two rules below that keep
    committed work reachable, and would drift from them the first time one
    changed.

    *start_point* is the merge-back's one addition: the commit the branch is
    made from and recorded as `base`, instead of `HEAD`. `orchestrator.integrate`
    makes its integration checkout from the lanes' common base with it, so the
    branch a person picks up starts where the lanes started even when the main
    checkout has moved on. Absent, the git commands are the ones this function
    has always run, byte for byte.

    Serialised against every other call into this module — see `_GIT_LOCK`."""
    with _GIT_LOCK:
        return _add(root, task_id, path, branch, start_point)


def _add(root, task_id, path=None, branch=None, start_point=None) -> Isolated:
    """`add`'s body. Split out only so the lock's extent is one readable line
    rather than an indent wrapping fifty."""
    tid = str(task_id)
    start = str(start_point) if start_point else "HEAD"
    target = Path(path) if path else path_for(root, tid)
    branch = str(branch) if branch else branch_for(tid)

    if target.exists():
        return Isolated(task_id=tid, path=str(target), branch=branch, ok=False,
                        reason=(f"{target} already exists — a previous run left "
                                f"it behind. Inspect it, then remove it with "
                                f"`git worktree remove --force {target}`"))

    # The base is captured BEFORE the worktree exists, because "ahead" is only
    # meaningful relative to where the branch started. Deriving it later is not
    # possible without guessing which branch was the parent.
    base_ok, base, base_err = _git(root, "rev-parse", start)
    if not base_ok:
        return Isolated(task_id=tid, path=str(target), branch=branch, ok=False,
                        reason=base_err or f"cannot resolve {start} to branch from")

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
        _ok2, count, _e2 = _git(root, "rev-list", "--count", f"{start}..{branch}")
        try:
            unmerged = int((count or "0").strip() or 0)
        except ValueError:
            unmerged = 1                    # unknown reads as "there is work"
        if unmerged:
            return Isolated(
                task_id=tid, path=str(target), branch=branch, base=base,
                ok=False,
                reason=(f"branch {branch} already holds {unmerged} commit(s) "
                        f"that {start} does not. Resetting it would make them "
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

    ok, _out, err = _git(root, "worktree", "add", "-B", branch, str(target),
                         *([start] if start_point else []))
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


def _is_work(line) -> bool:
    """Whether one `status --porcelain` line names work. An untracked (`??`)
    or ignored (`!!`) entry under one of `CACHE_DIRS` does not; everything
    else does, including a tracked change anywhere."""
    text = line.strip()
    if not text:
        return False
    if text[:2] not in ("??", "!!"):
        return True
    rel = text[2:].strip().strip('"').replace("\\", "/")
    return not any(part in CACHE_DIRS for part in rel.strip("/").split("/"))


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

    The one exemption is `CACHE_DIRS`: an untracked or ignored entry under
    `__pycache__/`, `.pytest_cache/` and the like is what a test run leaves
    behind, never a task's output, and counting it kept every gated lane on
    disk after its work had merged. A tracked change anywhere still counts.

    Answers ``(True, 0)`` when it cannot tell at all. Unknown must read as "there
    may be work here", because the only consumer is the decision to delete."""
    # `-uall` lists untracked FILES, not the directories collapsing them: a
    # lane's only leftovers are `src/__pycache__/*.pyc`, and collapsed to
    # `?? src/` the cache exemption below could never see them.
    ok, out, _err = _git(path, "status", "--porcelain", "--ignored=matching",
                         "-uall")
    if not ok:
        return True, 0
    dirty = any(_is_work(line) for line in out.splitlines())
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


def _rev(rev):
    """*rev* as git should receive it, or ``None`` when it cannot be one.

    A revision here is an argument, and git reads an argument that starts with
    ``-`` as an OPTION -- `--output=<file>` makes `diff` write a file. Every rev
    this module is handed is `HEAD` or came out of `rev-parse`, so refusing the
    rest costs nothing and shuts the one door that argument position has."""
    text = str(rev or "").strip()
    if not text or text.startswith("-"):
        return None
    return text


def changed_files(path, rev="HEAD"):
    """Repo-relative POSIX paths of every file the checkout at *path* has
    changed against *rev* — modified, added, deleted, renamed — plus every
    untracked file that is not ignored. ``()`` for a clean tree; ``None`` when
    git could not say, or when *rev* cannot be a revision (`_rev`).

    *rev* is `HEAD` unless a caller pinned an earlier commit, and
    `orchestrator.gate` does: it pins the commit a unit STARTED from. A child
    that commits its own work moves `HEAD` past that work, and "changed against
    `HEAD`" then answers that the unit changed nothing at all.

    Two listings rather than one `status --porcelain`, because a status line's
    leading column is whitespace and no column parser survives `_decoded`'s
    strip. `diff --name-only --no-renames` and `ls-files --others
    --exclude-standard`, both with `-z` (`_git_paths`): one NUL-terminated
    path each, taken verbatim -- `core.quotepath=false` still C-quoted a DEL,
    `str.splitlines` broke a name at LINE SEPARATOR or NEL, and `strip` ate a
    trailing NO-BREAK SPACE, and each came back as a name the gate compared
    against the wrong file. `--no-renames`, because a moved module changed at
    BOTH ends and the tests naming the old path have to run too.

    Under the lock like everything else here: the per-unit gate asks this from
    the scheduler's executor threads while another lane may be mid-`add`.
    Total."""
    rev = _rev(rev)
    if rev is None:
        return None
    with _GIT_LOCK:
        tracked = _git_paths(path, "diff", "--name-only", "-z", "--no-renames",
                             rev, "--")
        if tracked is None:
            return None
        untracked = _git_paths(path, "ls-files", "-z", "--others",
                               "--exclude-standard")
        if untracked is None:
            return None
    return tuple(dict.fromkeys([*tracked, *untracked]))


def modified_tracked(path, rev="HEAD"):
    """Repo-relative POSIX paths of files that EXISTED at *rev* in the
    checkout at *path* and have since been modified, deleted or had their type
    changed -- by a commit made since *rev* or in the working tree, alike.
    ``None`` when git could not say, so a caller can tell "nothing changed"
    from "I do not know".

    The distinction `changed_files` cannot make. `orchestrator.gate` refuses a
    unit that edited a test the lane was checked out WITH, while a test the
    unit CREATED is the whole point of the greenfield flow.

    That line is drawn by `--diff-filter=DMT`, not by "tracked": a plain
    `diff HEAD` also lists a new file that has merely been STAGED, and a lane
    carries staged new files whenever `commit_all` ran `git add -A` and the
    commit itself was then refused (a hook, no identity, an unusable signing
    key). The next unit on that lane would be refused for a file an earlier,
    validated unit created -- and no rollback clears it, because `checkout`
    and `clean` both leave the index alone. `--no-renames` reports a rename as
    the delete of the path that existed, which is the one this contract names.

    Read with `-z` (see `changed_files`), under `_GIT_LOCK` like every other
    invocation in this module. Total."""
    rev = _rev(rev)
    if rev is None:
        return None
    with _GIT_LOCK:
        tracked = _git_paths(path, "diff", "--name-only", "-z", "--no-renames",
                             "--diff-filter=DMT", rev, "--")
    if tracked is None:
        return None
    return tuple(dict.fromkeys(tracked))


def show_at(path, rel, rev="HEAD"):
    """The bytes of *rel* as committed at *rev* in the checkout at *path*, or
    ``None`` when it was not there or git could not say.

    Bytes, straight from `_git_bytes`, because this returns FILE CONTENT: a
    decode would alter it and fold its line endings, and the caller compares
    what pytest will read. `cat-file blob` rather than `show`, because it is
    the command that promises the stored bytes with no conversion of any kind.
    Under the lock. Total."""
    rev = _rev(rev)
    if rev is None:
        return None
    spec = f"{rev}:" + str(rel or "").replace("\\", "/").lstrip("/")
    with _GIT_LOCK:
        rc, out, _err = _git_bytes(path, "cat-file", "blob", spec)
    return out if rc == 0 else None


def tracked_at(path, rev="HEAD"):
    """Every repo-relative POSIX path committed at *rev*, as a frozenset, or
    ``None`` when git could not say.

    The other half of `modified_tracked`, which names what changed among the
    files *rev* held: this is how a caller tells a file that is NEW since *rev*
    from one that was always there. A new `conftest.py` changes what a suite
    does exactly as much as an edited one. Read with `-z` (see
    `changed_files`). Total."""
    rev = _rev(rev)
    if rev is None:
        return None
    with _GIT_LOCK:
        held = _git_paths(path, "ls-tree", "-r", "-z", "--name-only",
                          "--full-tree", rev)
    return None if held is None else frozenset(held)


def changes_between(path, base, head):
    """``((status, path), ...)`` for every file that differs between the
    commits *base* and *head* in the checkout at *path*, in git's path order:
    `A`dded, `D`eleted, `M`odified or `T`ype-changed at *head* against *base*.
    The working tree and the index are no part of the answer -- this is the
    commit-to-commit question `orchestrator.judge.judge_changes` asks before
    `SuiteGate.begin` trusts a later commit on a shared lane. `--no-renames`,
    so a move is the delete of the path that was and the add of the path that
    is, which is what a judge comparing files by name needs. ``()`` when the
    two trees are the same; ``None`` when git could not say or either
    revision cannot be one (`_rev`), decided before git runs. Under the lock.
    Total."""
    base, head = _rev(base), _rev(head)
    if base is None or head is None:
        return None
    with _GIT_LOCK:
        rows = _git_paths(path, "diff", "--name-status", "-z", "--no-renames",
                          base, head, "--")
    if rows is None:
        return None
    pairs = iter(rows)
    return tuple((status[:1], rel) for status, rel in zip(pairs, pairs))


def is_toplevel(path) -> bool:
    """Whether *path* is the top level of a checkout git registered -- not a
    directory inside one, and not a tree that merely carries a `.git` file.

    git asked in a subdirectory answers for the checkout around it: every
    path relative to a root the caller never named, `add -A -- .` staging one
    subtree of a tree `commit` then commits whole. A lane whose `.git` link
    is gone is that case one level up -- git walks out of the lane and
    answers for the MAIN checkout, from where every file in the lane is
    untracked and a `clean -fd` empties it. So `rev-parse --show-toplevel`
    must name *path* itself, and when the admin directory
    (`--absolute-git-dir`) holds a `gitdir` back-pointer -- a linked
    worktree's does -- it must point back at *path*, which a COPY of a lane
    fails: its `.git` file names the original's admin directory, and git
    would act on the original's records.

    A LINK standing at *path* -- a junction or a symlink -- is refused before
    git is asked: it passes every test of the path's shape and resolves to
    whatever it names, another lane or the main checkout, which git then
    vouches for as a top level of its own; a rollback or a commit through
    it would act on THAT tree. The lane must be the directory (`is_link`).
    The back-pointer may be relative (`worktree.useRelativePaths`, git
    2.48+) and is resolved against the admin directory that holds it.

    Asks git through `_git_bytes` directly, ONCE -- `rev-parse` answers both
    questions on consecutive lines -- so a caller that fakes `_git` to watch
    the commands a function issues does not see this one, and the guard that
    every commit, rollback and gate check runs first costs one spawn. Not
    cached, on purpose: the guard must see the link vanish. ``False`` for an
    empty path, a link, one that is not a directory, or anything git cannot
    answer. Never raises."""
    text = str(path or "")
    if not text or is_link(text):
        return False
    try:
        target = Path(text).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not target.is_dir():
        return False
    with _GIT_LOCK:
        rc, out, _err = _git_bytes(target, "rev-parse", "--show-toplevel",
                                   "--absolute-git-dir")
    if rc != 0:
        return False
    lines = _decoded(out).splitlines()
    if len(lines) < 2:
        return False
    top, admin = lines[0].strip(), lines[1].strip()
    try:
        if (os.path.normcase(str(Path(top).resolve()))
                != os.path.normcase(str(target))):
            return False
        link = Path(admin) / "gitdir"
        if not link.is_file():
            return True
        raw = link.read_text(encoding="utf-8", errors="replace").strip()
        back = (Path(admin) / raw).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return os.path.normcase(str(back)) == os.path.normcase(str(target / ".git"))


def is_link(path) -> bool:
    """Whether *path* is a symbolic link or a Windows junction. `os.walk`
    stops at the first (`followlinks=False`) and walks straight into the
    second, `Path.resolve` follows both, and `Path.is_symlink` sees only the
    first; the readers that must not cross into another tree -- the judge's
    walk, `is_toplevel` -- ask this. ``False`` for a path that cannot be
    stat'ed. Never raises."""
    try:
        st = os.lstat(str(path))
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) == stat.IO_REPARSE_TAG_MOUNT_POINT


# --- the merge-back's primitives ------------------------------------------------
#
# Added for `orchestrator.integrate`, and here rather than there because this
# module is the only git driver: the lock, the timeout, `shell=False`, and the
# refusal to force anything that could lose work are decided once, in one
# file. Each answers a value and never raises.


class Committed(NamedTuple):
    """What `commit_all` did. ``changed=False`` with ``ok=True`` is a recorded
    no-op: nothing to commit is not a failure."""

    ok: bool = False
    sha: str = ""
    changed: bool = False
    reason: str = ""


def commit_all(path, message) -> Committed:
    """Commit everything in the checkout at *path* -- tracked changes and
    untracked files alike, except `CACHE_DIRS` -- as one commit with *message*.

    `add -A` with the caches excluded, then `diff --cached --quiet` to learn
    whether anything was staged at all: a tree whose only additions are
    bytecode answers ``changed=False`` rather than failing at `commit` with
    "nothing to commit". Identity, hooks and the operator's config bind this
    exactly as they bind a person: a refusing hook or a missing identity comes
    back as git's own reason, and nothing here bypasses either.

    Refused before anything is staged when *path* is not a checkout's top
    level (`is_toplevel`): `add -A -- .` in a subdirectory stages that
    subtree and `commit` then commits the WHOLE index, whatever else was
    staged, under a message about one lane's unit -- and a lane whose `.git`
    link is gone is that call made against the main checkout."""
    if not is_toplevel(path):
        return Committed(reason=f"{path} is not a checkout's top level; "
                                f"nothing staged")
    excludes = [f":(exclude,glob)**/{d}/**" for d in CACHE_DIRS]
    with _GIT_LOCK:
        ok, _out, err = _git(path, "add", "-A", "--", ".", *excludes)
        if not ok:
            return Committed(reason=f"git add failed: {err or 'no reason'}")
        rc, _out, err = _git_rc(path, "diff", "--cached", "--quiet")
        if rc == 0:
            ok, sha, _err = _git(path, "rev-parse", "HEAD")
            return Committed(ok=True, sha=sha if ok else "", changed=False,
                             reason="nothing to commit")
        if rc != 1:
            return Committed(reason=f"git diff --cached failed: "
                                    f"{err or 'no reason'}")
        ok, out, err = _git(path, "commit", "-q", "-m", str(message))
        if not ok:
            return Committed(reason=f"git commit failed: "
                                    f"{err or out or 'no reason'}")
        ok, sha, err = _git(path, "rev-parse", "HEAD")
    if not ok:
        return Committed(ok=True, changed=True,
                         reason=f"committed, but HEAD could not be read: {err}")
    return Committed(ok=True, sha=sha, changed=True)


def _is_oid(text) -> bool:
    """Whether *text* is an object name git just printed (sha-1 or sha-256)."""
    return len(text) in (40, 64) and all(c in "0123456789abcdef" for c in text)


def validate_payload(payload) -> None:
    """The `merge_conflict_detected` contract (DATA_MODEL.md section 6.2).
    `branch` names the lane that collided, `conflicts` are the paths it
    collided on, and `tree` is the object name `git merge-tree` wrote while
    finding them -- the proof the probe really ran."""
    if not isinstance(payload, dict):
        raise ValueError("merge_conflict_detected payload must be an object")
    for key in ("branch", "tree"):
        if not isinstance(payload.get(key), str) or not payload.get(key):
            raise ValueError(f"merge_conflict_detected {key} must be a "
                             "non-empty string")
    rows = payload.get("conflicts")
    if not isinstance(rows, list) or not rows:
        raise ValueError("merge_conflict_detected conflicts must be a "
                         "non-empty list")
    if any(not isinstance(row, str) or not row for row in rows):
        raise ValueError("merge_conflict_detected conflicts must all be "
                         "non-empty strings")


# Payloads this module's OWN validator refused, newest last and capped. Not the
# ledger's business and never a reason to fail a probe -- but not nothing
# either: see `_record_conflict`.
CONTRACT_VIOLATIONS: list = []
_VIOLATION_CAP = 20


def _record_conflict(branch, conflicts, tree) -> None:
    """Best effort, and deliberately so. Answering "would this conflict" is
    the job; recording it is the side benefit that makes a repeated collision
    visible. An unwritable ledger must never turn a probe that ANSWERED into a
    probe that raised, so a failure to WRITE is swallowed.

    One failure is told apart from the rest. A payload `validate_payload`
    refuses is not the store failing, it is THIS MODULE failing: the two are
    opposite kinds of fault, and swallowing the second alongside the first let
    the contract drift with nothing whatever to show for it. It still must not
    raise into the probe, so it lands in `CONTRACT_VIOLATIONS` instead."""
    payload = {"branch": str(branch), "conflicts": list(conflicts),
               "tree": str(tree)}
    try:
        validate_payload(payload)
    except ValueError as exc:
        CONTRACT_VIOLATIONS.append(f"merge_conflict_detected: {exc}")
        del CONTRACT_VIOLATIONS[:-_VIOLATION_CAP]
        return
    try:
        from .. import events as ev_mod
        ev_mod.append_event("merge_conflict_detected", payload,
                            tags=["auto", "integration"])
    except Exception:                                   # noqa: BLE001
        pass


class Probe(NamedTuple):
    """What `merge_probe` found. ``clean`` is ``True``, ``False`` or ``None``
    -- three answers, because "could not tell" must never read as "no"."""

    clean: object = None
    conflicts: tuple = ()
    tree: str = ""
    reason: str = ""


def merge_probe(path, branch) -> Probe:
    """Would merging *branch* into the checkout at *path* conflict? Answered
    WITHOUT touching HEAD, the index or the working tree.

    `git merge-tree --write-tree` (git 2.38+) performs the merge in the object
    database only: exit 0 is a clean merge with the tree it would make, exit 1
    lists the conflicting paths (`--name-only`), anything else is a probe that
    could not run. The plan's U15 discipline in one call: the answer to "does
    this conflict" is obtained before anything is mutated."""
    with _GIT_LOCK:
        rc, out, err = _git_rc(path, "merge-tree", "--write-tree",
                               "--name-only", "HEAD", str(branch))
    if rc == 0:
        lines = out.splitlines()
        return Probe(clean=True, tree=(lines[0].strip() if lines else ""))
    if rc == 1:
        # The tree, then one conflicted path per line, then a blank line and
        # git's informational messages.
        head, _sep, _info = out.partition("\n\n")
        rows = [ln.strip() for ln in head.splitlines() if ln.strip()]
        # Exit 1 is ALSO how an unknown ref comes back, with nothing on
        # stdout. A real conflict always prints the tree it made first, so
        # that oid is what separates "conflicts" from "could not tell".
        if not rows or not _is_oid(rows[0]):
            return Probe(clean=None, reason=(err or out or "git merge-tree "
                                             "exited 1 without a tree"))
        conflicts = tuple(dict.fromkeys(rows[1:]))
        _record_conflict(branch, conflicts, rows[0])
        return Probe(clean=False, conflicts=conflicts, tree=rows[0],
                     reason=(f"{len(conflicts)} conflicting path(s): "
                             + ", ".join(conflicts)))
    return Probe(clean=None, reason=err or out or f"git merge-tree exited {rc}")


class Merged(NamedTuple):
    """What `merge_into` did."""

    ok: bool = False
    sha: str = ""
    reason: str = ""


def merge_into(path, branch, message) -> Merged:
    """Merge *branch* into the checkout at *path* as a merge commit, or leave
    the tree exactly as it was.

    `--no-ff` always: a fast-forward would leave no commit that says which lane
    this was, and the integration branch is read by a person. A merge that
    fails for any reason -- a conflict the probe should have caught, a hook, a
    dirty index -- is aborted before it is reported, so the checkout is never
    left mid-merge for the next lane to trip over."""
    with _GIT_LOCK:
        ok, out, err = _git(path, "merge", "--no-ff", "--no-edit", "-m",
                            str(message), str(branch))
        if not ok:
            _git(path, "merge", "--abort")
            return Merged(reason=err or out or "git merge failed")
        ok, sha, _err = _git(path, "rev-parse", "HEAD")
    return Merged(ok=True, sha=sha if ok else "")


def is_ancestor(root, ancestor, ref):
    """Whether *ancestor* is reachable from *ref*: ``True``, ``False``, or
    ``None`` when git could not answer (an unknown ref, a repository that
    would not open). `merge-base --is-ancestor` exits 0, 1 or something else
    for exactly those three, and the third must never be read as the second:
    this is the merge-back's proof that a lane really landed."""
    with _GIT_LOCK:
        rc, _out, _err = _git_rc(root, "merge-base", "--is-ancestor",
                                 str(ancestor), str(ref))
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


def rev_of(root, ref) -> str:
    """The commit *ref* names, or ``""``."""
    with _GIT_LOCK:
        ok, out, _err = _git(root, "rev-parse", "--verify", "--quiet",
                             f"{ref}^{{commit}}")
    return out.strip() if ok else ""


class LogEntry(NamedTuple):
    """One commit as `commits_since` reads it: its object name and the values
    of the one trailer asked for, in the order git prints them -- ``()`` when
    the message carries none."""

    sha: str = ""
    trailers: tuple = ()


def _is_trailer_key(key) -> bool:
    """A trailer token as git spells one: letters, digits and dashes, starting
    with a letter. Anything else is refused before it reaches a format string,
    where a comma would read as the next option."""
    return (bool(key) and key.isascii() and key[0].isalpha()
            and key.replace("-", "").isalnum())


def commits_since(root, base, ref, trailer):
    """Every commit *ref* has that *base* does not (``base..ref``), newest
    first, each with the values of *trailer* as git's OWN trailer parser reads
    the message -- `%(trailers:key=...)`: the last paragraph, `Key: value`
    lines -- so a key mentioned in a body is not one. ``()`` when nothing is
    ahead; ``None`` when git could not answer (an unknown rev, a repository
    that would not open) or was not asked: a rev that reads as an option
    (`--output=x..HEAD` makes `log` WRITE a file), or a key that is not a
    trailer token. Reads only; total.

    For `orchestrator.integrate`, which delivers a lane only when every commit
    past the base carries the stamp `commit_lane` puts there. NUL and SOH
    separate the fields and the records because, unlike the unit separators,
    Python does not count them as whitespace to strip."""
    base, ref, key = str(base or ""), str(ref or ""), str(trailer or "")
    if (not base or not ref or base.startswith("-") or ref.startswith("-")
            or not _is_trailer_key(key)):
        return None
    fmt = f"%H%x00%(trailers:key={key},valueonly,separator=%x00)%x01"
    with _GIT_LOCK:
        ok, out, _err = _git(root, "log", f"--format={fmt}", f"{base}..{ref}",
                             "--")
    if not ok:
        return None
    entries = []
    for record in out.split("\x01"):
        sha, *values = record.strip(" \t\r\n").split("\x00")
        if not sha:
            continue
        if not _is_oid(sha):
            return None
        entries.append(LogEntry(sha, tuple(v for v in (x.strip(" \t\r\n")
                                                        for x in values) if v)))
    return tuple(entries)
