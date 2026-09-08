# tests/test_orchestrator_gate.py
"""The per-unit test gate — "validated" means the lane's own tests passed.

Before `orchestrator/gate.py`, `lintester._run_child` called a unit validated
when its child exited cleanly. No test ever ran against the lane, so a child
that broke the very module it was asked to fix and exited 0 was reported as a
validated unit, and the PRD's `hive selftest` gate existed only as a sentence in
the module docstring. The first test below is that scenario and it is RED
without the change.

## Real git, real pytest

Every mission here runs on a real repository in `tmp_path` and every gate is a
real `pytest` subprocess in a real lane checkout. A faked `run_and_record`
appears only where the property under test is wiring (how many suites ran, on
which tree, with which flag) rather than the exit code — and those tests say so.

## Two facts the gate must keep straight

* A red gate is the TASK's failure, not the machine's. `environment_failure`
  must answer `""` for it, or the halt that stops a mission whose CLI is logged
  out would stop a mission whose child merely wrote a bug.
* A targeted run is NOT a ledger verification. `replenish` clears the
  "code changed, nothing verified since" gap on any exit-0 `verification`
  event; a two-file pass masquerading as a tree verification would clear it
  falsely. Only the closing full suite records one, stamped `scope: full`.
"""
import asyncio
import inspect
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from mindhive import events as ev_mod
from mindhive.orchestrator import environment as env_mod
from mindhive.orchestrator import gate as gate_mod
from mindhive.orchestrator import lintester as lint
from mindhive.orchestrator import queue as q_mod
from mindhive.orchestrator import worktree as wt_mod


# --- fixture builders ---------------------------------------------------------

def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *[str(a) for a in args]],
                       capture_output=True, text=True, shell=False)
    assert r.returncode == 0, f"git {' '.join(map(str, args))}: {r.stderr}"
    return r


def _write(root, rel, text):
    target = Path(root) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return target


GREEN_WRITER = """
    def write(x):
        return 1
"""

BROKEN_WRITER = """
    def write(x):
        return 0
"""


@pytest.fixture
def repo(tmp_path):
    """A real repository holding a package and a suite, so a lane has tests to
    run: `src/pkg/{writer,other}.py`, each covered by one test that imports it
    the way this repo's own tests import their modules."""
    root = tmp_path / "repo"
    _write(root, "src/pkg/__init__.py", "")
    _write(root, "src/pkg/writer.py", GREEN_WRITER)
    _write(root, "src/pkg/other.py", """
        def other():
            return 2
    """)
    _write(root, "tests/test_writer.py", """
        from pkg import writer


        def test_write():
            assert writer.write(1) == 1
    """)
    _write(root, "tests/test_other.py", """
        from pkg import other


        def test_other():
            assert other.other() == 2
    """)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


@pytest.fixture(autouse=True)
def session(monkeypatch):
    monkeypatch.setenv("MINDHIVE_SESSION", "s1")


def _one_unit(_root_id, _title, _intent):
    return [("t-writer", "Make write return 1", "fix src/pkg/writer.py")]


def _two_units(_root_id, _title, _intent):
    return [("t-writer", "Make write return 1", "fix src/pkg/writer.py"),
            ("t-other", "Keep other at 2", "fix src/pkg/other.py")]


def _writes(files, ok=True):
    """A runner standing in for a child: it writes *files* into ITS LANE and
    reports the exit its child would have."""
    def runner(entry, lane):
        for rel, text in files.items():
            _write(lane.worktree_path, rel, text)
        return ok
    return runner


def _fake_record(exit_code=0, counts=None, tail="1 passed in 0.10s"):
    """The dict `verify.run_and_record` answers, for the wiring tests."""
    return {"exit_code": int(exit_code), "counts": dict(counts or {}),
            "error": "", "artifact": None, "excerpt": tail, "tail": tail}


def _mission(repo, runner, validator, decomposer=_one_unit, lanes=1, **kw):
    kw.setdefault("max_attempts", 1)
    return lint.LintesterOrchestrator(
        root=repo, project="test", lanes=lanes, decomposer=decomposer,
        runner=runner, validator=validator, **kw,
    ).execute_mission("make write return 1", dry_run=False)


def _gate(repo, **kw):
    return gate_mod.SuiteGate(root=repo, project="test", **kw)


# --- the change ----------------------------------------------------------------

def test_a_child_that_exits_clean_but_breaks_a_test_is_not_validated(repo):
    """THE regression. The child rewrites `writer.py` so its own test fails,
    and reports success. Before the gate this was a validated unit."""
    got = _mission(repo, _writes({"src/pkg/writer.py": BROKEN_WRITER}),
                   _gate(repo))

    assert got.outcome == "partial", got.reason
    assert got.completed == ()
    assert any("t-writer" in line for line in got.failed), got.failed
    assert len(got.gates) == 1
    row = got.gates[0]
    assert row["ok"] is False
    assert row["scope"] == gate_mod.TARGETED
    assert row["exit_code"] == 1
    assert row["counts"].get("failed") == 1, row
    assert "test_write" in row["tail"], row["tail"]
    assert row["task_id"] == "t-writer" and row["attempt"] == 1


def test_without_a_validator_the_same_child_is_still_validated(repo):
    """CANARY for the test above, and the library default: `validator=None`
    is byte-for-byte the behaviour every runner-fake test in the suite was
    written against."""
    got = _mission(repo, _writes({"src/pkg/writer.py": BROKEN_WRITER}), None)

    assert got.outcome == "ok", got.reason
    assert got.completed == ("t-writer",)
    assert got.gates == () and got.suites == ()


def test_a_red_gate_is_the_tasks_failure_not_the_machines(repo):
    """A gate failure routed to the environment halt would stop every other
    lane and report the mission as `environment` — the mirror image of the bug
    `environment.py` fixes."""
    got = _mission(repo, _writes({"src/pkg/writer.py": BROKEN_WRITER}),
                   _gate(repo))
    assert got.outcome == "partial"
    assert got.environment == "" and got.left == ()

    res = gate_mod.GateResult(ok=False, scope=gate_mod.TARGETED, exit_code=1,
                              tail="1 failed in 0.1s", reason="1 failed",
                              task_id="t-1")
    out = gate_mod.failure_outcome(res)
    entry = q_mod.QueueEntry(task=lint.AgentTask(task_id="t-1"), result=out,
                             reason=out.reason)
    assert out.ok is False and out.result == "fail"
    assert out.executed is True
    assert out.exit_code == 1 and out.stderr == "1 failed in 0.1s"
    assert q_mod._succeeded(out) is False
    assert env_mod.environment_failure(entry) == ""


def test_the_retry_prompt_carries_the_pytest_tail(repo):
    """The first real stderr the repair loop has ever received. No real child
    populates `_RunOutcome.stderr` (`agent.run` answers a JSON envelope), so
    until the gate the "self-correcting (stderr injected)" caption described
    nothing that happened."""
    seen = []

    def runner(entry, lane):
        seen.append(entry.task.description)
        return _writes({"src/pkg/writer.py": BROKEN_WRITER})(entry, lane)

    got = _mission(repo, runner, _gate(repo), max_attempts=2)

    assert len(seen) == 2, "the unit was not retried"
    assert "test_write" not in seen[0]
    assert "test_write" in seen[1], seen[1]
    assert got.outcome == "partial"


def test_tails_are_stable_across_durations_and_temp_paths():
    """The repair loop hashes the tail into a failure signature. Wall-clock
    noise in it makes every red run a fresh signature and the breaker never
    sees the same failure twice."""
    first = ("FAILED tests/test_x.py::test_a - assert 1 == 2\n"
             "/tmp/pytest-of-hanif/pytest-31/repo0/src/x.py:3\n"
             "C:\\Users\\hanif\\AppData\\Local\\Temp\\pytest-of-hanif\\pytest-31\\x\n"
             "1 failed in 0.12s")
    second = ("FAILED tests/test_x.py::test_a - assert 1 == 2\n"
              "/tmp/pytest-of-other/pytest-7/repo0/src/x.py:3\n"
              "C:\\Users\\hanif\\AppData\\Local\\Temp\\pytest-of-other\\pytest-7\\x\n"
              "1 failed in 61.40s (0:01:01)")
    assert first != second
    assert gate_mod.stabilise(first) == gate_mod.stabilise(second)
    assert "test_a" in gate_mod.stabilise(first)
    assert "assert 1 == 2" in gate_mod.stabilise(first)


# --- what the gate runs ----------------------------------------------------------

def test_narrowing_runs_only_the_tests_that_mention_the_changed_module(repo):
    """`test_other` is red at the base commit. A gate that ran the whole suite
    would fail a unit that only touched `writer.py`; the targeted gate passes
    it, and the closing FULL suite is what carries the red — as a mission-level
    fact, not the unit's."""
    _write(repo, "tests/test_other.py", """
        from pkg import other


        def test_other():
            assert other.other() == 99
    """)
    _git(repo, "commit", "-qam", "red other")

    got = _mission(repo, _writes({"src/pkg/writer.py": GREEN_WRITER + "\n"}),
                   _gate(repo))

    row = got.gates[0]
    assert row["ok"] is True, row
    assert row["scope"] == gate_mod.TARGETED
    assert [Path(p).name for p in row["targets"]] == ["test_writer.py"]
    assert got.completed == ("t-writer",)
    assert len(got.suites) == 1
    assert got.suites[0]["ok"] is False
    assert got.suites[0]["counts"].get("failed") == 1, got.suites[0]
    assert got.outcome == "partial"
    assert "full suite" in got.reason, got.reason


def test_the_childs_own_new_test_file_is_always_targeted(repo):
    """A test the child wrote mentions nothing yet; it is still the child's
    claim about its own work, so it runs."""
    got = _mission(repo, _writes({"tests/test_fresh.py": """
        def test_fresh():
            assert 0 == 1
    """}), _gate(repo))

    row = got.gates[0]
    assert row["ok"] is False
    assert [Path(p).name for p in row["targets"]] == ["test_fresh.py"]
    assert got.outcome == "partial"


def test_a_change_no_test_mentions_is_a_recorded_vacuous_pass(repo):
    """Recorded, not hidden: the row says `none` and why. The alternative —
    failing every unit that adds an untested module — was rejected because it
    would fail the unit for the SUITE's gap, and the full suite at the end is
    the mission-level claim anyway."""
    got = _mission(repo, _writes({"src/pkg/lonely.py": """
        def lonely():
            return 3
    """}), _gate(repo))

    row = got.gates[0]
    assert row["ok"] is True
    assert row["scope"] == gate_mod.NONE
    assert row["targets"] == []
    assert "no test" in row["reason"], row["reason"]
    assert got.completed == ("t-writer",)
    assert got.outcome == "ok", got.reason


def test_a_clean_lane_falls_back_to_the_units_own_files(repo):
    """A child that changed nothing still claimed to do a job. Its unit's own
    files say which tests that job is judged by."""
    calls = []

    def spy(argv, **kw):
        calls.append(list(argv))
        return _fake_record()

    gate = _gate(repo, runner=spy)
    lane = wt_mod.acquire(repo, 0, "lm-fallback")
    assert lane.ok, lane.reason
    try:
        res = gate.check(lane.path, lint.AgentTask(
            task_id="t-1", lane_id=0,
            files=(str(repo / "src" / "pkg" / "writer.py"),)))
    finally:
        wt_mod.release(repo, lane)

    assert res.ok is True and res.scope == gate_mod.TARGETED
    assert [Path(p).name for p in res.targets] == ["test_writer.py"]
    assert calls and calls[0][-1].endswith("test_writer.py")


def test_the_gate_refuses_to_run_outside_a_lane_checkout(repo):
    """pytest never runs in the main checkout on the gate's account."""
    calls = []

    def spy(argv, **kw):
        calls.append(list(argv))
        return _fake_record()

    gate = _gate(repo, runner=spy)
    task = lint.AgentTask(task_id="t-1",
                          files=(str(repo / "src" / "pkg" / "writer.py"),))

    res = gate.check(repo, task)
    assert res.ok is False and res.scope == gate_mod.NONE
    assert "lane" in res.reason
    assert calls == [], "pytest was about to run in the main checkout"

    res = gate.check("", task)
    assert res.ok is False and calls == []


def test_a_run_with_no_root_makes_its_lanes_under_the_working_directory(
        repo, monkeypatch):
    """`root=None` means the working directory everywhere else in the
    pipeline; on the lane path it was `Path(None)` -- a `TypeError` on the
    first run that started work with `--root` unset, which no dry run and no
    runner-fake test had ever reached."""
    monkeypatch.chdir(repo)
    got = lint.LintesterOrchestrator(
        root=None, project="test", lanes=1, decomposer=_one_unit,
        runner=_writes({"src/pkg/writer.py": GREEN_WRITER}),
        validator=gate_mod.SuiteGate(root=None, project="test"),
        max_attempts=1,
    ).execute_mission("make write return 1", dry_run=False)

    assert got.outcome == "ok", got.reason
    assert got.completed == ("t-writer",)
    row = got.gates[0]
    assert [Path(p).name for p in row["targets"]] == ["test_writer.py"]
    lanes = (repo / wt_mod.LANE_DIR).resolve()
    assert Path(row["argv"][-1]).resolve().is_relative_to(lanes), (
        f"the gate ran outside this checkout's lanes: {row['argv'][-1]}")


def test_a_hung_gate_is_killed_at_its_cap(repo):
    started = time.monotonic()
    got = _mission(repo, _writes({"tests/test_writer.py": """
        import time


        def test_write():
            time.sleep(30)
    """}), _gate(repo, unit_timeout_s=2))

    assert time.monotonic() - started < 25, "the cap did not fire"
    row = got.gates[0]
    assert row["ok"] is False
    assert row["exit_code"] == gate_mod.TIMEOUT_EXIT
    assert "cap" in row["reason"], row["reason"]
    assert got.outcome == "partial"


# --- the mission around the gate ---------------------------------------------------

def test_gate_time_is_excluded_from_the_weight_calibration(repo):
    """The weight predicts how long the CHILD runs. A sample that carried the
    gate's pytest time would score the estimator against the suite."""
    def slow(argv, **kw):
        time.sleep(0.3)
        return _fake_record(counts={"passed": 1})

    got = _mission(repo, _writes({"src/pkg/writer.py": GREEN_WRITER + "\n"}),
                   _gate(repo, runner=slow))

    assert got.outcome == "ok", got.reason
    gate_ms = float(got.gates[0]["duration_s"]) * 1000.0
    assert gate_ms >= 250.0, got.gates[0]
    samples = {s["unit"]: s["ms"] for s in got.calibration["samples"]}
    assert 0 < samples["t-writer"] < gate_ms, (samples, gate_ms)


def test_the_full_suite_runs_once_per_lane_that_validated_a_unit(repo):
    """Wiring, with the runner faked: two lanes, two units, two targeted runs
    with `record=False`, then one full run per lane with `record=True` and the
    whole `tests` directory as its argument."""
    calls = []

    def spy(argv, **kw):
        calls.append((list(argv), kw.get("record"), str(kw.get("cwd") or "")))
        return _fake_record(counts={"passed": 1})

    got = _mission(repo, _writes({"src/pkg/writer.py": GREEN_WRITER + "\n"}),
                   _gate(repo, runner=spy), decomposer=_two_units, lanes=2)

    assert got.outcome == "ok", (got.reason, got.gates, got.suites)
    targeted = [c for c in calls if c[1] is False]
    full = [c for c in calls if c[1] is True]
    assert len(targeted) == 2, calls
    assert len(full) == 2, calls
    assert {Path(c[2]).name for c in full} == {"lane-0", "lane-1"}
    assert all(Path(c[0][-1]).name == "tests" for c in full), full
    assert len(got.suites) == 2
    assert {s["lane_id"] for s in got.suites} == {0, 1}
    assert all(s["ok"] and s["scope"] == gate_mod.FULL for s in got.suites)


def test_no_full_suite_runs_for_a_lane_that_validated_nothing(repo):
    calls = []

    def spy(argv, **kw):
        calls.append(kw.get("record"))
        return _fake_record(exit_code=1, counts={"failed": 1},
                            tail="1 failed in 0.10s")

    got = _mission(repo, _writes({"src/pkg/writer.py": BROKEN_WRITER}),
                   _gate(repo, runner=spy))

    assert got.outcome == "partial"
    assert calls == [False], "a suite ran on a lane whose only unit failed"
    assert got.suites == ()


def test_a_red_full_suite_makes_the_mission_partial(repo):
    def spy(argv, **kw):
        if kw.get("record"):
            return _fake_record(exit_code=1, counts={"failed": 3},
                                tail="3 failed, 40 passed in 9.10s")
        return _fake_record(counts={"passed": 1})

    got = _mission(repo, _writes({"src/pkg/writer.py": GREEN_WRITER + "\n"}),
                   _gate(repo, runner=spy))

    assert got.completed == ("t-writer",), "the unit itself validated"
    assert got.outcome == "partial"
    assert "full suite" in got.reason and "3 failed" in got.reason, got.reason
    assert got.suites[0]["ok"] is False


def test_a_targeted_run_is_not_a_ledger_verification_but_the_full_run_is(repo):
    got = _mission(repo, _writes({"src/pkg/writer.py": GREEN_WRITER + "\n"}),
                   _gate(repo))

    assert got.outcome == "ok", got.reason
    assert got.gates[0]["scope"] == gate_mod.TARGETED
    rows = [e for e in ev_mod.read_all() if e.type == "verification"]
    assert len(rows) == 1, (
        f"{len(rows)} verification row(s): a targeted run reached the ledger "
        f"as a tree verification")
    payload = rows[0].payload
    assert payload["scope"] == gate_mod.FULL
    assert payload["mission_id"] == got.mission_id
    assert payload["verified_by"] == "python"
    assert payload["exit_code"] == 0


def test_the_report_serialises_the_gate_rows(repo):
    import json

    got = _mission(repo, _writes({"src/pkg/writer.py": GREEN_WRITER + "\n"}),
                   _gate(repo, runner=lambda argv, **kw: _fake_record()))
    payload = json.loads(json.dumps(got.as_dict()))
    assert payload["gates"][0]["task_id"] == "t-writer"
    assert {"ok", "scope", "targets", "argv", "exit_code", "counts", "tail",
            "reason", "duration_s", "lane_id", "attempt"} <= set(payload["gates"][0])
    assert payload["suites"][0]["scope"] == gate_mod.FULL


# --- the wrapper on its own -------------------------------------------------------

class _Gate:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls: list = []

    def check(self, lane_path, task, attempt=0):
        self.calls.append((str(lane_path), attempt))
        return gate_mod.GateResult(ok=self.ok, scope=gate_mod.NONE,
                                   task_id=str(task.task_id), attempt=attempt)


def _entry(attempts=1):
    return q_mod.QueueEntry(task=lint.AgentTask(task_id="t-1"),
                            attempts=attempts)


def test_gated_runs_the_gate_after_a_green_child_only():
    gate = _Gate()
    wrapped = gate_mod.gated(lambda e, ln: False, gate=gate)
    assert wrapped(_entry(), q_mod.WorkerLane(0)) is False
    assert gate.calls == [], "a failed child reached the gate"

    kept = []
    wrapped = gate_mod.gated(lambda e, ln: True, gate=gate, record=kept)
    assert wrapped(_entry(attempts=2), q_mod.WorkerLane(0)) is True
    assert gate.calls == [("", 2)]
    assert kept and kept[0]["task_id"] == "t-1" and kept[0]["attempt"] == 2


def test_gated_turns_a_red_gate_into_the_units_failure():
    wrapped = gate_mod.gated(lambda e, ln: True, gate=_Gate(ok=False))
    out = wrapped(_entry(), q_mod.WorkerLane(0))
    assert q_mod._succeeded(out) is False
    assert out.executed is True


def test_gated_keeps_a_coroutine_runner_a_coroutine():
    async def runner(entry, lane):
        return True

    gate = _Gate()
    wrapped = gate_mod.gated(runner, gate=gate)
    assert inspect.iscoroutinefunction(wrapped)
    out = asyncio.run(wrapped(_entry(), q_mod.WorkerLane(0)))
    assert out is True and gate.calls == [("", 1)]


def test_gated_runs_after_only_past_a_green_gate():
    """PR-3's committer hangs on `after`. It must never see a unit the gate
    refused, and its own failure must never fail the unit."""
    seen = []

    def after(entry, lane, out):
        seen.append(entry.task_id)
        raise RuntimeError("the hook is broken")

    red = gate_mod.gated(lambda e, ln: True, gate=_Gate(ok=False), after=after)
    red(_entry(), q_mod.WorkerLane(0))
    assert seen == []

    green = gate_mod.gated(lambda e, ln: True, gate=_Gate(), after=after)
    assert green(_entry(), q_mod.WorkerLane(0)) is True
    assert seen == ["t-1"]


def test_gated_says_what_it_did_through_the_note_seam():
    notes = []
    wrapped = gate_mod.gated(lambda e, ln: True, gate=_Gate(),
                             emit=notes.append)
    wrapped(_entry(), q_mod.WorkerLane(0))
    assert notes and "t-1" in notes[0] and "ok" in notes[0]


# --- the helpers ------------------------------------------------------------------

@pytest.mark.parametrize("rel, expected", [
    ("src/mindhive/orchestrator/gate.py", "mindhive.orchestrator.gate"),
    ("src/mindhive/verify.py", "mindhive.verify"),
    ("src/pkg/__init__.py", "pkg"),
    ("src\\pkg\\writer.py", "pkg.writer"),
    ("pkg/writer.py", "pkg.writer"),
    ("README.md", ""),
    ("src/mindhive/data.json", ""),
    ("", ""),
])
def test_import_name_maps_a_source_path_to_its_module(rel, expected):
    assert gate_mod.import_name(rel) == expected


def test_tests_mentioning_matches_every_import_form_and_the_name_convention(
        tmp_path):
    tests = tmp_path / "tests"
    _write(tests, "test_a.py", "from mindhive.orchestrator import gate\n")
    _write(tests, "test_b.py", "from mindhive.orchestrator.gate import gated\n")
    _write(tests, "test_c.py", "import mindhive.orchestrator.gate as g\n")
    _write(tests, "test_d.py", "from mindhive.orchestrator import (\n"
                               "    lintester,\n    gate as gate_mod,\n)\n")
    _write(tests, "test_e.py", 'monkeypatch.setattr("mindhive.orchestrator.gate.x", 1)\n')
    _write(tests, "test_orchestrator_gate.py", "# named for it\n")
    _write(tests, "test_gate.py", "# named for it too\n")
    _write(tests, "test_unrelated.py", "from mindhive.orchestrator import queue\n")
    _write(tests, "test_gateway.py", "from mindhive.orchestrator import gateway\n")
    _write(tests, "helper.py", "from mindhive.orchestrator import gate\n")

    got = gate_mod.tests_mentioning(tests, ["mindhive.orchestrator.gate"])

    assert got == ["test_a.py", "test_b.py", "test_c.py", "test_d.py",
                   "test_e.py", "test_gate.py", "test_orchestrator_gate.py"]


def test_tests_mentioning_is_total_on_a_missing_or_junk_tree(tmp_path):
    assert gate_mod.tests_mentioning(tmp_path / "nope", ["a.b"]) == []
    assert gate_mod.tests_mentioning(tmp_path, []) == []
    tests = tmp_path / "tests"
    _write(tests, "test_bad.py", "from a import b\n")
    (tests / "test_bad.py").write_bytes(b"\xff\xfe from a import b")
    assert gate_mod.tests_mentioning(tests, ["a.b"]) == ["test_bad.py"]
