# SPDX-License-Identifier: Apache-2.0
"""`PgVectorIndex` — vectors in Postgres, with or without pgvector (P1i, K19).

**Two ranking paths, one contract.** `pg_extension` is asked once whether
`vector` is installed. It is, on a cluster somebody provisioned it on: then the
shadow column 007 adds is written alongside the bytes and ranking is an
index-assisted `ORDER BY embedding_vec <=> %s`. It is not, on the cluster this
repository was built against -- `pg_available_extensions` has no `vector` row --
and then the stored float32 bytes are fetched and ranked in NumPy, which is the
path `pgsearch` measured at 10.4 ms against 101.7 ms for a pure-SQL dot product
over `real[]`. Both answer `(EventRecord, similarity)` and both put the score in
the same units, so a caller never learns which one ran.

**This module holds no write statement**, for the reason
`storage.postgres_driver` states at length: `tests/test_pg_repo_gates.py` D4
keeps every mutating statement inside `pgmirror.py`. Writes go through
`pgmirror.upsert_embedding`, which is also the single place the two storage
shapes -- BYTEA and `vector` -- are written together, so they cannot drift.

**Incremental, by content.** An event whose stored `event_id` already equals
the hash of its canonical row is not re-encoded and not re-written. That is the
rule `pgmirror.embed_events` uses and it is the right one: presence alone would
keep a stale vector for an event that was rewritten at the same position, and a
stale vector still returns confident-looking hits.

**Hydration, and the NULL case.** A hit is a `(segment, seg_line)` pair; a
record needs the payload, which lives in `mindhive.events`, and the chain
hashes, which 007 added to `event_embeddings`. The join gets both in one
statement. Rows written BEFORE 007 -- or by `pgmirror.embed_events`, which
mirrors the ledger and has no fold -- carry NULL hashes, and those segments are
folded once each through `PostgresEventStore` rather than dropped. One fold per
distinct segment per query, bounded by `top_k`, and only for the rows that need
it.
"""
from . import records
from .interface import VectorIndex
from .postgres_driver import PostgresEventStore

__all__ = ["PgVectorIndex", "DEFAULT_COLLECTION", "SCAN_CEILING",
           "PROBE_EXTENSION_SQL", "PRESENT_SQL", "SCAN_SQL", "ORDER_SQL"]

#: The one collection `mindhive.event_embeddings` holds per namespace. The
#: table's `model` column namespaces ENCODERS, not collections, so a second
#: collection name has nowhere to live and is refused rather than silently
#: written into the same rows.
DEFAULT_COLLECTION = "events"

#: How many vectors the NumPy path pulls over the wire, newest first. Same
#: number and same bias as `pgsearch.VECTOR_SCAN_CEILING`, and deliberately the
#: same: two ranking paths over one table that disagreed about how much of it
#: they look at would return different answers to the same question.
SCAN_CEILING = 20_000

PROBE_EXTENSION_SQL = "SELECT 1 FROM pg_extension WHERE extname = 'vector'"

#: What is already indexed, by the ledger's own key. `unnest` of two parallel
#: arrays rather than an IN-list of literals: one statement whatever the batch.
PRESENT_SQL = (
    "SELECT segment, seg_line, event_id FROM mindhive.event_embeddings "
    "WHERE project = %s AND model = %s "
    "AND (segment, seg_line) IN "
    "(SELECT s, l FROM unnest(%s::text[], %s::int[]) AS t(s, l))")

#: The columns every hit is built from, in both branches. The join is to
#: `mindhive.events` because the payload is not denormalised into the embedding
#: table -- 006 denormalised only what the PRE-FILTER needs.
_HIT_COLUMNS = ("v.segment, v.seg_line, e.ts, e.session_id, e.event_type, "
                "e.payload, e.tags, v.prev_hash, v.chain_hash")

_JOIN = ("FROM mindhive.event_embeddings v "
         "JOIN mindhive.events e ON e.project = v.project "
         "AND e.segment = v.segment AND e.seg_line = v.seg_line "
         "WHERE v.project = %s AND v.model = %s ")

#: The NumPy path: newest first, so a bound that binds drops the OLDEST rows
#: rather than an arbitrary set -- a bias, stated, exactly as `pgsearch` states
#: its own.
SCAN_SQL = ("SELECT " + _HIT_COLUMNS + ", v.dim, v.embedding " + _JOIN +
            "ORDER BY v.ts DESC NULLS LAST, v.segment DESC, v.seg_line DESC "
            "LIMIT %s")

#: The pgvector path. `<=>` is cosine DISTANCE under `vector_cosine_ops`, so
#: `1 - distance` is the cosine similarity -- the same number the NumPy branch
#: computes as a dot product over two unit vectors.
ORDER_SQL = ("SELECT " + _HIT_COLUMNS + ", (v.embedding_vec <=> %s::vector) "
             "AS distance " + _JOIN +
             "ORDER BY v.embedding_vec <=> %s::vector LIMIT %s")


class PgVectorIndex(VectorIndex):
    """The namespace's vectors in `mindhive.event_embeddings`, as a
    `VectorIndex`.

    *connect* is a zero-argument callable returning an open OWNER connection,
    the same contract `PostgresEventStore` takes and for the same reason.
    *encoder* is the test seam that keeps a 90 MB model out of the suite."""

    def __init__(self, connect, project=None, *, model=None, encoder=None,
                 collection: str = DEFAULT_COLLECTION):
        from .. import vectors
        self._connect = connect
        self.project = project
        self.model = model or vectors.EMBEDDING_MODEL
        self.collection = str(collection)
        self._encoder = encoder
        self._store = PostgresEventStore(connect, project=project)
        self._has_vector = None

    # --- naming and capability --------------------------------------------

    @property
    def name(self) -> str:
        return self._store.name

    def _check(self, collection_name) -> str:
        name = str(collection_name)
        if name != self.collection:
            raise ValueError(
                f"{name!r}: mindhive.event_embeddings holds ONE collection per "
                f"namespace ({self.collection!r}). Its `model` column "
                f"namespaces encoders, not collections, so a second collection "
                f"would share the primary key with the first. Use "
                f"LocalVectorIndex, or a new table, for another collection.")
        return name

    def has_vector_extension(self) -> bool:
        """Whether this cluster has pgvector. Asked once, then remembered.

        Once per instance and not once per process: two instances may be
        pointed at two clusters, and a module-level cache would answer for the
        wrong one. A probe that fails counts as absent -- the BYTEA path works
        everywhere, so degrading to it is the safe direction."""
        if self._has_vector is None:
            try:
                with self._store.transaction() as cur:
                    cur.execute(PROBE_EXTENSION_SQL)
                    self._has_vector = bool(cur.fetchall())
            except Exception:                                  # noqa: BLE001
                self._has_vector = False
        return self._has_vector

    def _encode(self):
        if self._encoder is not None:
            return self._encoder
        from .. import vectors
        vectors.require_encoder()
        return lambda texts: vectors.encode(texts)

    # --- the SPI: writing --------------------------------------------------

    def embed_batch(self, collection_name, records_) -> int:
        """Index *records_*; return how many rows were written.

        Records already indexed under the same content hash are neither
        encoded nor written, so a second call over the same batch costs one
        SELECT and returns 0."""
        self._check(collection_name)
        from .. import pgmirror, vectors
        batch = list(records_)
        if not batch:
            return 0
        # Both of these are resolved BEFORE the write transaction opens.
        # `has_vector_extension` takes a connection of its own, and asking for
        # one while holding another is a second connection per batch at best
        # and a self-deadlock against a one-connection pool at worst.
        encode = self._encode()
        literal = self.has_vector_extension()

        keyed = {}
        for rec in batch:
            segment = self._store.segment_of(rec.session_id)
            keyed[(segment, rec.seg_line - 1)] = rec

        written = 0
        with self._store.transaction() as cur:
            present = self._present(cur, keyed)
            fresh = [(key, rec) for key, rec in keyed.items()
                     if present.get(key) != vectors.event_id(
                         records.event_of(rec))]
            if not fresh:
                return 0
            docs = [vectors.event_document(records.event_of(rec))
                    for _key, rec in fresh]
            for (key, rec), vec in zip(fresh, encode(docs)):
                row = self._row(key, rec, vec)
                if pgmirror.upsert_embedding(
                        cur, row,
                        vector_literal=_vector_literal(vec) if literal
                        else None):
                    written += 1
        return written

    def _present(self, cur, keyed) -> dict:
        """`{(segment, seg_line): stored event_id}` for the keys in *keyed*."""
        if not keyed:
            return {}
        segs = [k[0] for k in keyed]
        lines = [k[1] for k in keyed]
        cur.execute(PRESENT_SQL, (self.name, self.model, segs, lines))
        return {(r[0], int(r[1])): r[2] for r in cur.fetchall()}

    def _row(self, key, rec, vec) -> dict:
        from .. import pgmirror, vectors
        segment, line0 = key
        blob, dim, norm = vectors.pack_embedding(vec)
        return {"project": self.name, "segment": segment, "seg_line": line0,
                "model": self.model, "ts": rec.timestamp,
                "ts_utc": pgmirror._ts_utc(rec.timestamp),
                "session_id": rec.session_id, "event_type": rec.event_type,
                "event_id": vectors.event_id(records.event_of(rec)),
                "dim": dim, "embedding": blob, "norm": norm,
                "prev_hash": rec.prev_hash, "chain_hash": rec.chain_hash}

    # --- the SPI: reading --------------------------------------------------

    def query(self, collection_name, text, top_k: int = 5) -> list:
        """`(EventRecord, similarity)` best first, by whichever path exists."""
        self._check(collection_name)
        if top_k <= 0:
            return []
        (qvec,) = self._encode()([text])
        rows = (self._order_in_sql(qvec, top_k) if self.has_vector_extension()
                else self._rank_in_numpy(qvec, top_k))
        return self._hydrate(rows)

    def _order_in_sql(self, qvec, top_k) -> list:
        literal = _vector_literal(qvec)
        with self._store.transaction() as cur:
            cur.execute(ORDER_SQL, (literal, self.name, self.model, literal,
                                    top_k))
            fetched = list(cur.fetchall())
        return [(row[:9], round(1.0 - float(row[9]), 4)) for row in fetched]

    def _rank_in_numpy(self, qvec, top_k) -> list:
        from .. import vectors
        with self._store.transaction() as cur:
            cur.execute(SCAN_SQL, (self.name, self.model, SCAN_CEILING))
            fetched = list(cur.fetchall())
        if not fetched:
            return []
        dim = int(fetched[0][9])
        matrix = vectors.unpack_matrix([row[10] for row in fetched], dim)
        return [(fetched[i][:9], round(float(score), 4))
                for i, score in vectors.rank(qvec, matrix, limit=top_k)]

    # --- hydration ---------------------------------------------------------

    def _hydrate(self, scored) -> list:
        """`(row, score)` pairs into `(EventRecord, score)`.

        Segments whose rows carry NULL hashes are folded once each -- the fold
        is per SEGMENT, not per hit, so five hits in one segment cost one."""
        folds: dict = {}
        out = []
        for row in scored:
            values, score = row
            segment, line0 = values[0], int(values[1])
            rec = _record_of(values)
            if not (rec.prev_hash and rec.chain_hash):
                rec = self._folded(folds, segment, line0) or rec
            out.append((rec, score))
        return out

    def _folded(self, folds, segment, line0):
        """The record at `(segment, seg_line)` from a fold of that segment.

        None when the fold cannot reach it, in which case the caller keeps the
        partial record it already had -- a hit with a real payload and no
        chain coordinates is still a search result, and dropping it would
        silently shorten the answer."""
        if segment not in folds:
            stem = segment[:-len(".jsonl")] if segment.endswith(".jsonl") \
                else segment
            try:
                folds[segment] = {rec.seg_line: rec
                                  for rec in self._store.read_stream(stem)}
            except Exception:                                  # noqa: BLE001
                folds[segment] = {}
        return folds[segment].get(line0 + 1)


def _vector_literal(vec) -> str:
    """pgvector's text input form, `[0.1,0.2,...]`.

    Built from the RAW values, not the normalised ones `pack_embedding`
    returns: `vector_cosine_ops` normalises internally, and pre-normalising
    would make `norm` -- the only evidence a zero vector leaves -- describe
    bytes nothing stored."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _record_of(values):
    """One joined row as an `EventRecord`, hashes included when they are there.

    The mirror's `seg_line` is 0-based, so the `+ 1` here is the same
    conversion `PostgresEventStore.read_stream` makes -- and it is made here
    rather than delegated because this row never passes through a fold."""
    from ..core.models import EventRecord
    segment, line0, ts, session_id, event_type, payload, tags = values[:7]
    prev_hash, chain_hash = values[7], values[8]
    stem = str(segment)
    if stem.endswith(".jsonl"):
        stem = stem[:-len(".jsonl")]
    line = int(line0) + 1
    return EventRecord(
        event_id=f"{session_id or stem}:{line}",
        session_id=session_id or "", seg_line=line,
        timestamp=ts or "", event_type=event_type or "",
        payload=payload if isinstance(payload, dict) else {},
        tags=list(tags or []),
        prev_hash=prev_hash or "", chain_hash=chain_hash or "")
