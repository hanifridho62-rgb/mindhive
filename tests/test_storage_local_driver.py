# tests/test_storage_local_driver.py
"""`LocalEventStore` — the SPI over the file ledger.

Two things are being pinned. First, that the driver adds coordinates without
changing BYTES: the segment after an SPI append must be exactly what
`events.append_event` would have written on its own, because the ledger is
hash-chained and a driver that added a field would move every head. Second,
that the coordinates it reports are the ones `chain.py` would compute — the
same agreement `tests/test_storage_records.py` pins for the fold, here through
the driver that a caller actually touches.
"""
import json
import os
from pathlib import Path

import pytest

from mindhive import chain, events, paths
from mindhive.core.models import GENESIS_PREV_HASH
from mindhive.storage import records
from mindhive.storage.local_driver import (STATE_FILE, STATE_LOCK_NAME,
                                           LocalEventStore)


def _store(**kw):
    return LocalEventStore(**kw)


def _seg(session_id="s1"):
    return paths.events_dir() / events.segment_name(session_id)


# --------------------------------------------------------------------------
# append: the coordinates, and only the coordinates
# --------------------------------------------------------------------------

def test_the_first_row_is_line_one_off_genesis():
    rec = _store().append("s1", "decision", {"text": "a"})
    assert (rec.seg_line, rec.prev_hash, rec.event_id) \
        == (1, GENESIS_PREV_HASH, "s1:1")


def test_each_record_chains_onto_the_one_before():
    store = _store()
    first = store.append("s1", "decision", {"text": "a"})
    second = store.append("s1", "decision", {"text": "b"})
    assert second.seg_line == 2
    assert second.prev_hash == first.chain_hash


def test_the_segment_is_byte_identical_to_what_append_event_writes():
    """The driver must add NOTHING to the row. A sixth key would move every
    head in the store the next time `hive verify` ran."""
    _store().append("s1", "decision", {"text": "a"})
    through_spi = _seg().read_bytes()
    _seg().unlink()
    events.append_event("decision", {"text": "a"}, session_id="s1",
                        ts=json.loads(through_spi)["ts"])
    assert _seg().read_bytes() == through_spi


def test_the_head_after_three_appends_is_the_head_chain_computes():
    store = _store()
    for i in range(3):
        rec = store.append("s1", "decision", {"text": f"d{i}"})
    assert rec.chain_hash == chain.write_chain()["head"]
    assert chain.verify()["ok"] is True


def test_two_segments_fold_to_the_namespace_head():
    store = _store()
    store.append("alpha", "decision", {"text": "a"})
    store.append("beta", "decision", {"text": "b"})
    store.append("alpha", "decision", {"text": "c"})
    assert chain.write_chain()["head"]
    assert chain.verify()["ok"] is True


def test_the_tags_reach_the_row():
    rec = _store().append("s1", "decision", {"text": "a"}, tags=["x", "y"])
    assert rec.tags == ["x", "y"]
    assert json.loads(_seg().read_text(encoding="utf-8"))["tags"] == ["x", "y"]


# --------------------------------------------------------------------------
# append under interleaving
# --------------------------------------------------------------------------

def test_a_row_written_by_someone_else_in_between_is_counted():
    """A hook appending to the same segment between two SPI calls. The third
    SPI record is on PHYSICAL line 3, not line 2."""
    store = _store()
    store.append("s1", "decision", {"text": "first"})
    events.append_event("decision", {"text": "the hook's"}, session_id="s1")
    third = store.append("s1", "decision", {"text": "third"})
    assert third.seg_line == 3
    assert third.event_id == "s1:3"


def test_the_interleaved_row_is_chained_through_not_over():
    store = _store()
    store.append("s1", "decision", {"text": "first"})
    events.append_event("decision", {"text": "the hook's"}, session_id="s1")
    third = store.append("s1", "decision", {"text": "third"})
    assert third.chain_hash == chain.write_chain()["head"]


def test_a_byte_identical_duplicate_reports_the_later_line():
    """The documented limit of content matching. It costs a `seg_line`, never
    a row: the ledger holds both, and `read_stream` returns both."""
    store = _store()
    stamp = "2026-01-01T00:00:00Z"
    events.append_event("decision", {"text": "same"}, session_id="s1", ts=stamp)
    rec = store.append("s1", "decision", {"text": "same"})
    if rec.timestamp == stamp:                      # the collision really happened
        assert rec.seg_line == 2
    assert len(list(store.read_stream("s1"))) == 2


def test_a_segment_rewritten_underneath_the_driver_refolds_from_genesis():
    """An append-only ledger does not shrink. If it did, the cached head
    attests to bytes that are gone, and chaining onto it would be a fiction."""
    store = _store()
    store.append("s1", "decision", {"text": "a"})
    store.append("s1", "decision", {"text": "b"})
    rows = _seg().read_text(encoding="utf-8").rstrip("\n").split("\n")
    _seg().write_text(rows[0] + "\n", encoding="utf-8")   # truncate to one row
    rec = store.append("s1", "decision", {"text": "c"})
    assert rec.seg_line == 2
    assert rec.chain_hash == chain.write_chain()["head"]


# --------------------------------------------------------------------------
# read_stream
# --------------------------------------------------------------------------

def test_reading_back_gives_the_records_append_returned():
    store = _store()
    written = [store.append("s1", "decision", {"text": f"d{i}"})
               for i in range(4)]
    assert list(store.read_stream("s1")) == written


def test_from_line_is_inclusive_and_one_based():
    store = _store()
    for i in range(4):
        store.append("s1", "decision", {"text": f"d{i}"})
    assert [r.seg_line for r in store.read_stream("s1", from_line=2)] == [2, 3, 4]
    assert [r.seg_line for r in store.read_stream("s1", from_line=1)] == [1, 2, 3, 4]


def test_a_line_past_the_end_is_an_empty_stream():
    store = _store()
    store.append("s1", "decision", {"text": "a"})
    assert list(store.read_stream("s1", from_line=99)) == []


def test_an_unknown_session_is_an_empty_stream_not_an_error():
    assert list(_store().read_stream("never-written")) == []


def test_a_record_read_at_from_line_still_carries_its_own_prev_hash():
    """The reason this path does not use the fold cache: a reader starting at
    line 3 still needs the two hashes before it."""
    store = _store()
    written = [store.append("s1", "decision", {"text": f"d{i}"})
               for i in range(3)]
    (third,) = list(store.read_stream("s1", from_line=3))
    assert third.prev_hash == written[1].chain_hash


# --------------------------------------------------------------------------
# list_sessions
# --------------------------------------------------------------------------

def test_the_sessions_are_the_segment_stems_sorted():
    store = _store()
    store.append("zeta", "decision", {"text": "a"})
    store.append("alpha", "decision", {"text": "b"})
    assert store.list_sessions() == ["alpha", "zeta"]


def test_no_events_dir_is_no_sessions():
    assert _store().list_sessions() == []


def test_a_quarantined_id_appears_under_the_name_it_was_stored_as():
    """That IS the id the store can address it by."""
    store = _store(strict_session=False)
    rec = store.append("../escape", "decision", {"text": "a"})
    (name,) = store.list_sessions()
    assert name.startswith(events.QUARANTINE_PREFIX)
    assert rec.session_id == "../escape"          # the row keeps the real id
    assert list(store.read_stream("../escape"))[0].seg_line == 1


def test_a_quarantined_segment_reads_back_by_the_name_it_is_listed_under():
    """The other half of the contract above: the listed `unsafe-<hash>` stem
    is the id the store can ADDRESS the segment by. `events.segment_name`
    quarantines that stem a second time -- the reservation that keeps a
    caller from writing into a quarantine slot by naming it -- so a read by
    the listed name found an empty, never-written segment, and `hive db
    migrate` walked every quarantined session reading nothing."""
    store = _store(strict_session=False)
    store.append("../escape", "decision", {"text": "a"})
    store.append("../escape", "decision", {"text": "b"})
    (name,) = store.list_sessions()
    assert [r.seg_line for r in store.read_stream(name)] == [1, 2]
    assert store.read_events(name)[0].session_id == "../escape"
    assert [r.seg_line for r in store.read_stream(name, from_line=2)] == [2]
    # The canary: naming the slot still does not let a WRITE into it. A
    # fail-open store quarantines the name again, elsewhere; a strict one
    # refuses it outright. The listed segment keeps exactly its two rows.
    store.append(name, "decision", {"text": "c"})
    assert len(store.list_sessions()) == 2
    assert len(list(store.read_stream(name))) == 2
    with pytest.raises(ValueError, match="Format session_id"):
        _store().append(name, "decision", {"text": "d"})


@pytest.mark.parametrize("stem,verbatim", [
    ("unsafe-1ba7343c47dc442d", True),
    ("UNSAFE-1ba7343c47dc442d", True),            # NTFS is case-blind
    ("unsafe-unstringifiable", True),
    ("unsafe-../escape", False),                  # no traversal by naming
    ("../escape", False),
    ("s1", False),
])
def test_a_listed_stem_names_its_own_file_only_when_it_is_a_safe_component(
        stem, verbatim):
    """`records.listed_segment_name` is the inverse of `list_sessions`, for
    reads only. A quarantine stem is its own file; anything else -- a stem
    that would not be one component, a raw id -- is what `segment_name`
    makes of it, so the reservation holds and nothing traverses."""
    name = records.listed_segment_name(stem)
    assert records.is_quarantine_stem(stem) is verbatim
    if verbatim:
        assert name == stem + ".jsonl"
    else:
        assert name == events.segment_name(stem)
    assert "/" not in name and "\\" not in name and ".." not in name


def test_a_strict_store_refuses_the_bad_id_instead_of_renaming_it():
    with pytest.raises(ValueError, match="Format session_id"):
        _store().append("../escape", "decision", {"text": "a"})
    assert _store().list_sessions() == []


# --------------------------------------------------------------------------
# fsync
# --------------------------------------------------------------------------

def test_the_durable_append_really_calls_fsync(monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real(fd))
    _store(fsync=True).append("s1", "decision", {"text": "a"})
    assert len(calls) == 1


def test_fsync_off_costs_no_flush(monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real(fd))
    _store(fsync=False).append("s1", "decision", {"text": "a"})
    assert calls == []


def test_the_driver_defaults_to_durable_and_the_hook_path_does_not():
    """The split is the design: the hook appends on every tool call and must
    not pay a disk flush per event."""
    assert _store().fsync is True
    assert _store().strict_session is True
    sig = events.append_event.__defaults__ or ()
    assert events.append_event.__kwdefaults__ == {"strict_session": False,
                                                  "fsync": False}


# --------------------------------------------------------------------------
# sync_state
# --------------------------------------------------------------------------

def test_sync_state_writes_the_sidecar_under_the_project_dir():
    store = _store()
    store.append("s1", "decision", {"text": "a"})
    got = store.sync_state()
    target = paths.project_dir() / STATE_FILE
    assert got == {"driver": "LocalEventStore", "sessions": 1,
                   "persisted": True, "path": str(target)}
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["sessions"]["s1"]["lines"] == 1
    assert len(doc["sessions"]["s1"]["head"]) == 64


def test_the_sidecar_is_never_written_to_the_store_root():
    """`paths.STORE_ROOT_FILES` is a declared set; a sidecar minted beside it
    makes a project directory out of a file."""
    store = _store()
    store.append("s1", "decision", {"text": "a"})
    store.sync_state()
    assert not (paths.store_home() / STATE_FILE).exists()


def test_sync_state_takes_the_namespace_lock_and_gives_it_back(monkeypatch):
    seen = []
    from mindhive.storage import local_driver as mod
    real = mod.file_lock

    def _spy(path, policy, *, name=None, **kw):
        seen.append((Path(path).name, name, policy.timeout_s))
        return real(path, policy, name=name, **kw)

    monkeypatch.setattr(mod, "file_lock", _spy)
    store = _store()
    store.append("s1", "decision", {"text": "a"})
    store.sync_state()
    assert seen == [(STATE_LOCK_NAME, STATE_LOCK_NAME, 5.0)]
    assert not (paths.project_dir() / STATE_LOCK_NAME).exists()


def test_the_write_is_atomic_and_leaves_no_temp_behind():
    store = _store()
    store.append("s1", "decision", {"text": "a"})
    store.sync_state()
    leftovers = [p.name for p in paths.project_dir().glob(STATE_FILE + ".*")]
    assert leftovers == []


def test_sync_state_on_an_empty_store_writes_an_empty_map():
    doc_path = paths.project_dir() / STATE_FILE
    assert _store().sync_state()["sessions"] == 0
    assert json.loads(doc_path.read_text(encoding="utf-8"))["sessions"] == {}


# --------------------------------------------------------------------------
# what the cache actually buys
# --------------------------------------------------------------------------

def _counting(monkeypatch):
    """Count the records BUILT — one SHA-256 chain step each. That is what the
    cache saves; the segment is still read in full, and the docstring says so
    rather than this test pretending otherwise."""
    built = []
    real = records.build_record
    monkeypatch.setattr(records, "build_record",
                        lambda ev, n, prev: built.append(n) or real(ev, n, prev))
    return built


def test_a_second_append_rehashes_only_the_new_row(monkeypatch):
    store = _store()
    store.append("s1", "decision", {"text": "a"})
    store.append("s1", "decision", {"text": "b"})
    built = _counting(monkeypatch)
    store.append("s1", "decision", {"text": "c"})
    assert built == [3]


def test_a_fresh_store_resumes_from_the_sidecar_instead_of_refolding(monkeypatch):
    first = _store()
    for i in range(4):
        first.append("s1", "decision", {"text": f"d{i}"})
    first.sync_state()

    built = _counting(monkeypatch)
    resumed = _store()
    rec = resumed.append("s1", "decision", {"text": "fifth"})
    assert built == [5], "the four already-folded rows were hashed again"
    assert rec.seg_line == 5
    assert rec.chain_hash == chain.write_chain()["head"]


def test_a_fresh_store_with_no_sidecar_folds_from_genesis(monkeypatch):
    first = _store()
    for i in range(3):
        first.append("s1", "decision", {"text": f"d{i}"})
    built = _counting(monkeypatch)
    rec = _store().append("s1", "decision", {"text": "fourth"})
    assert built == [1, 2, 3, 4]
    assert rec.chain_hash == chain.write_chain()["head"]


@pytest.mark.parametrize("doc", [
    "{not json",
    '{"sessions": {"s1": {"lines": "many", "head": "x"}}}',
    '{"sessions": {"s1": {"head": "' + "a" * 64 + '"}}}',
    '{"sessions": {"s1": {"lines": 2, "head": "too-short"}}}',
    '["a list"]',
], ids=["torn", "bad-count", "no-count", "short-head", "not-an-object"])
def test_an_unusable_sidecar_means_fold_from_genesis_not_a_crash(doc):
    """A projection that can make the driver refuse to work is worse than no
    projection."""
    store = _store()
    for i in range(2):
        store.append("s1", "decision", {"text": f"d{i}"})
    (paths.project_dir() / STATE_FILE).write_text(doc, encoding="utf-8")
    rec = _store().append("s1", "decision", {"text": "third"})
    assert rec.seg_line == 3
    assert rec.chain_hash == chain.write_chain()["head"]


def test_the_sidecar_is_read_once_not_per_append():
    store = _store()
    store.append("s1", "decision", {"text": "a"})
    (paths.project_dir() / STATE_FILE).write_text("{not json", encoding="utf-8")
    store.append("s1", "decision", {"text": "b"})      # would raise if re-read
    assert store._loaded is True


# --------------------------------------------------------------------------
# the inherited half, over the real driver
# --------------------------------------------------------------------------

def test_read_events_walks_every_session():
    store = _store()
    store.append("beta", "decision", {"text": "b"})
    store.append("alpha", "decision", {"text": "a"})
    assert [r.event_id for r in store.read_events()] == ["alpha:1", "beta:1"]


def test_the_slice_over_the_real_driver():
    store = _store()
    for i in range(5):
        store.append("s1", "decision", {"text": f"d{i}"})
    assert [r.seg_line for r in store.get_session_slice("s1", 2, 4)] == [2, 3, 4]


def test_the_module_never_splits_a_ledger_line_itself():
    """This module names `events_dir(`, so `tests/test_architecture_drift.py`
    forbids it a splitter of its own. Pinned locally too, so the failure names
    the module."""
    from mindhive.storage import local_driver as mod
    text = Path(mod.__file__).read_text(encoding="utf-8")
    assert "events_dir(" in text
    assert '.split("\\n")' not in text
    assert ".splitlines()" not in text
