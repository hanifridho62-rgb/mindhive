# tests/test_consensus.py
"""Contracts for the participatory-consensus family: `contribution` events,
`consensus_record` events, and the weighted fold between them.

Encodes the blueprint at
docs/plans/2026-08-19-001-participatory-intelligence-gateway-plan.md — §5
("what the job may and may not do") and §8 (this test plan). Two properties are
the whole point and both are asserted against a plausible wrong implementation
rather than against a hand-picked happy case:

* **Not naive averaging.** Per-subject weight normalisation plus a weighted
  MEDIAN. A weighted mean, or an unnormalised count, produces a different number
  on the fixtures below — that is how these tests discriminate.
* **No black boxes.** A `consensus_record` that does not say what weight each
  input got, and why, is refused at the append surface. `trace` walks a record
  back to the events it folded, and reports a ref it cannot resolve instead of
  raising.
"""
import pytest

from mindhive import events
from mindhive import consensus
from mindhive.consensus import contributions, records


# --- fixture builders: valid by default, one corruption per test --------------

def make_contribution(**overrides) -> dict:
    """A complete valid `contribution` payload. Each test breaks ONE field."""
    body = {
        "frame_id": "jakarta-anxiety-weekly",
        "kind": "biometric.resting_hr",
        "subject": "subject-0001",
        "value": 62,
    }
    body.update(overrides)
    return body


def make_record(**overrides) -> dict:
    """A complete valid `consensus_record` payload."""
    body = {
        "frame_id": "jakarta-anxiety-weekly",
        "rule_version": consensus.RULE_VERSION,
        "window": {"start": "2026-08-01T00:00:00Z", "end": "2026-08-08T00:00:00Z"},
        "inputs": [
            {"ref": "a" * 64, "occurrence": 1, "weight": 1.0,
             "weight_exact": "1", "reason": "subject-share 1/1"},
        ],
        "value": 62,
        "value_kind": "numeric",
        "subjects": 1,
    }
    body.update(overrides)
    return body


def contribution_event(subject, value, *, frame="f1", kind="numeric.probe",
                       ts="2026-08-02T00:00:00Z", confidence=None) -> events.Event:
    """A `contribution` Event as read_all would hand it to the fold. Built
    directly rather than appended so the fold tests stay pure folds over a
    golden list — no store, no I/O."""
    payload = {"frame_id": frame, "kind": kind, "subject": subject,
               "value": value}
    if confidence is not None:
        payload["confidence"] = confidence
    return events.Event(ts=ts, session_id="s1", type="contribution",
                        payload=payload, tags=[])


# --- the contribution contract ------------------------------------------------

def test_contribution_payload_rejects_a_non_object():
    with pytest.raises(ValueError):
        contributions.validate_payload("just a string")


def test_contribution_payload_requires_a_frame_id():
    body = make_contribution()
    del body["frame_id"]
    with pytest.raises(ValueError):
        contributions.validate_payload(body)


def test_contribution_payload_requires_a_kind_discriminant():
    # The kind is what makes this family general-purpose: a youth anxiety
    # sample and an executive's strategic input differ HERE and nowhere else,
    # so a missing kind would put the demographic back into the schema.
    body = make_contribution()
    del body["kind"]
    with pytest.raises(ValueError):
        contributions.validate_payload(body)


def test_contribution_payload_requires_a_subject():
    # Without a subject the per-subject weight normalisation has nothing to
    # normalise by, and one flooding participant silently outvotes a crowd.
    body = make_contribution()
    del body["subject"]
    with pytest.raises(ValueError):
        contributions.validate_payload(body)


def test_contribution_payload_requires_a_value():
    body = make_contribution()
    del body["value"]
    with pytest.raises(ValueError):
        contributions.validate_payload(body)


def test_contribution_payload_rejects_an_out_of_range_confidence():
    with pytest.raises(ValueError):
        contributions.validate_payload(make_contribution(confidence=1.5))
    with pytest.raises(ValueError):
        contributions.validate_payload(make_contribution(confidence=-0.1))


def test_contribution_payload_refuses_a_self_claimed_weight():
    # A contributor may not tell the fold how much it counts. Accepting a
    # submitted weight would make the audit trail a record of what the sender
    # asked for rather than of what the rule decided.
    with pytest.raises(ValueError):
        contributions.validate_payload(make_contribution(weight=99))


def test_contribution_payload_accepts_a_minimal_valid_body():
    # The silence half of every fire/silence pair above.
    contributions.validate_payload(make_contribution())
    # NOT 0.0, which this line used to assert was valid. The range is
    # half-open: 0 multiplies the weight to nothing, the record contract
    # refuses a zero weight, and the contribution cannot be un-appended — so
    # accepting it meant one contribution could make a frame permanently
    # unfoldable. See
    # test_a_zero_confidence_contribution_is_refused_at_the_append_surface.
    contributions.validate_payload(make_contribution(confidence=0.001))
    contributions.validate_payload(make_contribution(confidence=1.0))
    contributions.validate_payload(make_contribution(value="expand"))


# --- the consensus_record contract -------------------------------------------

def test_record_payload_rejects_an_empty_inputs_list():
    with pytest.raises(ValueError):
        records.validate_payload(make_record(inputs=[]))


def test_record_payload_requires_a_rule_version():
    # Changing the weighting rule must produce records that say which rule made
    # them; an unversioned record cannot be re-derived.
    body = make_record()
    del body["rule_version"]
    with pytest.raises(ValueError):
        records.validate_payload(body)


def test_record_payload_requires_a_window_with_both_bounds():
    with pytest.raises(ValueError):
        records.validate_payload(make_record(window={"start": "2026-08-01T00:00:00Z"}))
    with pytest.raises(ValueError):
        records.validate_payload(make_record(window="last week"))


def test_record_payload_requires_every_input_to_carry_a_weight_and_a_reason():
    # THE black-box guard. A number whose weights are not recorded cannot be
    # walked backwards, which is the failure condition the PRD names.
    no_weight = make_record(
        inputs=[{"ref": "a" * 64, "occurrence": 1, "weight_exact": "1",
                 "reason": "because"}])
    with pytest.raises(ValueError):
        records.validate_payload(no_weight)

    no_reason = make_record(
        inputs=[{"ref": "a" * 64, "occurrence": 1, "weight": 1.0,
                 "weight_exact": "1"}])
    with pytest.raises(ValueError):
        records.validate_payload(no_reason)

    blank_reason = make_record(
        inputs=[{"ref": "a" * 64, "occurrence": 1, "weight": 1.0,
                 "weight_exact": "1", "reason": "   "}])
    with pytest.raises(ValueError):
        records.validate_payload(blank_reason)


def test_record_payload_requires_an_occurrence_on_every_input():
    # A ref is a CONTENT address, so N byte-identical contributions share one.
    # Without an occurrence the record says "one of these" N times, and N inputs
    # would prove one event — the ambiguity this field exists to remove.
    with pytest.raises(ValueError):
        records.validate_payload(make_record(
            inputs=[{"ref": "a" * 64, "weight": 1.0, "weight_exact": "1",
                     "reason": "no occurrence"}]))
    with pytest.raises(ValueError):
        records.validate_payload(make_record(
            inputs=[{"ref": "a" * 64, "occurrence": 0, "weight": 1.0,
                     "weight_exact": "1",
                     "reason": "1-based, so 0 names nothing"}]))


def test_record_payload_requires_a_ref_on_every_input():
    with pytest.raises(ValueError):
        records.validate_payload(
            make_record(inputs=[{"weight": 1.0, "weight_exact": "1", "reason": "no ref"}]))


def test_record_payload_accepts_a_minimal_valid_body():
    records.validate_payload(make_record())


def test_a_folded_record_satisfies_its_own_append_contract():
    # Round trip: whatever the fold produces must be appendable. A fold whose
    # output its own validator rejects is unusable, and nothing else here would
    # catch that.
    evs = [contribution_event("s-a", 10), contribution_event("s-b", 20)]
    folded = consensus.fold_window(evs, "f1")
    records.validate_payload(folded)


# --- event identity -----------------------------------------------------------

def test_event_ref_is_content_addressed_and_moves_when_the_payload_moves():
    # A rewritten event must get a new ref, so a stale trace breaks loudly
    # instead of pointing confidently at changed data.
    a = contribution_event("s-a", 10)
    same = contribution_event("s-a", 10)
    different = contribution_event("s-a", 11)
    assert consensus.event_ref(a) == consensus.event_ref(same)
    assert consensus.event_ref(a) != consensus.event_ref(different)
    assert len(consensus.event_ref(a)) == 64


# --- the fold -----------------------------------------------------------------

def test_fold_answers_none_for_a_window_with_no_contributions():
    # A native branch of normal execution, not an error: "nothing arrived yet"
    # is an expected state of a participatory frame, and raising here would
    # make every caller wrap the call.
    assert consensus.fold_window([], "f1") is None
    assert consensus.fold_window([contribution_event("s-a", 10)], "other") is None


def test_fold_normalises_weight_per_subject_so_one_flooder_gets_one_share():
    # DISCRIMINATING. Subject A sends three 100s, subject B sends one 0.
    #   subject-share + weighted median -> 50   (this implementation)
    #   unnormalised weighted median    -> 100  (a flooder wins)
    #   naive arithmetic mean           -> 75
    # All three differ, so this fixture kills both wrong implementations.
    evs = [
        contribution_event("s-a", 100),
        contribution_event("s-a", 100),
        contribution_event("s-a", 100),
        contribution_event("s-b", 0),
    ]
    folded = consensus.fold_window(evs, "f1")
    assert folded["value"] == 50
    assert folded["subjects"] == 2


def test_fold_uses_a_weighted_median_not_a_weighted_mean():
    # DISCRIMINATING. Four distinct subjects, one extreme outlier.
    #   weighted median -> 25    (mean of the two straddling the half-weight)
    #   weighted mean   -> 265   (the outlier drags it)
    evs = [
        contribution_event("s-a", 10),
        contribution_event("s-b", 20),
        contribution_event("s-c", 30),
        contribution_event("s-d", 1000),
    ]
    assert consensus.fold_window(evs, "f1")["value"] == 25


def test_fold_lets_a_stated_confidence_move_the_result():
    # DISCRIMINATING. Same two subjects, different confidence.
    #   with confidence    -> 20  (B's full share crosses the half-weight)
    #   ignoring confidence -> 15 (an exact tie, averaged)
    evs = [
        contribution_event("s-a", 10, confidence=0.5),
        contribution_event("s-b", 20, confidence=1.0),
    ]
    assert consensus.fold_window(evs, "f1")["value"] == 20


def test_fold_ranks_a_categorical_frame_by_weighted_agreement():
    # DISCRIMINATING. Subject A sends "expand" three times; B and C each send
    # "consolidate" once.
    #   weighted by subject share -> consolidate (2.0 vs 1.0)
    #   naive vote count          -> expand      (3 vs 2)
    evs = [
        contribution_event("s-a", "expand", kind="strategic.direction"),
        contribution_event("s-a", "expand", kind="strategic.direction"),
        contribution_event("s-a", "expand", kind="strategic.direction"),
        contribution_event("s-b", "consolidate", kind="strategic.direction"),
        contribution_event("s-c", "consolidate", kind="strategic.direction"),
    ]
    folded = consensus.fold_window(evs, "f1")
    assert folded["value"] == "consolidate"
    assert folded["value_kind"] == "categorical"


def test_fold_ignores_contributions_belonging_to_another_frame():
    evs = [
        contribution_event("s-a", 10, frame="f1"),
        contribution_event("s-b", 9999, frame="f2"),
    ]
    folded = consensus.fold_window(evs, "f1")
    assert folded["value"] == 10
    assert len(folded["inputs"]) == 1


def test_fold_window_bounds_are_inclusive_at_both_ends():
    evs = [
        contribution_event("s-a", 1, ts="2026-08-01T00:00:00Z"),
        contribution_event("s-b", 2, ts="2026-08-05T00:00:00Z"),
        contribution_event("s-c", 3, ts="2026-08-09T00:00:00Z"),
    ]
    inside = consensus.fold_window(evs, "f1", start="2026-08-01T00:00:00Z",
                                   end="2026-08-09T00:00:00Z")
    assert len(inside["inputs"]) == 3

    narrowed = consensus.fold_window(evs, "f1", start="2026-08-02T00:00:00Z",
                                      end="2026-08-08T00:00:00Z")
    assert len(narrowed["inputs"]) == 1
    assert narrowed["value"] == 2


def test_fold_records_a_ref_a_weight_and_a_reason_for_every_input():
    evs = [contribution_event("s-a", 10), contribution_event("s-a", 20),
           contribution_event("s-b", 30)]
    folded = consensus.fold_window(evs, "f1")
    assert len(folded["inputs"]) == 3
    for row in folded["inputs"]:
        assert len(row["ref"]) == 64
        assert row["occurrence"] >= 1
        assert row["weight"] > 0
        assert row["reason"].strip()
    # The reason must name the share that produced the weight, not merely exist.
    assert any("1/2" in row["reason"] for row in folded["inputs"])


def test_fold_numbers_byte_identical_contributions_1_2_3():
    # Three contributions from one subject with the same value in the same
    # second are BYTE-identical, so all three hash to one ref. The fold must
    # still emit three distinguishable input rows.
    evs = [contribution_event("s-a", 100) for _ in range(3)]
    rows = consensus.fold_window(evs, "f1")["inputs"]
    assert len({row["ref"] for row in rows}) == 1          # one content address
    assert [row["occurrence"] for row in rows] == [1, 2, 3]


def test_fold_skips_a_malformed_contribution_row_instead_of_raising():
    # Reads stay total: a torn payload is skipped, exactly as read_all skips a
    # torn line. Swallowing it into nothing would be worse, so the surviving
    # rows are asserted too.
    broken = events.Event(ts="2026-08-02T00:00:00Z", session_id="s1",
                          type="contribution", payload=None, tags=[])
    no_subject = events.Event(ts="2026-08-02T00:00:00Z", session_id="s1",
                              type="contribution",
                              payload={"frame_id": "f1", "value": 5}, tags=[])
    evs = [contribution_event("s-a", 10), broken, no_subject,
           contribution_event("s-b", 20)]
    folded = consensus.fold_window(evs, "f1")
    assert len(folded["inputs"]) == 2
    assert folded["value"] == 15


def test_fold_ignores_events_that_are_not_contributions():
    other = events.Event(ts="2026-08-02T00:00:00Z", session_id="s1",
                         type="caveat", payload={"text": "not a contribution"},
                         tags=[])
    evs = [contribution_event("s-a", 10), other]
    assert len(consensus.fold_window(evs, "f1")["inputs"]) == 1


# --- the audit trace ----------------------------------------------------------

def test_trace_resolves_every_input_ref_to_the_event_it_folded():
    evs = [contribution_event("s-a", 10), contribution_event("s-b", 20)]
    folded = consensus.fold_window(evs, "f1")
    walked = consensus.trace(evs, folded)
    assert len(walked) == 2
    assert all(row["found"] for row in walked)
    assert {row["event"]["payload"]["value"] for row in walked} == {10, 20}
    # Each resolved row carries the weight and reason from the record, so a
    # reader sees the derivation, not just the inputs.
    assert all(row["reason"].strip() and row["weight"] > 0 for row in walked)


def test_trace_reports_an_unresolvable_ref_instead_of_raising():
    evs = [contribution_event("s-a", 10)]
    folded = consensus.fold_window(evs, "f1")
    orphan = dict(folded, inputs=list(folded["inputs"]) +
                  [{"ref": "f" * 64, "occurrence": 1, "weight": 1.0,
                    "reason": "planted"}])
    walked = consensus.trace(evs, orphan)
    assert len(walked) == 2
    assert [row["found"] for row in walked] == [True, False]
    assert walked[1]["event"] is None


def test_trace_resolves_identical_inputs_to_that_many_separate_events():
    # Discriminates against resolving a ref to ONE event: with three identical
    # events in the ledger all three rows resolve, and the walk names each by
    # its own occurrence.
    evs = [contribution_event("s-a", 100) for _ in range(3)]
    folded = consensus.fold_window(evs, "f1")
    walked = consensus.trace(evs, folded)
    assert [row["occurrence"] for row in walked] == [1, 2, 3]
    assert all(row["found"] for row in walked)


def test_trace_refuses_to_prove_three_identical_inputs_from_one_event():
    # THE defect this field fixes, as a test. A record folded over three
    # identical contributions, checked against a ledger holding only one of
    # them, is NOT proven — a ref-only walk reports all three as found, which
    # is a false proof of two events that are not there.
    evs = [contribution_event("s-a", 100) for _ in range(3)]
    folded = consensus.fold_window(evs, "f1")

    walked = consensus.trace(evs[:1], folded)
    assert [row["found"] for row in walked] == [True, False, False]
    assert [row["event"] for row in walked[1:]] == [None, None]


def test_trace_canary_a_record_whose_weights_were_stripped_is_refused():
    # Arms test_record_payload_requires_every_input_to_carry_a_weight_and_a_
    # reason: prove the guard CAN fire on a record produced by the real fold,
    # not only on a hand-built fixture.
    evs = [contribution_event("s-a", 10), contribution_event("s-b", 20)]
    folded = consensus.fold_window(evs, "f1")
    records.validate_payload(folded)                      # the silence half

    stripped = dict(folded, inputs=[{"ref": row["ref"]}
                                    for row in folded["inputs"]])
    with pytest.raises(ValueError):                       # the canary
        records.validate_payload(stripped)


# --- registry wiring, through the public gate --------------------------------

def test_both_types_append_and_are_contract_gated():
    # Registration is asserted through the two public seams rather than by
    # reading the private table: append_event refuses an unregistered type, and
    # validate_typed_payload is a no-op for a type nobody registered — so an
    # unwired type would pass the second check vacuously. Both halves here.
    events.validate_typed_payload("contribution", make_contribution())
    with pytest.raises(ValueError):
        events.validate_typed_payload("contribution", {"nothing": "valid"})

    events.validate_typed_payload("consensus_record", make_record())
    with pytest.raises(ValueError):
        events.validate_typed_payload("consensus_record", {"nothing": "valid"})


def test_contribute_appends_a_contribution_that_the_fold_reads_back():
    ev = consensus.contribute("numeric.probe", "f1", "s-a", 12, project="test")
    assert ev.type == "contribution"
    folded = consensus.fold_window(events.read_all("test"), "f1")
    assert folded["value"] == 12
    assert folded["inputs"][0]["ref"] == consensus.event_ref(ev)


def test_fold_and_record_appends_a_record_and_answers_none_when_empty():
    assert consensus.fold_and_record("empty-frame", project="test") is None

    consensus.contribute("numeric.probe", "f1", "s-a", 10, project="test")
    consensus.contribute("numeric.probe", "f1", "s-b", 20, project="test")
    rec = consensus.fold_and_record("f1", project="test")
    assert rec.type == "consensus_record"
    assert rec.payload["value"] == 15
    assert rec.payload["rule_version"] == consensus.RULE_VERSION


def test_latest_record_finds_the_newest_record_for_one_frame():
    consensus.contribute("numeric.probe", "f1", "s-a", 10, project="test")
    consensus.fold_and_record("f1", project="test")
    consensus.contribute("numeric.probe", "f2", "s-a", 99, project="test")
    consensus.fold_and_record("f2", project="test")

    evs = events.read_all("test")
    assert consensus.latest_record(evs, "f1")["value"] == 10
    assert consensus.latest_record(evs, "f2")["value"] == 99
    assert consensus.latest_record(evs, "f3") is None


# --- CLI exit codes -----------------------------------------------------------

def test_cli_contribute_fold_and_trace_exit_zero():
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["consensus", "contribute", "--frame", "f1",
                         "--kind", "numeric.probe", "--subject", "s-a",
                         "--value", "10"]) == 0
    assert cli_mod.main(["consensus", "contribute", "--frame", "f1",
                         "--kind", "numeric.probe", "--subject", "s-b",
                         "--value", "20"]) == 0
    assert cli_mod.main(["consensus", "fold", "--frame", "f1"]) == 0
    assert cli_mod.main(["consensus", "trace", "--frame", "f1"]) == 0


def test_cli_contribute_refuses_an_invalid_body_with_exit_one():
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["consensus", "contribute", "--frame", "f1",
                         "--kind", "numeric.probe", "--subject", "s-a",
                         "--value", "10", "--confidence", "9"]) == 1


def test_cli_fold_on_an_empty_frame_is_a_success_not_a_failure():
    # `nothing_to_do` is a success in this codebase's outcome vocabulary, so an
    # empty frame exits 0 — a non-zero here would make a scheduled fold look
    # broken every quiet week.
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["consensus", "fold", "--frame", "nobody-answered"]) == 0


def test_cli_trace_exits_non_zero_when_a_ref_cannot_be_resolved():
    # The gate semantics `hive pg parity` set: exit non-zero when the claim
    # cannot be PROVEN, not merely when it is known false.
    from mindhive import __main__ as cli_mod
    consensus.contribute("numeric.probe", "f1", "s-a", 10, project="test")
    events.append_event("consensus_record", {
        "frame_id": "f1",
        "rule_version": consensus.RULE_VERSION,
        "window": {"start": "2026-01-01T00:00:00Z", "end": "2030-01-01T00:00:00Z"},
        "inputs": [{"ref": "f" * 64, "occurrence": 1, "weight": 1.0,
                    "reason": "planted orphan"}],
        "value": 10, "value_kind": "numeric", "subjects": 1,
    }, project="test")
    assert cli_mod.main(["consensus", "trace", "--frame", "f1"]) == 1


def test_cli_trace_without_a_record_is_a_success():
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["consensus", "trace", "--frame", "never-folded"]) == 0


# --- what a measured audit pass found ----------------------------------------

def test_a_zero_confidence_contribution_is_refused_at_the_append_surface():
    """MEASURED: `confidence: 0` sits inside [0.0, 1.0], so the contract
    accepted it; the fold then weighed the row 0; and `records.validate_payload`
    -- which requires a POSITIVE weight, for its own good reason -- refused the
    fold's own output. `fold_and_record` raised ValueError, and because the
    contribution is in an append-only ledger it would raise on every later run:
    one legal contribution bricked a frame permanently.

    A withheld input is not a weaker input. Refused at the surface, where an
    append-only store has to be defended, with the corrective hint."""
    with pytest.raises(ValueError) as e:
        contributions.validate_payload(make_contribution(confidence=0))
    assert "weightless" in str(e.value).lower()
    contributions.validate_payload(make_contribution(confidence=0.01))


def test_a_weightless_row_already_in_the_ledger_cannot_brick_the_frame():
    """The other half. The gate above stops new ones; this is a frame that
    already holds one, which no gate can undo. The fold drops rows that carry
    no weight -- they moved no median and can be argued with from the event
    itself -- so the frame keeps folding."""
    evs = [contribution_event("s-a", 10, confidence=0),
           contribution_event("s-b", 70)]
    folded = consensus.fold_window(evs, "f1")
    assert folded is not None, "one weightless row killed the whole frame"
    records.validate_payload(folded)
    assert folded["value"] == 70
    assert len(folded["inputs"]) == 1
    assert folded["subjects"] == 1


def test_a_frame_whose_every_row_is_weightless_folds_to_none():
    """`None` is the module's native answer for nothing to fold, and it is
    what `fold_and_record` already treats as a success. An empty `inputs` list
    would be refused by the record contract instead -- the brick again."""
    evs = [contribution_event("s-a", 10, confidence=0),
           contribution_event("s-b", 70, confidence=0)]
    assert consensus.fold_window(evs, "f1") is None


def test_a_non_finite_value_never_reaches_the_ledger():
    """MEASURED: `canonical_json({"value": float("nan")})` returned
    `{"value":NaN}` and that is what landed on disk -- a bare `NaN` token, which
    is not JSON, inside a hash-chained append-only ledger. Python's own reader
    accepts it, which is what kept it invisible; the Postgres mirror's JSONB and
    every strict parser downstream do not.

    Three gates, because the hole was three-deep: the contribution contract, the
    record contract, and `canonical_json` itself -- the one writer every event
    type goes through, so it is the seam that has to hold."""
    import math

    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            contributions.validate_payload(make_contribution(value=bad))
        with pytest.raises(ValueError):
            records.validate_payload(make_record(value=bad))
        with pytest.raises(ValueError):
            events.canonical_json({"value": bad})
    assert math.isfinite(62)
    contributions.validate_payload(make_contribution(value=62))


def test_the_mode_tie_break_really_is_the_smallest_value():
    """The docstring says ties break on the lexicographically smallest
    value. MEASURED: ('a','b') -> 'a' but ('a','ab') -> 'ab' and ('x','xy') ->
    'xy'. `[-ord(c) for c in value]` compares element-wise and then LENGTH, so a
    string that is a prefix of its rival loses to it -- the opposite of what was
    promised, on exactly the values a category name and its longer form take.
    A determinism rule that is wrong is not determinism."""
    from fractions import Fraction

    half = Fraction(1, 2)
    for pair in (("a", "b"), ("a", "ab"), ("x", "xy"), ("ab", "b")):
        got = consensus._weighted_mode([(pair[0], half), (pair[1], half)])
        assert got == min(pair), (
            "tie %s broke to %r, not the smallest %r" % (pair, got, min(pair)))


def test_the_published_weights_re_derive_the_recorded_value():
    """THE trace guarantee, and it was broken. MEASURED: a frame of three
    contributions from one subject plus one from another records value 50.0
    (exact shares 1/3,1/3,1/3,1) while the same median computed from the
    PUBLISHED weights -- `round(float(Fraction(1,3)), 6)` -- gives 90. The
    record's own numbers did not reproduce the record's own answer, which is the
    black box this module's whole contract exists to refuse, arriving through
    the audit trail rather than around it.

    The module's header already explains why the weights are Fractions and not
    floats. The record now publishes that exact share too."""
    from fractions import Fraction

    evs = [contribution_event("flooder", 10), contribution_event("flooder", 10),
           contribution_event("flooder", 10), contribution_event("solo", 90)]
    folded = consensus.fold_window(evs, "f1")
    records.validate_payload(folded)
    exact = [Fraction(row["weight_exact"]) for row in folded["inputs"]]
    assert exact == [Fraction(1, 3)] * 3 + [Fraction(1)]
    redrawn = consensus._weighted_median(
        list(zip([10, 10, 10, 90], exact)))
    assert redrawn == folded["value"], (
        "the trail publishes %r but re-derives %r" % (folded["value"], redrawn))


def test_the_record_contract_requires_the_exact_share():
    """Required, not merely written. The rounded float is a rendering for a
    human; the exact share is what makes the record re-derivable, and this file
    already holds that a record missing what it needs is refused at the surface
    rather than trusted downstream."""
    body = make_record()
    del body["inputs"][0]["weight_exact"]
    with pytest.raises(ValueError):
        records.validate_payload(body)
    for junk in ("", "   ", "1/0", "0/3", "-1/3", "one third", 0.333):
        with pytest.raises(ValueError):
            records.validate_payload(make_record(
                inputs=[{"ref": "a" * 64, "occurrence": 1, "weight": 1.0,
                         "weight_exact": junk,
                         "reason": "subject-share 1/1"}]))


def test_one_participant_under_two_spellings_gets_one_share():
    """MEASURED: 'alice', 'alice ' and 'Alice' counted as three subjects,
    each taking a full share against bob's one -- the flooding the per-subject
    rule is the module's first anti-gaming claim. The contract VALIDATES
    `subject.strip()` and then keys on the unstripped string, so a trailing
    space passed as a distinct participant.

    Whitespace is fixed here. CASE is deliberately NOT folded: a subject id may
    legitimately be case-sensitive, and quietly merging 'Alice' into 'alice'
    would be a policy decision wearing a bug fix's clothes."""
    evs = [contribution_event("alice", 10), contribution_event("alice ", 10),
           contribution_event(" alice", 10), contribution_event("bob", 90)]
    folded = consensus.fold_window(evs, "f1")
    assert folded["subjects"] == 2, (
        "counted %d subjects for 2 participants" % folded["subjects"])
    assert folded["value"] == 50, (
        "one voter under three spellings still outweighs the other")


def test_latest_record_is_last_wins_not_max_on_the_timestamp():
    """The docstring's rule, which had no test: last-wins over
    `read_all`'s (ts, segment, line) order rather than a max() on `ts`, so two
    records folded inside the SAME second resolve here the way they resolve
    everywhere else that reads this ledger. The existing test uses one record
    per frame, so ordering never arose in it."""
    first = events.Event(ts="2026-08-08T00:00:00Z", session_id="s1",
                         type=consensus.RECORD_TYPE,
                         payload={"frame_id": "f1", "value": 10}, tags=[])
    second = events.Event(ts="2026-08-08T00:00:00Z", session_id="s1",
                          type=consensus.RECORD_TYPE,
                          payload={"frame_id": "f1", "value": 20}, tags=[])
    assert consensus.latest_record([first, second], "f1")["value"] == 20
    assert consensus.latest_record([second, first], "f1")["value"] == 10


def test_a_mixed_frame_is_still_categorical_and_says_so():
    """NOT a fix -- a pin. MEASURED: four numeric subjects fold to median
    25; adding ONE string subject folds the same frame to mode '20'. The
    aggregation is chosen from the data, which is what lets one pipeline serve a
    heart-rate frame and a strategic-direction frame, and it also means one
    mistyped row silently changes the rule for everyone in a 400-subject frame
    -- while `kind`, a declared discriminant, sits unread on every event.

    Changing that changes the RULE, so it changes RULE_VERSION, so it is not a
    bug fix to slip in under one. This test states the current behaviour
    exactly, so whoever decides to change it has to change a test that says
    what they are giving up."""
    numeric = [contribution_event(s, v) for s, v in
               (("a", 10), ("b", 20), ("c", 30), ("d", 40))]
    assert consensus.fold_window(numeric, "f1")["value"] == 25
    assert consensus.fold_window(numeric, "f1")["value_kind"] == "numeric"
    mixed = numeric + [contribution_event("e", "20")]
    folded = consensus.fold_window(mixed, "f1")
    assert folded["value"] == "20"
    assert folded["value_kind"] == "categorical"
