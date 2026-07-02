from mindhive import events, incidents, paths


def _add_failure(sig, action, session):
    events.append_event("tool_failed",
                        {"text": f"err {sig}", "signature": sig, "action": action},
                        session_id=session, tags=["auto", "failure"], project="test")


def test_single_occurrence_is_candidate(tmp_path):
    _add_failure("pymodule:mindhive", "python mindhive.py", "s1")
    res = incidents.project(project="test", threshold=2)
    assert res["records"] == 1
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "candidate"
    assert rec["occurrences"] == 1


def test_second_occurrence_activates(tmp_path):
    _add_failure("pymodule:mindhive", "python mindhive.py", "s1")
    _add_failure("pymodule:mindhive", "python mindhive.py state", "s2")
    incidents.project(project="test", threshold=2)
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "active"
    assert rec["occurrences"] == 2


def test_one_record_per_signature_across_sessions(tmp_path):
    for i in range(5):
        _add_failure("pymodule:tools", "x", f"s{i}")
    res = incidents.project(project="test", threshold=2)
    assert res["records"] == 1
    incident_dir = paths.project_dir("test") / "rules" / "incidents"
    assert len(list(incident_dir.glob("*.md"))) == 1


def test_record_file_has_required_sections(tmp_path):
    _add_failure("pscmd:head", "head foo.txt", "s1")
    incidents.project(project="test", threshold=2)
    incident_dir = paths.project_dir("test") / "rules" / "incidents"
    body = list(incident_dir.glob("*.md"))[0].read_text(encoding="utf-8")
    for section in ("## Raw Signal", "## Auto Evaluation",
                    "## Human Evaluation", "## Active Prevention Rule"):
        assert section in body


import json
from mindhive import failures


def test_end_to_end_two_sessions_same_signature_activates(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    def line(tu, cmd, err):
        return [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tu, "name": "Bash", "input": {"command": cmd}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tu, "is_error": True, "content": ""}]},
             "toolUseResult": {"stderr": err, "interrupted": False}},
        ]
    (tdir / "s1.jsonl").write_text("\n".join(
        json.dumps(o) for o in line("a", "python mindhive.py", "No module named 'mindhive'")),
        encoding="utf-8")
    (tdir / "s2.jsonl").write_text("\n".join(
        json.dumps(o) for o in line("b", "python mindhive.py state", "No module named 'mindhive.paths'")),
        encoding="utf-8")

    failures.capture_dir(str(tdir), project="test")
    incidents.project(project="test", threshold=2)
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["occurrences"] == 2
    assert rec["status"] == "active"


def test_derive_active_prefers_human():
    active, src = incidents.derive_active(
        "- Prevention rule: use -m from repo root",
        "always cd to repo root first")
    assert active == "always cd to repo root first"
    assert src == "human"


def test_derive_active_falls_back_to_auto():
    active, src = incidents.derive_active(
        "- Prevention rule: use -m from repo root",
        "<!-- leave blank to accept Auto -->")
    assert active == "use -m from repo root"
    assert src == "auto"


def test_derive_active_none_when_empty():
    active, src = incidents.derive_active("", "<!-- blank -->")
    assert active == ""
    assert src == "none"


def test_reprojection_preserves_human_and_auto():
    _add_failure("pymodule:mindhive", "python mindhive.py", "s1")
    _add_failure("pymodule:mindhive", "python mindhive.py state", "s2")
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    txt = path.read_text(encoding="utf-8")
    txt = incidents._replace_section(txt, "Auto Evaluation",
                                     "- Prevention rule: run with -m from repo root")
    txt = incidents._replace_section(txt, "Human Evaluation",
                                     "ALWAYS activate the venv first")
    path.write_text(txt, encoding="utf-8")
    _add_failure("pymodule:mindhive", "python x", "s3")
    incidents.project(project="test", threshold=2)
    body = path.read_text(encoding="utf-8")
    assert "run with -m from repo root" in body           # Auto preserved
    assert "ALWAYS activate the venv first" in body        # Human preserved
    assert "> ALWAYS activate the venv first" in body      # Active = Human
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["rule_source"] == "human"
    assert rec["occurrences"] == 3                          # frontmatter refreshed


def test_reconcile_appends_rule_revised_for_human_edits():
    _add_failure("pymodule:mindhive", "python x", "s1")
    _add_failure("pymodule:mindhive", "python y", "s2")
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                     "Human Evaluation", "activate venv first")
    path.write_text(txt, encoding="utf-8")

    res = incidents.reconcile(project="test")
    assert res["revised"] == 1
    revs = [e for e in events.read_all("test") if e.type == "rule_revised"]
    assert revs and revs[0].payload["source"] == "human"
    assert revs[0].payload["text"] == "activate venv first"
    assert revs[0].payload["signature"] == "pymodule:mindhive"

    # idempotent: re-running with no new edit adds nothing
    assert incidents.reconcile(project="test")["revised"] == 0


def _add_failure_ts(sig, action, session, ts):
    events.append_event("tool_failed",
                        {"text": f"err {sig}", "signature": sig, "action": action},
                        session_id=session, tags=["auto", "failure"], ts=ts, project="test")


def test_audit_stamps_activation_at_threshold_crossing():
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")
    res = incidents.audit(project="test", threshold=2)
    assert res["activated"] == 1
    acts = [e for e in events.read_all("test") if e.type == "rule_activated"]
    assert len(acts) == 1
    assert acts[0].payload["signature"] == "pymodule:mindhive"
    assert acts[0].ts == "2026-07-01T11:00:00Z"          # the 2nd (crossing) failure
    assert incidents.audit(project="test", threshold=2)["activated"] == 0


def test_audit_ignores_below_threshold():
    _add_failure_ts("pymodule:tools", "x", "s1", "2026-07-01T10:00:00Z")
    assert incidents.audit(project="test", threshold=2)["activated"] == 0


def test_leaks_counts_failures_after_activation():
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")   # activation
    incidents.audit(project="test", threshold=2)
    assert incidents.leaks(project="test", signature="pymodule:mindhive") == 0
    _add_failure_ts("pymodule:mindhive", "z", "s3", "2026-07-01T12:00:00Z")   # leak 1
    _add_failure_ts("pymodule:mindhive", "w", "s4", "2026-07-01T13:00:00Z")   # leak 2
    assert incidents.leaks(project="test", signature="pymodule:mindhive") == 2


def test_auto_rule_demoted_after_two_leaks():
    for i, ts in enumerate(["10", "11", "12", "13"]):
        _add_failure_ts("pymodule:mindhive", "x", f"s{i}", f"2026-07-01T{ts}:00:00Z")
    incidents.audit(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    incidents.project(project="test", threshold=2)
    txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                     "Auto Evaluation", "- Prevention rule: do X")
    path.write_text(txt, encoding="utf-8")
    incidents.project(project="test", threshold=2)
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "demoted"           # auto + 2 leaks


def test_human_rule_flagged_not_demoted_on_leak():
    for i, ts in enumerate(["10", "11", "12", "13"]):
        _add_failure_ts("pymodule:mindhive", "x", f"s{i}", f"2026-07-01T{ts}:00:00Z")
    incidents.audit(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    incidents.project(project="test", threshold=2)
    txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                     "Human Evaluation", "always cd first")
    path.write_text(txt, encoding="utf-8")
    incidents.project(project="test", threshold=2)
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "active"            # human never auto-demoted
    assert rec["flag"] == "needs_review"


def test_inject_lists_only_active_rules_with_text():
    # active with an auto rule -> included
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")
    # candidate (1 occurrence) -> excluded
    _add_failure_ts("pymodule:tools", "z", "s3", "2026-07-01T10:00:00Z")
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", "- Prevention rule: run with -m"), encoding="utf-8")
    incidents.project(project="test", threshold=2)

    block = incidents.inject(project="test")
    assert "PREVENTION RULES" in block
    assert "run with -m" in block                 # active + has rule
    assert "pymodule:tools" not in block          # candidate excluded


def test_inject_empty_when_no_active_rules():
    assert incidents.inject(project="test") == ""


def test_promote_appends_to_soul_and_marks_promoted():
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", "- Prevention rule: run with -m"), encoding="utf-8")
    incidents.project(project="test", threshold=2)

    res = incidents.promote("pymodule:mindhive", project="test")
    assert res["promoted"] is True
    soul = incidents.soul_path("test").read_text(encoding="utf-8")
    assert "run with -m" in soul
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "promoted"
    # excluded from Quarantine injection once promoted
    assert "pymodule:mindhive" not in incidents.inject(project="test")


def test_promote_noop_without_active_rule():
    _add_failure_ts("pymodule:tools", "x", "s1", "2026-07-01T10:00:00Z")
    incidents.project(project="test", threshold=2)
    assert incidents.promote("pymodule:tools", project="test")["promoted"] is False


def test_manual_demote_sets_status_and_excludes_from_inject():
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", "- Prevention rule: run with -m"), encoding="utf-8")
    incidents.project(project="test", threshold=2)

    assert incidents.demote("pymodule:mindhive", project="test")["demoted"] is True
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "demoted"
    assert "pymodule:mindhive" not in incidents.inject(project="test")


def test_promote_is_idempotent():
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", "- Prevention rule: run with -m"), encoding="utf-8")
    incidents.project(project="test", threshold=2)
    assert incidents.promote("pymodule:mindhive", project="test")["promoted"] is True
    assert incidents.promote("pymodule:mindhive", project="test")["promoted"] is False
    soul = incidents.soul_path("test").read_text(encoding="utf-8")
    assert soul.count("run with -m") == 1          # not duplicated


# --- summaries(): structured lesson dicts for the Improvements view ---

_VERDICT = ("- Root cause: module run from wrong directory\n"
            "- Lesson: python -m needs the repo root on sys.path\n"
            "- Prevention rule: run with -m from the repo root\n"
            "- Category: tool_use")


def _write_auto(sig, body):
    path = (paths.project_dir("test") / "rules" / "incidents"
            / f"{incidents._safe_name(sig)}.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", body), encoding="utf-8")


def test_summaries_unreflected_record_has_empty_lesson_fields():
    _add_failure("pymodule:mindhive", "python mindhive.py", "s1")
    _add_failure("pymodule:mindhive", "python mindhive.py state", "s2")
    incidents.project(project="test", threshold=2)
    recs = incidents.summaries(project="test")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["signature"] == "pymodule:mindhive"
    assert rec["occurrences"] == 2
    assert rec["sessions"] == 2
    assert rec["excerpt"] == "err pymodule:mindhive"
    assert rec["lesson"] == "" and rec["root_cause"] == ""
    assert rec["prevention_rule"] == "" and rec["category"] == ""
    assert rec["rule_source"] == "none" and rec["active_rule"] == ""


def test_summaries_parses_reflected_verdict():
    _add_failure("pymodule:mindhive", "x", "s1")
    _add_failure("pymodule:mindhive", "y", "s2")
    incidents.project(project="test", threshold=2)
    _write_auto("pymodule:mindhive", _VERDICT)
    rec = incidents.summaries(project="test")[0]
    assert rec["root_cause"] == "module run from wrong directory"
    assert rec["lesson"] == "python -m needs the repo root on sys.path"
    assert rec["prevention_rule"] == "run with -m from the repo root"
    assert rec["category"] == "tool_use"
    assert rec["rule_source"] == "auto"
    assert rec["active_rule"] == "run with -m from the repo root"


def test_summaries_human_override_wins():
    _add_failure("pymodule:mindhive", "x", "s1")
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents"
            / "pymodule-mindhive.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Human Evaluation", "always cd to repo root"), encoding="utf-8")
    rec = incidents.summaries(project="test")[0]
    assert rec["rule_source"] == "human"
    assert rec["active_rule"] == "always cd to repo root"


def test_summaries_empty_when_no_incident_dir():
    assert incidents.summaries(project="fresh-nothing") == []


def test_summaries_skips_malformed_file_without_raising():
    _add_failure("pymodule:mindhive", "x", "s1")
    incidents.project(project="test", threshold=2)
    d = paths.project_dir("test") / "rules" / "incidents"
    (d / "junk.md").write_text("no frontmatter here\n", encoding="utf-8")
    recs = incidents.summaries(project="test")
    assert [r["signature"] for r in recs] == ["pymodule:mindhive"]


def test_summaries_multiline_verdict_keeps_first_line():
    _add_failure("pymodule:mindhive", "x", "s1")
    incidents.project(project="test", threshold=2)
    _write_auto("pymodule:mindhive",
                "- Lesson: first line\nsecond line of the lesson\n"
                "- Prevention rule: one liner")
    rec = incidents.summaries(project="test")[0]
    assert rec["lesson"] == "first line"
    assert rec["prevention_rule"] == "one liner"
