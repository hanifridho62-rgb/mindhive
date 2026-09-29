# SPDX-License-Identifier: Apache-2.0
# src/mindhive/orchestrator/driver.py
"""Process-group spawn and whole-tree kill (blueprint 10.14.4, Bug 16; MEB ORCH-04).

`Popen.kill` reaches ONE process. A capped child that had spawned its own -- a
pytest run with a subprocess under test, an agent CLI with a tool running --
left those grandchildren alive past the cap, holding the pipes and whatever
files they had open. Two pieces close that:

* `isolation_kwargs` starts a child at the head of its own process group
  (``CREATE_NEW_PROCESS_GROUP`` on Windows, ``start_new_session`` on POSIX --
  `setsid` without `preexec_fn`, which is not safe beside threads), so the tree
  under it can be addressed as one thing;
* `hold_tree` keeps that tree addressable after its LEADER has died, which a pid
  cannot: `taskkill /F /T` on an exited pid answers rc 128, "process not found",
  and `os.getpgid(pid)` raises ESRCH the moment `poll()` reaps it. On Windows the
  tree goes into a Job Object at spawn; on POSIX the hold records that the child
  heads its own group, whose id is the child's pid and which outlives it;
* `terminate_process_tree` takes that tree and SAYS WHAT IT REACHED -- the job,
  ``taskkill /F /T``, ``SIGKILL`` to the group, the direct child alone, or
  nothing at all.

The orphan case is why the hold exists rather than the kill simply trying
harder: a parent that exits the moment it has spawned leaves a grandchild on the
inherited pipe, and the kill by pid then had nothing to walk from. Measured
before the fix: a 2.0 s cap returned after 8.69 s, the grandchild's own lifetime,
while the run recorded "killed at the 2.0s cap" -- a kill that never happened.

`run_agent_process_isolated` is the two together with `subprocess.run`'s
contract: a real `CompletedProcess`, or the same `TimeoutExpired` a caller
already handles -- raised only after the tree is gone and the pipes drained.
`verify._stream` uses the pieces directly, because it streams, and
`resilience._run_capped` calls the kill for a child no Job Object is holding.

Not a second fence. `resilience.run_sandboxed` holds a child in a Job Object
with memory and CPU ceilings and stays the way an untrusted agent is run. This
module is for "when the cap fires, nothing is left running": no ceilings, no
verdict.

A child that heads its own group no longer receives the console's Ctrl+C with
its parent. Whoever spawns through `isolation_kwargs` therefore owns ending the
tree on ANY exit from its wait, not only the timeout -- `run_agent_process_isolated`
and `verify._stream` both do, and both `release_tree` on every path out.

`release_tree` is itself a kill on Windows, because the job carries
`KILL_ON_JOB_CLOSE`: the hold lasts exactly as long as the call that took it, so
"when the cap fires, nothing is left running" holds for the ordinary exit too.
A caller that wants a child to outlive its run must not hold it.

Every environment built here carries ``PYTHONDONTWRITEBYTECODE=1`` unless the
caller already chose a value (`paths.no_bytecode_env`, MEB operational risk 7),
and never the SOP process token (`advice.scrub_agent_env`, blueprint P1d): what
`run_agent_process_isolated` runs is an agent by its own name.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import weakref
from dataclasses import dataclass

from .. import paths
from ..paths import NO_BYTECODE_ENV, no_bytecode_env
from .jobs import _assign, _close_job, _containment_job, _terminate_job

DEFAULT_TIMEOUT_S = 300.0
# After the tree kill the pipes close as the last holder dies; this bounds the
# wait for a holder the kill could not reach (a process that left the group).
DRAIN_GRACE_S = 5.0
TASKKILL_TIMEOUT_S = 15.0
# `reap`'s own bound, on top of its wait: long enough for a close nothing is
# contending for, short enough that a close behind a live reader thread costs a
# leaked handle instead of a pinned caller.
CLOSE_GRACE_S = 1.0
# The ways of reaching a tree, as opposed to reaching the child in front of it.
WHOLE_TREE = ("job", "taskkill", "group")


@dataclass(frozen=True)
class TreeKill:
    """What `terminate_process_tree` ACTUALLY did.

    `verify._stream` writes its cap's answer into a `verification` event stamped
    ``verified_by="python"``, and `gate.py` records it. A kill that reached
    nothing must not come back wearing the same word as one that took the whole
    tree: an assertion nothing performed is exactly what that stamp exists to
    rule out."""

    how: str
    reason: str

    @property
    def reached(self) -> bool:
        """Whether anything at all was signalled."""
        return self.how != "nothing"

    @property
    def took_the_tree(self) -> bool:
        """Whether the DESCENDANTS were reached, not only the direct child."""
        return self.how in WHOLE_TREE


@dataclass(frozen=True)
class _Held:
    """How one held child's tree can be reached: a Windows job handle, and
    whether the child heads a process group of its own."""

    job: object
    group: bool


# Keyed weakly so a `Popen` the caller drops is not kept alive by its own hold;
# `release_tree` is still the documented way out, and this only covers the caller
# that forgot.
_HELD: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def isolation_kwargs(creationflags: int | None = None) -> dict:
    """The `Popen` keywords that start a child at the head of its own process
    group. *creationflags* are the caller's other Windows flags (default
    `paths.NO_WINDOW`); they are ignored on POSIX, where they must be 0."""
    if sys.platform == "win32":
        base = paths.NO_WINDOW if creationflags is None else int(creationflags)
        return {"creationflags": base | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _held_for(proc):
    """The hold taken for *proc*, or ``None``. Never raises -- a stand-in that
    cannot be weakly referenced is simply not held."""
    try:
        return _HELD.get(proc)
    except TypeError:
        return None


def hold_tree(proc, *, group: bool = True) -> str:
    """Make *proc*'s whole tree reachable for as long as this call owns it, and
    answer the reason it could NOT be held (``""`` when it was, and ``""`` on a
    platform that needs no handle). Never raises.

    On Windows the tree goes into a Job Object: a job reaches every descendant
    whether or not the leader is still alive, which is the one thing `taskkill
    /F /T` cannot do. *group* records what `isolation_kwargs` did on POSIX --
    the child was started with `start_new_session`, so its process group id is
    its own pid and the group outlives it. Pass ``group=False`` for a child that
    shares this process's group; signalling that group would take the caller.

    The assignment window is the same one `jobs._assign` documents: the child is
    assigned after `Popen` returns, because `subprocess` closes the primary
    thread handle before handing the object back.

    `release_tree` ends the hold and must be called on every path out."""
    job, why = None, ""
    if sys.platform == "win32":
        job, why = _containment_job()
        if job is not None:
            assigned, refused = _assign(job, proc)
            if not assigned:
                _close_job(job)
                job, why = None, refused
    try:
        _HELD[proc] = _Held(job, bool(group))
    except TypeError as exc:                            # not weakly referenceable
        if job is not None:
            _close_job(job)
        return f"{type(exc).__name__}: {exc}"
    return why


def release_tree(proc) -> None:
    """End the hold taken by `hold_tree`. Never raises.

    On Windows this closes the job handle, which is itself a kill: the job
    carries `KILL_ON_JOB_CLOSE`, so anything the run left behind goes with it.
    That is this module's contract ("when the cap fires, nothing is left
    running") applied to the ordinary exit as well, and it is the backstop for
    a holder the kill could not reach."""
    try:
        held = _HELD.pop(proc, None)
    except TypeError:
        return
    if held is not None and held.job is not None:
        _close_job(held.job)


def _taskkill(proc) -> bool:
    """``taskkill /F /T`` on *proc*, windowless and bounded. Answers whether it
    reported taking the tree: on a pid that has already gone it answers rc 128,
    "process not found", and that is the case this must not launder."""
    try:
        done = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                              capture_output=True, creationflags=paths.NO_WINDOW,
                              timeout=TASKKILL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return int(getattr(done, "returncode", 0) or 0) == 0


def _kill_child(proc) -> None:
    """The direct child, whatever the tree kill managed, so the caller's wait on
    it always ends."""
    try:
        proc.kill()
    except OSError:
        pass


def _group_kill(proc, held) -> bool:
    """``SIGKILL`` to the child's OWN process group, or ``False`` when it has
    none of its own.

    Held, the group id is the child's pid: the child IS the group leader, and
    the group outlives it -- so this still works once `poll()` has reaped the
    child, where `os.getpgid(proc.pid)` raises ESRCH and the lookup form could
    never answer the case it was most needed for. Unheld, the group is looked up
    and compared, because a child not started through `isolation_kwargs` shares
    OUR group and signalling it would kill the caller."""
    if held is not None:
        if not held.group:
            return False
        pgid = proc.pid
    else:
        try:
            pgid = os.getpgid(proc.pid)
            if pgid == os.getpgid(0):
                return False
        except OSError:
            return False
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return False
    return True


def terminate_process_tree(proc) -> TreeKill:
    """Kill *proc* and every descendant, and answer what was actually reached.
    Total and idempotent: no failure of the kill itself is raised.

    The order is the fix. A hold is tried FIRST and without regard to whether
    the child is still alive, because that is the only mechanism that survives
    the leader's death; the pid-based kills follow for a child nothing is
    holding, and an exited child nothing held is reported as the dead end it is
    rather than as a kill."""
    held = _held_for(proc)
    exited = proc.poll() is not None
    if held is not None and held.job is not None and _terminate_job(held.job):
        _kill_child(proc)
        return TreeKill("job", "the job object took the whole tree")
    if sys.platform == "win32":
        if not exited and _taskkill(proc):
            _kill_child(proc)
            return TreeKill("taskkill", "taskkill /F /T walked the tree from the child")
    elif _group_kill(proc, held):
        _kill_child(proc)
        return TreeKill("group", "SIGKILL went to the child's own process group")
    if exited:
        return TreeKill("nothing",
                        "the direct child had already exited and nothing was holding "
                        "its tree, so any descendant it left is out of reach")
    _kill_child(proc)
    return TreeKill("child",
                    "only the direct child could be reached, so a descendant that "
                    "outlived it may still be running")


def reap(proc, grace_s: float = DRAIN_GRACE_S) -> None:
    """After a kill, on the way to raising: a bounded wait for *proc* and its
    pipes closed, so the exception does not leave a handle for the collector to
    complain about. Never raises, and is bounded in full.

    `pipe.close()` takes the buffered-reader lock that `communicate`'s daemon
    reader thread holds for as long as an unkilled holder keeps the write end
    open -- which is precisely the case `reap` is called for. Only `proc.wait`
    used to be bounded, so the "bounded wait" blocked for 5.13 s at a grace of
    0.0, and for 40.92 s against a 2.0 s cap. The body therefore runs on a daemon
    thread joined for ``grace_s + CLOSE_GRACE_S``.

    A pipe still held is then left to its reader: CPython's
    `Popen._readerthread` closes it itself when its read finally returns. The
    cost of a holder the kill could not reach is one leaked handle and one
    daemon thread; the cost of waiting for it is a pinned lane thread, which is
    the orchestrator's only liveness bound."""
    def _body():
        try:
            proc.wait(timeout=grace_s)
        except (subprocess.TimeoutExpired, OSError):
            pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except (OSError, ValueError):
                pass

    worker = threading.Thread(target=_body, name="hive-reap", daemon=True)
    worker.start()
    worker.join(max(float(grace_s), 0.0) + CLOSE_GRACE_S)


def run_agent_process_isolated(cmd_args: list[str], cwd: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                               *, env=None, input_text: str | None = None
                               ) -> subprocess.CompletedProcess:
    """Run *cmd_args* in *cwd* at the head of its own process group and answer a
    `CompletedProcess` (text mode). Past *timeout_s* the whole tree is killed,
    what it had written is drained, and `subprocess.TimeoutExpired` is raised
    carrying that output. *env* is the child's environment (``None``: inherit),
    always passed through `advice.scrub_agent_env` and then `no_bytecode_env`.
    argv list only, never a shell."""
    from .. import advice as authorship     # lazy: `advice` reaches into `mindnet`
    argv = [str(a) for a in cmd_args]
    proc = subprocess.Popen(
        argv, cwd=str(cwd) if cwd else None, shell=False,
        env=no_bytecode_env(authorship.scrub_agent_env(env)),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **isolation_kwargs())
    hold_tree(proc)
    try:
        try:
            stdout, stderr = proc.communicate(input=input_text, timeout=timeout_s)
        except subprocess.TimeoutExpired as expired:
            terminate_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=DRAIN_GRACE_S)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                stdout, stderr = expired.output, expired.stderr
                reap(proc, 0.0)
            raise subprocess.TimeoutExpired(argv, timeout_s,
                                            output=stdout, stderr=stderr) from None
        except BaseException:
            terminate_process_tree(proc)
            reap(proc)
            raise
    finally:
        release_tree(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


__all__ = ["CLOSE_GRACE_S", "DEFAULT_TIMEOUT_S", "DRAIN_GRACE_S", "NO_BYTECODE_ENV",
           "TASKKILL_TIMEOUT_S", "TreeKill", "WHOLE_TREE", "hold_tree", "isolation_kwargs",
           "no_bytecode_env", "reap", "release_tree", "run_agent_process_isolated",
           "terminate_process_tree"]
