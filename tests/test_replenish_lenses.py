# tests/test_replenish_lenses.py
"""The evidence-gap lens: code changed, nothing verified it since.

The discriminating question throughout is what counts as a PASS. `verify.py` is
explicit that the mechanical stamp attaches to the exit code, which was observed,
and not to the counts, which are a best-effort parse recorded absent rather than
zero when they cannot be read. A lens that judged on counts would call an
unparsed green run a failure and re-propose work that was already done.
"""
from pathlib import Path

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
    assert (_gap()[0].fingerprint
            == replenish.generate_fingerprint("evidence-gap",
                                              ("2026-08-02T00:00:00Z",)))


def test_the_fingerprint_changes_after_a_newer_code_change():
    _code_change("2026-08-02T00:00:00Z")
    first = _gap()[0].fingerprint
    _code_change("2026-08-04T00:00:00Z")
    assert _gap()[0].fingerprint != first


def test_the_lens_is_registered():
    assert replenish.LENSES["evidence-gap"] is replenish.evidence_gap


def test_evidence_gap_asserts_severity_and_carries_the_change_count():
    # Unverified code IS the blocking condition (SOP change-audit §3), so the
    # severity term is asserted by the lens rather than inferred by the scorer.
    _code_change("2026-08-02T00:00:00Z")
    _code_change("2026-08-03T00:00:00Z")
    cand = _gap()[0]
    assert cand.severity is True
    assert cand.occurrences == 2


def test_evidence_gap_clears_the_balanced_threshold():
    # The regression the integer-scale migration could have caused: the shipped
    # lens used to carry score 1.0, which is below every threshold on the new
    # scale, so it would have been refused everywhere in silence.
    _code_change("2026-08-02T00:00:00Z")
    breakdown = replenish.score_candidate(_gap()[0])
    assert breakdown.total >= replenish.PROFILES["balanced"].min_score


# --- the lens interface -------------------------------------------------------

def test_a_lens_accepts_a_bare_event_list_or_a_context():
    # Normalising once in `_as_context` is why no lens below has a special case.
    # Both call shapes must land on the same answer.
    _code_change("2026-08-02T00:00:00Z")
    evs = events.read_all()
    assert (replenish.evidence_gap(evs)[0].fingerprint
            == replenish.evidence_gap(replenish.LensContext(evs=tuple(evs)))[0]
            .fingerprint)


# --- the repair lens ----------------------------------------------------------
# A prevention rule stops the agent repeating a mistake; it does not remove what
# made the mistake possible. This lens proposes that, and only where no rule
# already covers the signature.

def _failure(ts, signature="", text="boom"):
    payload = {"text": text, "action": "do the thing"}
    if signature:
        payload["signature"] = signature
    return events.append_event("tool_failed", payload, ts=ts)


def _no_rules(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda _p=None: [])


def _repair():
    return replenish.repair(events.read_all())


def test_a_signature_with_no_rule_yields_one_candidate(monkeypatch):
    _no_rules(monkeypatch)
    _failure("2026-08-02T00:00:00Z", signature="edit:not-read")
    cands = _repair()
    assert len(cands) == 1
    assert "edit:not-read" in cands[0].title
    assert cands[0].lens == "repair"


def test_a_signature_with_an_active_rule_yields_nothing(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda _p=None: [
        {"signature": "edit:not-read",
         "active_rule": "Read the exact path before editing it."}])
    _failure("2026-08-02T00:00:00Z", signature="edit:not-read")
    assert _repair() == []


def test_a_record_with_no_active_rule_does_not_suppress(monkeypatch):
    # An incident record exists but the reflection pass has not produced a rule.
    # The cause is still unaddressed, so the work is still outstanding.
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda _p=None: [
        {"signature": "edit:not-read", "active_rule": ""}])
    _failure("2026-08-02T00:00:00Z", signature="edit:not-read")
    assert len(_repair()) == 1


def test_four_failures_of_one_signature_yield_one_candidate(monkeypatch):
    _no_rules(monkeypatch)
    for day in range(2, 6):
        _failure(f"2026-08-0{day}T00:00:00Z", signature="edit:not-read")
    cands = _repair()
    assert len(cands) == 1
    assert cands[0].occurrences == 4
    assert sum("failed @" in e for e in cands[0].evidence) == 4


def test_recurrence_makes_a_repeated_signature_outscore_a_lone_one(monkeypatch):
    _no_rules(monkeypatch)
    for day in range(2, 6):
        _failure(f"2026-08-0{day}T00:00:00Z", signature="repeated")
    _failure("2026-08-07T00:00:00Z", signature="once")
    # Keyed on `deferral_key`, which for this lens IS the bare signature. The
    # fingerprint is a digest and carries no readable parts to split on.
    scores = {c.deferral_key: replenish.score_candidate(c).total
              for c in _repair()}
    assert scores["repeated"] == 25
    assert scores["once"] == 10


def test_two_distinct_signatures_yield_two_candidates(monkeypatch):
    _no_rules(monkeypatch)
    _failure("2026-08-02T00:00:00Z", signature="sig-a")
    _failure("2026-08-03T00:00:00Z", signature="sig-b")
    assert len(_repair()) == 2


def test_an_unsignatured_failure_is_canonicalised_from_its_text(monkeypatch):
    # SOP tool-mcp-integration §1: 260 historical rows carry no parseable
    # fields, so deriving the signature is the common path for history.
    _no_rules(monkeypatch)
    _failure("2026-08-02T00:00:00Z", text="ModuleNotFoundError: No module named 'tools'")
    cands = _repair()
    assert len(cands) == 1
    # A signature was derived at all — that is what canonicalisation means here,
    # and it is what the fingerprint is keyed on.
    assert cands[0].deferral_key
    assert len(cands[0].fingerprint) == 64        # a SHA-256 digest, not a key


def test_a_failure_with_neither_signature_nor_text_is_skipped(monkeypatch):
    _no_rules(monkeypatch)
    events.append_event("tool_failed", {"action": "nothing"},
                        ts="2026-08-02T00:00:00Z")
    assert _repair() == []


def test_a_malformed_failure_row_does_not_break_the_repair_lens(monkeypatch):
    _no_rules(monkeypatch)
    events.append_event("tool_failed", ["not", "a", "dict"],
                        ts="2026-08-01T00:00:00Z")
    _failure("2026-08-02T00:00:00Z", signature="sig-a")
    assert len(_repair()) == 1


def test_the_repair_fingerprint_tracks_the_newest_failure(monkeypatch):
    # A signature that recurs after being fixed is a NEW instance and must be
    # proposable again; keying on the bare signature would dedup it away forever.
    _no_rules(monkeypatch)
    _failure("2026-08-02T00:00:00Z", signature="sig-a")
    first = _repair()[0].fingerprint
    _failure("2026-08-09T00:00:00Z", signature="sig-a")
    assert _repair()[0].fingerprint != first


def test_the_repair_candidate_carries_the_signature_as_its_deferral_key(monkeypatch):
    _no_rules(monkeypatch)
    _failure("2026-08-02T00:00:00Z", signature="sig-a")
    assert _repair()[0].deferral_key == "sig-a"


def test_an_unreadable_incident_store_still_proposes(monkeypatch):
    # Failing open toward proposing: hiding outstanding work is worse than
    # proposing something already covered, which the dedup will refuse anyway.
    from mindhive import incidents

    def boom(_p=None):
        raise OSError("gone")

    monkeypatch.setattr(incidents, "summaries", boom)
    _failure("2026-08-02T00:00:00Z", signature="sig-a")
    assert len(_repair()) == 1


def test_repair_on_an_empty_ledger_does_not_raise(monkeypatch):
    _no_rules(monkeypatch)
    assert replenish.repair([]) == []


def test_the_repair_lens_is_registered():
    assert replenish.LENSES["repair"] is replenish.repair


# --- the caveat lens ----------------------------------------------------------
# Pinned is the discriminating tag (SOP memo-hygiene §4): a standing
# safety-critical fact, not a passing observation.

def _caveat(ts, text, tags=("pinned",)):
    return events.append_event("caveat", {"text": text}, ts=ts, tags=list(tags))


def _caveats():
    return replenish.caveat(events.read_all())


def test_a_pinned_caveat_with_nothing_addressing_it_yields_a_candidate():
    _caveat("2026-08-02T00:00:00Z", "src/mindhive/oversee.py folds untrusted rows")
    cands = _caveats()
    assert len(cands) == 1
    assert "Resolve pinned architectural caveat" in cands[0].title
    assert "src/mindhive/oversee.py" in cands[0].title


def test_a_pinned_caveat_a_later_code_change_addressed_yields_nothing():
    _caveat("2026-08-02T00:00:00Z", "src/mindhive/oversee.py folds untrusted rows")
    _code_change("2026-08-03T00:00:00Z", files=["src/mindhive/oversee.py"])
    assert _caveats() == []


def test_a_code_change_to_a_different_file_does_not_address_it():
    _caveat("2026-08-02T00:00:00Z", "src/mindhive/oversee.py folds untrusted rows")
    _code_change("2026-08-03T00:00:00Z", files=["src/mindhive/graph.py"])
    assert len(_caveats()) == 1


def test_an_experiment_result_naming_the_path_addresses_it():
    _caveat("2026-08-02T00:00:00Z", "src/mindhive/oversee.py folds untrusted rows")
    events.append_event("experiment_result",
                        {"text": "hardened src/mindhive/oversee.py, fold is total"},
                        ts="2026-08-03T00:00:00Z")
    assert _caveats() == []


def test_an_unpinned_caveat_is_ignored():
    _caveat("2026-08-02T00:00:00Z", "src/mindhive/oversee.py is fragile",
            tags=("memo",))
    assert _caveats() == []


def test_a_caveat_with_no_text_yields_no_candidate():
    events.append_event("caveat", {"text": "   "}, ts="2026-08-02T00:00:00Z",
                        tags=["pinned"])
    assert _caveats() == []


def test_a_caveat_naming_no_path_is_still_outstanding_but_has_no_deferral_key():
    _caveat("2026-08-02T00:00:00Z", "the loop breaker must never fire on success")
    cands = _caveats()
    assert len(cands) == 1
    assert cands[0].deferral_key == ""


def test_the_caveat_title_takes_only_the_headline_line():
    _caveat("2026-08-02T00:00:00Z", "headline fact\nsecond line of detail")
    assert "second line" not in _caveats()[0].title


def test_a_malformed_caveat_row_does_not_break_the_lens():
    events.append_event("caveat", "not a dict", ts="2026-08-01T00:00:00Z",
                        tags=["pinned"])
    _caveat("2026-08-02T00:00:00Z", "src/mindhive/x.py needs work")
    assert len(_caveats()) == 1


def test_the_caveat_fingerprint_is_stable_for_one_caveat():
    _caveat("2026-08-02T00:00:00Z", "src/mindhive/x.py needs work")
    assert (_caveats()[0].fingerprint
            == replenish.generate_fingerprint("caveat",
                                              ("2026-08-02T00:00:00Z",)))


def test_a_pinned_caveat_can_actually_land():
    # The dead-lens check. This lens runs under `balanced` (25) and `ambitious`
    # (15); scoring a flat base 10 it could never have landed at either, and the
    # only symptom would have been pinned caveats silently never proposed.
    # Severity is read off the `pinned` tag, which SOP memo-hygiene §4 reserves
    # for safety-critical caveats.
    _caveat("2026-08-02T00:00:00Z", "src/mindhive/x.py needs work")
    cand = _caveats()[0]
    assert cand.severity is True
    assert (replenish.score_candidate(cand).total
            >= replenish.PROFILES["balanced"].min_score)


def test_caveat_on_an_empty_ledger_does_not_raise():
    assert replenish.caveat([]) == []


def test_the_caveat_lens_is_registered():
    assert replenish.LENSES["caveat"] is replenish.caveat


# --- the surface lenses -------------------------------------------------------
# These read a tree, so every test builds its own. A lens that resolved its own
# root could only be tested against the real repo — which has real oversized
# modules, so the assertions would change the day someone refactored one.

def _trees(tmp_path, modules=(), test_files=()):
    """A throwaway checkout. `modules` is (name, line_count); `test_files` is
    (name, text)."""
    src_root = tmp_path / "src" / "mindhive"
    tests_root = tmp_path / "tests"
    src_root.mkdir(parents=True)
    tests_root.mkdir(parents=True)
    for name, lines in modules:
        (src_root / name).write_text("# line\n" * lines, encoding="utf-8")
    for name, text in test_files:
        (tests_root / name).write_text(text, encoding="utf-8")
    return replenish.LensContext(src_root=str(src_root),
                                 tests_root=str(tests_root),
                                 repo_root=str(tmp_path))


# --- technical debt ---

def test_a_module_past_the_threshold_is_flagged(tmp_path):
    ctx = _trees(tmp_path, modules=[("huge.py", 1200)])
    cands = [c for c in replenish.technical_debt(ctx)
             if c.kind == "lines"]
    assert len(cands) == 1
    assert "huge.py" in cands[0].title
    assert any("1200 lines" in e for e in cands[0].evidence)


def test_a_module_under_the_threshold_is_not_flagged(tmp_path):
    ctx = _trees(tmp_path, modules=[("small.py", 400)])
    assert [c for c in replenish.technical_debt(ctx)
            if c.kind == "lines"] == []


def test_the_threshold_is_exclusive_at_exactly_the_limit(tmp_path):
    # 1000 lines is AT the threshold, not past it. An off-by-one here would flag
    # every file the review skill considers acceptable.
    ctx = _trees(tmp_path, modules=[("exact.py", replenish.LINE_THRESHOLD)])
    assert [c for c in replenish.technical_debt(ctx)
            if c.kind == "lines"] == []
    ctx = _trees(tmp_path / "b", modules=[("over.py", replenish.LINE_THRESHOLD + 1)])
    assert len([c for c in replenish.technical_debt(ctx)
                if c.kind == "lines"]) == 1


def test_the_debt_fingerprint_is_bucketed_so_one_added_line_does_not_re_propose(tmp_path):
    first = replenish.technical_debt(_trees(tmp_path, modules=[("huge.py", 1200)]))
    second = replenish.technical_debt(
        _trees(tmp_path / "b", modules=[("huge.py", 1201)]))
    assert first[0].fingerprint == second[0].fingerprint


def test_a_hundred_more_lines_does_mint_a_new_debt_instance(tmp_path):
    first = replenish.technical_debt(_trees(tmp_path, modules=[("huge.py", 1200)]))
    second = replenish.technical_debt(
        _trees(tmp_path / "b", modules=[("huge.py", 1350)]))
    assert first[0].fingerprint != second[0].fingerprint


def test_dunder_modules_are_not_line_flagged(tmp_path):
    # `__main__.py` IS over the threshold in this repo and is a standing caveat
    # rather than a task this lens should mint every round.
    ctx = _trees(tmp_path, modules=[("__main__.py", 3400)])
    assert replenish.technical_debt(ctx) == []


def test_a_queued_review_file_is_flagged_with_severity(tmp_path, monkeypatch):
    from mindhive import review
    monkeypatch.setattr(review, "load_pending",
                        lambda _p=None: {"files": ["src/mindhive/queued.py"]})
    ctx = _trees(tmp_path)
    cands = [c for c in replenish.technical_debt(ctx)
             if c.kind == "review"]
    assert len(cands) == 1
    assert cands[0].severity is True
    assert "Clear the thermo-nuclear review queue" in cands[0].title


def test_a_queued_review_file_outscores_an_oversized_module(tmp_path, monkeypatch):
    # A gate that is blocking right now must outrank debt that is merely
    # accruing, and that ordering is the severity term's entire job.
    from mindhive import review
    monkeypatch.setattr(review, "load_pending",
                        lambda _p=None: {"files": ["src/mindhive/queued.py"]})
    ctx = _trees(tmp_path, modules=[("huge.py", 1200)])
    by_kind = {c.kind: replenish.score_candidate(c).total
               for c in replenish.technical_debt(ctx)}
    assert by_kind["review"] > by_kind["lines"]


def test_an_absent_review_marker_yields_no_queue_candidates(tmp_path, monkeypatch):
    from mindhive import review
    monkeypatch.setattr(review, "load_pending", lambda _p=None: None)
    assert replenish.technical_debt(_trees(tmp_path)) == []


def test_a_corrupt_review_marker_does_not_raise(tmp_path, monkeypatch):
    from mindhive import review
    monkeypatch.setattr(review, "load_pending", lambda _p=None: "not a dict")
    assert replenish.technical_debt(_trees(tmp_path)) == []


def test_debt_titles_carry_repo_relative_paths(tmp_path):
    # An absolute path in a title would leak one machine's layout into the
    # ledger and the dashboard.
    cands = replenish.technical_debt(_trees(tmp_path, modules=[("huge.py", 1200)]))
    assert cands[0].title.count(str(tmp_path)) == 0
    assert "src/mindhive/huge.py" in cands[0].title


def test_technical_debt_on_a_missing_tree_does_not_raise():
    assert replenish.technical_debt(
        replenish.LensContext(src_root="/nope/does/not/exist",
                              tests_root="/nope/tests", repo_root="/nope")) == []


def test_the_tech_debt_lens_is_registered():
    assert replenish.LENSES["tech-debt"] is replenish.technical_debt


# --- audit ---

def _audit(ctx, modified):
    import mindhive.replenish as rp

    original = rp.git_modified_paths
    rp.git_modified_paths = lambda _root: list(modified)
    try:
        return rp.audit(ctx)
    finally:
        rp.git_modified_paths = original


def test_a_large_change_set_keeps_a_bounded_and_distinct_identity(tmp_path):
    # The regression the digest fixes, at the lens rather than at the helper.
    # Keyed on `"audit:" + ",".join(uncovered)`, six modified source files already
    # produced a 166-character key that `normalise` clamped to 128 — so two
    # change-sets sharing that prefix became ONE fingerprint and the second was
    # refused as already proposed, with nothing on the record to say so.
    shared = [f"src/mindhive/loop/module_number_{i}.py" for i in range(5)]
    a = _audit(_trees(tmp_path), shared + ["src/mindhive/aaa.py"])[0]
    b = _audit(_trees(tmp_path / "b"), shared + ["src/mindhive/bbb.py"])[0]
    assert len(a.fingerprint) <= replenish.MAX_FINGERPRINT_CHARS
    assert a.fingerprint != b.fingerprint
    assert (replenish.normalise(a).fingerprint
            != replenish.normalise(b).fingerprint)


def test_a_modified_source_file_with_no_change_log_is_flagged(tmp_path):
    ctx = _trees(tmp_path)
    cands = _audit(ctx, ["src/mindhive/foo.py"])
    assert len(cands) == 1
    assert "src/mindhive/foo.py" in cands[0].title
    assert cands[0].severity is True


def test_two_uncovered_files_yield_one_candidate_naming_both(tmp_path):
    # One candidate, not one per file: the outstanding fact is singular, and a
    # candidate per file would blow the roadmap step ceiling on an ordinary day.
    cands = _audit(_trees(tmp_path),
                   ["src/mindhive/foo.py", "tests/test_foo.py"])
    assert len(cands) == 1
    assert "foo.py" in cands[0].title and "test_foo.py" in cands[0].title


def test_a_file_a_code_change_already_names_is_not_flagged(tmp_path):
    _code_change("2026-08-02T00:00:00Z", files=["src/mindhive/foo.py"])
    ctx = replenish.LensContext(evs=tuple(events.read_all()),
                                src_root=str(tmp_path / "src" / "mindhive"),
                                tests_root=str(tmp_path / "tests"),
                                repo_root=str(tmp_path))
    assert _audit(ctx, ["src/mindhive/foo.py"]) == []


def test_a_worktree_absolute_logged_path_still_matches_a_relative_one(tmp_path):
    # The ledger holds worktree-absolute paths from every past checkout while git
    # reports repo-relative ones. Comparing them literally called everything
    # unlogged, which is the failure this normalisation exists to prevent.
    _code_change("2026-08-02T00:00:00Z",
                 files=["C:/Users/x/.claude/worktrees/abc/src/mindhive/foo.py"])
    ctx = replenish.LensContext(evs=tuple(events.read_all()),
                                src_root=str(tmp_path / "src" / "mindhive"),
                                tests_root=str(tmp_path / "tests"),
                                repo_root=str(tmp_path))
    assert _audit(ctx, ["src/mindhive/foo.py"]) == []


def test_an_out_of_scope_modification_is_ignored(tmp_path):
    assert _audit(_trees(tmp_path), ["README.md", "docs/plans/x.md"]) == []


def test_a_clean_working_tree_yields_nothing(tmp_path):
    assert _audit(_trees(tmp_path), []) == []


def test_a_renamed_path_is_read_as_its_new_name(tmp_path):
    # Porcelain reports "old -> new"; the new name is the one that exists to log.
    import mindhive.replenish as rp
    import subprocess

    class _Done:
        returncode = 0
        stdout = 'R  src/mindhive/old.py -> src/mindhive/new.py\n'
        stderr = ""

    original = subprocess.run
    subprocess.run = lambda *a, **k: _Done()
    try:
        assert rp.git_modified_paths(str(tmp_path)) == ["src/mindhive/new.py"]
    finally:
        subprocess.run = original


def test_a_git_failure_reaches_the_lens_error_channel(tmp_path):
    # Not an empty result: the lens must be able to tell "nothing is modified"
    # from "I could not look", and only the second belongs in tool_failed.
    import mindhive.replenish as rp

    def boom(_root):
        raise RuntimeError("git status failed (128): not a repository")

    original = rp.git_modified_paths
    rp.git_modified_paths = boom
    try:
        res = replenish.run_round([], surface="session",
                                  sink=lambda kept: ([], 0),
                                  lenses={"audit": rp.audit},
                                  ctx=_trees(tmp_path), append=False)
    finally:
        rp.git_modified_paths = original
    assert res.lens_errors and res.lens_errors[0][0] == "audit"


def test_the_audit_lens_is_registered():
    assert replenish.LENSES["audit"] is replenish.audit


# --- coverage ---

def test_a_module_with_no_test_file_is_flagged(tmp_path):
    ctx = _trees(tmp_path, modules=[("lonely.py", 50)])
    cands = replenish.coverage(ctx)
    assert len(cands) == 1
    assert "src/mindhive/lonely.py" in cands[0].title


def _coverage_with_ledger(tmp_path, modules):
    """The coverage lens over a throwaway tree AND the real event list, which
    `_trees` alone does not supply — the recurrence term reads it."""
    base = _trees(tmp_path, modules=modules)
    return replenish.coverage(replenish.LensContext(
        evs=tuple(events.read_all()), src_root=base.src_root,
        tests_root=base.tests_root, repo_root=base.repo_root))


def test_a_repeatedly_changed_untested_module_can_actually_land(tmp_path):
    # The dead-lens check. This lens runs under `balanced` (25) and `ambitious`
    # (15); scoring a flat base 10 it could never have landed at either, and the
    # only symptom would have been missing test suites silently never proposed.
    for day in range(2, 6):
        _code_change(f"2026-08-0{day}T00:00:00Z",
                     files=["src/mindhive/lonely.py"])
    cand = _coverage_with_ledger(tmp_path, [("lonely.py", 50)])[0]
    assert cand.occurrences == 4
    assert (replenish.score_candidate(cand).total
            >= replenish.PROFILES["balanced"].min_score)


def test_an_untouched_untested_module_does_not_interrupt_the_operator(tmp_path):
    # The other half of the gradient: recurrence has to MEAN something, so a
    # module the record never changed must still sit below the bar.
    cand = _coverage_with_ledger(tmp_path, [("quiet.py", 50)])[0]
    assert cand.occurrences == 1
    assert cand.severity is False
    assert (replenish.score_candidate(cand).total
            < replenish.PROFILES["balanced"].min_score)


def test_one_event_naming_a_path_twice_is_one_change_to_it(tmp_path):
    # Counted per event, not per listed file, or a sloppy change-log entry would
    # inflate the score of the module it names.
    _code_change("2026-08-02T00:00:00Z",
                 files=["src/mindhive/lonely.py", "src/mindhive/lonely.py"])
    cand = _coverage_with_ledger(tmp_path, [("lonely.py", 50)])[0]
    assert cand.occurrences == 1


def test_a_module_with_a_matching_test_file_is_covered(tmp_path):
    ctx = _trees(tmp_path, modules=[("paired.py", 50)],
                 test_files=[("test_paired.py", "def test_x(): pass\n")])
    assert replenish.coverage(ctx) == []


def test_a_module_a_differently_named_test_imports_is_covered(tmp_path):
    # The naive rule is wrong here and would emit confident nonsense:
    # dashboard_page.py is exercised from test_dashboard.py, and several modules
    # are covered by suites named for the behaviour rather than the file.
    ctx = _trees(tmp_path, modules=[("dashboard_page.py", 50)],
                 test_files=[("test_dashboard.py",
                              "from mindhive import dashboard_page\n")])
    assert replenish.coverage(ctx) == []


def test_a_dotted_attribute_reference_also_counts_as_coverage(tmp_path):
    ctx = _trees(tmp_path, modules=[("oversee.py", 50)],
                 test_files=[("test_ui.py", "assert mindhive.oversee.fold\n")])
    assert replenish.coverage(ctx) == []


def test_a_grouped_import_covers_every_name_in_it(tmp_path):
    ctx = _trees(tmp_path, modules=[("alpha.py", 10), ("beta.py", 10)],
                 test_files=[("test_both.py",
                              "from mindhive import alpha, beta\n")])
    assert replenish.coverage(ctx) == []


def test_a_test_file_in_a_subdirectory_is_still_scanned(tmp_path):
    ctx = _trees(tmp_path, modules=[("nested.py", 10)])
    sub = Path(ctx.tests_root) / "unit"
    sub.mkdir()
    (sub / "test_nested_behaviour.py").write_text(
        "from mindhive import nested\n", encoding="utf-8")
    assert replenish.coverage(ctx) == []


def test_dunder_modules_are_excluded_from_coverage(tmp_path):
    ctx = _trees(tmp_path, modules=[("__init__.py", 3), ("__main__.py", 40)])
    assert replenish.coverage(ctx) == []


def test_coverage_on_a_missing_tree_yields_nothing_and_does_not_raise():
    assert replenish.coverage(
        replenish.LensContext(src_root="/nope/src/mindhive",
                              tests_root="/nope/tests",
                              repo_root="/nope")) == []


def test_the_coverage_lens_is_registered():
    assert replenish.LENSES["coverage"] is replenish.coverage


def test_a_module_barely_over_the_threshold_does_not_interrupt_work(tmp_path):
    # Base 10 only, which is below every dial position. Deliberate: a file just
    # past the line is debt accruing, not a reason to add work to a plan.
    ctx = _trees(tmp_path, modules=[("just_over.py", 1100)])
    cand = replenish.technical_debt(ctx)[0]
    assert replenish.score_candidate(cand).total == 10
    assert (replenish.score_candidate(cand).total
            < min(p.min_score for p in replenish.PROFILES.values()))


def test_a_badly_oversized_module_does_land(tmp_path):
    # The bug this pins: without a magnitude term the line condition was DEAD —
    # no oversized module could ever have cleared any threshold.
    ctx = _trees(tmp_path, modules=[("enormous.py", 4200)])
    cand = replenish.technical_debt(ctx)[0]
    assert cand.occurrences == 4
    assert (replenish.score_candidate(cand).total
            >= replenish.PROFILES["balanced"].min_score)


def test_debt_score_rises_with_how_far_past_the_threshold_a_module_is(tmp_path):
    scores = []
    for index, lines in enumerate((1100, 2100, 3100)):
        ctx = _trees(tmp_path / f"t{index}", modules=[("m.py", lines)])
        scores.append(replenish.score_candidate(
            replenish.technical_debt(ctx)[0]).total)
    assert scores == sorted(scores) and len(set(scores)) == 3
