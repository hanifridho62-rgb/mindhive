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


def test_recall_ranks_by_relevance():
    """A doc matching more (and rarer) query terms ranks above one matching
    fewer, and each hit carries a positive numeric score so callers can
    threshold the tail."""
    events.append_event("decision", {"text": "ofi gate threshold tuned"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    events.append_event("caveat", {"text": "ofi only, nothing else here"},
                        session_id="s", ts="2026-06-20T11:00:00Z")
    projector.project()
    hits = recall.recall("ofi gate threshold")
    assert len(hits) == 2                                # both share >=1 term
    assert hits[0]["score"] > hits[1]["score"]           # 3-term doc ranks first
    assert hits[1]["score"] > 0
    assert "threshold" in hits[0]["snippet"].lower()


def test_recall_partial_match_not_empty():
    """A query where only some terms appear still returns the doc (an AND-match
    would return [] and silently lose the result)."""
    events.append_event("decision", {"text": "default OFF for ofi gate"},
                        session_id="s", ts="2026-06-20T11:00:00Z")
    projector.project()
    hits = recall.recall("ofi missingword")
    assert len(hits) == 1
    assert hits[0]["score"] > 0


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


def test_recall_word_boundary_not_substring():
    """`taker` must not match `caretaker` — the old substring scorer's core
    false-positive class."""
    events.append_event("caveat", {"text": "the caretaker swept the floor"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    projector.project()
    assert recall.recall("taker") == []


def test_recall_all_stopword_query_falls_back():
    """A query of only stopwords still returns a best-effort result rather than
    ranking every doc equally at full score or crashing."""
    events.append_event("caveat", {"text": "the value is for the record"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    projector.project()
    hits = recall.recall("the for is")
    assert hits                                          # degenerate fallback engaged
    assert hits[0]["score"] > 0


def test_recall_rarity_outranks_common_term():
    """A doc containing a rare query term outranks a doc containing only a common
    (high-document-frequency) query term, even if the common term repeats."""
    # "gate" is common (appears in 3 docs); "recvwindow" is rare (1 doc).
    for i in range(3):
        events.append_event("caveat", {"text": "gate gate gate tuning notes"},
                            session_id="s", ts=f"2026-06-20T1{i}:00:00Z")
    events.append_event("decision", {"text": "recvwindow drift on the gate"},
                        session_id="s", ts="2026-06-20T20:00:00Z")
    projector.project()
    hits = recall.recall("gate recvwindow")
    assert hits[0]["score"] > hits[1]["score"]
    assert "recvwindow" in hits[0]["snippet"].lower()    # the rare-term doc wins


def test_recall_tag_boost():
    """Two docs with equal body matches rank the one carrying the term in its
    tags frontmatter higher."""
    events.append_event("decision", {"text": "notes about sharpe over the run"},
                        session_id="s", tags=["sharpe"], ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "notes about sharpe over the run"},
                        session_id="s", tags=[], ts="2026-06-20T11:00:00Z")
    projector.project()
    hits = recall.recall("sharpe")
    assert len(hits) == 2
    assert hits[0]["score"] > hits[1]["score"]           # tagged doc ranks first


def test_recall_threshold_drops_weak_tail():
    """One strong multi-term hit plus a long tail of weak single-common-term hits
    -> the tail below the relative threshold is not returned."""
    events.append_event("decision", {"text": "fee tier maker taker rebate schedule"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    for i in range(6):
        events.append_event("caveat", {"text": "generic fee mention only"},
                            session_id="s", ts=f"2026-06-20T1{i}:30:00Z")
    projector.project()
    hits = recall.recall("fee tier maker taker rebate")
    assert hits[0]["score"] > 0
    # The strong hit stays; the weak "fee"-only tail is cut, so we don't return
    # all 7 docs.
    assert len(hits) < 7
    assert "maker" in hits[0]["snippet"].lower()


def test_recall_threshold_every_hit_above_cut():
    """Every returned hit (except a guaranteed single best) scores at or above
    the relative-with-floor cut."""
    events.append_event("decision", {"text": "fee tier maker taker rebate schedule"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    for i in range(6):
        events.append_event("caveat", {"text": "generic fee mention only"},
                            session_id="s", ts=f"2026-06-20T1{i}:30:00Z")
    projector.project()
    hits = recall.recall("fee tier maker taker rebate")
    top = hits[0]["score"]
    cut = max(top * recall._THRESHOLD_RATIO, recall._THRESHOLD_FLOOR)
    for h in hits[1:]:
        assert h["score"] >= cut


def test_recall_single_weak_best_hit_retained():
    """A lone hit weaker than the floor is still returned — the i==0 always-keep
    guard fires so recall never returns [] when something matched."""
    events.append_event("caveat", {"text": "a for the is only stopwords plus fee"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    projector.project()
    hits = recall.recall("fee")
    assert len(hits) == 1                             # the single match survives


def test_recall_regression_fee_tier_query():
    """Audit regression: `real fee tier maker taker` must rank the fee/cost entry
    above governance-config entries (the original failure returned the reverse)."""
    events.append_event("decision",
                        {"text": "real maker taker fee tier is ~8bps taker cost"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    events.append_event("decision",
                        {"text": "use type hard when the metric is a scalar number"},
                        session_id="s", ts="2026-06-20T11:00:00Z")
    events.append_event("decision",
                        {"text": "decide for each question whether to use the browser"},
                        session_id="s", ts="2026-06-20T12:00:00Z")
    projector.project()
    hits = recall.recall("real fee tier maker taker")
    assert hits
    assert "fee" in hits[0]["snippet"].lower()
    assert "tier" in hits[0]["snippet"].lower()


# --- which semantic index answers -------------------------------------------
#
# `--semantic` has two indexes behind it now. A file-level hit points at
# LEDGER/decisions.md and shows 200 characters of it; an event-level hit IS the
# decision, with its timestamp and session. These pin the routing between them,
# because getting it wrong is invisible: both return plausible-looking hits.


def _fake_vectors(monkeypatch, *, count, event_hits, file_hits):
    """Replace the vector layer wholesale. Records what was asked of it."""
    from mindhive import vectors
    seen = {"events": 0, "files": 0}

    def _count(project=None):
        return count

    def _events(query, project=None, limit=10, force=False, types=None):
        seen["events"] += 1
        return list(event_hits)

    def _files(query, project=None, limit=10, force=False):
        seen["files"] += 1
        return list(file_hits)

    monkeypatch.setattr(vectors, "indexed_event_count", _count)
    monkeypatch.setattr(vectors, "semantic_recall_events", _events)
    monkeypatch.setattr(vectors, "semantic_recall", _files)
    return seen


_EVENT_HIT = {"id": "abc", "type": "decision", "ts": "2026-01-01T00:00:00Z",
              "session_id": "alpha", "snippet": "the memo itself", "score": 0.8}
_FILE_HIT = {"file": "decisions.md", "snippet": "a file", "score": 0.5}


def test_semantic_recall_prefers_the_event_index(monkeypatch):
    seen = _fake_vectors(monkeypatch, count=42, event_hits=[_EVENT_HIT],
                         file_hits=[_FILE_HIT])
    hits = recall.recall("postgres", semantic=True)
    assert hits == [_EVENT_HIT]
    assert seen == {"events": 1, "files": 0}


def test_semantic_recall_falls_back_when_nothing_is_indexed(monkeypatch):
    """The event index is empty until `hive embed` has run once. Returning
    nothing there would read as "no such memory" rather than "not indexed yet",
    which is the more damaging of the two errors."""
    seen = _fake_vectors(monkeypatch, count=0, event_hits=[_EVENT_HIT],
                         file_hits=[_FILE_HIT])
    hits = recall.recall("postgres", semantic=True)
    assert hits == [_FILE_HIT]
    assert seen == {"events": 0, "files": 1}, (
        "the event index was queried despite being empty")


def test_semantic_recall_falls_back_when_the_event_index_has_no_answer(
        monkeypatch):
    """A populated index that misses this particular query still leaves the
    coarser file-level index worth asking."""
    seen = _fake_vectors(monkeypatch, count=42, event_hits=[],
                         file_hits=[_FILE_HIT])
    assert recall.recall("postgres", semantic=True) == [_FILE_HIT]
    assert seen == {"events": 1, "files": 1}
