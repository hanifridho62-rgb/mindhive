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
               "score": 1.0}
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
    # `severity=True` is what makes the stub admissible on the real scale: the
    # gate computes the score (base 10, +20 for severity = 30) and the default
    # threshold is 25. A stub scoring the bare base would be refused by every
    # round here, and each test would be asserting the threshold rather than the
    # thing it names.
    kw = {"lens": "test-lens", "title": "do the thing", "fingerprint": fp,
          "evidence": ("code_change @ ts",), "severity": True}
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
    # A bare candidate scores the base (10) and the default threshold is 25.
    kept, refused = replenish.gate([_cand(severity=False)], frozenset(),
                                   replenish.ReplenishConfig())
    assert kept == []
    assert "below the round's minimum" in refused[0][1]


# --- fingerprints (U1) -------------------------------------------------------

def test_the_same_lens_and_key_hash_the_same():
    assert (replenish.fingerprint("repair", "sig-a")
            == replenish.fingerprint("repair", "sig-a"))


def test_two_lenses_with_the_same_key_do_not_collide():
    # The lens name is INSIDE the hash, not a prefix on it — otherwise `debt`
    # and `coverage` both keyed on a path would dedup each other's work.
    assert (replenish.fingerprint("debt", "src/mindhive/x.py")
            != replenish.fingerprint("coverage", "src/mindhive/x.py"))


def test_a_key_cannot_be_split_into_a_different_lens_and_key():
    # Without the length prefix these two encode to identical bytes, so two
    # unrelated conditions would share one fingerprint forever.
    assert (replenish.fingerprint("a", "bc") != replenish.fingerprint("ab", "c"))


def test_an_empty_key_raises_rather_than_hashing_nothing():
    # A digest over "" would collapse every such candidate onto one fingerprint,
    # which then reads as "already proposed" for the life of the store.
    with pytest.raises(ValueError, match="key"):
        replenish.fingerprint("repair", "")
    with pytest.raises(ValueError, match="key"):
        replenish.fingerprint("repair", "   ")


def test_an_empty_lens_raises():
    with pytest.raises(ValueError, match="lens"):
        replenish.fingerprint("", "sig-a")


def test_a_non_ascii_key_hashes_deterministically():
    # Explicit utf-8, not the platform default: the same ledger must dedup the
    # same way on Windows and on CI.
    first = replenish.fingerprint("caveat", "jangan ubah — pinned")
    assert first == replenish.fingerprint("caveat", "jangan ubah — pinned")
    assert len(first) == 64


# --- scoring (U2) ------------------------------------------------------------

def test_a_plain_candidate_scores_the_base():
    assert replenish.score_for(_cand(severity=False)) == replenish.BASE_SCORE


def test_recurrence_adds_a_step_each():
    assert (replenish.score_for(_cand(severity=False, recurrence=3))
            == replenish.BASE_SCORE + 3 * replenish.RECURRENCE_STEP)


def test_severity_adds_its_bonus_and_stacks_with_recurrence():
    assert (replenish.score_for(_cand(severity=True, recurrence=2))
            == replenish.BASE_SCORE + 2 * replenish.RECURRENCE_STEP
            + replenish.SEVERITY_BONUS)


def test_a_rule_covered_candidate_scores_zero_and_names_the_rule():
    kept, refused = replenish.gate([_cand(rule_covered=True)], frozenset(),
                                   replenish.ReplenishConfig())
    assert kept == []
    # Not the generic below-threshold text: the lens is right and the condition
    # is real, the project simply already wrote the fix down.
    assert "prevention rule" in refused[0][1]
    assert "below the round's minimum" not in refused[0][1]


def test_a_candidate_exactly_at_the_threshold_is_admitted():
    cfg = replenish.ReplenishConfig(min_score=30)
    kept, _ = replenish.gate([_cand()], frozenset(), cfg)      # severity -> 30
    assert len(kept) == 1


def test_a_candidate_one_point_below_the_threshold_is_refused():
    cfg = replenish.ReplenishConfig(min_score=31)
    kept, refused = replenish.gate([_cand()], frozenset(), cfg)
    assert kept == [] and "below the round's minimum" in refused[0][1]


def test_a_score_the_lens_set_itself_is_overwritten():
    # A lens that could assert its own number could outrank every other lens.
    kept, _ = replenish.gate([_cand(score=999.0, severity=True)], frozenset(),
                             replenish.ReplenishConfig())
    assert kept[0].score == replenish.BASE_SCORE + replenish.SEVERITY_BONUS


def test_an_absurd_recurrence_is_clamped_rather_than_unbounded():
    huge = replenish.score_for(_cand(severity=False, recurrence=10_000))
    assert huge == (replenish.BASE_SCORE
                    + replenish.MAX_RECURRENCE * replenish.RECURRENCE_STEP)
    # A negative or boolean count is a malformed lens, not a negative score.
    assert replenish.score_for(_cand(severity=False,
                                     recurrence=-5)) == replenish.BASE_SCORE
    assert replenish.score_for(_cand(severity=False,
                                     recurrence=True)) == replenish.BASE_SCORE


def test_raising_the_threshold_above_every_candidate_makes_the_round_dry():
    cfg = replenish.ReplenishConfig(min_score=10_000, dry_rounds_to_exit=2)
    res = replenish.run_round([], surface="session", sink=_sink(), cfg=cfg,
                              lenses={"l": lambda _e: [_cand()]})
    assert res.dry and res.landed == []
    assert [r for r in _rows() if r["outcome"] == "dry"]


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


# --- the ambition dial (U6) --------------------------------------------------

def test_each_level_carries_its_documented_numbers():
    assert replenish.AMBITION["conservative"]["min_score"] == 40
    assert replenish.AMBITION["balanced"]["min_score"] == 25
    assert replenish.AMBITION["ambitious"]["min_score"] == 15
    assert replenish.AMBITION["conservative"]["max_per_round"] == 3
    assert replenish.AMBITION["balanced"]["max_per_round"] == 10
    assert replenish.AMBITION["ambitious"]["max_per_round"] == 25


def test_the_defaults_are_the_balanced_level():
    # "No dial" and "the dial at its middle" must behave identically, or an
    # operator who never opens loop.toml runs a configuration that is documented
    # nowhere.
    default = replenish.ReplenishConfig()
    balanced = replenish.ReplenishConfig.from_ambition("balanced")
    assert default.min_score == balanced.min_score
    assert default.max_per_round == balanced.max_per_round


def test_conservative_runs_only_the_two_ledger_lenses():
    cfg = replenish.ReplenishConfig.from_ambition("conservative")
    assert set(cfg.lenses) == {"repair", "evidence-gap"}


def test_balanced_runs_every_deterministic_lens():
    cfg = replenish.ReplenishConfig.from_ambition("balanced")
    assert set(cfg.lenses) == {"repair", "evidence-gap", "debt", "audit",
                               "caveat", "coverage"}
    # Every name it lists is actually registered — a level naming a lens that
    # does not exist would quietly run fewer lenses than it advertises.
    assert set(cfg.lenses) <= set(replenish.LENSES)


def test_ambitious_admits_the_model_lens_before_it_is_registered():
    cfg = replenish.ReplenishConfig.from_ambition("ambitious")
    assert replenish.INNOVATION_LENS in cfg.lenses
    assert replenish.INNOVATION_LENS not in replenish.LENSES
    # A name with no registered lens must be absent, not fatal — otherwise the
    # boldest level is the only one that cannot run.
    res = replenish.run_round([], surface="session", sink=_sink(), cfg=cfg,
                              lenses={"repair": lambda _e: [_cand()]})
    assert len(res.landed) == 1 and res.lens_errors == []


def test_an_unknown_level_falls_back_to_balanced_and_says_so():
    level, warning = replenish.resolve_ambition("agressive")
    assert level == "balanced"
    assert "agressive" in warning and "balanced" in warning


def test_a_blank_level_is_unset_not_a_typo():
    assert replenish.resolve_ambition("") == ("balanced", "")
    assert replenish.resolve_ambition(None) == ("balanced", "")


def test_the_level_name_is_case_insensitive():
    assert replenish.resolve_ambition("  Ambitious ") == ("ambitious", "")


def test_the_lens_filter_selects_by_name():
    cfg = replenish.ReplenishConfig(lenses=("keep",))
    res = replenish.run_round(
        [], surface="session", sink=_sink(), cfg=cfg,
        lenses={"keep": lambda _e: [_cand("fp-keep")],
                "drop": lambda _e: [_cand("fp-drop")]})
    assert [c.fingerprint for c, _ in res.landed] == ["fp-keep"]


def test_an_empty_lens_tuple_means_every_registered_lens():
    cfg = replenish.ReplenishConfig(lenses=())
    res = replenish.run_round(
        [], surface="session", sink=_sink(), cfg=cfg,
        lenses={"a": lambda _e: [_cand("fp-a")],
                "b": lambda _e: [_cand("fp-b")]})
    assert len(res.landed) == 2


def test_disabled_skips_the_round():
    cfg = replenish.ReplenishConfig.from_ambition("balanced", enabled=False)
    res = replenish.run_round([], surface="session", sink=_sink(), cfg=cfg,
                              lenses={"l": lambda _e: [_cand()]})
    assert not res.ran and res.skipped == "disabled"


# --- run-scoped bounds (U8) --------------------------------------------------

def _run(run_id="run-a", cfg=None, n=1, tag="a", **kw):
    # `tag` varies the fingerprints between rounds. Without it the second round
    # refuses everything as a spent duplicate and the bound under test never
    # gets to speak.
    lenses = {"l": lambda _e, n=n: [_cand(f"fp-{run_id}-{tag}-{i}")
                                    for i in range(n)]}
    return replenish.run_round(events.read_all(), surface="loop",
                               sink=_sink(), cfg=cfg, lenses=lenses,
                               run_id=run_id, **kw)


def test_a_run_at_its_injection_ceiling_lands_nothing_more():
    cfg = replenish.ReplenishConfig(max_per_run=2)
    first = _run(cfg=cfg, n=2, tag="a")
    assert len(first.landed) == 2
    second = _run(cfg=cfg, n=2, tag="b")
    assert not second.ran and "max injections per run" in second.skipped


def test_the_per_run_remainder_caps_the_round_and_records_the_cut():
    cfg = replenish.ReplenishConfig(max_per_run=3)
    _run(cfg=cfg, n=2, tag="a")
    res = _run(cfg=cfg, n=2, tag="b")
    assert len(res.landed) == 1
    assert res.dropped == 1
    truncated = [r for r in _rows() if r["outcome"] == "truncated"]
    assert truncated and truncated[-1]["dropped"] == 1


def test_a_capped_round_is_not_reported_as_dry():
    # R15/R16: "the cap cut this" and "there was nothing" mean opposite things.
    cfg = replenish.ReplenishConfig(max_per_run=1)
    res = _run(cfg=cfg, n=3)
    assert res.dropped == 2 and res.dry is False


def test_a_round_ceiling_skips_the_round_and_names_the_bound():
    cfg = replenish.ReplenishConfig(max_rounds=1)
    assert _run(cfg=cfg, tag="a").ran
    second = _run(cfg=cfg, tag="b")
    assert not second.ran and "max injection rounds" in second.skipped


def test_the_bounds_are_per_run():
    cfg = replenish.ReplenishConfig(max_per_run=1, max_rounds=1)
    _run(run_id="run-a", cfg=cfg)
    # A different run starts with a clean count — a ceiling read off the
    # surface's whole history would refuse the first round of every later run.
    assert _run(run_id="run-b", cfg=cfg).ran


def test_a_round_at_the_depth_ceiling_runs_no_lens():
    seen = []

    def lens(_evs):
        seen.append(1)
        return [_cand()]

    res = replenish.run_round(
        [], surface="loop", sink=_sink(),
        cfg=replenish.ReplenishConfig(max_depth=2), lenses={"l": lens},
        depth=3)
    assert not res.ran and "depth" in res.skipped
    # Mining at a depth where nothing may land would spend a full ledger pass
    # producing candidates that are all refused.
    assert seen == []


def test_a_round_at_the_ceiling_itself_still_lands():
    res = replenish.run_round(
        [], surface="loop", sink=_sink(),
        cfg=replenish.ReplenishConfig(max_depth=2),
        lenses={"l": lambda _e: [_cand()]}, depth=2)
    assert len(res.landed) == 1


def test_the_landed_row_carries_the_run_and_the_depth():
    replenish.run_round([], surface="loop", sink=_sink(),
                        lenses={"l": lambda _e: [_cand()]},
                        run_id="run-a", depth=1)
    row = [r for r in _rows() if r["outcome"] == "landed"][0]
    assert row["run_id"] == "run-a"
    assert row["depth"] == 1


def test_depth_zero_is_omitted_so_historical_rows_stay_identical():
    replenish.run_round([], surface="loop", sink=_sink(),
                        lenses={"l": lambda _e: [_cand()]}, run_id="run-a")
    row = [r for r in _rows() if r["outcome"] == "landed"][0]
    assert "depth" not in row


def test_the_per_run_fold_ignores_rows_from_other_runs_and_junk():
    events.append_event("work_injected", ["not", "a", "dict"])
    events.append_event("work_injected",
                        {"outcome": "landed", "round": 1, "surface": "loop",
                         "lens": "l", "title": "t", "fingerprint": "fp-x",
                         "evidence": ["e"], "run_id": "other-run"})
    hist = replenish.history(events.read_all(), surface="loop",
                             run_id="run-a")
    assert hist.run_landed == 0 and hist.run_rounds == 0


def test_a_run_id_is_rejected_when_blank():
    with pytest.raises(ValueError, match="run_id"):
        _check(_landed(run_id="   "))


def test_a_negative_depth_is_rejected():
    with pytest.raises(ValueError, match="depth"):
        _check(_landed(depth=-1))


def test_a_boolean_depth_is_rejected():
    with pytest.raises(ValueError, match="depth"):
        _check(_landed(depth=True))


def test_run_id_and_depth_are_optional():
    payload = _landed()
    assert "run_id" not in payload and "depth" not in payload
    _check(payload)


def test_the_bounds_are_off_by_default_for_the_session_surface():
    # A session runs one round, so a per-run ceiling there bounds nothing.
    cfg = replenish.ReplenishConfig()
    assert cfg.max_per_run == 0 and cfg.max_rounds == 0


# --- auto-tuning the threshold (U7) ------------------------------------------
# Measured, not configured: step completion is already on the record, so the
# tuner asks the ledger rather than asking the operator.

from mindhive import replenish_sinks                               # noqa: E402

PLAN = "plan-tune"


def _plan_with(statuses):
    steps = [{"text": f"step {i}", "status": s, "new": True}
             for i, s in enumerate(statuses)]
    events.append_event("roadmap",
                        {"action": "set", "plan_id": PLAN, "prompt": "ask",
                         "title": "ask", "steps": steps},
                        ts="2026-08-01T00:00:00Z", tags=["roadmap"])


def _landed_row(i, ts="2026-08-02T00:00:00Z", session_id="s1", **over):
    payload = {"outcome": "landed", "round": 1, "surface": "session",
               "lens": "l", "title": f"step {i}", "fingerprint": f"fp-{i}",
               "evidence": ["e"], "plan_id": PLAN, "step_index": i}
    payload.update(over)
    events.append_event("work_injected", payload, ts=ts,
                        session_id=session_id, tags=["replenish", "landed"])


def _pass(ts="2026-08-03T00:00:00Z"):
    events.append_event("verification",
                        {"text": "pytest", "argv": ["pytest"], "exit_code": 0,
                         "counts": {"passed": 1}, "excerpt": "",
                         "verified_by": "python"}, ts=ts)


def _fail(ts="2026-08-03T00:00:00Z", session_id="s1"):
    events.append_event("tool_failed",
                        {"text": "boom", "signature": "x:y", "action": "z"},
                        ts=ts, session_id=session_id, tags=["failure"])


def _outs():
    evs = events.read_all()
    # The resolver is a parameter for the same reason the sink is: the core
    # must not import a surface, and its own architecture test asserts that.
    return replenish.outcomes(
        evs, step_status=replenish_sinks.roadmap_step_status(evs),
        surface="session")


def test_a_done_step_with_a_passing_run_after_it_is_green():
    _plan_with(["done"])
    _landed_row(0)
    _pass()
    assert _outs() == ["green"]


def test_a_done_step_with_no_passing_run_is_open_not_green():
    _plan_with(["done"])
    _landed_row(0)
    assert _outs() == ["open"]


def test_an_unfinished_step_followed_by_a_failure_in_the_same_session_is_bad():
    _plan_with(["pending"])
    _landed_row(0)
    _fail()
    assert _outs() == ["bad"]


def test_a_failure_in_a_different_session_does_not_mark_it_bad():
    # "Any failure anywhere after this landed" measures the day, not the work.
    _plan_with(["pending"])
    _landed_row(0, session_id="s1")
    _fail(session_id="s2")
    assert _outs() == ["open"]


def test_a_breaker_open_run_marks_its_injections_bad():
    _plan_with(["pending"])
    _landed_row(0, run_id="run-x")
    events.append_event("loop_run_end",
                        {"run_id": "run-x", "reason": "breaker_open",
                         "iterations": 3}, ts="2026-08-03T00:00:00Z")
    assert _outs() == ["bad"]


def test_a_landed_row_whose_plan_is_gone_is_open_not_green():
    _landed_row(0)                      # no roadmap set at all
    assert _outs() == ["open"]


def test_a_landed_row_with_no_step_index_is_skipped_without_raising():
    _plan_with(["done"])
    payload = {"outcome": "landed", "round": 1, "surface": "session",
               "lens": "l", "title": "t", "fingerprint": "fp-n",
               "evidence": ["e"], "plan_id": PLAN}
    events.append_event("work_injected", payload, ts="2026-08-02T00:00:00Z")
    assert _outs() == ["open"]


def test_the_outcome_fold_is_total_against_a_malformed_row():
    events.append_event("work_injected", ["not", "a", "dict"])
    _plan_with(["done"])
    _landed_row(0)
    _pass()
    assert _outs() == ["green"]


def test_three_bad_outcomes_in_a_row_raise_the_threshold():
    new, reason = replenish.tune(25, ["bad", "bad", "bad"])
    assert new == 35 and "raising" in reason


def test_five_green_outcomes_in_a_row_lower_the_threshold():
    new, reason = replenish.tune(25, ["green"] * 5)
    assert new == 20 and "lowering" in reason


def test_a_mixed_sequence_moves_nothing():
    assert replenish.tune(25, ["green", "bad", "green", "bad", "green"]) \
        == (25, "")


def test_open_outcomes_are_filtered_from_both_rules():
    assert replenish.tune(25, ["open", "open", "open"]) == (25, "")
    # Filtered, not counted against: three bads separated by opens still raise.
    new, _ = replenish.tune(25, ["bad", "open", "bad", "open", "bad"])
    assert new == 35


def test_the_threshold_stops_at_the_ceiling():
    at_top = replenish.TUNE_MAX_THRESHOLD
    assert replenish.tune(at_top, ["bad"] * 3) == (at_top, "")


def test_the_threshold_stops_at_the_floor():
    at_bottom = replenish.TUNE_MIN_THRESHOLD
    assert replenish.tune(at_bottom, ["green"] * 5) == (at_bottom, "")


def test_a_move_is_recorded_as_one_decision_event():
    _plan_with(["pending"] * 3)
    for i in range(3):
        _landed_row(i, ts=f"2026-08-0{i + 2}T00:00:00Z")
    _fail(ts="2026-08-09T00:00:00Z")
    evs = events.read_all()
    cfg, reason = replenish.autotune(
        replenish.ReplenishConfig(), evs,
        step_status=replenish_sinks.roadmap_step_status(evs),
        surface="session")
    assert cfg.min_score == 35 and reason
    rows = [e for e in events.read_all() if e.type == "decision"]
    assert len(rows) == 1
    assert "replenish threshold" in rows[0].payload["text"]
    assert len(rows[0].payload["text"]) <= 500        # memo hygiene cap


def test_no_move_records_nothing():
    evs = events.read_all()
    cfg, reason = replenish.autotune(
        replenish.ReplenishConfig(), evs,
        step_status=replenish_sinks.roadmap_step_status(evs),
        surface="session")
    assert reason == ""
    assert [e for e in events.read_all() if e.type == "decision"] == []


# --- injector-directed skills and the honest trail (U9) ----------------------
# R20: the direction is recorded before the invocation, says who issued it, and
# the rendered reason is recorded fact — never a synthesized motive, and never
# the prompt gate's wording for a call the prompt gate did not cause.

from mindhive import oversee                                       # noqa: E402

TSID = "sess-trail-01"


def _direct(source="replenish_injector", name="ce-code-review"):
    payload = {"skills": [{"name": name}]}
    if source is not None:
        payload["source"] = source
    events.append_event("skill_directed", payload, session_id=TSID,
                        ts="2026-08-02T00:00:00Z", tags=["skill"])


def _used(name="ce-code-review", ts="2026-08-02T00:01:00Z", **over):
    # `ts=None` stamps now, which is what a use that follows a REAL direction
    # looks like: `read_all` is chronological, so a fixed past timestamp would
    # sort the use ahead of a direction written by `direct_skill` and the
    # consume-once pairing would never fire.
    payload = {"name": name}
    payload.update(over)
    events.append_event("skill_used", payload, session_id=TSID,
                        ts=ts, tags=["skill"])


def _why():
    return oversee.skill_trails(evs=events.read_all())[TSID][0]["why"]


def test_an_injector_direction_renders_as_the_replenish_loop():
    _direct()
    _used()
    assert _why() == "directed by autonomous replenish loop"


def test_a_row_with_no_source_still_renders_as_the_prompt_gate():
    # Every historical row predates the field; the fold must not change for one.
    _direct(source=None)
    _used()
    assert _why() == "directed by prompt-gate"


def test_an_unknown_source_is_not_attributed_to_the_prompt_gate():
    # R20 forbids attributing an injector-driven call to the gate, and that
    # cannot hold if every unknown writer defaults to the gate's wording.
    _direct(source="some-future-writer")
    _used()
    why = _why()
    assert why == "directed"
    assert "prompt-gate" not in why
    assert "some-future-writer" not in why      # never echo the raw value


def test_invocation_args_still_outrank_any_direction():
    _direct()
    _used(args="review the parser change")
    assert "review the parser change" in _why()


def test_consumed_directions_counts_an_injector_direction_the_same():
    _direct()
    _used()
    assert oversee.consumed_directions(evs=events.read_all()) == {
        "ce-code-review": 1}


def _stats(fail=(), ok=()):
    for n in fail:
        events.append_event("tool_failed",
                            {"text": "boom", "signature": f"skill:{n}",
                             "action": "x"}, ts="2026-08-01T00:00:00Z")
    for n in ok:
        events.append_event("invoke_result", {"name": n, "text": "fine"},
                            ts="2026-08-01T00:00:00Z")


def test_a_skill_whose_last_call_failed_is_not_picked():
    _stats(fail=["ce-code-review"], ok=["ce-plan"])
    assert replenish.pick_skill(["ce-code-review", "ce-plan"]) == "ce-plan"


def test_the_more_followed_skill_wins():
    _stats(ok=["ce-plan", "ce-work"])
    # One direction for ce-work, followed.
    events.append_event("skill_directed", {"skills": [{"name": "ce-work"}]},
                        session_id=TSID, ts="2026-08-01T00:00:00Z")
    events.append_event("skill_used", {"name": "ce-work"}, session_id=TSID,
                        ts="2026-08-01T00:01:00Z")
    assert replenish.pick_skill(["ce-plan", "ce-work"]) == "ce-work"


def test_an_exact_tie_breaks_on_the_name_so_the_choice_is_reproducible():
    _stats(ok=["b-skill", "a-skill"])
    assert replenish.pick_skill(["b-skill", "a-skill"]) == "a-skill"


def test_no_viable_skill_directs_nothing_and_records_nothing():
    _stats(fail=["only-skill"])
    assert replenish.direct_skill(["only-skill"], session_id=TSID) == ""
    assert [e for e in events.read_all() if e.type == "skill_directed"] == []


def test_an_empty_name_list_directs_nothing():
    assert replenish.pick_skill([]) == ""
    assert replenish.pick_skill(["  "]) == ""


def test_the_direction_is_recorded_before_the_invocation():
    _stats(ok=["ce-plan"])
    assert replenish.direct_skill(["ce-plan"], session_id=TSID) == "ce-plan"
    row = [e for e in events.read_all() if e.type == "skill_directed"][0]
    assert row.payload["source"] == replenish.SOURCE_REPLENISH
    assert row.payload["skills"] == [{"name": "ce-plan"}]
    # Recorded first is what makes the consume-once pairing join it to the
    # skill_used that follows.
    _used(name="ce-plan", ts=None)
    assert _why() == "directed by autonomous replenish loop"


def test_append_false_picks_without_recording():
    _stats(ok=["ce-plan"])
    assert replenish.direct_skill(["ce-plan"], session_id=TSID,
                                  append=False) == "ce-plan"
    assert [e for e in events.read_all() if e.type == "skill_directed"] == []
