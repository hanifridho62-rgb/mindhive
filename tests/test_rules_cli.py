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
