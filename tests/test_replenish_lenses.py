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


# --- the filesystem lenses (U4) ----------------------------------------------
# Injectable root throughout: a lens that could only be tested against the real
# repo would make every assertion depend on the current state of this checkout.

import json                                                        # noqa: E402
from mindhive import review                                        # noqa: E402


def _tree(root, files):
    """Write ``{repo-relative path: line count}`` under *root*."""
    for rel, lines in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(f"# line {i}" for i in range(lines)),
                     encoding="utf-8")
    return root


def _debt(root, **kw):
    return replenish.debt(events.read_all(), root=root, **kw)


def test_a_file_past_the_threshold_is_proposed(tmp_path):
    _tree(tmp_path, {"src/mindhive/big.py": 1600})
    cands = _debt(tmp_path)
    assert len(cands) == 1
    assert cands[0].lens == "debt"
    assert "src/mindhive/big.py" in cands[0].title
    assert any("1600 lines" in e for e in cands[0].evidence)


def test_a_file_under_the_threshold_is_not_proposed(tmp_path):
    _tree(tmp_path, {"src/mindhive/small.py": 40})
    assert _debt(tmp_path) == []


def test_a_bigger_file_outranks_a_smaller_one(tmp_path):
    _tree(tmp_path, {"src/mindhive/a.py": 1100, "src/mindhive/b.py": 3000})
    by_name = {c.title.rsplit("/", 1)[-1]: c for c in _debt(tmp_path)}
    assert by_name["b.py"].recurrence > by_name["a.py"].recurrence


def _queue(root, rel):
    review.pending_path(None).parent.mkdir(parents=True, exist_ok=True)
    review.pending_path(None).write_text(
        json.dumps({"files": [str(root / rel)], "batches": [[str(root / rel)]],
                    "batch_count": 1}), encoding="utf-8")


def test_a_queued_file_is_severe_even_when_small(tmp_path):
    _tree(tmp_path, {"src/mindhive/queued.py": 30})
    _queue(tmp_path, "src/mindhive/queued.py")
    cands = _debt(tmp_path)
    assert len(cands) == 1 and cands[0].severity is True


def test_a_file_that_is_both_oversized_and_queued_yields_one_candidate(tmp_path):
    _tree(tmp_path, {"src/mindhive/both.py": 2000})
    _queue(tmp_path, "src/mindhive/both.py")
    cands = _debt(tmp_path)
    assert len(cands) == 1
    assert cands[0].severity is True
    assert any("threshold" in e and "thermo-nuclear" in e
               for e in cands[0].evidence)


def test_growth_inside_a_bucket_keeps_the_fingerprint(tmp_path):
    _tree(tmp_path, {"src/mindhive/g.py": 1600})
    first = _debt(tmp_path)[0].fingerprint
    _tree(tmp_path, {"src/mindhive/g.py": 1700})
    assert _debt(tmp_path)[0].fingerprint == first


def test_crossing_into_a_worse_bucket_changes_the_fingerprint(tmp_path):
    _tree(tmp_path, {"src/mindhive/g.py": 1600})
    first = _debt(tmp_path)[0].fingerprint
    _tree(tmp_path, {"src/mindhive/g.py": 2600})
    assert _debt(tmp_path)[0].fingerprint != first


def test_out_of_scope_files_are_ignored(tmp_path):
    _tree(tmp_path, {"src/other/huge.py": 5000, "docs/huge.py": 5000})
    assert _debt(tmp_path) == []


def test_a_missing_tree_yields_nothing(tmp_path):
    assert _debt(tmp_path / "nope") == []


def test_the_walk_is_bounded_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(replenish, "SCAN_MAX_FILES", 2)
    _tree(tmp_path, {f"src/mindhive/m{i}.py": 1600 for i in range(5)})
    cands = _debt(tmp_path)
    # Bounded, and the truncation rides the evidence — a silent cut would read
    # as "these are the only oversized files in the tree".
    assert 0 < len(cands) <= 2
    assert any("scan stopped at 2 files" in e for e in cands[0].evidence)


def test_the_debt_lens_is_registered():
    assert replenish.LENSES["debt"] is replenish.debt


def _cov(root):
    return replenish.coverage(events.read_all(), root=root)


def test_a_module_with_no_test_counterpart_is_proposed(tmp_path):
    _tree(tmp_path, {"src/mindhive/foo.py": 20})
    cands = _cov(tmp_path)
    assert len(cands) == 1
    assert cands[0].lens == "coverage"
    assert "src/mindhive/foo.py" in cands[0].title


def test_a_module_with_a_matching_test_is_not_proposed(tmp_path):
    _tree(tmp_path, {"src/mindhive/foo.py": 20, "tests/test_foo.py": 5})
    assert _cov(tmp_path) == []


def test_a_package_module_matched_by_the_prefixed_name(tmp_path):
    _tree(tmp_path, {"src/mindhive/loop/task.py": 20,
                     "tests/test_loop_task.py": 5})
    assert _cov(tmp_path) == []


def test_a_package_module_matched_by_the_bare_name(tmp_path):
    # Both conventions are live in this repo; accepting only one would propose
    # work for modules that are in fact tested.
    _tree(tmp_path, {"src/mindhive/loop/task.py": 20, "tests/test_task.py": 5})
    assert _cov(tmp_path) == []


def test_package_markers_and_the_cli_entry_point_are_skipped(tmp_path):
    _tree(tmp_path, {"src/mindhive/__init__.py": 3,
                     "src/mindhive/__main__.py": 900})
    assert _cov(tmp_path) == []


def test_recorded_churn_raises_the_coverage_recurrence(tmp_path):
    _tree(tmp_path, {"src/mindhive/foo.py": 20})
    for ts in ("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"):
        _code_change(ts, files=["src/mindhive/foo.py"])
    assert _cov(tmp_path)[0].recurrence == 2


def test_an_untouched_module_does_not_clear_the_balanced_threshold(tmp_path):
    # A coverage proposal has to earn its slot with recorded churn; without it
    # the module is a cheaper problem than everything else in the round.
    _tree(tmp_path, {"src/mindhive/foo.py": 20})
    kept, refused = replenish.gate(_cov(tmp_path), frozenset(),
                                   replenish.ReplenishConfig())
    assert kept == [] and "below the round's minimum" in refused[0][1]


def test_tests_themselves_are_never_proposed_for_coverage(tmp_path):
    _tree(tmp_path, {"tests/test_thing.py": 20})
    assert _cov(tmp_path) == []


def test_the_coverage_lens_is_registered():
    assert replenish.LENSES["coverage"] is replenish.coverage
