# tests/test_graph.py
import json
from mindhive import events, paths
from mindhive.graph import build, related


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


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


def test_build_creates_session_nodes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    node_ids = {n["id"] for n in g["nodes"]}
    assert "session:sess1" in node_ids


def test_build_creates_file_nodes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    node_ids = {n["id"] for n in g["nodes"]}
    assert "file:src/foo.py" in node_ids


def test_build_creates_tag_nodes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    node_ids = {n["id"] for n in g["nodes"]}
    assert "tag:python" in node_ids
    assert "tag:arch" in node_ids


def test_build_creates_event_nodes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    node_ids = {n["id"] for n in g["nodes"]}
    assert "ev:2026-06-20T09:01:00Z:file_touched" in node_ids
    assert "ev:2026-06-20T09:02:00Z:decision" in node_ids


def test_build_event_nodes_carry_kind_and_text(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    nodes = {n["id"]: n for n in g["nodes"]}
    dec = nodes["ev:2026-06-20T09:02:00Z:decision"]
    assert dec["kind"] == "decision"
    assert dec["text"] == "Use TDD"
    # event without payload text still gets a kind, and no text field
    ft = nodes["ev:2026-06-20T09:01:00Z:file_touched"]
    assert ft["kind"] == "file_touched"
    assert "text" not in ft


def test_build_in_session_edges(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    assert ("ev:2026-06-20T09:01:00Z:file_touched", "session:sess1", "in_session") in edges
    assert ("ev:2026-06-20T09:02:00Z:decision", "session:sess1", "in_session") in edges


def test_build_touched_edges(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    assert ("ev:2026-06-20T09:01:00Z:file_touched", "file:src/foo.py", "touched") in edges


def test_build_tagged_edges(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    # file_touched has tag "python"
    assert ("ev:2026-06-20T09:01:00Z:file_touched", "tag:python", "tagged") in edges
    # decision has tags arch + python
    assert ("ev:2026-06-20T09:02:00Z:decision", "tag:arch", "tagged") in edges


def test_build_next_edges(tmp_path, monkeypatch):
    """Consecutive semantic events get a 'next' edge."""
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    # decision -> caveat are consecutive semantic events
    assert ("ev:2026-06-20T09:02:00Z:decision", "ev:2026-06-20T09:03:00Z:caveat", "next") in edges


def test_build_shares_tag_edges(tmp_path, monkeypatch):
    """Pairs of semantic events sharing >=1 tag get shares_tag edge."""
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, _evs=evs)
    edges = {(e["src"], e["dst"], e["rel"]) for e in g["edges"]}
    # decision(arch,python) and caveat(arch) share "arch"
    found = (
        ("ev:2026-06-20T09:02:00Z:decision", "ev:2026-06-20T09:03:00Z:caveat", "shares_tag") in edges
        or ("ev:2026-06-20T09:03:00Z:caveat", "ev:2026-06-20T09:02:00Z:decision", "shares_tag") in edges
    )
    assert found


def test_build_persists_graph_json(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    build(project=None, _evs=evs)
    graph_file = paths.project_dir() / "graph.json"
    assert graph_file.exists()
    data = json.loads(graph_file.read_text(encoding="utf-8"))
    assert "nodes" in data and "edges" in data


def test_build_semantic_false_no_error(tmp_path, monkeypatch):
    """semantic=False must never fail even if vector deps missing."""
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    g = build(project=None, semantic=False, _evs=evs)
    assert "nodes" in g


def test_related_finds_file_node(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    build(project=None, _evs=evs)
    neighbors = related("foo.py", project=None)
    ids = [n["id"] for n in neighbors]
    # file:src/foo.py should be in neighbors (depth=1 from the event)
    # or the event that touched it
    assert any("foo" in nid for nid in ids)


def test_related_returns_event_that_touched_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    build(project=None, _evs=evs)
    # Query the file path substring
    neighbors = related("src/foo.py", project=None)
    ids = [n["id"] for n in neighbors]
    assert "ev:2026-06-20T09:01:00Z:file_touched" in ids


def test_related_sorted_by_id(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    build(project=None, _evs=evs)
    neighbors = related("python", project=None)
    ids = [n["id"] for n in neighbors]
    assert ids == sorted(ids)


def test_related_includes_event_text(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = _make_evs()
    build(project=None, _evs=evs)
    neighbors = related("decision", project=None)
    dec = [n for n in neighbors if n["id"] == "ev:2026-06-20T09:02:00Z:decision"]
    assert dec and dec[0].get("text") == "Use TDD"
    # nodes without text don't grow a spurious empty field
    tag = [n for n in neighbors if n["type"] == "tag"]
    assert all("text" not in n for n in tag)
