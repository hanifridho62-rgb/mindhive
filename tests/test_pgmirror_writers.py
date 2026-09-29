# tests/test_pgmirror_writers.py
"""`pgmirror.insert_event_idempotent` / `upsert_embedding` — the single-row
write seam the migration engine and the vector driver call (blueprint P1i).

No server, and no psycopg. That is the point of passing JSON as TEXT with an
explicit `::jsonb` cast instead of `psycopg.types.json.Jsonb`: the whole write
path becomes exercisable with a fake cursor, so the statement, its parameters
and its idempotency are checked on a machine that has no PostgreSQL on it —
which is every machine this repository has been developed on so far.

What these tests can NOT prove is that the SQL is valid. `tests/test_pg_live.py`
does that where a cluster exists. What they prove is the part a live test would
not notice: that the conflict target is the constraint 001 actually declares,
that the parameters are in statement order, and that the two helpers differ in
the way they were designed to — DO NOTHING for an immutable event, DO UPDATE
for a derived vector.
"""
import pytest

from mindhive import events, pgmirror


class FakeCursor:
    """The three members these helpers touch.

    `rowcount` is settable because it is the ENTIRE return value of
    `insert_event_idempotent`: the difference between "this call wrote the row"
    and "it was already there" is a number the server reports and nothing else
    can be derived from."""

    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))

    @property
    def sql(self):
        return self.executed[-1][0]

    @property
    def params(self):
        return self.executed[-1][1]


def _ev(text="x", ts="2026-01-01T00:00:00Z", tags=None):
    return events.Event(ts=ts, session_id="s1", type="decision",
                        payload={"text": text}, tags=list(tags or []))


def _row(**kw):
    base = {"project": "p", "segment": "s1.jsonl", "seg_line": 0,
            "model": "all-MiniLM-L6-v2", "ts": "2026-01-01T00:00:00Z",
            "ts_utc": None, "session_id": "s1", "event_type": "decision",
            "event_id": "a" * 32, "dim": 384, "embedding": b"\x00" * 1536,
            "norm": 1.0, "prev_hash": "0" * 64, "chain_hash": "f" * 64}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# insert_event_idempotent — the statement
# --------------------------------------------------------------------------

def test_the_conflict_target_is_the_constraint_the_schema_declares():
    """`events_natural_key UNIQUE (project, segment, seg_line)`, 001_schema.sql.
    A conflict target that is not a real unique constraint is not a slow
    statement — it is a runtime error, on a path an operator runs once."""
    assert ("ON CONFLICT (project, segment, seg_line) DO NOTHING"
            in pgmirror._INSERT_EVENT_SQL)


def test_the_insert_names_the_mirror_table_and_nine_columns():
    sql = pgmirror._INSERT_EVENT_SQL
    assert "INSERT INTO mindhive.events" in sql
    for col in ("project", "segment", "seg_line", "ts", "ts_utc",
                "session_id", "event_type", "payload", "tags"):
        assert col in sql, col
    assert sql.count("%s") == 9


def test_the_json_columns_are_cast_rather_than_adapted():
    """The cast is what lets a fake cursor stand in for psycopg here."""
    assert "%s::jsonb, %s::jsonb" in pgmirror._INSERT_EVENT_SQL


def test_the_statement_never_names_global_seq_or_ts_key():
    """`global_seq` is identity only and `ts_key` is GENERATED ALWAYS — naming
    the second is an error rather than an optimisation to skip."""
    assert "global_seq" not in pgmirror._INSERT_EVENT_SQL
    assert "ts_key" not in pgmirror._INSERT_EVENT_SQL


# --------------------------------------------------------------------------
# insert_event_idempotent — the parameters
# --------------------------------------------------------------------------

def test_the_parameters_are_in_statement_order():
    cur = FakeCursor()
    ev = _ev()
    pgmirror.insert_event_idempotent(cur, "p", "s1.jsonl", 0, ev)
    assert cur.params == ("p", "s1.jsonl", 0, ev.ts, pgmirror._ts_utc(ev.ts),
                          "s1", "decision", '{"text":"x"}', "[]")


def test_the_payload_bytes_are_the_ledgers_own_canonical_bytes():
    """Not merely equal JSON: the same string `canonical_json` writes into the
    segment, so the mirror and the chain hash cover identical material."""
    cur = FakeCursor()
    ev = _ev(text="ünïcode", tags=["b", "a"])
    pgmirror.insert_event_idempotent(cur, "p", "s1.jsonl", 3, ev)
    assert cur.params[7] == events.canonical_json(ev.payload)
    assert cur.params[8] == events.canonical_json(ev.tags)
    assert cur.params[8] == '["b","a"]', "tag order is the event's, not sorted"


def test_the_seg_line_is_passed_through_as_an_int():
    """The helper takes the MIRROR's 0-based coordinate and does not convert:
    the 1-based `EventRecord.seg_line` is translated once, at the driver
    boundary, and a second conversion here would undo it."""
    cur = FakeCursor()
    pgmirror.insert_event_idempotent(cur, "p", "s1.jsonl", "7", _ev())
    assert cur.params[2] == 7 and isinstance(cur.params[2], int)


def test_an_unparsable_timestamp_becomes_a_null_instant_not_a_failure():
    """001_schema.sql: `ts_utc` NULL is a reportable anomaly, never a load
    failure — the TEXT `ts` is still the authoritative value."""
    cur = FakeCursor()
    pgmirror.insert_event_idempotent(cur, "p", "s1.jsonl", 0, _ev(ts="not a date"))
    assert cur.params[3] == "not a date" and cur.params[4] is None


def test_an_event_with_no_tags_sends_an_empty_array():
    cur = FakeCursor()
    ev = events.Event(ts="2026-01-01T00:00:00Z", session_id="s1",
                      type="decision", payload={}, tags=None)
    pgmirror.insert_event_idempotent(cur, "p", "s1.jsonl", 0, ev)
    assert cur.params[8] == "[]"


# --------------------------------------------------------------------------
# insert_event_idempotent — what the return value means
# --------------------------------------------------------------------------

def test_a_row_that_landed_reports_true():
    assert pgmirror.insert_event_idempotent(
        FakeCursor(rowcount=1), "p", "s1.jsonl", 0, _ev()) is True


def test_a_row_the_conflict_swallowed_reports_false():
    """The whole reason the helper exists: DO NOTHING succeeds either way, and
    `rowcount` is the only thing that tells the two apart."""
    assert pgmirror.insert_event_idempotent(
        FakeCursor(rowcount=0), "p", "s1.jsonl", 0, _ev()) is False


def test_a_second_call_is_one_round_trip_not_a_select_then_insert():
    """The check and the write are one statement under one snapshot, so two
    migrations racing on a segment cannot both see "absent" and both insert."""
    cur = FakeCursor(rowcount=0)
    pgmirror.insert_event_idempotent(cur, "p", "s1.jsonl", 0, _ev())
    assert len(cur.executed) == 1
    assert "SELECT" not in cur.sql.upper()


# --------------------------------------------------------------------------
# upsert_embedding
# --------------------------------------------------------------------------

def test_the_upsert_targets_the_embedding_tables_primary_key():
    sql = pgmirror._upsert_embedding_sql(False)
    assert "INSERT INTO mindhive.event_embeddings" in sql
    assert "ON CONFLICT (project, segment, seg_line, model) DO UPDATE SET" in sql


def test_every_non_key_column_is_overwritten_on_conflict():
    """DO UPDATE, not DO NOTHING, and the difference is the point: an event is
    immutable, a vector derived from it is not. A column left out here keeps a
    stale value forever while every count reports success."""
    sql = pgmirror._upsert_embedding_sql(False)
    for col in pgmirror.EMBEDDING_COLUMNS[4:]:
        assert f"{col} = EXCLUDED.{col}" in sql, col
    for key in pgmirror.EMBEDDING_COLUMNS[:4]:
        assert f"{key} = EXCLUDED.{key}" not in sql, key


def test_the_indexed_at_stamp_moves_with_the_row():
    """"How far behind is the index" has to keep meaning something."""
    assert "indexed_at = now()" in pgmirror._upsert_embedding_sql(False)


def test_the_hash_columns_ride_along():
    sql = pgmirror._upsert_embedding_sql(False)
    assert "prev_hash" in sql and "chain_hash" in sql


def test_the_parameters_are_the_columns_in_order():
    cur = FakeCursor()
    row = _row()
    pgmirror.upsert_embedding(cur, row)
    assert cur.params == tuple(row[k] for k in pgmirror.EMBEDDING_COLUMNS)
    assert len(cur.params) == 14


def test_a_missing_column_is_refused_by_name():
    """A silently dropped column would insert NULL into a NOT NULL and surface
    as a constraint violation naming the table, not the caller."""
    row = _row()
    del row["norm"]
    with pytest.raises(KeyError) as e:
        pgmirror.upsert_embedding(FakeCursor(), row)
    assert "norm" in str(e.value)


def test_an_extra_key_in_the_row_is_ignored():
    """The caller's dict may carry more than the statement wants — the column
    list decides, not the row."""
    cur = FakeCursor()
    pgmirror.upsert_embedding(cur, _row(extra="ignored"))
    assert len(cur.params) == 14


# --------------------------------------------------------------------------
# the pgvector branch
# --------------------------------------------------------------------------

def test_without_a_vector_literal_the_statement_never_mentions_pgvector():
    """This cluster has no `vector` type. A statement naming the column would
    not be slow, it would fail to parse."""
    sql = pgmirror._upsert_embedding_sql(False)
    assert "embedding_vec" not in sql and "::vector" not in sql


def test_with_a_vector_literal_the_shadow_column_is_written_too():
    cur = FakeCursor()
    pgmirror.upsert_embedding(cur, _row(), vector_literal="[0.1,0.2]")
    assert "embedding_vec" in cur.sql
    assert "%s::vector" in cur.sql
    assert cur.params[-1] == "[0.1,0.2]"
    assert len(cur.params) == 15


def test_the_shadow_column_is_refreshed_on_conflict_as_well():
    sql = pgmirror._upsert_embedding_sql(True)
    assert "embedding_vec = EXCLUDED.embedding_vec" in sql


def test_the_two_forms_differ_by_exactly_one_column():
    """Built rather than spelled twice, so a column added to one cannot go
    missing from the other."""
    plain = pgmirror._upsert_embedding_sql(False)
    vector = pgmirror._upsert_embedding_sql(True)
    assert vector.count("%s") == plain.count("%s") + 1
    for col in pgmirror.EMBEDDING_COLUMNS:
        assert col in plain and col in vector


def test_an_empty_string_literal_still_takes_the_vector_branch():
    """`None` means no extension; `""` is a caller bug that must not silently
    fall through to the BYTEA-only statement and lose the column."""
    cur = FakeCursor()
    pgmirror.upsert_embedding(cur, _row(), vector_literal="")
    assert "embedding_vec" in cur.sql and cur.params[-1] == ""


# --------------------------------------------------------------------------
# where these statements are allowed to live
# --------------------------------------------------------------------------

def test_both_helpers_are_reachable_from_the_module_the_gate_exempts():
    """`tests/test_pg_repo_gates.py` D4 permits write SQL in `pgmirror.py` and
    nowhere else. These two being importable from here is what lets the storage
    drivers call them without carrying a statement of their own."""
    assert callable(pgmirror.insert_event_idempotent)
    assert callable(pgmirror.upsert_embedding)
    assert pgmirror.insert_event_idempotent.__module__ == "mindhive.pgmirror"
    assert pgmirror.upsert_embedding.__module__ == "mindhive.pgmirror"
