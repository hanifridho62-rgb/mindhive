# tests/test_vectors.py
# Cover vectors.py without requiring the heavy optional extras (chromadb +
# sentence_transformers): exercise the guard gating, the dependency check, and
# the result/IO shapes by stubbing the encoder + collection.
import pytest
from mindhive import vectors, paths


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def _ledger_file(name="20260101-000000-decision-abcd1234.md", body="alpha beta gamma"):
    ldir = paths.project_dir() / "LEDGER"
    ldir.mkdir(parents=True, exist_ok=True)
    (ldir / name).write_text(body, encoding="utf-8")
    return name


class _FakeCollection:
    """Stand-in for a chromadb collection; records what embed() does."""
    def __init__(self):
        self.added = {}
        self.deleted = []
        self._query_result = {"documents": [[]], "ids": [[]], "distances": [[]]}

    def set_query_result(self, docs, ids, distances=None):
        self._query_result = {"documents": [docs], "ids": [ids],
                              "distances": [distances if distances is not None
                                            else [0.0] * len(docs)]}

    def delete(self, ids=None):
        self.deleted = ids

    def add(self, ids=None, documents=None, embeddings=None):
        self.added = {"ids": ids, "documents": documents, "embeddings": embeddings}

    def query(self, query_embeddings=None, n_results=10):
        return self._query_result


def _stub_backend(monkeypatch, col):
    """Replace the real deps/encoder/collection with deterministic stand-ins."""
    monkeypatch.setattr(vectors, "_require_deps", lambda: None)
    monkeypatch.setattr(vectors, "_encoder", lambda: (lambda texts: [[0.0] for _ in list(texts)]))
    monkeypatch.setattr(vectors, "_collection",
                        lambda project=None, **kw: col)


# --- dependency check -------------------------------------------------------

def test_require_deps_matches_environment():
    """_require_deps raises a clear RuntimeError when the extras are missing and
    is a no-op when they're present."""
    try:
        import chromadb  # noqa: F401
        import sentence_transformers  # noqa: F401
        have = True
    except ImportError:
        have = False
    if have:
        assert vectors._require_deps() is None
    else:
        with pytest.raises(RuntimeError, match="vectors"):
            vectors._require_deps()


# --- guard gating (LIVE_LOCK) ----------------------------------------------

def test_semantic_recall_blocked_by_live_lock(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    paths.store_home().mkdir(parents=True, exist_ok=True)
    (paths.store_home() / "LIVE_LOCK").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="LIVE_LOCK"):
        vectors.semantic_recall("anything")


def test_embed_blocked_by_live_lock(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    paths.store_home().mkdir(parents=True, exist_ok=True)
    (paths.store_home() / "LIVE_LOCK").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="LIVE_LOCK"):
        vectors.embed()


def test_force_overrides_live_lock_for_embed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    paths.store_home().mkdir(parents=True, exist_ok=True)
    (paths.store_home() / "LIVE_LOCK").write_text("x", encoding="utf-8")
    _stub_backend(monkeypatch, _FakeCollection())
    # no LEDGER yet -> nothing to embed, but force must get us past the guard
    assert vectors.embed(force=True) == {"embedded": 0}


# --- embed behavior ---------------------------------------------------------

def test_embed_empty_ledger_returns_zero(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _stub_backend(monkeypatch, _FakeCollection())
    assert vectors.embed() == {"embedded": 0}


def test_embed_indexes_ledger_files_by_bare_name(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    col = _FakeCollection()
    _stub_backend(monkeypatch, col)
    _ledger_file("20260101-000000-decision-aaaa1111.md", "first")
    _ledger_file("20260102-000000-caveat-bbbb2222.md", "second")
    res = vectors.embed()
    assert res == {"embedded": 2}
    # ids are BARE filenames (matching recall/semantic_recall shape)
    assert set(col.added["ids"]) == {
        "20260101-000000-decision-aaaa1111.md",
        "20260102-000000-caveat-bbbb2222.md",
    }
    assert all("/" not in i and "\\" not in i for i in col.added["ids"])


# --- semantic_recall shape --------------------------------------------------

def test_semantic_recall_returns_recall_compatible_shape(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    col = _FakeCollection()
    col.set_query_result(docs=["hello   world   doc"],
                         ids=["20260101-000000-decision-abcd1234.md"],
                         distances=[0.25])
    _stub_backend(monkeypatch, col)
    hits = vectors.semantic_recall("hello", limit=1)
    assert len(hits) == 1
    h = hits[0]
    # Same file/snippet/score shape as recall.recall (score lets callers rank
    # and threshold). Score is similarity = 1 - cosine distance.
    assert set(h.keys()) == {"file", "snippet", "score"}
    assert h["file"] == "20260101-000000-decision-abcd1234.md"
    assert "/" not in h["file"] and "\\" not in h["file"]
    assert h["snippet"] == "hello world doc"        # whitespace collapsed
    assert h["score"] == 0.75                        # 1 - 0.25


def test_semantic_recall_score_none_when_distances_absent(tmp_path, monkeypatch):
    """Defensive: some chroma configs omit distances; score is None, not a crash."""
    _isolate(tmp_path, monkeypatch)
    col = _FakeCollection()
    col._query_result = {"documents": [["a doc"]],
                         "ids": [["20260101-000000-decision-abcd1234.md"]]}
    _stub_backend(monkeypatch, col)
    hits = vectors.semantic_recall("a", limit=1)
    assert hits[0]["score"] is None


# --- event-level vectors -----------------------------------------------------
#
# `embed()` above indexes one vector per LEDGER/*.md FILE, so a hit points at
# the projection a memo lives in. These cover the half that indexes one vector
# per EVENT, where the hit IS the memo — and the two properties that make it
# affordable over 37k events: content-addressed ids (so re-running is a scan,
# not 37k re-encodes) and semantic types only.

from mindhive import events as events_mod        # noqa: E402


def _ev(type="decision", text="chose postgres for the mirror",
        ts="2026-01-01T00:00:00Z", session_id="alpha"):
    return events_mod.Event(ts=ts, session_id=session_id, type=type,
                            payload={"text": text}, tags=[])


class _FakeEventCollection:
    """Stand-in for the events collection: records adds, answers id probes."""

    def __init__(self, present=(), space="cosine"):
        self.present = set(present)
        self.batches = []
        self.metadata = {"hnsw:space": space}
        self.where = "unset"
        self._query_result = {"documents": [[]], "ids": [[]], "distances": [[]],
                              "metadatas": [[]]}
        self.get_raises = False

    def set_query_result(self, docs, ids, distances, metadatas):
        self._query_result = {"documents": [docs], "ids": [ids],
                              "distances": [distances], "metadatas": [metadatas]}

    def get(self, ids=None):
        if self.get_raises:
            raise RuntimeError("chroma is unhappy")
        return {"ids": [i for i in (ids or []) if i in self.present]}

    def add(self, ids=None, documents=None, embeddings=None, metadatas=None):
        self.batches.append({"ids": list(ids or []),
                             "documents": list(documents or []),
                             "metadatas": list(metadatas or [])})
        self.present.update(ids or [])

    def query(self, query_embeddings=None, n_results=10, where=None):
        self.where = where
        return self._query_result

    def count(self):
        return len(self.present)


def _stub_events_backend(monkeypatch, col, evs):
    monkeypatch.setattr(vectors, "_require_deps", lambda: None)
    monkeypatch.setattr(vectors, "_encoder",
                        lambda: (lambda texts: [[0.0] for _ in list(texts)]))
    monkeypatch.setattr(vectors, "_events_collection",
                        lambda project=None, **kw: col)
    monkeypatch.setattr(events_mod, "read_all", lambda project=None: list(evs))


# --- ids and documents -------------------------------------------------------

def test_an_event_id_is_the_hash_of_the_event_and_nothing_else():
    """Content-addressed, which is what makes incrementality cursor-free.

    Two events equal in every chained field get one id; change any of them and
    it is a NEW id rather than a silent overwrite of the old vector."""
    a, b = _ev(), _ev()
    assert vectors.event_id(a) == vectors.event_id(b)
    assert len(vectors.event_id(a)) == 32

    for mutant in (_ev(text="chose sqlite instead"),
                   _ev(ts="2026-01-02T00:00:00Z"),
                   _ev(session_id="beta"), _ev(type="caveat")):
        assert vectors.event_id(mutant) != vectors.event_id(a), mutant


def test_an_event_document_leads_with_its_type_and_timestamp():
    """The encoder sees them, so "what did we decide in January" has something
    to match beyond the body — and a human reading the snippet can tell what
    they are looking at."""
    doc = vectors.event_document(_ev())
    assert doc.startswith("[decision] 2026-01-01T00:00:00Z ")
    assert "chose postgres for the mirror" in doc


def test_an_event_with_no_text_is_still_searchable():
    """A semantic type whose payload carries no `text` must not become an empty
    vector — a structure that can be matched beats nothing at all."""
    ev = events_mod.Event(ts="2026-01-01T00:00:00Z", session_id="alpha",
                          type="state_set",
                          payload={"key": "next_step", "value": "land it"},
                          tags=[])
    doc = vectors.event_document(ev)
    assert '"key":"next_step"' in doc and '"value":"land it"' in doc


def test_an_event_document_is_bounded():
    assert len(vectors.event_document(_ev(text="x" * 9000))) == 4000


# --- what gets indexed -------------------------------------------------------

def test_only_semantic_events_are_embedded(tmp_path, monkeypatch):
    """`command_run`, `file_touched` and `assistant_response` are the bulk of
    this ledger and carry no meaning worth a vector. Indexing them would triple
    the cost and bury the memos in near-duplicate telemetry."""
    _isolate(tmp_path, monkeypatch)
    col = _FakeEventCollection()
    _stub_events_backend(monkeypatch, col, [
        _ev(type="decision", text="a decision"),
        events_mod.Event(ts="2026-01-01T00:00:01Z", session_id="alpha",
                         type="command_run", payload={"cmd": "ls"}, tags=[]),
        _ev(type="caveat", text="a caveat", ts="2026-01-01T00:00:02Z"),
    ])
    out = vectors.embed_events()
    assert out == {"scanned": 3, "eligible": 2, "embedded": 2, "skipped": 0}
    docs = [d for b in col.batches for d in b["documents"]]
    assert not any("command_run" in d for d in docs)


def test_the_types_indexed_are_a_reference_to_the_registry_not_a_copy():
    """A new semantic type becomes recallable by being registered once. A copied
    set is a second place to forget."""
    assert vectors._semantic_types() is events_mod.SEMANTIC_TYPES


def test_a_second_run_embeds_nothing(tmp_path, monkeypatch):
    """The property the whole design rests on: at event granularity a
    re-encode-everything embed would be 37k encodes per invocation, and nobody
    would run it twice."""
    _isolate(tmp_path, monkeypatch)
    col = _FakeEventCollection()
    evs = [_ev(text=f"decision {i}", ts="2026-01-01T00:00:0%dZ" % i)
           for i in range(3)]
    _stub_events_backend(monkeypatch, col, evs)

    first = vectors.embed_events()
    second = vectors.embed_events()
    assert first["embedded"] == 3 and first["skipped"] == 0
    assert second["embedded"] == 0 and second["skipped"] == 3
    assert len(col.batches) == 1, "the second run added a batch anyway"


def test_a_failed_id_probe_re_embeds_rather_than_dropping_the_batch(
        tmp_path, monkeypatch):
    """Losing a memo from the index is worse than paying to encode it twice."""
    _isolate(tmp_path, monkeypatch)
    col = _FakeEventCollection()
    col.get_raises = True
    _stub_events_backend(monkeypatch, col, [_ev()])
    assert vectors.embed_events()["embedded"] == 1


def test_each_indexed_event_carries_its_type_ts_and_session(tmp_path,
                                                            monkeypatch):
    """Metadata is what lets a hit be CITED rather than merely quoted."""
    _isolate(tmp_path, monkeypatch)
    col = _FakeEventCollection()
    _stub_events_backend(monkeypatch, col, [_ev()])
    vectors.embed_events()
    assert col.batches[0]["metadatas"] == [
        {"type": "decision", "ts": "2026-01-01T00:00:00Z",
         "session_id": "alpha"}]


# --- recall ------------------------------------------------------------------

def test_an_event_hit_names_the_memo_not_the_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    col = _FakeEventCollection()
    col.set_query_result(docs=["[decision] 2026-01-01T00:00:00Z   chose   pg"],
                         ids=["abc123"], distances=[0.25],
                         metadatas=[{"type": "decision",
                                     "ts": "2026-01-01T00:00:00Z",
                                     "session_id": "alpha"}])
    _stub_events_backend(monkeypatch, col, [])
    hits = vectors.semantic_recall_events("postgres", limit=1)
    assert len(hits) == 1
    h = hits[0]
    assert set(h) == {"id", "type", "ts", "session_id", "snippet", "score"}
    assert h["type"] == "decision" and h["session_id"] == "alpha"
    assert h["snippet"] == "[decision] 2026-01-01T00:00:00Z chose pg"
    assert h["score"] == 0.75
    assert col.where is None, "an unfiltered query must not send a where clause"


def test_a_type_filter_reaches_the_query(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    col = _FakeEventCollection()
    _stub_events_backend(monkeypatch, col, [])
    vectors.semantic_recall_events("x", types=["decision", "caveat"])
    assert col.where == {"type": {"$in": ["decision", "caveat"]}}


def test_a_failing_query_returns_no_hits_rather_than_raising(tmp_path,
                                                             monkeypatch):
    _isolate(tmp_path, monkeypatch)

    class _Angry(_FakeEventCollection):
        def query(self, **kw):
            raise RuntimeError("no")

    _stub_events_backend(monkeypatch, _Angry(), [])
    assert vectors.semantic_recall_events("x") == []


def test_the_index_count_is_zero_rather_than_an_exception(tmp_path, monkeypatch):
    """It is called to DECIDE whether the semantic path is worth taking, and a
    check that can fail is a check every caller has to wrap."""
    _isolate(tmp_path, monkeypatch)

    def _boom():
        raise RuntimeError("no deps")

    monkeypatch.setattr(vectors, "_require_deps", _boom)
    assert vectors.indexed_event_count() == 0


# --- cosine space ------------------------------------------------------------

class _FakeClient:
    def __init__(self, space):
        self.space = space
        self.deleted = []
        self.created = []

    def get_or_create_collection(self, name, metadata=None):
        self.created.append((name, metadata))
        return _FakeEventCollection(space=self.space)

    def delete_collection(self, name):
        self.deleted.append(name)
        self.space = "cosine"


def test_a_collection_is_asked_for_cosine_space(monkeypatch):
    """chroma defaults to squared L2; all-MiniLM-L6-v2 is a cosine model. The
    mismatch is what made `1 - distance` come back NEGATIVE.

    The space now travels with M and the ef parameters (blueprint P1h, K20);
    `tests/test_vectors_v1.py` is where the rest of the set is pinned."""
    client = _FakeClient("cosine")
    monkeypatch.setattr(vectors, "_client", lambda project: client)
    vectors._open(None, "events")
    assert client.created == [("events", dict(vectors.HNSW_METADATA))]
    assert client.created[0][1]["hnsw:space"] == "cosine"
    assert client.deleted == []


def test_a_wrong_space_collection_is_rebuilt_only_where_it_will_be_refilled(
        monkeypatch):
    """chroma IGNORES the metadata argument for a collection that already
    exists, so one created before the fix keeps L2 forever unless it is dropped.
    Dropping is only safe where the caller is about to refill it — a query path
    takes what is there, because wrong scores beat no results."""
    client = _FakeClient("l2")
    monkeypatch.setattr(vectors, "_client", lambda project: client)

    vectors._open(None, "events", refill=False)
    assert client.deleted == [], "a query path destroyed the index"

    col = vectors._open(None, "events", refill=True)
    assert client.deleted == ["events"]
    assert col.metadata["hnsw:space"] == "cosine"
