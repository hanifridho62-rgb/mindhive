# src/mindhive/orchestrator/jobs.py
"""Windows Job Objects: the kernel fence `setrlimit` is on POSIX.

Split out of `resilience.py` when that module crossed the 1000-line cap, along
the seam that was already there: everything here is `ctypes` and `kernel32` and
nothing here knows what a subprocess is. `resilience.run_sandboxed` is the only
caller.

## What a job object buys, and what it costs

`JOB_OBJECT_LIMIT_PROCESS_MEMORY` is `RLIMIT_AS` and
`JOB_OBJECT_LIMIT_PROCESS_TIME` is `RLIMIT_CPU`, both set through
`SetInformationJobObject` on a `JOBOBJECT_EXTENDED_LIMIT_INFORMATION`. Reached
through `ctypes` because this project's `dependencies = []` and pywin32 is not
going to be added for one struct.

`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` rides with them, and buys something POSIX
rlimits do not: closing the handle takes the child's whole process TREE, so a
runner that spawns its own children cannot outlive the fence.

The cost is one gap, stated rather than hidden: the child is assigned AFTER
`CreateProcess` returns, because `subprocess` closes the primary thread handle
before handing the object back and there is nothing left to resume. `setrlimit`
is applied by the kernel before `exec` and has no such window.

## Everything here is total, and a failure DOWNGRADES

`resilience`'s whole contract is that it never reports a stronger fence than it
applied. So every function here answers a `(value, why)` pair instead of raising,
and a non-empty *why* is the caller's cue to report `timeout-only` and say so.
A sandbox that answered `kernel` on the strength of having TRIED would launder
an unfenced run as a fenced one, which is the failure `resilience`'s module
docstring names first.
"""
from __future__ import annotations

# `JOBOBJECTINFOCLASS.JobObjectExtendedLimitInformation`, and the three limit
# flags this module sets. Literals rather than an enum because there is exactly
# one consumer and the names below are the ones MSDN uses.
_JOB_EXTENDED_LIMIT_INFORMATION = 9
_JOB_LIMIT_PROCESS_TIME = 0x00000002        # RLIMIT_CPU
_JOB_LIMIT_PROCESS_MEMORY = 0x00000100      # RLIMIT_AS
_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
# `PerProcessUserTimeLimit` is in 100-nanosecond units, which is the units every
# Windows time field uses and the one every wrapper of this API gets wrong once.
_HUNDRED_NS_PER_S = 10_000_000


def _kernel32():
    """`kernel32` with the entry points this module uses, or ``None`` off Windows.

    Signatures are declared rather than left to ctypes' defaults: a HANDLE is
    pointer-sized and the implicit `c_int` return truncates it on 64-bit, which
    is the failure that makes job objects look like they silently do nothing."""
    import ctypes
    if not hasattr(ctypes, "WinDLL"):
        return None
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    lib.CreateJobObjectW.restype = ctypes.c_void_p
    lib.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_void_p, ctypes.c_uint32]
    lib.SetInformationJobObject.restype = ctypes.c_int
    lib.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.AssignProcessToJobObject.restype = ctypes.c_int
    lib.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.TerminateJobObject.restype = ctypes.c_int
    lib.CloseHandle.argtypes = [ctypes.c_void_p]
    lib.CloseHandle.restype = ctypes.c_int
    return lib


def _limit_structs():
    """The two structures `SetInformationJobObject` wants, built on demand.

    Defined inside a function so importing this module on POSIX costs nothing
    and so a `ctypes` that cannot describe them fails HERE, where the caller
    turns it into a `timeout-only` reason, rather than at import."""
    import ctypes

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64)]

    class _Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _Basic),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    return _Extended


def _jobs_available() -> tuple:
    """``(available, why)`` for Windows job objects. Never raises."""
    try:
        lib = _kernel32()
    except (OSError, AttributeError, ImportError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if lib is None:
        return False, "this is not Windows"
    return True, ""


def _job_for(limits) -> tuple:
    """``(handle, why)`` — a job object carrying *limits*, or the reason there
    is none.

    ``(None, "")`` when nothing was asked for or this is not Windows: that is
    not a degradation and `fencing` already has a word for it. A non-empty *why*
    with a ``None`` handle is a real downgrade and the caller must report it.

    Never raises. Every failure path closes the handle it opened — leaking a job
    handle with `KILL_ON_JOB_CLOSE` set would keep an empty job alive for the
    life of the process."""
    if limits is None or limits.empty:
        return None, ""
    available, why = _jobs_available()
    if not available:
        return None, ""                     # POSIX: `_preexec_for` has this run
    import ctypes

    lib = _kernel32()
    job = None
    try:
        job = lib.CreateJobObjectW(None, None)
        if not job:
            return None, (f"CreateJobObject failed (WinError "
                          f"{ctypes.get_last_error()})")
        info = _limit_structs()()
        flags = _JOB_LIMIT_KILL_ON_JOB_CLOSE
        if int(limits.memory_bytes or 0) > 0:
            flags |= _JOB_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = int(limits.memory_bytes)
        if int(limits.cpu_seconds or 0) > 0:
            flags |= _JOB_LIMIT_PROCESS_TIME
            info.BasicLimitInformation.PerProcessUserTimeLimit = (
                int(limits.cpu_seconds) * _HUNDRED_NS_PER_S)
        info.BasicLimitInformation.LimitFlags = flags
        ok = lib.SetInformationJobObject(
            job, _JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
            ctypes.sizeof(info))
        if not ok:
            err = ctypes.get_last_error()
            lib.CloseHandle(job)
            return None, f"SetInformationJobObject failed (WinError {err})"
        return job, ""
    except Exception as exc:                            # noqa: BLE001
        # TOTAL, like the rest of this module: a sandbox that raises while
        # setting up its own fence has cost the caller a run it could have had
        # unfenced-but-reported.
        if job:
            try:
                lib.CloseHandle(job)
            except Exception:                           # noqa: BLE001
                pass
        return None, f"{type(exc).__name__}: {exc}"


def _assign(job, proc) -> tuple:
    """``(assigned, why)`` for one child. Never raises.

    The child is assigned immediately after `Popen` returns rather than being
    created suspended, because `subprocess` closes the primary thread handle
    before it hands the object back and there is nothing left to resume. The
    window is the few microseconds between `CreateProcess` and this call, during
    which the child has not been scheduled long enough to allocate past a
    ceiling — stated rather than hidden, since it is the one gap between this
    fence and `setrlimit`, which the kernel applies before `exec`."""
    import ctypes

    try:
        lib = _kernel32()
        ok = lib.AssignProcessToJobObject(job, int(proc._handle))
        if not ok:
            return False, (f"AssignProcessToJobObject failed (WinError "
                           f"{ctypes.get_last_error()})")
        return True, ""
    except Exception as exc:                            # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _terminate_job(job, exit_code=1) -> bool:
    """Kill everything in *job* NOW, leaving the handle open. Never raises.

    KILL_ON_JOB_CLOSE takes the tree too, but only when the handle closes, and
    the close is the LAST thing `run_sandboxed` does. On the timeout path that
    is far too late: `proc.kill()` is TerminateProcess on the child alone, a
    grandchild holding the inherited pipe write-ends keeps them open, and the
    post-kill drain then waits on that grandchild -- so the mechanism could not
    run until the thing it exists to interrupt had already finished by itself.
    Measured at 20.1s against a 4s cap, identical to an unfenced control.

    Separate from `_close_job` rather than an early call to it, because *job* is
    a raw HANDLE: closing it twice is worse than the bug it would fix, since the
    value can be reused by another open in between and the second close would
    take an unrelated handle. This kills; the single existing close still closes.

    Returns whether the kill was actually made, so a caller can report a tree it
    could NOT take rather than assume it did.
    """
    try:
        lib = _kernel32()
        if lib is None or job is None:
            return False
        return bool(lib.TerminateJobObject(job, exit_code))
    except Exception:                                   # noqa: BLE001
        return False


def _close_job(job) -> None:
    """Close a job handle. Never raises — a failed cleanup must not turn a
    finished run into an exception.

    Called exactly once per handle, from `run_sandboxed`'s `finally`. Anything
    that needs the tree dead EARLIER wants `_terminate_job`, which leaves this
    close the only one."""
    try:
        _kernel32().CloseHandle(job)
    except Exception:                                   # noqa: BLE001
        pass

