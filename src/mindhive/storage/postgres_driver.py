# SPDX-License-Identifier: Apache-2.0
"""`PostgresEventStore` — the mirror behind the SPI (blueprint P1i, STO-02).

**This module holds no write statement.** `tests/test_pg_repo_gates.py` D4
keeps every statement that can mutate `mindhive.*` inside `pgmirror.py`, so
`insert_record` below is a CALL to `pgmirror.insert_event_idempotent` and not a
statement of its own. That is not ceremony: the reader role is read-only at the
ROLE level, so a write seam anywhere else cannot work -- it can only fail at
runtime, quietly, on the one path an operator runs once.

**The two coordinate systems, and where they meet.** `pgmirror.scan_segments`
numbers rows with `enumerate`, so the mirror's `seg_line` is 0-BASED;
`EventRecord.seg_line` is 1-BASED, because that is the line a person counts to
in the file. Both number BLANK and TORN lines, so the two differ by exactly
one and nothing else. The `+1` lives in `read_stream` and the `-1` in
`insert_record`, one each, at the boundary -- a conversion sprinkled through
call sites is how a join key quietly starts meaning a different row.

**`append` is here and is not the ordinary path.** While the file ledger is
the writer, a row written straight into Postgres has no line in any segment,
and `hive pg parity` -- which compares the mirror against the files -- will
report it as a divergence, correctly. It exists for a store whose ledger IS
Postgres, and the docstring says so where someone reaching for it will read
it.
"""
from contextlib import contextmanager

from .. import events
from ..core.models import GENESIS_PREV_HASH
from . import records
from .interface import EventStore

__all__ = ["PostgresEventStore", "READ_SEGMENT_SQL", "SEGMENTS_SQL",
           "MAX_SEG_LINE_SQL"]

#: The columns `records.build_record` needs, in the order it reads them. `ts`
#: first among the payload columns because the fold's material is the canonical
#: row and that row starts at `ts`.
READ_SEGMENT_SQL = ("SELECT seg_line, ts, session_id, event_type, payload, tags "
                    "FROM mindhive.events "
                    "WHERE project = %s AND segment = %s "
                    "ORDER BY seg_line")

#: `DISTINCT` rather than a join to a segment table: the mirror has no such
#: table, because the segment IS a column of the row it belongs to.
SEGMENTS_SQL = ("SELECT DISTINCT segment FROM mindhive.events "
                "WHERE project = %s ORDER BY segment")

MAX_SEG_LINE_SQL = ("SELECT max(seg_line) FROM mindhive.events "
                    "WHERE project = %s AND segment = %s")

#: What `max_seg_line` answers for a segment with no rows. `-1` and not `None`
#: so a caller can write `line <= have` without a null check, and so the first
#: line to insert is always `have + 1`.
NO_ROWS = -1

_SUFFIX = ".jsonl"


class PostgresEventStore(EventStore):
    """One namespace of the Postgres mirror, as an `EventStore`.

    *connect* is a zero-argument callable returning an open connection. A
    callable rather than a connection because every method here owns a
    transaction and closes it: a driver holding one long-lived connection
    would pin a server-side snapshot for the life of the process, and a
    migration that runs for minutes is exactly where that hurts.

    *project* is the namespace NAME the mirror stores in its `project` column.
    Left None it is detected once, on first use, and cached -- detection reads
    the filesystem and the answer cannot change inside one process."""

    def __init__(self, connect, project=None):
        self._connect = connect
        self.project = project
        self._name = project

    # --- naming -----------------------------------------------------------

    @property
    def name(self) -> str:
        if self._name is None:
            from .. import paths
            self._name = paths.detect_project()
        return self._name

    @staticmethod
    def segment_of(session_id) -> str:
        """The mirror's `segment` value for *session_id* -- the WRITE rule.

        `events.segment_name`, not `session_id + ".jsonl"`: a quarantined id
        lands in `unsafe-<hash>.jsonl` on disk and therefore in the mirror too,
        and a driver that spelled the name itself would look for a segment
        that does not exist.

        `append` and `insert_record` resolve through this, and a listed
        `unsafe-*` stem is deliberately quarantined AGAIN here, so nobody can
        write into a chosen quarantine slot by spelling its name. Reads take
        the listed name at face value instead: see `read_stream`."""
        return events.segment_name(session_id)

    @classmethod
    def from_dsn(cls, dsn=None, project=None) -> "PostgresEventStore":
        """A store on the OWNER DSN, resolved now and connected later.

        The DSN is required EAGERLY -- `_require_admin` raises here, naming
        `MINDHIVE_PG_DSN_ADMIN` -- while psycopg is not imported until the
        first connection. So a misconfigured environment fails at construction
        with an actionable message, and a correctly configured one that is
        never used costs no import."""
        from .. import pgmirror
        resolved = pgmirror._require_admin(dsn)

        def _open():
            from .. import pgmirror as _pg
            return _pg._connect_admin(resolved)

        return cls(_open, project=project)

    # --- transactions -----------------------------------------------------

    @contextmanager
    def transaction(self):
        """One connection, one transaction, one cursor. Commits on a clean
        exit and rolls back on ANY exception, `KeyboardInterrupt` included --
        `BaseException`, because a migration interrupted halfway through a
        session must not leave that session half-written.

        A rollback that itself fails is swallowed: the original exception is
        what the caller needs to see, and a dead connection cannot be rolled
        back by definition."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:                              # noqa: BLE001
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:                              # noqa: BLE001
                pass

    def _fetch(self, sql, params) -> list:
        with self.transaction() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    # --- the three primitives ---------------------------------------------

    def read_stream(self, session_id, from_line: int = 1):
        """Fold one segment out of the mirror, and yield from *from_line*.

        The fold runs over the WHOLE segment whatever *from_line* says, for
        the reason `LocalEventStore.read_stream` gives: every record carries
        its own `prev_hash`, so a reader starting at line 400 still needs the
        399 hashes before it. Same rule, same shape, deliberately -- the two
        drivers producing different `chain_hash` values for one ledger is the
        failure the shared `records` module exists to prevent.

        The segment is resolved with `records.listed_segment_name`, NOT
        `segment_of`. *session_id* is whatever `list_sessions` answered, and
        for a quarantined session that is the `unsafe-<hash>` stem itself --
        the id the SPI says the store can address it by. `segment_of` is the
        write rule and quarantines that stem a second time (the reservation),
        which asked the mirror for a segment nothing ever wrote and yielded
        nothing for a session this store lists."""
        segment = records.listed_segment_name(session_id)
        rows = self._fetch(READ_SEGMENT_SQL, (self.name, segment))
        pairs = [(int(r[0]) + 1, _event_of_row(r)) for r in rows]
        for rec in records.fold_rows(pairs):
            if rec.seg_line >= from_line:
                yield rec

    def list_sessions(self) -> list:
        """The segment stems, sorted -- the same strings `LocalEventStore`
        answers, including a quarantined `unsafe-<hash>`."""
        rows = self._fetch(SEGMENTS_SQL, (self.name,))
        out = []
        for (segment,) in rows:
            text = str(segment)
            out.append(text[:-len(_SUFFIX)] if text.endswith(_SUFFIX) else text)
        return sorted(out)

    def append(self, session_id, event_type, payload, tags=None):
        """Write one event straight into the mirror. Returns its record.

        **This breaks `hive pg parity` while the file ledger is the writer.**
        The row has no line in any segment, so the gate that compares the two
        will report a divergence -- and it will be right. Use it only where
        Postgres IS the ledger.

        The id is validated rather than quarantined: a caller holding the SPI
        can handle an exception, and silently renaming their session is the
        behaviour `append_event` has because it sits on a hook path where a
        raise would break the session."""
        events.validate_session_id(session_id)
        segment = self.segment_of(session_id)
        ev = events.Event(ts=events.now_iso(), session_id=session_id,
                          type=event_type, payload=payload,
                          tags=list(tags or []))
        from .. import pgmirror
        with self.transaction() as cur:
            cur.execute(READ_SEGMENT_SQL, (self.name, segment))
            rows = list(cur.fetchall())
            fold = records.fold_rows([(int(r[0]) + 1, _event_of_row(r))
                                      for r in rows])
            line0 = (int(rows[-1][0]) + 1) if rows else 0
            head = fold[-1].chain_hash if fold else GENESIS_PREV_HASH
            rec = records.build_record(ev, line0 + 1, head)
            if not pgmirror.insert_event_idempotent(cur, self.name, segment,
                                                    line0, ev):
                raise RuntimeError(
                    f"{segment} already holds a row at seg_line {line0}; "
                    f"another writer advanced the segment during this append")
        return rec

    # --- what the migration engine needs ----------------------------------

    def max_seg_line(self, segment) -> int:
        """The highest 0-based line the mirror holds for *segment*, or `-1`.

        Takes a SEGMENT name, not a session id: the migration engine iterates
        the segments it is replaying and never holds the id they came from."""
        rows = self._fetch(MAX_SEG_LINE_SQL, (self.name, str(segment)))
        if not rows or rows[0][0] is None:
            return NO_ROWS
        return int(rows[0][0])

    def insert_record(self, cur, rec) -> bool:
        """Write one `EventRecord` through the admin module. True iff it landed.

        Takes a CURSOR rather than opening its own transaction, so a caller
        replaying a session commits it as one unit. The `-1` here is the only
        1-based-to-0-based conversion in the driver."""
        from .. import pgmirror
        return pgmirror.insert_event_idempotent(
            cur, self.name, self.segment_of(rec.session_id),
            rec.seg_line - 1, records.event_of(rec))

    def sync_state(self) -> dict:
        """Nothing is cached, so nothing is persisted -- and the namespace
        name is reported because `hive db status` needs to print which
        namespace the numbers describe."""
        state = super().sync_state()
        state["project"] = self.name
        return state


def _event_of_row(row):
    """One mirror row as an `Event`.

    `payload` and `tags` arrive already decoded (psycopg adapts `jsonb` to
    Python), so this is a rename and not a parse. `tags` is copied into a list
    because the adapter's value is shared with the connection's row buffer."""
    return events.Event(ts=row[1], session_id=row[2], type=row[3],
                        payload=row[4], tags=list(row[5] or []))
