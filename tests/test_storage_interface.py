# tests/test_storage_interface.py
"""`storage.interface` — the contract, and what it refuses to let a driver skip.

Three abstract methods and five concrete ones. The concrete five are tested
against a toy in-memory store rather than a real driver on purpose: what is
being pinned is that the BASE CLASS gets them right, so a backend author
writes `append`/`read_stream`/`list_sessions` and inherits behaviour that has
already been thought about. A test that exercised them through
`LocalEventStore` would pass just as well if the base class were empty and the
driver reimplemented everything, which is the arrangement this module exists
to prevent.
"""
import inspect
from pathlib import Path

import pytest

from mindhive.core.models import GENESIS_PREV_HASH, EventRecord
from mindhive.storage import interface
from mindhive.storage.interface import DataIntegrityError, EventStore, VectorIndex


class _Toy(EventStore):
    """The minimum a backend may be: three methods, no cache, no I/O."""

    def __init__(self):
        self.rows: dict = {}

    def append(self, session_id, event_type, payload, tags=None):
        held = self.rows.setdefault(session_id, [])
        prev = held[-1].chain_hash if held else GENESIS_PREV_HASH
        rec = EventRecord(event_id=f"{session_id}:{len(held) + 1}",
                          session_id=session_id, seg_line=len(held) + 1,
                          timestamp=f"2026-01-01T00:00:{len(held):02d}Z",
                          event_type=event_type, payload=payload,
                          tags=list(tags or []), prev_hash=prev,
                          chain_hash=f"h{session_id}{len(held) + 1}")
        held.append(rec)
        return rec

    def read_stream(self, session_id, from_line=1):
        return iter([r for r in self.rows.get(session_id, [])
                     if r.seg_line >= from_line])

    def list_sessions(self):
        return sorted(self.rows)


def _filled():
    store = _Toy()
    for i in range(3):
        store.append("alpha", "decision", {"text": f"a{i}"})
    for i in range(2):
        store.append("beta", "caveat", {"text": f"b{i}"})
    return store


# --------------------------------------------------------------------------
# the abstract half
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["append", "read_stream", "list_sessions"])
def test_a_backend_cannot_skip_the_primitive(name):
    ns = {n: (lambda self, *a, **k: None)
          for n in ("append", "read_stream", "list_sessions") if n != name}
    Partial = type("Partial", (EventStore,), ns)
    with pytest.raises(TypeError, match=name):
        Partial()


@pytest.mark.parametrize("name", ["embed_batch", "query"])
def test_a_vector_backend_cannot_skip_the_primitive(name):
    ns = {n: (lambda self, *a, **k: None)
          for n in ("embed_batch", "query") if n != name}
    Partial = type("Partial", (VectorIndex,), ns)
    with pytest.raises(TypeError, match=name):
        Partial()


def test_the_three_that_are_abstract_are_exactly_these_three():
    """A fourth abstract method is a breaking change to every driver; it
    should be a deliberate edit here, not a quiet one there."""
    assert EventStore.__abstractmethods__ == frozenset(
        {"append", "read_stream", "list_sessions"})
    assert VectorIndex.__abstractmethods__ == frozenset({"embed_batch", "query"})


# --------------------------------------------------------------------------
# the concrete half
# --------------------------------------------------------------------------

def test_append_event_is_the_module_functions_argument_order():
    store = _Toy()
    rec = store.append_event("decision", {"text": "x"}, session_id="s1",
                             tags=["t"])
    assert (rec.session_id, rec.event_type, rec.tags) == ("s1", "decision", ["t"])


def test_append_event_is_an_alias_and_not_a_second_writer():
    """Two spellings of one write is how the two drift. It must reach
    `append` — the method a driver overrides — and add nothing."""
    seen = []

    class _Spy(_Toy):
        def append(self, session_id, event_type, payload, tags=None):
            seen.append((session_id, event_type, payload, tags))
            return super().append(session_id, event_type, payload, tags)

    _Spy().append_event("caveat", {"text": "y"}, session_id="s2", tags=None)
    assert seen == [("s2", "caveat", {"text": "y"}, None)]


def test_read_events_without_a_session_walks_every_session_sorted():
    got = _filled().read_events()
    assert [r.event_id for r in got] == ["alpha:1", "alpha:2", "alpha:3",
                                         "beta:1", "beta:2"]


def test_read_events_with_a_session_is_that_session_only():
    assert [r.event_id for r in _filled().read_events("beta")] == ["beta:1",
                                                                  "beta:2"]


def test_read_events_of_an_unknown_session_is_empty():
    assert _filled().read_events("nope") == []


def test_the_slice_is_inclusive_at_both_ends():
    assert [r.seg_line for r in _filled().get_session_slice("alpha", 1, 2)] \
        == [1, 2]
    assert [r.seg_line for r in _filled().get_session_slice("alpha", 2, 3)] \
        == [2, 3]


def test_the_slice_runs_to_the_end_when_no_upper_bound_is_given():
    assert [r.seg_line for r in _filled().get_session_slice("alpha", 2)] == [2, 3]


def test_an_inverted_slice_is_empty_rather_than_an_error():
    """A slice is a question about what is there, and `nothing` is a truthful
    answer to a range containing nothing."""
    assert _filled().get_session_slice("alpha", 3, 1) == []


def test_the_slice_stops_reading_at_the_upper_bound():
    """`break`, not a filter: on a session of 40k rows the difference is the
    whole stream."""
    pulled = []

    class _Counting(_Toy):
        def read_stream(self, session_id, from_line=1):
            for rec in super().read_stream(session_id, from_line):
                pulled.append(rec.seg_line)
                yield rec

    store = _Counting()
    for _ in range(6):
        store.append("s", "decision", {"text": "x"})
    assert [r.seg_line for r in store.get_session_slice("s", 1, 2)] == [1, 2]
    assert pulled == [1, 2, 3]      # one over the edge is what `break` costs


def test_sync_state_is_honest_about_caching_nothing():
    got = _Toy().sync_state()
    assert got["persisted"] is False and got["driver"] == "_Toy"
    assert _filled().sync_state()["sessions"] == 2


# --------------------------------------------------------------------------
# VectorIndex's own concrete half
# --------------------------------------------------------------------------

class _ToyIndex(VectorIndex):
    def __init__(self, hits=()):
        self.hits = list(hits)
        self.added = []

    def embed_batch(self, collection_name, records):
        self.added.append((collection_name, list(records)))
        return len(list(records))

    def query(self, collection_name, text, top_k=5):
        return self.hits[:top_k]


def test_query_similarity_drops_what_nobody_would_act_on():
    idx = _ToyIndex([("a", 0.9), ("b", 0.4), ("c", 0.05)])
    assert idx.query_similarity("c", "q", min_score=0.5) == [("a", 0.9)]
    assert idx.query_similarity("c", "q") == [("a", 0.9), ("b", 0.4), ("c", 0.05)]


def test_query_similarity_passes_top_k_through():
    idx = _ToyIndex([("a", 0.9), ("b", 0.8), ("c", 0.7)])
    assert len(idx.query_similarity("c", "q", top_k=2)) == 2


# --------------------------------------------------------------------------
# the error, and the module's own cleanliness
# --------------------------------------------------------------------------

def test_the_integrity_error_is_a_value_error():
    """Callers that already handle `this data is wrong` keep working."""
    assert issubclass(DataIntegrityError, ValueError)
    with pytest.raises(ValueError):
        raise DataIntegrityError("x")


def test_reading_the_contract_opens_no_connection_and_loads_no_model():
    """Stdlib only. A contract a caller reads must not drag psycopg, chromadb
    or a 90 MB encoder onto the import graph."""
    text = Path(interface.__file__).read_text(encoding="utf-8")
    for heavy in ("psycopg", "chromadb", "sentence_transformers", "numpy",
                  "pgmirror", "vectors"):
        assert f"import {heavy}" not in text, heavy
    imported = [n.__name__ for n in vars(interface).values()
                if inspect.ismodule(n)]
    assert imported == [] or imported == ["abc"], imported
