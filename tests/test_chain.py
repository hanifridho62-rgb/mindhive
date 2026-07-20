# tests/test_chain.py
from mindhive import events, chain


def test_verify_ok_after_write():
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    assert chain.verify()["ok"] is True

def test_verify_detects_tamper(tmp_path):
    events.append_event("decision", {"text": "alpha"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    # Replace payload value only — keeps valid JSON structure
    seg.write_text(seg.read_text(encoding="utf-8").replace('"alpha"', '"beta"'), encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"

def test_appended_garbage_keeps_verify_ok_but_shows_in_corrupt_rows(tmp_path):
    # New contract: reads are TOTAL. An appended unparseable line is not a
    # logical event, so it neither crashes verify nor changes the chain — a torn
    # write must never take the hive down. verify() stays ok (the sealed chain of
    # real events is intact); events.corrupt_rows() is where the garbage is
    # surfaced loudly. Tamper-evidence for ALTERED or DELETED existing events is
    # still caught as hash_mismatch (see test_verify_detects_tamper).
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert chain.verify()["ok"] is True
    bad = events.corrupt_rows("test")
    assert len(bad) == 1
    assert "unparseable json" in bad[0]["reason"]


def test_verify_detects_delete_by_corruption(tmp_path):
    # Deleting a sealed event by corrupting its line is still caught: read_all
    # skips the row, so the recomputed chain head no longer matches chain.json.
    events.append_event("decision", {"text": "one"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "two"}, session_id="s",
                        ts="2026-06-20T11:00:00Z")
    chain.write_chain()
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    seg.write_text(
        seg.read_text(encoding="utf-8").replace('"text":"one"', "GARBAGE"),
        encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"

def test_verify_corrupt_chain_file(tmp_path):
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    # Truncate/corrupt chain.json itself — verify must return a verdict, not raise.
    (tmp_path / "test" / "chain.json").write_text("{not json", encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "corrupt_chain_file"


def test_verify_without_chain_file():
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    assert chain.verify()["ok"] is False

def test_write_chain_accepts_pre_parsed_events():
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    from_disk = chain.write_chain()
    evs = events.read_all()
    from_mem = chain.write_chain(evs=evs)
    # Passing the events in must produce an identical chain to reading from disk.
    assert from_mem == from_disk

def test_project_reads_event_log_only_once(monkeypatch):
    """projector.project must not re-read the event log inside write_chain."""
    from mindhive import projector
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


def test_verify_accepts_pre_read_events():
    """verify(evs=...) must equal verify() — lets the overview poll reuse the
    events it already read instead of a second full event-log read."""
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    evs = events.read_all()
    assert chain.verify(evs=evs) == chain.verify()
