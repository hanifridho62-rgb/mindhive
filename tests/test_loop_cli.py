# tests/test_loop_cli.py
import os
import pytest
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


def test_live_lock_refuses_run_and_does_not_launch_dashboard(tmp_path, monkeypatch, capsys):
    """LIVE_LOCK (V17 live trading) must refuse the run BEFORE spawning the
    dashboard + its auto-harvest loop. The gate fires with a clean error + exit
    code 2, not a raw traceback, and nothing with side effects runs."""
    from mindhive import paths
    _setup(tmp_path, monkeypatch)
    main(["loop", "init"])
    calls = _stub_run(monkeypatch)
    (paths.store_home() / "LIVE_LOCK").write_text("x", encoding="utf-8")
    rc = main(["loop"])                 # no --force
    err = capsys.readouterr().err
    assert rc == 2
    assert calls["launch"] == 0         # dashboard NOT spawned during live trading
    assert calls["run_loop"] == 0       # run refused before the engine
    assert "LIVE_LOCK" in err


def test_live_lock_with_force_allows_launch(tmp_path, monkeypatch):
    """--force overrides LIVE_LOCK, so the dashboard launches and the loop runs."""
    from mindhive import paths
    _setup(tmp_path, monkeypatch)
    main(["loop", "init"])
    calls = _stub_run(monkeypatch)
    (paths.store_home() / "LIVE_LOCK").write_text("x", encoding="utf-8")
    rc = main(["loop", "--force"])
    assert rc == 0
    assert calls["launch"] == 1
    assert calls["run_loop"] == 1


def test_soft_launch_failure_reports_to_stderr(tmp_path, monkeypatch, capsys):
    """launch_background returning {'launched': False} (graceful soft failure,
    no exception) still prints a diagnostic to stderr and never aborts the loop."""
    _setup(tmp_path, monkeypatch)
    main(["loop", "init"])
    calls = _stub_run(monkeypatch,
                      launch_result={"launched": False, "reason": "port in use"})
    rc = main(["loop"])
    err = capsys.readouterr().err
    assert rc == 0
    assert calls["run_loop"] == 1
    assert "not launched" in err and "port in use" in err


@pytest.mark.parametrize("val", ["0", "false", "off", "no", "OFF", "False"])
def test_env_opt_out_spellings_all_suppress(tmp_path, monkeypatch, val):
    """Every documented opt-out spelling (case-insensitive) suppresses launch."""
    _setup(tmp_path, monkeypatch)
    main(["loop", "init"])
    monkeypatch.setenv("MINDHIVE_LOOP_DASHBOARD", val)
    calls = _stub_run(monkeypatch)
    main(["loop"])
    assert calls["launch"] == 0
    assert calls["run_loop"] == 1
