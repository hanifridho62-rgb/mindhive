# tests/test_replenish.py
"""The `work_injected` audit row: registration and payload contract.

These rows are the injector's only persistent state — the dedup set, the round
counter, and the dry-round count are folded back out of them. A malformed row
therefore does not merely look wrong, it changes what gets proposed, so the
contract is tested at the registry dispatch (`events.validate_typed_payload`)
rather than only against the module function: registering the contract and
writing it are two separate mistakes.
"""
import pytest

from mindhive import events, replenish


def _landed(**over):
    payload = {"outcome": "landed", "round": 1, "surface": "session",
               "lens": "evidence-gap", "title": "run the suite",
               "fingerprint": "evidence-gap:2026-08-05T00:00:00Z",
               "evidence": ["code_change @ 2026-08-05T00:00:00Z"],
               "score": 30}
    payload.update(over)
    return payload


def _check(payload):
    """Through the registry, not the module — this is what the append surfaces
    actually call."""
    events.validate_typed_payload("work_injected", payload)


# --- registration ------------------------------------------------------------

def test_work_injected_is_a_registered_event_type():
    assert "work_injected" in events.VALID_TYPES
    assert "work_injected" in events.DETERMINISTIC_TYPES
    # Deterministic membership is the whole point: semantic would put a LEDGER
    # file and a recall entry on disk per refusal.
    assert "work_injected" not in events.SEMANTIC_TYPES
    assert "work_injected" not in events.MEMORY_VIEW_TYPES


def test_work_injected_round_trips_through_the_ledger():
    events.append_event("work_injected", _landed())
    rows = [e for e in events.read_all() if e.type == "work_injected"]
    assert len(rows) == 1
    assert rows[0].payload["fingerprint"] == "evidence-gap:2026-08-05T00:00:00Z"


def test_the_registry_dispatches_to_this_module():
    # A contract registered against the wrong module accepts everything.
    with pytest.raises(ValueError) as exc:
        _check({"outcome": "nonsense", "round": 1, "surface": "session"})
    assert "hive replenish run" in str(exc.value)


# --- the contract ------------------------------------------------------------

def test_valid_landed_payload_passes():
    _check(_landed())


def test_unknown_outcome_is_refused():
    with pytest.raises(ValueError, match="outcome"):
        _check(_landed(outcome="shipped"))


def test_landed_without_a_lens_is_refused():
    payload = _landed()
    del payload["lens"]
    with pytest.raises(ValueError, match="lens"):
        _check(payload)


def test_landed_without_a_fingerprint_is_refused():
    payload = _landed()
    del payload["fingerprint"]
    with pytest.raises(ValueError, match="fingerprint"):
        _check(payload)


def test_landed_with_non_list_evidence_is_refused():
    # `.get("evidence", [])` would default only on a MISSING key; a present
    # non-list is the shape that gets through a lazy check.
    with pytest.raises(ValueError, match="evidence"):
        _check(_landed(evidence="code_change @ ts"))


def test_landed_with_empty_evidence_is_refused():
    with pytest.raises(ValueError, match="evidence"):
        _check(_landed(evidence=[]))


def test_landed_with_a_blank_evidence_item_is_refused():
    with pytest.raises(ValueError, match="evidence"):
        _check(_landed(evidence=["   "]))


def test_oversized_evidence_item_is_refused():
    big = "x" * (replenish.MAX_EVIDENCE_CHARS + 1)
    with pytest.raises(ValueError, match="evidence"):
        _check(_landed(evidence=[big]))


def test_refused_row_requires_a_reason():
    payload = _landed(outcome="refused")
    payload.pop("evidence")
    with pytest.raises(ValueError, match="reason"):
        _check(payload)


def test_refused_row_with_a_reason_passes_without_evidence():
    payload = _landed(outcome="refused", reason="no evidence")
    payload.pop("evidence")
    _check(payload)


def test_truncated_row_requires_a_dropped_count():
    with pytest.raises(ValueError, match="dropped"):
        _check({"outcome": "truncated", "round": 1, "surface": "session"})


def test_truncated_row_passes_with_a_dropped_count():
    _check({"outcome": "truncated", "round": 1, "surface": "session",
            "dropped": 4})


def test_dry_row_needs_nothing_beyond_round_and_surface():
    _check({"outcome": "dry", "round": 2, "surface": "session"})


def test_round_must_be_a_positive_integer():
    with pytest.raises(ValueError, match="round"):
        _check(_landed(round=0))


def test_boolean_round_is_refused():
    # bool subclasses int: `True` would pass a bare isinstance(x, int) check and
    # make a malformed row read as round 1.
    with pytest.raises(ValueError, match="round"):
        _check(_landed(round=True))


def test_surface_is_required():
    payload = _landed()
    del payload["surface"]
    with pytest.raises(ValueError, match="surface"):
        _check(payload)


def test_non_object_payload_is_refused():
    with pytest.raises(ValueError, match="object"):
        _check(["outcome", "landed"])


def test_score_must_be_a_number_when_present():
    with pytest.raises(ValueError, match="score"):
        _check(_landed(score="high"))


def test_score_is_optional():
    payload = _landed()
    del payload["score"]
    _check(payload)


# --- the core ----------------------------------------------------------------

def _cand(fp="fp-1", **over):
    kw = {"lens": "test-lens", "title": "do the thing", "fingerprint": fp,
          "evidence": ("code_change @ ts",), "score": replenish.BASE_SCORE}
    kw.update(over)
    return replenish.Candidate(**kw)


def _sink(placed_plan="plan-1", drop=0):
    """A stub sink. Its existence is the point of the design: the core takes the
    sink as a parameter, so a full round runs with no surface present."""
    def sink(kept):
        keep = kept[:len(kept) - drop] if drop else kept
        return ([replenish.Placement(c.fingerprint, placed_plan, i)
                 for i, c in enumerate(keep)], drop)
    return sink


def _rows():
    return [e.payload for e in events.read_all() if e.type == "work_injected"]


def test_a_candidate_without_evidence_is_refused_with_a_reason():
    kept, refused = replenish.gate([_cand(evidence=())], frozenset(),
                                   replenish.ReplenishConfig())
    assert kept == []
    assert "no evidence" in refused[0][1]


def test_a_candidate_whose_evidence_is_blank_is_refused():
    # AE2: the pointer must resolve to something, not merely exist.
    kept, refused = replenish.gate([_cand(evidence=("   ",))], frozenset(),
                                   replenish.ReplenishConfig())
    assert kept == []
    assert "no evidence" in refused[0][1]


def test_a_spent_fingerprint_is_refused_as_a_duplicate():
    kept, refused = replenish.gate([_cand()], frozenset({"fp-1"}),
                                   replenish.ReplenishConfig())
    assert kept == []
    assert "already proposed" in refused[0][1]


def test_two_lenses_proposing_the_same_condition_produce_one_piece_of_work():
    kept, refused = replenish.gate([_cand(), _cand(lens="other")], frozenset(),
                                   replenish.ReplenishConfig())
    assert len(kept) == 1
    assert len(refused) == 1


def test_a_candidate_below_min_score_is_refused():
    # Integer scale: thresholds are the ambition dial's numbers, and a bare
    # base-score candidate must not clear `balanced`.
    cfg = replenish.ReplenishConfig(min_score=25)
    kept, refused = replenish.gate([_cand(score=replenish.BASE_SCORE)],
                                   frozenset(), cfg)
    assert kept == []
    assert "below the round's minimum" in refused[0][1]


def test_a_candidate_exactly_at_the_threshold_is_kept():
    cfg = replenish.ReplenishConfig(min_score=25)
    kept, _refused = replenish.gate([_cand(score=25)], frozenset(), cfg)
    assert len(kept) == 1


def test_the_bare_config_has_no_score_floor():
    # 0 is deliberate, not an oversight: the threshold belongs to the dial, and a
    # hard-coded default here would be a fourth ambition position nobody chose.
    assert replenish.ReplenishConfig().min_score == 0


def test_history_is_recovered_from_the_ledger_with_no_sidecar():
    # AE3 precursor: the injector's state must survive a process boundary
    # without a file, because a sidecar is a second source of truth.
    replenish.run_round([], surface="session", sink=_sink(),
                        lenses={"l": lambda _e: [_cand("fp-a")]})
    hist = replenish.history(events.read_all())
    assert "fp-a" in hist.fingerprints
    assert hist.last_round == 1
    assert hist.dry_rounds == 0


def test_a_refused_candidates_fingerprint_is_also_spent():
    replenish.run_round([], surface="session", sink=_sink(),
                        lenses={"l": lambda _e: [_cand("fp-b", evidence=())]})
    assert "fp-b" in replenish.history(events.read_all()).fingerprints


def test_history_skips_a_malformed_row_without_raising():
    events.append_event("work_injected", ["not", "a", "dict"])
    events.append_event("work_injected", {"outcome": "from-the-future",
                                          "round": 9, "surface": "session"})
    events.append_event("work_injected",
                        {"outcome": "landed", "round": 2, "surface": "session",
                         "lens": "l", "title": "t", "fingerprint": "fp-c",
                         "evidence": ["e"]})
    hist = replenish.history(events.read_all())
    assert hist.fingerprints == frozenset({"fp-c"})
    assert hist.last_round == 2


def test_a_cap_that_truncates_a_round_is_recorded():
    # AE5: a silent cut reads as "nothing more was found".
    cfg = replenish.ReplenishConfig(max_per_round=1)
    res = replenish.run_round(
        [], surface="session", sink=_sink(), cfg=cfg,
        lenses={"l": lambda _e: [_cand("fp-1"), _cand("fp-2")]})
    assert len(res.landed) == 1
    assert res.dropped == 1
    truncated = [r for r in _rows() if r["outcome"] == "truncated"]
    assert truncated and truncated[0]["dropped"] == 1


def test_a_truncated_candidate_stays_unspent_so_it_returns_next_round():
    cfg = replenish.ReplenishConfig(max_per_round=1)
    lenses = {"l": lambda _e: [_cand("fp-1"), _cand("fp-2")]}
    replenish.run_round([], surface="session", sink=_sink(), cfg=cfg,
                        lenses=lenses)
    hist = replenish.history(events.read_all())
    assert "fp-1" in hist.fingerprints
    assert "fp-2" not in hist.fingerprints


def test_a_candidate_the_sink_could_not_place_gets_no_row():
    res = replenish.run_round(
        [], surface="session", sink=_sink(drop=1),
        lenses={"l": lambda _e: [_cand("fp-1"), _cand("fp-2")]})
    assert len(res.landed) == 1
    assert res.dropped == 1
    assert "fp-2" not in replenish.history(events.read_all()).fingerprints


def _dry_cfg(n=2):
    """A config with the dry-round exit ON. It is OFF by default because the
    session surface runs one round per session and nothing could ever reset the
    counter; the exit belongs to the loop surface, which runs rounds back to
    back."""
    return replenish.ReplenishConfig(dry_rounds_to_exit=n)


def test_a_round_that_lands_nothing_records_a_dry_row_only_when_read():
    res = replenish.run_round([], surface="session", sink=_sink(), cfg=_dry_cfg(),
                              lenses={"l": lambda _e: []})
    assert res.dry
    assert [r for r in _rows() if r["outcome"] == "dry"]


def test_no_dry_row_is_written_when_nothing_consumes_it():
    # Default config: the exit is off, so a dry row would be an identical event
    # appended once per session forever, hashed by every `hive verify`.
    res = replenish.run_round([], surface="session", sink=_sink(),
                              lenses={"l": lambda _e: []})
    assert res.dry
    assert _rows() == []


def test_two_dry_rounds_end_replenishment_and_no_lens_runs():
    calls = []

    def lens(_evs):
        calls.append(1)
        return []

    for _ in range(2):
        replenish.run_round(events.read_all(), surface="session",
                            sink=_sink(), cfg=_dry_cfg(), lenses={"l": lens})
    assert len(calls) == 2

    res = replenish.run_round(events.read_all(), surface="session",
                              sink=_sink(), cfg=_dry_cfg(), lenses={"l": lens})
    assert not res.ran
    assert res.skipped == "dry rounds exhausted"
    assert len(calls) == 2          # the third round never invoked the lens


def test_the_dry_exit_is_off_by_default_so_quiet_sessions_keep_working():
    # The bug this guards: nothing resets the dry counter except work landing,
    # and work can only land if a round runs. With the exit on by default, two
    # quiet sessions switched the injector off permanently, project-wide.
    calls = []

    def lens(_evs):
        calls.append(1)
        return []

    for _ in range(5):
        res = replenish.run_round(events.read_all(), surface="session",
                                  sink=_sink(), lenses={"l": lens})
        assert res.ran
    assert len(calls) == 5


def test_a_quiet_loop_cannot_switch_off_the_session_surface():
    for _ in range(3):
        replenish.run_round(events.read_all(), surface="loop", sink=_sink(),
                            cfg=_dry_cfg(), lenses={"l": lambda _e: []})
    res = replenish.run_round(events.read_all(), surface="session",
                              sink=_sink(), cfg=_dry_cfg(),
                              lenses={"l": lambda _e: [_cand("fp-s")]})
    assert res.ran and len(res.landed) == 1


def test_fingerprints_are_shared_across_surfaces():
    # The dry streak is per-surface; "has this been proposed" is not. A loop and
    # a session must not each mint a step for the same outstanding fact.
    replenish.run_round(events.read_all(), surface="loop", sink=_sink(),
                        lenses={"l": lambda _e: [_cand("fp-shared")]})
    res = replenish.run_round(events.read_all(), surface="session",
                              sink=_sink(),
                              lenses={"l": lambda _e: [_cand("fp-shared")]})
    assert res.landed == []
    assert res.refused[0][1] == replenish.DUPLICATE_REASON


def test_a_duplicate_refusal_is_not_recorded_twice():
    lenses = {"l": lambda _e: [_cand("fp-dup")]}
    replenish.run_round(events.read_all(), surface="session", sink=_sink(),
                        lenses=lenses)
    before = len(_rows())
    for _ in range(3):
        replenish.run_round(events.read_all(), surface="session", sink=_sink(),
                            lenses=lenses)
    assert len(_rows()) == before      # no byte-identical row per round forever


def test_work_landing_breaks_the_dry_streak():
    replenish.run_round(events.read_all(), surface="session", sink=_sink(),
                        cfg=_dry_cfg(), lenses={"l": lambda _e: []})
    replenish.run_round(events.read_all(), surface="session", sink=_sink(),
                        cfg=_dry_cfg(), lenses={"l": lambda _e: [_cand("fp-x")]})
    assert replenish.history(events.read_all(),
                             surface="session").dry_rounds == 0


def test_a_broken_lens_does_not_cost_the_round():
    def boom(_evs):
        raise RuntimeError("lens exploded")

    res = replenish.run_round(
        [], surface="session", sink=_sink(),
        lenses={"bad": boom, "good": lambda _e: [_cand("fp-good")]})
    assert len(res.landed) == 1


def _boom(_evs):
    raise RuntimeError("lens exploded")


def test_a_broken_lens_is_recorded_under_the_signature_the_failure_loop_audits():
    replenish.run_round([], surface="session", sink=_sink(),
                        lenses={"bad": _boom})
    failures = [e.payload for e in events.read_all() if e.type == "tool_failed"]
    assert len(failures) == 1
    assert failures[0]["signature"] == "lens:bad"
    assert "lens exploded" in failures[0]["text"]


def test_a_round_where_every_lens_broke_is_distinguishable_from_a_quiet_one():
    # The false statement this prevents: an all-lenses-fail round wrote nothing
    # and the CLI reported "nothing outstanding that the record proves".
    broken = replenish.run_round([], surface="session", sink=_sink(),
                                 lenses={"bad": _boom})
    quiet = replenish.run_round([], surface="session", sink=_sink(),
                                lenses={"ok": lambda _e: []})
    assert broken.lens_errors and not quiet.lens_errors
    assert broken.dry and quiet.dry          # both found nothing, and say so


def test_reporting_a_lens_failure_never_fails_the_round(monkeypatch):
    monkeypatch.setattr(replenish.ev_mod, "append_event",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    res = replenish.run_round([], surface="session", sink=_sink(),
                              lenses={"bad": _boom})
    assert res.ran and res.lens_errors


# --- normalisation: the two-phase write window -------------------------------

def test_a_blank_evidence_item_is_stripped_rather_than_reaching_the_contract():
    # The demonstrated failure: gate() filtered blanks only to DECIDE, kept the
    # raw tuple, the sink wrote the work, and validate_payload then rejected the
    # row — leaving a step with no landed row and an unspent fingerprint.
    res = replenish.run_round(
        [], surface="session", sink=_sink(),
        lenses={"l": lambda _e: [_cand(evidence=("   ", "a real pointer"))]})
    assert len(res.landed) == 1
    row = [r for r in _rows() if r["outcome"] == "landed"][0]
    assert row["evidence"] == ["a real pointer"]


def test_every_gate_survivor_produces_a_contract_legal_row():
    long_title = "t" * (replenish.MAX_TITLE_CHARS + 50)
    kept, _refused = replenish.gate(
        [_cand(title=long_title,
               evidence=tuple(f"e{i}" for i in range(40)))],
        frozenset(), replenish.ReplenishConfig())
    payload = replenish._row("landed", 1, "session", lens=kept[0].lens,
                             title=kept[0].title,
                             fingerprint=kept[0].fingerprint,
                             evidence=list(kept[0].evidence), score=1.0)
    replenish.validate_payload(payload)          # must not raise


def test_normalise_does_not_rescue_a_candidate_with_nothing_real():
    kept, refused = replenish.gate([_cand(evidence=("  ", ""))], frozenset(),
                                   replenish.ReplenishConfig())
    assert kept == [] and "no evidence" in refused[0][1]


# --- refusal ordering ---------------------------------------------------------

def test_a_spent_fingerprint_outranks_every_other_refusal_reason():
    # The ordering bug: evidence was checked first, so an evidence-less
    # candidate never reached the duplicate branch and its identical refusal row
    # was appended every round forever.
    _kept, refused = replenish.gate([_cand(evidence=())],
                                    frozenset({"fp-1"}),
                                    replenish.ReplenishConfig())
    assert refused[0][1] == replenish.DUPLICATE_REASON


# --- the score ----------------------------------------------------------------
# The scale is what makes a threshold mean anything, so each term is asserted
# alone as well as in combination. A single test on the sum would pass with two
# terms swapped.

def test_a_lone_condition_scores_exactly_the_base():
    assert replenish.score_candidate(_cand()).total == 10


def test_recurrence_adds_five_per_repeat_beyond_the_first():
    # Four occurrences is three repeats: 10 + 15 = 25.
    assert replenish.score_candidate(_cand(occurrences=4)).total == 25


def test_severity_adds_twenty():
    assert replenish.score_candidate(_cand(severity=True)).total == 30


def test_severity_absent_adds_nothing():
    assert replenish.score_candidate(_cand(severity=False)).severity == 0


def test_the_terms_sum_and_stay_separately_readable():
    breakdown = replenish.score_candidate(_cand(occurrences=3, severity=True))
    assert (breakdown.base, breakdown.recurrence, breakdown.severity) == (10, 10, 20)
    assert breakdown.total == 40


def test_occurrences_that_are_not_a_count_degrade_to_seen_once():
    # A lens is the one input here with no boundary validation, so junk must
    # degrade rather than poison the arithmetic. bool is an int subclass; True
    # would otherwise read as 1 occurrence by accident rather than by rule.
    for bad in (None, "4", True, 0, -3):
        assert replenish.score_candidate(_cand(occurrences=bad)).recurrence == 0


def test_a_deferring_prevention_rule_vetoes_the_candidate(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda _p=None: [
        {"signature": "edit:not-read",
         "active_rule": "Deferred — this is by design, do not chase it."}])
    index = replenish.build_deferral_index([])
    breakdown = replenish.score_candidate(
        _cand(deferral_key="edit:not-read"), index)
    assert breakdown.total == 0
    assert breakdown.vetoed
    assert "prevention rule" in breakdown.veto_reason


def test_a_prevention_rule_that_does_not_defer_does_not_veto(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda _p=None: [
        {"signature": "edit:not-read",
         "active_rule": "Read the exact path before editing it."}])
    index = replenish.build_deferral_index([])
    breakdown = replenish.score_candidate(
        _cand(deferral_key="edit:not-read"), index)
    assert not breakdown.vetoed
    assert breakdown.total == 10


def test_a_pinned_caveat_that_defers_vetoes_by_substring(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda _p=None: [])
    events.append_event("caveat", {"text": "src/mindhive/__main__.py is over "
                                           "the line threshold; deferred until "
                                           "the CLI split."},
                        tags=["pinned"])
    index = replenish.build_deferral_index(events.read_all())
    breakdown = replenish.score_candidate(
        _cand(deferral_key="src/mindhive/__main__.py"), index)
    assert breakdown.vetoed
    assert "caveat" in breakdown.veto_reason


def test_an_unpinned_deferring_caveat_does_not_veto(monkeypatch):
    # Pinned is the marker for a STANDING decision; an ordinary caveat is an
    # observation, and treating it as a veto would let any passing remark
    # switch a lens off.
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda _p=None: [])
    events.append_event("caveat", {"text": "deferred for now, src/mindhive/x.py"},
                        tags=["memo"])
    index = replenish.build_deferral_index(events.read_all())
    assert not replenish.score_candidate(
        _cand(deferral_key="src/mindhive/x.py"), index).vetoed


def test_a_candidate_with_no_deferral_key_is_never_vetoed(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda _p=None: [
        {"signature": "", "active_rule": "deferred"}])
    index = replenish.build_deferral_index([])
    assert not replenish.score_candidate(_cand(deferral_key=""), index).vetoed


def test_the_deferral_index_is_total_on_malformed_input(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries",
                        lambda _p=None: ["not a dict", None, {}])
    events.append_event("caveat", {"nope": 1}, tags=["pinned"])
    index = replenish.build_deferral_index(events.read_all())   # must not raise
    assert index.rule_reasons == {}


def test_an_unreadable_incident_store_still_yields_a_usable_index(monkeypatch):
    # A round that cannot read the deferrals must still run. The cost of a
    # missing deferral is a candidate proposed again, not a broken round.
    from mindhive import incidents

    def boom(_p=None):
        raise OSError("incident dir gone")

    monkeypatch.setattr(incidents, "summaries", boom)
    index = replenish.build_deferral_index([])                  # must not raise
    assert index.rule_reasons == {} and index.caveat_texts == ()


def test_a_vetoed_candidate_is_refused_with_the_records_own_words(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda _p=None: [
        {"signature": "sig-x", "active_rule": "wontfix — accepted risk"}])
    res = replenish.run_round(
        [], surface="session", sink=_sink(),
        lenses={"l": lambda _e: [_cand(deferral_key="sig-x")]})
    assert res.landed == []
    assert "already declined" in res.refused[0][1]


def test_the_veto_holds_even_with_no_threshold_configured(monkeypatch):
    # The bug this pins: a veto zeroes the score, but the bare config has no
    # score floor — so a veto expressed only as arithmetic would let every
    # declined candidate straight through.
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda _p=None: [
        {"signature": "sig-y", "active_rule": "deferred by design"}])
    cfg = replenish.ReplenishConfig(min_score=0)
    index = replenish.build_deferral_index([])
    scored = replenish.score_candidate(_cand(deferral_key="sig-y"), index)
    candidate = _cand(deferral_key="sig-y", score=scored.total,
                      veto_reason=scored.veto_reason)
    kept, refused = replenish.gate([candidate], frozenset(), cfg)
    assert kept == [] and "already declined" in refused[0][1]


def test_the_round_overwrites_whatever_score_a_lens_set():
    # Scoring belongs to the round: a lens that scored itself would make the
    # threshold meaningless the moment two lenses disagreed about what 25 meant.
    res = replenish.run_round(
        [], surface="session", sink=_sink(),
        lenses={"l": lambda _e: [_cand(score=999)]})
    assert res.landed[0][0].score == 10


def test_normalise_carries_the_fields_it_does_not_clamp():
    # The drop this pins: rebuilding a Candidate field-by-field silently loses
    # every field added later — including the two the gate reads.
    kept, _refused = replenish.gate(
        [_cand(occurrences=4, severity=True, depth=2, deferral_key="k")],
        frozenset(), replenish.ReplenishConfig())
    assert (kept[0].occurrences, kept[0].severity) == (4, True)
    assert (kept[0].depth, kept[0].deferral_key) == (2, "k")


def test_a_candidate_deeper_than_the_max_is_refused():
    cfg = replenish.ReplenishConfig(max_depth=2)
    kept, refused = replenish.gate([_cand(depth=3)], frozenset(), cfg)
    assert kept == []
    assert "exceeds the maximum injection depth" in refused[0][1]


def test_a_candidate_at_the_max_depth_is_kept():
    cfg = replenish.ReplenishConfig(max_depth=2)
    kept, _refused = replenish.gate([_cand(depth=2)], frozenset(), cfg)
    assert len(kept) == 1


def test_the_threshold_floor_is_the_base_score():
    # The auto-tuner clamps to this. Below the base score every candidate any
    # lens emitted would clear the gate, which is the quota-filling failure the
    # gate exists to prevent.
    assert replenish.MIN_THRESHOLD == replenish.BASE_SCORE


def test_an_evidence_less_candidate_is_recorded_once_not_every_round():
    lenses = {"l": lambda _e: [_cand("fp-noev", evidence=())]}
    for _ in range(4):
        replenish.run_round(events.read_all(), surface="session", sink=_sink(),
                            lenses=lenses)
    assert len([r for r in _rows() if r["outcome"] == "refused"]) == 1


def test_a_fingerprint_less_candidate_gets_no_row_at_all():
    lenses = {"l": lambda _e: [_cand("")]}
    for _ in range(3):
        replenish.run_round(events.read_all(), surface="session", sink=_sink(),
                            lenses=lenses)
    assert _rows() == []


# --- dry means "nothing qualified", not "nothing landed" ---------------------

def test_a_capped_round_is_not_dry():
    # It was printing "N candidate(s) did not fit" and "nothing outstanding" in
    # the same breath.
    cfg = replenish.ReplenishConfig(max_per_round=1)
    res = replenish.run_round(
        [], surface="session", sink=_sink(), cfg=cfg,
        lenses={"l": lambda _e: [_cand("fp-1"), _cand("fp-2")]})
    assert res.dropped == 1
    assert not res.dry


def test_row_growth_tracks_unresolved_conditions_not_session_count():
    # The bound the performance review confirmed by reading the code. Pinned
    # here so it survives the higher-cardinality lenses phases 2 and 4 add:
    # rows must follow genuine unresolved evidence, never the number of rounds.
    for _ in range(10):
        replenish.run_round(events.read_all(), surface="session", sink=_sink(),
                            lenses={"quiet": lambda _e: []})
    assert _rows() == []                      # ten quiet rounds, zero rows

    lenses = {"l": lambda _e: [_cand("fp-standing")]}
    for _ in range(10):
        replenish.run_round(events.read_all(), surface="session", sink=_sink(),
                            lenses=lenses)
    # One standing condition, written once — not once per round.
    assert len(_rows()) == 1


def test_a_round_the_sink_could_not_place_is_not_dry():
    res = replenish.run_round(
        [], surface="session", sink=_sink(drop=1),
        lenses={"l": lambda _e: [_cand("fp-1")]})
    assert res.landed == [] and not res.dry


def test_a_disabled_round_does_nothing_at_all():
    cfg = replenish.ReplenishConfig(enabled=False)
    res = replenish.run_round([], surface="session", sink=_sink(), cfg=cfg,
                              lenses={"l": lambda _e: [_cand()]})
    assert not res.ran
    assert _rows() == []


def test_append_false_decides_without_writing():
    res = replenish.run_round([], surface="session", sink=_sink(),
                              append=False,
                              lenses={"l": lambda _e: [_cand()]})
    assert len(res.landed) == 1
    assert _rows() == []


def test_landed_row_points_at_the_work_it_produced():
    replenish.run_round([], surface="session", sink=_sink("session-42"),
                        lenses={"l": lambda _e: [_cand()]})
    landed = [r for r in _rows() if r["outcome"] == "landed"][0]
    assert landed["plan_id"] == "session-42"
    assert landed["step_index"] == 0
    assert landed["evidence"] == ["code_change @ ts"]


def test_the_core_does_not_import_a_sink():
    # The whole reason `sink` is a parameter: the loop sink lands checklist
    # items, the session sink lands roadmap steps, and the decision layer must
    # not know which surface called it.
    #
    # Asserted against the real import graph, not the source text. The first cut
    # grepped for the literals "import roadmap" and "from .roadmap", which
    # `import mindhive.roadmap as rm_mod` walks straight past while
    # reintroducing exactly the coupling this exists to prevent.
    import ast

    with open(replenish.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            imported.update(f"{base}.{a.name}".lstrip(".") for a in node.names)
            imported.add(base.lstrip("."))
    leaves = {name.rsplit(".", 1)[-1] for name in imported}
    assert "roadmap" not in leaves
    assert "replenish_sinks" not in leaves
