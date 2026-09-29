# SPDX-License-Identifier: Apache-2.0
"""Event-replay migration, hash chain verified (blueprint P1i, MEB MIG-01).

Replay, never copy. The ledger's authority is its hash chain, and a migration
that moved bytes without re-folding them would carry a corrupted row into the
destination with the same confidence as a good one. So every record is verified
against the chain BEFORE it is offered to the destination, and a break stops
the session it is in.

**Three properties this is built around.**

*Idempotent by construction.* The destination's insert is
`ON CONFLICT ... DO NOTHING`, and a session the mirror already holds is skipped
by arithmetic (`max_seg_line`) rather than by a per-row probe. A second run is
therefore one SELECT per session and no writes at all -- which is the number
worth reporting, because a run that silently re-wrote everything would look
identical without it.

*Chunked commits, and why a partial commit is safe.* A session is committed in
chunks of `batch` records instead of one transaction per session. An unbounded
transaction over a multi-hundred-thousand-row replay is a real operational
hazard, and the usual reason to accept it -- all-or-nothing -- does not apply
here: every insert is idempotent, so a run that stopped after chunk 3 leaves a
prefix that a re-run skips and continues from. What a break DOES roll back is
the in-flight chunk, so no chunk is ever half-written.

*The verifier is per session, because that is the chain the reader produces.*
`LocalEventStore.read_stream` folds each segment from the genesis hash, so each
session's records form their own chain. The NAMESPACE chain that `chain.verify`
attests to is a different fold -- one head carried across segments in name
order -- and `HashChainVerifier` computes that one too when it is fed rows in
that order. Both are the same arithmetic asked a different question, and
`tests/test_storage_migration.py` pins the second against `chain.write_chain`.

**What the verifier does NOT prove, stated plainly.** A fold COMPUTES both
hashes from the bytes it just read, so a stream that came straight out of
`records.fold_rows` is self-consistent by construction and this verifier cannot
fail on it. What it catches is a stream that is not a single contiguous chain:
records reordered, a record skipped or repeated, two sessions concatenated, or
a destination-side record whose hashes were computed over different material.
Detecting a ledger EDITED on disk is a different question with a different
answer -- `chain.verify` compares the fold against the stored checkpoint -- and
`hive db migrate` runs that first and refuses a namespace that fails it. Two
checks, in the two places each can actually be made.
"""
import time
from dataclasses import dataclass

from .. import events
from ..core.models import GENESIS_PREV_HASH, compute_chain_hash
from . import records
from .interface import DataIntegrityError
from .postgres_driver import NO_ROWS

__all__ = ["MigrationReport", "HashChainVerifier",
           "EventReplayMigrationEngine"]


@dataclass(frozen=True)
class MigrationReport:
    """What one replay did. Frozen: a report that a caller could edit is a
    report nobody can cite."""

    sessions: int = 0
    sessions_quarantined: int = 0
    events_read: int = 0
    events_inserted: int = 0
    events_skipped: int = 0
    chain_ok: bool = True
    duration_s: float = 0.0
    dry_run: bool = False
    error: str | None = None

    def as_dict(self) -> dict:
        """The shape `hive db migrate --json` prints. Spelled out rather than
        `asdict`, so adding a private field does not silently change a
        documented output."""
        return {"sessions": self.sessions,
                "sessions_quarantined": self.sessions_quarantined,
                "events_read": self.events_read,
                "events_inserted": self.events_inserted,
                "events_skipped": self.events_skipped,
                "chain_ok": self.chain_ok,
                "duration_s": self.duration_s,
                "dry_run": self.dry_run,
                "error": self.error}


class HashChainVerifier:
    """Walks a chain one record at a time and refuses the first break.

    Stateful on purpose: the head after record N is the only thing record N+1
    can be checked against, and a stateless helper would make every caller
    carry it -- which is how a caller ends up carrying the WRONG one."""

    def __init__(self, prev_hash: str = GENESIS_PREV_HASH):
        self.head = prev_hash
        self.checked = 0

    def verify_next(self, record) -> str:
        """Accept *record* and return the new head, or raise.

        Two checks, and both are needed. The first says the record points at
        the chain we are on; the second says its own hash is the one its bytes
        produce. Either alone passes a whole class of forgery: a record with a
        correct `prev_hash` and an edited payload fails only the second, and a
        correctly self-hashed record spliced from another chain fails only the
        first."""
        if record.prev_hash != self.head:
            raise DataIntegrityError(
                f"rantai putus di {record.event_id}: prev_hash "
                f"{_short(record.prev_hash)} tidak cocok dengan kepala rantai "
                f"{_short(self.head)}")
        expected = compute_chain_hash(
            self.head, events.canonical_json(records.record_row(record)))
        if record.chain_hash != expected:
            raise DataIntegrityError(
                f"rantai putus di {record.event_id}: chain_hash tersimpan "
                f"{_short(record.chain_hash)} bukan {_short(expected)}, "
                f"yang dihasilkan baris kanoniknya sendiri")
        self.head = record.chain_hash
        self.checked += 1
        return self.head


def _short(digest) -> str:
    text = str(digest or "")
    return (text[:12] + "…") if len(text) > 12 else text


@dataclass
class _Counts:
    """One chunk's or one session's tally, staged before it is believed."""

    read: int = 0
    inserted: int = 0
    skipped: int = 0
    error: str | None = None

    def add(self, other: "_Counts") -> None:
        self.read += other.read
        self.inserted += other.inserted
        self.skipped += other.skipped


class EventReplayMigrationEngine:
    """Replays a local ledger into a destination that speaks `insert_record`.

    *clock* is a seam so a test can assert the duration is measured rather
    than assert on a real elapsed time, which is the kind of assertion that
    goes red on a loaded machine and teaches people to ignore it."""

    def __init__(self, *, dry_run: bool = False, batch: int = 500,
                 clock=time.monotonic):
        if batch < 1:
            raise ValueError(f"batch must be at least 1, got {batch}")
        self.dry_run = bool(dry_run)
        self.batch = int(batch)
        self._clock = clock

    # --- the whole run ----------------------------------------------------

    def migrate_to_postgres(self, local_store, pg_engine) -> MigrationReport:
        """Replay every session of *local_store* into *pg_engine*.

        *pg_engine* may be None, and that is a dry run BY DEFINITION -- there
        is nowhere to write -- so the report says `dry_run: True` rather than
        claiming inserts that had no destination.

        The first chain break stops the RUN, not just its session. A ledger
        whose chain is broken at session 3 is a ledger under investigation, and
        migrating sessions 4 onward would produce a destination that is
        partially trustworthy with nothing recording which part.

        A session whose id was refused is listed by its `unsafe-<hash>` stem
        and replayed like any other -- its rows carry the raw id, and the
        destination files them under the segment that id maps to -- and the
        report counts it (`sessions_quarantined`), so an operator can tell a
        quarantined id from a lost one. It used to be walked empty: the stem
        read back through `segment_name` names a segment nothing wrote."""
        started = self._clock()
        dry = self.dry_run or pg_engine is None
        total = _Counts()
        sessions = quarantined = 0
        error = None

        for session_id in local_store.list_sessions():
            sessions += 1
            quarantined += records.is_quarantine_stem(session_id)
            got = self._replay_session(local_store, pg_engine, session_id)
            total.add(got)
            if got.error is not None:
                error = got.error
                break

        return MigrationReport(
            sessions=sessions, sessions_quarantined=quarantined,
            events_read=total.read,
            events_inserted=total.inserted, events_skipped=total.skipped,
            chain_ok=error is None, duration_s=self._clock() - started,
            dry_run=dry, error=error)

    # --- one session ------------------------------------------------------

    def _replay_session(self, local_store, pg_engine, session_id) -> _Counts:
        out = _Counts()
        verifier = HashChainVerifier()
        have = NO_ROWS
        if pg_engine is not None:
            have = pg_engine.max_seg_line(_segment_of(pg_engine, session_id))
        stream = local_store.read_stream(session_id)

        while True:
            chunk = _Counts()
            try:
                done = self._one_chunk(stream, verifier, pg_engine, have,
                                       chunk)
            except DataIntegrityError as e:
                # The chunk's transaction has rolled back, so nothing it
                # counted happened -- except the reads, which really did.
                out.read += chunk.read
                out.error = str(e)
                return out
            out.add(chunk)
            if done:
                return out

    def _one_chunk(self, stream, verifier, pg_engine, have,
                   chunk: _Counts) -> bool:
        """Consume up to `batch` records. True when the stream is exhausted.

        A dry run opens no transaction at all: `max_seg_line` has already told
        it what the destination holds, and a transaction that will never write
        is a lock taken for nothing."""
        if pg_engine is None or self.dry_run:
            return self._pump(stream, verifier, None, pg_engine, have, chunk)
        with pg_engine.transaction() as cur:
            return self._pump(stream, verifier, cur, pg_engine, have, chunk)

    def _pump(self, stream, verifier, cur, pg_engine, have,
              chunk: _Counts) -> bool:
        for _ in range(self.batch):
            try:
                rec = next(stream)
            except StopIteration:
                return True
            verifier.verify_next(rec)
            chunk.read += 1
            if rec.seg_line - 1 <= have:
                # Already in the destination, by arithmetic rather than by a
                # per-row probe: the mirror's lines are contiguous from 0, so
                # "at or below the highest line it holds" IS "already there".
                chunk.skipped += 1
            elif cur is None:
                chunk.inserted += 1          # would insert
            elif pg_engine.insert_record(cur, rec):
                chunk.inserted += 1
            else:
                chunk.skipped += 1           # the conflict swallowed it
        return False


def _segment_of(pg_engine, session_id) -> str:
    """The destination's name for a LISTED session's segment.

    A quarantined session is listed by its `unsafe-<hash>` stem, and that
    stem IS its segment: `records.listed_segment_name` says so for every
    store, because asking `segment_of` would quarantine the name a second
    time and the answer would describe a segment nothing wrote -- so the
    arithmetic skip (`max_seg_line`) asked about the wrong segment and every
    re-run re-offered the rows. Any other stem asks the engine when it can
    answer, so a destination with its own naming rule is not second-guessed,
    and falls back to the ledger's own rule -- which is what
    `PostgresEventStore.segment_of` returns anyway."""
    if records.is_quarantine_stem(session_id):
        return records.listed_segment_name(session_id)
    getter = getattr(pg_engine, "segment_of", None)
    if callable(getter):
        return getter(session_id)
    return events.segment_name(session_id)
