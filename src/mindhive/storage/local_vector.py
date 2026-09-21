# SPDX-License-Identifier: Apache-2.0
"""`LocalVectorIndex` — chroma behind the SPI (blueprint P1i, MEB VEC-01).

Thin on purpose. `vectors.py` already owns every hard decision this touches:
the encoder, the HNSW tuning, the rebuild-on-drift rule, the write lock, the
document template and the content-addressed id. This module is the ABC's
shape wrapped around them, plus the one thing `vectors` has no reason to do --
turning a hit back into an `EventRecord`.

**The collection name is taken VERBATIM**, not sanitised. `hive embed` writes
the `events` collection and `hive recall --semantic` reads it; a driver that
quietly rewrote `"events"` into `vectors.collection_name("events")` --
`mindhive_events` -- would index into a collection nothing else reads, and
would look like it worked. An illegal name is refused with the name of the
helper that makes a legal one, which is the loud half of the same decision.

**Ids are `vectors.event_id`**, the content-addressed hash of the canonical
row, so a record indexed here and an event indexed by `hive embed` land on the
same id. That is the whole point of sharing the collection, and it has one
consequence that has to be handled rather than hoped about: `hive embed`
writes THREE metadata keys and this module needs nine, so an id already
present may carry metadata too thin to rebuild a record from. Such a row is
UPGRADED in place -- same vector, fuller metadata -- instead of being counted
as a skip, because a skip there would make `query` return records with an
empty `chain_hash` and no way for a caller to tell.

Sharing ids also means two BYTE-IDENTICAL events on two ledger lines are one
vector: correct for search, where the second is the same text, and the
metadata then describes whichever row was written last -- written down here
rather than discovered later.
"""
import json
import re

from .. import events, vectors
from ..core.models import EventRecord
from . import records
from .interface import VectorIndex

__all__ = ["LocalVectorIndex", "LEGAL_COLLECTION", "METADATA_KEYS"]

#: chroma's own rule: 3-63 characters, alphanumeric at both ends, and only
#: `[a-z0-9._-]` between. `vectors.collection_name` is the helper that turns
#: an arbitrary slug into one of these.
LEGAL_COLLECTION = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")

#: What a hit has to carry to come back as an `EventRecord`. chroma metadata
#: values must be scalars, so the two structured fields ride as JSON strings.
#: This tuple is also the completeness test: metadata missing any of these was
#: written by something that is not this module.
METADATA_KEYS = ("event_id", "session_id", "seg_line", "ts", "type",
                 "prev_hash", "chain_hash", "payload_json", "tags_json")


class LocalVectorIndex(VectorIndex):
    """One namespace's chroma store, as a `VectorIndex`.

    *encoder* is a seam, not a feature: it is what lets a test exercise the
    batching, the id scheme and the metadata round trip without loading a
    90 MB model. Left None, the real `vectors._encoder()` is used and the
    thread-pool caps are applied first, exactly as `hive embed` applies them."""

    def __init__(self, project=None, *, encoder=None):
        self.project = project
        self._encoder = encoder
        vectors.apply_thread_env()

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _check(collection_name) -> str:
        name = str(collection_name)
        if not LEGAL_COLLECTION.match(name):
            raise ValueError(
                f"{name!r} is not a legal chroma collection name (3-63 chars, "
                f"alphanumeric at both ends); use "
                f"vectors.collection_name({name!r}) to derive one")
        return name

    #: The record -> `Event` shim, which lives in `records` because the
    #: Postgres driver needs the same one. Kept as a name on the class so a
    #: subclass could substitute it, and so the id pin below reads locally.
    as_event = staticmethod(records.event_of)

    def record_id(self, rec) -> str:
        return vectors.event_id(self.as_event(rec))

    def _encode(self):
        if self._encoder is not None:
            return self._encoder
        vectors._require_deps()
        return vectors._encoder()

    @staticmethod
    def metadata(rec) -> dict:
        """chroma metadata for one record. Scalars only -- chroma rejects a
        nested value -- so `payload` and `tags` ride as canonical JSON, which
        makes them byte-comparable with the ledger row they came from."""
        return {"event_id": rec.event_id,
                "session_id": rec.session_id or "",
                "seg_line": int(rec.seg_line),
                "ts": rec.timestamp or "",
                "type": rec.event_type or "",
                "prev_hash": rec.prev_hash or "",
                "chain_hash": rec.chain_hash or "",
                "payload_json": events.canonical_json(rec.payload),
                "tags_json": events.canonical_json(list(rec.tags))}

    @staticmethod
    def is_complete(meta) -> bool:
        """Whether *meta* carries everything `from_metadata` needs.

        A row `hive embed` wrote has `type`/`ts`/`session_id` and nothing
        else; it is a perfectly good VECTOR and an unusable RECORD."""
        return isinstance(meta, dict) and all(k in meta for k in METADATA_KEYS)

    @staticmethod
    def from_metadata(meta) -> EventRecord:
        """A hit's metadata back into a record.

        A malformed `payload_json` becomes an empty payload rather than an
        exception: this is a SEARCH result, and one unreadable hit must not
        take the other nine with it."""
        def _loads(raw, fallback):
            try:
                got = json.loads(raw)
            except (TypeError, ValueError):
                return fallback
            return got if isinstance(got, type(fallback)) else fallback

        return EventRecord(
            event_id=meta.get("event_id") or "",
            session_id=meta.get("session_id") or "",
            seg_line=int(meta.get("seg_line") or 0),
            timestamp=meta.get("ts") or "",
            event_type=meta.get("type") or "",
            payload=_loads(meta.get("payload_json"), {}),
            tags=_loads(meta.get("tags_json"), []),
            prev_hash=meta.get("prev_hash") or "",
            chain_hash=meta.get("chain_hash") or "")

    # --- the SPI ----------------------------------------------------------

    def embed_batch(self, collection_name, records_) -> int:
        """Index *records_*; return how many vectors were ADDED.

        Batched in `vectors.EMBED_BATCH_SIZE` chunks with one id probe per
        chunk, the way `vectors.embed_events` does it: chroma round trips are
        the cost, not the comparison. `refill=True` on the open is what
        corrects a collection whose HNSW tuning has drifted -- a query path
        takes whatever is there, a write path fixes it first.

        A metadata upgrade is NOT an add and is not counted: nothing new is
        searchable afterwards, and a count that moved would make a re-run of
        an already-indexed batch look like fresh work."""
        name = self._check(collection_name)
        batch = list(records_)
        if not batch:
            return 0
        encode = self._encode()
        added = 0
        with vectors.chroma_lock(self.project):
            col = vectors._open(self.project, name, refill=True)
            size = vectors.EMBED_BATCH_SIZE
            for start in range(0, len(batch), size):
                added += self._write_chunk(col, batch[start:start + size],
                                           encode)
        return added

    def _write_chunk(self, col, chunk, encode) -> int:
        # Deduplicate WITHIN the chunk as well as against the store: two
        # identical rows in one call would make `col.add` raise on a repeated
        # id, losing the whole chunk over a collision that is a no-op by
        # design. Dict insertion order keeps the LAST record for an id, which
        # matches the append-only reading of "whichever row was written last".
        wanted: dict = {}
        for rec in chunk:
            wanted[self.record_id(rec)] = rec
        try:
            got = col.get(ids=list(wanted))
            present = list(got.get("ids") or [])
            metas = list(got.get("metadatas") or [])
        except Exception:                                      # noqa: BLE001
            present, metas = [], []   # re-embedding is idempotent; refusing is loss
        thin = {i for i, m in zip(present, metas) if not self.is_complete(m)}
        seen = set(present)

        upgrade = [(i, r) for i, r in wanted.items() if i in thin]
        if upgrade:
            # Same vector, fuller metadata: no re-encode, and `update` leaves
            # the embedding alone for ids it does not carry one for.
            col.update(ids=[i for i, _ in upgrade],
                       metadatas=[self.metadata(r) for _, r in upgrade])

        fresh = [(i, r) for i, r in wanted.items() if i not in seen]
        if not fresh:
            return 0
        docs = [vectors.event_document(self.as_event(r)) for _, r in fresh]
        col.add(ids=[i for i, _ in fresh], documents=docs,
                embeddings=encode(docs),
                metadatas=[self.metadata(r) for _, r in fresh])
        return len(fresh)

    def query(self, collection_name, text, top_k: int = 5) -> list:
        """`(EventRecord, similarity)` best first.

        The collection is opened WITHOUT `refill`: a reader must never drop a
        collection, and a wrong score beats no results. Similarity is
        `1 - distance` in cosine space, rounded to four places -- the same
        conversion `vectors.VectorStoreManager.semantic_query` reports, so two
        callers asking the same question get the same number.

        A hit whose metadata is too thin to rebuild a record is DROPPED, not
        padded with empty hashes. It belongs to `hive embed`'s side of the
        shared collection; handing back a record whose `chain_hash` is `""`
        would be a record-shaped object that attests to nothing."""
        name = self._check(collection_name)
        encode = self._encode()
        col = vectors._open(self.project, name)
        try:
            res = col.query(query_embeddings=encode([text]), n_results=top_k)
        except Exception:                                      # noqa: BLE001
            return []
        metas = (res.get("metadatas") or [[]])[0] or []
        dist_outer = res.get("distances") or [[]]
        dists = dist_outer[0] if dist_outer else []
        out = []
        for i, meta in enumerate(metas):
            if not self.is_complete(meta):
                continue
            distance = dists[i] if i < len(dists) and dists[i] is not None else None
            score = 0.0 if distance is None else round(1.0 - distance, 4)
            out.append((self.from_metadata(meta), score))
        return out
