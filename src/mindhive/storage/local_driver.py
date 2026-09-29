# SPDX-License-Identifier: Apache-2.0
"""`LocalEventStore` — the JSONL ledger behind the SPI (blueprint P1i, STO-01).

The ledger stays the primary writer. This driver does not reimplement the
append: it calls `events.append_event`, which owns the lock, the quarantine
rule, the canonical bytes and the watcher's marker. What it adds is the part
`append_event` deliberately does not return -- the chain coordinates -- and a
resumable fold so getting them does not cost a re-hash of the whole session
on every write.

**Finding our own row.** `append_event` returns the `Event`, not the line it
landed on, and it cannot: the line is assigned inside the lock and the lock is
released before the call returns. So the driver folds the rows that appeared
since it last looked and takes the LAST one whose canonical bytes are ours.
Last, not first, because a hook appending concurrently can interleave; last is
the row most recently written, which is the one this call just produced. Two
BYTE-IDENTICAL events in one segment -- same timestamp to the second, same
session, same type, same payload, same tags -- are indistinguishable by
construction, and the driver reports the later line. That is a real limit of
content matching and it is written down rather than papered over; it costs a
`seg_line` that is off by the number of duplicates, never a lost or duplicated
ROW, because the ledger already holds both.

**What the cache saves, precisely.** `_folded` remembers `(rows_seen, head)`
per session, so the SHA-256 chain is resumed rather than recomputed. The
segment is still READ in full on every append -- `records.segment_rows` parses
it -- so the saving is the hashing, not the I/O. A driver that also skipped
the read would have to trust a byte offset, and a byte offset into an
append-only file that another process is writing is the one number this
codebase has already been burned by.
"""
import json
import os
import threading

from .. import events, paths
from ..core.locks import LockPolicy, file_lock
from ..core.models import GENESIS_PREV_HASH
from . import records
from .interface import EventStore

__all__ = ["LocalEventStore", "STATE_FILE", "STATE_LOCK_NAME"]

#: Where the resumable fold is persisted, beside the namespace's other
#: sidecars. Under `project_dir`, NEVER `store_home()`: the store root's files
#: are a declared set (`paths.STORE_ROOT_FILES`) and a name that collides with
#: one of them makes a project directory out of a file.
STATE_FILE = "storage_state.json"
STATE_LOCK_NAME = ".storage.lock"

_STATE_LOCK_POLICY = LockPolicy(timeout_s=5.0, stale_s=60.0)


def _last_match(fold, canonical: str):
    """The LAST record in *fold* whose row is *canonical*, or None.

    Last, not first, because a hook appending concurrently can interleave and
    the row this call just produced is the most recently written one."""
    for rec in reversed(fold):
        if events.canonical_json(records.record_row(rec)) == canonical:
            return rec
    return None


class LocalEventStore(EventStore):
    """The file ledger of one namespace, as an `EventStore`.

    `fsync` defaults ON here and OFF in `events.append_event`, and the split
    is deliberate: the hook path appends on every tool call and must not pay a
    disk flush per event, while a caller that reached for the durable SPI
    asked for durability by name.

    `strict_session` likewise defaults ON. `append_event`'s default is the
    fail-open quarantine, which is right for a hook that must never lose a row
    to a bad id; a driver whose caller can handle an exception should refuse
    the id instead of silently renaming the session."""

    def __init__(self, project=None, *, fsync: bool = True,
                 strict_session: bool = True):
        self.project = project
        self.fsync = fsync
        self.strict_session = strict_session
        self._folded: dict = {}
        self._guard = threading.Lock()
        self._loaded = False

    # --- paths ------------------------------------------------------------

    def segment_path(self, session_id):
        """The physical segment *session_id* WRITES to, quarantine rule
        included -- the name `append_event` used for it.

        `paths.events_dir` MKDIRS, which is correct here -- a store asked for
        a segment path is about to read or write it -- and is the reason this
        is not spelled inline at three call sites with one of them forgetting."""
        return paths.events_dir(self.project) / events.segment_name(session_id)

    def _listed_path(self, session_id):
        """The segment a name from `list_sessions` READS from. A quarantined
        session is listed by its `unsafe-<hash>` stem, and `segment_name`
        would quarantine that stem again, onto a file nothing wrote --
        `records.listed_segment_name` takes it verbatim instead. Reads only:
        `append` still goes through `append_event`, whose reservation keeps
        a caller from writing into a quarantine slot by naming it."""
        return (paths.events_dir(self.project)
                / records.listed_segment_name(session_id))

    def _state_path(self):
        return paths.project_dir(self.project) / STATE_FILE

    # --- the three primitives ---------------------------------------------

    def append(self, session_id, event_type, payload, tags=None):
        ev = events.append_event(event_type, payload, session_id=session_id,
                                 tags=tags, project=self.project,
                                 strict_session=self.strict_session,
                                 fsync=self.fsync)
        mine = events.canonical_json(events.to_dict(ev))
        seg = self.segment_path(ev.session_id)
        with self._guard:
            self._load_state()
            seen, head = self._folded.get(ev.session_id,
                                          (0, GENESIS_PREV_HASH))
            rows = records.segment_rows(seg)
            if len(rows) < seen:
                seen, head = 0, GENESIS_PREV_HASH   # shrank; see below
            fresh = records.fold_rows(rows[seen:], head)
            found = _last_match(fresh, mine)
            if found is None and seen:
                # The row we JUST wrote is not among the rows the cache says
                # are new, so the cache is describing bytes that are no longer
                # there -- an append-only ledger does not shrink, but a hand
                # edit or a restored backup does. A row count cannot catch
                # this on its own: a segment truncated by exactly as many rows
                # as we then appended has the count the cache expects. Absence
                # of our own row is the check that does not depend on
                # arithmetic, so fold the whole segment and look again rather
                # than chain onto a head attesting to a fiction.
                fresh = records.fold_rows(rows, GENESIS_PREV_HASH)
                found = _last_match(fresh, mine)
            if fresh:
                self._folded[ev.session_id] = (len(rows), fresh[-1].chain_hash)
        if found is None:
            # Unreachable while the row we just wrote is still on disk. If it
            # is not, saying so beats returning a record for someone else's
            # line.
            raise RuntimeError(
                f"the row appended to {seg.name} was not found in it; the "
                f"segment was rewritten underneath this append")
        return found

    def read_stream(self, session_id, from_line: int = 1):
        """Fold from GENESIS every time, and yield from *from_line*.

        No cache on this path, on purpose: each record carries its OWN
        `prev_hash`, so a reader starting at line 400 still needs the 399
        hashes before it. A cache could supply the head at 400 but not the
        chain through it, and half a fold is the shape that reads as an answer
        while being one."""
        seg = self._listed_path(session_id)
        if not seg.exists():
            return
        for rec in records.fold_rows(records.segment_rows(seg)):
            if rec.seg_line >= from_line:
                yield rec

    def list_sessions(self) -> list:
        d = paths.events_dir(self.project)
        if not d.exists():
            return []
        return sorted(p.name[:-len(".jsonl")] for p in d.glob("*.jsonl"))

    # --- the resumable fold, persisted ------------------------------------

    def _load_state(self) -> None:
        """Seed the fold cache from disk, once. Caller holds `_guard`.

        Any failure is silent and total: a missing, unreadable or malformed
        sidecar means "fold from genesis", which is slower and correct. A
        projection that can make the driver refuse to work is worse than no
        projection."""
        if self._loaded:
            return
        self._loaded = True
        try:
            doc = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(doc, dict):
            return
        for stem, entry in (doc.get("sessions") or {}).items():
            try:
                lines, head = int(entry["lines"]), str(entry["head"])
            except (TypeError, ValueError, KeyError):
                continue
            if lines >= 0 and len(head) == 64:
                self._folded[stem] = (lines, head)

    def sync_state(self) -> dict:
        """Persist the fold cache, atomically, under the namespace's lock.

        Atomic because a half-written sidecar read back as a head would chain
        real rows onto a truncated hash; locked because two processes both
        holding a `LocalEventStore` is the ordinary case (`hive dashboard` and
        a CLI), and the loser of a plain overwrite would silently drop the
        other's progress."""
        with self._guard:
            self._load_state()
            doc = {"version": 1,
                   "sessions": {stem: {"lines": lines, "head": head}
                                for stem, (lines, head) in
                                sorted(self._folded.items())}}
        target = self._state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        lock = paths.project_dir(self.project) / STATE_LOCK_NAME
        with file_lock(lock, _STATE_LOCK_POLICY, name=STATE_LOCK_NAME):
            tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(doc, sort_keys=True,
                                      separators=(",", ":")),
                           encoding="utf-8")
            os.replace(tmp, target)
        return {"driver": type(self).__name__,
                "sessions": len(doc["sessions"]),
                "persisted": True,
                "path": str(target)}
