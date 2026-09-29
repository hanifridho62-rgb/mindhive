# SPDX-License-Identifier: Apache-2.0
"""The background embedding watcher (blueprint P1h; PDF BAB 4.4-4.6; MEB
VEC-02): the consumer half of `embedqueue`.

`events.append_event` writes one row and returns. For a SEMANTIC type it also
queues `(session_id, segment, seg_line)` into `state/pending_embed.json` --
three integers and two strings, written inside the append lock, so the writer
pays almost nothing. This module is what later turns those coordinates back
into rows and hands them to the vector store.

**There is no 24/7 daemon.** MEB 1.4 removed it: the marker is emitted
in-process, and the embedding happens in one of three places, all of which a
person can point at --

* a daemon THREAD that lives exactly as long as `hive dashboard` / `hive ui`,
* `hive watcher run --once`, one cycle and out,
* `hive embed`, which walks the whole eligible set rather than the queue.

Five things here are load-bearing.

**The marker lifecycle is a rename (PDF 4.4.1).** `pending_embed.json` is
renamed to `processing_embed_<ts>.json` before anything is read from it. The
rename is atomic, so two watchers cannot take the same queue -- the loser
finds nothing to rename rather than embedding the same rows twice -- and a
process that dies mid-embed leaves its claim on disk, where the next boot's
recovery finds it. The claim is unlinked only after the vectors are written.

**Renaming on Windows loses to things that are not bugs (PDF 4.4.2).** The
search indexer and antimalware both open files they are scanning, and
`os.replace` answers `PermissionError` for a file that is perfectly intact.
`atomic_rename_resilient` walks the blueprint's backoff ladder rather than
treating the first denial as failure.

**A queued row is read back through the SEGMENT the marker names, not through
the id (RP I.1).** `../x` and `CON` are quarantined to
`unsafe-<sha256[:16]>.jsonl`; a reader that rebuilt `f"{session_id}.jsonl"`
would walk the exact path the quarantine closed. The basename `append_event`
actually wrote to rides in the queue entry for precisely this reason.

**The chain coordinates are per SEGMENT.** `read_physical_record` folds the
segment it opens, from `GENESIS_PREV_HASH`, and stops at the line it wants.
That is the same coordinate `chain._fold` computes when a single namespace
holds a single segment, and the same one the migration engine (P1i) carries
across segments in name order to reproduce the global head. It is NOT a
whole-ledger fold: that would be the unbounded scan CLAUDE.md forbids, run on
every append.

**Without the vector extras, nothing is claimed.** `chromadb` and
`sentence_transformers` are optional (`pip install -e .[vectors]`), and a
watcher that claimed a queue it could not embed used to append
`vector_sync_started`, fail on the import inside `embed_batch`, and leave the
claim on disk -- every two seconds, in every namespace with a pending row.
`run_once` asks `vectors.missing_extras()` first, leaves the queue where it is
(bounded by `embedqueue.QUEUE_MAX_BYTES`) and says why once. Claims are
idempotent besides: a row a claim names twice is embedded once.
"""
import os
import sys
import threading
import time
from pathlib import Path

from . import embedqueue, events, jobs, paths
from .core.locks import is_stale
from .core.models import (GENESIS_PREV_HASH, EventRecord, compute_chain_hash)

__all__ = ["PROCESSING_PREFIX", "PROCESSING_GLOB", "RENAME_BACKOFF_S",
           "RENAME_ATTEMPTS", "STALE_MARKER_S", "POLL_INTERVAL_S", "POLL_ENV",
           "THREAD_NAME", "STOP_JOIN_S", "PROGRESS_PREFIX", "SYNC_STARTED",
           "SYNC_COMPLETED", "SYNC_PAYLOAD_KEYS", "validate_payload",
           "BackgroundEmbeddingWatcher",
           "atomic_rename_resilient", "read_physical_record",
           "recover_stale_markers", "run_once", "status"]

#: A claimed queue. The timestamp makes the name unique per claim; the glob is
#: what recovery scans for.
PROCESSING_PREFIX = "processing_embed_"
PROCESSING_GLOB = PROCESSING_PREFIX + "*.json"

#: PDF 4.4.2, verbatim: five attempts, four waits. The last failure has
#: nothing left to wait for, and sleeping after it is 0.32 s of latency
#: nobody spends.
RENAME_BACKOFF_S = (0.02, 0.04, 0.08, 0.16, 0.32)
RENAME_ATTEMPTS = 5

#: How old a claim has to be before its owner counts as dead (MEB VEC-02).
#: Ten minutes, because a cold model load plus a large first index genuinely
#: takes minutes and stealing the queue out from under it would re-embed
#: everything it had already done.
STALE_MARKER_S = 600.0

#: How often the daemon thread looks. Two seconds is invisible to a person
#: and costs one `Path.exists` per tick.
POLL_INTERVAL_S = 2.0
POLL_ENV = "MINDHIVE_WATCHER_POLL_S"

THREAD_NAME = "MindHive-Watcher"
STOP_JOIN_S = 3.0

#: What the progress frames are keyed on. Owned by `jobs`, which PARSES them
#: for the dashboard's bar: one spelling, in the module that has to recognise
#: it, rather than two that can drift.
PROGRESS_PREFIX = jobs.PROGRESS_PREFIX

#: Deliberately NOT in `events.SEMANTIC_TYPES`. A sync row that queued its own
#: embed would be a watcher that never stops.
SYNC_STARTED = "vector_sync_started"
SYNC_COMPLETED = "vector_sync_completed"

#: Both ends carry the same three keys. Identical on purpose: the contract
#: table cannot pass the type down to `validate_payload`, and one module
#: telling two types apart by payload SHAPE is how each ends up accepting the
#: other's rows (the lesson `worksessions`/`initiatives` records). These two
#: differ by name, which is all they need to differ by.
SYNC_PAYLOAD_KEYS = ("marker", "count", "duration_s")

_log = __import__("logging").getLogger("mindhive.watcher")


def validate_payload(payload) -> None:
    """The registry contract for both vector-sync types (DATA_MODEL section
    6.2), enforced at `hive append` and `POST /api/append`.

    `marker` has to NAME a claim, not merely be a string: these rows exist so
    an operator can line a sync up against the marker that caused it, and a
    row naming a file that never existed is worse than no row. `count` and
    `duration_s` are checked for sign because a negative count would render
    as a percentage the dashboard cannot draw."""
    if not isinstance(payload, dict):
        raise ValueError("a vector-sync payload must be an object")
    missing = [k for k in SYNC_PAYLOAD_KEYS if k not in payload]
    if missing:
        raise ValueError(
            f"a vector-sync payload needs {', '.join(SYNC_PAYLOAD_KEYS)}; "
            f"missing: {', '.join(missing)}")
    marker = payload["marker"]
    if not isinstance(marker, str) or not marker.startswith(PROCESSING_PREFIX):
        raise ValueError(
            f"marker must name a {PROCESSING_PREFIX}*.json claim: {marker!r}")
    count = payload["count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"count must be a whole number of rows: {count!r}")
    seconds = payload["duration_s"]
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or seconds < 0):
        raise ValueError(
            f"duration_s must be a non-negative number of seconds: {seconds!r}")


# --- paths -------------------------------------------------------------------

def _state_dir(project=None, make=False) -> Path:
    """The namespace's `state/`. `paths.state_dir` MKDIRS, which is right for
    a writer and wrong for `status` -- probing a store would mint the
    namespace, and `paths.is_freshly_minted` would then answer "not fresh" to
    the very read it exists to warn about."""
    if make:
        return paths.state_dir(project)
    return paths.project_dir(project) / "state"


def project_slug(project=None) -> str:
    """The collection slug for a namespace: the namespace directory's own
    name, so a quarantined project indexes under the quarantined name rather
    than under something only this module believes in."""
    return paths.project_dir(project).name


# --- atomic rename (PDF 4.4.2) -----------------------------------------------

def atomic_rename_resilient(src_path, dst_path, max_attempts=RENAME_ATTEMPTS,
                            *, sleep=time.sleep, strict=False) -> bool:
    """Rename *src_path* to *dst_path*, riding out the Windows filesystem.

    `PermissionError` (WinError 5) and `OSError` 32 (sharing violation) are
    what the search indexer and antimalware produce against a file that is
    intact; they clear on their own in tens of milliseconds. Returns False
    once the ladder is exhausted rather than raising, because the caller is
    usually a daemon loop and a transient scan must not end it -- *strict*
    re-raises instead, for a one-shot `hive watcher run --once` whose operator
    wants the reason.

    A MISSING source is not contention and is not retried: another watcher
    claimed the queue first, and waiting 0.6 s to be told so again is pure
    latency."""
    last = None
    for attempt in range(max_attempts):
        try:
            os.replace(src_path, dst_path)
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            last = e
            if attempt + 1 >= max_attempts:
                _log.error("watcher: atomic rename %s -> %s failed: %s",
                           src_path, dst_path, e)
                if strict:
                    raise
                return False
            sleep(RENAME_BACKOFF_S[min(attempt, len(RENAME_BACKOFF_S) - 1)])
    if strict and last is not None:                        # pragma: no cover
        raise last
    return False


# --- reading a queued row back (RP I.1) --------------------------------------

def read_physical_record(session_id, seg_line, segment=None, project=None):
    """The `EventRecord` at 1-based *seg_line*, or None.

    *segment* is the basename the append actually wrote to, carried in the
    queue entry; it is preferred over `events.segment_name(session_id)` so
    that a change to the quarantine rule cannot orphan queued work. Lines are
    taken from `events._segment_parts`, the one function allowed to decide
    where a ledger row ends (`tests/test_architecture_drift.py`).

    Total by construction: a missing segment, a line past the end, a torn row
    and an unreadable file are all None. The queue can outlive a `hive gc`,
    and a watcher that raised on a vanished row would stop embedding the rows
    that are still there."""
    try:
        line = int(seg_line)
    except (TypeError, ValueError):
        return None
    if line < 1:
        return None
    name = segment or events.segment_name(session_id)
    seg = paths.events_dir(project) / name
    if not seg.exists():
        return None
    try:
        parts = events._segment_parts(seg)
    except OSError:
        return None
    if line > len(parts) - 1:              # the tail after the last \n is partial
        return None
    prev = GENESIS_PREV_HASH
    wanted = None
    for index, raw in enumerate(parts[:line]):
        stripped = raw.strip()
        if not stripped:
            continue
        ev = events._row_to_event(stripped)
        if ev is None:
            continue                       # torn row: reads stay total
        canonical = events.canonical_json(events.to_dict(ev))
        chain_hash = compute_chain_hash(prev, canonical)
        if index == line - 1:
            wanted = EventRecord(
                event_id=f"{ev.session_id}:{line}", session_id=ev.session_id,
                seg_line=line, timestamp=ev.ts or "", event_type=ev.type,
                payload=ev.payload if isinstance(ev.payload, dict) else {},
                tags=list(ev.tags or []), prev_hash=prev, chain_hash=chain_hash)
        prev = chain_hash
    return wanted


# --- the queue file ----------------------------------------------------------
#
# Its readers live in `embedqueue` (`load_entries`, `entry_key`,
# `dedupe_entries`): one module, so the producer and the consumer cannot
# disagree about the file's shape -- one JSON object per line, and the
# `{"entries": [...]}` document it used to be, which a claim an older watcher
# took may still hold.

def _reconcile(project, entries) -> list:
    """Drop the entries whose vectors already exist (PDF 4.5 step 3), so a
    crash two rows from the end re-embeds two rows and not two hundred.

    Skipped entirely when the namespace has no `chroma/` directory: there is
    nothing to reconcile against, and opening a client to be told so would
    import chromadb and mint a store on a machine that never embedded."""
    if not entries or not (paths.project_dir(project) / "chroma").exists():
        return entries
    try:
        from . import vectors
        have = vectors.VectorStoreManager.get_instance(project).already_indexed(
            project_slug(project), [embedqueue.entry_key(e) for e in entries])
    except Exception as e:                                 # noqa: BLE001
        _log.warning("watcher: could not reconcile against the index: %s", e)
        return entries
    return [e for e in entries if embedqueue.entry_key(e) not in have]


def status(project=None) -> dict:
    """How much work is waiting and how much is claimed. Never mkdirs."""
    sdir = _state_dir(project)
    pending = embedqueue.load_entries(sdir / embedqueue.MARKER_NAME)
    markers = sorted(p.name for p in sdir.glob(PROCESSING_GLOB)) \
        if sdir.exists() else []
    return {"pending": len(pending), "processing": len(markers),
            "markers": markers}


def recover_stale_markers(project=None, stale_s=STALE_MARKER_S) -> int:
    """Return every ABANDONED claim to the pending queue (PDF 4.5).

    A claim younger than *stale_s* belongs to a watcher that is still working
    and is left alone. An older one is read, reconciled against the index,
    merged back into `pending_embed.json` and unlinked -- merged, not written
    over, because appends that landed while the dead watcher held its claim
    are in that file and must survive the recovery. An unreadable claim is
    unlinked with nothing to requeue: it would otherwise be retried forever.

    Returns how many claims it cleared."""
    sdir = _state_dir(project)
    if not sdir.exists():
        return 0
    cleared = 0
    for marker in sorted(sdir.glob(PROCESSING_GLOB)):
        if not is_stale(marker, stale_s):
            continue
        entries = _reconcile(project, embedqueue.load_entries(marker))
        if entries:
            # Under the APPEND lock, which every appender holds while it
            # writes the queue: the merge reads the queue and publishes over
            # it, and a line appended between the two would be lost to the
            # publish. Recovery is rare (boot, `hive watcher recover`) and
            # the merge is milliseconds; the lock fails open past its wait.
            with events._append_lock(project):
                embedqueue.requeue_entries(project, entries)
        try:
            marker.unlink()
        except OSError as e:                               # noqa: BLE001
            _log.warning("watcher: could not clear stale marker %s: %s",
                         marker.name, e)
            continue
        cleared += 1
    return cleared


# --- the watcher -------------------------------------------------------------

class BackgroundEmbeddingWatcher:
    """One namespace's marker loop.

    *out* is where progress frames go. `None` means silence -- the CLI passes
    `sys.stdout` and the dashboard job passes its own emitter, but a watcher
    constructed by a test writes nowhere unless asked."""

    def __init__(self, project=None, poll_interval_s=None, *, out=None):
        self.project = project
        self.poll_interval_s = self._interval(poll_interval_s)
        self.out = out
        self._stop = threading.Event()
        self._thread = None
        self._told_missing_extras = False

    @staticmethod
    def _interval(given) -> float:
        """An explicit argument beats the environment beats the default; a
        value that is not a positive number is not an instruction, it is a
        typo, and falling back beats a daemon that spins."""
        if given is not None:
            return float(given)
        raw = os.environ.get(POLL_ENV)
        if raw:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return POLL_INTERVAL_S
            if value > 0:
                return value
        return POLL_INTERVAL_S

    @property
    def thread(self):
        return self._thread

    # --- the marker lifecycle ------------------------------------------------

    def claim(self):
        """Rename the pending queue to a claim of our own, or None.

        The rename IS the mutual exclusion; nothing else in this module needs
        a lock. `time.time_ns` names the claim because two claims inside one
        millisecond are ordinary under a test."""
        sdir = _state_dir(self.project)
        pending = sdir / embedqueue.MARKER_NAME
        if not pending.exists():
            return None
        target = _state_dir(self.project, make=True) / (
            f"{PROCESSING_PREFIX}{time.time_ns()}.json")
        if not atomic_rename_resilient(pending, target):
            return None
        return target

    def process_marker(self, marker) -> dict:
        """Embed everything one claim names, each row once, then unlink it.

        `{"marker", "count", "duration_s"}`, plus `"error"` when the embed
        raised -- in which case the claim is LEFT ON DISK. Unlinking a failed
        claim would drop the rows silently: the marker is the only record that
        they were never indexed, and the next boot's recovery is what gets
        them back. Plus `"skipped"`, with the reason, when the vector extras
        are missing: then nothing is read, appended or unlinked -- the claim
        waits, intact, for the install that can drain it, and no sync row is
        written for a sync that never happened."""
        marker = Path(marker)
        reason = self._missing_extras()
        if reason is not None:
            return {"marker": marker.name, "count": 0, "duration_s": 0.0,
                    "skipped": reason}
        started = time.monotonic()
        # Each row once, whatever the claim repeats (a claim recovered twice, a
        # line the append path wrote again): chromadb refuses an `add` that
        # repeats an id, which failed the whole batch and left the claim to be
        # recovered -- and refused -- again.
        entries = embedqueue.dedupe_entries(embedqueue.load_entries(marker))
        records = [r for r in (read_physical_record(
            e.get("session_id"), e.get("seg_line"), e.get("segment"),
            self.project) for e in entries) if r is not None]
        error = None
        if records:
            # `duration_s` on the START row is what reading the rows back
            # cost: the claim names physical lines, and turning them into
            # records folds each segment. Real work, and the only place it is
            # ever measured.
            events.append_event(SYNC_STARTED,
                                {"marker": marker.name, "count": len(records),
                                 "duration_s": round(time.monotonic() - started,
                                                     3)},
                                project=self.project)
            try:
                self._embed(records)
            except Exception as e:                         # noqa: BLE001
                error = f"{type(e).__name__}: {e}"
                _log.error("watcher: embedding %s failed: %s", marker.name, e)
        duration = round(time.monotonic() - started, 3)
        if error is not None:
            return {"marker": marker.name, "count": 0, "duration_s": duration,
                    "error": error}
        if records:
            events.append_event(SYNC_COMPLETED,
                                {"marker": marker.name, "count": len(records),
                                 "duration_s": duration}, project=self.project)
        try:
            marker.unlink()
        except OSError as e:                               # noqa: BLE001
            _log.warning("watcher: could not clear %s: %s", marker.name, e)
        return {"marker": marker.name, "count": len(records),
                "duration_s": duration}

    def _embed(self, records) -> None:
        from . import vectors
        vectors.VectorStoreManager.get_instance(self.project).embed_batch(
            project_slug(self.project), records,
            progress_callback=self._progress)
        self._write("\n")

    def _progress(self, done, total) -> None:
        """One terminal line, overwritten (PDF 4.6). `\\r` rather than `\\n`
        because a hundred frames must be a hundred repaints of one line, not a
        hundred lines; `jobs.OutputRing` is what collapses the run for the
        browser, which cannot repaint."""
        pct = round((done / total) * 100, 1) if total else 0.0
        self._write(f"\r{PROGRESS_PREFIX} Menanam: {done}/{total} ({pct}%)")

    def _write(self, text) -> None:
        if self.out is None:
            return
        self.out.write(text)
        flush = getattr(self.out, "flush", None)
        if flush is not None:
            flush()

    # --- cycles --------------------------------------------------------------

    def run_once(self) -> dict:
        """Claim the queue and drain it. `count` 0 when there was nothing.

        Without the vector extras nothing is claimed either: the queue stays
        where it is -- bounded, `embedqueue.QUEUE_MAX_BYTES` -- for the install
        that can drain it, the result carries `skipped` and the reason, and the
        reason is said once per watcher rather than once per tick. It used to
        claim, append `vector_sync_started`, fail on the import inside
        `embed_batch` and leave the claim on disk: every two seconds, in every
        namespace with a pending row, a sync row for a sync that never happened
        and a claim for recovery to chew on. An idle queue asks no question."""
        idle = {"marker": None, "count": 0, "duration_s": 0.0}
        if not (_state_dir(self.project) / embedqueue.MARKER_NAME).exists():
            return idle
        reason = self._missing_extras()
        if reason is not None:
            return {**idle, "skipped": reason}
        marker = self.claim()
        return idle if marker is None else self.process_marker(marker)

    def _missing_extras(self):
        """`vectors.missing_extras()`, said once: to *out* (the CLI's stdout,
        the dashboard job's ring) and to the log, the first time it is asked.
        Asked again every cycle that has work, so an install made while the
        dashboard runs is noticed without a restart."""
        from . import vectors
        reason = vectors.missing_extras()
        if reason is not None and not self._told_missing_extras:
            self._told_missing_extras = True
            _log.warning("watcher: %s", reason)
            self._write(f"{PROGRESS_PREFIX} skipped: {reason}\n")
        return reason

    def recover(self, stale_s=STALE_MARKER_S) -> int:
        return recover_stale_markers(self.project, stale_s)

    def _run_loop(self) -> None:
        """Tick until stopped. Never raises: the thread has nobody to raise
        to, and a daemon that died on one bad marker would stop embedding for
        the life of the dashboard, silently."""
        while not self._stop.wait(self.poll_interval_s):
            try:
                self.run_once()
            except Exception as e:                         # noqa: BLE001
                _log.warning("watcher: cycle failed: %s", e)

    def start(self) -> None:
        """Start the daemon thread, or do nothing if it is already running.
        Daemon, because a watcher must never be the reason a dashboard cannot
        exit; named, so a stack dump says which thread this is."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop,
                                        name=THREAD_NAME, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread and wait up to `STOP_JOIN_S` for it. Bounded: a
        shutdown that can hang is a shutdown someone kills with Task Manager,
        which is how claims are abandoned in the first place."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(STOP_JOIN_S)


def run_once(project=None, out=None) -> dict:
    """One cycle for *project*. What `hive watcher run --once` runs."""
    return BackgroundEmbeddingWatcher(project, out=out).run_once()


def run_forever(project=None, out=None, watcher=None) -> int:
    """Tick until interrupted. What `hive watcher run` runs; returns 0 on a
    clean Ctrl+C, because asking a foreground loop to stop is not a failure."""
    w = watcher if watcher is not None else BackgroundEmbeddingWatcher(
        project, out=out if out is not None else sys.stdout)
    w.start()
    try:
        while w.thread is not None and w.thread.is_alive():
            w.thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        w.stop()
    return 0
