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
