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


class _ReadClient:
    """What `vectors._reading` yields: a client over a private COPY of the
    index. It can look a collection up and nothing else, so a read path that
    tried to create or drop one fails here instead of passing."""

    def __init__(self, cols):
        self.cols = dict(cols)

    def list_collections(self):
        import types
        return [types.SimpleNamespace(name=n) for n in self.cols]

    def get_collection(self, name):
        return self.cols[name]


def _stub_reading(monkeypatch, cols, opened=None):
    """Replace the read seam. *cols* None is "this namespace has no index";
    *opened* collects the project of every read that got as far as opening."""
    from contextlib import contextmanager

    @contextmanager
    def _reading(project=None, name=None):
        if opened is not None:
            opened.append(project)
        yield None if cols is None else _ReadClient(cols)

    monkeypatch.setattr(vectors, "_reading", _reading)


def _refusing(error):
    """A read seam that fails before any client exists, raising *error*."""
    from contextlib import contextmanager

    @contextmanager
    def _reading(project=None, name=None):
        raise error
        yield                                           # pragma: no cover

    return _reading


def _stub_backend(monkeypatch, col):
    """Replace the real deps/encoder/collection with deterministic stand-ins.
    Writes go through `_collection`, reads through `_reading`."""
    monkeypatch.setattr(vectors, "_require_deps", lambda: None)
    monkeypatch.setattr(vectors, "_encoder", lambda: (lambda texts: [[0.0] for _ in list(texts)]))
    monkeypatch.setattr(vectors, "_collection",
                        lambda project=None, **kw: col)
    _stub_reading(monkeypatch, {vectors.LEDGER_COLLECTION: col})


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
    _stub_reading(monkeypatch, {vectors.EVENT_COLLECTION: col})


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


class _Angry(_FakeEventCollection):
    """A collection whose every read raises, the way a corrupt index does."""

    def __init__(self, error=None):
        super().__init__()
        self.error = error or RuntimeError("Error loading hnsw index")

    def query(self, **kw):
        raise self.error

    def count(self):
        raise self.error


def test_a_failing_query_is_an_error_not_an_empty_answer(tmp_path,
                                                         monkeypatch):
    """The finding: a query exception was caught and turned into `[]`, so a
    broken index read exactly like "no semantic hits" to every caller."""
    _isolate(tmp_path, monkeypatch)
    boom = RuntimeError("Error loading hnsw index")
    _stub_events_backend(monkeypatch, _Angry(boom), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x")
    assert err.value.stage == "query"
    assert err.value.collection == vectors.EVENT_COLLECTION
    assert err.value.__cause__ is boom
    assert "hnsw" in str(err.value) and "hive embed --rebuild" in str(err.value)


def test_the_typed_error_is_still_a_runtime_error():
    """Callers that already catch RuntimeError (LIVE_LOCK, missing extras) keep
    catching this one; the type is how the ones that care tell it apart."""
    assert issubclass(vectors.SemanticQueryError, RuntimeError)


def test_a_failing_file_level_query_is_an_error_too(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _stub_backend(monkeypatch, _Angry())
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall("x")
    assert (err.value.stage, err.value.collection) == (
        "query", vectors.LEDGER_COLLECTION)


def test_an_encoder_failure_is_reported_as_one(tmp_path, monkeypatch):
    """A model that will not load is a different repair from a broken index,
    so the stage says which."""
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])

    def _no_model():
        raise OSError("all-MiniLM-L6-v2 is not in the local cache")

    monkeypatch.setattr(vectors, "_encoder", _no_model)
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x")
    assert err.value.stage == "encode"
    assert "model" in str(err.value)


def test_an_index_that_cannot_be_opened_is_reported_at_open(tmp_path,
                                                            monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])
    monkeypatch.setattr(vectors, "_reading",
                        _refusing(RuntimeError("file is not a database")))
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x")
    assert err.value.stage == "open"


def test_a_missing_index_is_empty_and_never_loads_the_model(tmp_path,
                                                            monkeypatch):
    """No index is a real "nothing found", not a failure -- and answering it
    must not cost a 90 MB model load."""
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])
    _stub_reading(monkeypatch, None)

    def _never():
        raise AssertionError("the model was loaded to search no index")

    monkeypatch.setattr(vectors, "_encoder", _never)
    assert vectors.semantic_recall_events("x") == []
    assert vectors.semantic_recall("x") == []
    assert vectors.indexed_event_count(strict=True) == 0


def test_a_read_never_creates_the_collection_it_asks_for(tmp_path,
                                                         monkeypatch):
    """An index that holds the ledger collection but not the events one is
    answered as empty. `_ReadClient` has no `get_or_create_collection`, so a
    read path that minted the collection would fail here."""
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])
    _stub_reading(monkeypatch, {vectors.LEDGER_COLLECTION: _FakeCollection()})
    assert vectors.semantic_recall_events("x") == []
    assert vectors.indexed_event_count(strict=True) == 0


def test_live_lock_refuses_an_event_read_before_the_index_is_opened(
        tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    opened = []
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])
    _stub_reading(monkeypatch, {}, opened)
    paths.store_home().mkdir(parents=True, exist_ok=True)
    (paths.store_home() / "LIVE_LOCK").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="LIVE_LOCK"):
        vectors.semantic_recall_events("x")
    with pytest.raises(RuntimeError, match="LIVE_LOCK"):
        vectors.semantic_recall("x")
    assert opened == [], "the index was opened while V17 was live"


def test_the_index_count_is_zero_rather_than_an_exception(tmp_path, monkeypatch):
    """Without the extras there is no semantic layer to count, and that is
    not a failure of one."""
    _isolate(tmp_path, monkeypatch)

    def _boom():
        raise RuntimeError("no deps")

    monkeypatch.setattr(vectors, "_require_deps", _boom)
    assert vectors.indexed_event_count() == 0
    assert vectors.indexed_event_count(strict=True) == 0


def test_a_broken_index_count_raises_when_strict_and_is_logged_otherwise(
        tmp_path, monkeypatch, caplog):
    """`recall` asks strictly, because it routes on the answer and a 0 that
    meant "broken" sent it to the other index in silence. A caller that only
    DISPLAYS the count may take 0 -- but the failure is logged, never lost."""
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _Angry(), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.indexed_event_count(strict=True)
    assert err.value.stage == "count"
    with caplog.at_level("WARNING", logger="mindhive.vectors"):
        assert vectors.indexed_event_count() == 0
    assert "count" in caplog.text and "hnsw" in caplog.text


# --- the error says what to do, and the advice fits the failure ---------------
#
# One remedy for every stage told an operator to rebuild an index that a busy
# writer, a full temp dir or a missing package had made unreadable -- and
# `--rebuild` itself opens the live index, so on a damaged one it is not a
# repair at all.

from mindhive import vector_snapshot                  # noqa: E402


def _snapshot_failure(tmp_path, monkeypatch, reason):
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])
    monkeypatch.setattr(vectors, "_reading", _refusing(
        vector_snapshot.SnapshotError(f"could not copy ({reason})",
                                      reason=reason)))
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x")
    assert err.value.stage == "snapshot"
    return str(err.value)


def test_a_busy_writer_is_reported_as_one(tmp_path, monkeypatch):
    msg = _snapshot_failure(tmp_path, monkeypatch, "busy")
    assert "retry when" in msg and "--rebuild" not in msg


def test_a_temp_dir_that_cannot_hold_a_copy_is_not_called_busy(tmp_path,
                                                               monkeypatch):
    msg = _snapshot_failure(tmp_path, monkeypatch, "space")
    assert "temp" in msg and "retry when" not in msg and "--rebuild" not in msg
    # ...nor handed the copy advice: freeing space mends neither a read-only
    # temp directory nor one that cannot take a directory at all.
    assert "full or not writable" in msg and "free space there" not in msg


def test_a_copy_that_failed_says_to_free_space(tmp_path, monkeypatch):
    msg = _snapshot_failure(tmp_path, monkeypatch, "copy")
    assert "free space" in msg and "--rebuild" not in msg


def test_a_damaged_index_names_the_directory_to_delete(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _Angry(), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.indexed_event_count(strict=True)
    msg = str(err.value)
    assert str(paths.project_dir() / "chroma") in msg
    assert "hive embed" in msg and "--rebuild" not in msg
    assert f"hive embed --project {paths.project_dir().name}" in msg, \
        "the repair does not say which namespace `hive embed` must run in"


def test_a_failed_query_still_offers_the_rebuild_and_then_the_delete(
        tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _Angry(), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x")
    msg = str(err.value)
    assert "hive embed --rebuild" in msg
    assert str(paths.project_dir() / "chroma") in msg


def test_the_error_names_the_project_and_how_to_select_it(tmp_path,
                                                          monkeypatch):
    """`hive embed` repairs whichever namespace it is pointed at, so advice
    that does not say which one repairs the wrong index as often as not."""
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _Angry(), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x", project="other")
    msg = str(err.value)
    assert "'other'" in msg and "hive embed --rebuild --project other" in msg
    assert str(paths.project_dir("other") / "chroma") in msg


def test_the_repair_selects_the_namespace_verbatim(tmp_path, monkeypatch):
    """The advice used to be `MINDHIVE_PROJECT=<ns>`, and `hive embed` reads
    that variable through `paths.normalize`: for `my_proj` it embedded
    `my-proj`, a different namespace, and left the broken index as it was.
    `--project` takes the name as it is -- or refuses it out loud."""
    from mindhive.commands.parsers import _build_parser
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _Angry(), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.indexed_event_count("my_proj", strict=True)
    msg = str(err.value)
    assert "hive embed --project my_proj" in msg
    assert "MINDHIVE_PROJECT" not in msg
    assert paths.normalize("my_proj") != "my_proj", "the case no longer bites"
    assert _build_parser().parse_args(
        ["embed", "--project", "my_proj"]).project == "my_proj"


def test_a_search_for_no_hits_asks_the_index_nothing(tmp_path, monkeypatch):
    """`hive recall --semantic --limit 0` reached chromadb, which refused the
    limit, and the refusal came back as a broken index to rebuild and then
    delete. A limit below 1 is `[]` before anything is opened -- the answer
    `PgVectorIndex.query` gives a top_k of 0."""
    _isolate(tmp_path, monkeypatch)
    opened = []
    _stub_events_backend(monkeypatch, _Angry(), [])
    _stub_reading(monkeypatch, {vectors.EVENT_COLLECTION: _Angry(),
                                vectors.LEDGER_COLLECTION: _Angry()}, opened)
    for limit in (0, -3):
        assert vectors.semantic_recall_events("x", limit=limit) == []
        assert vectors.semantic_recall("x", limit=limit) == []
    assert opened == [], "a search for no hits opened the index"


@pytest.mark.parametrize("cause", [
    TypeError("Number of requested results 0, cannot be negative, or zero."),
    OverflowError("out of range integral type conversion attempted")],
    ids=["type", "overflow"])
def test_an_argument_chromadb_refused_is_not_answered_with_a_repair(
        tmp_path, monkeypatch, cause):
    """chromadb refuses a bad `n_results` before it touches the index. The
    advice to rebuild, then delete, was advice to destroy a healthy index --
    and with `chroma/` the watcher's collection, which nothing refills."""
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _Angry(cause), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x", limit=5)
    msg = str(err.value)
    assert err.value.stage == "query" and err.value.__cause__ is cause
    assert "--rebuild" not in msg and "delete" not in msg
    assert "arguments" in msg and "not at fault" in msg


@pytest.mark.parametrize("stage", ["open", "count", "query"])
def test_a_broken_watcher_collection_is_not_sent_to_hive_embed(
        tmp_path, monkeypatch, stage):
    """`hive embed --rebuild` drops and refills `ledger` and `events` and never
    opens the watcher's `mindhive_<slug>` collection. The advice led with that
    rebuild and then added that `hive embed` does not rebuild this collection:
    two instructions, the first a full re-encode that cannot reach the damage.
    The one repair that does reach it is the delete, and it says what that
    costs."""
    _isolate(tmp_path, monkeypatch)
    name = vectors.collection_name("other")
    msg = str(vectors.SemanticQueryError(
        stage, name, RuntimeError("Error loading hnsw index"), "other"))
    assert "--rebuild" not in msg
    assert f"delete {paths.project_dir('other') / 'chroma'}" in msg
    assert "hive embed --project other" in msg
    assert "watcher" in msg and "appended after" in msg
    assert msg.count("hive embed") == 2, msg     # the repair, and what it skips


def test_a_watcher_collection_note_is_not_added_where_no_repair_is_given(
        tmp_path, monkeypatch):
    """A busy writer, a model that would not load or an argument chromadb
    refused says nothing about the watcher's collection."""
    _isolate(tmp_path, monkeypatch)
    name = vectors.collection_name("other")
    for stage, cause in (("snapshot", vector_snapshot.SnapshotError("busy")),
                         ("encode", RuntimeError("no model")),
                         ("query", TypeError("n_results 0"))):
        msg = str(vectors.SemanticQueryError(stage, name, cause, "other"))
        assert "delete" not in msg and "--rebuild" not in msg, (stage, msg)
        assert "watcher's collection" not in msg, (stage, msg)


def test_missing_extras_are_a_refusal_not_a_broken_index(tmp_path,
                                                         monkeypatch):
    """Without chromadb there is no index to be broken: the read refuses the
    way every other missing-extras path does, rather than advising the
    deletion of a healthy index."""
    import sys
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "chromadb", None)
    with pytest.raises(RuntimeError, match="needs extras") as err:
        with vectors._read_view(None, vectors.EVENT_COLLECTION):
            pass
    assert not isinstance(err.value, vectors.SemanticQueryError)


def _failed(stage, cause):
    err = vectors.SemanticQueryError(stage, vectors.EVENT_COLLECTION, cause)
    err.__cause__ = cause
    return err


def test_the_judge_claims_what_the_chroma_client_failed_at_and_no_more():
    """A claimed failure keeps its client for good, so the claim is narrow:
    the stages that ran inside chromadb, minus an argument chromadb refused
    in Python before touching the index."""
    inner = RuntimeError("Failed to apply logs to the hnsw segment writer")
    for stage in ("open", "count", "query"):
        assert vectors._judge(_failed(stage, inner)), stage
    for stage in ("encode", "snapshot"):
        assert not vectors._judge(_failed(stage, inner)), stage
    assert not vectors._judge(_failed("query", ValueError("n_results < 1")))
    assert not vectors._judge(inner)


def test_an_overflowing_argument_is_not_claimed_as_the_clients_failure():
    """An `n_results` past 2**32 - 1 raises OverflowError in Python, before
    the index is touched. Claimed, it poisoned a HEALTHY index: its client
    kept for good and every later read of it refused for 600 s."""
    assert not vectors._judge(_failed("query", OverflowError(
        "out of range integral type conversion attempted")))


def test_chromadbs_own_argument_errors_are_not_claimed_either():
    """chromadb validates a request -- the arguments, the embedding's
    dimension -- before it reads anything; an internal error is how a
    damaged index presents, and it stays claimed."""
    errors = pytest.importorskip("chromadb.errors")
    for cause in (errors.InvalidArgumentError("n_results must be positive"),
                  errors.InvalidDimensionException(
                      "Embedding dimension 3 does not match collection "
                      "dimensionality 384"),
                  # `validate_ids`, before anything is read: an id named twice
                  errors.DuplicateIDError(
                      "Expected IDs to be unique, found duplicates of: s1:5")):
        assert not vectors._judge(_failed("query", cause)), type(cause).__name__
    assert vectors._judge(_failed("query", errors.InternalError(
        "Error loading hnsw index")))


def test_an_index_remembered_as_unreadable_reports_its_failure(tmp_path,
                                                               monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])
    first = _failed("count", RuntimeError("Failed to apply logs"))
    for capped in (False, True):
        monkeypatch.setattr(vectors, "_reading", _refusing(
            vector_snapshot.IndexUnreadable("src", "events", first,
                                            capped=capped)))
        with pytest.raises(vectors.SemanticQueryError) as err:
            vectors.indexed_event_count(strict=True)
        assert err.value.stage == "count"
        assert "Failed to apply logs" in str(err.value)
        assert ("restart" in str(err.value)) is capped


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
