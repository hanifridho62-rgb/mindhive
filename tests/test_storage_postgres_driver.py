# tests/test_storage_postgres_driver.py
"""`storage.postgres_driver` — the mirror behind the SPI, with a fake cursor.

There is no PostgreSQL on this machine, which is the permanent state this
suite runs in. What a server would NOT prove is exactly what is proven here:

* **The two coordinate systems meet in one place each.** The mirror's
  `seg_line` is 0-based and `EventRecord.seg_line` is 1-based. A live test
  would pass with the conversion applied twice, or not at all, as long as the
  driver were self-consistent — because both sides would be wrong together.
  The pins below compare against `records.fold_rows` and against
  `pgmirror.insert_event_idempotent`'s parameters directly.
* **No write statement lives here.** Asserted with the gate's own scanner, so
  this file goes red for the same reason `tests/test_pg_repo_gates.py` would.
* **`psycopg` stays optional.** A misconfigured DSN has to fail at
  construction, with a message naming the variable, before any driver import.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import events, pgmirror
from mindhive.core.models import GENESIS_PREV_HASH
from mindhive.storage import postgres_driver, records
from mindhive.storage.postgres_driver import PostgresEventStore

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# the fake connection
# --------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1
        self._rows: list = []

    def execute(self, sql, params=()):
        self.conn.executed.append((sql, tuple(params)))
        self._rows = self.conn.rows_for(sql)
        self.rowcount = self.conn.next_rowcount()

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.conn.cursor_exits += 1
        return False


class FakeConn:
    """Answers keyed by a distinctive fragment of the statement, so a test
    says what it is answering rather than counting executes."""

    def __init__(self, rows=None, rowcounts=None):
        self.rows = dict(rows or {})
        self.rowcounts = list(rowcounts or [])
        self.executed: list = []
        self.commits = self.rollbacks = self.closes = 0
        self.cursors = self.cursor_exits = 0
        self.rollback_error = None

    def rows_for(self, sql):
        for key, value in self.rows.items():
            if key in sql:
                return list(value)
        return []

    def next_rowcount(self):
        return self.rowcounts.pop(0) if self.rowcounts else 1

    def cursor(self):
        self.cursors += 1
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self):
        self.closes += 1


def _store(conn, project="p"):
    return PostgresEventStore(lambda: conn, project=project)


def _ev(i, ts=None, session_id="s1"):
    return events.Event(ts=ts or f"2026-01-01T00:00:0{i}Z",
                        session_id=session_id, type="decision",
                        payload={"text": f"d{i}"}, tags=[])


def _mirror_rows(pairs, session_id="s1"):
    """`(seg_line0, Event)` as the six columns `READ_SEGMENT_SQL` selects."""
    return [(line0, ev.ts, session_id, ev.type, ev.payload, list(ev.tags))
            for line0, ev in pairs]


SEL = "SELECT seg_line"
SEGS = "SELECT DISTINCT segment"
MAXL = "max(seg_line)"


# --------------------------------------------------------------------------
# read_stream: 0-based in, 1-based out, one conversion
# --------------------------------------------------------------------------

def test_the_mirrors_zero_based_lines_come_back_one_based():
    evs = [_ev(0), _ev(1), _ev(2)]
    conn = FakeConn({SEL: _mirror_rows(list(enumerate(evs)))})
    got = list(_store(conn).read_stream("s1"))
    assert [r.seg_line for r in got] == [1, 2, 3]
    assert [r.event_id for r in got] == ["s1:1", "s1:2", "s1:3"]


def test_the_fold_is_the_one_records_computes():
    """Compared against `records.fold_rows` rather than against itself: a
    driver that folded consistently over the WRONG line numbers would satisfy
    every internal check and disagree with the file ledger."""
    evs = [_ev(0), _ev(1), _ev(2)]
    conn = FakeConn({SEL: _mirror_rows(list(enumerate(evs)))})
    got = list(_store(conn).read_stream("s1"))
    want = records.fold_rows([(i + 1, ev) for i, ev in enumerate(evs)])
    assert [r.chain_hash for r in got] == [r.chain_hash for r in want]
    assert got[0].prev_hash == GENESIS_PREV_HASH


def test_a_gap_left_by_a_torn_row_keeps_the_later_line_numbers():
    """The mirror skips a row it could not parse and does NOT renumber, for
    the reason `scan_segments` gives. A driver that renumbered would hand back
    a join key meaning a different row."""
    evs = [_ev(0), _ev(2)]
    conn = FakeConn({SEL: _mirror_rows([(0, evs[0]), (2, evs[1])])})
    got = list(_store(conn).read_stream("s1"))
    assert [r.seg_line for r in got] == [1, 3]
    want = records.fold_rows([(1, evs[0]), (3, evs[1])])
    assert [r.chain_hash for r in got] == [r.chain_hash for r in want]


def test_from_line_filters_but_does_not_shorten_the_fold():
    """Starting at line 3 still needs the two hashes before it."""
    evs = [_ev(i) for i in range(4)]
    conn = FakeConn({SEL: _mirror_rows(list(enumerate(evs)))})
    whole = list(_store(conn).read_stream("s1"))
    conn.executed.clear()
    part = list(_store(conn).read_stream("s1", from_line=3))
    assert [r.seg_line for r in part] == [3, 4]
    assert [r.chain_hash for r in part] == [r.chain_hash for r in whole[2:]]


def test_a_segment_with_no_rows_yields_nothing():
    assert list(_store(FakeConn()).read_stream("s1")) == []


def test_the_read_is_scoped_to_the_namespace_and_the_segment():
    conn = FakeConn()
    list(_store(conn, project="alpha").read_stream("s1"))
    sql, params = conn.executed[0]
    assert params == ("alpha", "s1.jsonl")
    assert "ORDER BY seg_line" in sql


def test_a_quarantined_session_id_reads_the_quarantined_segment():
    conn = FakeConn()
    list(_store(conn).read_stream("../escape"))
    assert conn.executed[0][1][1] == events.segment_name("../escape")
    assert conn.executed[0][1][1].startswith("unsafe-")


# --------------------------------------------------------------------------
# list_sessions
# --------------------------------------------------------------------------

def test_the_segment_stems_come_back_sorted_without_the_suffix():
    conn = FakeConn({SEGS: [("beta.jsonl",), ("alpha.jsonl",)]})
    assert _store(conn).list_sessions() == ["alpha", "beta"]


def test_a_segment_name_without_the_suffix_is_left_alone():
    """Defensive: the column is whatever was loaded, and truncating a name
    that does not end in `.jsonl` would invent a session id."""
    conn = FakeConn({SEGS: [("odd",)]})
    assert _store(conn).list_sessions() == ["odd"]


def test_an_empty_mirror_lists_no_sessions():
    assert _store(FakeConn()).list_sessions() == []


# --------------------------------------------------------------------------
# max_seg_line
# --------------------------------------------------------------------------

def test_the_highest_line_is_reported_as_the_mirrors_own_coordinate():
    conn = FakeConn({MAXL: [(41,)]})
    assert _store(conn).max_seg_line("s1.jsonl") == 41


def test_a_segment_the_mirror_has_never_seen_reports_minus_one():
    """`-1`, not None, so `line <= have` needs no null check and the first
    line to insert is always `have + 1`."""
    assert _store(FakeConn({MAXL: [(None,)]})).max_seg_line("s1.jsonl") == -1
    assert _store(FakeConn()).max_seg_line("s1.jsonl") == -1
    assert postgres_driver.NO_ROWS == -1


def test_max_seg_line_takes_a_segment_name_verbatim():
    conn = FakeConn({MAXL: [(0,)]})
    _store(conn, project="alpha").max_seg_line("unsafe-abc.jsonl")
    assert conn.executed[0][1] == ("alpha", "unsafe-abc.jsonl")


# --------------------------------------------------------------------------
# insert_record: the -1, and the call through the admin module
# --------------------------------------------------------------------------

def test_the_record_is_written_at_one_less_than_its_line():
    rec = records.build_record(_ev(0), 1, GENESIS_PREV_HASH)
    conn = FakeConn()
    cur = FakeCursor(conn)
    assert _store(conn).insert_record(cur, rec) is True
    sql, params = conn.executed[0]
    assert params == ("p", "s1.jsonl", 0, rec.timestamp,
                      pgmirror._ts_utc(rec.timestamp), "s1", "decision",
                      '{"text":"d0"}', "[]")
    assert "ON CONFLICT (project, segment, seg_line) DO NOTHING" in sql


def test_the_conversion_holds_at_a_line_that_is_not_the_first():
    rec = records.build_record(_ev(0), 42, "ab" * 32)
    conn = FakeConn()
    _store(conn).insert_record(FakeCursor(conn), rec)
    assert conn.executed[0][1][2] == 41


def test_a_row_the_conflict_swallowed_reports_false():
    rec = records.build_record(_ev(0), 1, GENESIS_PREV_HASH)
    conn = FakeConn(rowcounts=[0])
    assert _store(conn).insert_record(FakeCursor(conn), rec) is False


def test_insert_record_opens_no_transaction_of_its_own():
    """It takes a cursor precisely so a caller replaying a session commits it
    as ONE unit; opening a connection here would commit per row."""
    rec = records.build_record(_ev(0), 1, GENESIS_PREV_HASH)
    conn = FakeConn()
    _store(conn).insert_record(FakeCursor(conn), rec)
    assert (conn.commits, conn.closes, conn.cursors) == (0, 0, 0)


def test_the_round_trip_through_the_mirror_is_the_same_record():
    """The property the migration engine rests on: what `insert_record` writes
    is what `read_stream` reads back, coordinates and chain included."""
    evs = [_ev(0), _ev(1)]
    local = records.fold_rows([(i + 1, ev) for i, ev in enumerate(evs)])
    conn = FakeConn()
    store = _store(conn)
    for rec in local:
        store.insert_record(FakeCursor(conn), rec)
    written = [(params[2], params[3], params[5], params[6])
               for _sql, params in conn.executed]
    conn.rows = {SEL: _mirror_rows(list(enumerate(evs)))}
    back = list(store.read_stream("s1"))
    assert back == local
    assert [w[0] for w in written] == [0, 1]


# --------------------------------------------------------------------------
# transactions
# --------------------------------------------------------------------------

def test_a_clean_block_commits_and_closes():
    conn = FakeConn()
    with _store(conn).transaction() as cur:
        cur.execute("SELECT 1")
    assert (conn.commits, conn.rollbacks, conn.closes) == (1, 0, 1)
    assert conn.cursor_exits == 1


def test_an_exception_rolls_back_closes_and_propagates():
    conn = FakeConn()
    with pytest.raises(ValueError):
        with _store(conn).transaction():
            raise ValueError("boom")
    assert (conn.commits, conn.rollbacks, conn.closes) == (0, 1, 1)


def test_an_interrupt_rolls_back_too():
    """`BaseException`, not `Exception`: a migration interrupted halfway
    through a session must not leave that session half-written."""
    conn = FakeConn()
    with pytest.raises(KeyboardInterrupt):
        with _store(conn).transaction():
            raise KeyboardInterrupt
    assert (conn.rollbacks, conn.closes) == (1, 1)


def test_a_rollback_that_fails_does_not_mask_the_original_error():
    conn = FakeConn()
    conn.rollback_error = RuntimeError("the connection is gone")
    with pytest.raises(ValueError, match="boom"):
        with _store(conn).transaction():
            raise ValueError("boom")
    assert conn.closes == 1


def test_every_read_takes_its_own_connection():
    """No long-lived connection, so no server-side snapshot pinned for the
    life of the process."""
    conn = FakeConn({SEGS: [("s1.jsonl",)]})
    store = _store(conn)
    store.list_sessions()
    store.list_sessions()
    assert conn.cursors == 2 and conn.closes == 2


# --------------------------------------------------------------------------
# append
# --------------------------------------------------------------------------

def test_the_first_append_lands_at_line_one_on_the_genesis_hash():
    conn = FakeConn()
    rec = _store(conn).append("s1", "decision", {"text": "x"})
    assert rec.seg_line == 1 and rec.prev_hash == GENESIS_PREV_HASH
    assert rec.event_id == "s1:1"
    insert = [e for e in conn.executed if "INSERT" in e[0]][0]
    assert insert[1][2] == 0, "the mirror's coordinate is 0-based"


def test_a_later_append_chains_onto_the_head_that_is_there():
    evs = [_ev(0), _ev(1)]
    conn = FakeConn({SEL: _mirror_rows(list(enumerate(evs)))})
    head = records.fold_rows([(i + 1, ev) for i, ev in enumerate(evs)])[-1]
    rec = _store(conn).append("s1", "decision", {"text": "x"})
    assert rec.prev_hash == head.chain_hash
    assert rec.seg_line == 3


def test_an_append_after_a_gap_continues_from_the_highest_line():
    conn = FakeConn({SEL: _mirror_rows([(0, _ev(0)), (5, _ev(1))])})
    rec = _store(conn).append("s1", "decision", {"text": "x"})
    assert rec.seg_line == 7
    insert = [e for e in conn.executed if "INSERT" in e[0]][0]
    assert insert[1][2] == 6


def test_an_append_that_loses_the_race_says_so():
    conn = FakeConn(rowcounts=[1, 0])      # the SELECT, then the refused INSERT
    with pytest.raises(RuntimeError, match="already holds a row"):
        _store(conn).append("s1", "decision", {"text": "x"})
    assert conn.rollbacks == 1 and conn.commits == 0


def test_a_bad_session_id_is_refused_rather_than_quarantined():
    """The SPI's caller can handle an exception; `append_event` quarantines
    because it sits on a hook path where a raise breaks the session."""
    conn = FakeConn()
    with pytest.raises(ValueError, match="Format session_id"):
        _store(conn).append("../escape", "decision", {})
    assert conn.executed == []


def test_the_whole_append_is_one_transaction():
    conn = FakeConn()
    _store(conn).append("s1", "decision", {"text": "x"})
    assert (conn.cursors, conn.commits, conn.closes) == (1, 1, 1)
    assert len(conn.executed) == 2, "one read, one insert"


# --------------------------------------------------------------------------
# construction, and what it must not import
# --------------------------------------------------------------------------

def test_from_dsn_without_a_dsn_names_the_variable(monkeypatch):
    monkeypatch.delenv("MINDHIVE_PG_DSN_ADMIN", raising=False)
    with pytest.raises(RuntimeError, match="MINDHIVE_PG_DSN_ADMIN"):
        PostgresEventStore.from_dsn()


def test_from_dsn_resolves_the_dsn_without_connecting(monkeypatch):
    """Eager on the DSN, lazy on psycopg: a misconfigured environment fails at
    construction, a configured one that is never used costs no import."""
    monkeypatch.setenv("MINDHIVE_PG_DSN_ADMIN", "service=mindhive_owner")
    called = []
    monkeypatch.setattr(pgmirror, "_connect_admin",
                        lambda dsn=None: called.append(dsn))
    store = PostgresEventStore.from_dsn(project="p")
    assert called == []
    store._connect()
    assert called == ["service=mindhive_owner"]


def test_an_explicit_dsn_beats_the_environment(monkeypatch):
    monkeypatch.delenv("MINDHIVE_PG_DSN_ADMIN", raising=False)
    seen = []
    monkeypatch.setattr(pgmirror, "_connect_admin",
                        lambda dsn=None: seen.append(dsn))
    PostgresEventStore.from_dsn("service=explicit")._connect()
    assert seen == ["service=explicit"]


def test_the_namespace_is_detected_once_when_none_was_given(monkeypatch):
    from mindhive import paths
    calls = []

    def _detect():
        calls.append(1)
        return "detected"

    monkeypatch.setattr(paths, "detect_project", _detect)
    store = PostgresEventStore(lambda: FakeConn())
    assert store.name == "detected" == store.name
    assert len(calls) == 1


def test_importing_the_driver_pulls_in_neither_psycopg_nor_the_admin_module():
    """Both imports are inside functions. A module-scope `from .. import
    pgmirror` would put the owner-DSN module on the import graph of anything
    that touches the storage package."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    out = subprocess.run(
        [sys.executable, "-c",
         "import mindhive.storage.postgres_driver, sys; "
         "print('psycopg' in sys.modules, 'mindhive.pgmirror' in sys.modules)"],
        capture_output=True, text=True, cwd=str(REPO), env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False", out.stdout


# --------------------------------------------------------------------------
# the write-SQL gate, asserted here as well as in the gate file
# --------------------------------------------------------------------------

def test_the_driver_carries_no_write_statement():
    """Run with the GATE's own scanner, so this file goes red for exactly the
    reason `tests/test_pg_repo_gates.py` D4 would — and names the driver while
    doing it."""
    from tests.test_pg_repo_gates import _sql_writes
    path = REPO / "src" / "mindhive" / "storage" / "postgres_driver.py"
    assert _sql_writes(path) == []


def test_the_scanner_used_above_really_finds_statements():
    """CANARY. An emptiness claim over a scanner that returns [] for anything
    would hold forever."""
    from tests.test_pg_repo_gates import _sql_writes
    found = _sql_writes(REPO / "src" / "mindhive" / "pgmirror.py")
    assert any("INSERT INTO mindhive.events" in s for s in found)


def test_the_select_statements_are_reads():
    for sql in (postgres_driver.READ_SEGMENT_SQL, postgres_driver.SEGMENTS_SQL,
                postgres_driver.MAX_SEG_LINE_SQL):
        assert sql.lstrip().upper().startswith("SELECT"), sql


# --------------------------------------------------------------------------
# the SPI's floor
# --------------------------------------------------------------------------

def test_the_driver_is_an_event_store():
    from mindhive.storage.interface import EventStore
    assert isinstance(_store(FakeConn()), EventStore)


def test_read_events_and_the_slice_come_from_the_same_stream():
    evs = [_ev(i) for i in range(4)]
    conn = FakeConn({SEL: _mirror_rows(list(enumerate(evs))),
                     SEGS: [("s1.jsonl",)]})
    store = _store(conn)
    assert [r.seg_line for r in store.read_events("s1")] == [1, 2, 3, 4]
    assert [r.seg_line for r in
            store.get_session_slice("s1", from_line=2, to_line=3)] == [2, 3]


def test_sync_state_reports_the_namespace_and_persists_nothing():
    conn = FakeConn({SEGS: [("s1.jsonl",)]})
    state = _store(conn, project="alpha").sync_state()
    assert state == {"driver": "PostgresEventStore", "sessions": 1,
                     "persisted": False, "project": "alpha"}
