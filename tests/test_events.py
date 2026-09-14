# tests/test_events.py
import json

import pytest

from mindhive import events, paths


def test_read_all_skips_malformed_rows_and_stays_total(tmp_path):
    # Ledger-schema SOP: reads stay TOTAL. A torn/interleaved write from a past
    # session — a line of unparseable JSON, or a JSON object missing a required
    # Event field — must be SKIPPED, never raised. Every dashboard endpoint and
    # every `hive` CLI command reads via read_all; one corrupt byte-run must not
    # take down the whole hive (it did: a torn `command_run` fragment 500'd every
    # dashboard section and crashed every CLI command).
    events.append_event("decision", {"text": "before"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    seg = next(paths.events_dir("test").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as f:
        f.write('{"ts":"2026-06-20T11:00:00Z","type":"decision"}\n')  # missing fields
        f.write(':"command_run"}\n')                                   # torn fragment
    events.append_event("decision", {"text": "after"}, session_id="s",
                        ts="2026-06-20T12:00:00Z")
    evs = events.read_all("test")
    assert [e.payload.get("text") for e in evs] == ["before", "after"]


def test_corrupt_rows_surfaces_skipped_rows(tmp_path):
    # The strict companion to the total read: read_all() drops malformed rows so
    # folds never crash; corrupt_rows() SURFACES them so `hive verify` and the
    # dashboard can report corruption instead of it passing silently.
    events.append_event("decision", {"text": "ok"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    seg = next(paths.events_dir("test").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as f:
        f.write('{"ts":"2026-06-20T11:00:00Z","type":"decision"}\n')  # missing fields
        f.write(':"command_run"}\n')                                   # torn fragment
    bad = events.corrupt_rows("test")
    assert len(bad) == 2
    joined = " ".join(r["reason"] for r in bad)
    assert "missing required field" in joined
    assert "unparseable json" in joined
    assert all(r["segment"] == seg.name for r in bad)


def test_read_stays_total_when_a_torn_write_splits_a_utf8_character(tmp_path):
    # The total-read contract at FILE-DECODE granularity, not just per line. A
    # crash or unserialised append mid-character leaves invalid UTF-8 bytes, and
    # `read_text(encoding="utf-8")` without errors= raises UnicodeDecodeError
    # BEFORE splitlines — so the per-line JSON guard never runs and read_all /
    # corrupt_rows take down every dashboard section and CLI command on one torn
    # byte-run, the exact outage the line-level guard exists to prevent. And it
    # is reachable: canonical_json writes non-ASCII as UTF-8 (ensure_ascii=False),
    # so a tear can land mid-character; `verify` itself reads through corrupt_rows.
    events.append_event("decision", {"text": "before"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    seg = next(paths.events_dir("test").glob("*.jsonl"))
    with open(seg, "ab") as f:              # a dangling UTF-8 lead byte: the start
        f.write(b"\xc3")                    # of a 2-byte sequence, no continuation
    # read_all must skip the torn byte-run and still return the good row.
    assert [e.payload.get("text") for e in events.read_all("test")] == ["before"]
    # corrupt_rows must SURFACE it rather than crashing verify().
    bad = events.corrupt_rows("test")
    assert any(r["segment"] == seg.name and "unparseable" in r["reason"]
               for r in bad)


def test_canonical_json_is_stable():
    a = events.canonical_json({"b": 1, "a": 2})
    b = events.canonical_json({"a": 2, "b": 1})
    assert a == b == '{"a":2,"b":1}'

def test_append_rejects_unknown_type():
    import pytest
    with pytest.raises(ValueError):
        events.append_event("bogus", {"text": "x"})

def test_user_prompt_is_valid_and_in_memory_view():
    ev = events.append_event("user_prompt", {"text": "hello"}, session_id="s",
                             ts="2026-06-23T10:00:00Z")
    assert ev.type == "user_prompt"
    assert "user_prompt" in events.VALID_TYPES
    # shows in the dashboard Memory view, but is NOT a curated semantic type
    assert "user_prompt" in events.MEMORY_VIEW_TYPES
    assert "user_prompt" not in events.SEMANTIC_TYPES

def test_append_and_read_orders_by_ts():
    events.append_event("decision", {"text": "second"}, session_id="s1",
                        ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "first"}, session_id="s2",
                        ts="2026-06-20T09:00:00Z")
    evs = events.read_all()
    assert [e.payload["text"] for e in evs] == ["first", "second"]


# --- same-second order across segments ----------------------------------------
# `ts` is second-resolution and a segment is named after its session, so two
# rows written inside one second by two sessions used to come back in FILENAME
# order -- `aaa-editor` before `zzz-writer` whichever was appended first. Every
# order-sensitive fold (sop, roadmap, mindnet) then replayed an edit before the
# record it edits. The fixture names sort OPPOSITE to append order on purpose.

SAME_SECOND = "2026-06-20T10:00:00Z"


def _texts(evs):
    return [e.payload.get("text") for e in evs]


def _write_row(session_id, text, **extra):
    """One row exactly as `canonical_json` writes it, bypassing `append_event`.
    With no `extra` it is a LEGACY row -- the five keys pre-`seq` code wrote."""
    seg = paths.events_dir() / f"{session_id}.jsonl"
    seg.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": SAME_SECOND, "session_id": session_id, "type": "decision",
           "payload": {"text": text}, "tags": [], **extra}
    with open(seg, "a", encoding="utf-8") as f:
        f.write(events.canonical_json(row) + "\n")


def _seqs_by_text():
    out = {}
    for seg in paths.events_dir().glob("*.jsonl"):
        for line in seg.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            out[row["payload"]["text"]] = row.get("seq")
    return out


def test_same_second_rows_across_segments_keep_append_order():
    events.append_event("decision", {"text": "first"}, session_id="zzz-writer",
                        ts=SAME_SECOND)
    events.append_event("decision", {"text": "second"}, session_id="aaa-editor",
                        ts=SAME_SECOND)

    assert _texts(events.read_all()) == ["first", "second"]
    assert _texts(events.read_types(["decision"])) == ["first", "second"]


def test_the_same_second_fixture_really_does_invert_under_a_filename_tie_break():
    """CANARY for the test above: the segment that was written SECOND sorts
    FIRST by name, so a reader breaking the tie on filename returns the rows
    reversed -- the test above passes because the key changed, not because the
    fixture happens to agree with filename order."""
    events.append_event("decision", {"text": "first"}, session_id="zzz-writer",
                        ts=SAME_SECOND)
    events.append_event("decision", {"text": "second"}, session_id="aaa-editor",
                        ts=SAME_SECOND)

    names = sorted(p.name for p in paths.events_dir().glob("*.jsonl"))
    assert names == ["aaa-editor.jsonl", "zzz-writer.jsonl"]


def test_ts_still_outranks_append_order():
    """`seq` only breaks ties inside one second. A backdated row -- which
    `incidents.audit` writes on purpose -- still sorts by its ts."""
    events.append_event("decision", {"text": "later"}, session_id="s1",
                        ts="2026-06-20T10:00:01Z")
    events.append_event("decision", {"text": "backdated"}, session_id="s2",
                        ts="2026-06-20T10:00:00Z")

    assert _texts(events.read_all()) == ["backdated", "later"]


def test_append_stamps_a_strictly_increasing_seq_across_segments():
    for text, session in [("a", "zzz"), ("b", "aaa"), ("c", "mmm"), ("d", "aaa")]:
        events.append_event("decision", {"text": text}, session_id=session,
                            ts=SAME_SECOND)

    seqs = _seqs_by_text()
    assert all(type(seqs[t]) is int for t in "abcd")
    assert seqs["a"] < seqs["b"] < seqs["c"] < seqs["d"]


def test_seq_keeps_increasing_when_the_wall_clock_steps_backwards(monkeypatch):
    """The clock is adjustable (w32time can step it), so the stamp is
    `max(clock, last + 1)` against a high-water mark kept under the append lock.
    A stamp taken from the clock alone would fail this: the second reading is
    a full second EARLIER than the first."""
    readings = iter([2_000_000_000_000_000, 1_999_999_999_000_000,
                     1_999_999_999_500_000])                      # microseconds
    monkeypatch.setattr(events, "_now_us", lambda: next(readings))
    for text, session in [("a", "zzz"), ("b", "aaa"), ("c", "mmm")]:
        events.append_event("decision", {"text": text}, session_id=session,
                            ts=SAME_SECOND)

    seqs = _seqs_by_text()
    assert seqs["a"] < seqs["b"] < seqs["c"]
    assert _texts(events.read_all()) == ["a", "b", "c"]


def test_seq_rides_the_row_but_not_the_event():
    """`Event` keeps its five fields, so `to_dict` -- the chain's material for
    legacy rows and the shape every consumer serialises -- is unchanged."""
    ev = events.append_event("decision", {"text": "x"}, session_id="s",
                             ts=SAME_SECOND)

    assert "seq" not in events.to_dict(ev)
    assert "seq" not in events.to_dict(events.read_all()[0])
    assert type(_seqs_by_text()["x"]) is int


def test_legacy_rows_without_seq_keep_segment_then_line_order():
    """Nothing on disk records cross-segment append order for rows written
    before `seq` existed, so their order must not move: no truthful reorder
    exists, and every fold over the live store must replay exactly what it
    replayed before stamps arrived."""
    _write_row("zzz-writer", "z1")
    _write_row("aaa-editor", "a1")
    _write_row("zzz-writer", "z2")

    assert _texts(events.read_all()) == ["a1", "z1", "z2"]
    assert _texts(events.read_types(["decision"])) == ["a1", "z1", "z2"]


def test_a_legacy_row_sorts_before_a_sequenced_row_in_the_same_second():
    """The sentinel is fixed, not inferred: a row with no `seq`, and no stamped
    row above it in its own segment, carries nothing that records when it was
    written, so within one second it goes first whatever its segment is called.
    Under the filename tie-break the `aaa-` segment would have won."""
    events.append_event("decision", {"text": "sequenced"}, session_id="aaa-new",
                        ts=SAME_SECOND)
    _write_row("zzz-old", "legacy")

    assert _texts(events.read_all()) == ["legacy", "sequenced"]


@pytest.mark.parametrize("bad", [True, False, "5", 1.5, -1, 2 ** 63, None,
                                 [1], {"n": 1}])
def test_a_malformed_seq_is_treated_as_absent_and_never_raises(bad):
    """Reads stay TOTAL. The malformed row is `aaa-` and the valid one `zzz-`
    with seq 0: accepting the bad value as a number (True == 1, 1.5, 2**63)
    would sort `aaa-` LAST, and a str would make the sort itself raise.

    Not every parameter discriminates HERE, and this test does not pretend
    otherwise. `False` accepted is stamp 0, which ties `zzz-valid` and loses
    to `aaa-` on segment name; a negative stamp sorts first whether accepted or
    not. So a `_valid_seq` written with `isinstance` (bool is an int subclass),
    or without its `0 <=` lower bound, passes this test on those values.
    `test_an_invalid_seq_sorts_exactly_where_a_legacy_row_would` below is the
    one that catches both."""
    _write_row("zzz-valid", "valid", seq=0)
    _write_row("aaa-bad", "malformed", seq=bad)

    assert _texts(events.read_all()) == ["malformed", "valid"]
    assert _texts(events.read_types(["decision"])) == ["malformed", "valid"]
    assert events.corrupt_rows() == []


# Every value `_valid_seq` must refuse, written between two LEGACY rows whose
# segment names bracket it: `aaa-legacy`, `mmm-bad`, `zzz-legacy`, one second.
# Refused, the bad row is unstamped like its neighbours and segment order puts it
# in the MIDDLE. Accepted, it leaves the middle in a direction that depends on the
# value -- above LEGACY_SEQ (True == 1, False == 0, 1.5, 2**63) it sorts LAST,
# below it (-2, -5, -2**63) it sorts FIRST, and a str, list or dict makes the
# sort raise. One fixture therefore kills a missing upper bound, a missing lower
# bound, an `isinstance` check and a float-tolerant one alike, which no fixture
# with the bad row at either END can do: that one is blind in one direction.
#
# -1 is the value ordering cannot see. It IS `LEGACY_SEQ`, so accepted it ties
# the legacy rows and still lands in the middle. It is not harmless for that:
# `chain._fold` folds any stamp `_row_parts` returns into the v3 hash material,
# and `pgmirror.scan_segments` carries it to the mirror. So the test also asks
# `_row_parts` directly, and that assertion is the only one -1 can fail.
INVALID_SEQS = [True, False, "5", 1.5, -1, -2, -5, -(2 ** 63), 2 ** 63,
                2 ** 64, None, [1], {"n": 1}]


def _write_sandwich(bad):
    _write_row("aaa-legacy", "before")
    _write_row("mmm-bad", "malformed", seq=bad)
    _write_row("zzz-legacy", "after")


@pytest.mark.parametrize("bad", INVALID_SEQS)
def test_an_invalid_seq_sorts_exactly_where_a_legacy_row_would(bad):
    """Kills, by name: `_valid_seq` without `0 <= value` (fails on -2, -5,
    -2**63 by ORDER and on -1 by `_row_parts`); `_valid_seq` using
    `isinstance(value, int)` (fails on True and False by order -- the case the
    test above leaves open for False); and one without the `< 2**63` bound
    (fails on 2**63 and 2**64). The order assertions come first on purpose, so
    against any of those mutants the failure a value produces says which half
    caught it. `test_the_sandwich_moves_a_stamp_it_should_have_refused` is the
    canary that the fixture really can move."""
    _write_sandwich(bad)

    assert _texts(events.read_all()) == ["before", "malformed", "after"]
    assert _texts(events.read_types(["decision"])) == ["before", "malformed",
                                                      "after"]

    [(_, line)] = events._segment_lines(paths.events_dir() / "mmm-bad.jsonl")
    ev, seq = events._row_parts(line)
    assert ev is not None and ev.payload["text"] == "malformed"
    assert seq is None
    assert events.corrupt_rows() == []


@pytest.mark.parametrize("bad, if_accepted", [
    (True, ["before", "after", "malformed"]),                  # True == 1
    (False, ["before", "after", "malformed"]),                 # False == 0
    (1.5, ["before", "after", "malformed"]),
    (2 ** 63, ["before", "after", "malformed"]),
    (2 ** 64, ["before", "after", "malformed"]),
    (-2, ["malformed", "before", "after"]),
    (-5, ["malformed", "before", "after"]),
    (-(2 ** 63), ["malformed", "before", "after"]),
    (-1, ["before", "malformed", "after"]),                    # == LEGACY_SEQ
    ("5", TypeError),
    ([1], TypeError),
    ({"n": 1}, TypeError),
])
def test_the_sandwich_moves_a_stamp_it_should_have_refused(bad, if_accepted):
    """CANARY for the test above: where `_order_key` puts the middle row if its
    stamp is taken at face value -- what every accepting mutant of `_valid_seq`
    hands it. Each row here but -1 leaves the middle or raises, so the order
    assertions above are discriminating and not a fixture that happens to agree
    with segment order. -1 staying put is pinned too: it is why the test above
    has to ask `_row_parts`, and if `LEGACY_SEQ` ever moved this row would say
    the fixture needs another look."""
    keys = {"before": events._order_key(SAME_SECOND, None, "aaa-legacy.jsonl", 0),
            "malformed": events._order_key(SAME_SECOND, bad, "mmm-bad.jsonl", 0),
            "after": events._order_key(SAME_SECOND, None, "zzz-legacy.jsonl", 0)}

    if if_accepted is TypeError:
        with pytest.raises(TypeError):
            sorted(keys, key=keys.get)
    else:
        assert sorted(keys, key=keys.get) == if_accepted


def test_the_high_water_mark_is_read_from_and_written_back_to_append_lock(
        monkeypatch):
    """The mark lives in `append.lock`, not in this process. The dashboard and a
    `hive` command append to one namespace from two processes, and
    `max(clock, mark + 1)` only survives a clock step between THEM if each reads
    the mark the other wrote. `test_seq_keeps_increasing_when_the_wall_clock_
    steps_backwards` runs in one process, so a module-level counter passes it.

    Planting a mark far above the clock, then patching the clock low, is what
    tells the two apart: only a reader of the file can produce `planted + 1`.
    The second assertion is the write half. A stamp that honoured the mark but
    never wrote its own back would leave `planted` on disk, and the next
    process would repeat the stamp.

    The SECOND step is what kills a reader that reads the file once and then
    trusts itself -- a cache per lock file, or per process. The first append
    warms any such cache; the mark is then overwritten on disk, the way another
    process's append would leave it, and only a `_next_seq` that re-reads the
    file on every call produces `rewritten + 1`.

    Layout, as `_append_lock` and `_next_seq` keep it: byte 0 is the byte
    `msvcrt.locking` covers, and the mark is 20 ASCII digits from offset 1. The
    offsets are spelled out rather than read from `_SEQ_OFFSET`/`_SEQ_WIDTH`, so
    moving the mark -- which strands the one every existing lock file holds --
    has to move this test too."""
    planted = 2 ** 60
    lock = paths.project_dir() / "append.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"\0" + b"%020d" % planted)
    monkeypatch.setattr(events, "_now_us", lambda: 1_000)

    events.append_event("decision", {"text": "x"}, session_id="s",
                        ts=SAME_SECOND)

    assert _seqs_by_text()["x"] == planted + 1
    assert lock.read_bytes()[1:21] == b"%020d" % (planted + 1)

    rewritten = 2 ** 61
    with open(lock, "r+b") as f:
        f.seek(1)
        f.write(b"%020d" % rewritten)

    events.append_event("decision", {"text": "y"}, session_id="s",
                        ts=SAME_SECOND)

    assert _seqs_by_text()["y"] == rewritten + 1
    assert lock.read_bytes()[1:21] == b"%020d" % (rewritten + 1)


def test_an_append_that_times_out_on_the_lock_never_touches_the_mark(
        monkeypatch):
    """The lock fails open when its deadline passes, and a caller that does
    not HOLD it must not read or write the high-water mark. Handed the
    descriptor anyway, such a caller reads the mark, loses the CPU while locked
    appends raise it, and writes its stale value back: the mark goes DOWN and
    the next locked append repeats a stamp. So a timed-out append stamps from
    the bare clock, exactly like one whose lock file could not be opened.

    Planted far above the clock, the mark tells the two apart. A caller given
    the descriptor stamps `planted + 1` and writes it back; one given None
    stamps the clock's 1_000 and leaves `planted` on disk. The lock is held
    here by a real second descriptor -- `test_the_append_lock_excludes_a_second_
    holder` in test_events_append_race.py is what makes that a real wedge."""
    planted = 2 ** 60
    lock = paths.project_dir() / "append.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"\0" + b"%020d" % planted)
    monkeypatch.setattr(events, "_now_us", lambda: 1_000)
    monkeypatch.setattr(events, "_LOCK_WAIT_S", 0.05)

    with events._append_lock(None) as holder:
        assert holder is not None, "the wedging holder never took the lock"
        events.append_event("decision", {"text": "unheld"}, session_id="s",
                            ts=SAME_SECOND)

    assert _seqs_by_text()["unheld"] == 1_000
    assert lock.read_bytes()[1:21] == b"%020d" % planted


# --- the stamp a row is ORDERED by ----------------------------------------------
# Line order inside one segment IS append order, so a row was written no earlier
# than anything above it. `_segment_events` therefore orders a row by its own
# stamp RAISED to the highest stamp above it in its segment. Each fixture below
# carries an inline canary: the same rows under the row's OWN stamp -- the key
# before the carry -- come back in the wrong order.

def _texts_under_each_rows_own_stamp():
    rows = []
    for seg in paths.events_dir().glob("*.jsonl"):
        for i, line in events._segment_lines(seg):
            ev, seq = events._row_parts(line)
            rows.append((events._order_key(ev.ts, seq, seg.name, i), ev))
    return _texts(ev for _, ev in sorted(rows, key=lambda t: t[0]))


def test_an_unstamped_row_below_a_stamped_one_in_its_segment_keeps_line_order():
    """A pre-`seq` process -- the dashboard nobody restarted -- appends an
    `update` to `manual.jsonl` a moment after a `hive` command stamped the `add`
    there. Sorted as `LEGACY_SEQ` the update would replay FIRST: the lost edit
    again, inside one file, which even the filename tie-break never did."""
    events.append_event("decision", {"text": "add"}, session_id="manual",
                        ts=SAME_SECOND)
    _write_row("manual", "update")

    assert _texts(events.read_all()) == ["add", "update"]
    assert _texts(events.read_types(["decision"])) == ["add", "update"]
    assert _texts_under_each_rows_own_stamp() == ["update", "add"]


def test_a_late_unstamped_row_sorts_after_what_its_own_segment_shows_it_followed():
    """Across segments too: `other` then `stamped` were appended under the lock,
    and the unstamped row sits below `stamped` in its segment, so it was written
    after both. The sentinel would put it first."""
    events.append_event("decision", {"text": "other"}, session_id="aaa-new",
                        ts=SAME_SECOND)
    events.append_event("decision", {"text": "stamped"}, session_id="zzz-mixed",
                        ts=SAME_SECOND)
    _write_row("zzz-mixed", "unstamped")

    assert _texts(events.read_all()) == ["other", "stamped", "unstamped"]
    assert _texts(events.read_types(["decision"])) == ["other", "stamped",
                                                      "unstamped"]
    assert _texts_under_each_rows_own_stamp() == ["unstamped", "other",
                                                  "stamped"]


def test_a_stamp_below_one_above_it_in_its_segment_still_keeps_line_order():
    """Only an append the mark did not rule can write a stamp below an earlier
    one: one that did not hold the lock (the bare clock), or one that found the
    mark lost or stale while the clock read no later than a stamp already
    issued. `low` is below `high` in its file, so it was
    written after it -- and after `between`, whose stamp is below `high`'s."""
    _write_row("mmm-seg", "high", seq=500)
    _write_row("mmm-seg", "low", seq=100)
    _write_row("aaa-other", "between", seq=300)

    assert _texts(events.read_all()) == ["between", "high", "low"]
    assert _texts_under_each_rows_own_stamp() == ["low", "between", "high"]


def test_the_carry_runs_down_a_segment_and_never_up_it():
    """Rows ABOVE a segment's first stamp keep sorting as legacy rows, so a
    ledger written before stamps existed orders exactly as it always did. A
    carry taken from the whole segment -- its max stamp -- would move
    `z-legacy` after `a-stamped`, which was appended later."""
    _write_row("zzz-seg", "z-legacy")
    _write_row("aaa-seg", "a-stamped", seq=50)
    _write_row("zzz-seg", "z-stamped", seq=100)

    assert _texts(events.read_all()) == ["z-legacy", "a-stamped", "z-stamped"]
    whole_segment_max = sorted(
        [("z-legacy", (SAME_SECOND, 100, "zzz-seg.jsonl", 0)),
         ("a-stamped", (SAME_SECOND, 50, "aaa-seg.jsonl", 0)),
         ("z-stamped", (SAME_SECOND, 100, "zzz-seg.jsonl", 1))],
        key=lambda t: t[1])
    assert [t for t, _ in whole_segment_max] == ["a-stamped", "z-legacy",
                                                "z-stamped"]


def test_read_types_carries_the_stamp_over_rows_it_filters_out():
    """The stamp an unstamped `decision` sorts by can sit on a row of ANOTHER
    type above it. `read_types` must carry over every row and filter after;
    filtering first would drop the `caveat` that holds the stamp and put `late`
    before `other`, which was appended before that caveat."""
    events.append_event("decision", {"text": "other"}, session_id="aaa-new",
                        ts=SAME_SECOND)
    events.append_event("caveat", {"text": "holds-the-stamp"},
                        session_id="zzz-mixed", ts=SAME_SECOND)
    _write_row("zzz-mixed", "late")

    assert _texts(events.read_types(["decision"])) == ["other", "late"]
    assert [e for e in events.read_all() if e.type == "decision"] == \
        events.read_types(["decision"])


def test_a_torn_row_between_two_rows_does_not_reset_the_carry():
    """A torn fragment -- a crashed writer's half line -- is skipped, but it
    changes nothing about when the rows around it were written: `update` is
    still below `add` in the file, so it was appended after it. A carry that
    restarted at the torn row would sort `update` as `LEGACY_SEQ` and replay it
    first -- the lost edit, brought back by one bad line."""
    events.append_event("decision", {"text": "add"}, session_id="manual",
                        ts=SAME_SECOND)
    seg = paths.events_dir() / "manual.jsonl"
    with open(seg, "a", encoding="utf-8") as f:
        f.write('{"ts": "2026-06-20T10:00:00Z", "session_id": "man\n')
    _write_row("manual", "update")

    assert [i for i, _ in events._segment_lines(seg)] == [0, 1, 2]
    assert [i for i, _, _ in events._segment_events(seg)] == [0, 2]
    assert _texts(events.read_all()) == ["add", "update"]
    assert _texts(events.read_types(["decision"])) == ["add", "update"]
    carry_restarted_at_the_torn_row = sorted(
        [("add", (SAME_SECOND, _seqs_by_text_skipping_torn()["add"],
                  "manual.jsonl", 0)),
         ("update", (SAME_SECOND, events.LEGACY_SEQ, "manual.jsonl", 2))],
        key=lambda t: t[1])
    assert [t for t, _ in carry_restarted_at_the_torn_row] == ["update", "add"]


def _seqs_by_text_skipping_torn():
    out = {}
    for seg in paths.events_dir().glob("*.jsonl"):
        for _, line in events._segment_lines(seg):
            ev, seq = events._row_parts(line)
            if ev is not None:
                out[ev.payload["text"]] = seq
    return out


def test_segments_are_per_session():
    events.append_event("caveat", {"text": "x"}, session_id="abc",
                        ts="2026-06-20T10:00:00Z")
    assert (paths.events_dir() / "abc.jsonl").exists()


def test_assistant_response_is_valid_and_in_memory_view():
    ev = events.append_event("assistant_response", {"text": "ok"}, session_id="s",
                             ts="2026-06-23T10:00:00Z")
    assert ev.type == "assistant_response"
    assert "assistant_response" in events.MEMORY_VIEW_TYPES
    assert "assistant_response" not in events.SEMANTIC_TYPES


def test_tool_failed_is_a_valid_deterministic_type():
    assert "tool_failed" in events.VALID_TYPES
    assert "tool_failed" in events.DETERMINISTIC_TYPES
    # deterministic, not semantic: must stay out of the LEDGER/graph intent layer
    assert "tool_failed" not in events.SEMANTIC_TYPES
    ev = events.append_event("tool_failed", {"text": "boom", "signature": "pymodule:x"},
                             session_id="s1", tags=["auto", "failure"], project="test")
    assert ev.type == "tool_failed"


def test_rule_revised_is_a_valid_deterministic_type():
    assert "rule_revised" in events.DETERMINISTIC_TYPES
    assert "rule_revised" not in events.SEMANTIC_TYPES
    ev = events.append_event("rule_revised",
                             {"signature": "pymodule:x", "text": "do Y", "source": "human"},
                             session_id="s1", tags=["reconcile"], project="test")
    assert ev.type == "rule_revised"


def test_skill_used_is_a_valid_deterministic_type():
    # U5: without this registration append_event raises and every Skill-tool
    # capture would be silently dropped to hooks_errors.log
    assert "skill_used" in events.DETERMINISTIC_TYPES
    assert "skill_used" not in events.SEMANTIC_TYPES
    ev = events.append_event("skill_used",
                             {"name": "ce-plan", "plugin": "compound-engineering",
                              "ok": True, "text": "done"},
                             session_id="s1", tags=["auto"], project="test")
    assert ev.type == "skill_used"


def test_lifecycle_event_types_are_valid():
    for t in ("rule_activated", "rule_promoted", "rule_demoted"):
        assert t in events.DETERMINISTIC_TYPES
        assert t not in events.SEMANTIC_TYPES
    ev = events.append_event("rule_promoted", {"signature": "pymodule:x"},
                             session_id="s", tags=["promote"], project="test")
    assert ev.type == "rule_promoted"


def test_loop_run_dismissed_is_valid_deterministic_type():
    assert "loop_run_dismissed" in events.DETERMINISTIC_TYPES
    assert "loop_run_dismissed" not in events.SEMANTIC_TYPES
    ev = events.append_event("loop_run_dismissed",
                             {"run_id": "abc123", "cause": "manual"},
                             session_id="s", project="test")
    assert ev.type == "loop_run_dismissed"


def test_learning_is_a_valid_semantic_type():
    # regression guard: research._record_learning appends type "learning";
    # before it was registered, append_event rejected it and the controller's
    # crash guard silently dropped every PRD from the ledger
    assert "learning" in events.SEMANTIC_TYPES
    assert "learning" in events.MEMORY_VIEW_TYPES
    ev = events.append_event("learning", {"text": "Self-Reliant learning 1",
                                          "report": "PRD body"},
                             session_id="research-ab12cd34",
                             tags=["self-reliant", "learning"], project="test")
    assert ev.type == "learning"


def test_origin_of_marks_self_reliant_output():
    # tag mark (append-time) and session mark (research-<run_id> stream) both
    # resolve to the one provenance label; everything else stays unlabeled
    assert events.origin_of("research-ab12cd34") == "self-reliant"
    assert events.origin_of("sess-1", ["self-reliant"]) == "self-reliant"
    assert events.origin_of("sess-1", ["swarm"]) is None
    assert events.origin_of("sess-1") is None
    assert events.origin_of(None) is None
    assert events.origin_of("researchy-session") is None   # prefix, not substring


def test_concurrent_appends_never_lose_or_tear_a_row(tmp_path):
    """The ledger is the one file in this system that may never lose a write.

    Measured before the lock landed, six processes x sixty appends into one
    segment: 72 of 360 rows simply GONE, 5 runs out of 5, worst case 606 of 2400
    at twelve-way. The loss is a Windows story -- `open(seg, "a")` there is
    seek-to-end THEN write, two syscalls, so two processes seek to the same
    offset and the second overwrites the first -- but the same race also tears
    rows, leaving exactly the `:"command_run"}` fragment that this file's
    total-read test above was written to TOLERATE. Reads were made total years
    before the write was made atomic; this test is the other half.

    Processes, not threads, and that is not incidental: threads share one
    interpreter and would serialise on the GIL for a write this small, so a
    threaded version of this test passes against the broken code and proves
    nothing.

    `os.write` on an O_APPEND descriptor was measured too and does NOT fix it
    (328 lost vs 322 for the plain write) -- Windows has no atomic-append
    syscall to reach for, so the serialisation has to be explicit.
    """
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    nproc, nrows = 6, 60
    src = str(Path(events.__file__).resolve().parents[1])
    child = tmp_path / "appender.py"
    child.write_text(
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from mindhive import events\n"
        "tag, n = sys.argv[1], int(sys.argv[2])\n"
        "for i in range(n):\n"
        "    events.append_event('command_run',\n"
        "                        {'desc': '%%s-%%d' %% (tag, i),\n"
        "                         'text': 'x' * 120},\n"
        "                        session_id='race', tags=['auto'],\n"
        "                        project='test')\n" % src,
        encoding="utf-8")

    env = dict(os.environ, MINDHIVE_HOME=str(tmp_path), MINDHIVE_PROJECT="test",
               MINDHIVE_AUTORESUME_TIMER="off")
    procs = [subprocess.Popen([sys.executable, str(child), "p%d" % i, str(nrows)],
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
             for i in range(nproc)]
    for p in procs:
        _out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode("utf-8", "replace")[-800:]

    seg = paths.events_dir("test") / "race.jsonl"
    survived = set()
    for line in seg.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            survived.add(json.loads(line)["payload"]["desc"])
        except Exception:                                       # noqa: BLE001
            pass

    expected = {"p%d-%d" % (p, i) for p in range(nproc) for i in range(nrows)}
    missing = expected - survived
    assert not missing, (
        "%d of %d appended rows never reached the ledger — the append raced"
        % (len(missing), len(expected)))
    assert events.corrupt_rows("test") == [], "concurrent appends tore a row"


# --- where a ledger row ends --------------------------------------------------
#
# The ledger is NEWLINE-delimited JSON, but every reader here used
# `str.splitlines()` until 2026-09-10. That also breaks on U+0085, U+2028,
# U+2029 and five C0 controls, and `canonical_json` writes with
# `ensure_ascii=False`, so three of them reach a segment RAW through the
# ordinary writer. All three are legal inside a JSON string -- the row on disk
# was intact and only the reader destroyed it: the event vanished from
# `read_all` (and so from every projection, the briefing and the mirror),
# `corrupt_rows` invented two torn rows for it, and every later row's line index
# shifted by one, including `seg_line`, which Postgres stores as part of a
# UNIQUE natural key.
#
# Most of the tests below are written to FAIL on `splitlines()` and pass on the
# shared `_segment_lines` splitter. Verified that way rather than asserted:
# mutating the one `.split("\n")` in `events._segment_parts` -- the single
# place that decides where a ledger row ends -- back to `.splitlines()` fails
# 30 cases: 13 of the 18 here, 2 in test_pgmirror.py and 15 in test_chain.py.
# The chain count is that high because the split is now SHARED, so the seal
# tests defend the readers' rule too; before the shared seam they would all
# have stayed green while these went red, which is the drift
# tests/test_architecture_drift.py now forbids outright. The other 5 here pass
# either way, deliberately:
#
#   * the fixture canary (3 cases) and the canonical_json survey guard the OTHER
#     way this section could go quiet. If a writer ever started escaping these
#     characters, every test above would become vacuous while staying green;
#     these two go red instead.
#   * the blank-line test pins a property, not the defect. Both splitters agree
#     about a blank line -- that is the point. It is the one guarantee
#     `_segment_lines` makes that no exotic character is needed to state.

RAW_SEPARATORS = ["\u2028", "\u2029", "\u0085"]      # and ONLY these three -- see the
ESCAPED_BY_JSON = ["\u000b", "\u000c", "\u001c", "\u001d", "\u001e"]   # test two below


@pytest.mark.parametrize("sep", RAW_SEPARATORS, ids=["LS", "PS", "NEL"])
def test_the_separator_fixture_is_written_raw_and_fools_splitlines(sep):
    """CANARY for every test below it. They are only discriminating while the
    writer actually puts the character on disk unescaped; flip `ensure_ascii` and
    they all quietly become three-event tests that pass against the defect."""
    events.append_event("decision", {"text": "mid" + sep + "dle"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    text = (paths.events_dir("test") / "s.jsonl").read_text(encoding="utf-8")
    assert sep in text                          # raw, not the six-char escape
    assert len(text.splitlines()) > text.count("\n")


def test_only_three_separators_survive_canonical_json():
    """The other five are below U+0020, which JSON forbids unescaped, so
    `json.dumps` escapes them whatever `ensure_ascii` says -- they can never
    arrive as separators. Pinning that keeps the docstrings honest and stops the
    fixture list above growing on a guess."""
    for ch in RAW_SEPARATORS:
        assert ch in events.canonical_json({"t": ch}), repr(ch)
    for ch in ESCAPED_BY_JSON:
        assert ch not in events.canonical_json({"t": ch}), repr(ch)


@pytest.mark.parametrize("sep", RAW_SEPARATORS, ids=["LS", "PS", "NEL"])
def test_read_all_returns_a_separator_bearing_row_as_one_event(sep):
    """The defect in its plainest form: three events written, two read back."""
    for i, text in enumerate(["before", "mid" + sep + "dle", "after"]):
        events.append_event("decision", {"text": text}, session_id="s",
                            ts="2026-06-20T10:00:0%d" % i + "Z")
    assert [e.payload["text"] for e in events.read_all("test")] == [
        "before", "mid" + sep + "dle", "after"]


@pytest.mark.parametrize("sep", RAW_SEPARATORS, ids=["LS", "PS", "NEL"])
def test_read_types_sees_the_same_rows_read_all_does(sep):
    """The `== read_all()` half alone is NOT discriminating -- both readers broke
    identically, which is why the existing typed-read parity test could not catch
    this. The count assertion is what makes it one."""
    for i, text in enumerate(["before", "mid" + sep + "dle", "after"]):
        events.append_event("decision", {"text": text}, session_id="s",
                            ts="2026-06-20T10:00:0%d" % i + "Z")
    typed = events.read_types(["decision"], "test")
    assert typed == events.read_all("test")
    assert len(typed) == 3


@pytest.mark.parametrize("sep", RAW_SEPARATORS, ids=["LS", "PS", "NEL"])
def test_a_separator_bearing_row_is_not_reported_as_two_torn_rows(sep):
    """`corrupt_rows`' count is a documented regression detector -- if it moves,
    an unserialised writer exists again. A reader that tears intact rows makes it
    move for a reason that has nothing to do with writers."""
    events.append_event("decision", {"text": "mid" + sep + "dle"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    assert events.corrupt_rows("test") == []


def test_corrupt_rows_line_is_the_physical_newline_index():
    """The reported `line` must be the newline index a human can find with
    `sed -n <n+1>p`, and the same number `pgmirror.scan_segments` stores as
    `seg_line`. With `splitlines()` the intact row above counted as two, so the
    genuinely broken row on newline-line 1 was reported as line 2."""
    events.append_event("decision", {"text": "mid\u2028dle"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")                  # line 0
    with open(paths.events_dir("test") / "s.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n")                                        # line 1
    assert [(r["segment"], r["line"]) for r in events.corrupt_rows("test")] == [
        ("s.jsonl", 1)]


def test_a_blank_line_still_advances_the_line_index():
    """The property `_segment_lines` exists to keep, tested without any exotic
    character: a skipped line is skipped from the OUTPUT, never from the count.
    `corrupt_rows` reports this number to a human and `pgmirror.scan_segments`
    stores it as `seg_line`, so numbering only surviving rows would renumber the
    mirror the first time a segment gained a blank line -- a parity failure with
    no obvious cause. Every other test here happens to use blank-free fixtures."""
    events.append_event("decision", {"text": "first"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")                  # line 0
    seg = paths.events_dir("test") / "s.jsonl"
    with open(seg, "a", encoding="utf-8") as f:
        f.write("\n")                                             # line 1: blank
        f.write("   \n")                                          # line 2: whitespace
        f.write("{not json\n")                                    # line 3
    assert [(r["segment"], r["line"]) for r in events.corrupt_rows("test")] == [
        ("s.jsonl", 3)]
    assert [i for i, _ in events._segment_lines(seg)] == [0, 3]


@pytest.mark.parametrize("sep", RAW_SEPARATORS, ids=["LS", "PS", "NEL"])
def test_a_malformed_row_that_also_carries_a_separator_is_reported_once(sep):
    """The case the "corrupt_rows' count is a regression detector" claim rests
    on. A genuinely broken row must be reported ONCE however exotic its bytes:
    under `splitlines()` one bad row carrying a separator became two entries, so
    the count moved for a reason that had nothing to do with a writer."""
    d = paths.events_dir("test")
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "s.jsonl", "w", encoding="utf-8") as f:
        f.write('{"broken": "mid' + sep + 'dle"}\n')
    bad = events.corrupt_rows("test")
    assert [(r["segment"], r["line"]) for r in bad] == [("s.jsonl", 0)]
    assert "missing required field" in bad[0]["reason"]
