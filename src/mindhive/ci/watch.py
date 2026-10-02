# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/watch.py
"""`hive ci watch` -- keep every open PR's pre-merge verdict current
(spec 2026-09-30).

A pass reads the open PRs and, for each, what `premerge` last tested
(`state/ci_premerge.json`):

* **a head it never tested** -- a new PR, or a push -- is tested in full
  (`Premerge.run_pr`, never with `--merge`);
* **the same head on the same base** is left alone;
* **the same head on a base that moved** is checked with one `git merge-tree`
  -- seconds, not the hour a full run costs. A merge that now conflicts turns
  the PR red at once, with a `mindhive-ci` failure status naming the files. A
  merge that is still clean keeps its verdict -- as a PR's checks survive a
  push to its base on GitHub -- and `premerge --merge` re-checks the tree
  before it merges anyway. A PR that was red for a conflict alone and now
  merges cleanly is tested in full: no leg ever ran on it. A PR whose last
  check could not be made is tried again when the base moves.

A conflict is recorded as `premerge` records a verdict (`Premerge.
record_conflict`), so the ledger and `hive ci status` see the PR go red. A red
that errors alone made -- a leg whose setup broke, no test failing and no
conflict -- is tested again once for its head: what broke may have been this
machine. A verdict GitHub refused to take as a status is posted again on the
next pass.

A PR from a fork is skipped: its legs would run its code here, and only
`hive ci premerge N --trust`, once its diff is read, runs it. A PR whose last
run tested fewer legs than this watch asks for is tested again.

It never merges: that is `hive ci premerge N --merge`, asked for by name.
`--once` makes one pass, for a scheduled task. Without it the watch loops,
sleeping `--interval` seconds between passes; a pass that fails is reported
and the loop goes on. Each pass asks this machine again what it can run.

One pass runs at a time in a namespace. A pass holds `state/ci_watch.lock` from
its first step to its last, and a pass that finds it held does nothing else: one
`skipped` action names the lock, its holder's pid and how long it has held it,
and `--once` exits 0. A PR pushed after the running pass listed the open PRs
waits for the next pass. The lock is a kernel byte-range lock (`PassLock`), for
the harvest lock's reason: a pass has no bound -- one full run is most of an
hour, and a scheduled `--once` may fire every half hour -- so no stale age is
right, and a pid written in a file outlives its process (a reboot mid-pass, and
the OS may hand it to anything). The OS lets go of a kernel lock when its
process ends, however it ends, and the file it stands on is never removed; the
pid in it only names the holder, and a record whose pid is gone names none. A
holder past `STUCK_S`, half a day, makes that action an error and `--once` exit
2 -- still no pass -- so a pass wedged on a child that never returns cannot keep
every scheduled `--once` exiting 0. A loop holds the lock for each pass and not
while it sleeps, so a scheduled `--once` can take its turn between. A lock that
cannot be taken at all is an error and no pass: unlocked, two passes would spend
an hour each on one head. `hive ci premerge` takes no part in it, so a merge
asked for by name never waits behind a watch pass.

Every action carries the same five keys -- `pr`, `action`, `verdict`,
`exit_code` and `detail` -- so a script reads one shape; `--json` prints each
pass's actions as one JSON line. An action about the pass rather than a PR has
`pr` 0: the one a held lock makes -- `skipped`, or the error past `STUCK_S` --
also has `lock`, `holder_pid` and `held_s` (None when the record names no live
holder), and the error one a lock that cannot be taken makes has `lock`.
"""
import os
import time
from pathlib import Path

from .. import paths
from ..core.locks import pid_alive
from . import github as gh_mod
from . import legs as legs_mod
from . import premerge as premerge_mod
from . import preview as preview_mod

# The watch lock's mechanics are the harvest lock's: `msvcrt` on Windows, `fcntl`
# everywhere else, both a real OS byte-range lock that the kernel releases when
# its holder dies (`harvest.harvest_lock` says why a pass with no bound needs
# one; `events._append_lock` holds the measurements).
try:                                                    # pragma: no cover
    import msvcrt
    _flock = None
except ImportError:                                     # pragma: no cover
    msvcrt = None
    import fcntl as _flock

__all__ = ["DEFAULT_INTERVAL_S", "LOCK", "STUCK_S", "PassLock", "Watcher", "describe",
           "holder", "lock_path"]

DEFAULT_INTERVAL_S = 300.0
#: The file a pass holds, in the namespace's `state/`. It is never removed.
LOCK = "ci_watch.lock"
#: How long a pass may hold the lock before the pass that finds it held calls it
#: stuck: half a day, where one full run is most of an hour. Past it the finding
#: is an error, not a skip -- still no pass, since the lock is held.
STUCK_S = 12 * 3600
#: The holder's record -- `<pid> <epoch seconds>`, padded with spaces -- fills
#: this many bytes at the head of the lock file, and the byte locked is the one
#: after them: Windows refuses a read that reaches a byte another handle locked.
_RECORD = 64


def lock_path(project=None):
    """Where the watch lock of *project* stands."""
    return paths.state_dir(project) / LOCK


def _lock_byte(fd) -> None:
    """Lock the byte after the record on *fd*, never waiting. An `OSError`
    when it cannot be had: `BlockingIOError` (POSIX) or `PermissionError`
    (Windows) when another holder has it, anything else for a real fault."""
    if msvcrt is not None:
        os.lseek(fd, _RECORD, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        _flock.flock(fd, _flock.LOCK_EX | _flock.LOCK_NB)


def _unlock_byte(fd) -> None:
    try:
        if msvcrt is not None:
            os.lseek(fd, _RECORD, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            _flock.flock(fd, _flock.LOCK_UN)
    except OSError:
        pass


def _record(fd, text) -> None:
    """Write *text* as the holder's record. Best effort: the record says who
    holds the lock and since when; it never decides whether anyone does."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, text.encode("ascii").ljust(_RECORD)[:_RECORD])
    except OSError:
        pass


class PassLock:
    """The lock a watch pass holds: a kernel byte-range lock on *path*, taken
    without waiting and let go of by the OS when its process ends, however it
    ends -- so there is no stale age to get wrong, and no pid to outlive its
    process. The file is never removed: on POSIX `flock` binds to the inode, and
    a fresh file would be a second lock. Every taker opens a descriptor of its
    own, so two passes in one process contend as two processes do."""

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    def take(self) -> bool:
        """True when this process now holds the lock, False when another
        holder has it. An `OSError` when the file cannot be opened, or locked
        for any other reason."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0),
                     0o666)
        try:
            _lock_byte(fd)
        except (BlockingIOError, PermissionError):      # held: EWOULDBLOCK, EACCES
            os.close(fd)
            return False
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        _record(fd, f"{os.getpid()} {int(time.time())}")
        return True

    def release(self) -> None:
        """Let go: the record blanked, the byte unlocked, the file closed. A
        lock not held is let go of as nothing."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        _record(fd, "")
        _unlock_byte(fd)
        try:
            os.close(fd)
        except OSError:
            pass


def holder(path) -> tuple:
    """`(pid, held_s)` from the record at *path*: who holds the lock, and for
    how many seconds. Both None when it cannot be read -- a holder that has not
    written it yet, a lock let go of, no file -- or when the pid it names is no
    longer alive: a pass that died leaves its record until the next holder
    writes its own, and the OS may since have handed that pid to anything."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None, None
    try:
        head = os.read(fd, _RECORD)                     # the record alone: see _RECORD
    except OSError:
        return None, None
    finally:
        os.close(fd)
    fields = head.split()
    if len(fields) != 2 or not all(f.isdigit() for f in fields) or not int(fields[0]):
        return None, None
    pid = int(fields[0])
    if not pid_alive(pid):
        return None, None
    return pid, max(0, int(time.time()) - int(fields[1]))


#: A verdict's exit code, as `hive ci premerge` exits on it.
_EXIT = {"green": 0, "red": 1, "error": 2}


def _act(n, kind, verdict=None, detail="", exit_code=None, **extra) -> dict:
    """One action with the five keys every action has. *exit_code* defaults to
    the verdict's; a skipped PR has neither."""
    if exit_code is None and verdict is not None:
        exit_code = _EXIT.get(str(verdict), 2)
    return {"pr": n, "action": kind, "verdict": verdict, "exit_code": exit_code,
            "detail": detail, **extra}


def describe(action) -> str:
    """One line for one action, the way the watch prints it."""
    n = action.get("pr")
    subject = f"PR #{n}" if n else "watch"
    kind, detail = action.get("action"), action.get("detail") or ""
    verdict = str(action.get("verdict") or "")
    if kind == "tested":
        refused = len(action.get("status_errors") or ())
        return (f"{subject}: tested -- {verdict.upper()}: {detail}"
                + (f"; {refused} status(es) could not be posted" if refused else ""))
    if kind == "republished":
        return f"{subject}: its {verdict} verdict is posted again -- {detail}"
    if kind == "conflict":
        return f"{subject}: RED -- {detail}"
    if kind == "base-moved":
        return (f"{subject}: the base moved and it still merges cleanly; "
                f"the {verdict} verdict stands")
    if kind == "unchanged":
        return f"{subject}: unchanged since its last test ({verdict})"
    if kind == "skipped":
        return f"{subject}: skipped ({detail})"
    return f"{subject}: could not check -- {detail}"


def _held(path) -> dict:
    """The one action of a pass that found the watch lock held: skipped,
    naming the lock, its holder's pid and how long it has held it -- or, past
    `STUCK_S`, an error with the same keys."""
    pid, held_s = holder(path)
    detail = (f"another watch pass holds {path}" + (f", pid {pid}" if pid else "")
              + (f", for {held_s}s" if held_s is not None else ""))
    if held_s is not None and held_s > STUCK_S:
        return _act(0, "error", "error", f"{detail}: past {STUCK_S}s, it looks stuck",
                    lock=str(path), holder_pid=pid, held_s=held_s)
    return _act(0, "skipped", detail=detail, lock=str(path), holder_pid=pid, held_s=held_s)


def _untaken(path, exc) -> dict:
    """The one action of a pass that could not take the watch lock at all."""
    return _act(0, "error", "error", f"the watch lock {path or LOCK} could not be taken: {exc}",
                lock=None if path is None else str(path))


class Watcher:
    """Passes over the open PRs through one `premerge.Premerge`."""

    def __init__(self, premerge, *, github=None, project=None, legs_spec=None,
                 skip_drafts=False, interval_s=DEFAULT_INTERVAL_S, sleep=time.sleep,
                 say=None, merge_tree=None):
        self.premerge = premerge
        self.github = github if github is not None else premerge.github
        self.project = premerge.project if project is None else project
        self.legs_spec = legs_spec
        self._wanted = {leg.name for leg in legs_mod.select(legs_spec)}
        self.skip_drafts = bool(skip_drafts)
        self.interval_s = float(interval_s)
        self._sleep = sleep
        self._say = say or (lambda text: None)
        self._merge_tree = merge_tree or (
            lambda a, b: preview_mod.merge_tree(premerge.repo_root, a, b,
                                                run=premerge.spawn))

    def once(self) -> list:
        """One pass. Returns what it did, one action per open PR -- or, when
        the watch lock is held or cannot be taken, the one action that says
        so, and nothing else is done."""
        path = None
        try:
            path = lock_path(self.project)
            lock = PassLock(path)
            held = lock.take()
        except OSError as exc:
            held, action = False, _untaken(path, exc)
        else:
            action = None if held else _held(path)
        if not held:
            self._say(describe(action))
            return [action]
        try:
            return self._pass()
        finally:
            lock.release()

    def _pass(self) -> list:
        # Asked again each pass: a WSL too cold to answer the last pass's
        # probe inside its cap may answer this one.
        forget = getattr(getattr(self.premerge, "probe", None), "forget", None)
        if callable(forget):
            forget()
        try:
            prs = self.github.open_prs()
        except gh_mod.GitHubError as exc:
            action = _act(0, "error", "error", str(exc))
            self._say(describe(action))
            return [action]
        known = premerge_mod.load_sidecar(self.project)["prs"]
        actions = []
        for pr in prs:
            n = int(pr.get("number") or 0)
            if self.skip_drafts and pr.get("isDraft"):
                action = _act(n, "skipped", detail="a draft")
            elif gh_mod.from_fork(pr):
                action = _act(n, "skipped",
                              detail=f"from a fork; run it by hand with `hive ci premerge "
                                     f"{n} --trust` once its diff is read")
            else:
                last = known.get(str(n)) or {}
                same = (str(pr.get("headRefOid") or "") == last.get("head_sha")
                        and str(pr.get("baseRefName") or "") == last.get("base_ref")
                        and self._covers(last))
                action = self._recheck(n, pr, last) if same else self._test(n)
            self._say(describe(action))
            actions.append(action)
        return actions

    def loop(self, passes=None, report=None) -> int:
        """Pass after pass, *passes* times or for ever. *report*, when given,
        is handed each pass's actions; a pass that failed is one error action.
        Returns the passes made."""
        done = 0
        while passes is None or done < passes:
            try:
                actions = self.once()
            except Exception as exc:                    # noqa: BLE001
                detail = f"a pass failed: {type(exc).__name__}: {exc}"
                self._say(f"watch: {detail}")
                actions = [_act(0, "error", "error", detail)]
            if report is not None:
                report(actions)
            done += 1
            if passes is None or done < passes:
                self._sleep(self.interval_s)
        return done

    def _covers(self, last) -> bool:
        """Whether the run *last* remembers tested every leg this watch asks
        for. An entry kept before runs named their legs tested them all."""
        selected = last.get("selected")
        return selected is None or self._wanted <= set(selected)

    @staticmethod
    def _retryable(last) -> bool:
        """A red that errors alone made -- no conflict, no failing test -- and
        not tried again yet for this head."""
        if last.get("verdict") != "red" or last.get("conflicts") or last.get("retried"):
            return False
        states = set((last.get("legs") or {}).values())
        return "error" in states and "failure" not in states

    def _test(self, n, why="") -> dict:
        res = self.premerge.run_pr(n, legs_spec=self.legs_spec)
        action = _act(n, "tested", res.verdict, why + res.reason,
                      exit_code=int(res.exit_code))
        if res.status_errors:
            action["status_errors"] = list(res.status_errors)
        return action

    def _republish(self, n, last) -> dict:
        verdict = str(last.get("verdict") or "error")
        reason = str(last.get("reason") or "")
        why = self.github.post_status(str(last.get("head_sha") or ""),
                                      premerge_mod.OVERALL_STATE.get(verdict, "error"),
                                      gh_mod.CONTEXT, reason)
        if why:
            return _act(n, "error", "error",
                        f"the {verdict} verdict still could not be posted: {why}")
        premerge_mod.remember(self.project, n, {**last, "published": True})
        return _act(n, "republished", verdict, reason)

    def _recheck(self, n, pr, last) -> dict:
        if last.get("published") is False and self.premerge.post_statuses:
            return self._republish(n, last)
        # The mark goes down before the run: a run that cannot record it
        # must not be tried again on every pass.
        if self._retryable(last) and premerge_mod.remember(
                self.project, n, {**last, "retried": True}):
            return self._test(n, why="tried again once, its last red was errors alone: ")
        base_ref = str(pr.get("baseRefName") or "")
        base_now, error = self.premerge.fetch_base(base_ref)
        if error:
            return _act(n, "error", "error", error)
        if base_now == (last.get("checked_base_sha") or last.get("base_sha")):
            return _act(n, "unchanged", last.get("verdict"))
        if last.get("verdict") == "error":
            return self._test(n)
        head = str(last.get("head_sha") or "")
        tree, conflicts, error = self._merge_tree(base_now, head)
        if error:
            return self._test(n)
        if conflicts:
            return self._conflict(n, pr, base_ref, base_now, head, tree, conflicts)
        if last.get("verdict") == "red" and last.get("conflicts"):
            return self._test(n)
        premerge_mod.remember(self.project, n, {**last, "checked_base_sha": base_now,
                                                "checked_tree": tree})
        return _act(n, "base-moved", last.get("verdict"), f"{base_ref} at {base_now[:12]}")

    def _conflict(self, n, pr, base_ref, base_now, head, tree, conflicts) -> dict:
        res = self.premerge.record_conflict(
            n, branch=str(pr.get("headRefName") or ""), base_ref=base_ref,
            base_sha=base_now, head_sha=head, conflicts=conflicts, tree=tree)
        detail = res.reason
        if res.status_errors:
            detail += f"; the status could not be posted: {res.status_errors[0]}"
        return _act(n, "conflict", res.verdict, detail)
