# tests/test_storage_pgvector_driver.py
"""`storage.pgvector_driver` — two ranking paths, one contract.

`numpy` is required for the BYTEA branch and imported through
`pytest.importorskip`, because that branch really is unusable without it and a
test that quietly skipped the ranking would prove nothing about ranking.

The pgvector branch cannot be executed anywhere in this repository: the target
cluster has no `vector` row in `pg_available_extensions`, so the column 007
would add does not exist and `<=>` does not parse. What IS checkable without a
server is everything that decides WHICH branch runs and WHAT it sends — the
probe, the caching, the statement, the parameters — and that is what is pinned
here. `IMPLEMENTATION_PLAN` records that the operator-side half is unverified.
"""
import struct

import pytest

from mindhive import events, pgmirror, vectors
from mindhive.core.models import GENESIS_PREV_HASH, EventRecord
from mindhive.storage import pgvector_driver, records
from mindhive.storage.pgvector_driver import PgVectorIndex

np = pytest.importorskip("numpy")


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1
        self._rows: list = []

    def execute(self, sql, params=()):
        self.conn.executed.append((sql, tuple(params)))
        self._rows = self.conn.rows_for(sql)
        self.rowcount = 1

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeConn:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.executed: list = []
        self.commits = self.rollbacks = self.closes = self.cursors = 0

    def rows_for(self, sql):
        for key, value in self.rows.items():
            if key in sql:
                return list(value)
        return []

    def cursor(self):
        self.cursors += 1
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


PROBE = "pg_extension"
PRESENT = "SELECT segment, seg_line, event_id"
SCAN = "v.dim, v.embedding"
ORDER = "<=>"
UPSERT = "INSERT INTO mindhive.event_embeddings"


def _enc(texts):
    """Three dimensions, deterministic, and not unit length — so the
    normalisation in `pack_embedding` is exercised rather than bypassed."""
    return [[float(len(t)), 1.0, 0.5] for t in texts]


def _index(conn, **kw):
    kw.setdefault("encoder", _enc)
    kw.setdefault("project", "p")
    return PgVectorIndex(lambda: conn, **kw)


def _recs(n, session_id="s1"):
    for i in range(n):
        events.append_event("decision", {"text": f"d{i}"},
                            session_id=session_id)
    from mindhive import paths
    seg = paths.events_dir() / events.segment_name(session_id)
    return records.fold_rows(records.segment_rows(seg))


def _hit(rec, prev_hash=None, chain_hash=None, extra=()):
    """One joined row, exactly the nine columns both branches select."""
    return (events.segment_name(rec.session_id), rec.seg_line - 1,
            rec.timestamp, rec.session_id, rec.event_type, rec.payload,
            list(rec.tags),
            rec.prev_hash if prev_hash is None else prev_hash,
            rec.chain_hash if chain_hash is None else chain_hash) + tuple(extra)


def _blob(values):
    unit = np.asarray(values, dtype="<f4")
    unit = unit / float(np.linalg.norm(unit))
    return struct.pack(f"<{len(unit)}f", *unit)


# --------------------------------------------------------------------------
# the extension probe
# --------------------------------------------------------------------------

def test_a_cluster_without_the_extension_takes_the_bytea_path():
    idx = _index(FakeConn())
    assert idx.has_vector_extension() is False


def test_a_cluster_with_the_extension_says_so():
    idx = _index(FakeConn({PROBE: [(1,)]}))
    assert idx.has_vector_extension() is True


def test_the_probe_is_asked_once_and_remembered():
    conn = FakeConn({PROBE: [(1,)]})
    idx = _index(conn)
    for _ in range(5):
        idx.has_vector_extension()
    assert len([e for e in conn.executed if PROBE in e[0]]) == 1


def test_a_probe_that_raises_degrades_to_the_path_that_works_everywhere():
    """The BYTEA branch runs on a stock server; refusing to answer because the
    capability question failed would turn a degradation into an outage."""
    class Broken(FakeConn):
        def cursor(self):
            raise RuntimeError("the server went away")

    assert _index(Broken()).has_vector_extension() is False


def test_the_cache_is_per_instance_not_per_process():
    """Two instances may be pointed at two clusters."""
    assert _index(FakeConn({PROBE: [(1,)]})).has_vector_extension() is True
    assert _index(FakeConn()).has_vector_extension() is False


# --------------------------------------------------------------------------
# the one collection this table can hold
# --------------------------------------------------------------------------

def test_a_second_collection_name_is_refused_with_the_reason():
    idx = _index(FakeConn())
    with pytest.raises(ValueError) as e:
        idx.embed_batch("ledger", _recs(1))
    assert "ONE collection" in str(e.value)
    assert "model" in str(e.value) and "LocalVectorIndex" in str(e.value)


def test_a_query_checks_the_collection_too():
    with pytest.raises(ValueError):
        _index(FakeConn()).query("ledger", "anything")


def test_the_collection_can_be_renamed_at_construction():
    conn = FakeConn()
    idx = _index(conn, collection="archive")
    assert idx.embed_batch("archive", []) == 0
    with pytest.raises(ValueError):
        idx.embed_batch("events", [])


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def test_a_fresh_batch_is_written_through_the_admin_module():
    conn = FakeConn()
    assert _index(conn).embed_batch("events", _recs(2)) == 2
    upserts = [e for e in conn.executed if UPSERT in e[0]]
    assert len(upserts) == 2


def test_the_row_carries_the_ledgers_coordinates_and_its_hashes():
    (rec,) = _recs(1)
    conn = FakeConn()
    _index(conn).embed_batch("events", [rec])
    (_sql, params) = [e for e in conn.executed if UPSERT in e[0]][0]
    by_name = dict(zip(pgmirror.EMBEDDING_COLUMNS, params))
    assert by_name["project"] == "p"
    assert by_name["segment"] == "s1.jsonl"
    assert by_name["seg_line"] == 0, "the mirror's coordinate is 0-based"
    assert by_name["model"] == vectors.EMBEDDING_MODEL
    assert by_name["prev_hash"] == rec.prev_hash == GENESIS_PREV_HASH
    assert by_name["chain_hash"] == rec.chain_hash
    assert by_name["event_id"] == vectors.event_id(records.event_of(rec))


def test_the_bytes_are_the_packed_normalised_vector():
    (rec,) = _recs(1)
    conn = FakeConn()
    _index(conn).embed_batch("events", [rec])
    (_sql, params) = [e for e in conn.executed if UPSERT in e[0]][0]
    by_name = dict(zip(pgmirror.EMBEDDING_COLUMNS, params))
    doc = vectors.event_document(records.event_of(rec))
    blob, dim, norm = vectors.pack_embedding(_enc([doc])[0])
    assert (by_name["embedding"], by_name["dim"]) == (blob, dim)
    assert by_name["norm"] == pytest.approx(norm)
    assert by_name["norm"] > 1.0, "the norm is the PRE-normalisation magnitude"


def test_an_already_indexed_record_is_neither_encoded_nor_written():
    (rec,) = _recs(1)
    digest = vectors.event_id(records.event_of(rec))
    conn = FakeConn({PRESENT: [("s1.jsonl", 0, digest)]})

    def _boom(_texts):
        raise AssertionError("the encoder was reached for a known record")

    assert _index(conn, encoder=_boom).embed_batch("events", [rec]) == 0
    assert [e for e in conn.executed if UPSERT in e[0]] == []


def test_a_record_rewritten_at_the_same_line_is_re_encoded():
    """Presence alone would keep a stale vector forever, and a stale vector
    still returns confident-looking hits."""
    (rec,) = _recs(1)
    conn = FakeConn({PRESENT: [("s1.jsonl", 0, "a different digest")]})
    assert _index(conn).embed_batch("events", [rec]) == 1


def test_the_probe_asks_for_exactly_the_keys_in_the_batch():
    recs = _recs(3)
    conn = FakeConn()
    _index(conn).embed_batch("events", recs)
    (_sql, params) = [e for e in conn.executed if PRESENT in e[0]][0]
    assert params[0] == "p" and params[1] == vectors.EMBEDDING_MODEL
    assert params[2] == ["s1.jsonl"] * 3
    assert params[3] == [0, 1, 2]


def test_an_empty_batch_opens_no_connection():
    conn = FakeConn()
    assert _index(conn).embed_batch("events", []) == 0
    assert conn.executed == []


def test_the_whole_batch_is_one_transaction():
    """One transaction for the writes, however many rows. The capability
    probe's own transaction is taken first and settled first — pinned by the
    test below — so it is warmed here rather than counted."""
    conn = FakeConn()
    idx = _index(conn)
    idx.has_vector_extension()
    before = conn.commits
    idx.embed_batch("events", _recs(4))
    assert (conn.commits - before, conn.rollbacks) == (1, 0)
    assert len([e for e in conn.executed if UPSERT in e[0]]) == 4


def test_the_capability_probe_does_not_nest_inside_the_write_transaction():
    """Asking for a connection while holding one is a second connection per
    batch at best, and a self-deadlock against a one-connection pool at worst."""
    conn = FakeConn({PROBE: [(1,)]})
    _index(conn).embed_batch("events", _recs(1))
    order = [i for i, e in enumerate(conn.executed) if PROBE in e[0]]
    assert order == [0], "the probe did not run first"
    assert conn.closes == 2, "the probe's connection was not closed first"


# --------------------------------------------------------------------------
# writing: the pgvector shadow column
# --------------------------------------------------------------------------

def test_without_the_extension_no_vector_literal_is_sent():
    conn = FakeConn()
    _index(conn).embed_batch("events", _recs(1))
    (sql, params) = [e for e in conn.executed if UPSERT in e[0]][0]
    assert "embedding_vec" not in sql
    assert len(params) == len(pgmirror.EMBEDDING_COLUMNS)


def test_with_the_extension_the_literal_rides_along():
    conn = FakeConn({PROBE: [(1,)]})
    _index(conn).embed_batch("events", _recs(1))
    (sql, params) = [e for e in conn.executed if UPSERT in e[0]][0]
    assert "embedding_vec" in sql and "%s::vector" in sql
    assert params[-1].startswith("[") and params[-1].endswith("]")
    assert len(params[-1].split(",")) == 3


def test_the_literal_holds_the_raw_values_not_the_normalised_ones():
    """`vector_cosine_ops` normalises internally. Pre-normalising here would
    make `norm` — the only evidence a zero vector leaves — describe bytes
    nothing stored."""
    (rec,) = _recs(1)
    conn = FakeConn({PROBE: [(1,)]})
    _index(conn).embed_batch("events", [rec])
    (_sql, params) = [e for e in conn.executed if UPSERT in e[0]][0]
    doc = vectors.event_document(records.event_of(rec))
    raw = _enc([doc])[0]
    assert params[-1] == "[" + ",".join(repr(float(x)) for x in raw) + "]"


# --------------------------------------------------------------------------
# reading: the NumPy branch
# --------------------------------------------------------------------------

def test_the_bytea_branch_ranks_best_first():
    recs = _recs(3)
    rows = [_hit(recs[0], extra=(3, _blob([1.0, 0.0, 0.0]))),
            _hit(recs[1], extra=(3, _blob([0.0, 1.0, 0.0]))),
            _hit(recs[2], extra=(3, _blob([0.9, 0.1, 0.0])))]
    conn = FakeConn({SCAN: rows})
    idx = _index(conn, encoder=lambda texts: [[1.0, 0.0, 0.0]])
    out = idx.query("events", "anything", top_k=3)
    assert [r.seg_line for r, _ in out] == [1, 3, 2]
    assert out[0][1] == 1.0
    assert out[0][1] > out[1][1] > out[2][1]


def test_the_scan_is_bounded_and_biased_towards_the_newest():
    conn = FakeConn()
    _index(conn).query("events", "q")
    (sql, params) = [e for e in conn.executed if SCAN in e[0]][0]
    assert params == ("p", vectors.EMBEDDING_MODEL, pgvector_driver.SCAN_CEILING)
    assert "ORDER BY v.ts DESC NULLS LAST" in sql
    assert pgvector_driver.SCAN_CEILING == 20_000


def test_top_k_bounds_what_comes_back():
    recs = _recs(3)
    rows = [_hit(r, extra=(3, _blob([1.0, 0.0, 0.0]))) for r in recs]
    idx = _index(FakeConn({SCAN: rows}),
                 encoder=lambda texts: [[1.0, 0.0, 0.0]])
    assert len(idx.query("events", "q", top_k=2)) == 2


def test_a_top_k_of_zero_asks_the_server_nothing():
    conn = FakeConn()
    assert _index(conn).query("events", "q", top_k=0) == []
    assert conn.executed == []


def test_an_empty_index_answers_empty():
    assert _index(FakeConn()).query("events", "q") == []


def test_a_truncated_blob_is_refused_rather_than_reshaped():
    """`unpack_matrix`'s guard, reached through this driver: a short buffer
    reshapes without error into plausible noise."""
    (rec,) = _recs(1)
    conn = FakeConn({SCAN: [_hit(rec, extra=(3, b"\x00" * 8))]})
    with pytest.raises(ValueError, match="plausible noise"):
        _index(conn).query("events", "q")


# --------------------------------------------------------------------------
# reading: the pgvector branch
# --------------------------------------------------------------------------

def test_the_extension_branch_orders_in_sql():
    conn = FakeConn({PROBE: [(1,)]})
    _index(conn).query("events", "q", top_k=7)
    (sql, params) = [e for e in conn.executed if ORDER in e[0]][0]
    assert "ORDER BY v.embedding_vec <=> %s::vector" in sql
    assert params[0] == params[3], "the literal is bound twice, identically"
    assert params[1:3] == ("p", vectors.EMBEDDING_MODEL)
    assert params[4] == 7
    assert SCAN not in sql, "the bytes were fetched as well as ordered"


def test_the_extension_branch_converts_distance_to_similarity():
    """`<=>` under `vector_cosine_ops` is cosine DISTANCE, so `1 - distance` is
    the similarity — the same number the NumPy branch computes directly."""
    (rec,) = _recs(1)
    conn = FakeConn({PROBE: [(1,)], ORDER: [_hit(rec, extra=(0.25,))]})
    (_got, score) = _index(conn).query("events", "q")[0]
    assert score == 0.75


def test_both_branches_agree_about_a_perfect_match():
    """The contract a caller depends on: the score means the same thing
    whichever path ran."""
    (rec,) = _recs(1)
    enc = lambda texts: [[1.0, 0.0, 0.0]]                    # noqa: E731
    bytea = _index(FakeConn({SCAN: [_hit(rec, extra=(3, _blob([1.0, 0.0, 0.0])))]}),
                   encoder=enc).query("events", "q")
    pgv = _index(FakeConn({PROBE: [(1,)], ORDER: [_hit(rec, extra=(0.0,))]}),
                 encoder=enc).query("events", "q")
    assert bytea[0][1] == pgv[0][1] == 1.0
    assert bytea[0][0] == pgv[0][0]


# --------------------------------------------------------------------------
# hydration
# --------------------------------------------------------------------------

def test_a_hit_comes_back_as_the_record_that_was_indexed():
    (rec,) = _recs(1)
    conn = FakeConn({SCAN: [_hit(rec, extra=(3, _blob([1.0, 0.0, 0.0])))]})
    (got, _score) = _index(conn).query("events", "q")[0]
    assert isinstance(got, EventRecord)
    assert got == rec


def test_the_mirrors_zero_based_line_is_reported_one_based():
    recs = _recs(3)
    conn = FakeConn({SCAN: [_hit(recs[2], extra=(3, _blob([1.0, 0.0, 0.0])))]})
    (got, _score) = _index(conn).query("events", "q")[0]
    assert got.seg_line == 3 and got.event_id == "s1:3"


def test_a_row_written_before_007_is_folded_rather_than_dropped():
    """`pgmirror.embed_events` mirrors the ledger and has no fold, so its rows
    carry NULL hashes. Dropping them would silently shorten the answer; the
    segment is folded once instead."""
    recs = _recs(3)
    target = recs[1]
    scan = [_hit(target, prev_hash=None, chain_hash=None,
                 extra=(3, _blob([1.0, 0.0, 0.0])))]
    scan[0] = scan[0][:7] + (None, None) + scan[0][9:]
    fold_rows = [(r.seg_line - 1, r.timestamp, r.session_id, r.event_type,
                  r.payload, list(r.tags)) for r in recs]
    conn = FakeConn({SCAN: scan, "ORDER BY seg_line": fold_rows})
    (got, _score) = _index(conn).query("events", "q")[0]
    assert got == target
    assert got.chain_hash == target.chain_hash


def test_the_fold_runs_once_per_segment_not_once_per_hit():
    recs = _recs(3)
    scan = []
    for r in recs:
        row = _hit(r, extra=(3, _blob([1.0, 0.0, 0.0])))
        scan.append(row[:7] + (None, None) + row[9:])
    fold_rows = [(r.seg_line - 1, r.timestamp, r.session_id, r.event_type,
                  r.payload, list(r.tags)) for r in recs]
    conn = FakeConn({SCAN: scan, "ORDER BY seg_line": fold_rows})
    out = _index(conn).query("events", "q", top_k=3)
    assert len(out) == 3
    folds = [e for e in conn.executed if "ORDER BY seg_line" in e[0]]
    assert len(folds) == 1


def test_a_fold_that_cannot_reach_the_row_keeps_the_partial_record():
    """A hit with a real payload and no chain coordinates is still a search
    result; dropping it would shorten the answer without saying so."""
    (rec,) = _recs(1)
    row = _hit(rec, extra=(3, _blob([1.0, 0.0, 0.0])))
    conn = FakeConn({SCAN: [row[:7] + (None, None) + row[9:]]})
    (got, _score) = _index(conn).query("events", "q")[0]
    assert got.payload == rec.payload
    assert got.chain_hash == ""


# --------------------------------------------------------------------------
# where the write statements live
# --------------------------------------------------------------------------

def test_the_driver_carries_no_write_statement():
    from pathlib import Path
    from tests.test_pg_repo_gates import _sql_writes
    root = Path(__file__).resolve().parents[1]
    assert _sql_writes(root / "src" / "mindhive" / "storage"
                       / "pgvector_driver.py") == []


def test_the_driver_is_a_vector_index():
    from mindhive.storage.interface import VectorIndex
    assert isinstance(_index(FakeConn()), VectorIndex)


def test_query_similarity_applies_the_floor_over_this_drivers_scores():
    recs = _recs(2)
    rows = [_hit(recs[0], extra=(3, _blob([1.0, 0.0, 0.0]))),
            _hit(recs[1], extra=(3, _blob([0.0, 1.0, 0.0])))]
    idx = _index(FakeConn({SCAN: rows}),
                 encoder=lambda texts: [[1.0, 0.0, 0.0]])
    out = idx.query_similarity("events", "q", min_score=0.5)
    assert [r.seg_line for r, _ in out] == [1]
