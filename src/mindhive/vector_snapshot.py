# SPDX-License-Identifier: Apache-2.0
"""A private copy of a chroma index, so that a READ never writes the index.

chromadb 1.5.9 has no read-only client, and a read through an ordinary one is a
write. Measured on a scratch store: one `hive recall decision --semantic`
changed the sha256 of `chroma.sqlite3` and of the queried collection's
`data_level0.bin` and `length.bin`. Two mechanisms, both inside chromadb:

* every client open commits an `acquire_write` row to `chroma.sqlite3`, reader
  or not;
* an HNSW segment is persisted only once `hnsw:sync_threshold` (1000) rows have
  arrived, so below that every reader re-initialises the segment files and
  replays the queue into them.

DATA_MODEL.md section 4.19 records the decision this module implements: read
paths never mutate the derived index. A read opens a byte copy of the index
instead, and the copy is what chromadb scribbles on.

**Checked, not locked.** A reader takes no `chroma_lock`: a thirty-second
writer's lock on every recall would put the watcher's embed batches in front
of an interactive search. Instead the source is fingerprinted before and after
the copy -- every file's size and mtime, plus SQLite's own file change counter
(header bytes 24-27), which moves even when two commits land inside one mtime
tick with the size unchanged -- and a copy whose fingerprints disagree is
thrown away and taken again. A writer that never pauses across `COPY_ATTEMPTS`
tries is reported as `SnapshotError` (`reason` "busy"), never answered from a
torn copy; a copy that cannot be written is "copy", a temp directory that
cannot take one is "space".

**Cached per process.** An unchanged index is copied once: the next read whose
fingerprint matches reuses the open client, so a dashboard asking ten questions
pays for one copy. A changed index gets a fresh copy, and the old one is closed
and deleted as soon as its last reader is done -- never under a reader still
using it. `dispose_all` (registered with `atexit`) closes whatever is left;
`held` names the current copy, its size and what it cost, for a caller that
reports on it.

**Nothing left behind.** The copies live in the temp directory as
`SNAPSHOT_PREFIX<pid>-*`. On Windows a closed copy can refuse deletion for a
moment (a file the closed client has not quite let go of): measured under a
busy writer, 8 closed copies of 9.4 MB each stayed on disk, reported to nobody.
So a delete is retried; one that still fails is logged and kept in `_ORPHANS`,
retried on every fresh copy and at exit. A process killed before `atexit`, or
one that exits holding an orphan, leaves its copy behind: the first fresh copy
a process takes sweeps every `SNAPSHOT_PREFIX<pid>-*` whose pid is certainly
gone. A copy of a running process, or a pid that cannot be read, is left alone.

**A read that failed.** Measured on a scratch index whose persisted HNSW
`header.bin` was cut to 7 bytes: the count fails, and then ANY teardown of that
client -- `close()`, dropping its last reference, interpreter exit -- corrupts
the heap (exit 0xC0000374); with fewer rows waiting in the log -- 130 at
sync_threshold 100, 35 at 10 -- runs of the count itself crashed the process,
hung past two minutes, or failed, the outcome varying between runs.
`check_segments` refuses that damage before chromadb opens the copy, which
contains every one of them. What it cannot see -- a header of whole length but wrong
bytes, a cut `data_level0.bin` or `link_lists.bin` -- is met after the fact:
when the caller's *judge* claims a failure as the client's own, the client is
never closed and never freed (`_immortalise`), and its copy is left for the
dead-pid sweep of a later process. Were chromadb to crash or hang on such
damage, nothing in this process would contain it; a read in a child process
would. The failure is remembered against the bytes it happened on: the same
collection of an unchanged index is refused with `IndexUnreadable` rather than
copied and opened again, for `FAILED_RETRY_S` -- a hiccup on an index nobody
writes to must not end semantic search for the life of a dashboard. That memo
is asked on the way in, again once the copy is taken and again at install, so
a reader that overlapped a failure does not open, read and fail on the same
bytes too; a client such a refusal leaves unread is closed like any other.
Each client a read failed on, and its copy, is kept until the process ends, so
once `POISON_LIMIT` of them
are held no collection of a source that has failed is read again until the
process restarts; a source that first fails after that still keeps one more.
"""
import atexit
import logging
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

__all__ = ["SnapshotError", "IndexUnreadable", "DamagedSegment", "reading",
           "open_chroma", "check_segments", "fingerprint", "held", "dispose_all",
           "SNAPSHOT_PREFIX", "COPY_ATTEMPTS", "COPY_RETRY_PAUSE_S",
           "REMOVE_ATTEMPTS", "REMOVE_RETRY_PAUSE_S", "POISON_LIMIT",
           "FAILED_RETRY_S", "HNSW_HEADER_BYTES"]

_log = logging.getLogger("mindhive.vector_snapshot")

SNAPSHOT_PREFIX = "mindhive-chroma-read-"
#: Copies taken before a busy writer is reported. Five, 50 ms apart: an embed
#: batch commits in well under that, so a source still moving after a quarter
#: of a second is a writer mid-run rather than a writer between commits.
COPY_ATTEMPTS = 5
COPY_RETRY_PAUSE_S = 0.05
#: Deletes of a closed copy before it is left to a later sweep.
REMOVE_ATTEMPTS = 3
REMOVE_RETRY_PAUSE_S = 0.1
#: Where copies go. None is the platform temp directory; tests point it into
#: their own tmp dir.
SNAPSHOT_DIR = None
#: Clients a failed read left open for good, per process, past which no
#: collection of a source that has failed is read again until the process
#: restarts. Not a hard bound: a source first failing past it keeps one more.
POISON_LIMIT = 16
#: How long a failed read is remembered against unchanged bytes.
FAILED_RETRY_S = 600.0
#: Bytes in a whole HNSW `header.bin`: every segment measured under chromadb
#: 1.5.9 has exactly this many.
HNSW_HEADER_BYTES = 100

_SQLITE = "chroma.sqlite3"
_GUARD = threading.Lock()
_CURRENT: dict = {}
_ORPHANS: set = set()      # closed copies of this process that would not delete
_SWEPT = False             # copies of processes that are gone: swept once
_POISONED: list = []       # snapshots a read failed on: never closed or deleted
_FAILED: dict = {}         # (source, collection) -> (fingerprint, error, when)


class SnapshotError(RuntimeError):
    """The index could not be copied into a consistent snapshot. `reason` is
    why: "busy" (a writer changed it during every copy), "copy" (the copy
    itself failed) or "space" (the temp directory could not take one)."""

    def __init__(self, message, reason="busy"):
        super().__init__(message)
        self.reason = reason


class IndexUnreadable(RuntimeError):
    """A read refused before anything was copied: collection *name* of the
    index at *src* failed to read and has not changed since -- or, `capped`,
    too many clients are already held open by failed reads to risk another on
    a source that has failed at all. `original` is the failure that was
    remembered, on collection `of` (*name* unless a sibling failed); `why`
    says which refusal this is, without it."""

    def __init__(self, src, name, original, *, capped=False, of=None):
        self.src, self.name = src, name
        self.original, self.capped = original, capped
        self.of = name if of is None else of
        which = (f"the {name!r} index at {src}" if self.of == name else
                 f"the {name!r} index at {src}, whose {self.of!r} index failed "
                 f"a read,")
        self.why = (
            f"{len(_POISONED)} failed reads hold clients open in this process "
            f"(limit {POISON_LIMIT}); {which} is not read again until the "
            f"process restarts" if capped else
            f"the {name!r} index at {src} failed to read and has not changed "
            f"since; it is not read again until it changes or "
            f"{FAILED_RETRY_S:g}s pass")
        super().__init__(f"{self.why}: {type(original).__name__}: {original}")


class DamagedSegment(RuntimeError):
    """A persisted HNSW segment whose `header.bin` is shorter than a whole one."""


class _Snapshot:
    __slots__ = ("key", "fingerprint", "root", "client", "users", "retired",
                 "poisoned", "copy_ms")

    def __init__(self, key, fp, root, client, copy_ms=None):
        self.key = key
        self.fingerprint = fp
        self.root = root
        self.client = client
        self.users = 0
        self.retired = False
        self.poisoned = False
        self.copy_ms = copy_ms


def fingerprint(src):
    """What a copy of *src* has to agree with. None when *src* is not a
    directory: a namespace that was never embedded has no index to copy."""
    src = Path(src)
    if not src.is_dir():
        return None
    rows = []
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src).as_posix()
        try:
            if p.is_file():
                st = p.stat()
                rows.append((rel, st.st_size, st.st_mtime_ns))
        except OSError:
            rows.append((rel, -1, -1))  # vanished mid-walk: a writer is busy
    try:
        with open(src / _SQLITE, "rb") as fh:
            fh.seek(24)
            counter = fh.read(4)
    except OSError:
        counter = b""
    return tuple(rows), counter


def check_segments(copy) -> None:
    """Refuse a copy holding a persisted HNSW segment whose `header.bin` is
    cut short, before chromadb reads it. Only a segment chromadb persisted
    (it has `index_metadata.pickle`) is judged: an unpersisted one is rebuilt
    from the log, and a short header there was measured harmless."""
    for header in Path(copy).glob("*/header.bin"):
        if not (header.parent / "index_metadata.pickle").exists():
            continue
        size = header.stat().st_size
        if size < HNSW_HEADER_BYTES:
            raise DamagedSegment(
                f"HNSW segment {header.parent.name}: header.bin is {size} "
                f"bytes, a whole one is {HNSW_HEADER_BYTES}")


def open_chroma(copy):
    """A chromadb client on the copy at *copy*, once `check_segments` passes.
    chromadb registers a client's system BEFORE starting it, so an open that
    fails leaves the half-started system in its registry for the life of the
    process -- a leak per failed read; it is taken out here."""
    import chromadb
    check_segments(copy)
    try:
        return chromadb.PersistentClient(path=str(copy))
    except BaseException:
        from chromadb.api.shared_system_client import SharedSystemClient
        SharedSystemClient._identifier_to_system.pop(str(copy), None)
        getattr(SharedSystemClient, "_identifier_to_refcount", {}).pop(
            str(copy), None)
        raise


def _base() -> Path:
    base = Path(SNAPSHOT_DIR) if SNAPSHOT_DIR is not None else Path(
        tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    return base


def _new_root() -> Path:
    """A fresh directory for one copy; a temp directory that cannot hold one
    is a `SnapshotError` naming it, never an OSError that reads as a broken
    index."""
    try:
        return Path(tempfile.mkdtemp(
            prefix=f"{SNAPSHOT_PREFIX}{os.getpid()}-", dir=_base()))
    except OSError as e:
        where = SNAPSHOT_DIR if SNAPSHOT_DIR is not None else tempfile.gettempdir()
        raise SnapshotError(f"no copy of the index can be made under {where}: "
                            f"{e}", reason="space") from e


def _remove(root, attempts=None) -> bool:
    """Delete a copy. True when it is gone; otherwise it joins `_ORPHANS`,
    with a warning the first time, to be retried by `_sweep`."""
    root = Path(root)
    error = None
    for attempt in range(attempts or REMOVE_ATTEMPTS):
        if attempt:
            time.sleep(REMOVE_RETRY_PAUSE_S)
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        except OSError as e:
            error = e
            continue
        with _GUARD:
            _ORPHANS.discard(root)
        return True
    with _GUARD:
        first = root not in _ORPHANS
        _ORPHANS.add(root)
    if first:
        _log.warning("vector_snapshot: could not delete the copy %s (%s); "
                     "it will be retried", root, error)
    return False


def _pid_alive(pid: int) -> bool:
    """Whether process *pid* may still be running. Errs towards yes: a copy
    is swept only when its owner is certainly gone."""
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


def _owner(name: str):
    """The pid a copy's directory name carries, or None if it carries none."""
    head = name[len(SNAPSHOT_PREFIX):].split("-", 1)[0]
    try:
        pid = int(head) if head.isascii() else 0
    except ValueError:
        return None
    return pid if 0 < pid <= 0xFFFFFFFF else None


def _retry_orphans() -> None:
    with _GUARD:
        orphans = list(_ORPHANS)
    for root in orphans:
        _remove(root, attempts=1)


def _sweep() -> None:
    """Retry this process's orphans; once per process, delete the copies of
    processes that are gone. Best effort: a sweep never fails a read."""
    global _SWEPT
    _retry_orphans()
    with _GUARD:
        foreign, _SWEPT = not _SWEPT, True
    if not foreign:
        return
    try:
        leftovers = [d for d in _base().glob(f"{SNAPSHOT_PREFIX}*") if d.is_dir()]
    except OSError:
        return
    for d in leftovers:
        pid = _owner(d.name)
        if pid is not None and not _pid_alive(pid):
            shutil.rmtree(d, ignore_errors=True)


def _copy_stable(src):
    """`(fingerprint, root)` of a copy of *src* at `root/chroma` that held
    still while it was taken, or `(None, None)` when *src* is gone. Whatever
    stops a copy half-way takes its directory with it."""
    failure = None
    for attempt in range(COPY_ATTEMPTS):
        if attempt:
            time.sleep(COPY_RETRY_PAUSE_S)
        before = fingerprint(src)
        if before is None:
            return None, None
        root = _new_root()
        try:
            shutil.copytree(src, root / "chroma")
        except (OSError, shutil.Error) as e:
            failure = e
            _remove(root)
            continue
        except BaseException:
            _remove(root)
            raise
        if fingerprint(src) == before:
            return before, root
        failure = None
        _remove(root)
    if failure is not None:
        raise SnapshotError(f"could not copy the index at {src}: {failure}",
                            reason="copy") from failure
    raise SnapshotError(
        f"the index at {src} changed during each of {COPY_ATTEMPTS} copies; "
        f"a writer (hive embed or the watcher) is busy -- retry when it is done")


def _immortalise(client) -> None:
    """Give *client* one reference nobody owns, so its refcount never reaches
    zero and chromadb never tears it down -- not even at interpreter exit."""
    try:
        import ctypes
        ctypes.pythonapi.Py_IncRef(ctypes.py_object(client))
    except (ImportError, AttributeError, OSError):  # no CPython API: `_POISONED`
        pass                                         # holds it while we run


def _poison(snap, name, error) -> None:
    """Take *snap* out of service for good, and remember *error* against the
    bytes it was read from."""
    with _GUARD:
        first = not snap.poisoned
        snap.poisoned = True
        if _CURRENT.get(snap.key) is snap:
            del _CURRENT[snap.key]
        if first:
            _POISONED.append(snap)
        _FAILED[(snap.key, name)] = (snap.fingerprint, error, time.monotonic())
    if first:
        _immortalise(snap.client)
        _log.warning("vector_snapshot: a read of the %r index at %s failed "
                     "(%s); its client stays open until the process ends and "
                     "its copy %s is left for a later sweep",
                     name, snap.key, error, snap.root)


def _refused(key, name, fp, *, forget=True) -> None:
    """Raise `IndexUnreadable` when a remembered failure answers this read:
    this collection of these very bytes failed within `FAILED_RETRY_S`, or
    `POISON_LIMIT` clients are held and ANY collection of this source has
    failed. Per source, not per collection: capped per collection, a read of
    `ledger` in an index whose `events` had failed was copied, opened and
    failed again, and each such sibling kept one more client past the limit.
    *forget* drops a memo *fp* no longer matches -- only on the way in, where
    *fp* is the source as it is now: a copy taken earlier can be OLDER than
    the failure, and the bytes that failed may still be the source's.
    Called under `_GUARD`."""
    failed = _FAILED.get((key, name))
    if failed is not None:
        at, error, when = failed
        if at == fp and time.monotonic() - when < FAILED_RETRY_S:
            raise IndexUnreadable(key, name, error)
    if len(_POISONED) >= POISON_LIMIT:
        mine = [(n, f) for (k, n), f in _FAILED.items() if k == key]
        if mine:
            of, (_at, error, _when) = next(
                ((n, f) for n, f in mine if n == name), mine[0])
            raise IndexUnreadable(key, name, error, capped=True, of=of)
    if failed is not None and forget:
        del _FAILED[(key, name)]


def _dispose(snap) -> None:
    if snap.poisoned:              # closing it is what corrupts the heap
        return
    close = getattr(snap.client, "close", None)
    if close is not None:
        try:
            close()
        except Exception as e:                     # noqa: BLE001 -- cleanup
            _log.warning("vector_snapshot: closing %s failed: %s", snap.root, e)
    _remove(snap.root)


def _acquire(src, opener, name=None):
    key = str(Path(src))
    fp = fingerprint(src)
    if fp is None:
        return None
    with _GUARD:
        _refused(key, name, fp)
        snap = _CURRENT.get(key)
        if snap is not None and snap.fingerprint == fp:
            snap.users += 1
            return snap
    # Copy and open OUTSIDE the guard: a second reader of a different index,
    # or of this one, must not wait behind a copytree it does not need.
    _sweep()
    start = time.perf_counter()
    built_fp, root = _copy_stable(src)
    copy_ms = (time.perf_counter() - start) * 1000.0
    if root is None:
        return None
    # The memo is asked again once the copy is taken, and again at install: a
    # reader of these bytes may have failed meanwhile. Asked only on the way
    # in, every reader that overlapped one failure went on to open, read and
    # fail on the same bytes, and kept one more client for good. A client
    # refused at install has read nothing, and is closed: on copies with a cut
    # or a garbage header, a cut data or link file, an unread client closed
    # cleanly in each of 5 runs, where one whose count had failed on the cut
    # header corrupted the heap as it closed.
    try:
        with _GUARD:
            _refused(key, name, built_fp, forget=False)
        client = opener(root / "chroma")
    except BaseException:
        _remove(root)
        raise
    fresh = _Snapshot(key, built_fp, root, client, copy_ms)
    doomed, refusal = [], None
    with _GUARD:
        try:
            _refused(key, name, built_fp, forget=False)
        except IndexUnreadable as e:
            refusal = e
            doomed.append(fresh)           # it never read: closed as usual
        else:
            snap = _CURRENT.get(key)
            if snap is not None and snap.fingerprint == built_fp:
                chosen = snap              # a racing reader copied it first
                doomed.append(fresh)
            else:
                if snap is not None:
                    snap.retired = True
                    if snap.users == 0:
                        doomed.append(snap)
                _CURRENT[key] = chosen = fresh
            chosen.users += 1
    for s in doomed:
        _dispose(s)
    if refusal is not None:
        raise refusal
    return chosen


def _release(snap) -> None:
    with _GUARD:
        snap.users -= 1
        done = snap.retired and snap.users == 0
    if done:
        _dispose(snap)


@contextmanager
def reading(src, opener, *, judge=None, name=None):
    """Yield `opener(copy)` for a consistent copy of the index at *src*, or
    None when there is no index there -- in which case nothing is opened and
    nothing is created. Raises `SnapshotError` when no consistent copy could
    be taken; whatever *opener* raises propagates, with the copy removed.

    *judge(exc)* is asked about every exception that leaves the block: true
    means the client itself failed, so it is never closed and collection
    *name* of these bytes is refused (`IndexUnreadable`) until they change."""
    snap = _acquire(src, opener, name)
    if snap is None:
        yield None
        return
    try:
        yield snap.client
    except BaseException as e:
        if judge is not None and judge(e):
            _poison(snap, name, e)
        raise
    finally:
        _release(snap)


def held(src):
    """The copy reads of the index at *src* are answered from now, as
    `{"root", "bytes", "copy_ms"}` -- where it is, the bytes it holds and how
    long taking it took, retries included -- or None when there is none. For
    a caller that reports on the copy, such as `hive bench recall-semantic`,
    instead of taking one of its own."""
    with _GUARD:
        snap = _CURRENT.get(str(Path(src)))
        if snap is None:
            return None
        rows, _counter = snap.fingerprint
        return {"root": str(snap.root),
                "bytes": sum(size for _rel, size, _mtime in rows if size > 0),
                "copy_ms": snap.copy_ms}


def dispose_all() -> None:
    """Close and delete every snapshot; one still in use goes when released.
    Copies that would not delete earlier get one more try. A client a read
    failed on is not in `_CURRENT`, and is never closed."""
    with _GUARD:
        snaps = list(_CURRENT.values())
        _CURRENT.clear()
        for s in snaps:
            s.retired = True
        idle = [s for s in snaps if s.users == 0]
    for s in idle:
        _dispose(s)
    _retry_orphans()


atexit.register(dispose_all)
