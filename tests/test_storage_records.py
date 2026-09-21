# tests/test_storage_records.py
"""`storage.records` — the one fold, and its agreement with `chain`.

The value of this module is entirely in that agreement. If `record_row` drops
a key, or `build_record` chains over different material, or `segment_rows`
numbers a line differently from `events._segment_lines`, then every record the
SPI hands out carries a `chain_hash` that is internally consistent and
disagrees with the checkpoint on disk — and nothing would notice, because both
sides fold their own material. So the pins below are not "does it return a
record"; they are "is it the SAME number `chain.py` computes".
"""
import json

import pytest

from mindhive import chain, events, paths
from mindhive.core.models import GENESIS_PREV_HASH, EventRecord
from mindhive.storage import records


def _write(session_id, lines, project=None):
    """Bytes into a segment, bypassing `append_event` — several cases here need
    a blank line, a torn row or an unterminated tail, none of which the writer
    can produce."""
    d = paths.events_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    seg = d / events.segment_name(session_id)
    seg.write_text("".join(lines), encoding="utf-8")
    return seg


def _row(ts, text, session_id="s1", etype="decision"):
    return events.canonical_json({"ts": ts, "session_id": session_id,
                                  "type": etype, "payload": {"text": text},
                                  "tags": []}) + "\n"


def _append(n, session_id="s1"):
    return [events.append_event("decision", {"text": f"d{i}"},
                                session_id=session_id) for i in range(n)]


# --------------------------------------------------------------------------
# record_row: the five keys, and only the five
# --------------------------------------------------------------------------

def test_the_row_is_byte_identical_to_the_event_it_came_from():
    (ev,) = _append(1)
    rec = records.build_record(ev, 1, GENESIS_PREV_HASH)
    assert events.canonical_json(records.record_row(rec)) \
        == events.canonical_json(events.to_dict(ev))


def test_the_row_carries_none_of_the_chain_coordinates():
    """A sixth key would move every head in the store on the first verify."""
    (ev,) = _append(1)
    rec = records.build_record(ev, 7, "ab" * 32)
    assert set(records.record_row(rec)) == {"ts", "session_id", "type",
                                            "payload", "tags"}
    for absent in ("event_id", "seg_line", "prev_hash", "chain_hash"):
        assert absent not in records.record_row(rec)


def test_the_tags_list_is_copied_not_shared():
    """The record is frozen; its `tags` list is not, and a caller that mutated
    the list it got back would change the material the hash was taken over."""
    ev = events.append_event("decision", {"text": "x"}, tags=["a"],
                             session_id="s1")
    rec = records.build_record(ev, 1, GENESIS_PREV_HASH)
    row = records.record_row(rec)
    row["tags"].append("b")
    assert rec.tags == ["a"]


# --------------------------------------------------------------------------
# build_record: the id is the PHYSICAL address
# --------------------------------------------------------------------------

def test_the_event_id_is_session_and_physical_line():
    (ev,) = _append(1)
    assert records.build_record(ev, 42, GENESIS_PREV_HASH).event_id == "s1:42"


def test_two_identical_events_at_two_lines_are_two_records():
    """Content addressing (`vectors.event_id`) would collapse them; the ledger
    holds two rows and the SPI speaks the ledger's coordinates."""
    ev = events.Event(ts="2026-01-01T00:00:00Z", session_id="s1",
                      type="decision", payload={"text": "same"}, tags=[])
    first = records.build_record(ev, 1, GENESIS_PREV_HASH)
    second = records.build_record(ev, 2, first.chain_hash)
    assert first.event_id != second.event_id
    assert first.chain_hash != second.chain_hash


def test_the_record_is_frozen():
    (ev,) = _append(1)
    rec = records.build_record(ev, 1, GENESIS_PREV_HASH)
    assert isinstance(rec, EventRecord)
    with pytest.raises(Exception):
        rec.seg_line = 2


# --------------------------------------------------------------------------
# fold_rows vs chain: the same number, or the SPI is lying
# --------------------------------------------------------------------------

def test_the_head_over_one_segment_is_the_head_chain_computes():
    evs = _append(3)
    seg = paths.events_dir() / events.segment_name("s1")
    folded = records.fold_rows(records.segment_rows(seg))
    assert folded[-1].chain_hash == chain.compute_chain(evs)["head"]


def test_each_record_chains_onto_the_one_before_it():
    _append(4)
    seg = paths.events_dir() / events.segment_name("s1")
    folded = records.fold_rows(records.segment_rows(seg))
    assert folded[0].prev_hash == GENESIS_PREV_HASH
    for earlier, later in zip(folded, folded[1:]):
        assert later.prev_hash == earlier.chain_hash


def test_a_fold_resumed_from_a_head_equals_the_whole_fold():
    """The property the drivers' caches rest on: lines 3.. folded onto line 2's
    head are the same records as folding 1.. and slicing."""
    _append(5)
    seg = paths.events_dir() / events.segment_name("s1")
    rows = records.segment_rows(seg)
    whole = records.fold_rows(rows)
    resumed = records.fold_rows(rows[2:], whole[1].chain_hash)
    assert [r.chain_hash for r in resumed] == [r.chain_hash for r in whole[2:]]
    assert [r.event_id for r in resumed] == [r.event_id for r in whole[2:]]


def test_one_head_carried_across_segments_in_name_order_is_the_whole_namespace():
    """The migration engine's central claim, pinned against `chain.verify`'s
    own arithmetic: the namespace head is the segments' folds in NAME order,
    chained end to end."""
    _append(2, session_id="alpha")
    _append(3, session_id="beta")
    d = paths.events_dir()
    head = GENESIS_PREV_HASH
    for seg in sorted(d.glob("*.jsonl"), key=lambda p: p.name):
        folded = records.fold_rows(records.segment_rows(seg), head)
        if folded:
            head = folded[-1].chain_hash
    assert head == chain.write_chain()["head"]
    assert chain.verify()["ok"] is True


def test_an_empty_row_list_folds_to_the_head_it_was_given():
    assert records.fold_rows([]) == []
    assert records.fold_rows([], "ff" * 32) == []


# --------------------------------------------------------------------------
# segment_rows: 1-based, total, complete lines only
# --------------------------------------------------------------------------

def test_the_line_numbers_are_the_readers_index_plus_one():
    _append(3)
    seg = paths.events_dir() / events.segment_name("s1")
    mine = [n for n, _ev in records.segment_rows(seg)]
    theirs = [i for i, _text in events._segment_lines(seg)]
    assert mine == [i + 1 for i in theirs] == [1, 2, 3]


def test_a_blank_line_is_skipped_but_still_counted():
    """The failure this prevents: a reader that renumbered around a blank line
    would hand the mirror a join key meaning a different row."""
    seg = _write("s1", [_row("2026-01-01T00:00:00Z", "a"),
                        "\n",
                        _row("2026-01-01T00:00:01Z", "b")])
    got = records.segment_rows(seg)
    assert [n for n, _ in got] == [1, 3]
    assert [ev.payload["text"] for _, ev in got] == ["a", "b"]


def test_a_torn_row_is_skipped_but_still_counted():
    seg = _write("s1", [_row("2026-01-01T00:00:00Z", "a"),
                        '{"ts": "x", "session_id"\n',
                        _row("2026-01-01T00:00:01Z", "b")])
    assert [n for n, _ in records.segment_rows(seg)] == [1, 3]


def test_a_row_missing_a_required_field_is_skipped():
    seg = _write("s1", [json.dumps({"ts": "t", "session_id": "s1",
                                    "type": "decision"}) + "\n",
                        _row("2026-01-01T00:00:01Z", "b")])
    assert [n for n, _ in records.segment_rows(seg)] == [2]


def test_an_unterminated_tail_is_not_a_row():
    """Those bytes are still mutable — the next append concatenates onto them
    — so a record built over them would attest to something that can change."""
    seg = _write("s1", [_row("2026-01-01T00:00:00Z", "a"),
                        _row("2026-01-01T00:00:01Z", "b").rstrip("\n")])
    assert [n for n, _ in records.segment_rows(seg)] == [1]


def test_a_finished_tail_becomes_a_row_once_the_newline_lands():
    seg = _write("s1", [_row("2026-01-01T00:00:00Z", "a"),
                        _row("2026-01-01T00:00:01Z", "b").rstrip("\n")])
    assert len(records.segment_rows(seg)) == 1
    with open(seg, "a", encoding="utf-8") as f:
        f.write("\n")
    assert [n for n, _ in records.segment_rows(seg)] == [1, 2]


def test_an_empty_segment_has_no_rows():
    assert records.segment_rows(_write("s1", [])) == []


def test_the_rows_that_survive_are_the_rows_chain_folds():
    """CANARY over the skip rules: a segment carrying a blank line, a torn row
    and an unterminated tail must fold to the head `chain` computes for it,
    or the two disagree about which rows exist."""
    _write("s1", [_row("2026-01-01T00:00:00Z", "a"),
                  "\n",
                  '{"broken"\n',
                  _row("2026-01-01T00:00:01Z", "b"),
                  _row("2026-01-01T00:00:02Z", "c").rstrip("\n")])
    seg = paths.events_dir() / events.segment_name("s1")
    folded = records.fold_rows(records.segment_rows(seg))
    assert [r.seg_line for r in folded] == [1, 4]
    assert folded[-1].chain_hash == chain.write_chain()["head"]


def test_canary_the_module_never_splits_a_line_itself():
    """`tests/test_architecture_drift.py` owns the general rule; this is the
    local one, so the failure names this module."""
    from pathlib import Path
    text = Path(records.__file__).read_text(encoding="utf-8")
    # The CALL, not the word: this module's own docstring explains the rule,
    # and a canary that forbade the explanation would forbid documenting it.
    assert '.split("\\n")' not in text
    assert ".splitlines()" not in text
    assert "_segment_parts" in text
