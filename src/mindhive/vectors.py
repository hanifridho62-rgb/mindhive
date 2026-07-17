# src/mindhive/vectors.py
import logging

from . import paths, guard

_log = logging.getLogger("mindhive.vectors")
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
    # Best-effort delete-before-add so a reembed refreshes rather than
    # duplicates. On the first embed the ids don't exist yet; tolerate that but
    # surface any real failure instead of hiding it (a silently-failed delete
    # could otherwise leave stale vectors behind).
    try:
        col.delete(ids=ids)
    except Exception as e:  # noqa: BLE001 — best-effort, but logged not swallowed
        _log.warning("vectors: delete-before-reembed failed for %s ids in %s: %s",
                     len(ids), project, e)
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
    # Surface the similarity so callers can rank and drop weak matches. chroma
    # returns a distance (lower = closer); score = 1 - distance. Some configs
    # omit distances entirely — fall back to None rather than crash.
    dist_outer = res.get("distances") or [[]]
    dists = dist_outer[0] if dist_outer else []

    def _score(i):
        if i < len(dists) and dists[i] is not None:
            return round(1.0 - dists[i], 6)
        return None

    return [{"file": ids[i], "snippet": " ".join(d.split())[:200], "score": _score(i)}
            for i, d in enumerate(docs)]
