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

## The merge-back's primitives live here too

`commit_all`, `merge_probe`, `merge_into`, `is_ancestor` and `rev_of` exist for
`orchestrator.integrate`, which commits each validated lane on the child's
behalf and merges the lanes into an integration branch. They are in this module
because it is the ONLY git driver: the lock, the timeout, `shell=False`, and
the refusal to force anything that could lose work are decided once, here.
`add` grew `start_point` for the same caller. `CACHE_DIRS` names what a test
run leaves behind, which neither `state` nor `commit_all` treats as work.
"""
from __future__ import annotations

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


def _git_rc(root, *args):
    """Run one git command in *root*. Answers ``(exit_code, stdout, stderr)``.

    The primitive under `_git`, for the callers that read git's exit code as
    an answer rather than a verdict: `merge-tree` and `merge-base
    --is-ancestor` say "no" with 1 and "could not tell" with anything else.
    ``-1`` when the process could not run at all. ``shell=False`` always (U6's
    standing rule), and `paths.NO_WINDOW` so a console child spawned from the
    window-less dashboard process does not flash a console window."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *[str(a) for a in args]],
            capture_output=True, text=True, shell=False, timeout=_TIMEOUT_S,
            creationflags=paths.NO_WINDOW)
        return (proc.returncode, (proc.stdout or "").strip(),
                (proc.stderr or "").strip())
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def _git(root, *args):
    """Run one git command in *root*. Answers ``(ok, stdout, stderr)``."""
    rc, out, err = _git_rc(root, *args)
    return rc == 0, out, err


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


def changed_files(path):
    """Repo-relative POSIX paths of every file the checkout at *path* has
    changed against its `HEAD` — modified, added, deleted, renamed — plus every
    untracked file that is not ignored. ``()`` for a clean tree; ``None`` when
    git could not say.

    Two commands rather than one `status --porcelain`, because `_git` strips
    its output and the first status line's leading column is whitespace:
    ``" M seed.txt"`` comes back as ``"M seed.txt"`` and no column parser
    survives that. `diff --name-only HEAD` and `ls-files --others
    --exclude-standard` print one bare path per line and nothing to parse.
    `core.quotepath=false` keeps a non-ASCII name as itself rather than as
    octal escapes.

    Under the lock like everything else here: the per-unit gate asks this from
    the scheduler's executor threads while another lane may be mid-`add`.
    Total."""
    with _GIT_LOCK:
        ok, tracked, _err = _git(path, "-c", "core.quotepath=false", "diff",
                                 "--name-only", "HEAD")
        if not ok:
            return None
        ok, untracked, _err = _git(path, "-c", "core.quotepath=false",
                                   "ls-files", "--others", "--exclude-standard")
        if not ok:
            return None
    out = []
    for line in (tracked + "\n" + untracked).splitlines():
        rel = line.strip().replace("\\", "/")
        if rel:
            out.append(rel)
    return tuple(dict.fromkeys(out))
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
    back as git's own reason, and nothing here bypasses either."""
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
