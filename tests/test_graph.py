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
