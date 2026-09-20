# src/mindhive/vectors.py
import logging

from . import paths, guard

_log = logging.getLogger("mindhive.vectors")
_MODEL = None


def has_vectors_support() -> bool:
    """True when both packages the semantic layer needs are installed.

    Asks `importlib.util.find_spec` and imports neither: `chromadb` and
    `sentence_transformers` take seconds to import, and the answer is wanted on
    the digest path (`adaptivetail.rollover_notice`), which must stay cheap.
    Total: a spec lookup that raises reads as unavailable."""
    import importlib.util
    try:
        return all(importlib.util.find_spec(name) is not None
                   for name in ("chromadb", "sentence_transformers"))
    except Exception:
        return False


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


# all-MiniLM-L6-v2 is a COSINE model, and chroma defaults to squared L2. The
# mismatch is not academic: with L2, `1 - distance` — the score every caller
# ranks and thresholds on — comes back NEGATIVE for perfectly good matches
# (measured: -0.128 for the best hit on a real query), so any "drop weak
# results" rule silently drops everything. Cosine puts distance in [0, 2] and
# the score back in [-1, 1], where 1.0 means identical and the number means what
# a reader assumes it means.
_SPACE = {"hnsw:space": "cosine"}


def _client(project):
    import chromadb
    return chromadb.PersistentClient(
        path=str(paths.project_dir(project) / "chroma"))


def _open(project, name, *, refill=False):
    """Get one collection, in cosine space.

    chroma ignores the metadata argument when a collection already exists, so a
    collection created before this fix keeps its L2 space forever unless it is
    dropped. Dropping is only safe where the caller is about to refill it, hence
    `refill`: query paths take whatever is there (wrong scores beat no results),
    embed paths correct it first."""
    client = _client(project)
    col = client.get_or_create_collection(name, metadata=_SPACE)
    if refill and (col.metadata or {}).get("hnsw:space") != "cosine":
        _log.warning("vectors: rebuilding %r — it was created in %r space, "
                     "which makes similarity scores negative", name,
                     (col.metadata or {}).get("hnsw:space", "l2"))
        client.delete_collection(name)
        col = client.get_or_create_collection(name, metadata=_SPACE)
    return col


def _collection(project, *, refill=False):
    return _open(project, "ledger", refill=refill)


def embed(project=None, force=False) -> dict:
    guard.ensure_safe(force=force)
    _require_deps()
    ldir = paths.project_dir(project) / "LEDGER"
    files = sorted(ldir.glob("*.md")) if ldir.exists() else []
    if not files:
        return {"embedded": 0}
    enc = _encoder()
    col = _collection(project, refill=True)
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


# --- event-level vectors -----------------------------------------------------
#
# `embed()` above indexes one vector per LEDGER/*.md FILE. That makes the unit
# of retrieval "the decisions file" — a whole projection, of which recall can
# show 200 characters. For "which decision was that", the answer is in there
# somewhere and the snippet almost never contains it.
#
# This half indexes one vector per EVENT, so a hit IS the memo. It is what makes
# `hive recall --semantic` able to answer a specific historical question over
# 37k events instead of pointing at the file the answer lives in.
#
# Two properties that matter more than the embedding:
#
# * INCREMENTAL. Ids are content hashes, so an event already indexed is skipped
#   without being re-encoded. `embed()` re-encodes everything on every run; at
#   event granularity that would be 37k encodes per invocation and nobody would
#   run it twice.
# * SEMANTIC TYPES ONLY. `command_run`, `file_touched` and `assistant_response`
#   are 84% of this ledger and carry no meaning worth a vector — indexing them
#   would triple the cost and bury the memos in near-duplicate telemetry.

EVENT_COLLECTION = "events"

# What "important event" means here. `events.SEMANTIC_TYPES` is the set the
# project already treats as human meaning — decisions, caveats, experiment
# results, next steps, open questions, code changes, learnings, SOPs, roadmap
# and MindNetwork task/phase rows. Deliberately a REFERENCE to that set rather
# than a copy: a new semantic type becomes recallable by being registered once.
def _semantic_types():
    from . import events as _events
    return _events.SEMANTIC_TYPES


def _events_collection(project, *, refill=False):
    return _open(project, EVENT_COLLECTION, refill=refill)


def event_id(ev) -> str:
    """A stable, content-addressed id for one event.

    The hash of the canonical JSON, which is the same material the chain covers.
    Content addressing buys three things at once: re-running is idempotent, an
    edited event is a NEW id rather than a silent overwrite, and incrementality
    needs no cursor file that could drift from the store."""
    import hashlib
    from . import events as _events
    material = _events.canonical_json(_events.to_dict(ev))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def event_document(ev) -> str:
    """The text actually embedded for one event.

    Type and timestamp ride in front of the prose on purpose: the encoder sees
    them, so "what did we decide in August" has something to match on beyond the
    body, and a human reading the snippet can tell what they are looking at.

    Falls back to canonical JSON for a semantic type whose payload carries no
    `text` — better a searchable structure than a silently empty vector."""
    from . import events as _events
    payload = ev.payload if isinstance(ev.payload, dict) else {}
    body = payload.get("text")
    if not isinstance(body, str) or not body.strip():
        body = _events.canonical_json(payload)
    return f"[{ev.type}] {ev.ts or ''} {body}"[:4000]


def embed_events(project=None, force=False, batch=256, types=None) -> dict:
    """Index every semantic event not already indexed. Incremental by default.

    Returns `{"scanned", "eligible", "embedded", "skipped"}` — `skipped` being
    the ones already present, which on a second run is all of them. That is the
    number worth reporting: it is the evidence the incrementality works, and a
    run that re-embedded everything would look identical without it."""
    guard.ensure_safe(force=force)
    _require_deps()
    from . import events as _events
    wanted = set(types) if types else _semantic_types()

    evs = _events.read_all(project)
    eligible = [e for e in evs if e.type in wanted]
    if not eligible:
        return {"scanned": len(evs), "eligible": 0, "embedded": 0, "skipped": 0}

    col = _events_collection(project, refill=True)
    enc = _encoder()
    embedded = skipped = 0

    for start in range(0, len(eligible), batch):
        chunk = eligible[start:start + batch]
        ids = [event_id(e) for e in chunk]
        # One `get` per batch rather than one per event: chroma round trips are
        # the cost here, not the comparison.
        try:
            present = set(col.get(ids=ids).get("ids") or [])
        except Exception as e:                     # noqa: BLE001
            _log.warning("vectors: id probe failed, re-embedding batch: %s", e)
            present = set()
        fresh = [(i, ev) for i, ev in zip(ids, chunk) if i not in present]
        skipped += len(chunk) - len(fresh)
        if not fresh:
            continue
        docs = [event_document(ev) for _, ev in fresh]
        metas = [{"type": ev.type, "ts": ev.ts or "",
                  "session_id": ev.session_id or ""} for _, ev in fresh]
        col.add(ids=[i for i, _ in fresh], documents=docs,
                embeddings=enc(docs), metadatas=metas)
        embedded += len(fresh)

    return {"scanned": len(evs), "eligible": len(eligible),
            "embedded": embedded, "skipped": skipped}


def semantic_recall_events(query, project=None, limit=10, force=False,
                           types=None):
    """Nearest semantic EVENTS to a query, best first.

    Each hit carries its type, timestamp and session alongside the score, so a
    caller can cite the memo rather than merely quote it."""
    guard.ensure_safe(force=force)
    _require_deps()
    enc = _encoder()
    col = _events_collection(project)
    kwargs = {"query_embeddings": enc([query]), "n_results": limit}
    if types:
        kwargs["where"] = {"type": {"$in": list(types)}}
    try:
        res = col.query(**kwargs)
    except Exception as e:                         # noqa: BLE001
        _log.warning("vectors: event query failed: %s", e)
        return []
    docs = res.get("documents", [[]])[0]
    ids = res.get("ids", [[]])[0]
    metas = (res.get("metadatas") or [[]])[0] or [{}] * len(docs)
    dist_outer = res.get("distances") or [[]]
    dists = dist_outer[0] if dist_outer else []

    out = []
    for i, d in enumerate(docs):
        meta = metas[i] if i < len(metas) and metas[i] else {}
        score = (round(1.0 - dists[i], 6)
                 if i < len(dists) and dists[i] is not None else None)
        out.append({"id": ids[i] if i < len(ids) else None,
                    "type": meta.get("type"), "ts": meta.get("ts"),
                    "session_id": meta.get("session_id"),
                    "snippet": " ".join(d.split())[:300], "score": score})
    return out


def indexed_event_count(project=None) -> int:
    """How many events are in the vector index. 0 when the store is absent.

    Never raises: this is called to DECIDE whether the semantic path is worth
    taking, and a check that can fail is a check every caller has to wrap."""
    try:
        _require_deps()
        return int(_events_collection(project).count())
    except Exception:                              # noqa: BLE001
        return 0


# --- the Postgres vector shape -----------------------------------------------
#
# The half above stores vectors in ChromaDB, on the local filesystem, where the
# Postgres tools cannot see them. This half is the byte format those same
# vectors take inside `mindhive.event_embeddings`, kept HERE rather than in the
# loader for one reason: the loader holds the owner DSN and the search path must
# never import it, yet both have to agree byte-for-byte about what a stored
# vector is. A packer in the writer and an unpacker in the reader would be two
# descriptions of one format, and the day they disagree the symptom is not an
# error -- it is plausible nonsense in the ranking.
#
# Nothing below needs a database, and only `unpack_matrix`/`rank` need NumPy, so
# the format is testable with no server and no model.

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


def require_encoder():
    """Raise unless the ENCODER is importable. Deliberately not `_require_deps`.

    `_require_deps` also demands `chromadb`, because everything above it writes
    to a chroma collection. The Postgres path writes to Postgres, and making a
    SQL search refuse because a local vector database is missing would be a
    dependency that exists only in the error message."""
    try:
        import sentence_transformers  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "encoding needs the model: pip install -e .[vectors]") from e


def encode(texts) -> list:
    """Encode a batch of strings. Loads the model on first use."""
    require_encoder()
    return _encoder()(list(texts))


def pack_embedding(vec):
    """One vector as `(bytes, dim, norm)` ready for `event_embeddings`.

    THREE decisions, each of which shows up somewhere else:

    * **float32, little-endian, explicit.** `struct.pack('<Nf')` rather than
      `array('f').tobytes()`, whose byte order is whatever the CPU happens to
      be. The mirror is a file on disk that another machine may read.
    * **L2-normalised on the way in**, so cosine similarity IS the dot product
      and the search path never divides. A query vector is normalised the same
      way in `rank`, which is what makes the score land in [-1, 1].
    * **`norm` is the magnitude BEFORE normalising**, and it is returned because
      it is the only evidence that survives. A zero vector cannot be normalised;
      it is stored as-is and would then score 0.0 against every query, looking
      exactly like a weak-but-real match. `norm = 0` is how that is told apart
      afterwards, without re-reading the bytes."""
    import math
    import struct
    vals = [float(x) for x in vec]
    if not vals:
        raise ValueError("refusing to pack a zero-width embedding")
    norm = math.sqrt(sum(v * v for v in vals))
    if norm > 0.0:
        vals = [v / norm for v in vals]
    return struct.pack(f"<{len(vals)}f", *vals), len(vals), norm


def unpack_matrix(blobs, dim):
    """`(n, dim)` float32 matrix from n stored blobs, in the order given.

    One `b"".join` and one `np.frombuffer` -- no per-row parsing, no copy of the
    float data. That is the entire reason the column is BYTEA rather than
    `real[]`: measured against the same 1,000 x 384 vectors, ranking this way
    took 10.4 ms where a pure-SQL dot product over `real[]` took 101.7 ms.

    The width is CHECKED rather than trusted. A short blob reshapes without
    error into a matrix of the wrong length, and every score after it is
    garbage that looks like data -- the same failure the table's
    `event_embeddings_width` constraint exists to prevent, guarded again here
    because this function is also fed by tests and by callers that never touched
    the database."""
    import numpy as np
    joined = b"".join(bytes(b) for b in blobs)
    expected = len(blobs) * dim * 4
    if len(joined) != expected:
        raise ValueError(
            f"embedding blobs are {len(joined)} bytes, expected {expected} "
            f"({len(blobs)} rows x {dim} float32). A row is truncated or the "
            f"recorded dim is wrong; refusing to reshape into plausible noise.")
    return np.frombuffer(joined, dtype="<f4").reshape(-1, dim)


def rank(query_vec, matrix, limit=10):
    """`[(row_index, score)]`, best first, at most `limit` of them.

    The score is a cosine similarity in [-1, 1] because both sides are unit
    length -- `pack_embedding` normalised the stored rows and this normalises the
    query. `argpartition` rather than a full `argsort`: only the top `limit` need
    to be in order, and at 37k rows that is the difference between O(n) and
    O(n log n) on every query."""
    import numpy as np
    q = np.asarray(list(query_vec), dtype="<f4")
    n = float(np.linalg.norm(q))
    if n > 0.0:
        q = q / n
    if matrix.shape[0] == 0 or limit <= 0:
        return []
    scores = matrix @ q
    k = min(int(limit), scores.shape[0])
    if k < scores.shape[0]:
        top = np.argpartition(-scores, k - 1)[:k]
    else:
        top = np.arange(scores.shape[0])
    top = top[np.argsort(-scores[top])]
    return [(int(i), float(scores[i])) for i in top]
