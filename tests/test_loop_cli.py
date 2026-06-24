# tests/test_loop_cli.py
import os
from mindhive.__main__ import main


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")
    monkeypatch.chdir(tmp_path)


def test_loop_init_scaffolds_files(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    rc = main(["loop", "init"])
    assert rc == 0
    d = tmp_path / ".hive-loop"
    assert (d / "PROMPT.md").exists()
    assert (d / "fix_plan.md").exists()
    assert (d / "AGENT.md").exists()
    assert (d / "loop.toml").exists()


def test_loop_status_no_runs(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    rc = main(["loop", "status"])
    assert rc == 0
    assert "no loop runs" in capsys.readouterr().out.lower()


def test_loop_dry_run_does_not_call_cli(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    main(["loop", "init"])
    rc = main(["loop", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "dry-run" in out or "would run" in out
