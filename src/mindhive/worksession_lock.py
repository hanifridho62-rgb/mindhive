# SPDX-License-Identifier: Apache-2.0
"""The lock a work-session step writer holds across its step-id collision check
and its append (blueprint P1b; MEB CONC-01).

`worksessions.record_work_session_step` reads the session's segment for the
ids it already holds, suffixes a collision, then appends. Without a lock over
the three, two writers to one session whose §8.4 millisecond ids coincide both
see the id as free, both append it, and `worksessions.fold` keeps only the first
row -- the other step, possibly an ``[INITIATIVE: …]`` declaration, silently
vanishes while its writer printed the id as recorded. The ledger's own
`events._append_lock` covers only the file write and is not re-entrant, so it
cannot be widened to hold the read as well.

`record_lock` is two locks, taken in this order: a process-wide
`threading.Lock` (writers in one process -- parallel sub-agents, an API loop)
and a `core.locks` ``O_EXCL`` sidecar under `paths.project_dir` (writers in
separate ``hive`` processes). One per namespace rather than per session: the
critical section is one small segment read and one append, so the contention
is negligible and no session id has to become a file name. Fail-CLOSED: a
writer that cannot get the lock inside `RECORD_POLICY.timeout_s` raises
`core.locks.LockTimeoutError` (an `OSError`, which ``hive worksession`` reports
as exit 2) rather than risk the silent loss the lock exists to prevent.
"""
from __future__ import annotations

import contextlib
import threading

from .core.locks import LockPolicy, file_lock

LOCK_FILE = ".worksession.lock"
# The critical section is milliseconds, so a holder older than 10 s is dead, and
# 15 s of waiting outlasts a reclaim. DELETE-PENDING answers on Windows get a
# wider budget than the default five: separate processes release and re-take
# the sidecar in quick succession.
RECORD_POLICY = LockPolicy(timeout_s=15.0, stale_s=10.0, denied_spins=50)
_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def record_lock(project: str | None = None):
    """Hold the namespace's work-session record lock for the block."""
    from . import paths
    path = paths.project_dir(project) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, file_lock(path, RECORD_POLICY, name="worksession_lock"):
        yield


__all__ = ["LOCK_FILE", "RECORD_POLICY", "record_lock"]
