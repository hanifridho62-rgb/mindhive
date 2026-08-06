# tests/test_replenish_scoring.py
"""The score: its named terms, and the two that are not read off one candidate.

`ScoreBreakdown` has one invariant everything else here depends on — the total IS
the sum of the named terms. Both later terms are conceptually multiplicative
(resonance is 1.5x, decay is a fraction of the history-derived weight) and both
are stored as the addition they work out to, precisely so that invariant survives.
The moment a multiplier is applied to the total instead, a refusal stops being
explainable, which is the failure this whole subsystem keeps relearning.

Two terms cannot be computed from one candidate alone and so are not computed in
`score_candidate`:

  resonance  a property of this candidate's relationship to the OTHERS in the
             round, which one candidate cannot see
  decay      a function of the round's clock, which is an argument rather than a
             wall-clock read so that two rounds over the same ledger score
             identically
"""
import pytest

from mindhive import events, replenish

NOW = "2026-08-06T12:00:00Z"


def _cand(lens="probe", fp="fp", target="", **kwargs):
    return replenish.Candidate(lens=lens, title=f"{lens} work", fingerprint=fp,
                               evidence=("a real pointer",),
                               deferral_key=target, **kwargs)


def _sink(kept):
    return [replenish.Placement(c.fingerprint, "plan", i)
            for i, c in enumerate(kept)], 0


# --- the invariant ------------------------------------------------------------

def test_the_total_is_the_sum_of_the_named_terms():
    breakdown = replenish.ScoreBreakdown(base=10, recurrence=5, severity=20,
                                         magnitude=15, innovation=15,
                                         resonance=30, decay=-12)
    assert breakdown.total == 10 + 5 + 20 + 15 + 15 + 30 - 12


def test_a_veto_zeroes_the_total_whatever_the_terms_say():
    breakdown = replenish.ScoreBreakdown(recurrence=100, severity=20,
                                         veto_reason="the record declined it")
    assert breakdown.vetoed is True
    assert breakdown.total == 0


def test_the_total_never_goes_below_zero():
    # Decay is the one negative term, and a score under zero would compare
    # strangely against a threshold whose own floor is `BASE_SCORE`.
    assert replenish.ScoreBreakdown(base=0, decay=-500).total == 0


def test_as_terms_drops_the_zeroes_and_carries_the_total():
    terms = replenish.ScoreBreakdown(recurrence=5, magnitude=15).as_terms()
    assert terms == {"base": 10, "recurrence": 5, "magnitude": 15, "total": 30}


def test_a_candidate_with_no_new_signals_scores_exactly_what_it_used_to():
    """The regression guard for the six lenses the magnitude term did not touch.

    Adding a scoring term to a shared scale silently re-ranks everything that
    does not set it, so this pins the arithmetic those lenses still see: base
    plus recurrence plus severity, and nothing else."""
    breakdown = replenish.score_candidate(_cand(occurrences=4, severity=True))
    assert breakdown.total == (replenish.BASE_SCORE
                               + replenish.RECURRENCE_STEP * 3
                               + replenish.SEVERITY_BONUS)
    assert breakdown.magnitude == breakdown.resonance == breakdown.decay == 0


def test_occurrences_and_magnitude_are_independent_terms():
    # The bug the whole phase turns on: one field carrying two meanings. A
    # candidate may legitimately have both, and they must not be the same number.
    breakdown = replenish.score_candidate(_cand(occurrences=3, excess_steps=2))
    assert breakdown.recurrence == replenish.RECURRENCE_STEP * 2
    assert breakdown.magnitude == replenish.MAGNITUDE_STEP * 2


@pytest.mark.parametrize("junk", [None, True, -4, "3", 2.5])
def test_a_lens_returning_junk_for_a_count_degrades_rather_than_poisons(junk):
    # A lens is the one thing here not validated at a boundary.
    breakdown = replenish.score_candidate(_cand(occurrences=junk,
                                                excess_steps=junk))
    assert breakdown.total == replenish.BASE_SCORE


# --- temporal decay -----------------------------------------------------------

def test_a_condition_recorded_today_keeps_its_full_history_weight():
    breakdown = replenish.score_candidate(
        _cand(occurrences=3, severity=True,
              observed_ts="2026-08-06T09:00:00Z"), now_ts=NOW)
    assert breakdown.decay == 0


def test_a_condition_older_than_the_stale_line_keeps_half():
    breakdown = replenish.score_candidate(
        _cand(occurrences=3, severity=True,
              observed_ts="2026-06-01T00:00:00Z"), now_ts=NOW)
    history_terms = breakdown.recurrence + breakdown.severity
    assert breakdown.decay == -round(history_terms * (1 - replenish.DECAY_FLOOR))
    assert breakdown.total == replenish.BASE_SCORE + history_terms \
        + breakdown.decay


def test_decay_never_touches_the_base_or_the_magnitude():
    """Age discounts what the record PROVED, not the admission price and not a
    measurement of the tree as it stands right now."""
    breakdown = replenish.score_candidate(
        _cand(excess_steps=6, observed_ts="2020-01-01T00:00:00Z"), now_ts=NOW)
    assert breakdown.decay == 0
    assert breakdown.total == replenish.BASE_SCORE + breakdown.magnitude


def test_decay_is_monotonic_between_the_two_anchors():
    scores = [replenish.score_candidate(
        _cand(occurrences=9, observed_ts=f"2026-08-0{day}T12:00:00Z"),
        now_ts=NOW).total for day in (6, 5, 4, 3, 2, 1)]
    assert scores == sorted(scores, reverse=True)


def test_an_unreadable_timestamp_applies_no_decay():
    # Decay only ever lowers a score, so "I could not tell how old this is" must
    # not silently suppress work.
    assert replenish.score_candidate(
        _cand(occurrences=5, observed_ts="not a timestamp"),
        now_ts=NOW).decay == 0
    assert replenish.score_candidate(_cand(occurrences=5,
                                           observed_ts=NOW)).decay == 0


def test_a_naive_ledger_timestamp_is_read_as_utc_rather_than_discarded():
    # The ledger writes UTC with a `Z`, so a naive row is a legacy one.
    # Subtracting a naive from an aware datetime raises; that must not cost the
    # candidate its decay term.
    assert replenish.age_in_days("2026-08-05T12:00:00", NOW) == pytest.approx(1.0)


# --- the spike bypass ---------------------------------------------------------

def _cfg(min_score=25):
    return replenish.ReplenishConfig(min_score=min_score)


def test_a_spiking_candidate_clears_a_floor_it_would_otherwise_fail():
    kept, refused = replenish.gate([_cand(fp="a", spike=True)],
                                   frozenset(), _cfg())
    assert [c.fingerprint for c in kept] == ["a"]
    assert refused == []


def test_the_same_candidate_without_the_spike_is_refused():
    """The discriminating half. Without it this would pass against a gate that
    admitted everything."""
    _kept, refused = replenish.gate([_cand(fp="a")], frozenset(), _cfg())
    assert "below the round's minimum" in refused[0][1]


@pytest.mark.parametrize("kwargs,expected", [
    ({"evidence": ()}, "no evidence"),
    ({"veto_reason": "a rule defers this"}, "already declined"),
    ({"depth": 9}, "exceeds the maximum injection depth"),
])
def test_a_spike_bypasses_the_score_floor_and_nothing_else(kwargs, expected):
    candidate = replenish.Candidate(
        lens="probe", title="t", fingerprint="a", spike=True,
        evidence=kwargs.pop("evidence", ("a real pointer",)), **kwargs)
    _kept, refused = replenish.gate([candidate], frozenset(), _cfg())
    assert expected in refused[0][1]


def test_a_spike_does_not_bypass_a_spent_fingerprint():
    _kept, refused = replenish.gate([_cand(fp="a", spike=True)],
                                    frozenset({"a"}), _cfg())
    assert refused[0][1] == replenish.DUPLICATE_REASON


def test_a_burst_of_failures_today_is_a_spike():
    stamps = ["2026-08-06T09:00:00Z", "2026-08-06T10:00:00Z",
              "2026-08-06T11:00:00Z"]
    assert replenish.is_spiking(stamps, NOW) is True


def test_a_chronic_signature_spread_over_months_is_not_a_spike():
    stamps = ["2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z",
              "2026-07-01T00:00:00Z"]
    assert replenish.is_spiking(stamps, NOW) is False


def test_a_burst_that_happened_last_spring_is_not_a_spike_now():
    """Freshness is what stops the bypass being a back door: density alone would
    let any long-dead flurry walk past the score floor forever."""
    stamps = ["2026-03-01T09:00:00Z", "2026-03-01T10:00:00Z",
              "2026-03-01T11:00:00Z"]
    assert replenish.is_spiking(stamps, NOW) is False


def test_too_few_failures_is_not_a_spike():
    assert replenish.is_spiking(["2026-08-06T09:00:00Z"], NOW) is False


def test_the_repair_lens_marks_a_live_burst(tmp_path):
    for hour in (9, 10, 11):
        events.append_event("tool_failed",
                            {"text": "boom", "signature": "tool:flaky"},
                            ts=f"2026-08-06T{hour:02d}:00:00Z")
    ctx = replenish.LensContext(evs=tuple(events.read_all()), now_ts=NOW)
    assert replenish.repair(ctx)[0].spike is True


# --- cross-lens resonance -----------------------------------------------------

def _scored(lens, target, fp, **kwargs):
    candidate = _cand(lens=lens, fp=fp, target=target, **kwargs)
    breakdown = replenish.score_candidate(candidate)
    return replenish.replace(candidate, score=breakdown.total,
                             breakdown=breakdown)


def test_two_lenses_naming_one_target_fuse_into_a_systemic_intervention():
    fused = replenish.resonate([_scored("tech-debt", "src/a.py", "x"),
                                _scored("coverage", "src/a.py", "y")])
    assert len(fused) == 1
    assert fused[0].lens == "resonance"
    assert fused[0].kind == "systemic"
    assert fused[0].members == ("coverage", "tech-debt")
    assert "src/a.py" in fused[0].title


def test_a_fused_score_is_one_and_a_half_times_the_combined_score():
    members = [_scored("tech-debt", "src/a.py", "x", excess_steps=4),
               _scored("repair", "src/a.py", "y", occurrences=3)]
    combined = sum(m.score for m in members)
    fused = replenish.resonate(members)[0]
    assert fused.breakdown.resonance == combined // 2
    assert fused.score == combined + combined // 2


def test_a_fused_candidate_outranks_every_member_it_absorbed():
    """The point of fusing. Left separate, the per-round cap can drop two of
    three findings about one module and the plan never shows they were the same
    file."""
    members = [_scored("tech-debt", "src/a.py", "x", excess_steps=4),
               _scored("coverage", "src/a.py", "y"),
               _scored("repair", "src/a.py", "z", occurrences=2)]
    fused = replenish.resonate(list(members))[0]
    assert fused.score > max(m.score for m in members)


def test_one_lens_agreeing_with_itself_is_not_resonance():
    # `tech-debt` emits both an oversized-module and a review-queue condition; a
    # lens corroborating its own finding is that lens's business, not evidence.
    same = [_scored("tech-debt", "src/a.py", "x", kind="lines"),
            _scored("tech-debt", "src/a.py", "y", kind="review")]
    assert [c.lens for c in replenish.resonate(same)] == ["tech-debt",
                                                          "tech-debt"]


def test_candidates_naming_different_targets_do_not_fuse():
    apart = [_scored("tech-debt", "src/a.py", "x"),
             _scored("coverage", "src/b.py", "y")]
    assert [c.lens for c in replenish.resonate(apart)] == ["tech-debt",
                                                           "coverage"]


def test_a_candidate_with_no_target_never_fuses():
    loose = [_scored("evidence-gap", "", "x"), _scored("audit", "", "y")]
    assert [c.lens for c in replenish.resonate(loose)] == ["evidence-gap",
                                                           "audit"]


def test_a_vetoed_candidate_is_left_out_of_the_fusion():
    """A fused row would swallow the record's own words declining it."""
    vetoed = replenish.replace(_scored("coverage", "src/a.py", "y"),
                               veto_reason="a pinned caveat defers this")
    out = replenish.resonate([_scored("tech-debt", "src/a.py", "x"), vetoed])
    assert [c.lens for c in out] == ["tech-debt", "coverage"]


def test_a_spent_candidate_cannot_inflate_a_live_one():
    out = replenish.resonate([_scored("tech-debt", "src/a.py", "x"),
                              _scored("coverage", "src/a.py", "y")],
                             spent=frozenset({"y"}))
    assert [c.lens for c in out] == ["tech-debt", "coverage"]


def test_a_fused_candidate_keeps_the_deepest_members_depth():
    # Fusing must not launder a too-deep proposal past the depth stop by
    # averaging it with shallower ones.
    members = [_scored("tech-debt", "src/a.py", "x", depth=1),
               _scored("coverage", "src/a.py", "y", depth=7)]
    assert replenish.resonate(members)[0].depth == 7


def test_a_fused_identity_changes_when_any_member_changes():
    first = replenish.resonate([_scored("tech-debt", "src/a.py", "x"),
                                _scored("coverage", "src/a.py", "y")])[0]
    second = replenish.resonate([_scored("tech-debt", "src/a.py", "x2"),
                                 _scored("coverage", "src/a.py", "y")])[0]
    assert first.fingerprint != second.fingerprint


def test_a_fused_row_cites_the_constituent_findings():
    fused = replenish.resonate([_scored("tech-debt", "src/a.py", "x"),
                                _scored("coverage", "src/a.py", "y")])[0]
    assert any(e.startswith("[tech-debt]") for e in fused.evidence)
    assert any(e.startswith("[coverage]") for e in fused.evidence)


def test_resonance_lands_at_the_positions_that_run_all_its_members():
    # `conservative` runs neither tech-debt nor coverage, so however high a fused
    # score climbs it could not have been produced there.
    fused = replenish.resonate([_scored("tech-debt", "src/a.py", "x",
                                        excess_steps=20),
                                _scored("coverage", "src/a.py", "y",
                                        occurrences=20)])[0]
    assert "conservative" not in replenish.landing_positions(fused)
    assert "balanced" in replenish.landing_positions(fused)


# --- the audit row ------------------------------------------------------------

def test_a_landed_row_records_the_arithmetic_behind_its_score():
    def lens(_ctx):
        return [_cand(fp="a", occurrences=3, severity=True, excess_steps=2)]

    replenish.run_round([], surface="probe", sink=_sink,
                        lenses={"probe": lens}, now_ts=NOW)
    row = [e.payload for e in events.read_all()
           if e.type == "work_injected"][0]
    assert row["terms"] == {"base": 10, "recurrence": 10, "severity": 20,
                            "magnitude": 10, "total": 50}
    assert row["terms"]["total"] == row["score"]


def test_the_contract_rejects_a_non_integer_term():
    with pytest.raises(ValueError, match="whole number"):
        replenish.validate_payload(
            {"outcome": "landed", "round": 1, "surface": "s", "lens": "l",
             "title": "t", "fingerprint": "f", "evidence": ["e"],
             "terms": {"base": 10.5}})


def test_the_contract_rejects_a_terms_field_that_is_not_an_object():
    with pytest.raises(ValueError, match="must be an object"):
        replenish.validate_payload(
            {"outcome": "landed", "round": 1, "surface": "s", "lens": "l",
             "title": "t", "fingerprint": "f", "evidence": ["e"],
             "terms": ["base", 10]})


def test_a_row_without_terms_is_still_legal():
    # Every `landed` row the previous version wrote has no `terms` field, and the
    # fold has to keep reading them.
    replenish.validate_payload(
        {"outcome": "landed", "round": 1, "surface": "s", "lens": "l",
         "title": "t", "fingerprint": "f", "evidence": ["e"]})


def test_the_innovation_bonus_only_applies_on_a_healthy_record():
    proposal = _cand(lens="innovation", occurrences=2)
    assert replenish.score_candidate(proposal, healthy=False).innovation == 0
    assert (replenish.score_candidate(proposal, healthy=True).innovation
            == replenish.INNOVATION_BONUS)


def test_the_innovation_bonus_is_not_paid_to_any_other_lens():
    # The only lens-specific term in the scale, so the scoping is asserted rather
    # than assumed: a bonus that leaked would re-rank every lens at once.
    for lens in ("repair", "tech-debt", "coverage", "caveat", "audit",
                 "evidence-gap"):
        assert replenish.score_candidate(_cand(lens=lens),
                                         healthy=True).innovation == 0


def test_a_healthy_record_lets_a_proposal_outrank_middling_tech_debt():
    """§3.2.1's claim, asserted rather than described: when the system is coping,
    improvement work outranks debt that is merely accruing."""
    proposal = _cand(lens="innovation", occurrences=2)
    debt = _cand(lens="tech-debt", excess_steps=1)
    assert (replenish.score_candidate(proposal, healthy=True).total
            > replenish.score_candidate(debt, healthy=True).total)
