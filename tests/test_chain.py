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
    # A trailing event in a LATER second is what closes the second the
    # tampered event sits in — the checkpoint seals complete seconds only.
    events.append_event("decision", {"text": "closer"}, session_id="s",
                        ts="2026-06-20T10:00:01Z")
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


def test_verify_ok_when_events_appended_after_checkpoint():
    """The steady state: chain.json is a checkpoint refreshed at projection time,
    but events keep being appended after it. verify() must treat those as intact
    new history (prefix still hashes to the stored head), NOT a tamper mismatch."""
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "closer"}, session_id="s",
                        ts="2026-06-20T10:00:30Z")
    chain.write_chain()                       # checkpoint at 1 event ("a")
    events.append_event("decision", {"text": "b"}, session_id="s",
                        ts="2026-06-20T10:01:00Z")
    events.append_event("caveat", {"text": "c"}, session_id="s",
                        ts="2026-06-20T10:02:00Z")
    result = chain.verify()
    assert result["ok"] is True               # history is intact — not a mismatch
    # "closer" is unsealed too: the checkpoint stopped before its second.
    assert result["ahead"] == 3

def test_verify_no_ahead_key_when_checkpoint_current():
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    result = chain.verify()
    assert result["ok"] is True
    # The lone event is in the newest second, so nothing is sealed yet and the
    # whole log reads as ahead. "Fully up to date" now means "no complete second
    # is unsealed", which an empty checkpoint satisfies.
    assert result["ahead"] == 1

def test_verify_detects_tamper_before_checkpoint_with_later_appends():
    """A rewrite of already-checkpointed history must still be caught even when
    legitimate events were appended afterward — the prefix hash breaks."""
    events.append_event("decision", {"text": "alpha"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    # A trailing event in a LATER second is what closes the second the
    # tampered event sits in — the checkpoint seals complete seconds only.
    events.append_event("decision", {"text": "closer"}, session_id="s",
                        ts="2026-06-20T10:00:01Z")
    chain.write_chain()                       # checkpoint attests to "alpha"
    events.append_event("decision", {"text": "later"}, session_id="s",
                        ts="2026-06-20T10:05:00Z")
    from mindhive import paths
    seg = next((paths.project_dir() / "events").glob("*.jsonl"))
    seg.write_text(seg.read_text(encoding="utf-8").replace('"alpha"', '"beta"'),
                   encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"

def test_verify_detects_truncation_below_checkpoint():
    """If the log is shorter than the checkpoint claims, checkpointed history was
    deleted — genuine tamper, not a benign drift."""
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "b"}, session_id="s",
                        ts="2026-06-20T10:01:00Z")
    # A trailing event in a LATER second is what closes the second the
    # tampered event sits in — the checkpoint seals complete seconds only.
    events.append_event("decision", {"text": "closer"}, session_id="s",
                        ts="2026-06-20T10:02:00Z")
    chain.write_chain()                       # checkpoint at 2 events
    from mindhive import paths
    seg = next((paths.project_dir() / "events").glob("*.jsonl"))
    first = seg.read_text(encoding="utf-8").splitlines()[0]
    seg.write_text(first + "\n", encoding="utf-8")   # drop the 2nd event
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"

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


# --- the checkpoint seals whole seconds --------------------------------------
# `read_all` orders by (ts, segment_name, line_index): total and deterministic,
# but NOT append-monotonic. Ledger timestamps are second-resolution and the
# tie-break is the segment filename, so an event appended later with a ts equal
# to sealed rows can sort BEFORE them and shift the prefix the checkpoint
# attests to. Measured on the live store, that made verify red permanently —
# and an alarm that is always red attests to nothing.

def test_a_later_same_second_append_does_not_invalidate_the_checkpoint():
    # The segment name is the tie-break, so "a-later" sorts ahead of "m-sealed"
    # and lands INSIDE the old prefix. This is the exact live failure.
    events.append_event("decision", {"text": "one"}, session_id="m-sealed",
                        ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "two"}, session_id="m-sealed",
                        ts="2026-06-20T10:00:01Z")
    chain.write_chain()
    events.append_event("decision", {"text": "three"}, session_id="a-later",
                        ts="2026-06-20T10:00:01Z")
    result = chain.verify()
    assert result["ok"] is True
    assert result.get("ahead")


def test_the_checkpoint_stops_at_the_last_complete_second():
    events.append_event("decision", {"text": "one"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "two"}, session_id="s",
                        ts="2026-06-20T10:00:01Z")
    events.append_event("decision", {"text": "three"}, session_id="s",
                        ts="2026-06-20T10:00:01Z")
    stored = chain.write_chain()
    # Only the 10:00:00Z event is sealed; the 10:00:01Z pair stays open because
    # another 10:00:01Z event could still arrive and sort among them.
    assert stored["count"] == 1


def test_a_store_written_entirely_in_one_second_seals_nothing():
    # Deliberate, not an accident: there is no complete second to seal. The zero
    # digest is what compute_chain already returns for checkpoint 0.
    for i in range(3):
        events.append_event("decision", {"text": str(i)}, session_id="s",
                            ts="2026-06-20T10:00:00Z")
    stored = chain.write_chain()
    assert stored["count"] == 0
    result = chain.verify()
    assert result["ok"] is True and result["ahead"] == 3


def test_an_empty_ledger_seals_nothing_and_verifies():
    stored = chain.write_chain()
    assert stored["count"] == 0
    assert chain.verify()["ok"] is True


def test_a_tamper_in_the_unsealed_second_is_not_claimed_by_the_checkpoint(tmp_path):
    # The narrowed guarantee, pinned so nobody later reads it as a bug: the
    # checkpoint attests to complete seconds only. The newest second is sealed
    # by the next projection.
    events.append_event("decision", {"text": "sealed"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "open"}, session_id="s",
                        ts="2026-06-20T10:00:01Z")
    chain.write_chain()
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    seg.write_text(seg.read_text(encoding="utf-8").replace('"open"', '"edited"'),
                   encoding="utf-8")
    assert chain.verify()["ok"] is True
