"""Phase C U1 (ADR 0002) — every graph edge carries a `source` provenance tag.

Deterministic structural edges default to EXTRACTED; the embedding-derived
semantic_near edge (when embeddings are available) carries INFERRED. We assert
the EXTRACTED default + presence of the field on the deterministic edges; the
INFERRED path needs embeddings (graceful-degraded off here) so it is exercised
by the host-side Phase C tests instead.
"""
from mindhive import events, graph


def _ev(ts, text, tags):
    return events.Event(ts=ts, session_id="s", type="decision", payload={"text": text}, tags=tags)


def test_deterministic_edges_are_extracted():
    evs = [
        _ev("2026-01-01T00:00:00Z", "alpha", ["shared"]),
        _ev("2026-01-01T00:00:01Z", "beta", ["shared"]),
    ]
    g = graph.build(project="p", _evs=evs)
    assert g["edges"], "expected deterministic temporal/shares_tag edges"
    assert all("source" in e for e in g["edges"])
    assert all(e["source"] == "EXTRACTED" for e in g["edges"])


def test_edge_shape_is_backward_compatible():
    # Existing consumers read src/dst/rel; the new key is additive.
    g = graph.build(project="p", _evs=[_ev("2026-01-01T00:00:00Z", "x", ["t"]),
                                       _ev("2026-01-01T00:00:01Z", "y", ["t"])])
    for e in g["edges"]:
        assert {"src", "dst", "rel"} <= set(e)
