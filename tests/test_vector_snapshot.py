# tests/test_vector_snapshot.py
"""A read never writes the index it reads.

chromadb 1.5.9 has no read-only client, and a read through one writes: every
client open commits an `acquire_write` row to `chroma.sqlite3`, and a
collection whose HNSW segment was never persisted has its segment files
re-initialised by every reader. So every read path queries a private copy
(`vector_snapshot.reading`). The first half of this file pins the copy's
lifecycle with a fake client; the second half proves the property itself
against real chromadb: byte-identical index files after the read paths run.
"""
import errno
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from mindhive import events, paths, projector, recall, vector_snapshot, vectors


@pytest.fixture(autouse=True)
def _snapshots_in_tmp(tmp_path, monkeypatch):
    """Copies go under the test's own tmp dir, and none outlives the test.
    Each test starts with no copies left over, no sweep done yet, and no
    client or index remembered as failed."""
    monkeypatch.setattr(vector_snapshot, "SNAPSHOT_DIR", tmp_path / "snaps")
    monkeypatch.setattr(vector_snapshot, "_ORPHANS", set())
    monkeypatch.setattr(vector_snapshot, "_SWEPT", False)
    monkeypatch.setattr(vector_snapshot, "_POISONED", [])
    monkeypatch.setattr(vector_snapshot, "_FAILED", {})
    yield
    vector_snapshot.dispose_all()


class _FakeClient:
    """What `reading` hands back: records its path and whether it was closed."""

    def __init__(self, path):
        self.path = Path(path)
        self.closed = False

    def close(self):
        self.closed = True


def _opener(log):
    def _open(path):
        client = _FakeClient(path)
        log.append(client)
        return client
    return _open


def _source(tmp_path):
    src = tmp_path / "store" / "chroma"
    (src / "seg").mkdir(parents=True)
    (src / "chroma.sqlite3").write_bytes(b"\0" * 100)
    (src / "seg" / "data_level0.bin").write_bytes(b"abc")
    return src


def _tree(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


# --- the copy -----------------------------------------------------------------

def test_no_index_is_none_and_creates_nothing(tmp_path):
    log = []
    missing = tmp_path / "store" / "chroma"
    with vector_snapshot.reading(missing, _opener(log)) as client:
        assert client is None
    assert log == [], "a client was opened on a namespace with no index"
    assert not missing.exists(), "the read created the index directory"


def test_the_client_opens_a_copy_and_the_source_is_untouched(tmp_path):
    src = _source(tmp_path)
    before = _tree(src)
    log = []
    with vector_snapshot.reading(src, _opener(log)) as client:
        assert client.path != src
        assert _tree(client.path) == before, "the copy is not the index"
        (client.path / "chroma.sqlite3").write_bytes(b"written by the reader")
    assert _tree(src) == before, "a write through the reader reached the index"


def test_an_unchanged_index_is_copied_once(tmp_path):
    src = _source(tmp_path)
    log = []
    for _ in range(3):
        with vector_snapshot.reading(src, _opener(log)) as client:
            assert client is log[0]
    assert len(log) == 1, "every read paid for its own copy"


def test_a_changed_index_gets_a_fresh_copy_and_the_old_one_goes(tmp_path):
    src = _source(tmp_path)
    log = []
    with vector_snapshot.reading(src, _opener(log)):
        pass
    (src / "seg" / "data_level0.bin").write_bytes(b"a writer added rows")
    with vector_snapshot.reading(src, _opener(log)) as client:
        assert _tree(client.path)["seg/data_level0.bin".replace("/", _sep())] == \
            b"a writer added rows"
    assert len(log) == 2
    assert log[0].closed, "the replaced snapshot's client was never closed"
    assert not log[0].path.parent.exists(), "the replaced copy was left on disk"
    assert not log[1].closed


def _sep():
    import os
    return os.sep


def test_the_sqlite_change_counter_alone_is_a_change(tmp_path, monkeypatch):
    """Two writes inside one mtime tick that keep the size leave size+mtime
    alone; SQLite's own change counter (header bytes 24-27) still moves."""
    src = _source(tmp_path)
    first = vector_snapshot.fingerprint(src)
    db = src / "chroma.sqlite3"
    import os
    st = db.stat()
    data = bytearray(db.read_bytes())
    data[27] = 1
    db.write_bytes(bytes(data))
    os.utime(db, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert vector_snapshot.fingerprint(src) != first


def test_a_replaced_snapshot_waits_for_its_last_reader(tmp_path):
    src = _source(tmp_path)
    log = []
    with vector_snapshot.reading(src, _opener(log)) as held:
        (src / "seg" / "data_level0.bin").write_bytes(b"changed")
        with vector_snapshot.reading(src, _opener(log)):
            pass
        assert not held.closed, "a snapshot was closed under a reader using it"
        assert held.path.exists()
    assert held.closed and not held.path.parent.exists()


def test_a_source_that_never_holds_still_is_an_error_not_a_torn_copy(
        tmp_path, monkeypatch):
    """Readers take no lock, so a copy is checked instead: the fingerprint
    before and after must agree. A writer that never pauses is reported."""
    src = _source(tmp_path)
    ticks = iter(range(10_000))
    monkeypatch.setattr(vector_snapshot, "fingerprint",
                        lambda s: (next(ticks),) if Path(s).exists() else None)
    monkeypatch.setattr(vector_snapshot, "COPY_RETRY_PAUSE_S", 0.0)
    log = []
    with pytest.raises(vector_snapshot.SnapshotError, match="changed during") as err:
        with vector_snapshot.reading(src, _opener(log)):
            pass
    assert err.value.reason == "busy"
    assert log == [], "a client was opened on a copy that did not hold still"
    snaps = tmp_path / "snaps"
    assert not snaps.exists() or not any(snaps.iterdir()), \
        "a failed snapshot left its copy behind"


def test_a_failed_open_leaves_no_copy_behind(tmp_path):
    src = _source(tmp_path)

    def _broken(path):
        raise RuntimeError("file is not a database")

    with pytest.raises(RuntimeError, match="not a database"):
        with vector_snapshot.reading(src, _broken):
            pass
    snaps = tmp_path / "snaps"
    assert not snaps.exists() or not any(snaps.iterdir())


def test_dispose_all_closes_and_removes_every_current_snapshot(tmp_path):
    src = _source(tmp_path)
    log = []
    with vector_snapshot.reading(src, _opener(log)):
        pass
    vector_snapshot.dispose_all()
    assert log[0].closed and not log[0].path.parent.exists()


def test_held_describes_the_copy_reads_are_answered_from(tmp_path):
    """What a caller that reports on the copy -- `hive bench recall-semantic`
    -- reads instead of taking a copy of its own: where the current copy is,
    the bytes it holds, how long taking it took. None before there is one and
    once it is disposed; a fresh copy of a changed index is the one named."""
    src = _source(tmp_path)
    assert vector_snapshot.held(src) is None
    log = []
    with vector_snapshot.reading(src, _opener(log)):
        pass
    held = vector_snapshot.held(src)
    assert held["root"] == str(log[0].path.parent)
    assert held["bytes"] == 100 + 3          # chroma.sqlite3 + data_level0.bin
    assert isinstance(held["copy_ms"], float) and held["copy_ms"] >= 0
    (src / "seg" / "data_level0.bin").write_bytes(b"a writer added rows")
    with vector_snapshot.reading(src, _opener(log)):
        pass
    assert vector_snapshot.held(src)["root"] == str(log[1].path.parent)
    assert vector_snapshot.held(src)["bytes"] == 100 + len(b"a writer added rows")
    vector_snapshot.dispose_all()
    assert vector_snapshot.held(src) is None


def test_concurrent_readers_share_one_copy(tmp_path):
    src = _source(tmp_path)
    log = []
    lock = threading.Lock()

    def _open(path):
        with lock:
            client = _FakeClient(path)
            log.append(client)
            return client

    seen, errors = [], []

    def _read():
        try:
            with vector_snapshot.reading(src, _open) as client:
                seen.append(client)
        except Exception as e:                      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_read) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    current = [c for c in log if not c.closed]
    assert len(current) == 1, "more than one live copy of one unchanged index"
    assert all(not c.path.exists() for c in log if c.closed), \
        "a duplicate copy built by a racing reader was left on disk"


# --- cleanup: a copy that will not delete, and copies of processes gone ---------

class _Stubborn:
    """`shutil` as vector_snapshot sees it, with an rmtree that refuses the
    next `refusals` calls -- what Windows does to a copy whose file a closed
    client has not quite let go of yet."""

    def __init__(self, refusals):
        self.refusals = refusals
        self.copytree, self.Error = shutil.copytree, shutil.Error

    def rmtree(self, path, *args, **kwargs):
        if self.refusals > 0:
            self.refusals -= 1
            raise PermissionError(13, "The process cannot access the file "
                                      "because it is being used", str(path))
        return shutil.rmtree(path, *args, **kwargs)


def _stubborn(monkeypatch, refusals):
    stub = _Stubborn(refusals)
    monkeypatch.setattr(vector_snapshot, "shutil", stub)
    monkeypatch.setattr(vector_snapshot, "REMOVE_RETRY_PAUSE_S", 0.0)
    return stub


def test_a_copy_that_refuses_deletion_for_a_moment_is_retried(tmp_path,
                                                              monkeypatch):
    _stubborn(monkeypatch, vector_snapshot.REMOVE_ATTEMPTS - 1)
    log = []
    with vector_snapshot.reading(_source(tmp_path), _opener(log)):
        pass
    vector_snapshot.dispose_all()
    assert log[0].closed
    assert not log[0].path.parent.exists(), "a refused delete was not retried"


def test_a_copy_that_will_not_delete_is_reported_then_retried(tmp_path,
                                                             monkeypatch, caplog):
    """Measured under a busy writer: 8 closed copies of 9.4 MB each stayed in
    the temp dir, deleted by nobody and reported to nobody."""
    src = _source(tmp_path)
    stub = _stubborn(monkeypatch, 10**6)
    log = []
    with vector_snapshot.reading(src, _opener(log)):
        pass
    (src / "seg" / "data_level0.bin").write_bytes(b"a writer added rows")
    with caplog.at_level(logging.WARNING, logger="mindhive.vector_snapshot"):
        with vector_snapshot.reading(src, _opener(log)):
            pass
    stuck = log[0].path.parent
    assert log[0].closed and stuck.exists()
    assert str(stuck) in caplog.text, "an undeletable copy was left silently"
    stub.refusals = 0
    (src / "seg" / "data_level0.bin").write_bytes(b"and another")
    with vector_snapshot.reading(src, _opener(log)):
        pass
    assert not stuck.exists(), "the next copy did not retry the one left behind"


def test_dispose_all_retries_the_copies_left_behind(tmp_path, monkeypatch):
    stub = _stubborn(monkeypatch, 10**6)
    log = []
    with vector_snapshot.reading(_source(tmp_path), _opener(log)):
        pass
    vector_snapshot.dispose_all()
    assert log[0].path.parent.exists()
    stub.refusals = 0
    vector_snapshot.dispose_all()
    assert not log[0].path.parent.exists(), "exit left a stuck copy behind"


def _finished_process():
    """A process that has exited. The Popen is returned too: while it is
    referenced Windows keeps the process object, so the pid is not reused."""
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    return done


def _running_process():
    return subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(120)"])


def test_the_liveness_probe_tells_running_from_finished():
    assert vector_snapshot._pid_alive(os.getpid())
    sleeper = _running_process()
    try:
        assert vector_snapshot._pid_alive(sleeper.pid)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert not vector_snapshot._pid_alive(sleeper.pid), \
        "a finished process read as running"
    done = _finished_process()
    assert not vector_snapshot._pid_alive(done.pid)


def test_copies_of_a_process_that_is_gone_are_swept_on_the_next_copy(tmp_path):
    """A process killed before its `atexit` hook leaves its copy behind. The
    pid in the name says whose it was; only a copy whose owner is certainly
    gone is deleted -- never a running process's, never a stranger's dir."""
    snaps = tmp_path / "snaps"
    done, sleeper = _finished_process(), _running_process()
    try:
        prefix = vector_snapshot.SNAPSHOT_PREFIX
        dead = snaps / f"{prefix}{done.pid}-a1b2"
        live = snaps / f"{prefix}{sleeper.pid}-c3d4"
        odd = snaps / f"{prefix}notapid-e5f6"
        other = snaps / "someone-elses-dir"
        for d in (dead, live, odd, other):
            (d / "chroma").mkdir(parents=True)
            (d / "chroma" / "chroma.sqlite3").write_bytes(b"x")
        log = []
        with vector_snapshot.reading(_source(tmp_path), _opener(log)) as client:
            assert client.path.exists()
        assert not dead.exists(), "a gone process's copy stayed in the temp dir"
        assert live.exists(), "a running process's copy was deleted under it"
        assert odd.exists() and other.exists(), "a non-copy was deleted"
    finally:
        sleeper.kill()
        sleeper.wait()


# --- a copy that could not be taken says why -------------------------------------

def _shutil_with(copytree):
    """`shutil` as vector_snapshot sees it, with *copytree* swapped in."""
    return types.SimpleNamespace(copytree=copytree, rmtree=shutil.rmtree,
                                 Error=shutil.Error)


def test_a_writer_that_lands_mid_copy_costs_one_more_copy(tmp_path,
                                                          monkeypatch):
    """The checked copy's ordinary case: a commit lands while the first copy
    is being taken, so that copy is thrown away and the second one is read.
    One client, and nothing of the torn copy left on disk."""
    src = _source(tmp_path)
    taken = []

    def _copy(s, d, **kw):
        taken.append(Path(d))
        shutil.copytree(s, d, **kw)
        if len(taken) == 1:
            (Path(s) / "seg" / "data_level0.bin").write_bytes(b"landed mid-copy")

    monkeypatch.setattr(vector_snapshot, "shutil", _shutil_with(_copy))
    log = []
    with vector_snapshot.reading(src, _opener(log)) as client:
        assert _tree(client.path)[os.path.join("seg", "data_level0.bin")] == \
            b"landed mid-copy", "the torn copy was the one read"
    assert len(taken) == 2 and len(log) == 1
    assert not taken[0].parent.exists(), "the torn copy was left on disk"


def test_a_copy_stopped_by_anything_takes_its_directory_with_it(tmp_path,
                                                                monkeypatch):
    """Not only an OSError: whatever stops a copy half-way -- an interrupt,
    a MemoryError -- still removes the directory it was being written into."""
    class _Stopped(Exception):
        pass

    def _half(s, d, **kw):
        Path(d).mkdir(parents=True)
        (Path(d) / "chroma.sqlite3").write_bytes(b"half a copy")
        raise _Stopped()

    monkeypatch.setattr(vector_snapshot, "shutil", _shutil_with(_half))
    with pytest.raises(_Stopped):
        with vector_snapshot.reading(_source(tmp_path), _opener([])):
            pass
    snaps = tmp_path / "snaps"
    assert not snaps.exists() or not any(snaps.iterdir()), \
        "a copy stopped half-way was left in the temp dir"


def test_a_copy_that_cannot_be_written_is_not_blamed_on_a_writer(tmp_path,
                                                                 monkeypatch):
    def _full(s, d, **kw):
        raise OSError(errno.ENOSPC, "No space left on device", str(d))

    monkeypatch.setattr(vector_snapshot, "shutil", _shutil_with(_full))
    monkeypatch.setattr(vector_snapshot, "COPY_RETRY_PAUSE_S", 0.0)
    with pytest.raises(vector_snapshot.SnapshotError) as err:
        with vector_snapshot.reading(_source(tmp_path), _opener([])):
            pass
    assert err.value.reason == "copy"
    assert "No space left" in str(err.value)


def test_a_temp_dir_that_cannot_hold_a_copy_is_named(tmp_path, monkeypatch):
    """The copy's destination, not the index, is what is broken -- so it is a
    `SnapshotError` naming that directory, never a raw OSError that reads as a
    damaged index."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr(vector_snapshot, "SNAPSHOT_DIR", blocked)
    with pytest.raises(vector_snapshot.SnapshotError) as err:
        with vector_snapshot.reading(_source(tmp_path), _opener([])):
            pass
    assert err.value.reason == "space"
    assert str(blocked) in str(err.value)


# --- a client whose read failed ---------------------------------------------------
#
# chromadb 1.5.9, measured on a scratch index whose HNSW `header.bin` was cut to
# 7 bytes: the read fails with InternalError, and then ANY teardown of that
# client -- `close()`, dropping the last reference, even interpreter exit --
# corrupts the heap (0xC0000374). So a client a read failed on is never closed
# and never freed, and an index that failed is not copied and opened again
# until its bytes change.

class _Boom(RuntimeError):
    """What a chromadb call on a damaged index raised."""


def _claims(e):
    """The judge: which failures were the client's own."""
    return isinstance(e, _Boom)


@pytest.fixture
def kept(monkeypatch):
    """What `reading` kept alive for good, recorded instead of pinned."""
    kept = []
    monkeypatch.setattr(vector_snapshot, "_immortalise", kept.append)
    return kept


def _read(src, log, name="events"):
    return vector_snapshot.reading(src, _opener(log), judge=_claims, name=name)


def _fail(src, log, name="events", error=None):
    error = error or _Boom("Error loading hnsw index")
    with pytest.raises(type(error)):
        with _read(src, log, name):
            raise error
    return error


def test_a_client_whose_read_failed_is_never_closed(tmp_path, kept):
    src = _source(tmp_path)
    log = []
    _fail(src, log)
    assert kept == [log[0]], "the failed client was not kept alive"
    (src / "seg" / "data_level0.bin").write_bytes(b"a writer added rows")
    with _read(src, log) as client:
        assert client is log[1], "the client that failed served the next read"
    (src / "seg" / "data_level0.bin").write_bytes(b"and more rows")
    with _read(src, log):
        pass
    vector_snapshot.dispose_all()
    assert not log[0].closed, "the client a read failed on was closed"
    assert log[0].path.exists(), "the failed client's copy was deleted under it"
    assert log[1].closed and log[2].closed, "healthy clients stopped closing"


def test_a_client_that_fails_after_a_newer_copy_replaced_it_is_never_closed(
        tmp_path, kept):
    """The race the plain case cannot reach: a writer lands while a read is in
    flight, a second reader takes a newer copy and retires the old one, and
    THEN the first read fails. Its release is the last user of a retired
    snapshot leaving -- the one moment the ordinary path closes a client."""
    src = _source(tmp_path)
    log = []
    with pytest.raises(_Boom):
        with _read(src, log) as old:
            (src / "seg" / "data_level0.bin").write_bytes(b"a writer added rows")
            with _read(src, log) as newer:
                assert newer is not old, "the changed index was not copied again"
            raise _Boom("Error loading hnsw index")
    assert kept == [old], "the failed client was not kept alive"
    assert not old.closed, "a retired client a read failed on was closed"
    assert old.path.exists(), "the failed client's copy was deleted under it"


def test_an_unreadable_index_is_not_opened_again_until_it_changes(tmp_path,
                                                                  kept):
    src = _source(tmp_path)
    log = []
    first = _fail(src, log)
    copies = sorted((tmp_path / "snaps").iterdir())
    with pytest.raises(vector_snapshot.IndexUnreadable) as err:
        with _read(src, log):
            pass
    assert err.value.original is first and not err.value.capped
    assert len(log) == 1, "an unchanged unreadable index was opened again"
    assert sorted((tmp_path / "snaps").iterdir()) == copies, \
        "an unchanged unreadable index was copied again"
    # The failure belongs to the collection it happened on: another one in
    # the same index is still read.
    with _read(src, log, name="ledger") as client:
        assert client is log[1]
    (src / "seg" / "data_level0.bin").write_bytes(b"rebuilt")
    with _read(src, log) as client:
        assert client is log[2], "a changed index was not read again"


def test_a_remembered_failure_is_tried_again_after_a_while(tmp_path, kept,
                                                           monkeypatch):
    """Remembered against the index's bytes, and not forever: a hiccup on an
    index nobody writes to must not end semantic search for the life of the
    dashboard."""
    src = _source(tmp_path)
    log = []
    _fail(src, log)
    monkeypatch.setattr(vector_snapshot, "FAILED_RETRY_S", 0.0)
    with _read(src, log) as client:
        assert client is log[1]


def test_an_index_that_keeps_failing_is_not_reread_past_the_limit(tmp_path,
                                                                  kept,
                                                                  monkeypatch):
    """Every failed read keeps a client and a copy for the life of the
    process. A writer that keeps changing a damaged index would make that
    unbounded, so past `POISON_LIMIT` a source that failed is not read again
    -- and the error says the process has to restart to try."""
    monkeypatch.setattr(vector_snapshot, "POISON_LIMIT", 1)
    src = _source(tmp_path)
    log = []
    first = _fail(src, log)
    (src / "seg" / "data_level0.bin").write_bytes(b"a writer came by")
    with pytest.raises(vector_snapshot.IndexUnreadable) as err:
        with _read(src, log):
            pass
    assert err.value.capped and "restart" in str(err.value)
    assert err.value.original is first
    assert len(log) == 1


def test_past_the_limit_no_collection_of_a_source_that_failed_is_read(
        tmp_path, kept, monkeypatch):
    """The cap is per SOURCE. It refused only the collection that had failed:
    a read of `ledger` in the same damaged index was copied, opened, failed
    and kept one more client, so the clients held grew past the limit by one
    per collection. A source that has never failed is still read."""
    monkeypatch.setattr(vector_snapshot, "POISON_LIMIT", 1)
    src = _source(tmp_path)
    log = []
    first = _fail(src, log)                    # `events` fails: the one client
    with pytest.raises(vector_snapshot.IndexUnreadable) as err:
        with _read(src, log, name="ledger"):
            pass
    assert err.value.capped and err.value.original is first
    assert (err.value.name, err.value.of) == ("ledger", "events")
    assert "'ledger'" in str(err.value) and "'events'" in str(err.value)
    assert len(log) == 1, "a collection of a source past the limit was opened"
    other = _source(tmp_path / "other")
    with _read(other, log, name="ledger") as client:
        assert client is log[1], "a source that never failed was refused"


@pytest.mark.parametrize("b_at", ["copied", "opened"])
def test_a_reader_racing_a_failure_does_not_read_the_same_bytes_again(
        tmp_path, kept, monkeypatch, b_at):
    """Two readers of one unchanged index: the second had copied -- or
    opened -- when the first failed. The memo was asked only on the way in,
    so it went on to open, read and fail on the same bytes a second time:
    one more client kept for good and one more copy on disk per overlapping
    reader. Asked again after the copy and at install, it is refused
    instead: nothing is opened on a copy of bytes that failed, and a client
    that never read is closed with its copy."""
    src = _source(tmp_path)
    log, roots = [], []
    a_opening, b_ready, a_done = (threading.Event() for _ in range(3))
    real_copy = vector_snapshot._copy_stable

    def copy(path):
        built = real_copy(path)
        roots.append(built[1])
        if len(roots) == 2 and b_at == "copied":   # B waits for A to fail
            b_ready.set()
            assert a_done.wait(10)
        return built

    def opener(path):
        client = _FakeClient(path)
        log.append(client)
        if len(log) == 1:                 # A waits for B to be ready
            a_opening.set()
            assert b_ready.wait(10)
        else:                             # B waits for A to have failed
            b_ready.set()
            assert a_done.wait(10)
        return client

    monkeypatch.setattr(vector_snapshot, "_copy_stable", copy)
    outcome = {}

    def reader(tag):
        try:
            with vector_snapshot.reading(src, opener, judge=_claims,
                                         name="events"):
                raise _Boom(f"Error loading hnsw index ({tag})")
        except BaseException as e:        # noqa: BLE001 -- recorded
            outcome[tag] = e
        finally:
            if tag == "A":
                a_done.set()

    a = threading.Thread(target=reader, args=("A",))
    a.start()
    assert a_opening.wait(10)
    b = threading.Thread(target=reader, args=("B",))
    b.start()
    a.join(20)
    b.join(20)
    assert isinstance(outcome["A"], _Boom)
    assert isinstance(outcome["B"], vector_snapshot.IndexUnreadable), outcome
    assert kept == [log[0]], "a second client was kept on the same bytes"
    assert len(vector_snapshot._POISONED) == 1
    assert len(log) == (1 if b_at == "copied" else 2), \
        "a copy of bytes that failed was opened"
    assert all(c.closed for c in log[1:]), \
        "the refused reader's unused client was left open"
    assert roots[0].exists() and not roots[1].exists(), \
        "the refused reader's copy was left behind, or the kept one deleted"


def test_a_copy_older_than_a_failure_does_not_forget_it(tmp_path, kept,
                                                         monkeypatch):
    """The memo asked again after a copy is not dropped for not matching it:
    a copy taken before a writer came by is OLDER than a failure on the bytes
    the writer left, and those bytes, unchanged since, still fail."""
    src = _source(tmp_path)
    log, raced = [], []
    real_copy = vector_snapshot._copy_stable

    def copy(path):
        built = real_copy(path)
        if not raced:                      # B has copied: a writer, then A
            raced.append(built)
            (src / "seg" / "data_level0.bin").write_bytes(b"a writer came by")
            _fail(src, log)                # A copies the new bytes and fails
        return built

    monkeypatch.setattr(vector_snapshot, "_copy_stable", copy)
    with _read(src, log) as client:        # B, on the older copy
        assert client is log[1]
    with pytest.raises(vector_snapshot.IndexUnreadable):
        with _read(src, log):              # the bytes A failed on, unchanged
            pass
    assert kept == [log[0]] and len(log) == 2, \
        "the bytes that failed were opened again"


def test_a_failure_the_judge_does_not_claim_leaves_the_client_in_service(
        tmp_path, kept):
    """An encoder that would not load says nothing about the chroma client:
    it is reused, and closed as usual."""
    src = _source(tmp_path)
    log = []
    _fail(src, log, error=ValueError("no model"))
    with _read(src, log) as client:
        assert client is log[0]
    assert kept == []
    vector_snapshot.dispose_all()
    assert log[0].closed


def test_a_persisted_hnsw_segment_with_a_short_header_is_refused(tmp_path):
    """The measured damage, caught before chromadb reads it. A short header
    in a segment that was never persisted is harmless -- chromadb rebuilds
    those from the log -- so only a segment with `index_metadata.pickle` is
    judged."""
    copy = tmp_path / "chroma"
    whole, cut = copy / "seg-a", copy / "seg-b"
    for seg in (whole, cut):
        seg.mkdir(parents=True)
    (whole / "header.bin").write_bytes(b"h" * vector_snapshot.HNSW_HEADER_BYTES)
    (whole / "index_metadata.pickle").write_bytes(b"m")
    (cut / "header.bin").write_bytes(b"h" * 7)
    vector_snapshot.check_segments(copy)
    (cut / "index_metadata.pickle").write_bytes(b"m")
    with pytest.raises(vector_snapshot.DamagedSegment, match="seg-b"):
        vector_snapshot.check_segments(copy)


def test_a_client_kept_after_a_failure_outlives_every_other_reference():
    """The pin itself: nothing but `_immortalise` holds the object, and it
    still is not collected."""
    import gc
    import weakref

    class _Client:
        pass

    client = _Client()
    ref = weakref.ref(client)
    vector_snapshot._immortalise(client)
    del client
    gc.collect()
    assert ref() is not None, "a kept client was freed"


# --- the property, against real chromadb ---------------------------------------

def _enc(texts):
    """A deterministic 384-wide stand-in for the model: no download, no GPU,
    and two different texts get two different vectors."""
    out = []
    for t in texts:
        digest = hashlib.sha256(t.encode("utf-8")).digest()
        out.append([((digest[i % 32] + i) % 97) / 97.0 for i in range(384)])
    return out


def _hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


def _release_live(chroma_dir):
    """Stop the system the WRITER left open on the live index, so Windows can
    delete the tmp dir. Test hygiene only: chromadb keys it by path."""
    from chromadb.api.shared_system_client import SharedSystemClient
    system = SharedSystemClient._identifier_to_system.pop(str(chroma_dir), None)
    SharedSystemClient._identifier_to_refcount.pop(str(chroma_dir), None)
    if system is not None:
        system.stop()


@pytest.fixture
def real_index(monkeypatch):
    pytest.importorskip("chromadb")
    pytest.importorskip("sentence_transformers")
    monkeypatch.setattr(vectors, "_encoder", lambda: _enc)
    for i, text in enumerate(("chose postgres for the read mirror",
                              "the dashboard polls every two seconds",
                              "decision: keep the ledger append-only")):
        events.append_event("decision", {"text": text}, session_id="s1",
                            ts=f"2026-01-01T00:00:0{i}Z")
    projector.project()
    vectors.embed_events()
    vectors.embed()
    chroma_dir = paths.project_dir() / "chroma"
    _release_live(chroma_dir)
    yield chroma_dir
    vector_snapshot.dispose_all()
    _release_live(chroma_dir)


def test_a_semantic_read_leaves_every_index_file_byte_identical(real_index,
                                                                tmp_path):
    """The finding: one ordinary `hive recall --semantic` changed the sha256
    of `chroma.sqlite3`, `data_level0.bin` and `length.bin`. (Pointing
    `vectors._reading` at the live directory makes exactly those three
    differ here, which is what keeps this test honest.)"""
    before = _hashes(real_index)
    assert "chroma.sqlite3" in before
    hits = recall.recall("postgres mirror", semantic=True)
    assert hits and hits[0]["type"] == "decision"
    assert vectors.indexed_event_count() == 3
    assert vectors.semantic_recall("ledger")
    assert _hashes(real_index) == before, "a read path wrote to the index"
    vector_snapshot.dispose_all()
    assert not any((tmp_path / "snaps").iterdir()), \
        "a closed snapshot's files could not be deleted"


def test_a_read_on_a_never_embedded_namespace_creates_nothing(monkeypatch):
    pytest.importorskip("chromadb")
    pytest.importorskip("sentence_transformers")
    monkeypatch.setattr(vectors, "_encoder", lambda: _enc)
    chroma_dir = paths.project_dir() / "chroma"
    assert recall.recall("anything", semantic=True) == []
    assert vectors.indexed_event_count() == 0
    assert not chroma_dir.exists(), "a read minted an index on an empty namespace"


def test_a_damaged_index_is_an_error_never_an_empty_answer(real_index):
    (real_index / "chroma.sqlite3").write_bytes(b"not a database " * 64)
    with pytest.raises(vectors.SemanticQueryError) as err:
        recall.recall("postgres mirror", semantic=True)
    assert err.value.stage in ("open", "count")
    assert err.value.collection == vectors.EVENT_COLLECTION


def test_a_read_sees_what_a_writer_added_since_the_last_read(real_index):
    assert vectors.indexed_event_count() == 3
    events.append_event("decision", {"text": "a fourth decision"},
                        session_id="s2", ts="2026-01-02T00:00:00Z")
    vectors.embed_events()
    _release_live(real_index)
    assert vectors.indexed_event_count() == 4, "a stale copy answered"


def test_a_read_in_progress_keeps_its_copy_while_a_newer_one_is_read(
        real_index):
    with vectors._read_view(None, vectors.EVENT_COLLECTION) as held:
        events.append_event("decision", {"text": "a fourth decision"},
                            session_id="s2", ts="2026-01-02T00:00:00Z")
        vectors.embed_events()
        _release_live(real_index)
        assert vectors.indexed_event_count() == 4
        assert held.count() == 3, "the older copy was closed under its reader"


def _collections(chroma_dir, scratch):
    """The collection names an index holds, read from a copy of it: even a
    read-only sqlite3 connection may leave -wal/-shm files beside its file."""
    shutil.copytree(chroma_dir, scratch)
    con = sqlite3.connect(str(scratch / "chroma.sqlite3"))
    try:
        return {row[0] for row in con.execute("SELECT name FROM collections")}
    finally:
        con.close()


def _record(seg_line, text):
    return types.SimpleNamespace(
        session_id="s1", seg_line=seg_line, chain_hash="c0ffee",
        event_type="decision", timestamp="2026-01-01T00:00:00Z",
        payload={"text": text})


def test_the_watchers_id_probe_is_a_read_like_any_other(real_index, tmp_path):
    """`already_indexed` decides what the watcher re-embeds. It opened the
    LIVE index with `get_or_create_collection`, so probing a slug nothing was
    ever embedded under minted an empty collection for it, and every probe
    wrote to `chroma.sqlite3`."""
    mgr = vectors.VectorStoreManager()
    before = _hashes(real_index)
    assert mgr.already_indexed("probe", ["s1:1"]) == set()
    assert _hashes(real_index) == before, "an id probe wrote to the index"
    assert vectors.collection_name("probe") not in _collections(
        real_index, tmp_path / "c1"), "an id probe created a collection"
    mgr.embed_batch("probe", [_record(1, "chose postgres")],
                    sleep=lambda _s: None,
                    service=vectors.LocalEmbeddingService(encoder=_enc))
    _release_live(real_index)
    before = _hashes(real_index)
    assert mgr.already_indexed("probe", ["s1:1", "s1:2"]) == {"s1:1"}
    assert _hashes(real_index) == before


def test_a_copy_chromadb_cannot_open_is_not_left_registered(tmp_path):
    """chromadb registers a client's system BEFORE starting it, so an open
    that fails leaves the half-started system in its registry for the life of
    the process (measured: still registered after "file is not a database"):
    one leaked system per failed read."""
    pytest.importorskip("chromadb")
    from chromadb.api.shared_system_client import SharedSystemClient
    opener = vector_snapshot.open_chroma
    src = tmp_path / "store" / "chroma"
    src.mkdir(parents=True)
    (src / "chroma.sqlite3").write_bytes(b"not a database " * 64)
    with pytest.raises(Exception):
        with vector_snapshot.reading(src, opener):
            pass
    snaps = tmp_path / "snaps"
    assert not [k for k in SharedSystemClient._identifier_to_system
                if k.startswith(str(snaps))], \
        "the system of a client that failed to open stayed registered"
    vector_snapshot.dispose_all()
    assert not snaps.exists() or not any(snaps.iterdir()), \
        "the copy chromadb failed to open stayed on disk"


# --- a damaged HNSW segment, in a process of its own ---------------------------
#
# The failure these defences exist for kills the interpreter, so it is driven
# in a child: `build` persists an events segment (by default 1300 rows past
# chroma's sync_threshold of 1000, width 8 to keep it quick) and cuts its
# `header.bin` to 7 bytes; the read modes then count three times, the source
# changing once between the second and third.

_CHILD = r'''
import sys
from pathlib import Path
mode, snaps = sys.argv[1], Path(sys.argv[2])
total, sync = (int(a) for a in (sys.argv[3:5] or (1300, 0)))
from mindhive import events, paths, vector_snapshot, vectors
vector_snapshot.SNAPSHOT_DIR = snaps
chroma = paths.project_dir() / "chroma"
if mode == "build":
    events.append_event("decision", {"text": "use postgres"})
    if sync:     # fewer rows waiting behind a persisted segment
        col = vectors._client(None).get_or_create_collection(
            vectors.EVENT_COLLECTION, metadata=dict(
                vectors.HNSW_METADATA, **{"hnsw:sync_threshold": sync,
                                          "hnsw:batch_size": min(sync, 100)}))
    else:
        col = vectors._events_collection(None, refill=True)
    step = min(sync or 100, 100)
    for s in range(0, total, step):
        rows = range(s, min(s + step, total))
        col.add(ids=[f"id{i}" for i in rows], documents=[f"d{i}" for i in rows],
                embeddings=[[((i * 7 + j) % 11) / 11.0 for j in range(8)]
                            for i in rows],
                metadatas=[{"type": "decision", "ts": "t", "session_id": "s"}]
                * len(rows))
    from chromadb.api.shared_system_client import SharedSystemClient as S
    S._identifier_to_refcount.pop(str(chroma), None)
    S._identifier_to_system.pop(str(chroma)).stop()
    for header in chroma.glob("*/header.bin"):
        if (header.parent / "index_metadata.pickle").exists():
            header.write_bytes(header.read_bytes()[:7])
            print("damaged", header.parent.name)
    sys.exit(0)
if mode == "unchecked":
    vector_snapshot.check_segments = lambda copy: None
for attempt in range(3):
    try:
        print("count", vectors.indexed_event_count(strict=True), flush=True)
    except vectors.SemanticQueryError as e:
        print("failed", e.stage, flush=True)
    if attempt == 1:
        (chroma / "touched.txt").write_text("a writer came by")
print("copies", len(list(snaps.glob(vector_snapshot.SNAPSHOT_PREFIX + "*"))),
      flush=True)
'''


def _child(mode, snaps, cwd, *build):
    src = Path(vectors.__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(src),
               MINDHIVE_HOME=str(paths.store_home()))
    done = subprocess.run([sys.executable, "-c", _CHILD, mode, str(snaps),
                           *map(str, build)],
                          capture_output=True, text=True, env=env, cwd=cwd,
                          timeout=300)
    assert done.returncode == 0, (
        f"{mode}: exit {done.returncode:#x}\n{done.stdout}\n"
        f"{done.stderr[-3000:]}")
    return done.stdout


def test_a_damaged_hnsw_segment_fails_every_read_and_never_the_process(
        tmp_path):
    """Measured without these defences: the count fails, and closing that
    client at the next copy corrupts the heap (0xC0000374). `unchecked` is
    the damage getting past the header check: every read still fails as a
    count, the client it failed on is kept rather than torn down, and the
    unchanged index is not copied again. `checked` is the check itself: the
    copy is refused before chromadb opens it, and the copies the killed-off
    readers kept are swept by the next process."""
    pytest.importorskip("chromadb")
    # Without the model the count answers 0 before it reads anything, which
    # would read here as the damage being counted as an empty index.
    pytest.importorskip("sentence_transformers")
    snaps = tmp_path / "child-snaps"
    assert "damaged" in _child("build", snaps, tmp_path)
    out = _child("unchecked", snaps, tmp_path)
    assert out.count("failed count") == 3, out
    assert "copies 2" in out, out
    out = _child("checked", snaps, tmp_path)
    assert out.count("failed open") == 3, out
    assert "copies 0" in out, out


@pytest.mark.parametrize("total,sync", [(130, 100), (35, 10)],
                         ids=["130-rows-sync-100", "35-rows-sync-10"])
def test_the_damage_that_crashed_or_hung_the_count_never_reaches_chromadb(
        tmp_path, total, sync):
    """The same damage with fewer rows waiting in the log: measured with no
    header check, runs of the count crashed the process, hung it past two
    minutes, or failed -- which one varied between runs, and nothing in the
    process survives the first two once chromadb reads the segment. The
    check refuses the copy before it does, so each read fails as an `open`
    and the process lives to say so. Not run `unchecked`: that is the crash
    or the hang."""
    pytest.importorskip("chromadb")
    pytest.importorskip("sentence_transformers")
    snaps = tmp_path / "child-snaps"
    assert "damaged" in _child("build", snaps, tmp_path, total, sync)
    out = _child("checked", snaps, tmp_path)
    assert out.count("failed open") == 3, out
    assert "copies 0" in out, out
