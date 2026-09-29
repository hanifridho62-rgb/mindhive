# SPDX-License-Identifier: Apache-2.0
"""The `pending_embed.json` queue: the hand-off from the append path to the
background vector watcher.

`events.append_event` writes one row to one segment and returns. Something else
has to notice that a new decision or caveat exists and embed it, and it has to
find that row again WITHOUT re-scanning the ledger -- `post_tool_use` appends
on every tool call, and a watcher that re-read every segment to find the new
rows would be the unbounded scan CLAUDE.md forbids, run continuously. So every
SEMANTIC append queues one entry into `<namespace>/state/pending_embed.json`
and the watcher consumes the queue. This module owns both halves of that
entry the append path has to compute -- WHERE the row is (`newline_cursor`,
the physical line) and the queue write itself (`emit_pending_embed_marker`) --
and the queue's readers, so producer and consumer cannot disagree about the
file's shape.

**The file is one JSON object per line, appended to, never rewritten** -- with
one exception, below. It was a single `{"entries": [...]}` document, read,
re-serialised and `os.replace`d on every semantic append, inside the append
lock: a rewrite that grew with the queue, and in a namespace no watcher drains
(most of the 48 on this machine) the queue only grew. An append is now one
`open(..., "a")` and one write, O(1) in the size of the queue. A torn line -- a
writer killed mid-append -- costs that line alone, where a torn document lost
every entry. The document shape is still READ (`load_entries`), so a claim an
older watcher renamed from such a queue still drains after an upgrade.

**The file is bounded.** An append that takes it past `QUEUE_MAX_BYTES`
compacts it: one entry per row, the newest that fit in half the bound kept,
published atomically -- the one rewrite, once per half-bound of appends rather
than on every one. Dropping the oldest loses nothing for good: the queue is a
fast path, and `hive embed` re-indexes from the ledger.

Its own module rather than more of `events.py`, for two reasons. `events.py`
is at the file-size cap's door, and the marker is a SECONDARY projection: the
row is the primary record, and the code that runs between a row being written
and the lock being released should be as small as the code that writes it.
It imports `paths` only -- never `events` -- so `events` can bind
`emit_pending_embed_marker` by identity under its own private name and the
watcher can import this without pulling the type registry along.
"""
import io
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

# The binary read size for the newline count. One mebibyte keeps a live segment
# of tens of megabytes off the heap while costing a handful of reads; newlines
# are single bytes, so no chunk boundary can split one.
COUNT_CHUNK = 1 << 20

MARKER_NAME = "pending_embed.json"

#: The queue's bound, in bytes: what an append can measure for free (the
#: `fstat` it already does). 256 KiB is roughly 2,500 entries -- weeks of
#: decisions in a namespace nothing drains -- and still a claim the watcher
#: folds in one cycle. Past it the file is compacted to at most half.
QUEUE_MAX_BYTES = 256 * 1024

#: How many times an append is tried before the entry is given up. The
#: refusals it rides out clear in microseconds (a publisher's `os.replace`
#: holding the name on Windows; the file moved from under an open handle on
#: POSIX); anything that survives three is not transient, and this runs
#: inside the append lock, where a writer that spins holds every writer.
APPEND_ATTEMPTS = 3
_APPEND_RETRY_S = 0.001

_log = __import__("logging").getLogger("mindhive.embedqueue")


# --- where the row is: the newline cursor -----------------------------------

@dataclass(frozen=True, slots=True)
class NewlineCursor:
    """Where a newline count stopped: the byte extent it covers, the mtime the
    segment had when it was taken, and the `b"\\n"` bytes inside that extent."""
    size: int
    mtime_ns: int
    newlines: int


def newline_cursor(path, prior=None):
    """The `b"\\n"` count of *path* as a cursor, or None when it cannot be read.

    Binary, because `append_event` opens the segment in TEXT mode and Windows
    therefore writes CRLF: one `b"\\n"` per row is still one row, and counting a
    single byte is not a split -- `tests/test_architecture_drift.py` allows
    exactly one function to decide where a ledger row ends, and a `.split` or
    `.splitlines()` here would be a third reader making that decision.

    Called TWICE per semantic append. Without *prior*, from OUTSIDE the append
    lock: the whole segment is read, chunked, because a live segment can run to
    tens of megabytes -- 3.24 ms on the largest live one, the cost that used
    to sit inside the lock where every writer in the namespace waited it out.
    With *prior*, from INSIDE the lock: a `stat`, and then nothing when the
    size and mtime are the prior's; a read of the bytes past the prior's
    extent alone when the segment grew (an append-only ledger only ever grows,
    so the prefix is trusted); a full recount when it shrank or was rewritten
    at the same size. The extent recorded is what was READ, not what `stat`
    said: outside the lock the file can grow between the two, and a cursor
    short of its own count would have the reconciliation count bytes twice.

    A missing file is an empty cursor -- line 1. Any other failure to read is
    None, never 0: 0 would file the row under line 1, a wrong line the watcher
    would then embed the wrong row from, which is worse than no marker at all.
    `append_event` reads None as "skip the marker, never the row" -- the marker
    is a secondary projection, and the failure is the one `open(seg, "a")` is
    about to hit on the same file a line later, where the writer can see it."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return NewlineCursor(0, 0, 0)
    except OSError:
        return None
    start, base = 0, 0
    if prior is not None:
        if st.st_size == prior.size and st.st_mtime_ns == prior.mtime_ns:
            return prior
        if st.st_size > prior.size:
            start, base = prior.size, prior.newlines
    end, total = start, base
    try:
        with open(path, "rb") as f:
            if start:
                f.seek(start)
            for chunk in iter(lambda: f.read(COUNT_CHUNK), b""):
                total += chunk.count(b"\n")
                end += len(chunk)
    except OSError:
        return None
    return NewlineCursor(end, st.st_mtime_ns, total)


# --- the entries -------------------------------------------------------------

def entry_key(entry) -> str:
    """The row an entry names, spelled as the id its vector carries --
    `"<session_id>:<seg_line>"` (`VectorStoreManager.embed_batch`). One
    spelling, here, for the dedupe on both sides of the hand-off."""
    return f"{entry.get('session_id')}:{entry.get('seg_line')}"


def dedupe_entries(entries) -> list:
    """*entries* with each row once, first copy kept, order otherwise intact.
    First rather than last because the first copy carries the older
    `queued_at`: the one evidence of how long the row has really waited."""
    seen = {}
    for e in entries:
        seen.setdefault(entry_key(e), e)
    return list(seen.values())


def _is_entry(obj) -> bool:
    return isinstance(obj, dict) and "session_id" in obj and "seg_line" in obj


def load_entries(path) -> list:
    """Every entry *path* holds, in order; `[]` for a file that is missing,
    unreadable, or not a queue at all.

    Read through `paths.read_text_retry`: the watcher publishes over this file
    with `os.replace`, and for that instant Windows refuses a plain `open()` of
    the destination. Two shapes are read -- one JSON object per line, which
    every writer here emits, and the single `{"entries": [...]}` document the
    queue was before it was bounded, so a claim taken by an older watcher
    still drains, and so does a queue an older writer left that this writer
    then appended lines to: the document is decoded from the head of the file
    and the lines after it are read as lines. Whatever is not one object
    naming a row (a torn last line, a blank, a stray value, a byte-run that
    is not UTF-8) is skipped, never raised: a corrupt claim must be clearable,
    not immortal, and a corrupt queue must never cost the row it was queued
    for. Decoding the head of a line-shaped file stops at the first line's
    end, so trying the document first costs one object, not the file."""
    try:
        text = paths.read_text_retry(path)
    except UnicodeDecodeError:
        # A torn or foreign byte-run: decoded leniently, so the lines around
        # it survive. One plain read -- the retrying one has just succeeded.
        try:
            text = Path(path).read_bytes().decode("utf-8", "replace")
        except OSError:
            return []
    except Exception:                                   # noqa: BLE001
        return []
    out, rest = [], text
    try:
        head, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        head = None
    if isinstance(head, dict) and isinstance(head.get("entries"), list):
        out.extend(e for e in head["entries"] if _is_entry(e))
        rest = text[end:]
    for line in io.StringIO(rest):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if _is_entry(obj):
            out.append(obj)
    return out


def _line(entry) -> str:
    """One entry as one line: compact, keys sorted, ASCII only, so a session id
    holding a newline or a non-ASCII character is still one line that every
    platform decodes the same way."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"


def dump_entries(entries) -> str:
    """The text of a queue holding *entries*, in order."""
    return "".join(_line(e) for e in entries)


# --- the queue write ---------------------------------------------------------

def _append_line(marker_file, line):
    """Append *line* and return the file's size afterwards, or None when the OS
    refused every attempt (fail-open: the row is already safe, and the queue is
    disposable -- `hive embed` re-indexes from the ledger).

    One `open(..., "a")` and one write -- in binary, so the line is the bytes
    `_line` spelled on every platform and a torn tail can be seen: a writer
    killed mid-append leaves a line with no newline, and a line glued to it
    would be lost with it, so the file's last byte is checked and the new
    line starts fresh (the torn one becomes a line its readers skip). The
    check after the write is what keeps the cheap append safe against the
    two things that MOVE this file while a writer holds it open: the
    watcher's claim (`os.replace` to `processing_embed_*`) and a compaction
    or recovery publishing a new file over the name. On POSIX both succeed
    against an open handle, and a line written a microsecond later lands in
    the moved -- or orphaned -- inode; so when the name no longer refers to
    the file just written, the line is written again into whatever holds the
    name now (at worst the watcher sees it twice, and claims are idempotent).
    On Windows the rename is refused while the handle is open (the watcher
    retries it), so the check passes first time; the retry there covers the
    opposite refusal, a `PermissionError` for the instant a publisher holds
    the destination."""
    data = line.encode("utf-8")
    for attempt in range(APPEND_ATTEMPTS):
        try:
            with open(marker_file, "a+b") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
                f.write(data)
                f.flush()
                written = os.fstat(f.fileno())
            if os.path.samestat(os.stat(marker_file), written):
                return written.st_size
        except FileNotFoundError:
            pass                       # moved, and nothing holds the name yet
        except OSError:
            if attempt + 1 < APPEND_ATTEMPTS:
                time.sleep(_APPEND_RETRY_S)
    return None


def emit_pending_embed_marker(project, session_id, segment, seg_line) -> None:
    """Queue `(session_id, segment, seg_line)` for the watcher: one line
    appended, plus the one compaction when the file has outgrown its bound.

    Entries ACCUMULATE: the watcher is the only consumer, and the append path
    never drops what an earlier append queued -- until `QUEUE_MAX_BYTES`, past
    which the oldest rows leave a fast path that `hive embed` still covers.
    *segment* is the basename `events.segment_name(session_id)` actually wrote
    to, carried beside the RAW id because the two differ for a quarantined id
    (`../x` lands in `unsafe-<sha256[:16]>.jsonl`): a watcher that opened
    `f"{session_id}.jsonl"` would walk the exact path the quarantine closed,
    and raise. *seg_line* is 1-BASED -- the count of `b"\\n"` before the write,
    plus one -- and deliberately NOT the mirror's zero-based `seg_line` column
    (`pgmirror.scan_segments`); a reader that mixes the two is off by one.

    `queued_at` is the UTC shape `events.now_iso` emits, spelled here because
    this module must not import `events`. `paths.state_dir` mkdirs, which is
    why it is reached only here, with something to write: a telemetry-only
    namespace has no `state/`. Nothing here raises for a queue the OS refuses
    (`_append_line`); a marker must never cost the row."""
    marker_file = paths.state_dir(project) / MARKER_NAME
    size = _append_line(marker_file, _line({
        "session_id": session_id,
        "seg_line": seg_line,
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "segment": segment,
    }))
    if size is not None and size > QUEUE_MAX_BYTES:
        _compact(marker_file)


def _compact(marker_file) -> None:
    """The one rewrite: each row once, then the newest entries that fit in HALF
    the bound, published atomically. Half, so the next rewrite is a half-bound
    of appends away rather than the very next append. Reached through
    `append_event` this runs inside the append lock, so no other appender is
    writing the file it replaces; the watcher's claim can still take the file
    in between, and a row then in both the claim and the compacted queue is
    embedded twice at worst -- claims are idempotent."""
    lines = [_line(e) for e in dedupe_entries(load_entries(marker_file))]
    budget, kept, total = QUEUE_MAX_BYTES // 2, [], 0
    for line in reversed(lines):                 # ASCII: len is bytes
        if total + len(line) > budget:
            break
        kept.append(line)
        total += len(line)
    kept.reverse()
    if len(kept) < len(lines):
        _log.info("embedqueue: %s past %d bytes; the oldest %d of %d rows "
                  "leave the queue (hive embed re-indexes from the ledger)",
                  marker_file.name, QUEUE_MAX_BYTES, len(lines) - len(kept),
                  len(lines))
    paths.write_text_atomic(marker_file, "".join(kept))


def requeue_entries(project, entries) -> int:
    """Put *entries* BACK at the head of the queue, once each, keeping whatever
    is already there. Returns how many of them were new to it.

    The watcher's startup recovery is the only caller (`watcher.
    recover_stale_markers`): a claim abandoned by a killed process holds
    entries that left this file when it was claimed, and appends that landed
    while it held them are in this file now. Merged, therefore, and never
    written over -- an overwrite in either direction loses rows that nothing
    else records. At the HEAD because they are the older work: the queue is
    drained in order and the rows that waited through a crash should not wait
    through the next batch as well. Each row ONCE: a claim whose unlink was
    refused is recovered again on the next pass, and two watchers' claims can
    name one row; a queue holding two copies handed the watcher a batch
    chromadb refused (DuplicateIDError), which left the claim on disk to be
    recovered, and refused, again.

    Not bounded here: the bound is the append path's, and a namespace nothing
    drains has no claims to recover. Recovery is the path that must not lose
    work; it merges everything and the next append compacts.

    Entries keep their ORIGINAL `queued_at`. Re-stamping them would erase the
    evidence of how long the work has actually been outstanding, which is the
    one thing an operator reading this file wants to know. Published through
    `paths.write_text_atomic` -- a private temp name and `os.replace` with the
    NTFS retry -- so a reader sees the old queue or the new one, never a torn
    file; the caller holds the append lock, so no appender is writing the file
    being replaced."""
    entries = [e for e in entries if isinstance(e, dict)]
    if not entries:
        return 0
    marker_file = paths.state_dir(project) / MARKER_NAME
    current = load_entries(marker_file)
    have = {entry_key(e) for e in current}
    paths.write_text_atomic(marker_file,
                            dump_entries(dedupe_entries(entries + current)))
    return sum(1 for e in dedupe_entries(entries) if entry_key(e) not in have)
