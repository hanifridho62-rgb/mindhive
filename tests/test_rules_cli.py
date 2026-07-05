from mindhive import __main__ as cli, events, incidents


def _seed(project="test"):
    for s in ("s1", "s2"):
        events.append_event("tool_failed",
                            {"text": "No module named 'mindhive'",
                             "signature": "pymodule:mindhive", "action": "python x"},
                            session_id=s, tags=["auto", "failure"], project=project)
    incidents.project(project=project, threshold=2)


def test_rules_list_and_show(tmp_path, capsys):
    _seed()
    assert cli.main(["rules", "list", "--project", "test"]) == 0
    out = capsys.readouterr().out
    assert "pymodule:mindhive" in out and "active" in out

    assert cli.main(["rules", "show", "pymodule:mindhive", "--project", "test"]) == 0
    shown = capsys.readouterr().out
    assert "## Raw Signal" in shown


def test_rules_reconcile_cli(tmp_path, capsys):
    _seed()
    from mindhive import paths
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                     "Human Evaluation", "venv first")
    path.write_text(txt, encoding="utf-8")
    assert cli.main(["rules", "reconcile", "--project", "test"]) == 0
    assert '"revised": 1' in capsys.readouterr().out


def test_rules_inject_and_promote_cli(tmp_path, capsys):
    from mindhive import paths
    _seed()                                     # 2 failures -> active pymodule:mindhive
    assert cli.main(["rules", "audit", "--project", "test"]) == 0
    capsys.readouterr()
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", "- Prevention rule: run with -m"), encoding="utf-8")
    incidents.project(project="test", threshold=2)

    assert cli.main(["rules", "inject", "--project", "test"]) == 0
    assert "run with -m" in capsys.readouterr().out

    assert cli.main(["rules", "promote", "pymodule:mindhive", "--project", "test"]) == 0
    assert '"promoted": true' in capsys.readouterr().out.lower()
    assert incidents.soul_path("test").exists()


def test_rules_summaries_human_and_json(tmp_path, capsys):
    _seed()                                      # 2 failures -> active, unreflected
    # human digest
    assert cli.main(["rules", "summaries", "--project", "test"]) == 0
    out = capsys.readouterr().out
    assert "pymodule:mindhive" in out and "active" in out
    assert "not yet reflected" in out            # no lesson yet

    # --json emits the structured list the dashboard route also returns
    import json as _json
    assert cli.main(["rules", "summaries", "--project", "test", "--json"]) == 0
    recs = _json.loads(capsys.readouterr().out)
    assert isinstance(recs, list) and len(recs) == 1
    r = recs[0]
    assert r["signature"] == "pymodule:mindhive"
    assert r["occurrences"] == 2 and r["status"] == "active"
    for field in ("root_cause", "lesson", "prevention_rule", "category", "active_rule"):
        assert field in r


def test_rules_summaries_empty(tmp_path, capsys):
    assert cli.main(["rules", "summaries", "--project", "nothing"]) == 0
    assert "(no incidents yet)" in capsys.readouterr().out

    assert cli.main(["rules", "summaries", "--project", "nothing", "--json"]) == 0
    import json as _json
    assert _json.loads(capsys.readouterr().out) == []
