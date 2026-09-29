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

    def count(self):
        return len(self.added.get("ids") or [])


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
        self.matching = None        # rows a `where` matches; None: every row
        self.probed = []            # (where, limit) of each `get` by filter

    def set_query_result(self, docs, ids, distances, metadatas):
        self._query_result = {"documents": [docs], "ids": [ids],
                              "distances": [distances], "metadatas": [metadatas]}

    def get(self, ids=None, where=None, limit=None):
        if self.get_raises:
            raise RuntimeError("chroma is unhappy")
        if ids is not None:
            _refuse_repeats(ids, "get")
            return {"ids": [i for i in ids if i in self.present]}
        self.probed.append((where, limit))
        rows = sorted(self.present if self.matching is None else self.matching)
        return {"ids": rows[:limit] if limit else rows}

    def add(self, ids=None, documents=None, embeddings=None, metadatas=None):
        _refuse_repeats(ids or [], "add")
        self.batches.append({"ids": list(ids or []),
                             "documents": list(documents or []),
                             "metadatas": list(metadatas or [])})
        self.present.update(ids or [])

    def query(self, query_embeddings=None, n_results=10, where=None):
        self.where = where
        return self._query_result

    def count(self):
        return len(self.present)


def _refuse_repeats(ids, op):
    """chroma's own rule, kept by the fake: an id named twice in one `get` or
    one `add` raises DuplicateIDError before anything is read or written."""
    seen = set()
    for i in ids:
        if i in seen:
            raise RuntimeError(
                f"Expected IDs to be unique, found duplicates of: {i} in {op}")
        seen.add(i)


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


def test_a_row_the_ledger_holds_twice_is_indexed_once(tmp_path, monkeypatch):
    """A hook that fires twice appends byte-identical rows a line apart, and
    content addressing gives both one id. chroma refuses an id named twice in
    one `get` AND in one `add`, so the first `hive embed` after such a row
    died mid-batch -- on the merged code AFTER the rebuild had dropped the
    collection (seen on a copy of the live store: one `decision` appended
    twice at the same second). The second copy would embed to the same
    vector, so it is skipped, and counted as skipped."""
    _isolate(tmp_path, monkeypatch)
    col = _FakeEventCollection()
    twin = _ev(text="landed the mirror")
    _stub_events_backend(monkeypatch, col, [
        twin, twin, _ev(text="another memo", ts="2026-01-01T00:00:01Z")])

    out = vectors.embed_events()

    assert out == {"scanned": 3, "eligible": 3, "embedded": 2, "skipped": 1}
    ids = [i for b in col.batches for i in b["ids"]]
    assert len(ids) == len(set(ids)) == 2
    assert vectors.embed_events()["embedded"] == 0, "the twin re-embedded"


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
# writer, a full temp dir or a missing package had made unreadable. Where the
# files are damaged, what broke picks the repair: `hive embed --rebuild` asks
# chromadb for `ledger` and then `events` by name and drops each unread, so it
# mends a collection that opened and then failed, and not files chromadb
# cannot open at all.

from mindhive import vector_errors, vector_snapshot   # noqa: E402


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


def test_a_failed_count_is_rebuilt_first_and_then_names_the_directory(
        tmp_path, monkeypatch):
    """A count that failed read a collection chromadb could open, and
    `hive embed --rebuild` drops such a collection without reading it:
    measured, it repaired a garbage header, a cut pickle, a missing segment
    and a damaged sequence number, each of which had failed the count. The
    delete follows, for a read the rebuild did not mend."""
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _Angry(), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.indexed_event_count(strict=True)
    msg = str(err.value)
    ns = paths.project_dir().name
    assert f"hive embed --rebuild --project {ns}" in msg
    assert f"delete {paths.project_dir() / 'chroma'}" in msg
    assert f"hive embed --project {ns}" in msg, \
        "the repair does not say which namespace `hive embed` must run in"
    assert msg.index("--rebuild") < msg.index("delete")


def test_an_index_chromadb_cannot_open_is_deleted_not_rebuilt(tmp_path,
                                                               monkeypatch):
    """`hive embed --rebuild` needs the index files open too. Measured: on a
    `chroma.sqlite3` that is not a database it failed at its first open, and
    on an `events` collection whose configuration chromadb could not
    deserialize -- which fails every read, since a read lists every
    collection -- it rebuilt `ledger` and then failed on reaching `events`.
    The delete is the repair that reaches such files."""
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])
    monkeypatch.setattr(vectors, "_reading",
                        _refusing(RuntimeError("file is not a database")))
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x")
    msg = str(err.value)
    assert err.value.stage == "open" and "--rebuild" not in msg
    assert f"delete {paths.project_dir() / 'chroma'}" in msg
    assert "hive embed --project test" in msg


def _cut(owner):
    """The header check's refusal of a segment of collection *owner*."""
    return vector_snapshot.DamagedSegment(
        "HNSW segment 5c5cc466: header.bin is 7 bytes, a whole one is 100",
        collection=owner)


def test_a_cut_segment_is_repaired_as_its_collection_is(tmp_path,
                                                          monkeypatch):
    """The header check refuses a read of the collection whose segment is
    cut, before chromadb reads it. A cut `ledger` or `events` segment is
    rebuilt -- the rebuild drops it unread -- and a cut segment of the
    watcher's collection is given the one repair that reaches it, the
    delete."""
    _isolate(tmp_path, monkeypatch)
    watcher = vectors.collection_name("test")
    _stub_backend(monkeypatch, _FakeCollection())
    monkeypatch.setattr(vectors, "_reading",
                        _refusing(_cut(vectors.LEDGER_COLLECTION)))
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall("x")
    msg = str(err.value)
    assert (err.value.stage, err.value.collection) == (
        "open", vectors.LEDGER_COLLECTION)
    assert "hive embed --rebuild --project test" in msg
    assert "watcher's collection" not in msg
    for coll, rebuilt in ((vectors.EVENT_COLLECTION, True), (watcher, False)):
        msg = str(vectors.SemanticQueryError("open", coll, _cut(coll), "test"))
        assert ("hive embed --rebuild --project test" in msg) is rebuilt, msg
        assert (f"{watcher!r} is the watcher's collection" in msg) \
            is not rebuilt, msg
        assert f"delete {paths.project_dir() / 'chroma'}" in msg


def test_a_cut_segment_no_catalogue_names_is_deleted(tmp_path, monkeypatch):
    """A copy whose `chroma.sqlite3` could not be read cannot say whose cut
    segment it holds -- and a rebuild reads that file before anything else."""
    _isolate(tmp_path, monkeypatch)
    msg = str(vectors.SemanticQueryError(
        "open", vectors.EVENT_COLLECTION, _cut(None), "test"))
    assert "--rebuild" not in msg
    assert f"delete {paths.project_dir('test') / 'chroma'}" in msg


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
    # A client that answered is sound, whatever its answer lacked.
    for answered in (vector_errors.MissingDocument("hit 'b' has no text"),
                     vector_errors.UnansweredQuery("no hits from 2 rows")):
        assert not vectors._judge(_failed("query", answered)), answered


def test_an_overflowing_argument_is_not_claimed_as_the_clients_failure():
    """An `n_results` past 2**32 - 1 raises OverflowError in Python, before
    the index is touched. Claimed, it poisoned a HEALTHY index: its client
    kept for good and every later read of it refused for 600 s."""
    assert not vectors._judge(_failed("query", OverflowError(
        "out of range integral type conversion attempted")))


def test_an_id_named_twice_is_refused_and_a_query_of_the_wrong_width_rebuilt(
        tmp_path, monkeypatch):
    """chromadb refuses a request naming an id twice in Python, before it
    reads anything (`validate_ids`): the index is not at fault. A query
    embedding whose width is not the collection's is another matter -- the
    collection was built by another model, and `hive embed --rebuild`
    re-encodes it with this one. chromadb 1.5.9 raises that as
    InvalidArgumentError, the class it also raises for a log it cannot read;
    an internal error is how a damaged index presents. Both are claimed."""
    errors = pytest.importorskip("chromadb.errors")
    _isolate(tmp_path, monkeypatch)
    assert not vectors._judge(_failed("query", errors.DuplicateIDError(
        "Expected IDs to be unique, found duplicates of: s1:5")))
    for cause in (errors.InvalidArgumentError(
                      "Collection expecting embedding with dimension of 8, "
                      "got 384"),
                  errors.InvalidDimensionException(
                      "Embedding dimension 3 does not match collection "
                      "dimensionality 384")):
        err = _failed("query", cause)
        assert vectors._judge(err), type(cause).__name__
        assert "hive embed --rebuild --project test" in str(err)
        assert "not at fault" not in str(err)
    assert vectors._judge(_failed("query", errors.InternalError(
        "Error loading hnsw index")))


_LOG_DAMAGE = ("Error executing plan: Error sending backfill request to "
               "compactor: Failed to pull logs from the log store")


@pytest.mark.parametrize("stage", ["count", "query"])
def test_a_log_chromadb_cannot_read_is_damage_to_repair(tmp_path, monkeypatch,
                                                        stage):
    """chromadb 1.5.9 raises InvalidArgumentError for a log it cannot read:
    15 of 30 damaged-page seeds, at `count` -- which takes no arguments --
    and at `query`. Classed by its type as an argument chromadb refused, the
    message said the index was not at fault, named no repair, and the client
    that failed stayed in service."""
    errors = pytest.importorskip("chromadb.errors")
    _isolate(tmp_path, monkeypatch)
    cause = errors.InvalidArgumentError(_LOG_DAMAGE)
    _stub_events_backend(monkeypatch, _Angry(cause), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        if stage == "count":
            vectors.indexed_event_count(strict=True)
        else:
            vectors.semantic_recall_events("x")
    msg = str(err.value)
    assert err.value.stage == stage and err.value.__cause__ is cause
    assert "not at fault" not in msg
    assert f"delete {paths.project_dir() / 'chroma'}" in msg
    assert vectors._judge(err.value)


def test_no_argument_is_refused_by_a_step_that_takes_none(tmp_path,
                                                         monkeypatch):
    """`open` and `count` are asked nothing a caller chose, so what they
    raise is the index's to answer for -- a ValueError included."""
    _isolate(tmp_path, monkeypatch)
    for stage in ("open", "count"):
        for cause in (ValueError("x"), TypeError("x"), OverflowError("x")):
            err = _failed(stage, cause)
            assert vectors._judge(err), (stage, cause)
            assert "not at fault" not in str(err) and "delete" in str(err)


def test_an_index_remembered_as_unreadable_reports_its_failure(tmp_path,
                                                               monkeypatch):
    """Refused for bytes that failed, a read is mended by the rebuild, which
    changes them. Refused past the cap, no collection of the index is read
    again until the process restarts: the advice was the rebuild and then,
    since the read still failed -- as it must, capped -- the delete of an
    index the rebuild had just mended. The restart comes before the delete."""
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])
    first = _failed("count", RuntimeError("Failed to apply logs"))
    for capped in (False, True):
        monkeypatch.setattr(vectors, "_reading", _refusing(
            vector_snapshot.IndexUnreadable("src", "events", first,
                                            capped=capped)))
        with pytest.raises(vectors.SemanticQueryError) as err:
            vectors.indexed_event_count(strict=True)
        msg = str(err.value)
        assert err.value.stage == "count"
        assert "Failed to apply logs" in msg
        assert ("restart this process" in msg) is capped
        assert ("if a read after the restart still fails" in msg) is capped
        at = msg.index("rebuild it with `hive embed --rebuild --project test`")
        if capped:
            assert at < msg.index("restart this process") < msg.index("delete")
        else:
            assert "if the read still fails, delete" in msg


def test_a_capped_index_chromadb_cannot_open_is_deleted_then_restarted(
        tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])
    first = _failed("open", RuntimeError("file is not a database"))
    monkeypatch.setattr(vectors, "_reading", _refusing(
        vector_snapshot.IndexUnreadable("src", "events", first, capped=True)))
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.indexed_event_count(strict=True)
    msg = str(err.value)
    assert err.value.stage == "open" and "--rebuild" not in msg
    assert msg.index(f"delete {paths.project_dir() / 'chroma'}") < msg.index(
        "then restart this process")


def test_a_capped_refusal_is_reported_on_the_collection_that_failed(
        tmp_path, monkeypatch):
    """Past the cap, a read of `events` is refused for a sibling's failure --
    here the watcher's collection. Reported as the `events` index, its
    repair led with `hive embed --rebuild`, which never writes the
    collection that failed."""
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _FakeEventCollection(), [])
    watcher = vectors.collection_name("test")
    cause = RuntimeError("Error loading hnsw index")
    first = vectors.SemanticQueryError("query", watcher, cause)
    first.__cause__ = cause
    monkeypatch.setattr(vectors, "_reading", _refusing(
        vector_snapshot.IndexUnreadable("src", vectors.EVENT_COLLECTION, first,
                                        capped=True, of=watcher)))
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.indexed_event_count(strict=True)
    msg = str(err.value)
    assert (err.value.stage, err.value.collection) == ("query", watcher)
    assert "--rebuild" not in msg and "watcher's collection" in msg
    assert msg.index("delete") < msg.index("then restart this process")


def test_a_quarantined_namespace_is_repaired_the_way_it_was_selected(
        tmp_path, monkeypatch):
    """A project `paths.namespace_name` quarantines -- a name starting
    `unsafe-`, on Windows a device name -- is stored as `unsafe-<hash>`. The
    repair named `hive embed --project unsafe-<hash>`, which `--project`
    refuses (exit 2) as it refuses the name the project was stored under: no
    `--project` reaches that namespace. `hive embed` run the way the project
    was selected does. Every other namespace keeps its verbatim `--project`,
    and every command advised for one parses."""
    import argparse
    import re
    from mindhive import cliparse
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "store"))
    for project, quarantined in (("unsafe-code-guidelines", True),
                                 ("plain", False)):
        monkeypatch.setenv("MINDHIVE_PROJECT", project)
        ns = paths.project_dir().name
        assert ns.startswith("unsafe-") is quarantined, ns
        for stage in ("open", "count", "query"):
            for coll in (vectors.EVENT_COLLECTION, vectors.collection_name("x")):
                msg = str(vectors.SemanticQueryError(
                    stage, coll, RuntimeError("Error loading hnsw index")))
                advised = re.findall(r"`(hive [^`]*)`", msg)
                assert any(a.startswith("hive embed") for a in advised), msg
                for command in advised:
                    for name in re.findall(r"--project (\S+)", command):
                        assert cliparse.project_arg(name) == name, command
                assert ("MINDHIVE_PROJECT" in msg) is quarantined, msg
                assert ("--project" in " ".join(advised)) is not quarantined
    for name in ("unsafe-code-guidelines", paths.project_dir(
            "unsafe-code-guidelines").name):
        with pytest.raises(argparse.ArgumentTypeError):
            cliparse.project_arg(name)


# --- a panic in chromadb's Rust core is a failed read --------------------------
#
# chromadb 1.5.9 panics in Rust on a malformed SQLite page, and pyo3 raises the
# panic as `pyo3_runtime.PanicException` -- a BaseException, not an Exception.
# Measured on damaged scratch indexes (2 of 30 page-damage seeds, 5 of 40), it
# went past every `except Exception` of the read path: `hive recall --semantic`
# printed a raw traceback, HIVE MIND lost every other namespace's hits, a count
# for display raised, and the client that panicked stayed in service.

def _panic(message="Expect it to be fetched: Database(SqliteError { code: "
                   "11, message: \"database disk image is malformed\" })"):
    """What pyo3 raises for a panic. Every pyo3 extension builds its own
    class, so it is known by its module and name."""
    panic = type("PanicException", (BaseException,),
                 {"__module__": "pyo3_runtime"})
    return panic(message)


class _PanickyClient(_ReadClient):
    """A client whose collection lookup panics, as `list_collections` did."""

    def __init__(self, error):
        super().__init__({})
        self.error = error

    def list_collections(self):
        raise self.error


def _reading_per_project(clients):
    """A read seam yielding `clients[project]`."""
    from contextlib import contextmanager

    @contextmanager
    def _reading(project=None, name=None):
        yield clients[project]

    return _reading


@pytest.mark.parametrize("at", ["open", "lookup", "count", "query"])
def test_a_panic_in_chromadb_is_a_failed_read(tmp_path, monkeypatch, at):
    _isolate(tmp_path, monkeypatch)
    boom = _panic()
    _stub_events_backend(monkeypatch, _Angry(boom) if at in ("count", "query")
                         else _FakeEventCollection(), [])
    if at == "open":
        monkeypatch.setattr(vectors, "_reading", _refusing(boom))
    elif at == "lookup":
        monkeypatch.setattr(vectors, "_reading",
                            _reading_per_project({None: _PanickyClient(boom)}))
    with pytest.raises(vectors.SemanticQueryError) as err:
        if at == "query":
            vectors.semantic_recall_events("x")
        else:
            vectors.indexed_event_count(strict=True)
    msg = str(err.value)
    assert err.value.stage == ("open" if at == "lookup" else at)
    assert err.value.__cause__ is boom
    assert "PanicException" in msg and "delete" in msg
    assert vectors._judge(err.value), "the client that panicked stays in service"


def test_a_panic_leaves_the_other_namespaces_and_the_display_count_alone(
        tmp_path, monkeypatch, caplog):
    """HIVE MIND answers per namespace by catching `SemanticQueryError`, and a
    panic was not one: one malformed index sank every namespace's hits. A
    count only displayed answers 0 and logs the failure, as for any other."""
    from mindhive import recall
    _isolate(tmp_path, monkeypatch)
    good = _FakeEventCollection(present={"abc"})
    good.set_query_result(docs=["[decision] t chose postgres"], ids=["abc"],
                          distances=[0.25], metadatas=[
                              {"type": "decision", "ts": "t", "session_id": "s"}])
    _stub_events_backend(monkeypatch, good, [])
    monkeypatch.setattr(vectors, "_reading", _reading_per_project({
        "bad": _PanickyClient(_panic()),
        "good": _ReadClient({vectors.EVENT_COLLECTION: good})}))
    rows = recall.recall_across("postgres", ["bad", "good"], semantic=True)
    assert [(r["project"], r.get("id")) for r in rows if "error" not in r] == [
        ("good", "abc")]
    [row] = [r for r in rows if "error" in r]
    assert row["project"] == "bad" and "PanicException" in row["error"]
    with caplog.at_level("WARNING", logger="mindhive.vectors"):
        assert vectors.indexed_event_count("bad") == 0
    assert "PanicException" in caplog.text


class _Silent(_FakeEventCollection):
    """A collection holding rows that answers every query with none of them
    -- measured, what an index whose recorded dimension is NULL does. Its
    count raises *error*, when given one."""

    def __init__(self, error=None):
        super().__init__(present={"a", "b"})
        self.error = error

    def count(self):
        if self.error is not None:
            raise self.error
        return super().count()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("at", ["open", "lookup", "encode", "count",
                                "strict count", "query", "probe"])
def test_an_interrupt_is_not_taken_for_a_failed_read(tmp_path, monkeypatch,
                                                     at, interrupt):
    """Ctrl-C during a search stops the search; it is no index to repair.
    Every step's handler takes BaseException, for chromadb's panics, and
    hands an interrupt back as it is. Wrapped, Ctrl-C while the index was
    copied printed advice to delete a healthy index and exited 1, a HIVE MIND
    search went on to the next namespace, and the copy's client was kept for
    good."""
    _isolate(tmp_path, monkeypatch)
    stop = interrupt()
    col = {"count": _Angry(stop), "strict count": _Angry(stop),
           "query": _Angry(stop), "probe": _Silent(stop)}.get(
               at, _FakeEventCollection())
    _stub_events_backend(monkeypatch, col, [])
    if at == "open":
        monkeypatch.setattr(vectors, "_reading", _refusing(stop))
    elif at == "lookup":
        monkeypatch.setattr(vectors, "_reading",
                            _reading_per_project({None: _PanickyClient(stop)}))
    elif at == "encode":
        def _stopped():
            raise stop
        monkeypatch.setattr(vectors, "_encoder", _stopped)
    with pytest.raises(interrupt) as got:
        if at == "count":
            vectors.indexed_event_count()
        elif at == "strict count":
            vectors.indexed_event_count(strict=True)
        else:
            vectors.semantic_recall_events("x")
    assert got.value is stop


def test_an_interrupt_leaves_the_real_snapshot_holding_no_client(
        tmp_path, monkeypatch):
    """Through `vector_snapshot.reading` itself: an interrupt in the
    collection lookup is released like any read. Claimed as the client's
    failure, it kept that client open for good and refused the index for
    600 s."""
    pytest.importorskip("chromadb")
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(vector_snapshot, "SNAPSHOT_DIR", tmp_path / "snaps")
    for attr, empty in (("_CURRENT", {}), ("_ORPHANS", set()),
                        ("_SWEPT", False), ("_POISONED", []), ("_FAILED", {}),
                        ("_PASSED", {})):
        monkeypatch.setattr(vector_snapshot, attr, empty)
    chroma = paths.project_dir() / "chroma"
    chroma.mkdir(parents=True)
    (chroma / "chroma.sqlite3").write_bytes(b"not a database")
    stop, opened = KeyboardInterrupt(), []
    monkeypatch.setattr(vector_snapshot, "ISOLATE_READS", False)
    monkeypatch.setattr(vector_snapshot, "open_chroma", lambda copy: (
        opened.append(_PanickyClient(stop)) or opened[-1]))
    monkeypatch.setattr(vectors, "_require_deps", lambda: None)
    with pytest.raises(KeyboardInterrupt):
        vectors.indexed_event_count(strict=True)
    assert len(opened) == 1
    assert vector_snapshot._POISONED == [] and vector_snapshot._FAILED == {}
    vector_snapshot.dispose_all()


# --- a query that answers nothing from rows it could have returned -------------
#
# Measured on a scratch index whose `collections.dimension` was NULL: `count`
# answered 1300 and every query answered no hits, with no error. `recall` took
# that for an index with nothing relevant in it and read the file index in
# silence. A query that answers no hits from a collection holding rows it
# could have returned has failed; `hive embed --rebuild` repaired `ledger`
# and `events`, and the watcher's collection is the delete's to repair.

def test_a_query_that_answers_nothing_from_a_full_collection_failed(
        tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _Silent(), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x")
    msg = str(err.value)
    assert (err.value.stage, err.value.collection) == (
        "query", vectors.EVENT_COLLECTION)
    assert isinstance(err.value.__cause__, vector_errors.UnansweredQuery)
    assert "no hits" in msg and "2 rows" in msg
    assert "hive embed --rebuild --project test" in msg
    assert not vectors._judge(err.value), \
        "the client answered, so its copy is not poisoned"


def test_a_file_level_query_that_answers_nothing_from_a_full_index_failed(
        tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    col = _FakeCollection()
    _stub_backend(monkeypatch, col)
    assert vectors.semantic_recall("x") == [], "an empty index is no failure"
    col.added = {"ids": ["a.md"]}
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall("x")
    assert (err.value.stage, err.value.collection) == (
        "query", vectors.LEDGER_COLLECTION)
    assert isinstance(err.value.__cause__, vector_errors.UnansweredQuery)


def test_a_filtered_query_failed_only_where_its_filter_matched_rows(
        tmp_path, monkeypatch):
    """A filter that matches no row answers no hits, and that is an answer.
    Whether one matches is asked of the collection itself, with the same
    filter and for one row."""
    _isolate(tmp_path, monkeypatch)
    col = _Silent()
    _stub_events_backend(monkeypatch, col, [])
    col.matching = set()
    assert vectors.semantic_recall_events("x", types=["caveat"]) == []
    col.matching = {"b"}
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x", types=["caveat"])
    assert isinstance(err.value.__cause__, vector_errors.UnansweredQuery)
    assert col.probed == [({"type": {"$in": ["caveat"]}}, 1)] * 2


def test_a_probe_that_fails_is_a_failed_count(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    boom = RuntimeError("Error loading hnsw index")
    _stub_events_backend(monkeypatch, _Silent(boom), [])
    with pytest.raises(vectors.SemanticQueryError) as err:
        vectors.semantic_recall_events("x")
    assert err.value.stage == "count" and err.value.__cause__ is boom
    assert vectors._judge(err.value)


def test_recall_never_takes_an_unanswered_query_for_an_empty_index(
        tmp_path, monkeypatch):
    """The file index is `recall`'s answer to an EMPTY event index, never to
    one that failed."""
    from mindhive import recall
    _isolate(tmp_path, monkeypatch)
    _stub_events_backend(monkeypatch, _Silent(), [])

    def _files(*_a, **_kw):
        raise AssertionError("recall fell back to the file index")

    monkeypatch.setattr(vectors, "semantic_recall", _files)
    with pytest.raises(vectors.SemanticQueryError) as err:
        recall.recall("x", semantic=True)
    assert isinstance(err.value.__cause__, vector_errors.UnansweredQuery)


# --- a hit chromadb returns without its text -----------------------------------
#
# chromadb answers None for a stored document it cannot decode. Measured on a
# scratch index whose `chroma:document` rows held invalid UTF-8: every `ledger`
# and `events` hit came back without its text, and building the snippet raised
# AttributeError -- no SemanticQueryError, so `hive recall --semantic` printed
# a raw traceback and a HIVE MIND search lost every other namespace's hits.

def _hitless_text(monkeypatch, coll):
    """A copy whose second hit has no document; the read seam records any
    exception that passes through it, which is what `vector_snapshot` judges."""
    from contextlib import contextmanager
    if coll == vectors.LEDGER_COLLECTION:
        col = _FakeCollection()
        col.set_query_result(docs=["use postgres", None], ids=["a.md", "b.md"])
        _stub_backend(monkeypatch, col)
        read = lambda: vectors.semantic_recall("postgres")       # noqa: E731
    else:
        col = _FakeEventCollection()
        col.set_query_result(docs=["[decision] t use postgres", None],
                             ids=["a", "b"], distances=[0.1, 0.2],
                             metadatas=[{"type": "decision"}] * 2)
        _stub_events_backend(monkeypatch, col, [])
        read = lambda: vectors.semantic_recall_events("postgres")  # noqa: E731
    judged = []

    @contextmanager
    def _reading(project=None, name=None):
        try:
            yield _ReadClient({coll: col})
        except BaseException as e:
            judged.append(e)
            raise

    monkeypatch.setattr(vectors, "_reading", _reading)
    return read, judged


@pytest.mark.parametrize("coll", ["ledger", "events"])
def test_a_hit_without_its_text_is_a_failed_read(tmp_path, monkeypatch, coll):
    """Reported as the query that failed, naming the hit, with the repair
    that re-writes every document: `hive embed --rebuild`. The client
    answered, so the failure is raised after it is handed back -- never
    judged its own, never poisoned: a copy with one undecodable row still
    answers every query that does not reach it."""
    _isolate(tmp_path, monkeypatch)
    read, judged = _hitless_text(monkeypatch, coll)
    with pytest.raises(vectors.SemanticQueryError) as err:
        read()
    msg = str(err.value)
    assert (err.value.stage, err.value.collection) == ("query", coll)
    assert "'b" in msg and "without its document" in msg, msg
    assert "hive embed --rebuild --project test" in msg
    assert "not at fault" not in msg
    assert judged == []


def test_a_hit_without_its_text_is_one_namespace_of_a_hive_mind_search(
        tmp_path, monkeypatch):
    from mindhive import recall
    _isolate(tmp_path, monkeypatch)
    bad = _FakeEventCollection(present={"x"})
    bad.set_query_result(docs=[None], ids=["x"], distances=[0.1],
                         metadatas=[{"type": "decision"}])
    good = _FakeEventCollection(present={"abc"})
    good.set_query_result(docs=["[decision] t chose postgres"], ids=["abc"],
                          distances=[0.25], metadatas=[{"type": "decision"}])
    _stub_events_backend(monkeypatch, good, [])
    monkeypatch.setattr(vectors, "_reading", _reading_per_project({
        "bad": _ReadClient({vectors.EVENT_COLLECTION: bad}),
        "good": _ReadClient({vectors.EVENT_COLLECTION: good})}))
    rows = recall.recall_across("postgres", ["bad", "good"], semantic=True)
    assert [(r["project"], r.get("id")) for r in rows if "error" not in r] == [
        ("good", "abc")]
    [row] = [r for r in rows if "error" in r]
    assert row["project"] == "bad" and "without its document" in row["error"]


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
