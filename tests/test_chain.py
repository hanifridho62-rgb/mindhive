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


def test_verify_detects_delete_by_corruption():
    # Deleting a sealed event by corrupting its line is caught: the row stops
    # parsing, so it leaves the fold and the recomputed head moves.
    _add("one")
    _add("two", ts="2026-06-20T11:00:00Z")
    chain.write_chain()
    seg = _segments()[0]
    seg.write_text(
        seg.read_text(encoding="utf-8").replace('"text":"one"', "GARBAGE"),
        encoding="utf-8")
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"


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

@pytest.mark.parametrize("broken", [[], "x", 5, None])
def test_a_v2_checkpoint_with_a_wrecked_segments_map_is_refused(broken):
    """Dispatch keys on `version`, never on the shape of `segments`. Under
    shape-dispatch all four of these fell through to the v1 shim -- which does
    not read `segments` at all -- and returned ok=True/format=1: a silent
    downgrade to the weaker ts-ordered check, available to anyone who could
    replace one key in the file the check depends on."""
    _add("a")
    stored = chain.write_chain()
    _put_checkpoint({**stored, "segments": broken})
    result = chain.verify()
    assert result["ok"] is False
    assert result["reason"] == "corrupt_chain_file"
    assert result.get("format") == 2


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


@pytest.mark.parametrize("ver", [3, "2", 1.5, True])
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


# --- migration ----------------------------------------------------------------

def test_a_v1_checkpoint_is_still_verified_rather_than_alarming():
    """Stores checkpointed before this change carry `{count, head}` with no
    per-segment map. Re-anchoring happens on the next projection; until then the
    old ts-prefix check still runs, so nothing reports a false tamper."""
    _add("a", ts="2026-06-20T10:00:00Z")
    _add("b", ts="2026-06-20T10:00:01Z")
    v1 = chain.compute_chain(chain.sealable(events.read_all()))
    (paths.project_dir() / "chain.json").write_text(
        json.dumps({"count": v1["count"], "head": v1["head"]}), encoding="utf-8")

    result = chain.verify()
    assert result["ok"] is True
    assert result["format"] == 1


def test_a_v1_checkpoint_still_catches_a_tamper():
    """CANARY for the shim: prove the v1 path still verifies rather than waving
    everything through."""
    _add("alpha", ts="2026-06-20T10:00:00Z")
    _add("closer", ts="2026-06-20T10:00:01Z")
    v1 = chain.compute_chain(chain.sealable(events.read_all()))
    (paths.project_dir() / "chain.json").write_text(
        json.dumps({"count": v1["count"], "head": v1["head"]}), encoding="utf-8")
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
