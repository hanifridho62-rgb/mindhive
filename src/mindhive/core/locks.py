# SPDX-License-Identifier: Apache-2.0
# src/mindhive/core/locks.py
"""The cross-platform atomic lock primitive (blueprint P1j, MEB CONC-01):
one `O_CREAT|O_EXCL` sidecar file, taken by a spin-wait whose delay is a
randomised, jittered exponential backoff inside [10, 50] ms, a stale holder
(older than `LockPolicy.stale_s`, 30 s by default, AND its process gone --
`pid_alive` on the pid the file carries) reclaimed, and the
Windows DELETE-PENDING answer -- ACCESS_DENIED for a name whose last holder
has unlinked it but whose directory entry has not gone yet -- counted as
contention for `denied_spins` attempts rather than as an unwritable
directory, which raises the same errno and never clears.

WHAT THIS IS NOT. The ledger's append lock is `events._append_lock`, an OS
byte-range lock (`msvcrt.locking` / `fcntl.flock`) over one file per
namespace, and it stays that way: measured under twelve-way contention it
was three times faster than this pattern (0.89 s against 2.68 s), and the
kernel drops it when the holder dies, so no stale file can wedge a later
append. This module is for the sidecars that need an `O_EXCL` FILE -- the
Chroma directory (P1h), the storage driver's state file (P1i), the
integration breaker's record -- where the resource has no single file to
lock a byte of. NOT `tasktool`'s board: decision K4 gave its public
`task_lock` the MEB's own loop verbatim (P3), with a 60 s stale age against
this module's 30 s, so a `file_lock` pointed at `.tasktool.lock` would
reclaim a live holder -- one file, one policy. A caller always gives the
path under `paths.project_dir(...)`, never under `store_home()`
(`tests/test_store_root_files.py`).

Every OS call goes through the `os` module by name (`os.open`, `os.write`,
`os.read`, `os.close`, `os.unlink`, `os.stat`, `os.kill`), so a test can
monkeypatch one to simulate the fault it is about -- the pattern
`tests/test_tasktool.py` already uses on `os.open`. The clocks, the sleeper,
the random source and the liveness probe are parameters for the same reason.

STALE MEANS DEAD, NOT OLD. The age of the file was once the whole test, and
it reclaimed a lock from under a live holder that had simply held it for
longer than `stale_s` -- `hive embed` holds the chroma lock for the whole
encode. `acquire` writes `<pid> <monotonic>` into the file, and `is_stale`
reads the pid back and asks the OS whether that process still exists; a
holder that does keeps its lock at any age. The limit, stated: a pid the OS
has reused for an unrelated live process reads as alive, and that lock waits
until the process exits or an operator removes the file (RUNBOOK_RECOVERY).
"""
import contextlib
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["LockTimeoutError", "LockPolicy", "DEFAULT_POLICY", "LockHandle",
           "backoff_delay", "pid_alive", "holder_pid", "is_stale",
           "try_acquire", "acquire", "file_lock"]


class LockTimeoutError(TimeoutError):
    """The lock could not be had inside `LockPolicy.timeout_s`, or the
    ACCESS_DENIED budget ran out, and the policy is fail-closed."""


@dataclass(frozen=True)
class LockPolicy:
    """How patient one `acquire` is. Defaults: two seconds, 10-50 ms jittered
    backoff (CONC-01), a holder older than 30 s whose process is gone is
    dead, five ACCESS_DENIED answers before giving up, and fail-CLOSED -- a caller that would rather
    run unlocked than raise says `fail_open=True`, as a hook path must."""
    timeout_s: float = 2.0
    base_s: float = 0.010
    max_s: float = 0.050
    stale_s: float = 30.0
    denied_spins: int = 5
    fail_open: bool = False

    def __post_init__(self):
        if not (self.timeout_s >= 0):                        # also refuses NaN
            raise ValueError(f"timeout_s harus >= 0: {self.timeout_s!r}")
        if not (0 < self.base_s <= self.max_s):
            raise ValueError(f"butuh 0 < base_s <= max_s: "
                             f"base_s={self.base_s!r}, max_s={self.max_s!r}")
        if not (self.stale_s > 0):
            raise ValueError(f"stale_s harus > 0: {self.stale_s!r}")
        if (isinstance(self.denied_spins, bool) or not isinstance(self.denied_spins, int)
                or self.denied_spins < 0):
            raise ValueError(f"denied_spins harus bilangan bulat >= 0: {self.denied_spins!r}")


DEFAULT_POLICY = LockPolicy()


def backoff_delay(attempt: int, base_s: float = DEFAULT_POLICY.base_s,
                  max_s: float = DEFAULT_POLICY.max_s, rand=random.random) -> float:
    """The delay before retry number *attempt* (0-based): uniform over
    `[base_s, min(max_s, base_s * 2**attempt)]`, so the ceiling doubles each
    time and the floor never drops -- with the defaults every value lies in
    [10, 50] ms and the ceilings run 10, 20, 40, 50, 50, ... ms."""
    if attempt < 0:
        raise ValueError(f"attempt harus >= 0: {attempt!r}")
    hi = min(max_s, base_s * (2.0 ** attempt))
    return base_s + (hi - base_s) * rand()


def pid_alive(pid: int) -> bool:
    """Whether process *pid* may still be running. Errs towards yes: a lock
    is reclaimed (and, in `vector_snapshot`, a copy swept) only when its
    owner is certainly gone. POSIX asks with signal 0; Windows opens the
    process for query and reads its exit code, because `os.kill(pid, 0)`
    there TERMINATES the process."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (OSError, OverflowError):           # EPERM: someone else's
            return True
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() != 87       # INVALID_PARAMETER: no such pid
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259                    # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


# What `acquire` writes: the pid, a space, the clock reading. Anchored at the
# start and bounded to a pid's width, so a JSON marker (`watcher` runs its
# claim markers through `is_stale` too), a torn write or an empty file --
# created but not yet written -- reads as no holder rather than as pid 0.
_HOLDER_LINE = re.compile(rb"([1-9][0-9]{0,9}) ")


def holder_pid(path) -> int | None:
    """The pid `acquire` wrote into the lock file, or None when the file is
    missing or unreadable, carries no such line, or carries a number that is
    not a pid. None means the age rule alone decides, as it always did."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        head = os.read(fd, 32)
    except OSError:
        return None
    finally:
        os.close(fd)
    hit = _HOLDER_LINE.match(head)
    if not hit:
        return None
    pid = int(hit.group(1))
    return pid if 0 < pid <= 0xFFFFFFFF else None


def is_stale(path, stale_s: float, now=time.time, alive=None) -> bool:
    """True when the lock file is more than *stale_s* seconds old AND the
    process that wrote it is gone: the holder died mid-section, not merely
    slow, and not merely long. A live holder keeps its lock at any age (see
    the module docstring); a file with no readable holder (`holder_pid`)
    falls back to the age rule; a missing or unreadable file is not stale --
    there is nothing to break. *alive* is the liveness seam, `pid_alive`
    by default."""
    try:
        age = now() - os.stat(str(path)).st_mtime
    except OSError:
        return False
    if not age > stale_s:
        return False
    pid = holder_pid(path)
    if pid is None:
        return True
    return not (pid_alive if alive is None else alive)(pid)


def try_acquire(path) -> int | None:
    """One `O_CREAT|O_EXCL` attempt through `os.open`: the descriptor on
    success, `None` when the name exists (`FileExistsError`). Any other
    `OSError`, `PermissionError` included, propagates to `acquire`, which
    knows which of them is contention."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None


@dataclass(frozen=True)
class LockHandle:
    """A held lock. `release()` closes the descriptor and unlinks the file,
    swallowing `OSError`: a lock that cannot be unlinked is stale for the
    next taker after `stale_s`, which is the recovery this module has."""
    path: Path
    fd: int

    def release(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.unlink(str(self.path))
        except OSError:
            pass


def acquire(path, policy: LockPolicy = DEFAULT_POLICY, *, name: str | None = None,
            sleep=time.sleep, monotonic=time.monotonic, rand=random.random,
            now=time.time, alive=None) -> LockHandle | None:
    """Take the lock at *path* under *policy*: a `LockHandle`, or `None`
    when the policy is fail-open and the lock could not be had; a
    `LockTimeoutError` when it is fail-closed. Contention is
    `FileExistsError` (wait, reclaim first if stale -- old AND its holder
    gone, `is_stale`; *alive* is that probe's seam) and `PermissionError`
    (Windows DELETE-PENDING: wait, but only `denied_spins` times); any other
    `OSError` is a real fault -- `None` if fail-open, else re-raised as is.
    The message names *name* (or the file) and counts the attempts made:
    `Gagal memperoleh <name> setelah <timeout_s>s (spins=<n>).`"""
    path = Path(path)
    label = name or path.name
    deadline = monotonic() + policy.timeout_s
    attempts = 0
    denied = 0
    while True:
        try:
            fd = try_acquire(path)
        except PermissionError:
            fd = None
            denied += 1
            if denied > policy.denied_spins:
                return _give_up(policy, label, attempts + 1)
        except OSError:
            if policy.fail_open:
                return None
            raise
        else:
            if fd is not None:
                os.write(fd, f"{os.getpid()} {monotonic()}".encode("ascii"))
                return LockHandle(path, fd)
            if is_stale(path, policy.stale_s, now, alive):
                try:
                    os.unlink(str(path))
                except OSError:
                    pass
                else:
                    attempts += 1
                    if monotonic() >= deadline:
                        return _give_up(policy, label, attempts)
                    continue                    # reclaimed: retry at once
        attempts += 1
        if monotonic() >= deadline:
            return _give_up(policy, label, attempts)
        sleep(backoff_delay(attempts - 1, policy.base_s, policy.max_s, rand))


def _give_up(policy: LockPolicy, label: str, spins: int):
    if policy.fail_open:
        return None
    raise LockTimeoutError(
        f"Gagal memperoleh {label} setelah {policy.timeout_s}s (spins={spins}).")


@contextlib.contextmanager
def file_lock(path, policy: LockPolicy = DEFAULT_POLICY, *, name: str | None = None,
              **seams):
    """`with file_lock(p, policy) as held:` -- *held* is True when the lock
    is held for the block, False only under a fail-open policy that could
    not get it (the block still runs, unlocked: the caller's stated choice).
    *seams* are `acquire`'s injectable clocks, sleeper and liveness probe."""
    handle = acquire(path, policy, name=name, **seams)
    try:
        yield handle is not None
    finally:
        if handle is not None:
            handle.release()
