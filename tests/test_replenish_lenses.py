# tests/test_replenish_lenses.py
"""The evidence-gap lens: code changed, nothing verified it since.

The discriminating question throughout is what counts as a PASS. `verify.py` is
explicit that the mechanical stamp attaches to the exit code, which was observed,
and not to the counts, which are a best-effort parse recorded absent rather than
zero when they cannot be read. A lens that judged on counts would call an
unparsed green run a failure and re-propose work that was already done.
"""
from mindhive import events, replenish


def _code_change(ts, files=("src/mindhive/x.py",)):
    return events.append_event(
        "code_change",
        {"text": "changed something real", "category": "update",
         "files": list(files)}, ts=ts)


def _verification(ts, exit_code=0, counts=None):
    payload = {"text": "pytest", "argv": ["pytest"], "exit_code": exit_code,
               "counts": counts if counts is not None else {"passed": 10},
               "excerpt": "", "verified_by": "python"}
    return events.append_event("verification", payload, ts=ts)


def _gap():
    return replenish.evidence_gap(events.read_all())


def test_code_change_after_the_last_pass_produces_exactly_one_candidate():
    _verification("2026-08-01T00:00:00Z")
    _code_change("2026-08-02T00:00:00Z")
    cands = _gap()
    assert len(cands) == 1
    assert cands[0].lens == "evidence-gap"


def test_one_candidate_not_one_per_file():
    _verification("2026-08-01T00:00:00Z")
    for i in range(5):
        _code_change(f"2026-08-0{i + 2}T00:00:00Z", files=[f"src/m/{i}.py"])
    assert len(_gap()) == 1


def test_a_pass_after_the_newest_change_produces_nothing():
    _code_change("2026-08-02T00:00:00Z")
    _verification("2026-08-03T00:00:00Z")
    assert _gap() == []


def test_a_failing_run_does_not_count_as_a_pass():
    _code_change("2026-08-02T00:00:00Z")
    _verification("2026-08-03T00:00:00Z", exit_code=1)
    assert len(_gap()) == 1


def test_a_zero_exit_with_unparsed_counts_counts_as_a_pass():
    # verify.py records counts ABSENT rather than zero when the summary cannot
    # be parsed. "We could not tell" is not "nothing passed".
    _code_change("2026-08-02T00:00:00Z")
    _verification("2026-08-03T00:00:00Z", counts={})
    assert _gap() == []


def test_no_verification_at_all_still_produces_a_candidate():
    _code_change("2026-08-02T00:00:00Z")
    cands = _gap()
    assert len(cands) == 1
    assert any("no passing verification" in e for e in cands[0].evidence)


def test_an_empty_ledger_produces_nothing_and_does_not_raise():
    assert replenish.evidence_gap([]) == []
    assert _gap() == []


def test_a_malformed_row_does_not_break_the_lens():
    events.append_event("code_change", ["not", "a", "dict"],
                        ts="2026-08-02T00:00:00Z")
    events.append_event("verification", "nonsense", ts="2026-08-01T00:00:00Z")
    _code_change("2026-08-03T00:00:00Z")
    assert len(_gap()) == 1


def test_evidence_carries_both_timestamps_and_a_file():
    _verification("2026-08-01T00:00:00Z")
    _code_change("2026-08-02T00:00:00Z", files=["src/mindhive/replenish.py"])
    ev = _gap()[0].evidence
    assert any("2026-08-02T00:00:00Z" in e for e in ev)
    assert any("2026-08-01T00:00:00Z" in e for e in ev)
    assert any("src/mindhive/replenish.py" in e for e in ev)


def test_the_candidate_passes_the_value_gate():
    # The lens is useless if what it emits cannot survive the gate that judges
    # it — evidence non-empty, fingerprint present.
    _code_change("2026-08-02T00:00:00Z")
    kept, refused = replenish.gate(_gap(), frozenset(),
                                   replenish.ReplenishConfig())
    assert len(kept) == 1 and refused == []


def test_the_title_counts_only_changes_since_the_last_pass():
    _code_change("2026-08-01T00:00:00Z")
    _verification("2026-08-02T00:00:00Z")
    _code_change("2026-08-03T00:00:00Z")
    assert "1 code change since" in _gap()[0].title


def test_the_fingerprint_is_stable_on_an_unchanged_ledger():
    _code_change("2026-08-02T00:00:00Z")
    assert _gap()[0].fingerprint == _gap()[0].fingerprint
    # Content-addressed now (U1), but keyed on exactly what it was keyed on
    # before: the newest covered code_change timestamp.
    assert _gap()[0].fingerprint == replenish.fingerprint(
        "evidence-gap", "2026-08-02T00:00:00Z")


def test_the_lens_reports_recurrence_and_severity_rather_than_a_score():
    # The gate computes the score; the lens reports what it observed. Three
    # unverified changes is a recurrence of three, and the project's own rule
    # ("must land green") is what makes it severe.
    _verification("2026-08-01T00:00:00Z")
    _code_change("2026-08-02T00:00:00Z")
    _code_change("2026-08-03T00:00:00Z")
    _code_change("2026-08-04T00:00:00Z")
    cand = _gap()[0]
    assert cand.recurrence == 3
    assert cand.severity is True
    assert cand.rule_covered is False


def test_the_candidate_clears_the_default_threshold():
    # One unverified change scores 10 + 5 + 20 = 35 against a default of 25.
    # If this ever stops holding, the phase-1 session surface silently stops
    # proposing anything at all.
    _code_change("2026-08-02T00:00:00Z")
    kept, refused = replenish.gate(_gap(), frozenset(),
                                   replenish.ReplenishConfig())
    assert len(kept) == 1 and refused == []
    assert kept[0].score == 35


def test_the_fingerprint_changes_after_a_newer_code_change():
    _code_change("2026-08-02T00:00:00Z")
    first = _gap()[0].fingerprint
    _code_change("2026-08-04T00:00:00Z")
    assert _gap()[0].fingerprint != first


def test_the_lens_is_registered():
    assert replenish.LENSES["evidence-gap"] is replenish.evidence_gap


# --- the repair lens (U3) ----------------------------------------------------
# Recurrence, the threshold crossing, and whether a rule exists are resolved by
# `incidents.summaries` — the same fold `hive rules list` reads. The lens must
# not re-derive them, or the injector gets its own opinion of "recurring".

from mindhive import incidents, paths                              # noqa: E402


def _failure(sig, session="s1", action="do the thing"):
    events.append_event("tool_failed",
                        {"text": f"err {sig}", "signature": sig,
                         "action": action},
                        session_id=session, tags=["auto", "failure"])


def _build_incidents(threshold=2):
    incidents.project(threshold=threshold)


def _repair():
    return replenish.repair(events.read_all())


def _incident_file():
    return next(iter((paths.project_dir(None) / "rules" / "incidents")
                     .glob("*.md")))


def test_a_recurring_signature_with_no_rule_becomes_a_candidate():
    _failure("pscmd:hive", session="s1")
    _failure("pscmd:hive", session="s2")
    _build_incidents()
    cands = _repair()
    assert len(cands) == 1
    assert cands[0].lens == "repair"
    assert cands[0].recurrence == 2
    assert cands[0].rule_covered is False
    assert "pscmd:hive" in cands[0].title


def test_a_signature_below_the_threshold_yields_nothing():
    _failure("pscmd:once")
    _build_incidents(threshold=5)
    assert _repair() == []


def test_a_signature_with_a_rule_is_reported_but_refused_by_the_gate():
    # Not skipped silently: "declined" and "found nothing" are the one
    # distinction the dry-round exit reads.
    _failure("pscmd:ruled", session="s1")
    _failure("pscmd:ruled", session="s2")
    _build_incidents()
    path = _incident_file()
    path.write_text(
        incidents._replace_section(path.read_text(encoding="utf-8"),
                                   "Human Evaluation",
                                   "Prevention rule: use the venv path"),
        encoding="utf-8")
    cands = _repair()
    assert len(cands) == 1 and cands[0].rule_covered is True
    kept, refused = replenish.gate(cands, frozenset(),
                                   replenish.ReplenishConfig())
    assert kept == []
    assert "prevention rule" in refused[0][1]


def test_severity_needs_more_than_one_session():
    _failure("pscmd:solo", session="s1")
    _failure("pscmd:solo", session="s1")
    _build_incidents()
    assert _repair()[0].severity is False


def test_a_signature_across_two_sessions_is_severe():
    _failure("pscmd:spread", session="s1")
    _failure("pscmd:spread", session="s2")
    _build_incidents()
    assert _repair()[0].severity is True


def test_two_signatures_get_two_distinct_fingerprints():
    for sig in ("pscmd:a", "pscmd:b"):
        _failure(sig, session="s1")
        _failure(sig, session="s2")
    _build_incidents()
    cands = _repair()
    assert len(cands) == 2
    assert len({c.fingerprint for c in cands}) == 2


def test_repair_on_an_empty_store_yields_nothing_and_does_not_raise():
    assert replenish.repair([]) == []


def test_a_malformed_incident_record_is_skipped(monkeypatch):
    monkeypatch.setattr(
        incidents, "summaries",
        lambda project=None: [None, {"signature": ""},
                              {"signature": "ok:1", "status": "active",
                               "occurrences": 3, "sessions": 2}])
    cands = _repair()
    assert len(cands) == 1 and cands[0].recurrence == 3


def test_the_repair_lens_is_registered():
    assert replenish.LENSES["repair"] is replenish.repair


# --- the caveat lens (U3) ----------------------------------------------------
# `pinned` is the project's own marker for safety-critical: sync keeps pinned
# caveats through the MEMORY.md cap and consolidate never folds them.

def _caveat(text, pinned=True, ts="2026-08-02T00:00:00Z"):
    return events.append_event("caveat", {"text": text}, ts=ts,
                               tags=["pinned"] if pinned else ["memo"])


_CAV = ("write_text_atomic is flaky on Windows because os.replace raises "
        "PermissionError on the destination handle")


def _caveats():
    return replenish.caveat(events.read_all())


def test_a_pinned_caveat_with_nothing_closing_it_becomes_a_candidate():
    _caveat(_CAV)
    cands = _caveats()
    assert len(cands) == 1
    assert cands[0].lens == "caveat"
    assert cands[0].severity is True
    assert "write_text_atomic" in cands[0].title


def test_an_unpinned_caveat_yields_nothing():
    _caveat(_CAV, pinned=False)
    assert _caveats() == []


def test_a_later_experiment_result_that_repeats_the_caveat_closes_it():
    _caveat(_CAV)
    events.append_event(
        "experiment_result",
        {"text": "fixed: write_text_atomic no longer raises PermissionError "
                 "on the destination handle on Windows because os.replace is "
                 "now retried"},
        ts="2026-08-03T00:00:00Z")
    assert _caveats() == []


def test_an_unrelated_later_memo_does_not_close_it():
    _caveat(_CAV)
    events.append_event("code_change",
                        {"text": "renamed a dashboard pill", "category": "update",
                         "files": ["src/mindhive/dashboard_page.py"]},
                        ts="2026-08-03T00:00:00Z")
    assert len(_caveats()) == 1


def test_a_reworded_repeat_raises_recurrence_instead_of_adding_a_candidate():
    _caveat(_CAV, ts="2026-08-01T00:00:00Z")
    _caveat(_CAV, ts="2026-08-02T00:00:00Z")
    _caveat(_CAV, ts="2026-08-03T00:00:00Z")
    cands = _caveats()
    assert len(cands) == 1
    assert cands[0].recurrence == 3


def test_the_caveat_fingerprint_is_stable_across_re_appends():
    _caveat(_CAV, ts="2026-08-01T00:00:00Z")
    first = _caveats()[0].fingerprint
    _caveat(_CAV, ts="2026-08-05T00:00:00Z")
    assert _caveats()[0].fingerprint == first


def test_a_blank_caveat_is_skipped():
    _caveat("   ")
    assert _caveats() == []


def test_caveat_on_an_empty_ledger_yields_nothing():
    assert replenish.caveat([]) == []


def test_a_malformed_caveat_row_does_not_break_the_lens():
    events.append_event("caveat", ["not", "a", "dict"], tags=["pinned"],
                        ts="2026-08-01T00:00:00Z")
    _caveat(_CAV, ts="2026-08-02T00:00:00Z")
    assert len(_caveats()) == 1


def test_the_caveat_lens_is_registered():
    assert replenish.LENSES["caveat"] is replenish.caveat
