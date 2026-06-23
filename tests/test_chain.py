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

def test_verify_corrupt_chain_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    # Truncate/corrupt chain.json itself — verify must return a verdict, not raise.
    (tmp_path / "test" / "chain.json").write_text("{not json", encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "corrupt_chain_file"


def test_verify_without_chain_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    assert chain.verify()["ok"] is False

def test_write_chain_accepts_pre_parsed_events(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    from_disk = chain.write_chain()
    evs = events.read_all()
    from_mem = chain.write_chain(evs=evs)
    # Passing the events in must produce an identical chain to reading from disk.
    assert from_mem == from_disk

def test_project_reads_event_log_only_once(tmp_path, monkeypatch):
    """projector.project must not re-read the event log inside write_chain."""
    from mindhive import projector
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    calls = {"n": 0}
    real = events.read_all
    def counting(project=None):
        calls["n"] += 1
        return real(project)
    monkeypatch.setattr(events, "read_all", counting)
    projector.project()
    assert calls["n"] == 1


def test_verify_accepts_pre_read_events(tmp_path, monkeypatch):
    """verify(evs=...) must equal verify() — lets the overview poll reuse the
    events it already read instead of a second full event-log read."""
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    evs = events.read_all()
    assert chain.verify(evs=evs) == chain.verify()
