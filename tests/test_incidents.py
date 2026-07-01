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
