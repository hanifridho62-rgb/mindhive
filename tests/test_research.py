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


# --- U6: program composition -------------------------------------------------

def test_compose_program_lists_skills_in_order_and_goal():
    out = research.compose_program(
        "minimize val_bpb on the small preset",
        skills=[{"name": "python-performance-optimization"},
                {"name": "ml-pipeline-workflow", "plugin": "machine-learning-ops"}])
    prog = out["program"]
    assert "minimize val_bpb" in prog
    a = prog.index("python-performance-optimization")
    b = prog.index("ml-pipeline-workflow")
    assert a < b
    assert "machine-learning-ops" in prog


def test_compose_program_no_skills_still_valid():
    out = research.compose_program("goal only")
    assert "goal only" in out["program"]
    assert "strategy guidance" not in out["program"].lower()


def test_compose_program_preset_bounds_verbatim():
    preset = {"batch_size": "<= 8", "sequence_len": "<= 512"}
    prog = research.compose_program("g", preset=preset)["program"]
    assert "batch_size: <= 8" in prog
    assert "sequence_len: <= 512" in prog


def test_compose_program_primed_block_omitted_when_empty():
    prog = research.compose_program("g", primed=[])["program"]
    assert "past learnings" not in prog.lower()
    prog2 = research.compose_program("g", primed=["OOM at batch 16"])["program"]
    assert "OOM at batch 16" in prog2


def test_compose_program_iteration_contract_and_prior_result():
    prior = {"val_bpb": 1.31, "status": "ok", "change": "lr 0.01 -> 0.02"}
    out = research.compose_program("g", prior=prior)
    prog = out["program"]
    low = prog.lower()
    assert "edit" in low and "train.py" in prog
    assert "do not run" in low or "never run" in low
    assert "end your turn" in low or "end the turn" in low
    assert "1.31" in prog and "lr 0.01 -> 0.02" in prog


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


# --- U5/U9/U10/U11: controller, terminators, write-back, attribution --------

class StubResearcher:
    """Scripted researcher agent: each turn pops the next scripted result;
    after the script runs out every turn is a plain success."""

    def __init__(self, results=None, delay_s=0.0):
        self.prompts = []
        self.stopped = False
        self._results = list(results or [])
        self._delay = delay_s

    def turn(self, prompt):
        self.prompts.append(prompt)
        if self._delay:
            time.sleep(self._delay)
        return self._results.pop(0) if self._results else {"ok": True}

    def stop(self):
        self.stopped = True


class FakeLedger:
    def __init__(self):
        self.events = []

    def __call__(self, etype, payload, session_id=None, tags=None, project=None):
        self.events.append({"type": etype, "payload": payload,
                            "tags": list(tags or [])})


def _controller(tmp_path, trainer=None, researcher=None, ledger=None,
                factory=None, **kw):
    """A controller wired entirely to test doubles: mock trainer, stub agent,
    fake ledger, empty preflight, tiny cap."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    train_py = tmp_path / "train.py"
    if not train_py.exists():
        train_py.write_text("lr = 0.01\n", encoding="utf-8")
    researcher = researcher or StubResearcher()
    kw.setdefault("cap_s", 20)
    kw.setdefault("turn_timeout_s", 10)
    return research.ResearchController(
        trainer_cmd=trainer or _cmd("--mode", "ok", "--seconds", "0.05"),
        agent_factory=factory or (lambda profile: researcher),
        train_path=train_py,
        leaderboard=research.Leaderboard(tmp_path / "lb.json"),
        append_event=ledger if ledger is not None else FakeLedger(),
        preflight=lambda profile: [],
        **kw)


def _wait_ended(ctl, timeout=30):
    assert ctl.wait(timeout), f"run did not end in {timeout}s: {ctl.status()}"


def test_controller_cap_runs_exact_count(tmp_path):
    ctl = _controller(tmp_path)
    assert ctl.start("g", max_experiments=3)["ok"]
    _wait_ended(ctl)
    st = ctl.status()
    assert st["state"] == "ended" and st["reason"] == "cap"
    assert st["experiments"] == 3
    assert len(ctl.leaderboard.all()) == 3


def test_controller_start_while_running_rejected(tmp_path):
    ctl = _controller(tmp_path, trainer=_cmd("--mode", "ok", "--seconds", "0.4"))
    assert ctl.start("g", max_experiments=2)["ok"]
    res = ctl.start("again")
    assert not res["ok"] and "active" in res["error"].lower()
    _wait_ended(ctl)


def test_controller_preflight_failure_rejects_start(tmp_path):
    ctl = _controller(tmp_path)
    ctl._preflight = lambda profile: ["WSL not reachable"]
    res = ctl.start("g")
    assert not res["ok"] and "WSL not reachable" in res["error"]
    assert ctl.status()["state"] == "idle"
    assert ctl.leaderboard.all() == []


def test_controller_stop_kills_inflight_training(tmp_path):
    calls = {"n": 0}

    def trainer(i):
        calls["n"] += 1
        if i == 0:
            return _cmd("--mode", "ok", "--seconds", "0.05")
        return _cmd("--mode", "hang")           # second experiment hangs

    researcher = StubResearcher()
    ctl = _controller(tmp_path, trainer=trainer, researcher=researcher, cap_s=25)
    assert ctl.start("g")["ok"]
    deadline = time.time() + 15
    while calls["n"] < 2 and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)                             # let the hang trainer spawn
    t0 = time.monotonic()
    ctl.stop()
    _wait_ended(ctl, 10)
    assert time.monotonic() - t0 < 8            # killed, not waited to cap
    st = ctl.status()
    assert st["state"] == "stopped"
    assert researcher.stopped
    assert len(ctl.leaderboard.all()) == 1      # first experiment preserved


def test_controller_usage_exhaustion_graceful_end(tmp_path):
    researcher = StubResearcher(results=[{"ok": True},
                                         {"ok": False, "usage_exhausted": True}])
    ctl = _controller(tmp_path, researcher=researcher)
    assert ctl.start("g")["ok"]
    _wait_ended(ctl)
    st = ctl.status()
    assert st["state"] == "ended" and st["reason"] == "usage"
    assert len(ctl.leaderboard.all()) == 1
    assert ctl.leaderboard.best() is not None


def test_controller_time_budget_ends_run(tmp_path):
    ctl = _controller(tmp_path, trainer=_cmd("--mode", "ok", "--seconds", "0.3"))
    assert ctl.start("g", time_budget_s=0.2)["ok"]
    _wait_ended(ctl)
    st = ctl.status()
    assert st["state"] == "ended" and st["reason"] == "time"
    assert st["experiments"] == 1               # in-flight one finished


def test_controller_oom_does_not_end_loop(tmp_path):
    def trainer(i):
        return _cmd("--mode", "oom" if i == 0 else "ok", "--seconds", "0.05")

    ctl = _controller(tmp_path, trainer=trainer)
    assert ctl.start("g", max_experiments=2)["ok"]
    _wait_ended(ctl)
    records = ctl.leaderboard.all()
    assert [r["status"] for r in records] == ["oom", "ok"]
    assert ctl.status()["reason"] == "cap"


def test_controller_agent_turn_timeout_aborts_and_continues(tmp_path):
    class SlowFirstResearcher(StubResearcher):
        def turn(self, prompt):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                time.sleep(5)                   # exceeds turn_timeout_s below
            return {"ok": True}

    researcher = SlowFirstResearcher()
    ctl = _controller(tmp_path, researcher=researcher, turn_timeout_s=0.3)
    assert ctl.start("g", max_experiments=2)["ok"]
    _wait_ended(ctl)
    records = ctl.leaderboard.all()
    assert records[0]["status"] == "aborted"
    assert records[-1]["status"] == "ok"
    assert ctl.status()["reason"] == "cap"


def test_controller_unknown_agent_error_stops_conservatively(tmp_path):
    researcher = StubResearcher(results=[{"ok": False, "error": "boom"}])
    ctl = _controller(tmp_path, researcher=researcher)
    assert ctl.start("g")["ok"]
    _wait_ended(ctl)
    st = ctl.status()
    assert st["state"] == "ended" and st["reason"] == "error"


# --- U9: flywheel write-back -------------------------------------------------

def test_writeback_experiment_result_events(tmp_path):
    ledger = FakeLedger()
    ctl = _controller(tmp_path, ledger=ledger)
    assert ctl.start("the goal", skills=[{"name": "s1"}], max_experiments=1)["ok"]
    _wait_ended(ctl)
    results = [e for e in ledger.events if e["type"] == "experiment_result"
               and "signature" not in e["payload"]]
    assert len(results) == 1
    p = results[0]["payload"]
    assert p["status"] == "ok" and isinstance(p["val_bpb"], float)
    assert p["goal"] == "the goal"
    assert "self-reliant" in results[0]["tags"]
    assert any(t == "skill:s1" for t in results[0]["tags"])


def test_writeback_oom_event_has_status_no_score(tmp_path):
    ledger = FakeLedger()
    ctl = _controller(tmp_path, ledger=ledger,
                      trainer=_cmd("--mode", "oom", "--seconds", "0.05"))
    assert ctl.start("g", max_experiments=1)["ok"]
    _wait_ended(ctl)
    results = [e for e in ledger.events if e["type"] == "experiment_result"
               and "signature" not in e["payload"]]
    assert results[0]["payload"]["status"] == "oom"
    assert results[0]["payload"]["val_bpb"] is None


def test_writeback_decision_on_best_and_none_when_unscored(tmp_path):
    ledger = FakeLedger()
    ctl = _controller(tmp_path, ledger=ledger)
    assert ctl.start("g", max_experiments=2)["ok"]
    _wait_ended(ctl)
    decisions = [e for e in ledger.events if e["type"] == "decision"]
    assert len(decisions) == 1
    assert str(ctl.leaderboard.best()["val_bpb"]) in decisions[0]["payload"]["text"]

    ledger2 = FakeLedger()
    ctl2 = _controller(tmp_path / "second", ledger=ledger2,
                       trainer=_cmd("--mode", "crash", "--seconds", "0.05"))
    assert ctl2.start("g", max_experiments=2)["ok"]
    _wait_ended(ctl2)
    assert [e for e in ledger2.events if e["type"] == "decision"] == []


# --- U10: skill-effectiveness attribution ------------------------------------

def test_attribution_credits_skills_on_improvement(tmp_path):
    scores = iter(["2.0", "1.5", "1.8"])        # improve, improve, no improve

    def trainer(i):
        return _cmd("--mode", "ok", "--seconds", "0.05", "--val-bpb", next(scores))

    ledger = FakeLedger()
    ctl = _controller(tmp_path, ledger=ledger, trainer=trainer)
    assert ctl.start("g", skills=[{"name": "a"}, {"name": "b"}],
                     max_experiments=3)["ok"]
    _wait_ended(ctl)
    assert ctl.skill_tally() == {"a": 2, "b": 2}
    attr = [e for e in ledger.events
            if e["payload"].get("signature", "").startswith("skill:")]
    assert {e["payload"]["signature"] for e in attr} == {"skill:a", "skill:b"}
    assert len(attr) == 4                       # 2 improvements x 2 skills


def test_attribution_none_without_skills_or_improvement(tmp_path):
    ledger = FakeLedger()
    ctl = _controller(tmp_path, ledger=ledger,
                      trainer=_cmd("--mode", "crash", "--seconds", "0.05"))
    assert ctl.start("g", skills=[{"name": "a"}], max_experiments=1)["ok"]
    _wait_ended(ctl)
    assert ctl.skill_tally() == {}

    ledger2 = FakeLedger()
    ctl2 = _controller(tmp_path, ledger=ledger2)
    assert ctl2.start("g", max_experiments=1)["ok"]     # no skills staged
    _wait_ended(ctl2)
    assert ctl2.skill_tally() == {}
    assert [e for e in ledger2.events
            if e["payload"].get("signature", "").startswith("skill:")] == []


# --- U11: account-profile failover --------------------------------------------

def _failover_setup(tmp_path, auth_map, results_by_profile):
    made = {}

    def factory(profile):
        made[profile] = StubResearcher(results=results_by_profile.get(profile))
        return made[profile]

    ctl = _controller(tmp_path, factory=factory,
                      profiles=list(auth_map),
                      auth_status=lambda p: {"state": auth_map[p]})
    return ctl, made


def test_failover_continues_on_next_healthy_profile(tmp_path):
    ctl, made = _failover_setup(
        tmp_path, {"A": "ok", "B": "ok"},
        {"A": [{"ok": False, "usage_exhausted": True}]})
    assert ctl.start("g", max_experiments=2, failover=True, profile="A")["ok"]
    _wait_ended(ctl)
    st = ctl.status()
    assert st["reason"] == "cap" and st["experiments"] == 2
    assert "B" in made                          # rolled to profile B


def test_failover_no_healthy_profile_ends_gracefully(tmp_path):
    ctl, made = _failover_setup(
        tmp_path, {"A": "ok", "B": "expired"},
        {"A": [{"ok": False, "usage_exhausted": True}]})
    assert ctl.start("g", failover=True, profile="A")["ok"]
    _wait_ended(ctl)
    assert ctl.status()["reason"] == "usage"
    assert "B" not in made


def test_failover_off_usage_always_ends(tmp_path):
    ctl, made = _failover_setup(
        tmp_path, {"A": "ok", "B": "ok"},
        {"A": [{"ok": False, "usage_exhausted": True}]})
    assert ctl.start("g", failover=False, profile="A")["ok"]
    _wait_ended(ctl)
    assert ctl.status()["reason"] == "usage"
    assert "B" not in made
