# tests/test_embedqueue.py
"""The pending-embed queue and the newline cursor (`embedqueue`): the producer
half of the hand-off `tests/test_watcher.py` consumes.

`append_event` writes one row and, for a SEMANTIC type, queues
`(session_id, segment, seg_line)` for the background watcher. Two costs of that
hand-off used to sit on the append path -- inside `events._append_lock`, so
every writer in the namespace paid them while waiting -- and this file pins
their removal:

* **The queue is appended to, never rewritten.** It was one JSON document,
  re-serialised and `os.replace`d on every semantic append, and in a
  namespace no watcher drains it only grew (3.1 KB in the live store at the
  review that found it, with nothing to stop it). It is now one JSON object
  per line: an append is one `open(..., "a")` and one write, and the file is
  compacted -- one entry per row, the newest kept -- only once it passes
  `QUEUE_MAX_BYTES`. The document shape is still READ, so a claim taken by an
  older watcher still drains.
* **The segment is read before the lock.** `seg_line` needs the count of
  `b"\\n"` in the segment, 3.24 ms on the largest live segment, and it ran
  inside the lock. `newline_cursor` reads the whole segment BEFORE the lock
  and, inside it, reconciles that count against what other writers appended
  meanwhile -- a `stat`, and a read of those bytes alone.

Every negative here has a positive control beside it: a guard that cannot fire
proves nothing by staying quiet.
"""
import json
import os
import types
from pathlib import Path

import pytest

from mindhive import embedqueue, paths


# --- helpers -----------------------------------------------------------------

def _marker(project=None) -> Path:
    """Joined by hand, not through `paths.state_dir`, which mkdirs."""
    return paths.project_dir(project) / "state" / embedqueue.MARKER_NAME


def _emit(seg_line, session_id="alpha", project=None) -> None:
    embedqueue.emit_pending_embed_marker(project, session_id,
                                        f"{session_id}.jsonl", seg_line)


def _entry(seg_line, session_id="alpha", queued_at="2026-09-01T00:00:00Z"):
    return {"session_id": session_id, "seg_line": seg_line,
            "queued_at": queued_at, "segment": f"{session_id}.jsonl"}


def _lines(path) -> list:
    """The file as its readers see it -- but parsed HERE, line by line, so a
    test never trusts `load_entries` to check `load_entries`."""
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _width() -> int:
    """The bytes one single-digit entry takes on disk."""
    return len(embedqueue.dump_entries([_entry(1)]).encode("utf-8"))


def _publishes(monkeypatch) -> list:
    """Every path `paths.write_text_atomic` publishes while the test runs."""
    real = paths.write_text_atomic
    seen = []

    def spy(path, text):
        seen.append(Path(path))
        real(path, text)

    monkeypatch.setattr(paths, "write_text_atomic", spy)
    return seen


def _shadow_open(monkeypatch, opener) -> None:
    """Shadow the builtin `open` that `embedqueue` reaches through its module
    globals -- the one seam an append has -- with *opener*."""
    monkeypatch.setattr(embedqueue, "open", opener, raising=False)


# --- the newline cursor ------------------------------------------------------

def test_a_missing_segment_is_an_empty_cursor(tmp_path):
    cur = embedqueue.newline_cursor(tmp_path / "none.jsonl")
    assert cur == embedqueue.NewlineCursor(size=0, mtime_ns=0, newlines=0)


def test_the_cursor_counts_newline_bytes_over_the_extent_it_read(tmp_path):
    """A CRLF row is one row (one `b"\\n"`), and a torn tail with no newline
    is not a line yet: the count is of newline BYTES, the extent is of bytes."""
    seg = tmp_path / "s.jsonl"
    seg.write_bytes(b"a\r\nb\ntail")
    cur = embedqueue.newline_cursor(seg)
    assert (cur.newlines, cur.size) == (2, 9)
    assert cur.mtime_ns == os.stat(seg).st_mtime_ns


def test_the_extent_is_what_was_read_not_what_stat_said(tmp_path, monkeypatch):
    """Outside the lock the segment can grow between the `stat` and the read.
    A cursor that recorded the stat size would hand the in-lock reconciliation
    a start INSIDE bytes already counted, and the row would be filed one line
    too far."""
    seg = tmp_path / "s.jsonl"
    seg.write_bytes(b"a\nb\n")
    real = os.stat
    shrunk = types.SimpleNamespace(st_size=2, st_mtime_ns=real(seg).st_mtime_ns)
    monkeypatch.setattr(embedqueue.os, "stat",
                        lambda p, *a, **kw: shrunk if str(p) == str(seg)
                        else real(p, *a, **kw))
    cur = embedqueue.newline_cursor(seg)
    assert (cur.size, cur.newlines) == (4, 2)


def test_a_grown_segment_is_reconciled_from_the_prior_extent_alone(tmp_path):
    """Only the bytes past the prior's extent are read. Proven by REWRITING
    the prefix, which an append-only ledger never does: the prefix's newlines
    are trusted from the prior, so the answer is 4 where a full recount would
    say 2 -- and a reconciliation that trusted nothing would have read the
    whole segment inside the lock, the exact cost this exists to remove."""
    seg = tmp_path / "s.jsonl"
    seg.write_bytes(b"a\nb\n")
    prior = embedqueue.newline_cursor(seg)
    seg.write_bytes(b"XXXX" + b"c\nd\n")
    cur = embedqueue.newline_cursor(seg, prior)
    assert (cur.newlines, cur.size) == (4, 8)


def test_a_segment_that_shrank_is_recounted_from_the_start(tmp_path):
    seg = tmp_path / "s.jsonl"
    seg.write_bytes(b"a\nb\nc\n")
    prior = embedqueue.newline_cursor(seg)
    seg.write_bytes(b"x\n")
    cur = embedqueue.newline_cursor(seg, prior)
    assert (cur.newlines, cur.size) == (1, 2)


def test_an_unchanged_segment_costs_a_stat_and_no_read(tmp_path, monkeypatch):
    """The common case inside the lock: nobody appended in between. The prior
    comes back AS IS and the segment is not opened at all."""
    seg = tmp_path / "s.jsonl"
    seg.write_bytes(b"a\nb\n")
    prior = embedqueue.newline_cursor(seg)

    def no_read(*a, **kw):
        raise AssertionError("the segment was opened for an unchanged cursor")

    _shadow_open(monkeypatch, no_read)
    assert embedqueue.newline_cursor(seg, prior) is prior


def test_a_same_size_rewrite_is_told_apart_by_its_mtime(tmp_path):
    """Size alone cannot tell a rewrite from no change; the mtime can. And the
    canary: the same bytes with the prior's mtime put back read as unchanged,
    so it is the mtime doing the telling, not the content."""
    seg = tmp_path / "s.jsonl"
    seg.write_bytes(b"a\nb\n")
    prior = embedqueue.newline_cursor(seg)
    seg.write_bytes(b"abc\n")                 # same size, one newline fewer
    later = prior.mtime_ns + 5 * 10 ** 9
    os.utime(seg, ns=(later, later))
    assert embedqueue.newline_cursor(seg, prior).newlines == 1
    os.utime(seg, ns=(prior.mtime_ns, prior.mtime_ns))
    assert embedqueue.newline_cursor(seg, prior) is prior


def test_an_unreadable_segment_is_none_rather_than_a_count(tmp_path):
    """None, never 0: 0 would file the row under line 1 and the watcher would
    embed the wrong row. `append_event` reads None as "no marker"."""
    seg = tmp_path / "s.jsonl"
    seg.mkdir()                               # stats fine, cannot be opened
    assert embedqueue.newline_cursor(seg) is None
    prior = embedqueue.NewlineCursor(size=1, mtime_ns=1, newlines=1)
    assert embedqueue.newline_cursor(seg, prior) is None


# --- the queue: appended, never rewritten ------------------------------------

def test_a_semantic_append_adds_one_line_and_leaves_the_rest_untouched():
    _emit(1)
    first = _marker().read_bytes()
    _emit(2)
    second = _marker().read_bytes()
    assert second.startswith(first), "the earlier line was rewritten"
    assert second.count(b"\n") == 2
    entries = _lines(_marker())
    assert [e["seg_line"] for e in entries] == [1, 2]
    assert all(set(e) == {"session_id", "segment", "seg_line", "queued_at"}
               for e in entries), entries
    assert all(e["segment"] == "alpha.jsonl" for e in entries)
    assert list(_marker().parent.glob("*.tmp")) == []


def test_the_append_path_neither_publishes_nor_reads_the_queue(monkeypatch):
    """Both halves of the old cost. Publishing is refused outright, so an
    append that still went through `write_text_atomic` fails here rather than
    merely being slow; reads are counted."""
    def refuse(path, text):
        raise AssertionError("the append path re-published the queue")

    monkeypatch.setattr(paths, "write_text_atomic", refuse)
    reads = []
    real = paths.read_text_retry
    monkeypatch.setattr(paths, "read_text_retry",
                        lambda p: reads.append(Path(p)) or real(p))
    _emit(1)
    _emit(2)
    assert [e["seg_line"] for e in _lines(_marker())] == [1, 2]
    assert _marker() not in reads


def test_a_line_after_a_torn_tail_starts_on_a_fresh_line():
    """A writer killed mid-append leaves a line with no newline. Glued to it,
    the next line would be lost with it -- two rows for one crash."""
    _marker().parent.mkdir(parents=True, exist_ok=True)
    torn = b'{"session_id": "z", "seg'
    _marker().write_bytes(torn)
    _emit(1)
    raw = _marker().read_bytes()
    assert raw.startswith(torn + b"\n") and raw.count(b"\n") == 2
    assert [e["seg_line"] for e in embedqueue.load_entries(_marker())] == [1]


def test_a_queue_line_is_one_ascii_object_whatever_the_id_holds():
    """A newline or a non-ASCII character in a session id must not become a
    second line, nor a line another platform decodes differently."""
    _emit(1, session_id="a\nb")
    _emit(2, session_id="café")
    raw = _marker().read_bytes()
    assert raw.count(b"\n") == 2 and raw.isascii()
    assert [e["session_id"] for e in _lines(_marker())] == ["a\nb", "café"]


# --- reading the queue back --------------------------------------------------

def test_load_entries_reads_one_object_per_line_and_skips_what_is_not_one(tmp_path):
    """A torn last line -- the writer died mid-append -- costs that line and
    nothing else, where the document-shaped queue lost everything on one bad
    byte. A blank line, a non-object, and an object naming no row are skipped
    the same way."""
    q = tmp_path / "q.json"
    q.write_text(json.dumps(_entry(1)) + "\n\n" + json.dumps(_entry(2)) + "\n"
                 + "[1, 2]\n" + '{"note": "no row here"}\n'
                 + '{"session_id": "z", "seg', encoding="utf-8")
    assert [e["seg_line"] for e in embedqueue.load_entries(q)] == [1, 2]


def test_load_entries_still_reads_the_document_an_older_writer_left(tmp_path):
    """`{"entries": [...]}` was the queue's shape before it was bounded. A
    claim renamed from such a queue by an older watcher, or left by a crash
    across the upgrade, must still drain rather than sit on disk forever."""
    q = tmp_path / "q.json"
    q.write_text(json.dumps({"entries": [_entry(1), "junk", _entry(2)]},
                            indent=2), encoding="utf-8")
    assert [e["seg_line"] for e in embedqueue.load_entries(q)] == [1, 2]


def test_load_entries_reads_a_document_with_lines_appended_after_it(tmp_path):
    """The upgrade path itself: a queue the OLD writer left, in the document
    shape and with no trailing newline, that the new writer then appends to.
    Every row must be seen -- the document's and the lines' -- or the rows
    queued before the upgrade would wait for `hive embed` forever."""
    _marker().parent.mkdir(parents=True, exist_ok=True)
    _marker().write_text(json.dumps({"entries": [_entry(1), _entry(2)]},
                                    indent=2), encoding="utf-8")
    _emit(3)
    _emit(4)
    assert [e["seg_line"] for e in embedqueue.load_entries(_marker())] == [1, 2, 3, 4]


def test_load_entries_survives_a_byte_run_that_is_not_utf8(tmp_path):
    q = tmp_path / "q.json"
    q.write_bytes(b"\xff\xfe not text\n" + json.dumps(_entry(1)).encode() + b"\n")
    assert [e["seg_line"] for e in embedqueue.load_entries(q)] == [1]


@pytest.mark.parametrize("text", ["", "[1, 2]", "42", '"a string"', "{ not json",
                                  '{"entries": "not a list"}'])
def test_load_entries_is_empty_for_anything_that_is_not_a_queue(tmp_path, text):
    q = tmp_path / "q.json"
    q.write_text(text, encoding="utf-8")
    assert embedqueue.load_entries(q) == []


def test_load_entries_is_empty_for_a_missing_or_unopenable_file(tmp_path):
    assert embedqueue.load_entries(tmp_path / "none.json") == []
    (tmp_path / "dir.json").mkdir()
    assert embedqueue.load_entries(tmp_path / "dir.json") == []


def test_load_entries_reads_through_read_text_retry(monkeypatch):
    """The watcher publishes over this file with `os.replace`, and for that
    instant Windows refuses a plain `open()` of the destination; the retrying
    reader is the mirror of the retrying writer."""
    reads = []
    real = paths.read_text_retry
    monkeypatch.setattr(paths, "read_text_retry",
                        lambda p: reads.append(Path(p)) or real(p))
    _emit(1)
    assert embedqueue.load_entries(_marker()) == _lines(_marker())
    assert reads == [_marker()]


def test_entry_key_is_the_vector_id():
    assert embedqueue.entry_key(_entry(7)) == "alpha:7"
    assert embedqueue.entry_key({}) == "None:None"


def test_dedupe_keeps_the_first_copy_of_each_row():
    """First, not last: the first copy carries the older `queued_at`, the one
    evidence of how long the row has really been waiting."""
    a, b, again = _entry(1, queued_at="t0"), _entry(2), _entry(1, queued_at="t1")
    assert embedqueue.dedupe_entries([a, b, again]) == [a, b]


# --- the bound ---------------------------------------------------------------

def test_the_bound_is_the_one_the_review_fixed():
    """256 KiB: roughly 2,500 entries -- weeks of decisions in a namespace
    nothing drains -- and still a claim the watcher folds in one cycle."""
    assert embedqueue.QUEUE_MAX_BYTES == 256 * 1024


def test_a_queue_past_its_bound_is_compacted_to_the_newest_half(monkeypatch):
    """Six entries fit under the bound; the seventh takes the file past it
    and triggers the one rewrite, which keeps the newest entries that fit in
    HALF the bound -- so the next rewrite is another half-bound of appends
    away, not the very next one."""
    width = _width()
    monkeypatch.setattr(embedqueue, "QUEUE_MAX_BYTES", 6 * width + width // 2)
    published = _publishes(monkeypatch)
    for line in range(1, 7):
        _emit(line)
    assert published == [], "compaction ran before the bound was crossed"
    assert [e["seg_line"] for e in _lines(_marker())] == [1, 2, 3, 4, 5, 6]
    _emit(7)
    assert published == [_marker()]
    assert [e["seg_line"] for e in _lines(_marker())] == [5, 6, 7]
    assert len(_marker().read_bytes()) <= embedqueue.QUEUE_MAX_BYTES // 2
    assert list(_marker().parent.glob("*.tmp")) == []


def test_compaction_folds_duplicates_before_it_drops_a_row(monkeypatch):
    """Seven copies of one row are one row, not the newest three of seven."""
    width = _width()
    monkeypatch.setattr(embedqueue, "QUEUE_MAX_BYTES", 6 * width + width // 2)
    for _ in range(7):
        _emit(1)
    assert [e["seg_line"] for e in _lines(_marker())] == [1]


def test_a_compacted_queue_is_appended_to_afterwards(monkeypatch):
    width = _width()
    monkeypatch.setattr(embedqueue, "QUEUE_MAX_BYTES", 6 * width + width // 2)
    for line in range(1, 9):
        _emit(line)
    assert [e["seg_line"] for e in _lines(_marker())] == [5, 6, 7, 8]


# --- the moved-file guard ----------------------------------------------------
#
# `open(..., "a")` and `os.replace` do not exclude each other on POSIX: the
# watcher's claim, or a publish of a new file over the name, can move the
# queue while an appender holds it open, and the line then lands in the moved
# (or orphaned) inode. On Windows the rename is refused while the handle is
# open -- `watcher.atomic_rename_resilient` retries it -- so the race cannot
# happen there, and the two that stage it are skipped where they cannot be.

_posix_only = pytest.mark.skipif(
    os.name == "nt", reason="Windows refuses to move a file a writer holds open")


def _open_then(monkeypatch, after) -> list:
    """Open for real, then run *after* before the write lands -- once."""
    real_open = open
    fired = []

    def opener(path, mode="r", *a, **kw):
        f = real_open(path, mode, *a, **kw)
        if "a" in mode and not fired:
            fired.append(Path(path))
            after()
        return f

    _shadow_open(monkeypatch, opener)
    return fired


@_posix_only
def test_a_line_written_into_a_queue_the_watcher_just_claimed_is_written_again(
        monkeypatch):
    claim = _marker().parent / "processing_embed_1.json"
    _emit(1)
    fired = _open_then(monkeypatch, lambda: os.replace(_marker(), claim))
    _emit(2)
    assert fired == [_marker()]                            # positive control
    assert [e["seg_line"] for e in _lines(claim)] == [1, 2]
    assert [e["seg_line"] for e in _lines(_marker())] == [2], (
        "the line went only into the claim, which the watcher may have read already")


@_posix_only
def test_a_line_written_into_a_queue_being_published_over_is_written_again(
        monkeypatch):
    _emit(1)
    fired = _open_then(monkeypatch, lambda: paths.write_text_atomic(_marker(), ""))
    _emit(2)
    assert fired == [_marker()]
    assert [e["seg_line"] for e in _lines(_marker())] == [2], (
        "the line went into the orphaned inode the publish replaced")


def test_a_refused_append_gives_up_after_its_attempts_without_raising(monkeypatch):
    """Fail-open, and bounded: the row is already safe, the queue is
    disposable (`hive embed` re-indexes from the ledger), and this runs inside
    the append lock, where a writer that spins holds every other writer."""
    tries = []

    def refuse(path, mode="r", *a, **kw):
        tries.append(mode)
        raise PermissionError(32, "sharing violation", str(path))

    _shadow_open(monkeypatch, refuse)
    _emit(1)                                               # raises nothing
    assert len(tries) == embedqueue.APPEND_ATTEMPTS
    assert all("a" in mode for mode in tries), tries
    assert not _marker().exists()


def test_a_directory_in_the_way_is_survived_silently():
    _marker().mkdir(parents=True)
    _emit(1)
    assert _marker().is_dir() and list(_marker().iterdir()) == []


# --- recovery: merging back without duplicating ------------------------------

def test_requeue_puts_recovered_rows_at_the_head_keeping_their_stamps():
    _emit(3)
    old = [_entry(1, queued_at="2026-01-01T00:00:00Z"),
           _entry(2, queued_at="2026-01-01T00:00:01Z")]
    assert embedqueue.requeue_entries(None, old) == 2
    got = _lines(_marker())
    assert [e["seg_line"] for e in got] == [1, 2, 3]
    assert got[0]["queued_at"] == "2026-01-01T00:00:00Z"
    assert list(_marker().parent.glob("*.tmp")) == []


def test_requeue_never_queues_a_row_twice():
    """A claim whose unlink was refused is recovered again on the next pass,
    and two watchers' claims can name one row. Both put the same row back,
    and a queue holding two copies handed the watcher a batch chromadb
    refused (DuplicateIDError) -- which left the claim on disk to be recovered,
    and refused, again."""
    _emit(1)
    _emit(2)
    (again,) = [e for e in _lines(_marker()) if e["seg_line"] == 1]
    assert embedqueue.requeue_entries(None, [again, _entry(2), _entry(2)]) == 0
    assert embedqueue.requeue_entries(None, [_entry(5), _entry(5)]) == 1
    assert [e["seg_line"] for e in _lines(_marker())] == [5, 1, 2]


def test_requeue_of_nothing_creates_nothing():
    assert embedqueue.requeue_entries(None, []) == 0
    assert embedqueue.requeue_entries(None, ["junk", 3, None]) == 0
    assert not _marker().exists()
    assert not _marker().parent.exists(), "an empty requeue minted state/"


def test_requeue_merges_above_the_bound_rather_than_dropping_a_row(monkeypatch):
    """The bound is the APPEND path's: a namespace nothing drains has no
    claims to recover. Recovery is the path that must not lose work, so it
    merges everything and leaves the next append to compact."""
    monkeypatch.setattr(embedqueue, "QUEUE_MAX_BYTES", 2 * _width())
    _emit(9)
    assert embedqueue.requeue_entries(None, [_entry(i) for i in range(1, 9)]) == 8
    assert [e["seg_line"] for e in _lines(_marker())] == list(range(1, 10))
