# tests/test_chain.py
from mindhive import events, chain

def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")

def test_verify_ok_after_write(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    assert chain.verify()["ok"] is True

def test_verify_detects_tamper(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    seg.write_text(seg.read_text(encoding="utf-8").replace("a", "b"), encoding="utf-8")
    assert chain.verify()["ok"] is False

def test_verify_without_chain_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    assert chain.verify()["ok"] is False
