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
    events.append_event("decision", {"text": "alpha"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    # Replace payload value only — keeps valid JSON structure
    seg.write_text(seg.read_text(encoding="utf-8").replace('"alpha"', '"beta"'), encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"

def test_verify_corrupt_event_log(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    # Append a corrupt (unparseable) JSONL line
    with open(seg, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "corrupt_event_log"

def test_verify_without_chain_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    assert chain.verify()["ok"] is False
