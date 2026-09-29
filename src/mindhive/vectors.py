# SPDX-License-Identifier: Apache-2.0
# src/mindhive/vectors.py
import logging
import os
import re
import threading
import time
from contextlib import ExitStack, contextmanager

from . import paths, guard
from .vector_errors import (SemanticQueryError, UnansweredQuery,
                            as_failure as _as_failure, judge as _judge,
                            text_of as _text)

_log = logging.getLogger("mindhive.vectors")
_MODEL = None

# --- the CPU thread lock (MEB VEC-01) ---------------------------------------
#
# Every one of these defaults to "use all cores", and they COMPOUND: OpenMP
# spawns N workers, MKL spawns N inside each, the tokenizer forks again. On a
# background embed that is the whole machine, which is the opposite of what a
# watcher running behind an interactive session is for.
#
# Applied at IMPORT, before `sentence_transformers`/`chromadb` can be reached
# through any function below, because OpenMP reads `OMP_NUM_THREADS` when its
# runtime LOADS. A value set afterwards is a value nothing ever reads.
#
# `setdefault`, never assignment: an operator who exported `OMP_NUM_THREADS=8`
# on a 32-core box made a decision, and a library that silently overrode it
# would be lying about who is in charge of the machine.
#
# For THIS process only. `os.environ` is what every child inherits, so the
# defaults inserted here rode along into every `claude`, `pytest` and
# `python -m mindhive` this process spawned -- a two-thread BLAS cap nothing
# there asked for. What is inserted is registered in `paths.PROCESS_ONLY_ENV`
# (name -> value), and `paths.no_bytecode_env` (the one builder of child
# environments) strips each while it still holds that value; an operator's
# own value, set before or over it, is not ours to strip and propagates as
# it would without mindhive.
THREAD_ENV = {
    "OMP_NUM_THREADS": "2",
    "MKL_NUM_THREADS": "2",
    "OPENBLAS_NUM_THREADS": "2",
    "VECLIB_MAXIMUM_THREADS": "2",
    "NUMEXPR_NUM_THREADS": "2",
    "TOKENIZERS_PARALLELISM": "false",
}


def apply_thread_env(env=None):
    """Fill in `THREAD_ENV` on *env* (default `os.environ`) without ever
    overwriting a value already there. Returns the mapping it configured,
    so a caller that WANTS a child capped can apply it to that child's
    mapping and chain. What is inserted into the real environment is
    registered in `paths.PROCESS_ONLY_ENV` (comment above `THREAD_ENV`), so
    a child built by `paths.no_bytecode_env` does not inherit it; a mapping
    handed in explicitly is configured, not registered."""
    real = env is None or env is os.environ
    env = os.environ if real else env
    for key, value in THREAD_ENV.items():
        if key not in env:
            env[key] = value
            if real:
                paths.PROCESS_ONLY_ENV[key] = value
    return env


apply_thread_env()


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


_EXTRAS = "semantic layer needs extras: pip install -e .[vectors]"


def _require_deps():
    try:
        import chromadb  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ImportError as e:
        raise RuntimeError(_EXTRAS) from e


def missing_extras():
    """The one-line reason the semantic layer cannot run here, or None when
    both packages are installed. Asks `has_vectors_support` -- a spec lookup,
    no import -- so the watcher can ask on every cycle that has work without
    paying the import; `_require_deps` is what a write path then pays once."""
    return None if has_vectors_support() else _EXTRAS


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
#
# M, construction_ef and search_ef ride along because chroma's defaults are
# tuned for a far larger corpus than one developer's ledger, and because a
# collection built under one set keeps it FOREVER -- see `_open`.
HNSW_METADATA = {"hnsw:space": "cosine", "hnsw:M": 16,
                 "hnsw:construction_ef": 100, "hnsw:search_ef": 32}

#: Retained name for the space alone; `HNSW_METADATA` is what `_open` sends.
_SPACE = {"hnsw:space": HNSW_METADATA["hnsw:space"]}

#: `mindhive_` + a slug, capped so the whole name fits chroma's 63-character
#: limit with room to spare (PDF 8.2 caps the slug at 45).
COLLECTION_PREFIX = "mindhive_"
MAX_COLLECTION_CHARS = 45

#: The sidecar that keeps two processes out of one chroma SQLite file.
CHROMA_LOCK_NAME = "chroma.lock"
CHROMA_LOCK_TIMEOUT_S = 30.0
CHROMA_LOCK_STALE_S = 600.0

#: The batch the watcher and the CLI both write in, and the pause between two
#: of them. 32 records per `add` keeps one round trip worth of work in memory;
#: the yield is what stops a 70-record queue pinning a core while someone is
#: typing (PDF 8.2).
EMBED_BATCH_SIZE = 32
COOPERATIVE_YIELD_SECS = 0.050
#: Ids per `get` of the watcher's id probe. chromadb binds one SQL variable
#: per id and SQLite refuses a statement with too many (999 on old builds):
#: 40k ids in one `get` raised 'too many SQL variables' as an InternalError --
#: the class a damaged index raises.
ID_PROBE_BATCH = 256


def _client(project):
    import chromadb
    return chromadb.PersistentClient(
        path=str(paths.project_dir(project) / "chroma"))


# --- the read path -------------------------------------------------------------
#
# A read never writes the index it reads (DATA_MODEL.md 4.19). chromadb has no
# read-only client and a read through a normal one commits to `chroma.sqlite3`
# and can rewrite the HNSW segment files, so every read below goes through
# `_reading`, which hands back a client on a private copy (`vector_snapshot`).
#
# And a read that FAILED says so: each step that can break -- taking the copy,
# opening it, counting, encoding the query, querying -- raises
# `SemanticQueryError` naming the step and a repair chosen by what broke
# (`vector_errors`), where the `[]` or `0` it answered made a broken index look
# exactly like one with nothing relevant in it. `_as_failure` wraps what a step
# raised, a panic in chromadb's Rust core included; `_judge` tells
# `vector_snapshot` which failures were the client's own. A client that
# answered can fail a read too, and its copy is not poisoned: a hit that came
# back without its text (`_text`, asked once the client is handed back), and
# a query that answered no hits from rows it could have returned (`_ask`).

def _reading(project, name=None):
    """A client on a private copy of *project*'s index, or None when it has
    no index; used as `with _reading(project, name) as client:`. The one place
    a read path meets chromadb, and so the seam the tests replace. Without
    chromadb it is the extras refusal, before anything is read. The copy's
    segments are checked for collection *name* before it is read, and the
    copy is opened in a reading process of its own
    (`vector_snapshot.opener`), so a crash or a hang in chromadb is a
    failed read here, never the end of this process."""
    try:
        import chromadb  # noqa: F401
    except ImportError as e:
        raise RuntimeError(_EXTRAS) from e
    from . import vector_snapshot
    return vector_snapshot.reading(
        paths.project_dir(project) / "chroma", vector_snapshot.opener(),
        judge=_judge, name=name, check=vector_snapshot.check_segments)


def _existing(client, name):
    """Collection *name* if the index holds it, else None. Never creates:
    `get_or_create_collection` on a read would mint an empty collection."""
    have = {getattr(c, "name", c) for c in client.list_collections()}
    return client.get_collection(name) if name in have else None


@contextmanager
def _read_view(project, name, open_col=None):
    """Yield collection *name* from a copy of the index, None when there is
    none. *open_col(client)* overrides how the collection is looked up. A
    failure is a `SemanticQueryError` -- missing extras stay the refusal."""
    from . import vector_snapshot
    view = _reading(project, name=name)
    with ExitStack() as stack:
        try:
            client = stack.enter_context(view)
            col = None if client is None else (
                open_col(client) if open_col is not None
                else _open(project, name, client=client))
        except vector_snapshot.IndexUnreadable as e:
            # Reported as the read that failed, on the collection it failed
            # on -- past the cap that can be a sibling of the one asked for,
            # and the repair then ends in a restart.
            first = e.original
            raise SemanticQueryError(
                getattr(first, "stage", "open"), e.of,
                getattr(first, "__cause__", None) or first, project,
                note=e.why, restart=e.capped) from e
        except vector_snapshot.SnapshotError as e:
            raise SemanticQueryError("snapshot", name, e, project) from e
        except BaseException as e:                 # noqa: BLE001 -- re-raised typed
            raise _as_failure("open", name, e, project) from e
        yield col


def _embed_query(query, name, project=None):
    try:
        return _encoder()([query])
    except BaseException as e:                     # noqa: BLE001 -- re-raised typed
        raise _as_failure("encode", name, e, project) from e


def _answered(res) -> bool:
    """Whether query result *res* holds a hit, in any column a caller reads
    hits from."""
    for key in ("ids", "documents", "metadatas", "distances"):
        rows = res.get(key) or []
        if rows and rows[0]:
            return True
    return False


def _ask(col, name, project=None, **kwargs):
    """`col.query(**kwargs)`, or the failed query it was. No hits from a
    collection holding rows the query could have returned is one: measured
    on an index whose recorded dimension was NULL, `count` answered 1300 and
    every query nothing, with no error. Asked of the collection only when
    the query came back empty: for one row its `where` matches, else for its
    count."""
    try:
        res = col.query(**kwargs)
    except BaseException as e:                     # noqa: BLE001 -- re-raised typed
        raise _as_failure("query", name, e, project) from e
    if _answered(res):
        return res
    where = kwargs.get("where")
    try:
        held = (len(col.get(where=where, limit=1).get("ids") or []) if where
                else int(col.count()))
    except BaseException as e:                     # noqa: BLE001 -- re-raised typed
        raise _as_failure("count", name, e, project) from e
    if held:
        rows = "rows its filter matches" if where else f"{held} rows"
        cause = UnansweredQuery(
            f"the query answered no hits from a collection holding {rows}")
        raise SemanticQueryError("query", name, cause, project) from cause
    return res


def _open(project, name, *, refill=False, rebuild=False, client=None):
    """Get one collection, in cosine space.

    chroma ignores the metadata argument when a collection already exists, so a
    collection created before this fix keeps its L2 space forever unless it is
    dropped. Dropping is only safe where the caller is about to refill it, hence
    `refill`: query paths take whatever is there (wrong scores beat no results),
    embed paths correct it first. *rebuild* is the operator saying "start
    over" -- it drops a collection whose metadata already agrees, which is
    exactly the case the drift check would pass.

    *client* given is the READ form: the collection is looked up on that
    client and nothing is created, dropped or re-tuned, whatever *refill*
    says -- None when the index does not hold it."""
    if client is not None:
        return _existing(client, name)
    client = _client(project)
    col = client.get_or_create_collection(name, metadata=dict(HNSW_METADATA))
    if refill and (rebuild or hnsw_drift(col)):
        drift = hnsw_drift(col)
        _log.warning("vectors: rebuilding %r — %s", name,
                     f"its tuning differs at {sorted(drift)}" if drift
                     else "the operator asked for a rebuild")
        client.delete_collection(name)
        col = client.get_or_create_collection(name, metadata=dict(HNSW_METADATA))
    return col


def hnsw_drift(col) -> set:
    """Which `HNSW_METADATA` keys an existing collection disagrees about.

    Watching `hnsw:space` alone was not enough once M and the ef parameters
    joined the set: chroma IGNORES the metadata argument for a collection that
    already exists, so a collection built under the one-key form keeps
    whatever M chroma defaulted to, forever, and every key but the space would
    read as "already correct"."""
    have = col.metadata or {}
    return {k for k, v in HNSW_METADATA.items() if have.get(k) != v}


def collection_name(slug) -> str:
    """A legal chroma collection name for *slug*.

    chroma refuses a name that is shorter than 3, longer than 63, or does not
    start AND end alphanumeric. The PDF spells this
    `f"mindhive_{re.sub(r'[^a-zA-Z0-9_-]', '_', slug)[:45]}"`, which turns
    `My Slug!` into `mindhive_My_Slug_` -- a trailing underscore chroma
    rejects outright -- so the slug is lowercased and stripped here instead of
    copied. A slug with nothing usable in it still has to produce a name, or
    an unnamed project could not be indexed at all."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(slug).strip().lower()).strip("_")
    if not cleaned:
        cleaned = "default"
    name = (COLLECTION_PREFIX + cleaned)[:MAX_COLLECTION_CHARS]
    return name.rstrip("_-.") or COLLECTION_PREFIX + "default"


def chroma_lock_policy(timeout_s=None, stale_s=None):
    """The `LockPolicy` every chroma writer takes. Thirty seconds because one
    embed batch behind another one is a WAIT, not a deadlock; ten minutes
    before a holder counts as dead because a cold model load plus a large
    first index genuinely takes minutes and killing it halfway is worse."""
    from .core import locks
    return locks.LockPolicy(
        timeout_s=CHROMA_LOCK_TIMEOUT_S if timeout_s is None else timeout_s,
        stale_s=CHROMA_LOCK_STALE_S if stale_s is None else stale_s)


def chroma_lock(project=None, *, timeout_s=None, stale_s=None, **seams):
    """`with chroma_lock(project):` around every WRITE to chroma.

    chroma persists through SQLite, and two processes writing one SQLite file
    on NTFS is `database is locked` -- which surfaces as a failed embed, not
    as a wait. The sidecar lives beside the `chroma/` directory it guards,
    under `paths.project_dir`, never under the store root."""
    from .core import locks
    return locks.file_lock(paths.project_dir(project) / CHROMA_LOCK_NAME,
                           chroma_lock_policy(timeout_s, stale_s),
                           name="chroma_lock", **seams)


#: The file-level collection: one vector per LEDGER/*.md projection.
LEDGER_COLLECTION = "ledger"


def _collection(project, *, refill=False, rebuild=False):
    return _open(project, LEDGER_COLLECTION, refill=refill, rebuild=rebuild)


def embed(project=None, force=False, rebuild=False) -> dict:
    guard.ensure_safe(force=force)
    _require_deps()
    ldir = paths.project_dir(project) / "LEDGER"
    files = sorted(ldir.glob("*.md")) if ldir.exists() else []
    if not files:
        return {"embedded": 0}
    enc = _encoder()
    ids = [f.name for f in files]
    docs = [f.read_text(encoding="utf-8") for f in files]
    # The READ of the LEDGER files is outside the lock on purpose: it touches
    # no chroma state, and holding a thirty-second lock across an arbitrary
    # number of file reads would make one slow disk everybody's problem.
    with chroma_lock(project):
        col = _collection(project, refill=True, rebuild=rebuild)
        # Best-effort delete-before-add so a reembed refreshes rather than
        # duplicates. On the first embed the ids don't exist yet; tolerate that
        # but surface any real failure instead of hiding it (a silently-failed
        # delete could otherwise leave stale vectors behind).
        try:
            col.delete(ids=ids)
        except Exception as e:  # noqa: BLE001 — best-effort, logged not swallowed
            _log.warning("vectors: delete-before-reembed failed for %s ids in %s: %s",
                         len(ids), project, e)
        col.add(ids=ids, documents=docs, embeddings=enc(docs))
    return {"embedded": len(files)}


def semantic_recall(query, project=None, limit=10, force=False):
    """Nearest LEDGER files to a query. `[]` when nothing is indexed or the
    limit asks for no hits; `SemanticQueryError` when the index could not be
    read."""
    guard.ensure_safe(force=force)
    _require_deps()
    if limit < 1:          # chromadb refuses it; `PgVectorIndex` answers `[]`
        return []
    with _read_view(project, LEDGER_COLLECTION) as col:
        if col is None:
            return []
        res = _ask(col, LEDGER_COLLECTION, project, n_results=limit,
                   query_embeddings=_embed_query(query, LEDGER_COLLECTION,
                                                 project))
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

    return [{"file": ids[i], "snippet": " ".join(_text(
                d, ids[i], LEDGER_COLLECTION, project).split())[:200],
             "score": _score(i)}
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


def _events_collection(project, *, refill=False, rebuild=False):
    return _open(project, EVENT_COLLECTION, refill=refill, rebuild=rebuild)


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


def embed_events(project=None, force=False, batch=256, types=None,
                 rebuild=False) -> dict:
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

    enc = _encoder()
    embedded = skipped = 0

    with chroma_lock(project):
        col = _events_collection(project, refill=True, rebuild=rebuild)
        for start in range(0, len(eligible), batch):
            chunk = eligible[start:start + batch]
            # A ledger can hold one row twice (a hook that fired twice appends
            # byte-identical rows a line apart), and content addressing gives
            # both copies one id. chroma refuses an id named twice in one `get`
            # AND in one `add` (DuplicateIDError), which took the whole run
            # down mid-batch -- after a rebuild had already dropped the
            # collection. The second copy would embed to the same vector, so
            # it is skipped here, counted with the rows already present.
            seen = {}
            for ev in chunk:
                seen.setdefault(event_id(ev), ev)
            skipped += len(chunk) - len(seen)
            ids = list(seen)
            # One `get` per batch rather than one per event: chroma round trips
            # are the cost here, not the comparison.
            try:
                present = set(col.get(ids=ids).get("ids") or [])
            except Exception as e:                 # noqa: BLE001
                _log.warning("vectors: id probe failed, re-embedding batch: %s", e)
                present = set()
            fresh = [(i, ev) for i, ev in seen.items() if i not in present]
            skipped += len(seen) - len(fresh)
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
    caller can cite the memo rather than merely quote it. `[]` means nothing
    matched, nothing is indexed or the limit asks for no hits; an index that
    could not be read raises `SemanticQueryError` instead."""
    guard.ensure_safe(force=force)
    _require_deps()
    if limit < 1:
        return []
    with _read_view(project, EVENT_COLLECTION) as col:
        if col is None:
            return []
        kwargs = {"query_embeddings": _embed_query(query, EVENT_COLLECTION,
                                                   project),
                  "n_results": limit}
        if types:
            kwargs["where"] = {"type": {"$in": list(types)}}
        res = _ask(col, EVENT_COLLECTION, project, **kwargs)
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
        hit = ids[i] if i < len(ids) else None
        out.append({"id": hit, "type": meta.get("type"), "ts": meta.get("ts"),
                    "session_id": meta.get("session_id"),
                    "snippet": " ".join(_text(d, hit, EVENT_COLLECTION,
                                              project).split())[:300],
                    "score": score})
    return out


def indexed_event_count(project=None, *, strict=False) -> int:
    """How many events are in the vector index; 0 when there is no index or
    the extras are not installed.

    An index that could not be read is NOT 0. With *strict* it raises
    `SemanticQueryError` -- `recall` routes on this number, and a 0 that meant
    "broken" sent it to the file index in silence. Without, it logs the
    failure and answers 0, for callers that only display the count."""
    try:
        _require_deps()
    except RuntimeError:
        return 0
    try:
        with _read_view(project, EVENT_COLLECTION) as col:
            if col is None:
                return 0
            try:
                return int(col.count())
            except BaseException as e:             # noqa: BLE001 -- re-raised typed
                raise _as_failure("count", EVENT_COLLECTION, e, project) from e
    except SemanticQueryError as e:
        if strict:
            raise
        _log.warning("vectors: %s", e)
        return 0



# --- the v1 surface: one manager, cooperative batches, back-pointers ---------
#
# Everything above indexes on DEMAND -- an operator runs `hive embed`, or the
# dashboard presses a button, and the whole eligible set is walked. The half
# below is what the WATCHER drives (P1h): a queue of `(session_id, seg_line)`
# coordinates arrives, the rows behind them are read back, and each vector is
# stored with enough metadata to point at the physical line it came from.
#
# The difference that matters is who is waiting. Nobody is waiting on the
# watcher, but somebody is usually typing while it runs, so the batch is small
# and the CPU is handed back between batches.


class LocalEmbeddingService:
    """The encoder, loaded once and driven in cooperative batches.

    The model is ~90 MB and takes seconds to load, so it is loaded LAZILY and
    then kept: a service constructed for an empty queue must never pay for it.
    *encoder* and *sleep* are injectable for the same reason the locks module
    injects its clocks -- a test should be able to prove the batching without
    the extras installed."""

    def __init__(self, encoder=None, sleep=None):
        self._encoder = encoder
        self._sleep = sleep if sleep is not None else time.sleep

    def encoder(self):
        if self._encoder is None:
            self._encoder = _encoder()
        return self._encoder

    def encode_batch_cooperative(self, texts, progress_callback=None):
        """Encode *texts* in batches of `EMBED_BATCH_SIZE`, yielding the CPU
        for `COOPERATIVE_YIELD_SECS` BETWEEN batches and not after the last:
        a sleep after the final batch is latency nobody is waiting out."""
        texts = list(texts)
        if not texts:
            return []
        out = []
        total = len(texts)
        for start in range(0, total, EMBED_BATCH_SIZE):
            out.extend(self.encoder()(texts[start:start + EMBED_BATCH_SIZE]))
            if progress_callback is not None:
                progress_callback(len(out), total)
            if start + EMBED_BATCH_SIZE < total:
                self._sleep(COOPERATIVE_YIELD_SECS)
        return out


def record_provenance(record) -> str:
    """Where a record's row physically lives, as a store-relative path.

    The PDF spells this `f"events/{record.session_id}.jsonl"`. That path does
    not exist for a quarantined id: `../x` and `CON` are written to
    `unsafe-<sha256[:16]>.jsonl`, so a back-pointer built from the raw id
    would send a reader to the exact path the quarantine closed. The segment
    name is asked for instead."""
    from . import events as _events
    return f"events/{_events.segment_name(record.session_id)}"


def build_embedding_document(record) -> str:
    """The text embedded for one `EventRecord` (PDF 4.3.2).

    Type tag and timestamp lead, then the summary, then the body; a payload
    with no `text` is serialised rather than dropped, because a structure that
    can be matched beats an empty vector. The `" ".join(split())` collapse is
    load-bearing: without it a record with no summary embeds a different
    string than the same record with one, purely because of a double space."""
    import json as _json
    payload = record.payload if isinstance(record.payload, dict) else {}
    summary = payload.get("summary", "")
    body = payload.get("text", "")
    if not body:
        body = _json.dumps(payload, sort_keys=True)
    raw = f"[{record.event_type}] {record.timestamp} {summary} {body}".strip()
    return " ".join(raw.split())[:4000]


def record_metadata(record) -> dict:
    """The associative back-pointers stored beside one vector (PDF 4.3.3).

    Every value is a chroma scalar, because chroma refuses a list or a dict;
    `chain_hash` is what lets a hit be VERIFIED against the ledger rather than
    merely quoted."""
    return {"session_id": str(record.session_id or ""),
            "seg_line": int(record.seg_line or 0),
            "chain_hash": str(record.chain_hash or ""),
            "event_type": str(record.event_type or ""),
            "timestamp": str(record.timestamp or ""),
            "provenance": record_provenance(record)}


def vector_id(record) -> str:
    """The id a record's vector is stored under: `"<session_id>:<seg_line>"`,
    the physical row. `embedqueue.entry_key` spells the same id from a queue
    entry, so the two sides of the hand-off dedupe on one key."""
    return f"{record.session_id}:{record.seg_line}"


class VectorStoreManager:
    """One chroma client per namespace, shared by everything in the process.

    `chromadb.PersistentClient` opens a SQLite file; two of them against one
    path inside one process is two write handles where the lock protocol
    assumes one. A manager per project, and the manager's own client opened
    once, is what keeps that true without threading a handle through every
    call site."""

    _INSTANCES: dict = {}
    _GUARD = threading.Lock()

    def __init__(self, project=None):
        self.project = project
        self._client = None
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls, project=None) -> "VectorStoreManager":
        key = str(paths.project_dir(project))
        with cls._GUARD:
            got = cls._INSTANCES.get(key)
            if got is None:
                got = cls._INSTANCES[key] = cls(project)
            return got

    @classmethod
    def reset_instances(cls) -> None:
        """Drop every cached manager. For tests, which move `MINDHIVE_HOME`
        between cases and would otherwise inherit a client bound to a
        directory that no longer exists."""
        with cls._GUARD:
            cls._INSTANCES.clear()

    def client(self):
        with self._lock:
            if self._client is None:
                self._client = _client(self.project)
            return self._client

    def get_collection(self, project_slug, *, refill=False, rebuild=False,
                       client=None):
        """The collection for *project_slug* on the live index, or -- with
        *client*, a read's copy -- looked up there without being created."""
        return _open(self.project, collection_name(project_slug),
                     refill=refill, rebuild=rebuild, client=client)

    def already_indexed(self, project_slug, ids) -> set:
        """Which of *ids* the collection already holds, read from a copy of
        the index like every other read (DATA_MODEL.md 4.19): through the live
        `get_or_create_collection` a probe minted an empty collection for a
        slug never embedded, and wrote to the index every time. Empty on any
        failure, logged: this decides whether to RE-embed, and over-embedding
        is idempotent where refusing to embed is data loss."""
        # Each id once: a claim recovered twice queues its rows twice, and
        # chromadb refuses a `get` that names an id twice (DuplicateIDError).
        ids = list(dict.fromkeys(ids))
        if not ids:
            return set()
        name = collection_name(project_slug)

        def _on(client):
            return self.get_collection(project_slug, client=client)

        try:
            with _read_view(self.project, name, _on) as col:
                if col is None:
                    return set()
                try:
                    have = set()
                    for at in range(0, len(ids), ID_PROBE_BATCH):
                        batch = ids[at:at + ID_PROBE_BATCH]
                        have.update(col.get(ids=batch).get("ids") or [])
                    return have
                except BaseException as e:         # noqa: BLE001 -- re-raised typed
                    raise _as_failure("query", name, e, self.project) from e
        except Exception as e:                     # noqa: BLE001
            _log.warning("vectors: id probe failed for %s: %s", project_slug, e)
            return set()

    def embed_batch(self, project_slug, records, progress_callback=None, *,
                    sleep=None, service=None) -> int:
        """Store one vector per record, in batches, under the chroma lock.

        Returns how many were written. *progress_callback* is called with
        `(done, total)` after each batch -- that pair, not a percentage, so
        the caller decides how to render it. One vector per DISTINCT id,
        whatever the batch repeats: a claim recovered twice names its rows
        twice, and chromadb refuses an `add` that repeats an id
        (DuplicateIDError) -- the whole batch failed, the claim stayed on
        disk, and the next recovery queued the same duplicates again."""
        byid = {}
        for r in records:
            byid.setdefault(vector_id(r), r)
        records = list(byid.values())
        if not records:
            return 0
        _require_deps()
        svc = service if service is not None else LocalEmbeddingService()
        naptime = sleep if sleep is not None else time.sleep
        total = len(records)
        done = 0
        with chroma_lock(self.project):
            col = self.get_collection(project_slug)
            for start in range(0, total, EMBED_BATCH_SIZE):
                chunk = records[start:start + EMBED_BATCH_SIZE]
                docs = [build_embedding_document(r) for r in chunk]
                col.add(ids=[vector_id(r) for r in chunk],
                        documents=docs,
                        embeddings=svc.encoder()(docs),
                        metadatas=[record_metadata(r) for r in chunk])
                done += len(chunk)
                if progress_callback is not None:
                    progress_callback(done, total)
                if start + EMBED_BATCH_SIZE < total:
                    naptime(COOPERATIVE_YIELD_SECS)
        return done

    def semantic_query(self, project_slug, query, top_k=5) -> list:
        """Nearest vectors to *query*, best first, each with its
        back-pointers and a cosine score rounded to four places. Read from a
        copy of the index; a failure raises `SemanticQueryError`. A top_k
        below 1 is `[]`, as `PgVectorIndex.query` answers it."""
        _require_deps()
        if top_k < 1:
            return []
        name = collection_name(project_slug)
        with _read_view(self.project, name, lambda client: self.get_collection(
                project_slug, client=client)) as col:
            if col is None:
                return []
            res = _ask(col, name, self.project, n_results=top_k,
                       query_embeddings=_embed_query(query, name, self.project))
        docs = (res.get("documents") or [[]])[0]
        ids = (res.get("ids") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0] or [{}] * len(docs)
        dists = (res.get("distances") or [[]])[0]
        out = []
        for i, doc in enumerate(docs):
            dist = dists[i] if i < len(dists) else None
            out.append({"id": ids[i] if i < len(ids) else None,
                        "document": doc,
                        "metadata": metas[i] if i < len(metas) else {},
                        "score": None if dist is None else round(1.0 - dist, 4)})
        return out


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
