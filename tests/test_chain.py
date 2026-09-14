# tests/test_chain.py
"""The hash-chain checkpoint contract.

**The chain folds in APPEND order, not timestamp order.** That is the whole
point of this file, and it is a correction: the chain used to fold
`events.read_all`'s `(ts, segment, line)` order, which is total and
deterministic but NOT append-only. Ledger timestamps are assigned by the
CALLER, not by the writer -- `events.append_event` takes `ts` as a parameter --
so a row can legitimately arrive stamped in the past:

* `incidents.audit` backdates `rule_expired` / `rule_activated` deliberately,
  to the failure's own ts, so the operation stays deterministic.
* `hookcapture` stamps `ts=now` before slow work and before it contends for the
  append lock, then writes.

Measured on the live store: 8,547 rows whose ts runs backwards against their
physical position. Under a ts-ordered fold every one of those inserts INSIDE
the sealed prefix and turns `hive verify` red until the next projection
re-seals it. Observed live in one session: `ok: true`, then
`{"ok": false, "reason": "hash_mismatch"}` twenty minutes later, with no
tampering in between. An alarm that flickers is worse than one that is always
red -- it trains you to ignore it, and a real tamper hides in the noise.

Append order fixes it at the root. The checkpoint records a per-segment count
of COMPLETE (newline-terminated) lines, and verification re-folds exactly those
lines. Appending to a segment cannot change that segment's earlier lines, and a
brand-new segment is simply not in the map -- so no legitimate write can ever
disturb the sealed prefix, whatever ts it carries.
"""
import json

import pytest

from mindhive import chain, events, paths


# --- fixture builders ---------------------------------------------------------

def _segments():
    return sorted((paths.project_dir() / "events").glob("*.jsonl"))


def _seg(session_id="s"):
    return paths.project_dir() / "events" / f"{session_id}.jsonl"


def _add(text, session_id="s", ts="2026-06-20T10:00:00Z", etype="decision"):
    return events.append_event(etype, {"text": text}, session_id=session_id, ts=ts)


def _put_checkpoint(obj):
    """Overwrite chain.json with a hand-built checkpoint. Only the tests below
    that are ABOUT the checkpoint's format use this; everything else goes
    through `write_chain`."""
    (paths.project_dir() / "chain.json").write_text(
        events.canonical_json(obj), encoding="utf-8")


_EVENT_KEYS = ("ts", "session_id", "type", "payload", "tags")


def _pre_seq_order():
    """Every row in the order EVERY v1 writer folded: pre-`seq` `read_all`'s
    ``(ts, segment_name, line_index)``, blind to the append stamp.

    Built from the raw segment bytes, not through `events` or `chain`, because
    whether the v1 shim reproduces this order is exactly what the tests below
    ask. An oracle derived from the shim's own helpers would agree with the shim
    by construction, and a test of that shape passes against any order."""
    rows = []
    for seg in _segments():
        for i, line in enumerate(seg.read_text(encoding="utf-8").split("\n")):
            if not line.strip():
                continue
            obj = json.loads(line)
            ev = events.Event(**{k: obj[k] for k in _EVENT_KEYS})
            rows.append(((obj["ts"], seg.name, i), ev))
    rows.sort(key=lambda t: t[0])
    return [ev for _, ev in rows]


def _mint_v1():
    """chain.json exactly as the pre-append-order `write_chain` wrote it:
    `compute_chain(sealable(read_all()))` over that code's read order, stored as
    a bare ``{count, head}``. That code is still what the main checkout runs, so
    it keeps minting this over rows a newer writer has already stamped."""
    v1 = chain.compute_chain(chain.sealable(_pre_seq_order()))
    _put_checkpoint({"count": v1["count"], "head": v1["head"]})
    return v1


def _v2_head():
    """The head a v2 writer -- one that knows no stamp -- seals over the whole
    ledger, for fixtures whose rows sit in one segment: there `read_all`'s order
    IS append order, and `compute_chain` folds the five Event fields, which is
    v2's material."""
    return chain.compute_chain(events.read_all())["head"]


# --- the core contract --------------------------------------------------------

def test_verify_ok_after_write():
    _add("a")
    chain.write_chain()
    assert chain.verify()["ok"] is True


def test_verify_detects_tamper():
    _add("alpha")
    chain.write_chain()
    seg = _segments()[0]
    # Replace a payload value only -- the row stays valid JSON, so this is a
    # rewrite of sealed history rather than a corruption that read_all skips.
    seg.write_text(seg.read_text(encoding="utf-8").replace('"alpha"', '"beta"'),
                   encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"


def test_appended_garbage_keeps_verify_ok_but_shows_in_corrupt_rows():
    # Reads are TOTAL. An appended unparseable line is not a logical event, so
    # it neither crashes verify nor changes the sealed chain -- a torn write
    # must never take the hive down. corrupt_rows() surfaces it loudly instead.
    _add("a")
    chain.write_chain()
    with open(_seg(), "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert chain.verify()["ok"] is True
    bad = events.corrupt_rows("test")
    assert len(bad) == 1
    assert "unparseable json" in bad[0]["reason"]


@pytest.mark.parametrize("ver", [2, 3])
def test_verify_detects_delete_by_corruption(ver):
    # Deleting a sealed event by corrupting its line is caught: the row stops
    # parsing, so it leaves the fold and the recomputed head moves. It also
    # leaves the sealed prefix, so the stored `count` stops matching -- and the
    # verdict must still be the `hash_mismatch` it is, under BOTH segment-map
    # formats, because `verify` compares `count` only after the head. The v2
    # file is the v3 one with a stamp-blind head; the first verdict is the
    # canary that the hand-built head really verifies.
    _add("one")
    _add("two", ts="2026-06-20T11:00:00Z")
    stored = chain.write_chain()
    if ver == 2:
        _put_checkpoint({**stored, "version": 2, "head": _v2_head()})
    before = chain.verify()
    assert before["ok"] is True and before["format"] == ver, before
    seg = _segments()[0]
    seg.write_text(
        seg.read_text(encoding="utf-8").replace('"text":"one"', "GARBAGE"),
        encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"
    assert result["format"] == ver


@pytest.mark.parametrize("body", ['{not json', '[]', '"a string"', '{"count": 1}'])
def test_verify_corrupt_chain_file(body):
    """Every shape `_read_checkpoint` rejects: unparseable, not an object, and
    an object with no head. verify must return a VERDICT, not raise -- the
    dashboard overview poll calls it on every refresh, so a raise here is an
    outage rather than a test failure."""
    _add("a")
    chain.write_chain()
    (paths.project_dir() / "chain.json").write_text(body, encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "corrupt_chain_file"
    assert result["actual"]["count"] == 1     # the ledger is still described


def test_verify_without_chain_file():
    _add("a")
    result = chain.verify()
    assert result["ok"] is False
    # The reason is the whole value of this branch: an unanchored store is not a
    # tamper, and telling the user so -- with the command that fixes it -- is
    # what keeps `hash_mismatch` meaning something.
    assert "chain.json" in result["reason"]
    assert "hive project" in result["reason"]
    assert result["actual"]["count"] == 1     # it still reports what it found


# --- append order: the defect this file exists to pin -------------------------

def test_a_backdated_append_does_not_invalidate_the_checkpoint():
    """THE fix. An event stamped in the past, appended after the checkpoint, is
    a legitimate write (incidents.audit does exactly this) and must read as
    `ahead`, never as a tamper."""
    _add("one", ts="2026-06-20T10:00:00Z")
    _add("two", ts="2026-06-20T10:00:01Z")
    chain.write_chain()

    _add("backdated", ts="2026-06-20T09:00:00Z")   # an hour before everything

    result = chain.verify()
    assert result["ok"] is True
    assert result["ahead"] == 1


def test_the_backdated_fixture_really_does_break_a_ts_ordered_fold():
    """CANARY for the test above. A regression test whose fixture does not
    reproduce the defect proves nothing while reading exactly like one that
    does. This proves the same store DOES move a ts-ordered prefix -- so the
    test above passes because the fold order changed, not because the fixture
    is toothless."""
    _add("one", ts="2026-06-20T10:00:00Z")
    _add("two", ts="2026-06-20T10:00:01Z")
    sealed_before = chain.compute_chain(chain.sealable(events.read_all()))

    _add("backdated", ts="2026-06-20T09:00:00Z")

    after = chain.compute_chain(events.read_all(), checkpoint=sealed_before["count"])
    assert sealed_before["count"] > 0, "fixture sealed nothing -- it proves nothing"
    assert after["checkpoint_head"] != sealed_before["head"]


def test_a_new_segment_sorting_before_the_sealed_ones_does_not_invalidate():
    """Segment names are session UUIDs, so a new one sorts anywhere. Under a
    global positional prefix a low-sorting newcomer inserts into sealed
    history; under per-segment limits it is simply absent from the map.

    This is also the original tie-boundary failure -- same second, lower-sorting
    segment name -- which used to have its own test with a byte-identical
    fixture. `sealable()` passed it by refusing to seal the newest second; this
    passes it because an append cannot reach a sealed segment prefix at all."""
    _add("one", session_id="m-sealed", ts="2026-06-20T10:00:00Z")
    _add("two", session_id="m-sealed", ts="2026-06-20T10:00:01Z")
    chain.write_chain()

    _add("newcomer", session_id="a-later", ts="2026-06-20T10:00:01Z")

    result = chain.verify()
    assert result["ok"] is True
    assert result["ahead"] == 1


def test_the_whole_complete_log_is_sealed():
    """The narrowed guarantee is gone. `sealable()` used to hold back the newest
    second because a later arrival could sort into it; nothing can now, so the
    checkpoint attests to every complete line."""
    for i in range(3):
        _add(str(i), ts="2026-06-20T10:00:00Z")     # all in ONE second
    stored = chain.write_chain()
    assert stored["count"] == 3
    result = chain.verify()
    assert result["ok"] is True
    assert "ahead" not in result


def test_verify_no_ahead_key_when_checkpoint_current():
    _add("a")
    chain.write_chain()
    result = chain.verify()
    assert result["ok"] is True
    assert "ahead" not in result


def test_verify_ok_when_events_appended_after_checkpoint():
    """The steady state: chain.json is a checkpoint refreshed at projection
    time, and events keep arriving after it."""
    _add("a", ts="2026-06-20T10:00:00Z")
    chain.write_chain()                       # seals 1 event
    _add("b", ts="2026-06-20T10:01:00Z")
    _add("c", ts="2026-06-20T10:02:00Z", etype="caveat")
    result = chain.verify()
    assert result["ok"] is True
    assert result["ahead"] == 2


def test_an_empty_ledger_seals_nothing_and_verifies():
    stored = chain.write_chain()
    assert stored["count"] == 0
    assert chain.verify()["ok"] is True


# --- what the checkpoint records ----------------------------------------------

def test_the_checkpoint_records_complete_line_counts_per_segment():
    _add("one", session_id="alpha")
    _add("two", session_id="alpha")
    _add("three", session_id="beta")
    stored = chain.write_chain()
    assert stored["segments"] == {"alpha.jsonl": 2, "beta.jsonl": 1}
    assert stored["count"] == 3


def test_a_tamper_anywhere_in_the_sealed_prefix_is_caught():
    """Contract change, stated plainly: the old checkpoint refused to claim the
    newest second, so a tamper there went unreported. Every complete line is
    now claimed."""
    _add("sealed", ts="2026-06-20T10:00:00Z")
    _add("newest", ts="2026-06-20T10:00:01Z")
    chain.write_chain()
    seg = _segments()[0]
    seg.write_text(seg.read_text(encoding="utf-8").replace('"newest"', '"edited"'),
                   encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"


def test_verify_detects_tamper_before_checkpoint_with_later_appends():
    """A rewrite of checkpointed history is caught even when legitimate events
    were appended afterward."""
    _add("alpha", ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    _add("later", ts="2026-06-20T10:05:00Z")
    seg = _segments()[0]
    seg.write_text(seg.read_text(encoding="utf-8").replace('"alpha"', '"beta"'),
                   encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"


def test_verify_detects_truncation_below_checkpoint():
    """A segment shorter than its stored line count means sealed history was
    deleted -- a genuine tamper, not benign drift."""
    _add("a", ts="2026-06-20T10:00:00Z")
    _add("b", ts="2026-06-20T10:01:00Z")
    chain.write_chain()
    seg = _segments()[0]
    first = seg.read_text(encoding="utf-8").splitlines()[0]
    seg.write_text(first + "\n", encoding="utf-8")      # drop the 2nd event
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"


def test_a_segment_that_disappears_entirely_is_caught():
    """Truncation's other shape: the whole file is gone. A per-segment map makes
    this detectable where a global count could be satisfied by any other
    segment's growth."""
    _add("keep", session_id="alpha", ts="2026-06-20T10:00:00Z")
    _add("gone", session_id="beta", ts="2026-06-20T10:01:00Z")
    chain.write_chain()
    _seg("beta").unlink()
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"


# --- the incomplete trailing line ---------------------------------------------

def test_a_trailing_line_without_a_newline_is_not_sealed():
    """A torn write can leave a segment ending mid-row. Those bytes are still
    mutable -- the next append concatenates onto them -- so they must stay
    outside the checkpoint or the seal would attest to something that changes."""
    _add("complete")
    with open(_seg(), "a", encoding="utf-8") as f:
        f.write('{"partial":')                       # no newline
    stored = chain.write_chain()
    assert stored["segments"] == {"s.jsonl": 1}
    assert stored["count"] == 1
    assert chain.verify()["ok"] is True


def test_completing_a_partial_trailing_line_later_does_not_invalidate():
    _add("complete")
    with open(_seg(), "a", encoding="utf-8") as f:
        f.write('{"half":')
    chain.write_chain()
    with open(_seg(), "a", encoding="utf-8") as f:    # the next append lands on it
        f.write('1}\n')
    assert chain.verify()["ok"] is True


# --- where a line ends --------------------------------------------------------

def test_a_payload_carrying_u2028_does_not_shrink_the_sealed_prefix():
    """`canonical_json` writes with ensure_ascii=False, so a U+2028 typed into a
    memo reaches the segment RAW -- and `str.splitlines()` treats it as a line
    break where the file format does not. Splitting the scan on it dropped
    events out of the seal while the recorded line count still said three:
    measured on this exact fixture, the checkpoint sealed ONE event and recorded
    a count of three. Two readers disagreeing about where a row ends is how a
    hash chain stops meaning anything, so the scan splits on `\\n` and nothing
    else. splitlines() also breaks on U+2029, U+000B, U+000C, U+001C-U+001E and
    U+0085 -- one case is enough to pin the rule."""
    _add("before")
    _add("mid\u2028dle")
    _add("after")
    stored = chain.write_chain()
    assert stored["count"] == 3
    assert stored["segments"] == {"s.jsonl": 3}
    assert chain.verify()["ok"] is True


def test_the_u2028_fixture_really_does_fool_splitlines():
    """Canary for the test above. If the writer ever starts escaping U+2028, the
    fixture stops carrying the hazard and silently becomes a plain three-event
    test that would pass against the defect."""
    _add("mid\u2028dle")
    text = _seg().read_text(encoding="utf-8")
    assert "\u2028" in text                       # written raw, not as \u2028
    assert len(text.splitlines()) > text.count("\n")


def test_the_prefix_is_cut_by_physical_line_not_by_row_ordinal():
    """`_prefix` filters on the line index a row was READ from, not on its
    position among the parseable rows. An unreadable line makes those two
    numbers differ, and using the ordinal would pull a row appended AFTER the
    checkpoint into the sealed prefix -- the seal would then attest to bytes
    that were not on disk when it was written."""
    seg = _seg()
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_text("torn\n", encoding="utf-8")     # physical line 0, unreadable
    _add("sealed")                                # physical line 1, ordinal 0
    stored = chain.write_chain()
    assert stored["segments"] == {"s.jsonl": 2}
    assert stored["count"] == 1                   # one parseable row

    _add("after the seal")                        # physical line 2, ordinal 1
    result = chain.verify()
    assert result["ok"] is True
    assert result["ahead"] == 1


def test_a_truncated_segment_of_unparseable_rows_is_still_caught():
    """Discriminates the per-segment line count from the prefix fold. Every line
    in this segment is unreadable, so `_row_to_event` drops all of them and the
    fold is byte-identical before and after the deletion -- only the recorded
    count notices. Delete `_shortfall` and this returns ok=True with sealed
    history missing from disk; every other truncation test still passes, because
    they all have parseable rows for the fold to catch."""
    _add("real", session_id="alpha")
    seg = _seg("noise")
    seg.write_text("not json\nalso not json\n", encoding="utf-8")
    stored = chain.write_chain()
    assert stored["segments"]["noise.jsonl"] == 2
    assert stored["count"] == 1                   # nothing from noise is folded

    seg.write_text("not json\n", encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"
    assert result["truncated"] == ["noise.jsonl"]


@pytest.mark.parametrize("bogus", [-1, True, "3", 1.0, None])
def test_a_segment_limit_that_is_not_a_line_count_is_refused(bogus):
    """`have < want` is just a comparison, and against these it quietly
    succeeds or explodes: -1 is below any real length and `True == 1`, so both
    passed a segment off as intact; `"3"` raised TypeError out of verify, which
    for a function contracted never to raise is worse than either. A limit that
    is not a non-negative int is not a limit."""
    _add("a")
    stored = chain.write_chain()
    _put_checkpoint({**stored, "segments": {"s.jsonl": bogus}})
    result = chain.verify()
    assert result["ok"] is False
    assert result["truncated"] == ["s.jsonl"]


# --- the checkpoint's own format ----------------------------------------------

@pytest.mark.parametrize("ver", [2, 3])
@pytest.mark.parametrize("broken", [[], "x", 5, None])
def test_a_v2_checkpoint_with_a_wrecked_segments_map_is_refused(broken, ver):
    """Dispatch keys on `version`, never on the shape of `segments`. Under
    shape-dispatch all four of these fell through to the v1 shim -- which does
    not read `segments` at all -- and returned ok=True/format=1: a silent
    downgrade to the weaker ts-ordered check, available to anyone who could
    replace one key in the file the check depends on. Both segment-map formats
    carry the same contract."""
    _add("a")
    stored = chain.write_chain()
    _put_checkpoint({**stored, "version": ver, "segments": broken})
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "corrupt_chain_file"
    assert result.get("format") == ver


def test_a_v2_checkpoint_with_its_version_deleted_is_refused():
    """The downgrade from the other side: strip `version` and a v2 file has the
    shape of a legacy one. It does not, quite -- a genuine v1 checkpoint is
    exactly `{count, head}`, which is what the old `write_chain` emitted and
    what all 67 on disk contain -- so a `segments` key with no version is a
    wrecked v2 file, not an old one."""
    _add("a")
    stored = chain.write_chain()
    _put_checkpoint({k: v for k, v in stored.items() if k != "version"})
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "corrupt_chain_file"


@pytest.mark.parametrize("ver", [4, "3", "2", 1.5, 3.0, True])
def test_a_checkpoint_from_an_unknown_format_is_unverifiable_not_green(ver):
    """A version this build does not know is not evidence the ledger is wrong,
    so it is not `corrupt_chain_file`; it is also not a licence to fall back to
    an older check, so it is not ok either. `True` is in here because `True == 1`
    in Python and would otherwise route to the v1 shim -- the same downgrade,
    spelled differently."""
    _add("a")
    stored = chain.write_chain()
    _put_checkpoint({**stored, "version": ver})
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "unsupported_checkpoint_version"


def test_a_genuine_v1_checkpoint_still_reaches_the_shim():
    """The other half of the dispatch: tightening it must not strand the 67
    real v1 files, which carry no `version` key at all."""
    _add("a")
    _put_checkpoint({"count": 0, "head": "0" * 64})
    assert chain.verify()["format"] == 1


_ABSENT = object()


@pytest.mark.parametrize("ver", [2, 3])
@pytest.mark.parametrize("bogus", ["x", None, 1.5, 1.0, True, -1, _ABSENT, 0, 999],
                         ids=["str", "null", "float", "integral-float", "bool",
                              "negative", "missing", "zero", "too-many"])
def test_a_checkpoint_count_that_is_not_a_row_count_is_refused(bogus, ver):
    """`count` is the one checkpoint field nothing validated. `verify` subtracts
    it from the scanned row count, so `"x"` and `null` raised TypeError out of a
    function contracted never to raise -- the dashboard overview poll and
    `hive verify` turned that into a traceback -- while `1.5` returned `ok: true`
    with `ahead: -0.5`, `True` passed as the count 1, and `-1` passed with a
    phantom `ahead`. Every segment-map writer emits a non-negative int, so a
    file carrying anything else (or nothing) was not written by `write_chain`,
    which is what `corrupt_chain_file` says.

    `0` and `999` ARE non-negative ints, and a type check alone let them
    through: measured over a head sealing three rows, `0` returned `ok: true,
    ahead: 3` and `999` returned `ok: true, ahead: -996`. `write_chain` takes
    `count` and `segments` from one scan, so once the head matches the sealed
    prefix holds exactly `count` rows; here it holds one. Both values are
    needed: `0` kills a check that refuses only a count above the prefix, `999`
    one that refuses only a count below it.

    `1.0` is the case only the dispatch type check can refuse. The fixture seals
    one row and `1.0 == 1`, so the equality check after the head passes it --
    measured with the dispatch check widened to admit floats, it verified `ok:
    true` -- while `1.5` that later check refuses on its own. `True` is the same
    shape for `bool`.

    The head is valid for the declared format in both parametrisations, so the
    COUNT is the only thing wrong with the file: a v2 head folded without
    stamps, a v3 head as `write_chain` sealed it."""
    _add("a")
    stored = chain.write_chain()
    checkpoint = {**stored, "version": ver,
                  "head": stored["head"] if ver == 3 else _v2_head()}
    if bogus is _ABSENT:
        del checkpoint["count"]
    else:
        checkpoint["count"] = bogus
    _put_checkpoint(checkpoint)

    result = chain.verify()
    assert result["ok"] is False, result
    assert result["reason"] == "corrupt_chain_file"
    assert result["format"] == ver


@pytest.mark.parametrize("ver", [2, 3])
def test_a_count_naming_the_scanned_rows_rather_than_the_sealed_ones_is_refused(ver):
    """`count` is compared with the SEALED prefix, not with the rows on disk.
    Every case above seals the whole ledger, where the two numbers are equal, so
    a check admitting anything between them -- ``len(sealed) <= count <=
    scanned`` -- passes all of those. Here one row is appended after the seal
    and `count` is set to the scanned total: measured under that range check,
    the file verified `ok: true` with no `ahead`, hiding the appended row.

    The first verdict is the canary. The honest count must verify with `ahead:
    1`, so the refusal is about the count, not about the appended row or the
    hand-built v2 head -- which is taken BEFORE the append, because `_v2_head`
    folds the whole ledger."""
    _add("a")
    stored = chain.write_chain()
    checkpoint = {**stored, "version": ver,
                  "head": stored["head"] if ver == 3 else _v2_head()}
    _add("b")
    _put_checkpoint(checkpoint)
    honest = chain.verify()
    assert honest["ok"] is True and honest["format"] == ver, honest
    assert honest["ahead"] == 1

    _put_checkpoint({**checkpoint, "count": honest["actual"]["count"]})
    result = chain.verify()
    assert result["ok"] is False, result
    assert result["reason"] == "corrupt_chain_file"
    assert result["format"] == ver
    assert "ahead" not in result


def test_an_exact_int_count_still_verifies_zero_included():
    """CANARY for the refusal above: the untouched checkpoint's int count must
    still verify, or the refusal is proving only that `verify` refuses
    everything. Zero is here on purpose -- an empty ledger seals `count: 0`, and
    a falsy test (`not count`) would refuse it as readily as `null`."""
    stored = chain.write_chain()
    assert stored["count"] == 0
    assert chain.verify()["ok"] is True

    _add("a")
    stored = chain.write_chain()
    assert type(stored["count"]) is int and stored["count"] == 1
    result = chain.verify()
    assert result["ok"] is True, result
    assert "ahead" not in result


# --- the append sequence ------------------------------------------------------
# `seq` decides the read order of rows that share a second, so a sealed row's
# seq is history like any other field: rewriting it can replay a same-second
# edit before the record it edits, and every fold downstream would believe it.
# Checkpoint v3 folds `seq`; v2 was written before it existed and does not.

def _rewrite_seq(session_id, new_seq):
    seg = _seg(session_id)
    rows = [json.loads(line) for line in seg.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row["seq"] = new_seq
    seg.write_text("".join(events.canonical_json(r) + "\n" for r in rows),
                   encoding="utf-8")


def _legacy(text, session_id="s", ts="2026-06-20T10:00:00Z"):
    """A row as pre-`seq` code wrote it: the five Event keys and nothing else."""
    seg = _seg(session_id)
    seg.parent.mkdir(parents=True, exist_ok=True)
    with open(seg, "a", encoding="utf-8") as f:
        f.write(events.canonical_json(
            {"ts": ts, "session_id": session_id, "type": "decision",
             "payload": {"text": text}, "tags": []}) + "\n")


def test_write_chain_writes_a_v3_checkpoint():
    _add("a")
    stored = chain.write_chain()
    assert stored["version"] == chain.CHECKPOINT_VERSION == 3
    assert chain.verify()["format"] == 3


def test_rewriting_seq_in_a_sealed_row_is_caught():
    _add("add", session_id="zzz-writer")
    _add("edit", session_id="aaa-editor")
    chain.write_chain()

    _rewrite_seq("zzz-writer", 2 ** 62)

    # The tamper is real: the add now replays AFTER the edit.
    assert [e.payload["text"] for e in events.read_all()] == ["edit", "add"]
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"


def test_a_v2_checkpoint_still_verifies_once_rows_carry_seq():
    """A store sealed by pre-`seq` code is VERIFIED after the upgrade, not
    alarmed at: v2 folds the five Event fields, so its head is exactly the
    event-list chain over the one segment in append order."""
    _add("a")
    _add("b")
    head = chain.compute_chain(events.read_all())["head"]
    _put_checkpoint({"version": 2, "count": 2, "head": head,
                     "segments": {"s.jsonl": 2}})

    result = chain.verify()
    assert result["ok"] is True, result
    assert result["format"] == 2


def test_legacy_rows_hash_identically_under_v2_and_v3():
    """What makes the v3 bump free for history: a row without `seq` contributes
    the same material to both folds, so re-anchoring a legacy store to v3 does
    not move any head it could already be compared against."""
    _legacy("one")
    _legacy("two")
    stored = chain.write_chain()
    assert stored["version"] == 3
    assert stored["head"] == chain.compute_chain(events.read_all())["head"]


def test_a_v3_head_differs_from_a_v2_head_once_rows_carry_seq():
    """CANARY for the two tests above: if `seq` were silently left out of the v3
    material, v3 would equal v2 and the tamper test would be the only thing
    standing -- this names that failure directly."""
    _add("a")
    stored = chain.write_chain()
    assert stored["head"] != chain.compute_chain(events.read_all())["head"]


# A v2 checkpoint over stamped rows is GREEN and does not cover their stamps: a
# process still on pre-v3 code re-sealed rows a newer writer had stamped. That
# is correct -- it folded what it knew -- but a `seq` rewrite inside that prefix
# would verify too, so the verdict says how many sealed rows it left uncovered.

def test_a_v2_checkpoint_counts_the_sealed_stamps_it_does_not_cover():
    """Three sealed rows, two of them stamped, and a stamped row appended after
    the seal. `seq_unsealed` is 2. That number kills three wrong counts at
    once: every sealed row (3), every stamped row on disk (3), and every row
    the scan saw (4)."""
    _legacy("old")
    _add("a")
    _add("b")
    _put_checkpoint({"version": 2, "count": 3, "head": _v2_head(),
                     "segments": {"s.jsonl": 3}})
    _add("after the seal")

    result = chain.verify()
    assert result["ok"] is True, result
    assert result["format"] == 2
    assert result["ahead"] == 1
    assert result["seq_unsealed"] == 2


def test_a_v2_checkpoint_over_legacy_rows_reports_no_unsealed_stamp():
    """Nothing sealed carries a stamp, so nothing is uncovered and the field is
    ABSENT -- not zero, matching how `ahead` is reported. The stamped row
    appended after the seal is outside the prefix and must not count; the
    sibling test above is the canary proving the field can appear at all."""
    _legacy("one")
    _legacy("two")
    _put_checkpoint({"version": 2, "count": 2, "head": _v2_head(),
                     "segments": {"s.jsonl": 2}})
    _add("stamped after the seal")

    result = chain.verify()
    assert result["ok"] is True, result
    assert result["format"] == 2
    assert result["ahead"] == 1
    assert "seq_unsealed" not in result


def test_a_v3_checkpoint_never_reports_unsealed_stamps():
    """v3 folds every stamp it seals, so the field is a v2-only warning. Kills
    an implementation that counts stamped prefix rows whatever the format."""
    _add("a")
    _add("b")
    chain.write_chain()

    result = chain.verify()
    assert result["ok"] is True, result
    assert result["format"] == 3
    assert "seq_unsealed" not in result


# --- migration ----------------------------------------------------------------

def test_a_v1_checkpoint_is_still_verified_rather_than_alarming():
    """Stores checkpointed before this change carry `{count, head}` with no
    per-segment map. Re-anchoring happens on the next projection; until then the
    old ts-prefix check still runs, so nothing reports a false tamper."""
    _add("a", ts="2026-06-20T10:00:00Z")
    _add("b", ts="2026-06-20T10:00:01Z")
    _mint_v1()

    result = chain.verify()
    assert result["ok"] is True
    assert result["format"] == 1


def _stamped_rows_whose_orders_all_disagree():
    """An earlier-second row, then a same-second add/edit pair across two
    sessions appended writer first, then a later-second row. Every wrong order
    a fold could use differs from the one v1 was minted in over the three rows a
    v1 checkpoint seals (`sealable` holds back the newest second):

    * seq-blind legacy ``(ts, segment, line)``, which v1 was minted in:
      ``[early, edit, add]``
    * `read_all`'s ``(ts, seq, segment, line)``: ``[early, add, edit]``
    * the legacy key with its tail swapped, ``(ts, line, segment)``:
      ``[early, add, edit]``
    * the v2/v3 append walk ``(segment, line)``: ``[early, edit, later]``

    so a shim folding any wrong one misses the stored head. `early` is what arms
    the swapped tail: it pushes `edit` to line 1 of aaa-editor while `add` is
    line 0 of zzz-writer. With both tie rows at line 0 the line index tied too,
    the segment name decided under either tail, and that mutant verified green."""
    _add("early", session_id="aaa-editor", ts="2026-06-20T09:59:59Z")
    _add("add", session_id="zzz-writer", ts="2026-06-20T10:00:00Z")
    _add("edit", session_id="aaa-editor", ts="2026-06-20T10:00:00Z")
    _add("later", session_id="aaa-editor", ts="2026-06-20T10:00:01Z")


def test_a_v1_checkpoint_sealed_over_stamped_rows_still_verifies():
    """The main checkout still runs pre-`seq` code, and its `write_chain` mints
    v1 over whatever rows are on disk -- stamped ones included, folded in the
    seq-blind order. When `_verify_v1` folded `read_all`, whose same-second tie
    now follows `seq`, this untouched store came back `hash_mismatch`, and it
    stayed red until current code re-anchored it; the stale process's next
    projection turned it red again. Kills: a shim folding `read_all`, one
    folding the segment walk, and one sorting on the legacy key with its tail
    swapped, ``events._order_key(ev.ts, None, line_index, segment)``."""
    _stamped_rows_whose_orders_all_disagree()
    v1 = _mint_v1()
    assert v1["count"] == 3

    result = chain.verify()
    assert result["ok"] is True, result
    assert result["format"] == 1
    assert result["ahead"] == 1


def test_the_stamped_fixture_really_orders_three_ways():
    """CANARY for the test above. If the stamp stopped reordering the pair, the
    pair's session names stopped inverting it, or the pair fell back onto one
    line index, a wrong order would agree with the legacy one over the sealed
    three rows and the test above would pass against that defect. The swapped
    tail is built from the raw bytes for the reason `_pre_seq_order` is."""
    _stamped_rows_whose_orders_all_disagree()
    raw = [(json.loads(line), seg.name, i) for seg in _segments()
           for i, line in enumerate(seg.read_text(encoding="utf-8").splitlines())]
    walk = [obj["payload"]["text"] for obj, _, _ in raw]
    swapped = [obj["payload"]["text"] for obj, _, _ in
               sorted(raw, key=lambda t: (t[0]["ts"], t[2], t[1]))]

    legacy = [e.payload["text"] for e in _pre_seq_order()]
    assert legacy[:3] == ["early", "edit", "add"]
    assert [e.payload["text"] for e in events.read_all()][:3] == [
        "early", "add", "edit"]
    assert swapped[:3] == ["early", "add", "edit"]
    assert walk[:3] == ["early", "edit", "later"]


def test_a_v1_checkpoint_over_stamped_rows_still_catches_a_tamper():
    """CANARY that the legacy-order fold still checks something: rewrite the
    sealed `edit` row and the same checkpoint must go red."""
    _stamped_rows_whose_orders_all_disagree()
    _mint_v1()
    seg = _seg("aaa-editor")
    seg.write_text(seg.read_text(encoding="utf-8").replace('"edit"', '"forged"'),
                   encoding="utf-8")

    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"
    assert result["format"] == 1


def test_the_v1_shim_reads_the_segments_not_read_all(monkeypatch):
    """`read_all` is not a file read under every configuration: with
    `MINDHIVE_EVENT_BACKEND=pg` it answers from the Postgres mirror, which also
    orders by the stamp. The shim must fold the segments it was asked about in
    the order v1 was minted in, so it has no business calling it. Before this
    fix the refusal below surfaced as `read_error`."""
    _add("a", ts="2026-06-20T10:00:00Z")
    _add("b", ts="2026-06-20T10:00:01Z")
    _mint_v1()

    def refuse(project=None):
        raise AssertionError("the v1 shim called events.read_all")

    monkeypatch.setattr(events, "read_all", refuse)
    result = chain.verify()
    assert result["ok"] is True, result
    assert result["format"] == 1


@pytest.mark.parametrize("exc, reason", [
    (json.JSONDecodeError("bad", "{", 0), "corrupt_event_log"),
    (OSError("segment unreadable"), "read_error"),
])
def test_the_v1_shim_maps_a_failed_read_to_a_verdict(monkeypatch, exc, reason):
    """PRESERVATION pin, green before and after the legacy-order rewrite: the
    shim's read failures stay verdicts with the reasons they always had, never
    a raise. `_segment_lines` is the seam because every segment reader --
    `read_all` then, the shim's own walk now -- goes through it."""
    _add("a")
    _mint_v1()

    def explode(seg):
        raise exc

    monkeypatch.setattr(events, "_segment_lines", explode)
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == reason
    assert result["detail"] == str(exc)


def test_a_v1_checkpoint_still_catches_a_tamper():
    """CANARY for the shim: prove the v1 path still verifies rather than waving
    everything through."""
    _add("alpha", ts="2026-06-20T10:00:00Z")
    _add("closer", ts="2026-06-20T10:00:01Z")
    _mint_v1()
    seg = _segments()[0]
    seg.write_text(seg.read_text(encoding="utf-8").replace('"alpha"', '"beta"'),
                   encoding="utf-8")

    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"


def test_write_chain_upgrades_a_v1_checkpoint():
    _add("a")
    (paths.project_dir() / "chain.json").write_text(
        json.dumps({"count": 0, "head": "0" * 64}), encoding="utf-8")
    chain.write_chain()
    stored = json.loads(
        (paths.project_dir() / "chain.json").read_text(encoding="utf-8"))
    assert stored["segments"] == {"s.jsonl": 1}
    assert chain.verify().get("format") != 1


# --- projection wiring --------------------------------------------------------

def test_project_reads_event_log_only_once(monkeypatch):
    """projector.project must not re-read the event log through read_all inside
    write_chain. The chain now scans segments directly (it needs the segment and
    line each row came from, which read_all discards), so read_all stays at one
    call."""
    from mindhive import projector
    _add("a")
    calls = {"n": 0}
    real = events.read_all

    def counting(project=None):
        calls["n"] += 1
        return real(project)

    monkeypatch.setattr(events, "read_all", counting)
    projector.project()
    assert calls["n"] == 1


def test_projection_leaves_verify_green():
    _add("a", ts="2026-06-20T10:00:00Z")
    _add("b", ts="2026-06-20T10:00:01Z")
    from mindhive import projector
    projector.project()
    result = chain.verify()
    assert result["ok"] is True
    assert "ahead" not in result


# --- verify racing a projection -----------------------------------------------
# `verify` reads chain.json BEFORE it scans the segments. Reversed, a projection
# landing in the window leaves verify holding an OLD scan and a NEWER
# checkpoint, which claims lines the scan never saw -- a FALSE hash_mismatch,
# the exact flapping this module was rewritten to end. Both tests below failed
# against the scan-first order; they exist so nobody restores it.

def test_a_projection_landing_mid_verify_does_not_produce_a_false_mismatch(
        monkeypatch):
    _add("a")
    chain.write_chain()
    assert chain.verify()["ok"] is True            # baseline

    real_scan = chain._scan
    fired = []

    def scan_then_projection(project=None):
        out = real_scan(project)                   # verify's scan: sees 1 row
        if not fired:
            fired.append(1)
            _add("b")                              # a normal append ...
            chain.write_chain()                    # ... and a re-seal at 2 rows
        return out

    monkeypatch.setattr(chain, "_scan", scan_then_projection)
    assert chain.verify()["ok"] is True


def test_a_projection_sealing_a_brand_new_segment_mid_verify_is_not_a_mismatch(
        monkeypatch):
    """The same window, but the projection seals a segment verify never globbed
    — so the checkpoint names a file absent from the scan and `_shortfall` is
    the branch that would fire."""
    _add("a", session_id="s")
    chain.write_chain()

    real_scan = chain._scan
    fired = []

    def scan_then_projection(project=None):
        out = real_scan(project)
        if not fired:
            fired.append(1)
            _add("b", session_id="other")          # a BRAND-NEW segment
            chain.write_chain()
        return out

    monkeypatch.setattr(chain, "_scan", scan_then_projection)
    result = chain.verify()
    assert result["ok"] is True, result
    assert "truncated" not in result
