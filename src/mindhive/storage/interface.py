# SPDX-License-Identifier: Apache-2.0
"""The two storage ABCs (blueprint P1i, MEB STO-01/STO-02).

`EventStore` is the append-only log; `VectorIndex` is the similarity side.
Each declares the small set of operations a backend MUST implement and
supplies the rest on top of them, so a new driver writes three methods and
inherits five.

**Stdlib only, and no driver imported here.** This module is what a caller
reads to learn the contract, and reading a contract must not open a Postgres
connection or load a 90 MB encoder. The drivers import this; it imports none
of them.

`DataIntegrityError` lives here rather than in `migration` because it is the
SPI's own failure mode: a store that hands back a row whose `chain_hash` does
not follow from the row before it has broken this contract, whoever noticed.
"""
from abc import ABC, abstractmethod

__all__ = ["EventStore", "VectorIndex", "DataIntegrityError"]


class DataIntegrityError(ValueError):
    """A stored row contradicts the hash chain it claims to be part of.

    A `ValueError` subclass rather than a bare `Exception`: callers that
    already handle "this data is wrong" keep working, and the ones that must
    stop -- the migration engine -- catch the specific type. It is never
    raised for a row that is merely ABSENT; a missing row is a shorter
    stream, and only a row whose hash does not check is a broken chain."""


class EventStore(ABC):
    """An append-only event log addressed by `(session_id, seg_line)`.

    Three abstract operations. Everything else on this class is written in
    terms of them, so a backend cannot implement the convenience and forget
    the primitive.
    """

    # --- what a backend must implement -------------------------------------

    @abstractmethod
    def append(self, session_id, event_type, payload, tags=None):
        """Append one event and return the `EventRecord` it became.

        The record carries the chain coordinates the store assigned --
        `seg_line`, `prev_hash`, `chain_hash` -- because the caller cannot
        compute them: they depend on what was already there, which only the
        store knows and only under whatever lock it holds."""

    @abstractmethod
    def read_stream(self, session_id, from_line: int = 1):
        """Iterate one session's records from *from_line* (1-BASED, inclusive).

        An iterator rather than a list: a session can be tens of thousands of
        rows, and every caller here either folds or filters. `from_line` is a
        physical line number, so `from_line=1` is the whole session and a line
        past the end is an empty iterator rather than an error."""

    @abstractmethod
    def list_sessions(self) -> list:
        """Every session id the store holds, sorted.

        Ids, not segment file names: a quarantined id appears as the
        `unsafe-<hash>` name it was stored under, because that IS the id the
        store can address it by."""

    # --- what every backend gets for free -----------------------------------

    def append_event(self, event_type, payload, session_id=None, tags=None):
        """`events.append_event`'s argument order, for a caller migrating off
        the module-level function. Deliberately a thin alias and not a second
        implementation -- two spellings of one write is how the two drift."""
        return self.append(session_id, event_type, payload, tags=tags)

    def read_events(self, session_id=None) -> list:
        """Every record, or one session's. Sessions are walked in `sorted`
        order so the result is deterministic; it is NOT ledger order across
        sessions, because this store has no cross-session clock -- a caller
        that needs one sorts by `timestamp` itself."""
        if session_id is not None:
            return list(self.read_stream(session_id))
        out: list = []
        for name in self.list_sessions():
            out.extend(self.read_stream(name))
        return out

    def get_session_slice(self, session_id, from_line: int = 1,
                          to_line=None) -> list:
        """The records of one session between two 1-based lines, inclusive.

        `to_line=None` means "to the end". An inverted range answers empty
        rather than raising: a slice is a question about what is there, and
        "nothing" is a truthful answer to a range containing nothing."""
        out: list = []
        for rec in self.read_stream(session_id, from_line=from_line):
            if to_line is not None and rec.seg_line > to_line:
                break
            out.append(rec)
        return out

    def sync_state(self) -> dict:
        """Persist whatever the driver caches, and report what it holds.

        The base answer is honest about caching nothing: a driver that folds
        from disk on every call has no state to lose and nothing to write.
        `LocalEventStore` overrides it because it does keep a per-session fold
        cache, and a cache that is never written is a cache that re-reads the
        whole segment after every restart."""
        return {"driver": type(self).__name__,
                "sessions": len(self.list_sessions()),
                "persisted": False}


class VectorIndex(ABC):
    """A similarity index over `EventRecord`s, addressed by collection name."""

    @abstractmethod
    def embed_batch(self, collection_name, records) -> int:
        """Index *records* into *collection_name*; return how many were added.

        The count is ADDED, not seen: re-indexing a record the collection
        already holds is a no-op that returns 0, so a caller can re-run a
        batch without reasoning about what landed last time."""

    @abstractmethod
    def query(self, collection_name, text, top_k: int = 5) -> list:
        """The *top_k* records most similar to *text*, as `(record, score)`.

        Score is a SIMILARITY in `[0, 1]`, already converted from whatever
        distance the backend speaks, and sorted descending. Every backend
        converts in its own module because only it knows its metric."""

    def query_similarity(self, collection_name, text, top_k: int = 5,
                         min_score: float = 0.0) -> list:
        """`query` with a floor. A hit nobody would act on is worse than no
        hit: it reads as evidence, and the caller cannot see the score the
        way a ranked list makes visible."""
        return [(rec, score) for rec, score in
                self.query(collection_name, text, top_k=top_k)
                if score >= min_score]
