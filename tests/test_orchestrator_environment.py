# tests/test_orchestrator_environment.py
"""`orchestrator.environment` -- a machine that cannot start a child is not a
task that failed, and the mission has to tell the two apart.

The defect these pin was observed, not imagined. On 2026-09-07 the `claude` CLI
on this machine answered ``Not logged in · Please run /login`` at exit 1;
`mindnet.agent._classify` reported it correctly as `refused` with the child
never started, and `lintester._run_child` folded that into `fail`. From there
the repair loop did everything a failure deserves -- a `fall_back` ledger row,
the refusal quoted into a retry prompt, a lane rollback, `max_attempts` retries
per unit, a breaker opened per task, a reflection spent on each opening -- and
every lane kept dispatching, because nothing told the scheduler that the ninth
refusal would say what the first had.

Three layers, each with its own guard here:

* the classifier (`environment_failure`), with CANARIES for the two shapes it
  must NOT claim -- a child that ran and printed the phrase, and a task's own
  traceback naming a missing file;
* the repair loop's short-circuit and the scheduler's `halt`, asserted on what
  they do NOT do as much as on what they do;
* the mission, end to end on real lane worktrees, answering `environment` with
  zero calibration samples and zero fall-back rows.
"""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from mindhive.orchestrator import environment as env
from mindhive.orchestrator import lintester as lint
from mindhive.orchestrator import queue as q
from mindhive.orchestrator import resilience as r

# Verbatim from `mindnet.agent._classify` on this machine, 2026-09-07.
NOT_LOGGED_IN = ("[usage] the agent could not run: Not logged in · Please run "
                 "/login — nothing was attempted, so the task is left untouched")
# Verbatim from the Windows Store redirector, same day, same machine.
STORE_STUB = ("Python was not found; run without arguments to install from the "
              "Microsoft Store, or disable this shortcut from Settings > Apps > "
              "Advanced app settings > App execution aliases.")


# --- fixtures -----------------------------------------------------------------

class _Task:
    def __init__(self, task_id="t1", description="do it"):
        self.task_id = task_id
        self.description = description
        self.files = ()
        self.weight = 1

    def with_repair(self, suffix):
        out = _Task(self.task_id, f"{self.description}\n\n{suffix}")
        return out


def _res(result="refused", reason="", stderr="", started=False, code=None):
    """A runner's answer, shaped like `executor.RunResult`."""
    return SimpleNamespace(result=result, reason=reason, stderr=stderr,
                           executed=started, exit_code=code,
                           ok=(result == "ok"))


def _entry(task_id="t1", result=None, attempts=1, reason=""):
    e = q.QueueEntry(task=_Task(task_id))
    e.attempts = attempts
    e.result = result
    e.reason = reason or str(getattr(result, "reason", "") or "")
    e.state = q.FAILED
    return e


def _fresh(task_id):
    return q.QueueEntry(task=_Task(task_id))


def _breaker():
    return r.ToolCircuitBreaker(same_error=2, no_progress=3, cooldown_s=10.0,
                                clock=lambda: 0.0)


class _Bus:
    def __init__(self):
        self.events = []

    def emit(self, kind, **fields):
        self.events.append((kind, fields))


def _git(cwd, *args):
    out = subprocess.run(["git", "-C", str(cwd), *[str(a) for a in args]],
                         capture_output=True, text=True, shell=False)
    assert out.returncode == 0, f"git {' '.join(map(str, args))}: {out.stderr}"
    return out


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-qm", "seed")
    return root


@pytest.fixture(autouse=True)
def brief_on(monkeypatch):
    monkeypatch.setenv("MINDHIVE_BRIEF", "1")
    monkeypatch.setenv("MINDHIVE_SESSION", "s1")


def _two_units(_root_id, _title, _intent):
    return [
        ("t-worktree", "Serialise the worktree add",
         "worktree add lock concurrent thread pool collides"),
        ("t-ledger", "Rotate the ledger segment",
         "ledger segment rotation append path stays fast"),
    ]


# --- the classifier -----------------------------------------------------------

def test_a_refused_child_that_was_not_logged_in_is_an_auth_failure():
    got = env.environment_failure(_entry(result=_res(reason=NOT_LOGGED_IN)))
    assert got == env.AUTH


def test_a_refused_child_over_the_usage_limit_is_a_usage_failure():
    res = _res(reason="[usage] the agent could not run: You have hit your "
                      "weekly limit — nothing was attempted")
    assert env.environment_failure(_entry(result=res)) == env.USAGE


def test_a_missing_cli_is_named_as_such():
    res = _res(result="spawn-failed",
               reason="the `claude` CLI is not installed or not on PATH, so "
                      "no agent could be started")
    assert env.environment_failure(_entry(result=res)) == env.CLI_MISSING


def test_any_other_spawn_failure_is_still_the_machines():
    res = _res(result="spawn-failed",
               reason="TypeError: run() got an unexpected keyword argument")
    assert env.environment_failure(_entry(result=res)) == env.SPAWN


def test_a_child_that_ran_and_printed_the_phrase_is_a_task_failure():
    """The canary. A unit whose own test prints "not logged in" failed on its
    own account, and halting the mission for it would be the mirror image of
    the bug this module fixes."""
    res = _res(result="fail", reason="the agent exited 1",
               stderr="AssertionError: not logged in", started=True, code=1)
    assert env.environment_failure(_entry(result=res)) == ""


def test_a_task_traceback_naming_a_missing_file_is_not_an_environment_failure():
    res = _res(result="fail", reason="the agent exited 1",
               stderr="FileNotFoundError: [Errno 2] No such file or directory: "
                      "'fixtures/x.txt'", started=True, code=1)
    assert env.environment_failure(_entry(result=res)) == ""


def test_the_store_redirector_is_recognised_by_code_and_message_together():
    """The one environment failure that DID create a process: the redirector
    runs, prints its install prompt, and exits 49."""
    res = _res(result="fail", reason="the validation run exited 49",
               stderr=STORE_STUB, started=True, code=49)
    assert env.environment_failure(_entry(result=res)) == env.INTERPRETER_STUB


def test_exit_49_alone_is_not_the_stub():
    res = _res(result="fail", reason="the validation run exited 49",
               stderr="boom", started=True, code=49)
    assert env.environment_failure(_entry(result=res)) == ""


def test_a_refusal_with_no_recognisable_cause_is_left_to_the_repair_loop():
    res = _res(reason="the agent returned no readable result envelope, so "
                      "whether it did anything cannot be established")
    assert env.environment_failure(_entry(result=res)) == ""


def test_an_entry_whose_runner_raised_carries_no_result_and_is_not_claimed():
    assert env.environment_failure(_entry(result=None,
                                          reason="ValueError: boom")) == ""


def test_the_classifier_is_total():
    assert env.environment_failure(None) == ""
    assert env.environment_failure(object()) == ""


@pytest.mark.parametrize("kind, fix", [
    (env.AUTH, "log in"),
    (env.USAGE, "usage limit"),
    (env.CLI_MISSING, "install"),
    (env.INTERPRETER_STUB, "App Execution Alias"),
    (env.SPAWN, "created"),
])
def test_every_kind_is_described_with_its_fix(kind, fix):
    text = env.describe(kind, "detail")
    assert fix in text, text


def test_no_kind_describes_as_nothing():
    assert env.describe("") == ""
    assert env.describe("something-else") == ""


# --- the preflight ------------------------------------------------------------

def _run_answering(stdout, code=0):
    calls = []

    def run(argv, **kw):
        calls.append((list(argv), kw))
        return SimpleNamespace(stdout=stdout, stderr="", returncode=code)

    run.calls = calls
    return run


def test_preflight_refuses_when_no_cli_is_on_path():
    kind, why = env.preflight(which=lambda name: None,
                              run=_run_answering("{}"))
    assert kind == env.CLI_MISSING
    assert "PATH" in why


def test_preflight_refuses_a_cli_that_is_not_logged_in():
    # The exact answer `claude auth status --json` gave on this machine.
    run = _run_answering('{"loggedIn": false, "authMethod": "none", '
                         '"apiProvider": "firstParty"}', code=1)
    kind, why = env.preflight(which=lambda name: "/bin/claude", run=run)
    assert kind == env.AUTH
    assert "loggedIn=false" in why and "authMethod=none" in why
    argv, kw = run.calls[0]
    assert argv == ["/bin/claude", "auth", "status", "--json"]
    assert kw.get("shell") is False


def test_preflight_proceeds_when_logged_in():
    run = _run_answering('{"loggedIn": true, "authMethod": "oauth"}')
    assert env.preflight(which=lambda n: "/bin/claude", run=run) == ("", "")


def _raise_timeout(argv, **kw):
    raise subprocess.TimeoutExpired(argv, 1)


@pytest.mark.parametrize("run", [
    _raise_timeout,
    lambda argv, **kw: SimpleNamespace(stdout="error: unknown command 'auth'"),
    lambda argv, **kw: SimpleNamespace(stdout=""),
    lambda argv, **kw: SimpleNamespace(stdout="[1, 2, 3]"),
])
def test_an_inconclusive_probe_proceeds_rather_than_refusing(run):
    """The preflight is the cheap place to say it, not the only place: the run
    path halts on the first real refusal, so uncertainty here must not block
    a machine whose CLI merely lacks the subcommand."""
    assert env.preflight(which=lambda n: "/bin/claude", run=run) == ("", "")


def test_preflight_reads_the_object_out_of_noisy_output():
    run = _run_answering('warning: something\n{"loggedIn": false}\n')
    assert env.preflight(which=lambda n: "/bin/claude", run=run)[0] == env.AUTH


# --- the repair loop's short-circuit ------------------------------------------

def test_the_loop_neither_repairs_nor_records_a_machine_failure():
    fell, halted, rolled = [], [], []
    bus = _Bus()
    breaker = _breaker()
    loop = r.RepairLoop(breaker=breaker, max_attempts=3,
                        fall_back=lambda e: fell.append(e) or True,
                        rollback=lambda lane: rolled.append(lane),
                        halt=halted.append, bus=bus)
    lane = q.WorkerLane(0)
    entry = _entry(result=_res(reason=NOT_LOGGED_IN))

    assert asyncio.run(loop(entry, lane)) is False

    assert fell == [], "a fall-back was written for a task that never left its phase"
    assert rolled == [], "a lane was rolled back for work no child did"
    assert lane.depth() == 0, "the entry was re-queued"
    assert breaker.opened == [] and breaker.open_keys() == ()
    # Not even RECORDED: a breaker that heard about this would open the task's
    # key on the second refusal, for a fault the task does not own.
    assert breaker._breakers == {}
    assert halted == [loop.halted]
    assert "log in" in halted[0]
    decision = loop.decisions[-1]
    assert decision.environment == env.AUTH
    assert decision.requeued is False
    assert decision.prompt_suffix == ""
    assert loop.environment[0]["kind"] == env.AUTH
    assert loop.environment[0]["task_id"] == "t1"
    kinds = [k for k, _ in bus.events]
    assert kinds == ["note"], kinds
    assert "halted" in bus.events[0][1]["text"]


def test_the_halt_is_delivered_once_however_many_entries_arrive():
    halted = []
    loop = r.RepairLoop(breaker=_breaker(), halt=halted.append)
    for tid in ("t1", "t2", "t3"):
        loop.observe(_entry(task_id=tid, result=_res(reason=NOT_LOGGED_IN)))
    assert len(halted) == 1
    assert len(loop.environment) == 3


def test_a_halt_that_raises_is_recorded_and_does_not_fail_the_decision():
    def boom(reason):
        raise RuntimeError("no scheduler")

    loop = r.RepairLoop(breaker=_breaker(), halt=boom)
    decision = loop.observe(_entry(result=_res(reason=NOT_LOGGED_IN)))
    assert decision.environment == env.AUTH
    assert "RuntimeError" in loop.environment[0]["halt_error"]


def test_a_loop_without_a_brake_still_refuses_to_repair_such_an_entry():
    fell = []
    loop = r.RepairLoop(breaker=_breaker(),
                        fall_back=lambda e: fell.append(e) or True)
    decision = loop.observe(_entry(result=_res(reason=NOT_LOGGED_IN)))
    assert decision.requeued is False and decision.environment == env.AUTH
    assert fell == []


def test_a_task_failure_still_takes_the_ordinary_repair_path():
    """The other half: nothing above may have dulled the loop."""
    fell = []
    loop = r.RepairLoop(breaker=_breaker(), max_attempts=3,
                        fall_back=lambda e: fell.append(e) or True)
    entry = _entry(result=_res(result="fail", reason="the agent exited 1",
                               stderr="ValueError: boom", started=True, code=1))
    decision = loop.observe(entry)
    assert decision.requeued is True
    assert decision.environment == ""
    assert len(fell) == 1
    assert "ValueError: boom" in decision.prompt_suffix


# --- the scheduler's halt -----------------------------------------------------

def _drive(sched):
    return asyncio.run(sched.run())


def test_a_halted_scheduler_leaves_the_rest_queued_and_names_them():
    entries = {tid: _fresh(tid) for tid in ("a", "b", "c", "d")}
    lanes = [q.WorkerLane(0, [entries["a"], entries["b"]]),
             q.WorkerLane(1, [entries["c"], entries["d"]])]
    seen = []

    def runner(entry, lane):
        seen.append(entry.task_id)
        return _res(reason=NOT_LOGGED_IN)

    repair = r.RepairLoop(breaker=_breaker(), max_attempts=3)
    sched = q.WorkStealingScheduler(lanes, runner, repair=repair)
    repair.halt = sched.halt

    completed, failed = _drive(sched)

    assert completed == []
    assert sched.halted and "log in" in sched.halted
    left = sched.left()
    # At most one unit per lane -- each lane's first (LIFO) pop -- and possibly
    # only the first lane's, when its child answers before the second lane has
    # taken anything. Either way nothing after the halt was started, nothing
    # was retried, and the rest are still queued by name.
    assert 1 <= len(seen) <= 2 and set(seen) <= {"b", "d"}, seen
    assert len(seen) + len(left) == 4
    assert {e.task_id for e in left}.isdisjoint(seen)
    assert all(e.state == q.QUEUED for e in left)
    assert sorted(e.task_id for e in failed) == sorted(seen)
    assert all(e.attempts == 1 for e in failed), "a refused unit was retried"


def test_a_halt_before_the_run_starts_nothing():
    lanes = [q.WorkerLane(0, [_fresh("a")])]
    sched = q.WorkStealingScheduler(lanes, lambda e, ln: True)
    sched.halt("stop")
    assert _drive(sched) == ([], [])
    assert [e.task_id for e in sched.left()] == ["a"]


def test_halt_keeps_the_first_reason_and_never_an_empty_one():
    sched = q.WorkStealingScheduler([], lambda e, ln: True)
    sched.halt("first")
    sched.halt("second")
    assert sched.halted == "first"
    other = q.WorkStealingScheduler([], lambda e, ln: True)
    other.halt("")
    assert other.halted == "halted"


def test_a_drained_run_leaves_nothing_and_is_not_halted():
    lanes = [q.WorkerLane(0, [_fresh("a"), _fresh("b")])]
    sched = q.WorkStealingScheduler(lanes, lambda e, ln: True)
    completed, failed = _drive(sched)
    assert len(completed) == 2 and failed == []
    assert sched.left() == [] and sched.halted == ""


# --- the default runner -------------------------------------------------------

def test_the_default_runner_keeps_a_refusal_as_a_refusal(monkeypatch):
    """`_run_child` used to fold `refused` into `fail`, which is the exact
    point the machine's answer became a task's."""
    from mindhive.mindnet import agent as ag_mod
    from mindhive.mindnet import executor as x_mod

    monkeypatch.setattr(ag_mod, "run",
                        lambda key, prompt, **kw: x_mod.RunResult(
                            result="refused", reason=NOT_LOGGED_IN,
                            exit_code=1, executed=False, run_key=key))
    orc = lint.LintesterOrchestrator(project="test")
    entry = q.QueueEntry(task=lint.AgentTask(task_id="t1", title="t",
                                             description="d"))
    out = orc._run_child(entry, q.WorkerLane(0))
    assert out.result == "refused"
    assert out.executed is False
    assert out.ok is False
    probe = SimpleNamespace(result=out, reason=out.reason, task_id="t1")
    assert env.environment_failure(probe) == env.AUTH


def test_the_default_runner_does_not_call_a_crashed_child_ok(monkeypatch):
    from mindhive.mindnet import agent as ag_mod
    from mindhive.mindnet import executor as x_mod

    monkeypatch.setattr(ag_mod, "run",
                        lambda key, prompt, **kw: x_mod.RunResult(
                            result="fail", reason="the agent exited 1",
                            exit_code=1, executed=True, run_key=key))
    orc = lint.LintesterOrchestrator(project="test")
    entry = q.QueueEntry(task=lint.AgentTask(task_id="t1", title="t",
                                             description="d"))
    out = orc._run_child(entry, q.WorkerLane(0))
    assert out.ok is False and out.result == "fail" and out.executed is True


# --- the mission, end to end --------------------------------------------------

def test_a_mission_the_machine_refuses_halts_and_says_so(repo, monkeypatch):
    fell, samples, runs = [], [], []
    monkeypatch.setattr(r, "default_fall_back",
                        lambda entry, project=None: fell.append(entry) or True)
    monkeypatch.setattr(lint.payoff_mod, "record_samples",
                        lambda s, **kw: samples.extend(s))

    def runner(entry, lane):
        runs.append(entry.task_id)
        return _res(reason=NOT_LOGGED_IN)

    got = lint.LintesterOrchestrator(
        root=repo, project="test", lanes=1, decomposer=_two_units,
        runner=runner, max_attempts=3).execute_mission("do it", dry_run=False)

    assert got.outcome == "environment", got.reason
    assert got.environment == env.AUTH
    assert "log in" in got.reason
    assert "never started" in got.reason
    assert got.completed == ()
    assert len(runs) == 1, f"the halt did not stop the lane: {runs}"
    assert len(got.failed) == 1 and len(got.left) == 1
    assert set(got.left) | {runs[0]} == {"t-worktree", "t-ledger"}
    assert fell == [], "a fall-back row was written for a task that never left its phase"
    assert samples == [], "a wall-time sample was recorded for a unit that never ran"
    assert got.open_keys == () and got.reflections == ()
    assert got.rollbacks == (), "a lane was rolled back for work no child did"
    report = got.as_dict()
    assert report["outcome"] == "environment"
    assert report["environment"] == env.AUTH
    assert report["left"] == list(got.left)


def test_a_mission_whose_children_all_run_is_unaffected(repo):
    got = lint.LintesterOrchestrator(
        root=repo, project="test", lanes=1, decomposer=_two_units,
        runner=lambda e, ln: True, max_attempts=3).execute_mission(
            "do it", dry_run=False)
    assert got.outcome == "ok", got.reason
    assert got.environment == "" and got.left == ()
    assert got.as_dict()["left"] == []
