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
        self._query_result = {"documents": [[]], "ids": [[]]}

    def set_query_result(self, docs, ids):
        self._query_result = {"documents": [docs], "ids": [ids]}

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
    monkeypatch.setattr(vectors, "_collection", lambda project=None: col)


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
                         ids=["20260101-000000-decision-abcd1234.md"])
    _stub_backend(monkeypatch, col)
    hits = vectors.semantic_recall("hello", limit=1)
    assert len(hits) == 1
    h = hits[0]
    assert set(h.keys()) == {"file", "snippet"}     # same keys as recall.recall
    assert h["file"] == "20260101-000000-decision-abcd1234.md"
    assert "/" not in h["file"] and "\\" not in h["file"]
    assert h["snippet"] == "hello world doc"        # whitespace collapsed
