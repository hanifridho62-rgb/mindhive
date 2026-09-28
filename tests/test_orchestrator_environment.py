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
# Verbatim from `mindnet.agent._classify` for the answer `claude -p` gave on
# this machine on 2026-09-03, kept in the store's own reflection-health line.
# No `[usage]` mark: none of `agent._USAGE_HINTS` matches a 403, so the CLI
# said it, `_classify` wrapped it, and nothing marked it as a quota fault.
SUBSCRIPTION_DISABLED = (
    "the agent could not run: 403: Your organization has disabled Claude "
    "subscription access for Claude Code · Use an Anthropic API key "
    "instead, or ask your admin to enable access — nothing was attempted, "
    "so the task is left untouched")

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


def test_a_refused_child_whose_org_disabled_the_account_is_an_auth_failure():
    """The `subscription access` hint has been in `_AUTH_HINTS` since the module
    was written and nothing ever exercised it. This is the string it was put
    there for."""
    got = env.environment_failure(_entry(result=_res(reason=SUBSCRIPTION_DISABLED)))
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


def test_an_account_side_refusal_names_the_account_fix_not_a_login():
    """The reason this branch exists. `claude auth status` says the CLI IS
    logged in, the API answers 403, and the advice "log in" sends the operator
    to re-do the one thing that is already done. The fix belongs to the account
    or the organisation, and the CLI's own sentence is the evidence for it --
    which the AUTH branch used to drop on the floor."""
    text = env.describe(env.AUTH, SUBSCRIPTION_DISABLED)
    assert "API key" in text
    assert "403" in text
    assert "admin" in text
    assert "disabled Claude subscription access" in text, (
        "the operator never sees what the CLI actually said")
    assert "log in" not in text and "claude auth login" not in text


def test_a_long_refusal_is_quoted_back_but_bounded():
    """`_DETAIL_CHARS` is a real bound and nothing exercised it.

    The account branch quotes the CLI's own sentence because that sentence is
    the evidence an operator acts on, and bounds it because `agent._classify`
    has already wrapped the message in one of its own -- an unbounded quote puts
    a paragraph in the middle of one line of advice. A bound no fixture crosses
    is a bound nobody has checked, so this feeds one that crosses it."""
    tail = " and here is a paragraph the operator does not need." * 20
    text = env.describe(env.AUTH, SUBSCRIPTION_DISABLED + tail)
    assert "API key" in text                       # still the account branch
    assert "does not need" not in text, "the quote ran past its bound"
    assert len(text) < len(SUBSCRIPTION_DISABLED + tail)


def test_the_quote_collapses_the_whitespace_it_carries():
    """The quote is built with `" ".join(detail.split())`, so a CLI message
    wrapped over several lines arrives as one line of advice rather than
    breaking the sentence it is embedded in."""
    wrapped = ("403: Your organization has disabled\n\t"
               "Claude  subscription   access for Claude Code")
    text = env.describe(env.AUTH, wrapped)
    assert "API key" in text
    assert "\n" not in text and "\t" not in text
    assert "disabled Claude subscription access" in text


def test_a_plain_login_refusal_still_says_log_in():
    """The other half of the pair. A branch that answered the account sentence
    for every AUTH failure would be the same defect wearing the other hat."""
    text = env.describe(env.AUTH, NOT_LOGGED_IN)
    assert "log in" in text
    assert "API key" not in text


def test_the_preflight_refusal_keeps_the_login_wording():
    """`preflight` only ever refuses on `loggedIn` false, and its detail says
    so in its own words -- no hint of the account case, so no account advice."""
    text = env.describe(env.AUTH,
                        "claude auth status: loggedIn=false (authMethod=none)")
    assert "log in" in text and "API key" not in text


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


def test_the_probe_timeout_holds_for_the_whole_process_tree(tmp_path):
    """The real runner, no `run` seam, against a stub shaped like `claude.CMD`
    on this machine: the CLI a grandchild of cmd.exe, holding the pipes. With
    `subprocess.run` a 2 s probe came back after 30.12 s -- its timeout killed
    cmd.exe alone and then waited on the grandchild's pipes."""
    import time

    from tests.test_plugin_cache import (TREE_SLACK_S, kill_leftover, pid_of,
                                         process_gone, sleeping_tree_stub)

    stub, pidfile = sleeping_tree_stub(tmp_path)
    timeout = 2.0
    started = time.monotonic()
    got = env.preflight(which=lambda name: str(stub), timeout_s=timeout)
    elapsed = time.monotonic() - started
    pid = pid_of(pidfile)
    try:
        assert elapsed < timeout + TREE_SLACK_S, (
            f"a {timeout} s probe came back after {elapsed:.2f} s")
        assert got == ("", ""), "a timed-out probe is inconclusive, never a refusal"
        assert pid is not None, "the stub's grandchild never started"
        assert process_gone(pid), "the grandchild outlived the probe's timeout"
    finally:
        kill_leftover(pid)


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


def test_the_halt_reason_carries_the_account_fix():
    """`describe` is not read directly by an operator: it reaches them through
    the halt note the board renders. If the sentence changes there and not
    here, the board still says the wrong thing."""
    halted = []
    loop = r.RepairLoop(breaker=_breaker(), halt=halted.append)

    loop.observe(_entry(result=_res(reason=SUBSCRIPTION_DISABLED)))

    assert loop.environment[0]["kind"] == env.AUTH
    assert "API key" in halted[0]
    assert "log in" not in halted[0]


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


def test_a_mission_refused_by_the_organisation_setting_says_which_fix(
        repo, monkeypatch):
    """The same halt as the not-logged-in mission, reported with the fix that
    would actually clear it."""
    monkeypatch.setattr(lint.payoff_mod, "record_samples", lambda s, **kw: None)

    got = lint.LintesterOrchestrator(
        root=repo, project="test", lanes=1, decomposer=_two_units,
        runner=lambda e, ln: _res(reason=SUBSCRIPTION_DISABLED),
        max_attempts=3).execute_mission("do it", dry_run=False)

    assert got.outcome == "environment", got.reason
    assert got.environment == env.AUTH
    assert "API key" in got.reason
    assert "log in" not in got.reason
    assert "never started" in got.reason
    assert got.rollbacks == ()


def test_a_mission_whose_children_all_run_is_unaffected(repo):
    got = lint.LintesterOrchestrator(
        root=repo, project="test", lanes=1, decomposer=_two_units,
        runner=lambda e, ln: True, max_attempts=3).execute_mission(
            "do it", dry_run=False)
    assert got.outcome == "ok", got.reason
    assert got.environment == "" and got.left == ()
    assert got.as_dict()["left"] == []


# --- a limit that arrived late, at mission level ------------------------------

LATE_LIMIT = ("[usage] the agent could not run to completion: 429: You've hit "
              "your session limit · resets 9pm (Asia/Jakarta) — it had "
              "already taken 22 turn(s), so its tree may hold partial work "
              "worth looking at")

EARLY_LIMIT = ("[usage] the agent could not run: 429: You've hit your session "
               "limit — nothing was attempted, so the task is left "
               "untouched")


def test_a_late_limit_does_not_tell_the_operator_no_child_started():
    """The mission-level half of the same false claim.

    `agent` now says honestly that a child which took real turns may have left
    partial work, but the sentence the operator READS is this one -- it is what
    the board prints in its alert panel and what `Mission.reason` carries. On
    2026-09-08 it said "no child could start" for a run whose two lanes held
    146 and 7 lines of uncommitted edits. TODOS item 29 asked for exactly this
    to be checked against a real run."""
    text = env.describe(env.USAGE, LATE_LIMIT)
    assert "no child could start" not in text
    assert "partial" in text or "lane" in text
    assert "wait for the window to reset" in text


def test_an_early_limit_still_says_no_child_could_start():
    """The discriminating half. When the limit really did land before any work,
    the old sentence is the true one and must survive."""
    text = env.describe(env.USAGE, EARLY_LIMIT)
    assert "no child could start" in text
    assert "partial" not in text


def test_a_usage_halt_with_no_detail_keeps_the_original_sentence():
    """Nothing to go on means no new claim: the caller that passes no reason at
    all gets what it always got."""
    text = env.describe(env.USAGE)
    assert "no child could start" in text


# --- the preflight gate, blueprint §10.9.2 ------------------------------------
#
# §10.9.2 names two functions: `check_claude_environment() -> (ok, msg,
# details)` and `enforce_preflight_gate()`, which writes one fatal line and
# leaves with exit 3. Both stand ON `preflight`'s probe rather than beside it.
# A second probe would be a second opinion about the same `claude auth status
# --json`, and two opinions about one fact disagree the first time either one
# learns something -- so the verdicts below are `preflight`'s, and only the
# shape of the answer is new.

# The answer §1.4 quotes for a signed-in machine.
HEALTHY = ('{"loggedIn": true, "authMethod": "claude.ai", '
           '"subscriptionType": "max"}')
# What `claude auth status --json` printed on this machine signed out -- at
# exit 1, which is why the body is read whatever the exit code says. A check
# that tested the code first would never reach the `loggedIn` it exists for.
SIGNED_OUT = ('{"loggedIn": false, "authMethod": "none", '
              '"apiProvider": "firstParty"}')
# The fatal frame, verbatim from §10.9.2.
FATAL = "[FATAL PREFLIGHT REFUSAL - EXIT 3]: "


def test_the_refusal_exit_code_is_three():
    assert env.EXIT_ENVIRONMENT_REFUSAL == 3


def test_the_fatal_frame_is_the_specs_text_to_the_character():
    """What a wrapper greps for. The front door prints it by reference
    (`tests/test_cli_lintester.py`), never as a copy, so this is where its
    spelling answers to §10.9.2."""
    assert env.FATAL_REFUSAL_FRAME == FATAL


def test_the_check_refuses_a_machine_with_no_cli_and_names_the_binary():
    run = _run_answering(HEALTHY)

    ok, msg, details = env.check_claude_environment(which=lambda name: None,
                                                    run=run)

    assert ok is False
    assert "claude" in msg.lower() and "PATH" in msg, msg
    assert details == {}
    assert run.calls == [], "a CLI that is not on PATH was spawned anyway"
    # The canary: the same fake IS reached once a CLI is on PATH.
    env.check_claude_environment(which=lambda name: "/bin/claude", run=run)
    assert len(run.calls) == 1


def test_the_check_refuses_a_signed_out_cli_and_hands_back_its_answer():
    run = _run_answering(SIGNED_OUT, code=1)

    ok, msg, details = env.check_claude_environment(
        which=lambda name: "/bin/claude", run=run)

    assert ok is False
    assert "login" in msg, msg
    assert details == {"loggedIn": False, "authMethod": "none",
                       "apiProvider": "firstParty"}
    # `preflight`'s own probe, not a second one: one call, the same argv, no
    # shell, and `preflight`'s timeout rather than a number of the check's own.
    assert len(run.calls) == 1
    argv, kw = run.calls[0]
    assert argv == ["/bin/claude", "auth", "status", "--json"]
    assert kw.get("shell") is False
    assert kw.get("timeout") == env.PREFLIGHT_TIMEOUT_S


def test_the_check_passes_a_signed_in_cli_and_hands_back_its_answer():
    ok, msg, details = env.check_claude_environment(
        which=lambda name: "/bin/claude", run=_run_answering(HEALTHY))

    assert ok is True
    assert msg, "a pass with nothing to say"
    assert details == {"loggedIn": True, "authMethod": "claude.ai",
                       "subscriptionType": "max"}


@pytest.mark.parametrize("run", [
    _raise_timeout,
    lambda argv, **kw: SimpleNamespace(stdout="error: unknown command 'auth'"),
    lambda argv, **kw: SimpleNamespace(stdout=""),
    lambda argv, **kw: SimpleNamespace(stdout="[1, 2, 3]"),
])
def test_an_inconclusive_probe_passes_the_check_without_claiming_a_login(run):
    """§10.9.2's own check fails CLOSED here: a timeout, a non-zero exit, a body
    that is not JSON all answer False. `preflight` fails OPEN, on purpose and
    pinned above (`test_an_inconclusive_probe_proceeds_rather_than_refusing`):
    the run path halts on the first real refusal, and a gate that refused on
    its own uncertainty would block every mission on a CLI that merely lacks
    the subcommand. Built on `preflight`'s probe, the check inherits that
    verdict -- but not the right to call an unanswered question a confirmed
    login."""
    confirmed = env.check_claude_environment(
        which=lambda n: "/bin/claude", run=_run_answering(HEALTHY))[1]

    ok, msg, details = env.check_claude_environment(
        which=lambda n: "/bin/claude", run=run)

    assert env.preflight(which=lambda n: "/bin/claude", run=run) == ("", "")
    assert ok is True
    assert details == {}
    assert msg and msg != confirmed, (
        "an unanswered probe was reported as a confirmed login")


def _binding_of(source, name="ENVIRONMENT_STATUS"):
    """How *name* is bound at the top level of *source*: ``"alias"`` when it
    names `EXIT_ENVIRONMENT_REFUSAL`, ``"literal"`` when it is a number of its
    own, ``"other"`` for anything else, ``"missing"`` when it is not bound."""
    import ast

    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom):
            if any(a.name == "EXIT_ENVIRONMENT_REFUSAL" and a.asname == name
                   for a in node.names):
                return "alias"
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = node.value
            named = (value.attr if isinstance(value, ast.Attribute)
                     else getattr(value, "id", ""))
            if named == "EXIT_ENVIRONMENT_REFUSAL":
                return "alias"
            return "literal" if isinstance(value, ast.Constant) else "other"
    return "missing"


def test_the_cli_status_is_the_gates_constant_not_a_second_number():
    """`orchestrator/cli.py` answered 3 for "nothing could start here" before
    this module had a name for it. Two literals that agree today are two
    authorities tomorrow, so the CLI's status has to BE the gate's constant.

    `is` alone cannot prove that: CPython caches the small integers, so
    ``3 is 3`` holds for two independent literals. The binding itself is read
    instead, with a canary proving the reader can tell the two apart."""
    from pathlib import Path

    from mindhive.orchestrator import cli as lint_cli

    assert lint_cli.ENVIRONMENT_STATUS == 3
    assert lint_cli.ENVIRONMENT_STATUS is env.EXIT_ENVIRONMENT_REFUSAL
    source = Path(lint_cli.__file__).read_text(encoding="utf-8")
    assert _binding_of(source) == "alias", _binding_of(source)
    # The canary: the reader sees the old literal as the literal it is.
    assert _binding_of("ENVIRONMENT_STATUS = 3\n") == "literal"


@pytest.fixture
def no_real_cli(monkeypatch):
    """The net under every gate test. Each one replaces the check at the module
    seam or drives it through `which`; a gate that ever bypassed the seam finds
    no `claude` on PATH here instead of spawning the real one on this machine."""
    monkeypatch.setattr(env.shutil, "which", lambda name: None)


def _check_answering(ok, msg, details):
    """A stand-in for `check_claude_environment`, patched at the module seam."""
    def check(which=None, run=None):
        return ok, msg, details

    return check


@pytest.mark.parametrize("kwargs, named", [
    ({}, "hive lintester"),
    ({"argv0": "hive probe"}, "hive probe"),
])
def test_a_refusing_gate_exits_three_and_says_why_exactly_once(
        monkeypatch, capsys, no_real_cli, kwargs, named):
    monkeypatch.setattr(env, "check_claude_environment",
                        _check_answering(False, "no claude here", {}))
    exits = []

    got = env.enforce_preflight_gate(exit=exits.append, **kwargs)

    assert exits == [3] and exits == [env.EXIT_ENVIRONMENT_REFUSAL]
    assert got is None, "a refusal handed back what a caller could read as a pass"
    out, err = capsys.readouterr()
    assert out == "", "the refusal went to stdout"
    # §10.9.2 writes the frame FIRST, so a wrapper can anchor on it
    # (`^\[FATAL PREFLIGHT REFUSAL`); the command's name follows it.
    assert err.startswith(FATAL), err
    assert err == f"{FATAL}{named}: no claude here\n", err
    assert err.count(FATAL) == 1 and err.count("\n") == 1, (
        "the refusal is one line, printed once")


def test_a_passing_gate_returns_the_details_and_never_exits(
        monkeypatch, capsys, no_real_cli):
    details = {"loggedIn": True, "authMethod": "claude.ai",
               "subscriptionType": "max"}
    monkeypatch.setattr(env, "check_claude_environment",
                        _check_answering(True, "ready", details))
    exits = []

    got = env.enforce_preflight_gate(exit=exits.append)

    assert got == details
    assert exits == [], "a passing gate called exit"
    assert capsys.readouterr() == ("", ""), "a passing gate printed"
    # The canary: the same spy and the same capture DO see a refusal.
    monkeypatch.setattr(env, "check_claude_environment",
                        _check_answering(False, "no", {}))
    env.enforce_preflight_gate(exit=exits.append)
    assert exits == [3]
    assert FATAL in capsys.readouterr().err


def test_the_default_gate_leaves_the_process_with_status_three(
        monkeypatch, no_real_cli):
    monkeypatch.setattr(env, "check_claude_environment",
                        _check_answering(False, "no claude here", {}))

    with pytest.raises(SystemExit) as exc:
        env.enforce_preflight_gate()

    assert exc.value.code == 3


def test_the_gate_asks_the_real_check_through_the_real_probe(
        capsys, no_real_cli):
    """No stand-in for the check here. `which` answering nothing drives the
    whole chain -- gate, check, `preflight`'s probe -- without spawning."""
    exits = []

    env.enforce_preflight_gate(exit=exits.append)

    assert exits == [3]
    err = capsys.readouterr().err
    assert FATAL in err and "claude" in err.lower(), err
