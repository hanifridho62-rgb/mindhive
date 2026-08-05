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
    kw = {"lens": "test-lens", "title": "do the thing", "fingerprint": fp,
          "evidence": ("code_change @ ts",), "score": 1.0}
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
    cfg = replenish.ReplenishConfig(min_score=0.9)
    kept, refused = replenish.gate([_cand(score=0.1)], frozenset(), cfg)
    assert kept == []
    assert "below the round's minimum" in refused[0][1]


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
    src = (replenish.__file__)
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert "import roadmap" not in body
    assert "from .roadmap" not in body
