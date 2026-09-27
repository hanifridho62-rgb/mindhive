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


def test_a_new_module_with_no_test_fails_closed_and_names_the_path(repo):
    """THE reversal (blueprint P2, MEB ORCH-01). This was a recorded VACUOUS
    PASS: a unit that added an untested module was called validated, with a
    row saying `none` and a reason explaining that the closing full suite
    would judge it.

    Two things were wrong with that. The closing suite cannot fail a unit that
    already merged, so "the full suite is its judge" named a judge with no
    sentence to pass. And a child learns from the gate's answer: told its
    untested module passed, the cheapest way to satisfy the gate forever is to
    write modules no test mentions.

    So it refuses, and the refusal is an INSTRUCTION -- the exact path to
    create, not "add tests". A prescription a child can act on is what breaks
    the repair loop that otherwise retries the same unguided fix."""
    got = _mission(repo, _writes({"src/pkg/lonely.py": """
        def lonely():
            return 3
    """}), _gate(repo))

    row = got.gates[0]
    assert row["ok"] is False
    assert row["scope"] == gate_mod.GREENFIELD_NEEDS_TEST
    assert row["targets"] == []
    assert "MODUL BARU TERDETEKSI" in row["reason"], row["reason"]
    assert "src/pkg/lonely.py" in row["reason"]
    assert "'tests/test_lonely.py'" in row["reason"]
    assert row["tail"] == ("Instruksi: Tulis minimal satu pengujian unit di "
                           "tests/test_lonely.py.")
    assert got.outcome != "ok"


def test_the_prescribed_path_is_one_the_gate_would_actually_accept(repo):
    """The instruction has to be true. A child that creates exactly the file
    the refusal named must then PASS -- otherwise the gate sends children to a
    path it does not recognise and the loop never terminates."""
    got = _mission(repo, _writes({
        "src/pkg/lonely.py": """
            def lonely():
                return 3
        """,
        "tests/test_lonely.py": """
            from pkg import lonely


            def test_lonely():
                assert lonely.lonely() == 3
        """}), _gate(repo))

    row = got.gates[0]
    assert row["ok"] is True, row["reason"]
    assert row["scope"] == gate_mod.TARGETED
    assert [Path(p).name for p in row["targets"]] == ["test_lonely.py"]
    assert got.outcome == "ok", got.reason


def test_a_change_no_module_owns_is_refused_unless_overridden(repo):
    """The second refusal: files no test could ever mention — a README, a
    migration, a config. There is no path to prescribe, so this one IS
    overridable, and the message names the flag that does it."""
    got = _mission(repo, _writes({"README.md": "# notes\n"}), _gate(repo))

    row = got.gates[0]
    assert row["ok"] is False
    assert row["scope"] == gate_mod.NONE
    assert "PELANGGARAN GATE" in row["reason"], row["reason"]
    assert "--allow-untested" in row["reason"]
    assert got.outcome != "ok"


def test_the_override_passes_the_unit_and_says_so_in_its_own_scope(repo):
    """Overridden is not the same fact as tested, so it gets its own scope
    rather than reusing `none`. An operator reading a report can then count
    them — see `count_scopes`."""
    got = _mission(repo, _writes({"README.md": "# notes\n"}),
                   _gate(repo, allow_untested=True))

    row = got.gates[0]
    assert row["ok"] is True, row["reason"]
    assert row["scope"] == gate_mod.UNTESTED_OVERRIDE
    assert "--allow-untested" in row["reason"] or "override" in row["reason"]
    assert got.completed == ("t-writer",)
    assert got.outcome == "ok", got.reason


def test_the_override_does_not_excuse_a_new_untested_module(repo):
    """`--allow-untested` is for work no test could name. A new module always
    can be named — the gate just told the child the path — so the override
    deliberately does not reach it. The way out is to write the file."""
    got = _mission(repo, _writes({"src/pkg/lonely.py": """
        def lonely():
            return 3
    """}), _gate(repo, allow_untested=True))

    assert got.gates[0]["ok"] is False
    assert got.gates[0]["scope"] == gate_mod.GREENFIELD_NEEDS_TEST


# --- which files count as greenfield -------------------------------------------

def test_the_prescription_is_the_repos_own_short_test_name():
    assert gate_mod.canonical_test_path("src/pkg/lonely.py") \
        == "tests/test_lonely.py"
    assert gate_mod.canonical_test_path("src\\pkg\\lonely.py") \
        == "tests/test_lonely.py"


def test_the_prescribed_name_is_one_the_target_search_recognises():
    """The two halves of the same promise: `_named_for` is what accepts a
    test file by NAME, so the name the refusal prescribes has to be in it."""
    assert Path(gate_mod.canonical_test_path("src/pkg/lonely.py")).name \
        in gate_mod._named_for("pkg.lonely")


def test_a_deleted_module_is_not_greenfield(tmp_path):
    """A child cannot be told to write a test for a file it removed, and
    telling it to would be an instruction that can never be satisfied."""
    _write(tmp_path, "src/pkg/kept.py", "x = 1\n")
    assert gate_mod.greenfield_files(
        tmp_path, ["src/pkg/kept.py", "src/pkg/gone.py"]) == ["src/pkg/kept.py"]


def test_a_package_marker_is_not_greenfield(tmp_path):
    """`tests/test___init__.py` is not an instruction anyone should be given,
    and a package marker is not a module with behaviour to cover."""
    _write(tmp_path, "src/pkg/__init__.py", "")
    assert gate_mod.greenfield_files(tmp_path, ["src/pkg/__init__.py"]) == []


def test_tests_and_non_python_files_are_not_greenfield(tmp_path):
    _write(tmp_path, "tests/test_thing.py", "")
    _write(tmp_path, "docs/notes.md", "")
    _write(tmp_path, "src/pkg/data.sql", "")
    assert gate_mod.greenfield_files(
        tmp_path, ["tests/test_thing.py", "docs/notes.md",
                   "src/pkg/data.sql"]) == []


def test_the_greenfield_list_is_sorted_so_one_message_is_deterministic(tmp_path):
    """The refusal names ONE file, and `resilience` hashes the reason into the
    failure signature the breaker counts. A set-ordered pick would make two
    runs of one failure read as two different failures."""
    for rel in ("src/pkg/b.py", "src/pkg/a.py", "src/pkg/c.py"):
        _write(tmp_path, rel, "x = 1\n")
    order = ["src/pkg/c.py", "src/pkg/a.py", "src/pkg/b.py"]
    assert gate_mod.greenfield_files(tmp_path, order)[0] == "src/pkg/a.py"


# --- counting the scopes for a report ------------------------------------------

def test_count_scopes_totals_the_rows_by_scope():
    rows = [{"scope": "targeted"}, {"scope": "targeted"},
            {"scope": "untested_override"}, {"scope": "none"}]
    assert gate_mod.count_scopes(rows) == {"targeted": 2,
                                           "untested_override": 1,
                                           "none": 1}


def test_count_scopes_is_total_over_junk():
    """It runs at report time, after a mission. A malformed row must not be
    the thing that stops an operator seeing the rest."""
    assert gate_mod.count_scopes(None) == {}
    assert gate_mod.count_scopes([None, {}, {"scope": ""}, 7,
                                  {"scope": "full"}]) == {"full": 1}


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
    # `allow_test_edits` because this child's job IS to edit an existing test
    # -- a hang has to be written into the file the gate would run. Without it
    # K13 refuses before pytest starts and the cap never gets to fire.
    started = time.monotonic()
    got = _mission(repo, _writes({"tests/test_writer.py": """
        import time


        def test_write():
            time.sleep(30)
    """}), _gate(repo, unit_timeout_s=2, allow_test_edits=True))

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


# --- K13: existing tests are immutable during implementation -------------------

def test_a_child_that_edits_an_existing_test_is_refused(repo):
    """K13 Immutable Test Gates. A red test has exactly two cures: fix the
    code, or change what the test demands. The second one is always available
    and always cheaper, and a child under a repair loop will find it -- so a
    gate that only runs pytest can be satisfied by deleting the assertion that
    was failing.

    The baseline is the commit the lane stood at before the unit's FIRST
    attempt, pinned by `SuiteGate.begin` through `gated`. A snapshot taken
    when the gate first runs would be taken after the first attempt had
    already edited the file; the lane's `HEAD` at gate time moves with a
    child that commits (see the committing-child tests below)."""
    got = _mission(repo, _writes({
        "src/pkg/writer.py": GREEN_WRITER,
        "tests/test_writer.py": """
            def test_writer():
                assert True
        """}), _gate(repo))

    row = got.gates[0]
    assert row["ok"] is False, row["reason"]
    assert row["scope"] == gate_mod.IMMUTABLE_TESTS_VIOLATED
    assert "PELANGGARAN GATE" in row["reason"]
    assert "tests/test_writer.py" in row["reason"]
    assert "--allow-test-edits" in row["reason"]
    assert got.outcome != "ok"


def test_the_flag_is_what_lets_a_test_edit_through(repo):
    got = _mission(repo, _writes({
        "src/pkg/writer.py": GREEN_WRITER,
        "tests/test_writer.py": """
            from pkg import writer


            def test_writer():
                assert writer.write(1) == 1
        """}), _gate(repo, allow_test_edits=True))

    assert got.gates[0]["ok"] is True, got.gates[0]["reason"]
    assert got.gates[0]["scope"] == gate_mod.TARGETED
    assert got.outcome == "ok", got.reason


def test_turning_the_gate_off_entirely_also_lets_it_through(repo):
    got = _mission(repo, _writes({
        "src/pkg/writer.py": GREEN_WRITER,
        "tests/test_writer.py": """
            from pkg import writer


            def test_writer():
                assert writer.write(1) == 1
        """}), _gate(repo, immutable_tests=False))

    assert got.gates[0]["ok"] is True, got.gates[0]["reason"]
    assert got.outcome == "ok", got.reason


def test_a_brand_new_test_file_is_never_a_violation(repo):
    """The greenfield flow ENDS in a child creating a test file. If that
    counted as mutating the suite, the instruction the other half of this gate
    hands out would be refused the moment it was followed."""
    got = _mission(repo, _writes({
        "src/pkg/lonely.py": """
            def lonely():
                return 3
        """,
        "tests/test_lonely.py": """
            from pkg import lonely


            def test_lonely():
                assert lonely.lonely() == 3
        """}), _gate(repo))

    assert got.gates[0]["ok"] is True, got.gates[0]["reason"]
    assert got.gates[0]["scope"] == gate_mod.TARGETED


def test_the_violation_is_checked_before_a_single_test_runs(repo):
    """Refusing AFTER running the suite would report the doctored suite's
    green as the unit's result for as long as it took to notice."""
    ran = []

    def _spy(argv, **kw):                                # pragma: no cover - argv
        ran.append(list(argv))
        return {"exit_code": 0, "counts": {"passed": 1}, "tail": "1 passed"}

    got = _mission(repo, _writes({
        "src/pkg/writer.py": GREEN_WRITER,
        "tests/test_writer.py": "def test_writer():\n    assert True\n"}),
        _gate(repo, runner=_spy))

    assert got.gates[0]["scope"] == gate_mod.IMMUTABLE_TESTS_VIOLATED
    assert ran == [], f"the gate ran pytest anyway: {ran}"


def test_a_deleted_test_counts_as_mutated_too(repo):
    """Deleting the file that judges you is the same move as emptying it."""
    def _delete(entry, lane):
        (Path(lane.worktree_path) / "tests" / "test_writer.py").unlink()
        return True

    got = _mission(repo, _delete, _gate(repo))
    row = got.gates[0]
    assert row["ok"] is False
    assert row["scope"] == gate_mod.IMMUTABLE_TESTS_VIOLATED
    assert "tests/test_writer.py" in row["reason"]


# --- K13: the whole judge, not just `test_*.py` ----------------------------------

@pytest.fixture
def judged(repo):
    """`repo` plus the rest of what judges a lane: a conftest at the root and
    one under `tests/`, a data fixture, and a `pyproject.toml` whose pytest
    section sits beside a `[project]` table -- the section is what makes
    pytest root itself here instead of walking up out of the lane."""
    _write(repo, "tests/conftest.py", """
        import pytest


        @pytest.fixture
        def expected():
            return 1
    """)
    _write(repo, "conftest.py", "# the root plugin every test below loads\n")
    _write(repo, "tests/data/expected.json", '{"write": 1}\n')
    _write(repo, "pyproject.toml", """
        [project]
        name = "pkg"
        version = "0.1.0"
        dependencies = []

        [tool.pytest.ini_options]
        markers = ["slow: a slow test"]
    """)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the rest of the judge")
    return repo


def _refused_for(got, rel):
    row = got.gates[0]
    assert row["ok"] is False, row["reason"]
    assert row["scope"] == gate_mod.IMMUTABLE_TESTS_VIOLATED, row
    assert rel in row["reason"], row["reason"]
    assert got.outcome != "ok"


def test_editing_a_conftest_under_tests_is_refused(judged):
    """An autouse fixture in a conftest can make every assertion below it
    pass without a single test file changing."""
    got = _mission(judged, _writes({
        "src/pkg/writer.py": GREEN_WRITER,
        "tests/conftest.py": """
            import pytest


            @pytest.fixture(autouse=True)
            def _green(monkeypatch):
                from pkg import writer
                monkeypatch.setattr(writer, "write", lambda x: 1)
        """}), _gate(judged))
    _refused_for(got, "tests/conftest.py")


def test_editing_the_root_conftest_is_refused(judged):
    got = _mission(judged, _writes({
        "src/pkg/writer.py": GREEN_WRITER,
        "conftest.py": "def pytest_collection_modifyitems(items):\n"
                       "    items[:] = []\n"}), _gate(judged))
    _refused_for(got, "conftest.py")


def test_editing_a_data_fixture_the_tests_compare_against_is_refused(judged):
    got = _mission(judged, _writes({
        "src/pkg/writer.py": GREEN_WRITER,
        "tests/data/expected.json": '{"write": 0}\n'}), _gate(judged))
    _refused_for(got, "tests/data/expected.json")


def test_the_pytest_section_of_pyproject_is_part_of_the_judge(judged):
    text = (judged / "pyproject.toml").read_text(encoding="utf-8")
    got = _mission(judged, _writes({
        "src/pkg/writer.py": BROKEN_WRITER,
        "pyproject.toml": text + 'addopts = "--deselect '
                                 'tests/test_writer.py::test_write"\n'}),
        _gate(judged))
    _refused_for(got, "pyproject.toml")


def test_the_rest_of_pyproject_is_not(judged):
    """A dependency bump or a version change is the work, not the judge."""
    text = (judged / "pyproject.toml").read_text(encoding="utf-8")
    got = _mission(judged, _writes({
        # A real change to the module, still green: the fixture's writer IS
        # `GREEN_WRITER`, and rewriting it unchanged would leave the version
        # bump as the lane's only change -- which no test names.
        "src/pkg/writer.py": """
            RELEASE = "0.2.0"


            def write(x):
                return 1
        """,
        "pyproject.toml": text.replace('version = "0.1.0"',
                                       'version = "0.2.0"')}), _gate(judged))
    assert got.gates[0]["ok"] is True, got.gates[0]["reason"]
    assert got.gates[0]["scope"] == gate_mod.TARGETED
    assert got.outcome == "ok", got.reason


def test_a_new_conftest_is_a_judge_change_not_greenfield(repo):
    """"A new test file is always allowed" is about test MODULES. A new
    conftest is a plugin for every test below it, and existing is enough."""
    got = _mission(repo, _writes({
        "src/pkg/writer.py": BROKEN_WRITER,
        "tests/conftest.py": """
            import pytest


            @pytest.fixture(autouse=True)
            def _green(monkeypatch):
                from pkg import writer
                monkeypatch.setattr(writer, "write", lambda x: 1)
        """}), _gate(repo))
    _refused_for(got, "tests/conftest.py")


def test_a_staged_new_test_file_is_not_a_violation(repo):
    """Staged is how a lane looks after `commit_all` added everything and the
    commit itself was refused. A new file is new whether or not it is in the
    index."""
    def runner(entry, lane):
        _write(lane.worktree_path, "src/pkg/lonely.py", """
            def lonely():
                return 3
        """)
        _write(lane.worktree_path, "tests/test_lonely.py", """
            from pkg import lonely


            def test_lonely():
                assert lonely.lonely() == 3
        """)
        _git(lane.worktree_path, "add", "-A")
        return True

    got = _mission(repo, runner, _gate(repo))
    assert got.gates[0]["ok"] is True, got.gates[0]["reason"]
    assert got.gates[0]["scope"] == gate_mod.TARGETED


# --- K13: a child that commits cannot move the baseline ---------------------------

def _commits(files):
    """A child that writes *files* and then COMMITS them in its lane -- which
    `mindnet/agent.py` tells it not to do, and which nothing prevents."""
    def runner(entry, lane):
        for rel, text in files.items():
            _write(lane.worktree_path, rel, text)
        _git(lane.worktree_path, "add", "-A")
        _git(lane.worktree_path, "commit", "-qm", "the child committed")
        return True
    return runner


def test_a_child_that_commits_its_test_edit_is_still_refused(repo):
    """With the lane's `HEAD` as the baseline this passed: the commit made
    `HEAD` agree with the edit, and `diff HEAD` answered that nothing changed."""
    got = _mission(repo, _commits({
        "src/pkg/writer.py": BROKEN_WRITER,
        "tests/test_writer.py": "def test_write():\n    assert True\n"}),
        _gate(repo))
    _refused_for(got, "tests/test_writer.py")


def test_a_committed_module_with_no_test_is_still_greenfield(repo):
    """The fail-closed half has the same door: against `HEAD` a committed new
    module is no change at all, the gate falls back to the unit's own files,
    and their tests pass it -- the vacuous pass again."""
    got = _mission(repo, _commits({"src/pkg/lonely.py": """
        def lonely():
            return 3
    """}), _gate(repo))
    row = got.gates[0]
    assert row["ok"] is False, row["reason"]
    assert row["scope"] == gate_mod.GREENFIELD_NEEDS_TEST, row
    assert "tests/test_lonely.py" in row["reason"]


def test_the_baseline_is_the_units_first_attempt_not_the_latest(repo):
    """Rollback keeps commits. If each attempt pinned its own baseline, the
    edit a child committed in attempt 1 would BE attempt 2's baseline, and
    attempt 2 would pass on the doctored test it inherited."""
    calls = []

    def runner(entry, lane):
        calls.append(len(calls))
        if len(calls) == 1:
            return _commits({"tests/test_writer.py":
                             "def test_write():\n    assert True\n"})(entry, lane)
        return _writes({"src/pkg/writer.py": GREEN_WRITER})(entry, lane)

    got = _mission(repo, runner, _gate(repo), max_attempts=2)
    assert len(calls) == 2, "the repair loop did not retry"
    assert [row["scope"] for row in got.gates] == [
        gate_mod.IMMUTABLE_TESTS_VIOLATED] * 2, got.gates
    assert got.outcome != "ok"


def test_a_lane_git_cannot_read_is_refused_not_passed(repo, monkeypatch):
    monkeypatch.setattr(gate_mod, "tests_mutated", lambda *a, **k: None)
    got = _mission(repo, _writes({"src/pkg/writer.py": GREEN_WRITER}),
                   _gate(repo))
    row = got.gates[0]
    assert row["ok"] is False
    assert row["scope"] == gate_mod.NONE
    assert "could not verify" in row["reason"]
    assert "--allow-test-edits" in row["reason"]


@pytest.mark.parametrize("switch", [{"allow_test_edits": True},
                                    {"immutable_tests": False}])
def test_either_switch_skips_the_check_git_could_not_answer(repo, monkeypatch,
                                                           switch):
    """Both switches bypass the immutability check as a whole, so neither is
    stopped by a git that cannot answer it."""
    monkeypatch.setattr(gate_mod, "tests_mutated", lambda *a, **k: None)
    got = _mission(repo, _writes({"src/pkg/writer.py": GREEN_WRITER}),
                   _gate(repo, **switch))
    assert got.gates[0]["ok"] is True, got.gates[0]["reason"]
    assert got.gates[0]["scope"] == gate_mod.TARGETED


# --- the pin itself ---------------------------------------------------------------

def _task(tid="t-pin"):
    return lint.AgentTask(task_id=tid)


def test_begin_pins_the_first_attempt_and_only_the_first(repo):
    gate = _gate(repo)
    lane = wt_mod.acquire(repo, 0, "m-pin")
    assert lane.ok, lane.reason
    try:
        first = gate.begin(lane.path, _task())
        assert first and first == _git(lane.path, "rev-parse", "HEAD").stdout.strip()
        assert gate.baseline(lane.path, _task()) == first

        _write(lane.path, "later.txt", "x\n")
        _git(lane.path, "add", "-A")
        _git(lane.path, "commit", "-qm", "moved")
        assert gate.begin(lane.path, _task()) == first, "a later attempt re-pinned"
        assert gate.baseline(lane.path, _task()) == first
        assert gate.baseline(lane.path, _task("t-other")) == "HEAD"
    finally:
        wt_mod.release(repo, lane)


def test_begin_outside_a_lane_pins_nothing_and_runs_no_git(repo, monkeypatch):
    from mindhive.mindnet import worktree as mn_wt
    ran = []
    monkeypatch.setattr(mn_wt, "rev_of", lambda *a: ran.append(a) or "abc")
    gate = _gate(repo)
    assert gate.begin(str(repo), _task()) == ""
    assert gate.begin("", _task()) == ""
    assert gate.begin(None, _task()) == ""
    assert ran == [] and gate.baseline(str(repo), _task()) == "HEAD"


def test_a_unit_whose_start_could_not_be_pinned_is_refused(repo, monkeypatch):
    """`begin` asked git for the lane's commit and got nothing. Falling back to
    the lane's `HEAD` would reopen the door the pin exists to close -- a child
    that commits its edit moves `HEAD` -- so the unit is refused. And the
    failed pin is RECORDED: a later attempt pinning then would pin a commit
    made after the unit began, absorbing whatever the first attempt committed."""
    from mindhive.mindnet import worktree as mn_wt

    lane = wt_mod.acquire(repo, 0, "m-nopin")
    assert lane.ok, lane.reason
    real, broken = mn_wt.rev_of, {"on": True}
    monkeypatch.setattr(mn_wt, "rev_of", lambda root, ref: (
        "" if broken["on"] else real(root, ref)))
    try:
        gate = _gate(repo)
        assert gate.begin(lane.path, _task()) == ""
        broken["on"] = False
        assert gate.begin(lane.path, _task()) == "", "a later attempt pinned"
        assert gate.baseline(lane.path, _task()) == ""

        res = gate.check(lane.path, _task())
        assert res.ok is False and res.scope == gate_mod.NONE, res
        assert "could not verify" in res.reason
        assert "started from" in res.reason
        assert "--allow-test-edits" in res.reason

        # The switch stands the whole check down, and the gate then judges
        # the change against HEAD like any caller that never pinned.
        broken["on"] = True
        waived = _gate(repo, allow_test_edits=True)
        assert waived.begin(lane.path, _task()) == ""
        _write(lane.path, "src/pkg/writer.py", """
            def write(x):
                return 1  # unchanged behaviour, changed file
        """)
        broken["on"] = False
        res = waived.check(lane.path, _task())
        assert res.ok is True and res.scope == gate_mod.TARGETED, res.reason
    finally:
        wt_mod.release(repo, lane)


class _PinGate(_Gate):
    def __init__(self, log):
        super().__init__()
        self.log = log

    def begin(self, lane_path, task):
        self.log.append(("begin", str(lane_path)))

    def check(self, lane_path, task, attempt=0):
        self.log.append(("check", str(lane_path)))
        return super().check(lane_path, task, attempt)


def test_gated_tells_the_gate_before_the_child_runs():
    log = []

    def runner(entry, lane):
        log.append(("runner", ""))
        return True

    lane = q_mod.WorkerLane(
        0, worktree=wt_mod.LaneWorktree(lane_id=0, path="/lanes/lane-0"))
    gate_mod.gated(runner, gate=_PinGate(log))(_entry(), lane)
    assert [step for step, _ in log] == ["begin", "runner", "check"]
    assert log[0][1] == log[2][1], "begin and check saw different lanes"


def test_gated_tells_a_coroutine_runners_gate_first_too():
    log = []

    async def runner(entry, lane):
        log.append(("runner", ""))
        return True

    wrapped = gate_mod.gated(runner, gate=_PinGate(log))
    asyncio.run(wrapped(_entry(), q_mod.WorkerLane(0)))
    assert [step for step, _ in log] == ["begin", "runner", "check"]


def test_a_begin_that_raises_never_stops_the_child():
    class _Broken(_Gate):
        def begin(self, lane_path, task):
            raise RuntimeError("the pin is broken")

    ran = []
    gate = _Broken()
    out = gate_mod.gated(lambda e, ln: ran.append(1) or True, gate=gate)(
        _entry(), q_mod.WorkerLane(0))
    assert ran == [1] and out is True and gate.calls


# --- P2 round 2: the baseline a lane's next unit inherits -------------------------
#
# `begin` pins the lane's HEAD before a unit's first attempt. On a lane that
# ran an earlier unit, that HEAD may hold what the earlier unit COMMITTED --
# rollback keeps commits -- and a doctored test committed under a refused unit
# would then be the next unit's baseline: judged clean, and green. So a lane
# has a TRUSTED commit, and HEAD becomes the next pin only when nothing of the
# judge changed between the two (`judge.judge_changes`); otherwise the trusted
# commit stays the pin, and the committed edit stays the lane's change until
# it is undone.

def _doctor(lane_path):
    _write(lane_path, "tests/test_writer.py",
           "def test_write():\n    assert True\n")
    _git(lane_path, "add", "-A")
    _git(lane_path, "commit", "-qm", "the first unit's child doctored a test")


def test_a_judge_edit_an_earlier_unit_committed_is_the_next_units_change_too(
        repo):
    gate = _gate(repo)
    lane = wt_mod.acquire(repo, 0, "m-inherit")
    assert lane.ok, lane.reason
    try:
        first = gate.begin(lane.path, _task("t-a"))
        _doctor(lane.path)
        res = gate.check(lane.path, _task("t-a"))
        assert res.scope == gate_mod.IMMUTABLE_TESTS_VIOLATED, "canary"

        assert gate.begin(lane.path, _task("t-b")) == first, (
            "the next unit was pinned at the doctored commit")
        res = gate.check(lane.path, _task("t-b"))
        assert res.ok is False, res
        assert res.scope == gate_mod.IMMUTABLE_TESTS_VIOLATED, res
        assert "tests/test_writer.py" in res.reason
    finally:
        wt_mod.release(repo, lane)


def test_validated_work_an_earlier_unit_committed_moves_the_baseline_forward(
        repo):
    """The other half: what `commit_lane` commits past a green gate -- source,
    and the test module the greenfield refusal asked for -- leaves the judge
    as it was, and the next unit starts from there, not from a stale pin that
    would make it answer for the earlier unit's work."""
    gate = _gate(repo)
    lane = wt_mod.acquire(repo, 0, "m-advance")
    assert lane.ok, lane.reason
    try:
        first = gate.begin(lane.path, _task("t-a"))
        _write(lane.path, "src/pkg/lonely.py", "def lonely():\n    return 3\n")
        _write(lane.path, "tests/test_lonely.py", """
            from pkg import lonely


            def test_lonely():
                assert lonely.lonely() == 3
        """)
        _git(lane.path, "add", "-A")
        _git(lane.path, "commit", "-qm", "lint(m-advance) t-a: validated work")
        moved = _git(lane.path, "rev-parse", "HEAD").stdout.strip()
        assert moved != first

        assert gate.begin(lane.path, _task("t-b")) == moved
        _write(lane.path, "src/pkg/writer.py", GREEN_WRITER + "\n")
        res = gate.check(lane.path, _task("t-b"))
        assert res.ok is True and res.scope == gate_mod.TARGETED, res.reason
        assert res.targets == ("tests/test_writer.py",), res.targets
    finally:
        wt_mod.release(repo, lane)


def test_a_history_git_cannot_compare_pins_nothing(repo, monkeypatch):
    """Unknown is not clean: when "did the judge change since the trusted
    commit" has no answer, the unit is refused as one whose start could not
    be pinned -- not judged from a HEAD the gate cannot vouch for."""
    from mindhive.orchestrator import judge

    gate = _gate(repo)
    lane = wt_mod.acquire(repo, 0, "m-unknown")
    assert lane.ok, lane.reason
    try:
        first = gate.begin(lane.path, _task("t-a"))
        _write(lane.path, "src/pkg/writer.py", GREEN_WRITER + "\n")
        _git(lane.path, "add", "-A")
        _git(lane.path, "commit", "-qm", "moved on")
        monkeypatch.setattr(judge, "judge_changes", lambda *a, **k: None)
        assert gate.begin(lane.path, _task("t-b")) == ""
        res = gate.check(lane.path, _task("t-b"))
        assert res.ok is False and res.scope == gate_mod.NONE, res
        assert "could not verify" in res.reason
        assert gate.begin(lane.path, _task("t-a")) == first, "the first pin moved"
    finally:
        wt_mod.release(repo, lane)


def test_the_next_unit_on_a_lane_is_refused_for_the_doctored_test_it_inherited(
        repo):
    """End to end: two units, ONE lane. The first unit's child doctors a test
    and commits it; the gate refuses it, the repair loop rolls back what was
    uncommitted, and the commit stays. Judged from the lane's HEAD, the
    second unit would have been green on the doctored suite."""
    seen = []

    def runner(entry, lane):
        seen.append(entry.task.task_id)
        if len(seen) == 1:
            _write(lane.worktree_path, "src/pkg/writer.py", BROKEN_WRITER)
            _doctor(lane.worktree_path)
        return True

    got = _mission(repo, runner, _gate(repo), decomposer=_two_units, lanes=1)
    assert len(seen) == 2, seen
    rows = {row["task_id"]: row for row in got.gates}
    assert set(rows) == set(seen), got.gates
    for tid in seen:
        assert rows[tid]["scope"] == gate_mod.IMMUTABLE_TESTS_VIOLATED, rows[tid]
        assert "tests/test_writer.py" in rows[tid]["reason"]
    assert got.outcome != "ok"


# --- P2 round 2, review fixes: the pin, the pair, and one read of the lane -------

def test_a_lane_whose_first_pin_failed_never_trusts_a_later_head(repo,
                                                                 monkeypatch):
    """The first unit on a lane could not be pinned -- git answered nothing --
    and was refused (above). Its child ran anyway, doctored a test and
    committed it. The NEXT unit's `begin` then found a lane with no trusted
    commit and took `HEAD`, the doctored commit, on trust. A lane nothing
    ever vouched for stays unvouched: every later unit on it is refused too,
    until the check is stood down."""
    from mindhive.mindnet import worktree as mn_wt

    lane = wt_mod.acquire(repo, 0, "m-unvouched")
    assert lane.ok, lane.reason
    real, broken = mn_wt.rev_of, {"on": True}
    monkeypatch.setattr(mn_wt, "rev_of", lambda root, ref: (
        "" if broken["on"] else real(root, ref)))
    try:
        gate = _gate(repo)
        assert gate.begin(lane.path, _task("t-a")) == ""
        _doctor(lane.path)
        broken["on"] = False
        assert gate.begin(lane.path, _task("t-b")) == "", (
            "the next unit was pinned at a HEAD nothing vouched for")
        res = gate.check(lane.path, _task("t-b"))
        assert res.ok is False and res.scope == gate_mod.NONE, res
        assert "could not verify" in res.reason

        # Stood down, there is nothing to inherit and HEAD is the pin.
        waived = _gate(repo, allow_test_edits=True)
        assert waived.begin(lane.path, _task("t-b")) == real(lane.path, "HEAD")
    finally:
        wt_mod.release(repo, lane)


def test_a_module_and_a_test_side_twin_committed_together_move_the_baseline(
        repo):
    """`test_validated_work_an_earlier_unit_committed_moves_the_baseline_forward`
    with the pair named ALIKE: `src/twin.py` and a `tests/twin.py` helper. The
    unit's own check let the pair through (nothing the run imported was
    replaced), but read with the names the later commit holds, the test-side
    twin outranked the src-side one and `begin` never trusted the commit --
    the lane's baseline stopped there for good."""
    gate = _gate(repo)
    lane = wt_mod.acquire(repo, 0, "m-twins")
    assert lane.ok, lane.reason
    try:
        first = gate.begin(lane.path, _task("t-a"))
        _write(lane.path, "src/twin.py", "def twin():\n    return 4\n")
        _write(lane.path, "tests/twin.py", "EXPECTED = 4\n")
        res = gate.check(lane.path, _task("t-a"))
        assert res.scope != gate_mod.IMMUTABLE_TESTS_VIOLATED, res.reason
        _git(lane.path, "add", "-A")
        _git(lane.path, "commit", "-qm", "lint(m-twins) t-a: the pair")
        moved = _git(lane.path, "rev-parse", "HEAD").stdout.strip()
        assert moved != first

        assert gate.begin(lane.path, _task("t-b")) == moved
    finally:
        wt_mod.release(repo, lane)


def test_the_gate_asks_git_once_per_check_which_files_the_unit_touched(
        repo, monkeypatch):
    """`targets` and the greenfield search read ONE list, so a lane that
    changes underneath the check cannot make the refusal name a file the
    target search never saw -- and the second spawn was half the gate's git
    cost on the untested path."""
    from mindhive.mindnet import worktree as mn_wt

    calls, real = [], mn_wt.changed_files
    monkeypatch.setattr(mn_wt, "changed_files",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    gate = _gate(repo)
    lane = wt_mod.acquire(repo, 0, "m-once")
    assert lane.ok, lane.reason
    try:
        gate.begin(lane.path, _task())
        _write(lane.path, "src/pkg/lonely.py", "def lonely():\n    return 3\n")
        res = gate.check(lane.path, _task())
        assert res.scope == gate_mod.GREENFIELD_NEEDS_TEST, res
        assert len(calls) == 1, calls
    finally:
        wt_mod.release(repo, lane)


def test_a_lane_whose_changes_git_cannot_list_is_refused_not_judged_by_its_files(
        repo, monkeypatch):
    """`changed_files` answering ``None`` is "git could not say", and it used
    to fall through to the unit's own files: a stale list that judged a
    child's real change by the tests of the files it was SENT to edit. The
    same refusal an unreadable lane gets everywhere else in the gate."""
    from mindhive.mindnet import worktree as mn_wt

    monkeypatch.setattr(mn_wt, "changed_files", lambda *a, **k: None)
    gate = _gate(repo)
    lane = wt_mod.acquire(repo, 0, "m-unlisted")
    assert lane.ok, lane.reason
    try:
        task = lint.AgentTask(task_id="t-writer",
                              files=(str(repo / "src" / "pkg" / "writer.py"),))
        gate.begin(lane.path, task)
        _write(lane.path, "src/pkg/writer.py", GREEN_WRITER + "\n")
        res = gate.check(lane.path, task)
        assert res.ok is False and res.scope == gate_mod.NONE, res
        assert "could not read the lane's changes" in res.reason
    finally:
        wt_mod.release(repo, lane)
