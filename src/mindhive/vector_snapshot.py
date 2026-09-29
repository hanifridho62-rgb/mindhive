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
cannot take one is "space". Each file is copied with `shutil.copyfile`, never
`copytree`'s default `copy2`: on Windows, Python 3.14's `copy2` copies through
CopyFile2, which keeps any other process from opening the file for writing
while it copies -- measured, a writer opening a 32 MB segment file during
three copies was refused 225 times -- and a writer refused is `hive embed` or
the watcher's batch failing, when chromadb persists a segment. `copyfile`
shares the file for writing; its handle does not share deletion, which
SQLite, deleting its journal, retries.

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
`check_segments` refuses that damage before chromadb reads the collection,
which contains every one of them -- in a segment the copy's catalogue still
names, since chromadb leaves a dropped collection's segment directory behind.
It holds the segment's other files to what its header says they must hold
(`_segment_damage`): measured on four scratch indexes before that, a
`link_lists.bin` cut in half crashed the reading process with a segfault in
19 of 23 reads, hung it past 150 s in 2 and failed with InternalError in 2 --
and under `python -X faulthandler`, which neither the CLI nor the dashboard
sets, 4 reads of 4 failed with InternalError instead: the outcome follows the
memory layout -- and a `data_level0.bin` cut in half answered every read but
one count, which segfaulted, without an error; the one answer compared with
the whole index's had silently lost its two best hits.
It is the reader's *check*, asked for the collection read and once per copy:
chromadb loads a collection's HNSW segment only once that collection is
counted or queried, so with `events` cut, `ledger` is read from the same copy
-- whose client, never having loaded the cut segment, closes as any other. A
read the check refuses read nothing: it is not the client's failure, is not
remembered, and a read of the same copy is refused the same way until the
source changes. In `ledger` or `events` that damage is repaired by `hive
embed --rebuild`, which drops the collection without reading it. Damage the
check cannot see is met after the fact only
where chromadb raises on it -- a header of whole length but garbage bytes
failed the count with InternalError: when the caller's *judge* claims a failure
as the client's own, the client is never closed and never freed
(`_immortalise`), and its copy is left for the dead-pid sweep of a later
process. That is the fate of a client in this process; the default
(`opener`, `ISOLATE_READS`) is a client in a process of its own
(`vector_reader`), which keeps nothing: nothing here read the damage, so the
client is closed -- its process killed if it will not go -- and its copy
deleted after its last reader, and no client counts toward `POISON_LIMIT`.
Damage of the right length -- garbage bytes where a segment's data or links
were -- is what the check cannot see; read in a process of its own, a crash
or a hang on it is a failed read here, and only a wrong answer given without
an error is not contained.

The failure is remembered against the bytes it happened on, each fingerprint
of a collection in a memo of its own: the same collection of an unchanged
index is refused with `IndexUnreadable` rather than copied and opened again,
for `FAILED_RETRY_S` -- a hiccup on an index nobody writes to must not end
semantic search for the life of a dashboard. That memo is asked on the way
in, again once the copy is taken and again at install, so a reader that
overlapped a failure does not open, read and fail on the same bytes too. A
client such a refusal leaves unread is closed like any other, and a copy
never replaces a newer one: taken first and installed last, it serves the
reader that took it and is closed after it. Each client a read failed on, and
its copy, is kept until the process ends, so once `POISON_LIMIT` of them are
held, a source is not read again until the process restarts while it has a
failure no read of the same collection has got past since -- a source
repaired and read since is still read; one that first fails after that still
keeps one more client.
"""
import atexit
import logging
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

__all__ = ["SnapshotError", "IndexUnreadable", "DamagedSegment", "reading",
           "opener", "open_chroma", "check_segments", "fingerprint", "held",
           "dispose_all", "SNAPSHOT_PREFIX", "COPY_ATTEMPTS",
           "COPY_RETRY_PAUSE_S", "REMOVE_ATTEMPTS", "REMOVE_RETRY_PAUSE_S",
           "POISON_LIMIT", "FAILED_RETRY_S", "HNSW_HEADER",
           "HNSW_HEADER_BYTES", "ISOLATE_READS"]

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
#: collection of a source with a failure no read has got past since is read
#: again until the process restarts. Not a hard bound: a source first failing
#: past it keeps one more.
POISON_LIMIT = 16
#: How long a failed read is remembered against unchanged bytes.
FAILED_RETRY_S = 600.0
#: Whether a read's chromadb client runs in a process of its own
#: (`vector_reader`), so that a crash or a hang in chromadb is a failed read
#: here rather than the end of this process. Off, or in a frozen build, the
#: copy is opened in this process (`open_chroma`).
ISOLATE_READS = True
#: chroma-hnswlib's persisted `header.bin`, little-endian, as its
#: `HEADER_FIELDS`: PERSISTENCE_VERSION (int); offsetLevel0, max_elements,
#: cur_element_count, size_data_per_element, label_offset, offsetData
#: (size_t); maxlevel (int); enterpoint_node (unsigned); maxM, maxM0, M
#: (size_t); mult (double); ef_construction (size_t). 100 bytes: every
#: segment measured under chromadb 1.5.9 has exactly this many.
HNSW_HEADER = struct.Struct("<i6QiI3QdQ")
HNSW_HEADER_BYTES = HNSW_HEADER.size

_SQLITE = "chroma.sqlite3"
_GUARD = threading.Lock()
_CURRENT: dict = {}
_ORPHANS: set = set()      # closed copies of this process that would not delete
_SWEPT = False             # copies of processes that are gone: swept once
_POISONED: list = []       # snapshots a read failed on: never closed or deleted
_FAILED: dict = {}         # (source, collection) -> {fingerprint: (error, when)}
_PASSED: dict = {}         # (source, collection) -> the latest way in past its
                           # memos: failures before it count toward no cap
_UNCHECKED = object()      # a copy's collection the segment check has not seen


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
    a source with a failure no read has got past. `original` is the failure that was
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
    """A persisted HNSW segment whose files hold less than its `header.bin`
    says they must -- or a header cut short, or one at odds with itself.
    `collection` is the collection it belongs to -- the one read, when the
    check was asked for one; None when the copy's catalogue could not say."""

    def __init__(self, message, collection=None):
        super().__init__(message)
        self.collection = collection


class _Snapshot:
    """One copy and its client. `taken` is when the copy's fingerprint was
    taken, which orders two copies of one source; `checked` holds, per
    collection, what the segment check said of this copy -- None for a pass."""
    __slots__ = ("key", "fingerprint", "root", "client", "users", "retired",
                 "poisoned", "taken", "checked", "copy_ms")

    def __init__(self, key, fp, root, client, taken=0.0, copy_ms=None):
        self.key = key
        self.fingerprint = fp
        self.root = root
        self.client = client
        self.users = 0
        self.retired = False
        self.poisoned = False
        self.taken = taken
        self.checked = {}
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


def check_segments(copy, collection=None) -> None:
    """Refuse a read of *collection* whose persisted HNSW segment cannot hold
    what its `header.bin` says (`_segment_damage`), before chromadb reads it.
    Only a segment chromadb persisted (it has `index_metadata.pickle`) is
    judged: an unpersisted one is rebuilt from the log, and a short header
    there was measured harmless. Only one the copy's catalogue still names:
    chromadb 1.5.9 leaves a dropped collection's segment directory on disk
    and never opens it again, and judged, the cut segment `hive embed
    --rebuild` had just dropped refused every read after the rebuild. And
    only one of *collection*: chromadb loads a collection's segment once
    that collection is counted or queried, so a cut `events` segment is no
    reason to refuse `ledger`. A catalogue that cannot be read names no
    owner, and every segment is judged -- as every one is when *collection*
    is None."""
    live = _live_segments(copy)
    for header in Path(copy).glob("*/header.bin"):
        seg = header.parent
        if not (seg / "index_metadata.pickle").exists():
            continue
        owner = None
        if live is not None:
            sid = _canonical(seg.name)
            if sid not in live:
                continue
            owner = live[sid]
            if collection is not None and owner != collection:
                continue
        why = _segment_damage(seg)
        if why is not None:
            of = f" of {owner!r}" if owner else ""
            raise DamagedSegment(f"HNSW segment {seg.name}{of}: {why}",
                                 collection=owner)


def _segment_damage(seg):
    """Why the persisted segment at *seg* cannot hold what its `header.bin`
    says, or None. The header's `cur_element_count` elements -- deleted
    ones stay counted -- of `size_data_per_element` bytes must fit in
    `data_level0.bin` (sized for `max_elements` when never grown), one
    float each in `length.bin`, and `link_lists.bin` must walk: per element
    a u32 size, a multiple of `maxM * 4 + 4` and at most `maxlevel` of them,
    then that many bytes. Measured on six scratch indexes (1300, 130, 35 and
    2500 rows at sync thresholds 1000, 100 and 10, 200 rows deleted, widths
    8 and 384): every whole segment passes, and each of its four files cut
    in half, or by one byte, is refused -- but for a cut inside the room
    `data_level0.bin` keeps past the last element, which chromadb never
    reads. The walk costs 14 ms at 40k elements, once per copy."""
    raw = (seg / "header.bin").read_bytes()
    if len(raw) < HNSW_HEADER_BYTES:
        return (f"header.bin is {len(raw)} bytes, a whole one is "
                f"{HNSW_HEADER_BYTES}")
    (_version, _offset0, room, cur, size, label_at, data_at, maxlevel,
     _enter, maxm, _maxm0, _m, _mult, _ef) = HNSW_HEADER.unpack_from(raw)
    if cur > room or size == 0 or label_at >= size or data_at >= size:
        return (f"header.bin says {cur} of {room} elements of {size} bytes, "
                f"data at {data_at} and label at {label_at}")
    for name, need in (("data_level0.bin", cur * size),
                       ("length.bin", cur * 4)):
        try:
            have = (seg / name).stat().st_size
        except OSError:
            return f"{name} is missing"
        if have < need:
            return f"{name} is {have} bytes, {cur} elements need {need}"
    try:
        links = (seg / "link_lists.bin").read_bytes()
    except OSError:
        return "link_lists.bin is missing"
    per_level, levels = maxm * 4 + 4, max(maxlevel, 0)
    at = 0
    for i in range(cur):
        if at + 4 > len(links):
            return (f"link_lists.bin is {len(links)} bytes, cut inside "
                    f"element {i} of {cur}")
        n = struct.unpack_from("<I", links, at)[0]
        at += 4
        if n % per_level or n > per_level * levels:
            return (f"link_lists.bin: element {i} claims {n} bytes of links, "
                    f"not up to {levels} levels of {per_level}")
        if at + n > len(links):
            return (f"link_lists.bin is {len(links)} bytes, cut inside "
                    f"element {i} of {cur}")
        at += n
    return None


def _canonical(seg_id) -> str:
    """*seg_id* in one form. chromadb finds a segment's directory by parsing
    its id as a UUID, so a catalogue naming it in capitals or without its
    hyphens still names the lowercase directory."""
    import uuid
    try:
        return str(uuid.UUID(str(seg_id)))
    except ValueError:
        return str(seg_id).lower()


def _live_segments(copy):
    """`{segment id: collection name}` for every segment the copy's
    `chroma.sqlite3` names, ids in `_canonical` form, or None when it cannot
    be read. Opened read-only, and only ever on a copy."""
    import sqlite3
    try:
        con = sqlite3.connect(
            f"{(Path(copy).absolute() / _SQLITE).as_uri()}?mode=ro", uri=True)
    except Exception:                              # noqa: BLE001 -- judge them all
        return None
    try:
        return {_canonical(sid): name for sid, name in con.execute(
            "SELECT s.id, c.name FROM segments s "
            "LEFT JOIN collections c ON s.collection = c.id")}
    except Exception:                              # noqa: BLE001 -- judge them all
        return None
    finally:
        con.close()


def opener():
    """The opener a read hands `reading`: `vector_reader.open_isolated`, a
    client in a process of its own, unless `ISOLATE_READS` is off or there
    is no interpreter to start (`sys.frozen`), when it is `open_chroma`."""
    if ISOLATE_READS and not getattr(sys, "frozen", False):
        from .vector_reader import open_isolated
        return open_isolated
    return open_chroma


def open_chroma(copy):
    """A chromadb client on the copy at *copy*, in this process. chromadb
    registers a client's system BEFORE starting it, so an open that fails
    leaves the half-started system in its registry for the life of the
    process -- a leak per failed read; it is taken out here. The segment
    check is the reader's, per collection (`reading`'s *check*)."""
    import chromadb
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
    """`(fingerprint, root, taken)` of a copy of *src* at `root/chroma` that
    held still while it was taken, *taken* being when its fingerprint was;
    `(None, None, None)` when *src* is gone. Whatever stops a copy half-way
    takes its directory with it."""
    failure = None
    for attempt in range(COPY_ATTEMPTS):
        if attempt:
            time.sleep(COPY_RETRY_PAUSE_S)
        taken = time.monotonic()
        before = fingerprint(src)
        if before is None:
            return None, None, None
        root = _new_root()
        try:
            shutil.copytree(src, root / "chroma",
                            copy_function=shutil.copyfile)
        except (OSError, shutil.Error) as e:
            failure = e
            _remove(root)
            continue
        except BaseException:
            _remove(root)
            raise
        if fingerprint(src) == before:
            return before, root, taken
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
    bytes it was read from -- beside, never in place of, a failure of the
    same collection on other bytes. A client in a process of its own
    (`isolated`) is retired instead of kept: nothing in this process read
    the damage, so it is closed, its process killed if it will not go, and
    its copy deleted once its last reader is done."""
    isolated = getattr(snap.client, "isolated", False)
    with _GUARD:
        _FAILED.setdefault((snap.key, name), {})[snap.fingerprint] = (
            error, time.monotonic())
        if _CURRENT.get(snap.key) is snap:
            del _CURRENT[snap.key]
        if isolated:
            first, snap.retired = not snap.retired, True
        else:
            first, snap.poisoned = not snap.poisoned, True
            if first:
                _POISONED.append(snap)
    if not first:
        return
    if isolated:
        _log.warning("vector_snapshot: a read of the %r index at %s failed "
                     "(%s); its reading process is closed and its copy %s "
                     "deleted", name, snap.key, error, snap.root)
        return
    _immortalise(snap.client)
    _log.warning("vector_snapshot: a read of the %r index at %s failed "
                 "(%s); its client stays open until the process ends and "
                 "its copy %s is left for a later sweep",
                 name, snap.key, error, snap.root)


def _refused(key, name, fp, since=None) -> None:
    """Raise `IndexUnreadable` when a remembered failure answers this read.

    Collection *name* of these very bytes, *fp*, failed within
    `FAILED_RETRY_S`: each fingerprint has its own memo. One memo per
    collection held only the last failure: a failure on older bytes replaced
    the one on newer bytes, which a reader of those then copied, opened and
    failed on again; and a way in on newer bytes dropped the memo of older
    ones that a copy still in flight asked at install, which installed a copy
    of bytes that had failed over a newer one.

    Or `POISON_LIMIT` clients are held, and this source has a failure, of
    any of its collections, that no read of that collection has got past
    since: capped per collection, a
    read of `ledger` in an index whose `events` had failed was copied,
    opened and failed again, and each such sibling kept one more client past
    the limit. *since*, given on the way in, is when *fp* was taken: a way
    in no memo refuses marks the collection passed at *since*, and failures
    recorded before that count toward no cap -- they still refuse their own
    bytes until they expire. Kept for good, a failure refused a source that
    was repaired and read fine since, until the process restarted. One
    recorded after *since* may be of bytes *fp* never saw -- a writer came
    by, and a reader failed on what it left -- so it still counts. Only the
    way in expires memos and marks: the checks once the copy is taken never
    do, since a copy can be OLDER than a failure. Called under `_GUARD`."""
    now = time.monotonic()
    memo = _FAILED.get((key, name), {})
    hit = memo.get(fp)
    if hit is not None and now - hit[1] < FAILED_RETRY_S:
        raise IndexUnreadable(key, name, hit[0])
    if len(_POISONED) >= POISON_LIMIT:
        standing = [(n == name, when, n, error)
                    for (k, n), fails in _FAILED.items() if k == key
                    for error, when in fails.values()
                    if when >= _PASSED.get((k, n), float("-inf"))]
        if standing:
            _own, _when, of, error = max(standing, key=lambda s: s[:2])
            raise IndexUnreadable(key, name, error, capped=True, of=of)
    if since is None:
        return
    live = {f: v for f, v in memo.items() if now - v[1] < FAILED_RETRY_S}
    if live:
        _FAILED[(key, name)] = live
        _PASSED[(key, name)] = max(_PASSED.get((key, name), since), since)
    else:
        _FAILED.pop((key, name), None)
        _PASSED.pop((key, name), None)


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
    since = time.monotonic()          # before the fingerprint: see `_refused`
    fp = fingerprint(src)
    if fp is None:
        return None
    with _GUARD:
        _refused(key, name, fp, since)
        snap = _CURRENT.get(key)
        if snap is not None and snap.fingerprint == fp:
            snap.users += 1
            return snap
    # Copy and open OUTSIDE the guard: a second reader of a different index,
    # or of this one, must not wait behind a copytree it does not need.
    _sweep()
    start = time.perf_counter()
    built_fp, root, taken = _copy_stable(src)
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
            _refused(key, name, built_fp)
        client = opener(root / "chroma")
    except BaseException:
        _remove(root)
        raise
    fresh = _Snapshot(key, built_fp, root, client, taken, copy_ms)
    doomed, refusal = [], None
    with _GUARD:
        try:
            _refused(key, name, built_fp)
        except IndexUnreadable as e:
            refusal = e
            doomed.append(fresh)           # it never read: closed as usual
        else:
            snap = _CURRENT.get(key)
            if snap is not None and snap.fingerprint == built_fp:
                chosen = snap              # a racing reader copied it first
                doomed.append(fresh)
            elif snap is not None and snap.taken > fresh.taken:
                # A newer copy was installed while this one was opened. This
                # one serves its reader and goes after it; installed, it took
                # the newer one's place and closed it, and the next read of
                # the newer bytes copied the index again.
                fresh.retired = True
                chosen = fresh
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


def _checked(snap, name, check) -> None:
    """Raise what *check* says of collection *name* in *snap*'s copy, asked
    once per copy and collection: the copy's bytes do not change, so neither
    does the answer. Only an Exception is kept; anything else propagates and
    is asked again."""
    with _GUARD:
        known = snap.checked.get(name, _UNCHECKED)
    if known is _UNCHECKED:
        try:
            check(snap.root / "chroma", name)
            known = None
        except Exception as e:                     # noqa: BLE001 -- kept, raised
            known = e
        with _GUARD:
            snap.checked[name] = known
    if known is not None:
        raise known.with_traceback(None)


@contextmanager
def reading(src, opener, *, judge=None, name=None, check=None):
    """Yield `opener(copy)` for a consistent copy of the index at *src*, or
    None when there is no index there -- in which case nothing is opened and
    nothing is created. Raises `SnapshotError` when no consistent copy could
    be taken; whatever *opener* raises propagates, with the copy removed.

    *check(copy, name)*, when given, is asked before collection *name* is
    read -- once per copy -- and what it raises refuses the read. The client
    read nothing, so that is never its failure: it is not judged, and the
    copy stays in service for the collections the check passes.

    *judge(exc)* is asked about every exception that leaves the block: true
    means the client itself failed, so it is never closed and collection
    *name* of these bytes is refused (`IndexUnreadable`) until they change."""
    snap = _acquire(src, opener, name)
    if snap is None:
        yield None
        return
    if check is not None:
        try:
            _checked(snap, name, check)
        except BaseException:
            _release(snap)
            raise
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
