# SPDX-License-Identifier: Apache-2.0
"""The `pending_embed.json` marker: the hand-off from the append path to the
background vector watcher.

`events.append_event` writes one row to one segment and returns. Something else
has to notice that a new decision or caveat exists and embed it, and it has to
find that row again WITHOUT re-scanning the ledger -- `post_tool_use` appends
on every tool call, and a watcher that re-read every segment to find the new
rows would be the unbounded scan CLAUDE.md forbids, run continuously. So every
SEMANTIC append queues one entry into `<namespace>/state/pending_embed.json`
and the watcher consumes the queue. This module owns the two halves of that
entry the append path has to compute: WHERE the row is (`count_newlines`, the
physical line) and the queue write itself (`emit_pending_embed_marker`).

Its own module rather than more of `events.py`, for two reasons. `events.py`
is at the file-size cap's door, and the marker is a SECONDARY projection: the
row is the primary record, and the code that runs between a row being written
and the lock being released should be as small as the code that writes it.
It imports `paths` only -- never `events` -- so `events` can bind
`emit_pending_embed_marker` by identity under its own private name and the
watcher can import this without pulling the type registry along.
"""
import json
import time

from . import paths

# The binary read size for the newline count. One mebibyte keeps a live segment
# of tens of megabytes off the heap while costing a handful of reads; newlines
# are single bytes, so no chunk boundary can split one.
COUNT_CHUNK = 1 << 20

MARKER_NAME = "pending_embed.json"


def count_newlines(path) -> int:
    """How many `b"\\n"` bytes *path* holds; 0 when it does not exist yet.

    Binary, because `append_event` opens the segment in TEXT mode and Windows
    therefore writes CRLF: one `b"\\n"` per row is still one row, and counting a
    single byte is not a split -- `tests/test_architecture_drift.py` allows
    exactly one function to decide where a ledger row ends, and a `.split` or
    `.splitlines()` here would be a third reader making that decision. Chunked
    rather than `readlines()` (the PDF's shape) because this runs inside the
    append lock and a live segment can run to tens of megabytes.

    A missing file is 0 lines. Any other failure to read is left to propagate:
    it is the failure `open(seg, "a")` is about to hit on the same file a line
    later, and answering it with 0 would file the row under line 1 -- a wrong
    line number the watcher would then embed the wrong row from, which is worse
    than the append failing where the writer can see it."""
    total = 0
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(COUNT_CHUNK), b""):
                total += chunk.count(b"\n")
    except FileNotFoundError:
        return 0
    return total


def _load_queue(marker_file) -> dict:
    """The queue on disk, or a fresh one when the file is missing, unreadable,
    unparseable, or not the documented shape.

    Read through `paths.read_text_retry`: the watcher publishes over this file
    with `os.replace`, and for that instant Windows refuses a plain `open()` of
    the destination. REPLACED rather than raised on any defect -- this runs on
    the hook path, inside the append lock, and a corrupt sidecar must never
    cost the row it was queued for. The PDF guards only the parse exception;
    `current_queue["entries"].append` on a list or a string is a second crash
    that guard does not cover, so the shape is checked as well: one object
    holding a list under `entries`, and nothing else counts."""
    try:
        doc = json.loads(paths.read_text_retry(marker_file))
    except Exception:                                   # noqa: BLE001
        return {"entries": []}
    if isinstance(doc, dict) and isinstance(doc.get("entries"), list):
        return doc
    return {"entries": []}


def emit_pending_embed_marker(project, session_id, segment, seg_line) -> None:
    """Queue `(session_id, segment, seg_line)` for the watcher, atomically.

    Entries ACCUMULATE: the watcher is the only consumer and the only
    truncator, and the append path never drops what an earlier append queued.
    *segment* is the basename `events.segment_name(session_id)` actually wrote
    to, carried beside the RAW id because the two differ for a quarantined id
    (`../x` lands in `unsafe-<sha256[:16]>.jsonl`): a watcher that opened
    `f"{session_id}.jsonl"` would walk the exact path the quarantine closed,
    and raise. *seg_line* is 1-BASED -- the count of `b"\\n"` before the write,
    plus one -- and deliberately NOT the mirror's zero-based `seg_line` column
    (`pgmirror.scan_segments`); a reader that mixes the two is off by one.

    `queued_at` is the UTC shape `events.now_iso` emits, spelled here because
    this module must not import `events`. Published through
    `paths.write_text_atomic` -- a private temp name and `os.replace` with the
    NTFS retry -- so a reader sees either the old queue or the new one, never
    a torn file. `paths.state_dir` mkdirs, which is why it is reached only
    here, with something to write: a telemetry-only namespace has no `state/`."""
    marker_file = paths.state_dir(project) / MARKER_NAME
    queue = _load_queue(marker_file)
    queue["entries"].append({
        "session_id": session_id,
        "seg_line": seg_line,
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "segment": segment,
    })
    paths.write_text_atomic(marker_file, json.dumps(queue, indent=2))
