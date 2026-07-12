# tests/test_research.py
"""Self-Reliant Phase 1: runner + watchdog (U2), parser + diff (U3),
leaderboard (U4). All GPU-free — driven by scripts/self_reliant/mock_trainer.py.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mindhive import research

MOCK = Path(__file__).resolve().parents[1] / "scripts" / "self_reliant" / "mock_trainer.py"


def _cmd(*args):
    return [sys.executable, str(MOCK), *args]


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- U2: runner + watchdog -------------------------------------------------

def test_runner_ok_captures_score():
    res = research.run_experiment(_cmd("--mode", "ok", "--seconds", "0.2",
                                       "--val-bpb", "1.5"))
    assert res["status"] == "ok"
    assert res["val_bpb"] == pytest.approx(1.5)
    assert res["exit_code"] == 0


def test_runner_timeout_kills_at_cap():
    start = time.monotonic()
    res = research.run_experiment(_cmd("--mode", "hang"), cap_s=1)
    elapsed = time.monotonic() - start
    assert res["status"] == "timeout"
    assert res["val_bpb"] is None
    assert elapsed < 15          # killed near the cap, not after 600s


def test_runner_oom_classified():
    res = research.run_experiment(_cmd("--mode", "oom", "--seconds", "0.1"))
    assert res["status"] == "oom"
    assert res["val_bpb"] is None


def test_runner_crash_classified():
    res = research.run_experiment(_cmd("--mode", "crash", "--seconds", "0.1"))
    assert res["status"] == "crash"


def test_runner_zero_exit_without_score_is_crash():
    res = research.run_experiment(_cmd("--mode", "noscore", "--seconds", "0.1"))
    assert res["status"] == "crash"
    assert res["val_bpb"] is None


def test_runner_kills_grandchild(tmp_path):
    pidfile = tmp_path / "gc.pid"
    res = research.run_experiment(
        _cmd("--mode", "grandchild", "--pidfile", str(pidfile)), cap_s=1)
    assert res["status"] == "timeout"
    pid = int(pidfile.read_text().strip())
    deadline = time.time() + 8
    while _pid_alive(pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not _pid_alive(pid), "grandchild survived the tree kill"


def test_runner_bad_command_is_crash():
    res = research.run_experiment([sys.executable, str(MOCK) + "_nope.py"])
    assert res["status"] == "crash"


# --- U3: parser + diff -----------------------------------------------------

def test_parse_val_bpb_basic():
    assert research.parse_val_bpb("step 10 | val_bpb 1.2345") == pytest.approx(1.2345)


def test_parse_val_bpb_last_wins():
    txt = "val_bpb 2.0\nval_bpb 1.5\nval_bpb 1.1"
    assert research.parse_val_bpb(txt) == pytest.approx(1.1)


def test_parse_val_bpb_absent_returns_none():
    assert research.parse_val_bpb("training complete, no score") is None
    assert research.parse_val_bpb("") is None


def test_parse_val_bpb_non_numeric_returns_none():
    assert research.parse_val_bpb("val_bpb nan") is None
    assert research.parse_val_bpb("val_bpb inf") is None


def test_diff_summary_names_change():
    s = research.diff_summary("lr = 0.01\n", "lr = 0.02\n")
    assert "0.02" in s and s != "no change"


def test_diff_summary_no_change():
    assert research.diff_summary("x = 1\n", "x = 1\n") == "no change"


# --- U4: leaderboard -------------------------------------------------------

def _rec(score, status="ok", **extra):
    return {"val_bpb": score, "status": status, **extra}


def test_leaderboard_ranks_ascending(tmp_path):
    lb = research.Leaderboard(tmp_path / "lb.json")
    lb.add(_rec(1.5)); lb.add(_rec(0.9)); lb.add(_rec(1.2))
    order = [e["val_bpb"] for e in lb.ranked()]
    assert order == [0.9, 1.2, 1.5]


def test_leaderboard_failures_sort_last_and_never_best(tmp_path):
    lb = research.Leaderboard(tmp_path / "lb.json")
    lb.add(_rec(None, status="oom"))
    lb.add(_rec(1.1))
    ranked = lb.ranked()
    assert ranked[0]["val_bpb"] == 1.1
    assert ranked[-1]["status"] == "oom"
    assert lb.best()["val_bpb"] == pytest.approx(1.1)


def test_leaderboard_best_is_min(tmp_path):
    lb = research.Leaderboard(tmp_path / "lb.json")
    for s in (1.4, 0.8, 1.0):
        lb.add(_rec(s))
    assert lb.best()["val_bpb"] == pytest.approx(0.8)


def test_leaderboard_empty(tmp_path):
    lb = research.Leaderboard(tmp_path / "lb.json")
    assert lb.ranked() == []
    assert lb.best() is None


def test_leaderboard_concurrent_appends_no_loss(tmp_path):
    path = tmp_path / "lb.json"

    def worker(i):
        research.Leaderboard(path).add(_rec(1.0 + i / 100.0))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(research.Leaderboard(path).all()) == 20
