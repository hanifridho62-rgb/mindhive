# tests/test_recall.py
from mindhive import events, projector, recall

def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")

def test_recall_matches_all_terms(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("caveat", {"text": "incubate entries are 90% of loss"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "default OFF for ofi gate"},
                        session_id="s", ts="2026-06-20T11:00:00Z")
    projector.project()
    hits = recall.recall("ofi gate")
    assert len(hits) == 1
    assert "ofi" in hits[0]["snippet"].lower()

def test_recall_empty_when_no_match(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("caveat", {"text": "nothing here"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    projector.project()
    assert recall.recall("nonexistent term") == []
