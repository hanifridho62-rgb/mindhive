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


# --- auto-launch the dashboard on a real loop run ---------------------------

def _stub_run(monkeypatch, launch_result=None, launch_raises=False):
    """Stub the run path so `main(['loop'])` reaches the dashboard launch and a
    stubbed run_loop, without spawning claude or a real dashboard."""
    from mindhive.loop import driver as loop_driver, engine as loop_engine
    from mindhive import dashboard
    calls = {"launch": 0, "run_loop": 0}

    monkeypatch.setattr(loop_driver, "preflight", lambda *a, **k: True)

    def fake_run_loop(*a, **k):
        calls["run_loop"] += 1
        return {"reason": "completed", "iterations": 1}

    monkeypatch.setattr(loop_engine, "run_loop", fake_run_loop)

    def fake_launch(*a, **k):
        calls["launch"] += 1
        if launch_raises:
            raise RuntimeError("spawn exploded")
        return launch_result or {"launched": True,
                                 "url": "http://127.0.0.1:54461/?token=t", "pid": 9}

    monkeypatch.setattr(dashboard, "launch_background", fake_launch)
    return calls


def test_real_run_auto_launches_dashboard(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    main(["loop", "init"])
    calls = _stub_run(monkeypatch)
    rc = main(["loop"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls["launch"] == 1
    assert calls["run_loop"] == 1
    assert "dashboard:" in out and "54461" in out


def test_no_dashboard_flag_suppresses_launch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    main(["loop", "init"])
    calls = _stub_run(monkeypatch)
    rc = main(["loop", "--no-dashboard"])
    assert rc == 0
    assert calls["launch"] == 0
    assert calls["run_loop"] == 1


def test_env_opt_out_suppresses_launch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    main(["loop", "init"])
    monkeypatch.setenv("MINDHIVE_LOOP_DASHBOARD", "0")
    calls = _stub_run(monkeypatch)
    main(["loop"])
    assert calls["launch"] == 0
    assert calls["run_loop"] == 1


def test_launch_failure_is_non_fatal(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    main(["loop", "init"])
    calls = _stub_run(monkeypatch, launch_raises=True)
    rc = main(["loop"])
    assert rc == 0
    assert calls["launch"] == 1
    assert calls["run_loop"] == 1


def test_dry_run_does_not_launch_dashboard(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    main(["loop", "init"])
    calls = _stub_run(monkeypatch)
    main(["loop", "--dry-run"])
    assert calls["launch"] == 0
    assert calls["run_loop"] == 0


def test_init_and_status_do_not_launch_dashboard(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    calls = _stub_run(monkeypatch)
    main(["loop", "init"])
    main(["loop", "status"])
    assert calls["launch"] == 0
