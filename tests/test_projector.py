# tests/test_projector.py
from mindhive import events, projector, paths


def test_state_reflects_latest_state_set():
    events.append_event("state_set",
                        {"task": "OLD", "next_step": "x", "open_questions": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    events.append_event("state_set",
                        {"task": "OFI verdict", "verified": "trio green",
                         "next_step": "recapture 24h", "open_questions": ["perp feed?"],
                         "deferred": ["F11 multi-asset"]},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "default OFF"},
                        session_id="s", ts="2026-06-20T10:30:00Z")
    projector.project()
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "OFI verdict" in state
    assert "OLD" not in state
    assert "recapture 24h" in state
    assert "perp feed?" in state
    assert "default OFF" in state

def test_ledger_one_file_per_semantic_event():
    events.append_event("decision", {"text": "d1"}, session_id="s",
                        ts="2026-06-20T10:00:00Z", tags=["strategy"])
    events.append_event("caveat", {"text": "c1"}, session_id="s",
                        ts="2026-06-20T11:00:00Z")
    events.append_event("file_touched", {"path": "x.py"}, session_id="s",
                        ts="2026-06-20T11:30:00Z")
    projector.project()
    ldir = paths.project_dir() / "LEDGER"
    files = list(ldir.glob("*.md"))
    assert len(files) == 2  # deterministic events are NOT ledger entries

def test_projection_is_idempotent():
    events.append_event("decision", {"text": "d1"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    projector.project()
    first = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    projector.project()
    second = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert first == second

def test_index_maps_tags():
    events.append_event("decision", {"text": "d1"}, session_id="s",
                        ts="2026-06-20T10:00:00Z", tags=["ofi"])
    projector.project()
    import json
    idx = json.loads((paths.project_dir() / "index.json").read_text(encoding="utf-8"))
    assert "ofi" in idx["by_tag"]
    assert "decision" in idx["by_type"]
