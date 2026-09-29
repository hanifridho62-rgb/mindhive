# tests/test_recall.py
import pytest

from mindhive import events, paths, projector, recall


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
    seen = {"events": 0, "files": 0, "strict": []}

    def _count(project=None, *, strict=False):
        seen["strict"].append(strict)
        if isinstance(count, BaseException):
            raise count
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
    assert seen == {"events": 1, "files": 0, "strict": [True]}


def test_semantic_recall_falls_back_when_nothing_is_indexed(monkeypatch):
    """The event index is empty until `hive embed` has run once. Returning
    nothing there would read as "no such memory" rather than "not indexed yet",
    which is the more damaging of the two errors."""
    seen = _fake_vectors(monkeypatch, count=0, event_hits=[_EVENT_HIT],
                         file_hits=[_FILE_HIT])
    hits = recall.recall("postgres", semantic=True)
    assert hits == [_FILE_HIT]
    assert (seen["events"], seen["files"]) == (0, 1), (
        "the event index was queried despite being empty")


def test_semantic_recall_falls_back_when_the_event_index_has_no_answer(
        monkeypatch):
    """A populated index that misses this particular query still leaves the
    coarser file-level index worth asking."""
    seen = _fake_vectors(monkeypatch, count=42, event_hits=[],
                         file_hits=[_FILE_HIT])
    assert recall.recall("postgres", semantic=True) == [_FILE_HIT]
    assert (seen["events"], seen["files"]) == (1, 1)


# --- a failed semantic read is not an empty one -------------------------------
#
# The fallback above is for an index that is EMPTY. An index that is BROKEN used
# to take the same road: `indexed_event_count` swallowed the failure as 0, the
# file index answered, and the caller never learned the event index was gone.

def _broken():
    from mindhive import vectors
    return vectors.SemanticQueryError(
        "count", vectors.EVENT_COLLECTION, RuntimeError("Error loading hnsw"))


def test_a_broken_event_index_is_raised_not_routed_around(monkeypatch):
    from mindhive import vectors
    seen = _fake_vectors(monkeypatch, count=_broken(), event_hits=[],
                         file_hits=[_FILE_HIT])
    with pytest.raises(vectors.SemanticQueryError):
        recall.recall("postgres", semantic=True)
    assert seen["strict"] == [True], "the count was not asked strictly"
    assert seen["files"] == 0, "a broken event index fell back to the files"


def test_live_lock_refuses_semantic_recall_before_the_index_is_touched(
        monkeypatch):
    seen = _fake_vectors(monkeypatch, count=42, event_hits=[_EVENT_HIT],
                         file_hits=[_FILE_HIT])
    paths.store_home().mkdir(parents=True, exist_ok=True)
    (paths.store_home() / "LIVE_LOCK").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="LIVE_LOCK"):
        recall.recall("postgres", semantic=True)
    assert seen == {"events": 0, "files": 0, "strict": []}
    seen = _fake_vectors(monkeypatch, count=42, event_hits=[_EVENT_HIT],
                         file_hits=[_FILE_HIT])
    assert recall.recall("postgres", semantic=True, force=True) == [_EVENT_HIT]


# --- `hive recall --semantic`: the exit code says which failure it was ---------

def _recall_args(semantic=True, force=False):
    import argparse
    return argparse.Namespace(query="postgres", semantic=semantic, limit=5,
                              force=force, project=None)


def test_the_cli_exits_one_on_a_broken_index_and_says_how_to_fix_it(
        monkeypatch, capsys):
    from mindhive.commands import maintenance
    _fake_vectors(monkeypatch, count=_broken(), event_hits=[], file_hits=[])
    assert maintenance.cmd_recall(_recall_args()) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: semantic count failed")
    # A count that failed read a collection chromadb could open, which
    # `--rebuild` drops unread: the rebuild comes first, and the directory it
    # names is the delete if the read still fails.
    assert "hive embed --rebuild" in err
    assert str(paths.project_dir() / "chroma") in err


def test_the_cli_exits_two_without_the_extras_and_opens_nothing(monkeypatch,
                                                                capsys):
    """No chromadb is a refusal, like LIVE_LOCK -- not a broken index."""
    from mindhive import vectors
    from mindhive.commands import maintenance
    opened = []

    def _missing():
        raise RuntimeError("semantic layer needs extras: pip install -e .[vectors]")

    monkeypatch.setattr(vectors, "_require_deps", _missing)
    monkeypatch.setattr(vectors, "_reading",
                        lambda *a, **k: opened.append(a) or pytest.fail(
                            "the index was opened without the extras"))
    assert maintenance.cmd_recall(_recall_args()) == 2
    assert "needs extras" in capsys.readouterr().err
    assert opened == []


def test_the_cli_exits_two_on_live_lock(monkeypatch, capsys):
    from mindhive.commands import maintenance
    _fake_vectors(monkeypatch, count=42, event_hits=[], file_hits=[])
    paths.store_home().mkdir(parents=True, exist_ok=True)
    (paths.store_home() / "LIVE_LOCK").write_text("x", encoding="utf-8")
    assert maintenance.cmd_recall(_recall_args()) == 2
    assert "LIVE_LOCK" in capsys.readouterr().err


def test_the_cli_still_prints_hits_and_exits_zero(monkeypatch, capsys):
    from mindhive.commands import maintenance
    _fake_vectors(monkeypatch, count=42, event_hits=[_EVENT_HIT], file_hits=[])
    assert maintenance.cmd_recall(_recall_args()) is None
    assert "the memo itself" in capsys.readouterr().out


@pytest.mark.parametrize("limit", ["0", "-3"])
def test_the_cli_refuses_a_limit_below_one(limit, capsys):
    """`--limit 0` reads as "no limit" to whoever types it, and answered
    nothing at all; `-3` cut the keyword answer to all but its last three.
    Neither is a search, so neither runs."""
    from mindhive.commands.parsers import _build_parser
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["recall", "postgres", "--limit", limit])
    assert exc.value.code == 2
    assert "--limit" in capsys.readouterr().err


def test_a_keyword_search_below_one_hit_answers_none():
    """The library answers a limit below 1 alike in both modes: `[]`. The
    keyword path sliced `[:-3]` into all but the last three hits."""
    for n in range(5):
        events.append_event("decision", {"text": f"ofi gate note {n}"},
                            session_id="s", ts=f"2026-06-20T1{n}:00:00Z")
    projector.project()
    assert len(recall.recall("ofi gate")) == 5
    assert recall.recall("ofi gate", limit=0) == []
    assert recall.recall("ofi gate", limit=-3) == []


@pytest.mark.parametrize("limit", [0, -3])
def test_a_semantic_search_below_one_hit_asks_the_index_nothing(monkeypatch,
                                                                 limit):
    """`/api/recall?semantic=1&limit=0` reaches `recall.recall` unclamped --
    only the CLI refuses the limit. A search for no hits asks nothing: not
    the count, not either index. Asked, a broken index answered a search for
    nothing with a 500 and advice to delete it."""
    seen = _fake_vectors(monkeypatch, count=_broken(), event_hits=[_EVENT_HIT],
                         file_hits=[_FILE_HIT])
    assert recall.recall("postgres", semantic=True, limit=limit) == []
    assert seen == {"events": 0, "files": 0, "strict": []}


def test_the_dashboard_says_a_failed_search_failed():
    """`/api/recall` answers a raised error with a 500 and `{"ok": false,
    "error": ...}`, and `apiGet` never looks at the status -- so the view has
    to tell an error body from a hit list itself, or a broken index reads as
    'No matches'."""
    from mindhive import dashboard_js_views
    body = dashboard_js_views.JS.split("function runRecall(){", 1)[1]
    body = body.split("function showGraph(){", 1)[0]
    guard = body.index("if (!Array.isArray(hits))")
    line = body[guard:body.index("\n", guard)]
    assert "Recall failed" in line
    # ...and THROWS it, ahead of the "No matches" branch: run() paints a thrown
    # error with fail() and announces the run failed. Painted by hand, the
    # status chip read "searching -- ready" over the error.
    # `tests/test_recall_view_node.py` runs it.
    assert "throw new Error(" in line and "showText" not in line
    assert guard < body.index("if (!found.length && !notes)")


# --- HIVE MIND: one namespace it could not read does not sink the rest ---------

def _per_namespace(monkeypatch, broken=(), refused=None):
    """`recall.recall` per namespace: hits, a `SemanticQueryError` for a
    namespace in *broken*, or *refused* raised for every namespace."""
    from mindhive import vectors

    def fake(query, project=None, semantic=False, limit=10, force=False):
        if refused is not None:
            raise refused
        if project in broken:
            raise vectors.SemanticQueryError(
                "count", vectors.EVENT_COLLECTION,
                RuntimeError("Error loading hnsw index"), project)
        return [{"file": f"{project}.md", "snippet": "chose postgres",
                 "score": 1.0}]

    monkeypatch.setattr(recall, "recall", fake)


def test_hive_mind_recall_answers_around_a_namespace_it_could_not_read(
        monkeypatch):
    """HIVE MIND searched every namespace in one loop, so one unreadable
    index -- or a writer busy on any one of them -- turned the whole search
    into a 500 and threw the other namespaces' hits away."""
    _per_namespace(monkeypatch, broken={"bad"})
    rows = recall.recall_across("postgres", ["bad", "good"], semantic=True)
    assert [r for r in rows if "error" not in r] == [
        {"file": "good.md", "snippet": "chose postgres", "score": 1.0,
         "project": "good"}]
    [row] = [r for r in rows if "error" in r]
    assert row["project"] == "bad"
    assert "hnsw" in row["error"] and "hive embed --project bad" in row["error"]


def test_hive_mind_recall_that_read_nothing_is_the_error(monkeypatch):
    from mindhive import vectors
    _per_namespace(monkeypatch, broken={"a", "b"})
    with pytest.raises(vectors.SemanticQueryError, match="project 'a'"):
        recall.recall_across("postgres", ["a", "b"], semantic=True)


def test_a_refusal_still_refuses_the_whole_hive_mind_search(monkeypatch):
    """LIVE_LOCK and missing extras refuse every namespace alike: that is one
    answer for the search, not one row per namespace."""
    _per_namespace(monkeypatch, refused=RuntimeError("LIVE_LOCK present"))
    with pytest.raises(RuntimeError, match="LIVE_LOCK"):
        recall.recall_across("postgres", ["a", "b"], semantic=True)


def test_the_hive_mind_route_serves_every_namespace_it_could_read(
        monkeypatch):
    from mindhive import dashboard_get
    _per_namespace(monkeypatch, broken={"bad"})
    monkeypatch.setattr(dashboard_get, "_all_projects", lambda: ["bad", "good"])
    sent = []

    class _Route:
        def _json(self, obj):
            sent.append(obj)

    dashboard_get.GetRoutes._get_recall(
        _Route(), None, True, {"q": ["postgres"], "semantic": ["1"]})
    [rows] = sent
    assert [(r["project"], "error" in r) for r in rows] == [
        ("bad", True), ("good", False)]
