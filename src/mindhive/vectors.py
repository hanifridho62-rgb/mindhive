# src/mindhive/vectors.py
from . import paths, guard

_MODEL = None


def _require_deps():
    try:
        import chromadb  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "semantic layer needs extras: pip install -e .[vectors]"
        ) from e


def _encoder():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    return lambda texts: _MODEL.encode(list(texts)).tolist()


def _collection(project):
    import chromadb
    client = chromadb.PersistentClient(path=str(paths.project_dir(project) / "chroma"))
    return client.get_or_create_collection("ledger")


def embed(project=None, force=False) -> dict:
    guard.ensure_safe(force=force)
    _require_deps()
    ldir = paths.project_dir(project) / "LEDGER"
    files = sorted(ldir.glob("*.md")) if ldir.exists() else []
    if not files:
        return {"embedded": 0}
    enc = _encoder()
    col = _collection(project)
    ids = [f.name for f in files]
    docs = [f.read_text(encoding="utf-8") for f in files]
    try:
        col.delete(ids=ids)
    except Exception:
        pass
    col.add(ids=ids, documents=docs, embeddings=enc(docs))
    return {"embedded": len(files)}


def semantic_recall(query, project=None, limit=10, force=False):
    guard.ensure_safe(force=force)
    _require_deps()
    enc = _encoder()
    col = _collection(project)
    res = col.query(query_embeddings=enc([query]), n_results=limit)
    docs = res.get("documents", [[]])[0]
    ids = res.get("ids", [[]])[0]
    return [{"file": ids[i], "snippet": " ".join(d.split())[:200]}
            for i, d in enumerate(docs)]
