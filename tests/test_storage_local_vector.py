# tests/test_storage_local_vector.py
"""`storage.local_vector` — chroma behind the SPI, without chroma.

Nothing here opens a real collection. The vector extras ARE installed on this
machine, so a test that reached `vectors._open` for real would load a 90 MB
model and write a SQLite database into the isolated store — slow, and it would
test chroma rather than this module. `_open` is replaced by a fake collection
that records every call, which is also the only way to assert the things that
matter most: that the write happened INSIDE the lock, that a second call adds
nothing, and that a row `hive embed` wrote gets upgraded rather than skipped.
"""
import hashlib
import json

import pytest

from mindhive import events, paths, vectors
from mindhive.core.models import EventRecord
from mindhive.storage import records
from mindhive.storage.local_vector import (LEGAL_COLLECTION, METADATA_KEYS,
                                           LocalVectorIndex)


# --------------------------------------------------------------------------
# the fake collection
# --------------------------------------------------------------------------

class FakeCollection:
    """As much of chroma's `Collection` as this module touches.

    `add` REFUSES a duplicate id on purpose: chroma's own behaviour there has
    varied between versions, and a fake that quietly accepted one would hide
    the in-chunk deduplication this module does."""

    def __init__(self):
        self.rows: dict = {}
        self.calls: list = []
        self.get_error = None
        self.query_error = None
        self.hits: list = []        # [(id, distance)] the next query returns

    def get(self, ids=None, **_kw):
        self.calls.append(("get", list(ids or [])))
        if self.get_error is not None:
            raise self.get_error
        have = [i for i in (ids or []) if i in self.rows]
        return {"ids": have,
                "metadatas": [self.rows[i]["metadata"] for i in have],
                "documents": [self.rows[i]["document"] for i in have]}

    def add(self, ids=None, documents=None, embeddings=None, metadatas=None):
        self.calls.append(("add", list(ids or [])))
        for n, i in enumerate(ids or []):
            if i in self.rows:
                raise ValueError(f"ID {i} already exists")
            self.rows[i] = {"document": documents[n],
                            "embedding": embeddings[n],
                            "metadata": metadatas[n]}

    def update(self, ids=None, metadatas=None, **_kw):
        self.calls.append(("update", list(ids or [])))
        for n, i in enumerate(ids or []):
            self.rows[i]["metadata"] = metadatas[n]

    def query(self, query_embeddings=None, n_results=5, **_kw):
        self.calls.append(("query", n_results))
        if self.query_error is not None:
            raise self.query_error
        hits = self.hits[:n_results]
        return {"ids": [[i for i, _ in hits]],
                "metadatas": [[self.rows[i]["metadata"] for i, _ in hits]],
                "distances": [[d for _, d in hits]]}

    def count(self):
        self.calls.append(("count", None))
        return len(self.rows)

    def kinds(self):
        return [name for name, _ in self.calls]


#: The client `vectors._reading` hands a read: a stand-in for the one chroma
#: opens on a private copy of the index.
READ_CLIENT = object()


@pytest.fixture
def col(monkeypatch):
    """A fake collection in place of `vectors._open`, plus the open log.

    Each entry records whether the chroma lock file existed AT THE MOMENT the
    collection was opened, which is how the lock is asserted without
    instrumenting `core.locks` -- and which client it was opened through:
    None for the live index a write uses, `READ_CLIENT` for a read's copy."""
    from contextlib import contextmanager
    fake = FakeCollection()
    fake.opened = []

    def _open(project, name, *, refill=False, rebuild=False, client=None):
        lock = paths.project_dir(project) / vectors.CHROMA_LOCK_NAME
        fake.opened.append({"project": project, "name": name,
                            "refill": refill, "rebuild": rebuild,
                            "locked": lock.exists(), "client": client})
        return fake

    @contextmanager
    def _reading(project=None, name=None):
        yield READ_CLIENT

    monkeypatch.setattr(vectors, "_open", _open)
    monkeypatch.setattr(vectors, "_reading", _reading)
    return fake


def _enc(docs):
    """A deterministic stand-in for the sentence transformer: one float per
    document, so a test can tell two embeddings apart without a model."""
    return [[float(len(d))] for d in docs]


def _index(**kw):
    return LocalVectorIndex(encoder=_enc, **kw)


def _recs(n, session_id="s1"):
    for i in range(n):
        events.append_event("decision", {"text": f"d{i}"},
                            session_id=session_id)
    seg = paths.events_dir() / events.segment_name(session_id)
    return records.fold_rows(records.segment_rows(seg))


# --------------------------------------------------------------------------
# the encoder seam and the thread caps
# --------------------------------------------------------------------------

def test_a_preset_thread_cap_survives_construction():
    """`apply_thread_env` is setdefault, never assignment: an operator who
    exported a value made a decision, and silently overriding it would be
    lying about who is in charge of the machine."""
    import os
    os.environ["OMP_NUM_THREADS"] = "7"
    try:
        _index()
        assert os.environ["OMP_NUM_THREADS"] == "7"
    finally:
        del os.environ["OMP_NUM_THREADS"]


def test_construction_fills_in_the_caps_that_are_absent(monkeypatch):
    monkeypatch.delenv("TOKENIZERS_PARALLELISM", raising=False)
    _index()
    import os
    assert os.environ["TOKENIZERS_PARALLELISM"] == "false"


def test_the_real_encoder_is_never_reached_when_one_was_given(monkeypatch):
    """The seam has to be total: a single fall-through to `vectors._encoder`
    would load the model and make this file take minutes."""
    def _boom():
        raise AssertionError("the real encoder was reached")
    monkeypatch.setattr(vectors, "_encoder", _boom)
    monkeypatch.setattr(vectors, "_require_deps", _boom)
    assert _index()._encode() is _enc


# --------------------------------------------------------------------------
# the collection name is taken verbatim
# --------------------------------------------------------------------------

def test_the_name_reaches_chroma_exactly_as_it_was_given(col):
    """The whole reason the SPI can address `hive embed`'s collection: no
    `mindhive_` prefix is applied on the way through."""
    _index().embed_batch("events", _recs(1))
    assert col.opened[0]["name"] == "events"
    assert col.opened[0]["name"] != vectors.collection_name("events")


def test_an_illegal_name_is_refused_and_names_the_helper(col):
    for bad in ("", "ab", "_events", "events_", "Events", "x" * 64):
        with pytest.raises(ValueError) as e:
            _index().embed_batch(bad, _recs(1))
        assert "vectors.collection_name" in str(e.value)
    assert col.calls == []


def test_the_names_collection_name_builds_are_all_legal():
    """The helper the error message points at has to actually produce
    something this module accepts, or the advice is a dead end."""
    for slug in ("events", "My Slug!", "   ", "-", "x" * 200, "a.b-c"):
        assert LEGAL_COLLECTION.match(vectors.collection_name(slug))


def test_a_query_checks_the_name_too(col):
    with pytest.raises(ValueError):
        _index().query("Events", "anything")


# --------------------------------------------------------------------------
# the id, and the lock around the write
# --------------------------------------------------------------------------

def test_the_id_is_the_one_hive_embed_would_use(col):
    (rec,) = _recs(1)
    ev = events.Event(**records.record_row(rec))
    assert _index().record_id(rec) == vectors.event_id(ev)


def test_the_id_is_the_documented_hash_of_the_canonical_row(col):
    (rec,) = _recs(1)
    material = events.canonical_json(records.record_row(rec))
    assert _index().record_id(rec) == hashlib.sha256(
        material.encode("utf-8")).hexdigest()[:32]


def test_two_records_of_the_same_event_share_one_id(col):
    """Documented, not accidental: content addressing collapses byte-identical
    events, and the metadata then describes the row written last."""
    ev = events.Event(ts="2026-01-01T00:00:00Z", session_id="s1",
                      type="decision", payload={"text": "same"}, tags=[])
    first = records.build_record(ev, 1, "00" * 32)
    second = records.build_record(ev, 2, first.chain_hash)
    idx = _index()
    assert idx.record_id(first) == idx.record_id(second)
    assert idx.embed_batch("events", [first, second]) == 1
    (only,) = col.rows.values()
    assert only["metadata"]["seg_line"] == 2
    assert col.kinds().count("add") == 1


def test_the_collection_is_opened_inside_the_chroma_lock(col):
    _index().embed_batch("events", _recs(1))
    assert col.opened[0]["locked"] is True


def test_the_lock_is_released_afterwards(col):
    _index().embed_batch("events", _recs(1))
    assert not (paths.project_dir(None) / vectors.CHROMA_LOCK_NAME).exists()


def test_the_lock_is_the_one_vectors_names(monkeypatch, col):
    """Pinned by name because two writers holding two DIFFERENT locks over one
    SQLite file is the failure the lock exists to prevent, and it looks
    exactly like a working lock from inside either process."""
    from mindhive.core import locks
    seen = {}
    real = locks.file_lock

    def _spy(path, policy, *, name=None, **kw):
        seen["path"], seen["name"] = path, name
        seen["timeout_s"] = policy.timeout_s
        seen["stale_s"] = policy.stale_s
        return real(path, policy, name=name, **kw)

    monkeypatch.setattr(locks, "file_lock", _spy)
    _index().embed_batch("events", _recs(1))
    assert seen["name"] == "chroma_lock"
    assert seen["path"] == paths.project_dir(None) / vectors.CHROMA_LOCK_NAME
    assert (seen["timeout_s"], seen["stale_s"]) == (vectors.CHROMA_LOCK_TIMEOUT_S,
                                                    vectors.CHROMA_LOCK_STALE_S)


def _embedded_and_asked(col):
    """One record written, and a query answered with it."""
    idx = _index()
    (rec,) = _recs(1)
    idx.embed_batch("events", [rec])
    col.hits = [(idx.record_id(rec), 0.1)]
    idx.query("events", "anything")


def test_a_write_opens_with_refill_and_a_read_does_not(col):
    _embedded_and_asked(col)
    assert [o["refill"] for o in col.opened] == [True, False]


def test_a_write_opens_the_live_index_and_a_read_opens_the_copy(col):
    """A read through the live client writes to the index it reads
    (DATA_MODEL.md 4.19), so only the write may be handed that client."""
    _embedded_and_asked(col)
    assert [o["client"] for o in col.opened] == [None, READ_CLIENT]


# --------------------------------------------------------------------------
# what gets written
# --------------------------------------------------------------------------

def test_the_metadata_carries_every_chain_coordinate(col):
    (rec,) = _recs(1)
    _index().embed_batch("events", [rec])
    (meta,) = [r["metadata"] for r in col.rows.values()]
    assert set(meta) == set(METADATA_KEYS)
    assert meta["chain_hash"] == rec.chain_hash
    assert meta["prev_hash"] == rec.prev_hash
    assert meta["seg_line"] == rec.seg_line == 1
    assert meta["event_id"] == rec.event_id == "s1:1"


def test_every_metadata_value_is_a_scalar_chroma_accepts(col):
    rec = _recs(1)[0]
    rec = records.build_record(
        events.Event(ts=rec.timestamp, session_id="s1", type="decision",
                     payload={"nested": {"a": [1, 2]}}, tags=["x", "y"]),
        1, "00" * 32)
    _index().embed_batch("events", [rec])
    (meta,) = [r["metadata"] for r in col.rows.values()]
    for key, value in meta.items():
        assert isinstance(value, (str, int, float, bool)), key
    assert json.loads(meta["payload_json"]) == {"nested": {"a": [1, 2]}}
    assert json.loads(meta["tags_json"]) == ["x", "y"]


def test_the_document_is_the_one_hive_embed_embeds(col):
    (rec,) = _recs(1)
    _index().embed_batch("events", [rec])
    (row,) = col.rows.values()
    assert row["document"] == vectors.event_document(
        events.Event(**records.record_row(rec)))


def test_the_embedding_is_the_encoders_output_for_that_document(col):
    (rec,) = _recs(1)
    _index().embed_batch("events", [rec])
    (row,) = col.rows.values()
    assert row["embedding"] == _enc([row["document"]])[0]


# --------------------------------------------------------------------------
# incrementality
# --------------------------------------------------------------------------

def test_a_second_call_adds_nothing(col):
    recs = _recs(3)
    idx = _index()
    assert idx.embed_batch("events", recs) == 3
    assert idx.embed_batch("events", recs) == 0
    assert col.kinds().count("add") == 1
    assert len(col.rows) == 3


def test_only_the_new_records_of_a_mixed_batch_are_added(col):
    recs = _recs(4)
    idx = _index()
    idx.embed_batch("events", recs[:2])
    assert idx.embed_batch("events", recs) == 2
    added = [ids for kind, ids in col.calls if kind == "add"]
    assert len(added[1]) == 2
    assert set(added[0]).isdisjoint(added[1])


def test_an_empty_batch_touches_nothing(col):
    assert _index().embed_batch("events", []) == 0
    assert col.calls == [] and col.opened == []


def test_the_batch_is_chunked_at_the_size_vectors_declares(col, monkeypatch):
    monkeypatch.setattr(vectors, "EMBED_BATCH_SIZE", 2)
    assert _index().embed_batch("events", _recs(5)) == 5
    assert [len(ids) for kind, ids in col.calls if kind == "get"] == [2, 2, 1]
    assert col.kinds().count("add") == 3


def test_a_failed_id_probe_re_embeds_rather_than_refusing(col):
    """Re-embedding is idempotent and refusing is loss, so a probe that raises
    is treated as "nothing is present" — the same call `vectors.embed_events`
    makes, and the reason `add` is reached with ids that may exist."""
    col.get_error = RuntimeError("chroma is sulking")
    assert _index().embed_batch("events", _recs(2)) == 2
    assert len(col.rows) == 2


# --------------------------------------------------------------------------
# the shared collection: upgrading what `hive embed` wrote
# --------------------------------------------------------------------------

def _thin_row(col, rec, idx):
    """A row exactly as `vectors.embed_events` writes it: three metadata keys,
    a real vector, and nothing a record could be rebuilt from."""
    ev = events.Event(**records.record_row(rec))
    doc = vectors.event_document(ev)
    col.rows[idx.record_id(rec)] = {
        "document": doc, "embedding": _enc([doc])[0],
        "metadata": {"type": ev.type, "ts": ev.ts or "",
                     "session_id": ev.session_id or ""}}


def test_a_row_hive_embed_wrote_is_upgraded_not_skipped(col):
    (rec,) = _recs(1)
    idx = _index()
    _thin_row(col, rec, idx)
    assert idx.embed_batch("events", [rec]) == 0      # nothing NEW is searchable
    assert col.kinds() == ["get", "update"]
    meta = col.rows[idx.record_id(rec)]["metadata"]
    assert set(meta) == set(METADATA_KEYS)
    assert meta["chain_hash"] == rec.chain_hash


def test_an_upgrade_keeps_the_vector_it_found(col):
    (rec,) = _recs(1)
    idx = _index()
    _thin_row(col, rec, idx)
    before = col.rows[idx.record_id(rec)]["embedding"]
    idx.embed_batch("events", [rec])
    assert col.rows[idx.record_id(rec)]["embedding"] == before


def test_a_row_this_module_wrote_is_not_upgraded_again(col):
    recs = _recs(2)
    idx = _index()
    idx.embed_batch("events", recs)
    idx.embed_batch("events", recs)
    assert "update" not in col.kinds()


def test_a_mixed_chunk_upgrades_the_thin_and_adds_the_new(col):
    first, second = _recs(2)
    idx = _index()
    _thin_row(col, first, idx)
    assert idx.embed_batch("events", [first, second]) == 1
    assert col.kinds() == ["get", "update", "add"]
    for rec in (first, second):
        meta = col.rows[idx.record_id(rec)]["metadata"]
        assert meta["chain_hash"] == rec.chain_hash


# --------------------------------------------------------------------------
# query
# --------------------------------------------------------------------------

def test_a_hit_comes_back_as_the_record_that_was_indexed(col):
    (rec,) = _recs(1)
    idx = _index()
    idx.embed_batch("events", [rec])
    col.hits = [(idx.record_id(rec), 0.25)]
    (got, score) = idx.query("events", "anything")[0]
    assert got == rec
    assert isinstance(got, EventRecord)
    assert score == 0.75


def test_the_score_is_one_minus_distance_rounded_to_four_places(col):
    (rec,) = _recs(1)
    idx = _index()
    idx.embed_batch("events", [rec])
    col.hits = [(idx.record_id(rec), 0.123456789)]
    assert idx.query("events", "q")[0][1] == 0.8765


def test_the_hits_keep_the_order_the_backend_ranked_them_in(col):
    recs = _recs(3)
    idx = _index()
    idx.embed_batch("events", recs)
    col.hits = [(idx.record_id(r), d)
                for r, d in zip(recs, (0.1, 0.4, 0.9))]
    out = idx.query("events", "q", top_k=3)
    assert [r.event_id for r, _ in out] == [r.event_id for r in recs]
    assert [s for _, s in out] == [0.9, 0.6, 0.1]


def test_top_k_reaches_the_backend(col):
    _recs(1)
    _index().query("events", "q", top_k=9)
    assert ("query", 9) in col.calls


def test_a_thin_hit_is_dropped_rather_than_padded(col):
    """A record whose `chain_hash` is `""` is a record-shaped object that
    attests to nothing; `hive embed`'s rows belong to `hive recall`."""
    first, second = _recs(2)
    idx = _index()
    _thin_row(col, first, idx)
    idx.embed_batch("events", [second])
    col.hits = [(idx.record_id(first), 0.1), (idx.record_id(second), 0.2)]
    out = idx.query("events", "q")
    assert [r.event_id for r, _ in out] == [second.event_id]


def test_hits_dropped_as_thin_are_said_out_loud(col, caplog):
    """An `[]` from a populated collection whose hits are all `hive embed`
    rows is not "no match": the vectors matched, and no record could be
    rebuilt from them. The answer stays a list; the log says what it is not."""
    (rec,) = _recs(1)
    idx = _index()
    _thin_row(col, rec, idx)
    col.hits = [(idx.record_id(rec), 0.1)]
    with caplog.at_level("WARNING", logger="mindhive.storage.local_vector"):
        assert idx.query("events", "q") == []
    assert "1 of 1" in caplog.text and "hive embed" in caplog.text


def test_a_query_with_no_thin_hit_logs_nothing(col, caplog):
    (rec,) = _recs(1)
    idx = _index()
    idx.embed_batch("events", [rec])
    col.hits = [(idx.record_id(rec), 0.1)]
    with caplog.at_level("WARNING", logger="mindhive.storage.local_vector"):
        assert len(idx.query("events", "q")) == 1
    assert caplog.text == ""


def test_a_query_that_raises_is_an_error_not_an_empty_answer(col):
    """A broken index and an index with no match must not look alike."""
    boom = RuntimeError("Error loading hnsw index")
    col.query_error = boom
    with pytest.raises(vectors.SemanticQueryError) as err:
        _index().query("events", "q")
    assert (err.value.stage, err.value.collection) == ("query", "events")
    assert err.value.__cause__ is boom


@pytest.mark.parametrize("panics", [True, False], ids=["panic", "interrupt"])
def test_an_encoder_that_panics_is_a_failed_encode(col, panics):
    """The default encoder tokenises through HF tokenizers, a pyo3 extension,
    whose panics are BaseExceptions: one went past this driver's `except
    Exception` raw, where every other step of a read reports it as the stage
    that failed. An interrupt is still no failure."""
    stop = (type("PanicException", (BaseException,),
                 {"__module__": "pyo3_runtime"})("tokenizer panicked")
            if panics else KeyboardInterrupt())

    def _enc_stops(_docs):
        raise stop

    if panics:
        with pytest.raises(vectors.SemanticQueryError) as err:
            LocalVectorIndex(encoder=_enc_stops).query("events", "q")
        assert (err.value.stage, err.value.collection) == ("encode", "events")
        assert err.value.__cause__ is stop
    else:
        with pytest.raises(KeyboardInterrupt):
            LocalVectorIndex(encoder=_enc_stops).query("events", "q")


def test_a_query_that_answers_nothing_from_its_rows_failed(col):
    """A collection holding rows that answers no hits has failed -- the
    shared `vectors._ask` says so for this driver as for the others."""
    from mindhive import vector_errors
    (rec,) = _recs(1)
    idx = _index()
    idx.embed_batch("events", [rec])
    col.hits = []
    with pytest.raises(vectors.SemanticQueryError) as err:
        idx.query("events", "q")
    assert isinstance(err.value.__cause__, vector_errors.UnansweredQuery)


def test_a_top_k_below_one_asks_the_index_nothing(col):
    """`PgVectorIndex.query` answers a top_k of 0 with `[]` before it asks
    the server; this driver let chromadb refuse it and reported the refusal
    as a broken index. The two drivers answer alike again."""
    for top_k in (0, -1):
        assert _index().query("events", "q", top_k=top_k) == []
    assert col.calls == [] and col.opened == []


def test_a_namespace_with_no_index_answers_empty(col, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _nothing(project=None, name=None):
        yield None

    monkeypatch.setattr(vectors, "_reading", _nothing)
    assert _index().query("events", "q") == []
    assert col.opened == [], "a read opened a collection with no index there"


def test_a_query_without_chromadb_is_a_refusal_not_a_broken_index(
        monkeypatch):
    """An injected encoder skips the extras check up front, so the missing
    package is met at the read itself -- where it must still be the extras
    refusal, not a `SemanticQueryError` advising the deletion of an index."""
    import sys
    monkeypatch.setitem(sys.modules, "chromadb", None)
    with pytest.raises(RuntimeError, match="needs extras") as err:
        _index().query("events", "q")
    assert not isinstance(err.value, vectors.SemanticQueryError)


def test_a_collection_with_nothing_in_it_answers_empty(col):
    assert _index().query("events", "q") == []


def test_a_missing_distance_scores_zero_rather_than_crashing(col):
    """chroma omits `distances` when a caller did not ask for them. Zero says
    "no evidence of similarity", which is what a missing distance means."""
    (rec,) = _recs(1)
    idx = _index()
    idx.embed_batch("events", [rec])
    meta = col.rows[idx.record_id(rec)]["metadata"]
    monkey = {"metadatas": [[meta]], "distances": None}
    col.query = lambda **_kw: monkey
    assert idx.query("events", "q")[0][1] == 0.0


def test_a_broken_payload_json_degrades_to_an_empty_payload():
    """One unreadable hit must not take the other nine with it."""
    meta = dict.fromkeys(METADATA_KEYS, "")
    meta.update({"seg_line": 3, "payload_json": "{not json",
                 "tags_json": "[1, 2", "session_id": "s1"})
    rec = LocalVectorIndex.from_metadata(meta)
    assert (rec.payload, rec.tags, rec.seg_line) == ({}, [], 3)


def test_a_json_field_of_the_wrong_shape_degrades_too():
    meta = dict.fromkeys(METADATA_KEYS, "")
    meta.update({"seg_line": 1, "payload_json": "[1,2]", "tags_json": '{"a":1}'})
    rec = LocalVectorIndex.from_metadata(meta)
    assert (rec.payload, rec.tags) == ({}, [])


def test_the_round_trip_is_exact_for_a_record_with_everything_in_it():
    rec = records.build_record(
        events.Event(ts="2026-01-01T00:00:00Z", session_id="s1",
                     type="decision", payload={"text": "x", "n": 1.5},
                     tags=["a", "b"]),
        42, "ab" * 32)
    assert LocalVectorIndex.from_metadata(LocalVectorIndex.metadata(rec)) == rec


# --------------------------------------------------------------------------
# the ABC's floor
# --------------------------------------------------------------------------

def test_query_similarity_applies_the_floor_over_this_drivers_scores(col):
    recs = _recs(2)
    idx = _index()
    idx.embed_batch("events", recs)
    col.hits = [(idx.record_id(r), d) for r, d in zip(recs, (0.1, 0.8))]
    out = idx.query_similarity("events", "q", min_score=0.5)
    assert [r.event_id for r, _ in out] == [recs[0].event_id]


def test_the_driver_is_a_vector_index():
    from mindhive.storage.interface import VectorIndex
    assert isinstance(_index(), VectorIndex)
