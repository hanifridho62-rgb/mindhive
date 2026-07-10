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


# --- rules reflect: --limit / --cooldown-hours / --all-projects (KTD3) -------

import json as _json
from mindhive import paths

_GOOD = ('{"root_cause": "rc", "lesson": "le", '
         '"prevention_rule": "always pin the venv", "task_category": "tool_use"}')


def _seed_unreflected(n, project="test"):
    # raw:* signatures: no deterministic fallback, so a failing runner leaves
    # them unreflected — which the failing-runner CLI test depends on.
    for i in range(n):
        sig = f"raw:batch{i}"
        for s in ("s1", "s2"):
            events.append_event("tool_failed",
                                {"text": "boom", "signature": sig, "action": "x"},
                                session_id=s, tags=["auto", "failure"],
                                project=project)
    incidents.project(project=project, threshold=2)


def test_rules_reflect_cli_respects_limit(tmp_path, capsys, monkeypatch):
    from mindhive import reflect as reflect_mod
    _seed_unreflected(5)
    monkeypatch.setattr(reflect_mod, "_claude_runner",
                        lambda prompt, timeout_s=120: _GOOD)
    assert cli.main(["rules", "reflect", "--project", "test", "--limit", "3"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["reflected"] == 3 and out["attempted"] == 3


def test_rules_reflect_cli_cooldown_skips_second_run(tmp_path, capsys, monkeypatch):
    from mindhive import reflect as reflect_mod
    _seed_unreflected(2)
    calls = {"n": 0}

    def stub(prompt, timeout_s=120):
        calls["n"] += 1
        return _GOOD

    monkeypatch.setattr(reflect_mod, "_claude_runner", stub)
    assert cli.main(["rules", "reflect", "--project", "test", "--limit", "1"]) == 0
    capsys.readouterr()
    assert cli.main(["rules", "reflect", "--project", "test", "--limit", "1"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["skipped"] == "cooldown"
    assert calls["n"] == 1


def test_rules_reflect_cli_failing_runner_exits_zero(tmp_path, capsys, monkeypatch):
    from mindhive import reflect as reflect_mod
    _seed_unreflected(1)

    def boom(prompt, timeout_s=120):
        raise RuntimeError("auth expired")

    monkeypatch.setattr(reflect_mod, "_claude_runner", boom)
    assert cli.main(["rules", "reflect", "--project", "test",
                     "--cooldown-hours", "0"]) == 0     # hook-safe: exit 0
    out = _json.loads(capsys.readouterr().out)
    assert out["reflected"] == 0
    m = reflect_mod.read_marker("test")
    assert m["last_error"] == "RuntimeError"
    assert not m.get("cooldown_started")                # window not consumed


def test_rules_reflect_all_projects_skips_live_lock(tmp_path, capsys, monkeypatch):
    from mindhive import reflect as reflect_mod
    store = paths.store_home()
    # register two namespaces (empty folder -> no native MEMORY.md write)
    (store / "autosync.json").write_text(_json.dumps([
        {"project": "alpha", "transcripts": "t", "folder": ""},
        {"project": "fabio-live", "transcripts": "t", "folder": ""},
    ]), encoding="utf-8")
    _seed_unreflected(2, project="alpha")
    _seed_unreflected(2, project="fabio-live")
    (store / "LIVE_LOCK").write_text("fabio-live\n", encoding="utf-8")
    monkeypatch.setattr(reflect_mod, "_claude_runner",
                        lambda prompt, timeout_s=120: _GOOD)

    assert cli.main(["rules", "reflect", "--all-projects", "--limit", "3"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["alpha"]["reflected"] == 2               # namespace drained
    assert out["fabio-live"] == {"reflected": 0, "attempted": 0,
                                 "skipped": "live_lock"}
    # the locked namespace was never touched: no marker, no reflected rule
    assert reflect_mod.read_marker("fabio-live") == {}
    assert incidents.inject(project="fabio-live") == ""
    assert "always pin the venv" in incidents.inject(project="alpha")
