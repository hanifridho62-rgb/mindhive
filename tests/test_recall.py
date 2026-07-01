# tests/test_recall.py
from mindhive import events, projector, recall


def test_recall_matches_all_terms():
    events.append_event("caveat", {"text": "incubate entries are 90% of loss"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "default OFF for ofi gate"},
                        session_id="s", ts="2026-06-20T11:00:00Z")
    projector.project()
    hits = recall.recall("ofi gate")
    assert len(hits) == 1
    assert "ofi" in hits[0]["snippet"].lower()

def test_recall_empty_when_no_match():
    events.append_event("caveat", {"text": "nothing here"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    projector.project()
    assert recall.recall("nonexistent term") == []

def test_recall_ranks_by_term_overlap():
    """A doc matching more query terms ranks above one matching fewer, and each
    hit carries a numeric score so callers can threshold the tail."""
    events.append_event("decision", {"text": "ofi gate threshold tuned"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    events.append_event("caveat", {"text": "ofi only, nothing else here"},
                        session_id="s", ts="2026-06-20T11:00:00Z")
    projector.project()
    hits = recall.recall("ofi gate threshold")
    assert len(hits) == 2                      # OR-match: both share >=1 term
    assert hits[0]["score"] >= hits[1]["score"]
    assert "threshold" in hits[0]["snippet"].lower()   # the 3-term doc ranks first
    assert hits[1]["score"] == 1                        # the other shares only "ofi"


def test_recall_partial_match_not_empty():
    """OR-match: a query where only some terms appear still returns the doc
    (the old AND-match returned [] and silently lost the result)."""
    events.append_event("decision", {"text": "default OFF for ofi gate"},
                        session_id="s", ts="2026-06-20T11:00:00Z")
    projector.project()
    hits = recall.recall("ofi missingword")
    assert len(hits) == 1
    assert hits[0]["score"] == 1


def test_recall_returns_bare_filename():
    """Lexical recall must return a bare ledger filename (no path separators),
    matching the shape of vectors.semantic_recall."""
    events.append_event("decision", {"text": "default OFF for ofi gate"},
                        session_id="s", ts="2026-06-20T11:00:00Z")
    projector.project()
    hits = recall.recall("ofi gate")
    assert hits
    f = hits[0]["file"]
    assert "/" not in f and "\\" not in f
    assert f.endswith(".md")
