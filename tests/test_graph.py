# tests/test_graph.py
import json
from mindhive import events, paths
from mindhive.graph import build, related, _ev_id


def _make_evs():
    """Craft a deterministic set of events for graph tests."""
    return [
        events.Event(
            ts="2026-06-20T09:00:00Z", session_id="sess1", type="session_start",
            payload={}, tags=[],
        ),
        events.Event(
            ts="2026-06-20T09:01:00Z", session_id="sess1", type="file_touched",
            payload={"path": "src/foo.py"}, tags=["python"],
        ),
        events.Event(
            ts="2026-06-20T09:02:00Z", session_id="sess1", type="decision",
            payload={"text": "Use TDD"}, tags=["arch", "python"],
        ),
        events.Event(
            ts="2026-06-20T09:03:00Z", session_id="sess1", type="caveat",
            payload={"text": "Watch cache"}, tags=["arch"],
        ),
    ]


def test_build_creates_session_nodes():
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    node_ids = {n["id"] for n in g["nodes"]}
    assert "session:sess1" in node_ids


def test_build_creates_file_nodes():
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    node_ids = {n["id"] for n in g["nodes"]}
    assert "file:src/foo.py" in node_ids


def test_build_creates_tag_nodes():
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    node_ids = {n["id"] for n in g["nodes"]}
    assert "tag:python" in node_ids
    assert "tag:arch" in node_ids


def test_build_creates_event_nodes():
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    node_ids = {n["id"] for n in g["nodes"]}
    assert _ev_id(evs[1]) in node_ids
    assert _ev_id(evs[2]) in node_ids


def test_build_event_nodes_carry_kind_and_text():
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    nodes = {n["id"]: n for n in g["nodes"]}
    dec = nodes[_ev_id(evs[2])]
    assert dec["kind"] == "decision"
    assert dec["text"] == "Use TDD"
    # event without payload text still gets a kind, and no text field
    ft = nodes[_ev_id(evs[1])]
    assert ft["kind"] == "file_touched"
    assert "text" not in ft


def test_build_in_session_edges():
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    assert (_ev_id(evs[1]), "session:sess1", "in_session") in edges
    assert (_ev_id(evs[2]), "session:sess1", "in_session") in edges


def test_build_touched_edges():
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    assert (_ev_id(evs[1]), "file:src/foo.py", "touched") in edges


def test_build_tagged_edges():
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    # file_touched has tag "python"
    assert (_ev_id(evs[1]), "tag:python", "tagged") in edges
    # decision has tags arch + python
    assert (_ev_id(evs[2]), "tag:arch", "tagged") in edges


def test_build_next_edges():
    """Consecutive semantic events get a 'next' edge."""
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    # decision -> caveat are consecutive semantic events
    assert (_ev_id(evs[2]), _ev_id(evs[3]), "next") in edges


def test_build_shares_tag_edges():
    """Pairs of semantic events sharing >=1 tag get shares_tag edge."""
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    # decision(arch,python) and caveat(arch) share "arch"
    found = (
        (_ev_id(evs[2]), _ev_id(evs[3]), "shares_tag") in edges
        or (_ev_id(evs[3]), _ev_id(evs[2]), "shares_tag") in edges
    )
    assert found


def test_shares_tag_skips_mega_hub_bucket():
    """A tag shared by more than SHARES_TAG_MAX_BUCKET semantic events is a
    mega-hub with no discriminating signal; pairing inside it would emit an
    O(n^2) edge explosion. Those pairs must be skipped, while a small, specific
    co-tag bucket still gets its shares_tag edge."""
    from mindhive.graph import SHARES_TAG_MAX_BUCKET

    hub_n = SHARES_TAG_MAX_BUCKET + 5
    evs = [
        events.Event(ts=f"2026-06-20T10:{i:02d}:00Z", session_id="s",
                     type="decision", payload={"text": f"d{i}"}, tags=["auto"])
        for i in range(hub_n)
    ]
    # two of the hub events ALSO share a small, specific tag
    evs[0].tags.append("narrow")
    evs[1].tags.append("narrow")

    g = build(project=None, _evs=evs)
    shares = [(e["src"], e["dst"]) for e in g["edges"] if e["rel"] == "shares_tag"]
    # The mega-hub "auto" (hub_n events) contributes nothing quadratic...
    assert len(shares) == 1
    # ...but the narrow two-event co-tag survives.
    pair = {_ev_id(evs[0]), _ev_id(evs[1])}
    assert {shares[0][0], shares[0][1]} == pair


def _write_events(evs):
    for e in evs:
        events.append_event(e.type, e.payload, session_id=e.session_id,
                            tags=e.tags, ts=e.ts)


def _make_cache_fresh():
    """Stamp graph.json newer than the event segments so the cache reads fresh."""
    import os
    import time
    gfile = paths.project_dir(None) / "graph.json"
    future = time.time() + 10
    os.utime(gfile, (future, future))
    return gfile


def test_load_or_build_serves_fresh_cache(monkeypatch):
    """When graph.json is at least as fresh as the event log, load_or_build
    returns the cached graph WITHOUT rebuilding."""
    import mindhive.graph as gmod

    _write_events(_make_evs())
    gmod.build(project=None)          # persist a real graph.json
    _make_cache_fresh()

    monkeypatch.setattr(gmod, "build", lambda *a, **k:
                        (_ for _ in ()).throw(AssertionError("rebuilt on a fresh cache")))
    g = gmod.load_or_build(project=None)
    assert "session:sess1" in {n["id"] for n in g["nodes"]}


def test_load_or_build_rebuilds_when_stale(monkeypatch):
    """A graph.json older than the event segments must be rebuilt."""
    import os
    import mindhive.graph as gmod

    _write_events(_make_evs())
    gmod.build(project=None)
    os.utime(paths.project_dir(None) / "graph.json", (1, 1))  # ancient -> stale

    calls = {"n": 0}
    real = gmod.build
    monkeypatch.setattr(gmod, "build",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1])
    gmod.load_or_build(project=None)
    assert calls["n"] == 1


def test_load_or_build_semantic_bypasses_cache(monkeypatch):
    """Semantic graphs are never served from the deterministic cache."""
    import mindhive.graph as gmod

    _write_events(_make_evs())
    gmod.build(project=None)
    _make_cache_fresh()               # a fresh cache exists...

    calls = {"n": 0}
    real = gmod.build
    monkeypatch.setattr(gmod, "build",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1])
    gmod.load_or_build(project=None, semantic=True)
    assert calls["n"] == 1            # ...yet semantic still rebuilds


def test_load_or_build_evs_factory_lazy_on_hit():
    """build_evs is invoked only on a cache miss, never on a hit — so the
    (potentially large) aggregate read is skipped when the cache is warm."""
    import mindhive.graph as gmod

    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _make_evs()

    gmod.load_or_build(project=None, build_evs=factory)   # miss -> builds + persists
    assert calls["n"] == 1
    _make_cache_fresh()
    gmod.load_or_build(project=None, build_evs=factory)   # hit -> factory untouched
    assert calls["n"] == 1


def test_build_stamps_algorithm_version():
    """build() records the algorithm version in the result and on disk, so a
    later load can tell a fresh cache apart from one built by an older
    algorithm (e.g. before the shares_tag cap)."""
    import mindhive.graph as gmod
    g = gmod.build(project=None, _evs=_make_evs())
    assert g["version"] == gmod.GRAPH_BUILD_VERSION
    on_disk = json.loads((paths.project_dir(None) / "graph.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == gmod.GRAPH_BUILD_VERSION


def test_load_or_build_rejects_versionless_cache():
    """A fresh-mtime graph.json with NO version (built by the pre-cap algorithm)
    must be rebuilt, not served — otherwise a dormant project whose events never
    change keeps serving a stale giant forever."""
    import mindhive.graph as gmod
    _write_events(_make_evs())
    gfile = paths.project_dir(None) / "graph.json"
    gfile.parent.mkdir(parents=True, exist_ok=True)
    # a versionless cache carrying a sentinel a real build never produces
    gfile.write_text(json.dumps(
        {"nodes": [{"id": "STALE_SENTINEL", "type": "tag", "label": "x"}], "edges": []}),
        encoding="utf-8")
    _make_cache_fresh()                        # newer than the events
    g = gmod.load_or_build(project=None)
    assert "STALE_SENTINEL" not in {n["id"] for n in g["nodes"]}   # rebuilt, not served
    assert g.get("version") == gmod.GRAPH_BUILD_VERSION


def test_load_or_build_rejects_outdated_version_cache():
    """A fresh cache stamped with an older algorithm version is rebuilt."""
    import mindhive.graph as gmod
    _write_events(_make_evs())
    gfile = paths.project_dir(None) / "graph.json"
    gfile.write_text(json.dumps(
        {"version": gmod.GRAPH_BUILD_VERSION - 1,
         "nodes": [{"id": "OLD_SENTINEL", "type": "tag", "label": "x"}], "edges": []}),
        encoding="utf-8")
    _make_cache_fresh()
    g = gmod.load_or_build(project=None)
    assert "OLD_SENTINEL" not in {n["id"] for n in g["nodes"]}
    assert g["version"] == gmod.GRAPH_BUILD_VERSION


def test_distinct_events_same_ts_type_do_not_collide():
    """Two different events sharing a timestamp AND type must not overwrite each
    other — the node id includes a payload hash to keep them distinct."""
    evs = [
        events.Event(ts="2026-06-20T09:00:00Z", session_id="s", type="decision",
                     payload={"text": "alpha"}, tags=[]),
        events.Event(ts="2026-06-20T09:00:00Z", session_id="s", type="decision",
                     payload={"text": "beta"}, tags=[]),
    ]
    g = build(project=None, _evs=evs)
    ev_nodes = [n for n in g["nodes"] if n["type"] == "event"]
    assert len(ev_nodes) == 2
    assert {n.get("text") for n in ev_nodes} == {"alpha", "beta"}
    # the two ids are genuinely different
    assert _ev_id(evs[0]) != _ev_id(evs[1])


def test_build_persists_graph_json():
    evs = _make_evs()
    build(project=None, _evs=evs)
    graph_file = paths.project_dir() / "graph.json"
    assert graph_file.exists()
    data = json.loads(graph_file.read_text(encoding="utf-8"))
    assert "nodes" in data and "edges" in data


def test_build_semantic_false_no_error():
    """semantic=False must never fail even if vector deps missing."""
    evs = _make_evs()
    g = build(project=None, semantic=False, _evs=evs)
    assert "nodes" in g
    assert "semantic_error" not in g


def test_a_failed_semantic_pass_is_recorded_not_hidden(monkeypatch, caplog):
    """The semantic edges stay optional -- a failure still yields the
    deterministic graph -- but a graph that asked for them and silently has
    none is indistinguishable from one whose memos are just unrelated."""
    from mindhive import vectors
    boom = vectors.SemanticQueryError("query", vectors.LEDGER_COLLECTION,
                                      RuntimeError("Error loading hnsw index"))

    def _raise(*_a, **_k):
        raise boom

    monkeypatch.setattr(vectors, "semantic_recall", _raise)
    with caplog.at_level("WARNING", logger="mindhive.graph"):
        g = build(project=None, semantic=True, _evs=_make_evs())
    assert "nodes" in g
    assert not [e for e in g["edges"] if e.get("rel") == "semantic_near"]
    assert g["semantic_error"] == str(boom)
    assert "hnsw" in caplog.text


def test_a_semantic_failure_is_returned_but_never_persisted(monkeypatch):
    """The error belongs to the build that asked for semantic edges.
    graph.json is what every later viewer is served, semantic or not, and a
    failure written into it would outlive the index that caused it."""
    from mindhive import vectors

    def _raise(*_a, **_k):
        raise vectors.SemanticQueryError(
            "query", vectors.LEDGER_COLLECTION, RuntimeError("hnsw"))

    monkeypatch.setattr(vectors, "semantic_recall", _raise)
    g = build(project=None, semantic=True, _evs=_make_evs())
    assert "semantic_error" in g
    persisted = json.loads((paths.project_dir() / "graph.json").read_text(
        encoding="utf-8"))
    assert "semantic_error" not in persisted
    assert persisted["nodes"] == g["nodes"]


def test_a_semantic_pass_that_worked_records_no_error(monkeypatch):
    from mindhive import vectors
    monkeypatch.setattr(vectors, "semantic_recall", lambda *a, **k: [])
    g = build(project=None, semantic=True, _evs=_make_evs())
    assert "semantic_error" not in g


def _near(edges):
    return [e for e in edges if e.get("rel") == "semantic_near"]


def _linking_recall(monkeypatch, fail_from=None, error=None):
    """`semantic_recall` answering every ledger entry as a hit, so each query
    links its memo to the others -- and raising from call `fail_from` on."""
    from mindhive import vectors
    from mindhive.projector import build_ledger_entries
    files = [fn for fn, _c, _ev in build_ledger_entries(_make_evs())]
    calls = []

    def _recall(body, **_k):
        calls.append(body)
        if fail_from is not None and len(calls) >= fail_from:
            raise error or vectors.SemanticQueryError(
                "query", vectors.LEDGER_COLLECTION, RuntimeError("hnsw"))
        return [{"file": f} for f in files]

    monkeypatch.setattr(vectors, "semantic_recall", _recall)
    return calls


def test_a_completed_semantic_pass_links_its_memos(monkeypatch):
    """The control for the test below: this double does produce edges."""
    _linking_recall(monkeypatch)
    g = build(project=None, semantic=True, _evs=_make_evs())
    assert _near(g["edges"]) and "semantic_error" not in g


def test_a_semantic_pass_that_fails_midway_keeps_none_of_its_edges(
        monkeypatch):
    """The semantic layer is all or nothing. A pass that linked the first memo
    and then failed handed the viewer -- and graph.json, which every later
    viewer is served without the error -- a partial layer that reads as the
    whole of it."""
    calls = _linking_recall(monkeypatch, fail_from=2)
    g = build(project=None, semantic=True, _evs=_make_evs())
    assert len(calls) == 2           # the first query did answer, and link
    assert g["semantic_error"]
    assert _near(g["edges"]) == []
    persisted = json.loads((paths.project_dir() / "graph.json").read_text(
        encoding="utf-8"))
    assert _near(persisted["edges"]) == []


def test_a_semantic_failure_without_a_message_still_names_itself(monkeypatch):
    """`str(RuntimeError())` is '' -- and an empty error is falsy, so the page
    showed no banner over a graph that had lost its semantic edges."""
    from mindhive import graphview
    _linking_recall(monkeypatch, fail_from=1, error=RuntimeError())
    g = build(project=None, semantic=True, _evs=_make_evs())
    assert g["semantic_error"] == "RuntimeError"
    assert '"semantic_error":"RuntimeError"' in graphview.render_html(g)


def test_related_finds_file_node():
    evs = _make_evs()
    build(project=None, _evs=evs)
    neighbors = related("foo.py", project=None)
    ids = [n["id"] for n in neighbors]
    # file:src/foo.py should be in neighbors (depth=1 from the event)
    # or the event that touched it
    assert any("foo" in nid for nid in ids)


def test_related_returns_event_that_touched_file():
    evs = _make_evs()
    build(project=None, _evs=evs)
    # Query the file path substring
    neighbors = related("src/foo.py", project=None)
    ids = [n["id"] for n in neighbors]
    assert _ev_id(evs[1]) in ids


def test_related_sorted_by_id():
    evs = _make_evs()
    build(project=None, _evs=evs)
    neighbors = related("python", project=None)
    ids = [n["id"] for n in neighbors]
    assert ids == sorted(ids)


def test_related_includes_event_text():
    evs = _make_evs()
    build(project=None, _evs=evs)
    neighbors = related("decision", project=None)
    dec = [n for n in neighbors if n["id"] == _ev_id(evs[2])]
    assert dec and dec[0].get("text") == "Use TDD"
    # nodes without text don't grow a spurious empty field
    tag = [n for n in neighbors if n["type"] == "tag"]
    assert all("text" not in n for n in tag)


def _catalog_event(ts, scanned_at, rec_name="free-plugin"):
    return events.Event(
        ts=ts, session_id="s", type="plugin_catalog", tags=[],
        payload={
            "scanned_at": scanned_at,
            "free_plugins": [{
                "marketplace": "mkt", "name": "free-plugin", "version": "1.2.0",
                "description": "d", "free": True,
                "skills": [{"name": "alpha", "trigger": "Use when testing."}],
                "topics": ["test"],
                "score": {"trigger_count": 1, "topic_spread": 1, "breadth_score": 2.0},
            }],
            "paid_plugins": [],
            "recommendations": [{"name": rec_name, "breadth_score": 2.0, "note": "n"}],
        },
    )


def _seed_catalog(scanned_at, project=None):
    """Write a plugin catalog sidecar (the new source the graph expands from)."""
    from mindhive import plugins
    plugins._write_catalog(_catalog_event(scanned_at, scanned_at).payload, project=project)


def test_plugin_catalog_expands_into_nodes():
    _seed_catalog("2026-06-21T10:00:00Z")
    g = build(project=None, _evs=[])
    ids = {n["id"] for n in g["nodes"]}
    assert "plugin:mkt/free-plugin" in ids
    assert "skill:free-plugin/alpha" in ids
    assert "topic:test" in ids
    assert "recommendation:2026-06-21T10:00:00Z" in ids
    # the catalog is NOT a ledger event, so no event node for it
    assert not any(nid.startswith("ev:") and "plugin_catalog" in nid for nid in ids)


def test_plugin_catalog_edges():
    _seed_catalog("2026-06-21T10:00:00Z")
    g = build(project=None, _evs=[])
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    assert ("plugin:mkt/free-plugin", "skill:free-plugin/alpha", "provides") in edges
    assert ("plugin:mkt/free-plugin", "topic:test", "covers") in edges
    assert ("recommendation:2026-06-21T10:00:00Z", "plugin:mkt/free-plugin", "recommends") in edges


def test_latest_plugin_catalog_supersedes():
    # the sidecar is overwritten in place — only the most recent snapshot remains
    _seed_catalog("2026-06-21T10:00:00Z")
    _seed_catalog("2026-06-21T11:00:00Z")
    g = build(project=None, _evs=[])
    ids = {n["id"] for n in g["nodes"]}
    assert "recommendation:2026-06-21T11:00:00Z" in ids
    assert "recommendation:2026-06-21T10:00:00Z" not in ids
