# SPDX-License-Identifier: Apache-2.0
"""`hive ci premerge | watch | conflicts | legs` -- parsing, dispatch, exit
codes and output. The modules behind the verbs are swapped for fakes at the
names the handlers look up, so what runs here is the handler and argparse."""
import json
import os
import time
from types import SimpleNamespace

import pytest

from mindhive import __main__ as cli_mod
from mindhive import events, paths
from mindhive.ci import cli as ci_cli
from mindhive.ci import github as gh_mod
from mindhive.ci import legrun, legs, premerge, premerge_cli, preview, watch


def parse(*argv):
    return cli_mod._build_parser().parse_args(["ci", *argv])


def run(*argv):
    args = parse(*argv)
    return args.func(args)


class Recorder:
    """Stands in for `premerge.Premerge`; remembers how it was built and called."""

    def __init__(self, result=None, heads=None, prs=None):
        self.result = result or premerge.PremergeResult(
            pr_id=151, verdict="green", exit_code=3, reason="green: 1 leg(s) passed",
            report_text="THE REPORT")
        self.heads, self.prs = heads, prs or []
        self.made, self.calls = [], []

    def __call__(self, root, **kw):
        self.made.append((root, kw))
        outer = self

        class _Premerge:
            spawn = "the-spawn"
            project = None
            github = SimpleNamespace(open_prs=lambda: [dict(p) for p in outer.prs])

            def run_pr(self, n, **kw):
                outer.calls.append(("pr", n, kw))
                return outer.result

            def run_branch(self, branch, **kw):
                outer.calls.append(("branch", branch, kw))
                return outer.result

            def fetch_heads(self, numbers, base_ref):
                outer.calls.append(("heads", list(numbers), base_ref))
                return outer.heads
        return _Premerge()


def test_the_verbs_parse_and_dispatch():
    args = parse("premerge", "151", "--legs", "node", "--merge", "--root", "R")
    assert args.func is premerge_cli.cmd_ci_premerge
    assert (args.pr, args.legs, args.merge, args.root) == (151, "node", True, "R")
    assert parse("watch", "--once").func is premerge_cli.cmd_ci_watch
    assert parse("conflicts").func is premerge_cli.cmd_ci_conflicts
    assert parse("legs").func is premerge_cli.cmd_ci_legs


def test_the_flag_defaults_are_the_modules_own():
    args = parse("premerge", "1")
    assert (args.jobs, args.timeout) == (premerge.DEFAULT_JOBS, legrun.LEG_TIMEOUT_S)
    assert parse("watch").interval == watch.DEFAULT_INTERVAL_S
    assert (args.base, args.no_status, args.keep, args.work_root) == (
        "master", False, False, None)


@pytest.mark.parametrize("argv,why", [
    ((), "exactly one of a PR number or --branch"),
    (("151", "--branch", "x"), "exactly one of a PR number or --branch"),
    (("--branch", "x", "--merge"), "--merge needs a PR number"),
    (("151", "--allow-base-drift"), "--allow-base-drift only means something with --merge"),
    (("151", "--merge", "--legs", "node"), "--merge tests every leg: drop --legs"),
])
def test_premerge_refuses_a_request_it_cannot_honour(monkeypatch, capsys, argv, why):
    rec = Recorder()
    monkeypatch.setattr(premerge, "Premerge", rec)
    assert run("premerge", *argv) == 2
    assert why in capsys.readouterr().err and rec.made == []


def test_premerge_exits_with_the_runs_code_and_prints_its_report(monkeypatch, capsys, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(premerge, "Premerge", rec)
    code = run("premerge", "151", "--merge", "--jobs", "2",
               "--timeout", "60", "--keep", "--root", str(tmp_path), "--project", "test")
    assert code == 3 and capsys.readouterr().out.strip() == "THE REPORT"
    root, kw = rec.made[0]
    assert str(root) == str(tmp_path)
    assert (kw["project"], kw["jobs"], kw["timeout_s"], kw["post_statuses"], kw["keep"]) == (
        "test", 2, 60.0, True, True)
    assert rec.calls == [("pr", 151, {"legs_spec": None, "merge": True,
                                      "allow_base_drift": False, "trust": False})]


def test_premerge_json_and_branch_mode_never_post(monkeypatch, capsys, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(premerge, "Premerge", rec)
    assert run("premerge", "--branch", "claude/x", "--base", "dev", "--json",
               "--root", str(tmp_path)) == 3
    assert json.loads(capsys.readouterr().out)["exit_code"] == 3
    assert rec.made[0][1]["post_statuses"] is False
    assert rec.calls == [("branch", "claude/x", {"base": "dev", "legs_spec": None})]
    run("premerge", "151", "--no-status", "--root", str(tmp_path))
    assert rec.made[1][1]["post_statuses"] is False
    run("premerge", "151", "--trust", "--root", str(tmp_path))
    assert rec.calls[-1] == ("pr", 151, {"legs_spec": None, "merge": False,
                                         "allow_base_drift": False, "trust": True})


class FakeProbe:
    def check(self, leg):
        if leg.host == legs.HOST_WINDOWS:
            return legs.Availability(leg, True, python=f"C:/py{leg.python}.exe", via="native")
        return legs.Availability(leg, False, reason="not on this machine")


def test_legs_lists_each_leg_and_whether_this_machine_can_run_it(monkeypatch, capsys):
    monkeypatch.setattr(legs, "Probe", FakeProbe)
    assert run("legs") == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == len(legs.CATALOG)
    assert out[0].split() == ["windows-py3.11", "ready", "(native:", "C:/py3.11.exe)"]
    assert "pg" in out[-1] and "NOT RUNNABLE: not on this machine" in out[-1]
    assert run("legs", "--legs", "node,browser") == 2          # no full-suite leg ready
    assert run("legs", "--legs", "nope") == 2
    assert run("legs", "--json", "--legs", "windows-py3.12") == 0
    row = json.loads(capsys.readouterr().out.splitlines()[-1])[0]
    assert (row["name"], row["ok"], row["python"]) == ("windows-py3.12", True, "C:/py3.12.exe")


def test_conflicts_prints_the_matrix_and_exits_1_on_a_conflict(monkeypatch, capsys, tmp_path):
    rec = Recorder(heads=("1" * 40, {146: "2" * 40, 151: "3" * 40}, ""),
                   prs=[{"number": 151, "baseRefName": "master"},
                        {"number": 146, "baseRefName": "master"},
                        {"number": 99, "baseRefName": "dev"}])
    monkeypatch.setattr(premerge, "Premerge", rec)
    seen = []

    def matrix(root, base, heads, run=None):
        seen.append((base, heads, run))
        return {"base": {146: [], 151: ["src/a.py"]},
                "pairs": [{"a": 146, "b": 151, "paths": ["x.py"]}], "errors": {}}
    monkeypatch.setattr(preview, "conflict_matrix", matrix)
    assert run("conflicts", "--root", str(tmp_path)) == 1
    out = capsys.readouterr().out
    assert rec.calls == [("heads", [146, 151], "master")]
    assert seen == [("1" * 40, {146: "2" * 40, 151: "3" * 40}, "the-spawn")]
    assert "  #146  clean" in out and "  #151  CONFLICTS: src/a.py" in out
    assert "  #146 + #151: x.py" in out


def test_conflicts_with_nothing_open_is_0(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(premerge, "Premerge", Recorder(prs=[]))
    assert run("conflicts", "--root", str(tmp_path)) == 0
    assert "no open PR targets master" in capsys.readouterr().out


@pytest.mark.parametrize("actions,code", [
    ([{"pr": 1, "action": "tested", "verdict": "green"}], 0),
    ([{"pr": 1, "action": "unchanged", "verdict": "green"},
      {"pr": 2, "action": "conflict", "verdict": "red"}], 1),
    ([{"pr": 1, "action": "error", "detail": "gh down"}], 2),
    ([{"pr": 1, "action": "skipped", "detail": "a draft"}], 0),
])
def test_watch_once_exits_by_the_verdicts_it_saw(monkeypatch, tmp_path, actions, code):
    monkeypatch.setattr(premerge, "Premerge", Recorder())
    built = []

    class FakeWatcher:
        def __init__(self, pm, **kw):
            built.append(kw)

        def once(self):
            return actions
    monkeypatch.setattr(watch, "Watcher", FakeWatcher)
    assert run("watch", "--once", "--skip-drafts", "--interval", "9",
               "--root", str(tmp_path)) == code
    assert (built[0]["skip_drafts"], built[0]["interval_s"]) == (True, 9.0)


_OPEN = [{"number": 151, "state": "OPEN", "isDraft": False, "isCrossRepository": False,
          "headRefName": "claude/f151", "headRefOid": "2" * 40, "baseRefName": "master"}]


def test_watch_once_finding_another_pass_running_does_nothing_and_exits_0(
        monkeypatch, tmp_path, capsys):
    rec = Recorder(prs=_OPEN)
    monkeypatch.setattr(premerge, "Premerge", rec)
    path = watch.lock_path("test")
    other = watch.PassLock(path)
    assert other.take()
    try:
        code = run("watch", "--once", "--json", "--root", str(tmp_path))
    finally:
        other.release()
    [action] = json.loads(capsys.readouterr().out)
    assert (code, rec.calls) == (0, [])
    assert (action["pr"], action["action"], action["lock"], action["holder_pid"]) == (
        0, "skipped", str(path), os.getpid())


def test_watch_once_whose_lock_cannot_be_taken_exits_2_and_tests_nothing(
        monkeypatch, tmp_path, capsys):
    rec = Recorder(prs=_OPEN)
    monkeypatch.setattr(premerge, "Premerge", rec)
    namespace = paths.project_dir("test")
    namespace.mkdir(parents=True, exist_ok=True)
    (namespace / "state").write_text("a file where the state directory belongs",
                                     encoding="utf-8")
    assert run("watch", "--once", "--json", "--root", str(tmp_path)) == 2
    [action] = json.loads(capsys.readouterr().out)
    assert (action["action"], action["exit_code"], action["lock"], rec.calls) == (
        "error", 2, None, [])


def test_watch_once_finding_a_pass_held_past_any_honest_run_exits_2(
        monkeypatch, tmp_path, capsys):
    rec = Recorder(prs=_OPEN)
    monkeypatch.setattr(premerge, "Premerge", rec)
    path = watch.lock_path("test")
    other = watch.PassLock(path)
    assert other.take()
    try:
        fd = os.open(str(path), os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            os.write(fd, f"{os.getpid()} {int(time.time()) - watch.STUCK_S - 60}"
                     .encode("ascii").ljust(64))
        finally:
            os.close(fd)
        code = run("watch", "--once", "--json", "--root", str(tmp_path))
    finally:
        other.release()
    [action] = json.loads(capsys.readouterr().out)
    assert (code, rec.calls) == (2, [])
    assert (action["action"], action["exit_code"], action["lock"], action["holder_pid"]) == (
        "error", 2, str(path), os.getpid())


def test_watch_refuses_legs_it_does_not_know_before_anything_runs(monkeypatch, capsys,
                                                                 tmp_path):
    rec = Recorder()
    monkeypatch.setattr(premerge, "Premerge", rec)
    assert run("watch", "--once", "--legs", "nope", "--root", str(tmp_path)) == 2
    assert "hive ci watch: no leg matches nope" in capsys.readouterr().err
    assert rec.made == []


def test_status_judges_a_premerge_run_by_its_verdict_and_names_its_legs(capsys):
    payload = {"text": "t", "argv": ["hive", "ci", "premerge", "151", "--merge"],
               "exit_code": 3, "verified_by": "python", "scope": "ci",
               "gate": "premerge", "pr_id": 151, "branch": "claude/x",
               "mode": "github", "merge_state": "refused",
               "merge_reason": "the PR is a draft",
               "verdict": "green", "reason": "green: 2 leg(s) passed; not run here: pg",
               "legs": [{"name": "windows-py3.12", "state": "success"},
                        {"name": "node", "state": "success"}],
               "unavailable": [{"name": "pg", "reason": "no dsn"}],
               "commit_sha": "2" * 40, "counts": {"passed": 20}}
    events.append_event("verification", payload, project="test")
    args = SimpleNamespace(project="test", pr=None, page=1, json=False)
    assert ci_cli.cmd_ci_status(args) == 0
    out = capsys.readouterr().out
    assert "verdict: green -- green: 2 leg(s) passed; not run here: pg" in out
    assert "legs: 2 success; not run: pg" in out
    assert "merge: refused -- the PR is a draft" in out
    events.append_event("verification", {**payload, "verdict": "red", "exit_code": 1},
                        project="test")
    assert ci_cli.cmd_ci_status(args) == 1


# --- every verb's exit codes and --json, pinned -------------------------------------


class _FW:
    def __init__(self, pm, **kw):
        pass

def test_watch_once_exits_2_for_a_tested_pr_whose_verdict_was_error(monkeypatch, tmp_path):
    monkeypatch.setattr(premerge, "Premerge", Recorder())

    class FW(_FW):
        def once(self):
            return [{"pr": 1, "action": "tested", "verdict": "error",
                     "detail": "no full-suite leg ran on this machine"}]
    monkeypatch.setattr(watch, "Watcher", FW)
    assert run("watch", "--once", "--root", str(tmp_path)) == 2

def test_watch_without_once_loops_and_ctrl_c_is_a_clean_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(premerge, "Premerge", Recorder())
    calls = []

    class FW(_FW):
        def loop(self):
            calls.append("loop")
            raise KeyboardInterrupt
    monkeypatch.setattr(watch, "Watcher", FW)
    try:
        code = run("watch", "--root", str(tmp_path))
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C escaped `hive ci watch`")
    assert code == 0 and calls == ["loop"]

def _conflicts_pm(open_prs=None, heads=("1" * 40, {146: "2" * 40, 151: "3" * 40}, "")):
    class PM:
        spawn = "s"
        github = SimpleNamespace(open_prs=open_prs or (lambda: [
            {"number": 146, "baseRefName": "master"},
            {"number": 151, "baseRefName": "master"}]))

        def __init__(self, root, **kw):
            pass

        def fetch_heads(self, numbers, base_ref):
            return heads
    return PM

CLEAN = {"base": {146: [], 151: []}, "pairs": [], "errors": {}}

@pytest.mark.parametrize("matrix,code", [
    ({**CLEAN, "base": {146: [], 151: ["a.py"]}}, 1),                       # base only
    ({**CLEAN, "pairs": [{"a": 146, "b": 151, "paths": ["x.py"]}]}, 1),     # pair only
    (CLEAN, 0),                                                             # canary
    ({**CLEAN, "errors": {"146+151": "boom"}}, 2),
])
def test_conflicts_exit_codes(monkeypatch, tmp_path, matrix, code):
    monkeypatch.setattr(premerge, "Premerge", _conflicts_pm())
    monkeypatch.setattr(preview, "conflict_matrix", lambda *a, **k: matrix)
    assert run("conflicts", "--root", str(tmp_path)) == code

def test_conflicts_that_cannot_check_exits_2_and_says_why(monkeypatch, tmp_path, capsys):
    def boom():
        raise gh_mod.GitHubError("gh pr list: not logged in")
    monkeypatch.setattr(premerge, "Premerge", _conflicts_pm(open_prs=boom))
    assert run("conflicts", "--root", str(tmp_path)) == 2
    assert "not logged in" in capsys.readouterr().err
    monkeypatch.setattr(premerge, "Premerge",
                        _conflicts_pm(heads=("", {}, "git fetch origin: down")))
    assert run("conflicts", "--root", str(tmp_path)) == 2
    assert "git fetch origin: down" in capsys.readouterr().err

def test_legs_exits_2_when_only_a_non_full_suite_leg_can_run(monkeypatch):
    class NodeOnly:
        def check(self, leg):
            if leg.name == "node":
                return legs.Availability(leg, True, python="C:/py.exe", via="native")
            return legs.Availability(leg, False, reason="no")
    monkeypatch.setattr(legs, "Probe", NodeOnly)
    assert run("legs") == 2

def test_conflicts_json_is_the_matrix_under_stable_keys(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(premerge, "Premerge", _conflicts_pm())
    monkeypatch.setattr(preview, "conflict_matrix",
                        lambda *a, **k: {**CLEAN, "base": {146: [], 151: ["a.py"]}})
    assert run("conflicts", "--json", "--root", str(tmp_path)) == 1
    assert json.loads(capsys.readouterr().out) == {
        "base": "master", "base_sha": "1" * 40, "prs": [146, 151],
        "against_base": {"146": [], "151": ["a.py"]}, "pairs": [], "errors": {}}

def test_watch_once_json_prints_what_the_pass_did(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(premerge, "Premerge", Recorder())
    done = [{"pr": 1, "action": "tested", "verdict": "green", "exit_code": 0, "detail": "ok"}]

    class FW(_FW):
        def once(self):
            return done
    monkeypatch.setattr(watch, "Watcher", FW)
    assert run("watch", "--once", "--json", "--root", str(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out) == done


@pytest.mark.parametrize("argv", [("premerge", "151"),
                                  ("premerge", "--branch", "claude/x")])
def test_ctrl_c_during_premerge_is_exit_130_and_a_line_not_a_traceback(
        monkeypatch, capsys, tmp_path, argv):
    class Stopped:
        def __init__(self, root, **kw):
            pass

        def run_pr(self, number, **kw):
            raise KeyboardInterrupt

        def run_branch(self, branch, **kw):
            raise KeyboardInterrupt
    monkeypatch.setattr(premerge, "Premerge", Stopped)
    try:
        code = run(*argv, "--root", str(tmp_path))
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C escaped `hive ci premerge`")
    assert code == 130
    assert "interrupted" in capsys.readouterr().err



def test_ctrl_c_during_watch_once_is_exit_130_and_a_line_not_a_traceback(
        monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(premerge, "Premerge", Recorder())

    class FW(_FW):
        def once(self):
            raise KeyboardInterrupt
    monkeypatch.setattr(watch, "Watcher", FW)
    try:
        code = run("watch", "--once", "--root", str(tmp_path))
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C escaped `hive ci watch --once`")
    assert code == 130
    assert "interrupted" in capsys.readouterr().err


# --- a refusal under --json is still JSON ---------------------------------------------


@pytest.mark.parametrize("argv,why", [
    (("premerge", "--json"), "give exactly one of a PR number or --branch"),
    (("premerge", "151", "--merge", "--legs", "node", "--json"), "--merge tests every leg"),
    (("watch", "--once", "--json", "--legs", "nope"), "no leg matches nope"),
    (("legs", "--json", "--legs", "nope"), "no leg matches nope"),
    (("legs", "--json", "--legs", ","), "names no leg"),
    (("legs", "--legs", ","), "names no leg"),
])
def test_a_refusal_prints_one_line_and_under_json_one_json_object(
        monkeypatch, capsys, tmp_path, argv, why):
    rec = Recorder()
    monkeypatch.setattr(premerge, "Premerge", rec)
    monkeypatch.setattr(legs, "Probe", FakeProbe)
    extra = ("--root", str(tmp_path)) if argv[0] in ("premerge", "watch") else ()
    assert run(*argv, *extra) == 2
    got = capsys.readouterr()
    assert why in got.err and rec.made == []
    if "--json" in argv:
        printed = json.loads(got.out)
        assert (printed["verdict"], printed["exit_code"]) == ("error", 2)
        assert why in printed["reason"]
    else:
        assert got.out == ""


def test_conflicts_that_cannot_check_under_json_says_why_in_json(monkeypatch, capsys,
                                                                tmp_path):
    def boom():
        raise gh_mod.GitHubError("gh pr list: not logged in")
    monkeypatch.setattr(premerge, "Premerge", _conflicts_pm(open_prs=boom))
    assert run("conflicts", "--json", "--root", str(tmp_path)) == 2
    assert json.loads(capsys.readouterr().out) == {
        "verdict": "error", "exit_code": 2, "reason": "gh pr list: not logged in"}
    monkeypatch.setattr(premerge, "Premerge",
                        _conflicts_pm(heads=("", {}, "git fetch origin: down")))
    assert run("conflicts", "--json", "--root", str(tmp_path)) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "git fetch origin: down"


def test_ctrl_c_under_json_is_exit_130_in_json_too(monkeypatch, capsys, tmp_path):
    class Stopped:
        def __init__(self, root, **kw):
            pass

        def run_pr(self, number, **kw):
            raise KeyboardInterrupt
    monkeypatch.setattr(premerge, "Premerge", Stopped)
    assert run("premerge", "151", "--json", "--root", str(tmp_path)) == 130
    got = capsys.readouterr()
    printed = json.loads(got.out)
    assert (printed["verdict"], printed["exit_code"]) == ("error", 130)
    assert "interrupted" in printed["reason"] and "interrupted" in got.err


def test_status_with_no_run_under_json_says_so_in_json(capsys):
    args = SimpleNamespace(project="test", pr=7, page=1, json=True, gate=None, branch=None)
    assert ci_cli.cmd_ci_status(args) == 2
    assert json.loads(capsys.readouterr().out) == {
        "found": False, "pr": 7, "gate": None, "branch": None, "page": 1}


# --- the same fact under the same key; the exit codes where an agent looks -----------


def test_legs_json_names_the_job_and_the_check_as_a_run_does(monkeypatch, capsys):
    monkeypatch.setattr(legs, "Probe", FakeProbe)
    assert run("legs", "--json", "--legs", "windows-py3.12") == 0
    row = json.loads(capsys.readouterr().out)[0]
    leg = legs.select("windows-py3.12")[0]
    ran = legrun.LegResult(name=leg.name, job=leg.job, ci_name=leg.ci_name).as_dict()
    assert (row["job"], row["ci_name"]) == (ran["job"], ran["ci_name"])
    assert row["job"] != row["ci_name"]                 # canary: two different facts


@pytest.mark.parametrize("verb,lines", [
    ("premerge", ("0    green", "1    red", "2    the check could not be made",
                  "3    green, but --merge did not merge",
                  "130  stopped by hand (Ctrl+C): every leg it started is stopped")),
    ("watch", ("0    every PR the pass looked at is green, or another pass still held "
               "the watch lock and this one did nothing; a loop stopped by Ctrl+C",
               "1    a PR is red",
               "2    a PR could not be checked, the watch lock could not be taken, or "
               "another pass has held it past 12 hours",
               "130  --once stopped by hand (Ctrl+C)")),
    ("conflicts", ("0    no conflict",
                   "1    a PR conflicts with its base or with another PR",
                   "2    the check could not be made")),
    ("legs", ("0    a full-suite leg can run here",
              "2    none can, or --legs names no leg")),
])
def test_each_verbs_help_names_its_exit_codes_and_its_json_refusal(capsys, verb, lines):
    with pytest.raises(SystemExit):
        parse(verb, "--help")
    text = capsys.readouterr().out
    assert "exit codes:" in text
    tail = text.split("exit codes:", 1)[1]
    for line in lines:
        assert "  " + line in tail, line
    assert '{"verdict": "error", "exit_code": N, "reason": "..."}' in tail


def test_watch_help_says_what_a_pass_about_its_lock_prints(capsys):
    with pytest.raises(SystemExit):
        parse("watch", "--help")
    text = " ".join(capsys.readouterr().out.split())
    assert "An action about the pass rather than a PR has pr 0" in text
    assert "also has lock, holder_pid and held_s" in text
    assert "an error with them when its holder has held it past 12 hours" in text
    assert watch.STUCK_S == 12 * 3600                   # the hours the help names


def _row(**kw):
    return {"text": "t", "argv": ["hive"], "verified_by": "python", "scope": "ci",
            "mode": "github", "merge_state": "skipped", **kw}


def test_status_picks_runs_by_gate_and_branch_and_says_when_each_was_recorded(capsys):
    assert (parse("status", "--gate", "premerge", "--branch", "b").gate,
            parse("status", "--branch", "b").branch) == ("premerge", "b")
    events.append_event("verification", _row(gate="premerge", pr_id=151, branch="claude/x",
                                             exit_code=0, verdict="green"), project="test")
    events.append_event("verification", _row(gate="premerge", pr_id=0, branch="feature/y",
                                             exit_code=1, verdict="red"), project="test")
    events.append_event("verification", _row(gate="pytest", pr_id=151, branch="claude/x",
                                             exit_code=0), project="test")
    stamps = [e.ts for e in events.read_all("test") if e.type == "verification"]

    def status(**kw):
        args = SimpleNamespace(**{"project": "test", "pr": None, "page": 1, "json": True,
                                  "gate": None, "branch": None, **kw})
        code = ci_cli.cmd_ci_status(args)
        return code, json.loads(capsys.readouterr().out)
    code, row = status()
    assert (code, row["gate"], row["recorded_at"]) == (0, "pytest", stamps[2])
    code, row = status(gate="premerge")
    assert (code, row["branch"], row["recorded_at"]) == (1, "feature/y", stamps[1])
    code, row = status(gate="premerge", pr=151)
    assert (code, row["branch"], row["recorded_at"]) == (0, "claude/x", stamps[0])
    code, row = status(branch="feature/y")
    assert (code, row["pr_id"]) == (1, 0)
    assert status(gate="nosuch") == (2, {"found": False, "pr": None, "gate": "nosuch",
                                         "branch": None, "page": 1})


def test_status_says_when_a_run_was_recorded_in_words_too(capsys):
    events.append_event("verification", _row(gate="premerge", pr_id=151, branch="claude/x",
                                             exit_code=0, verdict="green"), project="test")
    ts = [e.ts for e in events.read_all("test") if e.type == "verification"][0]
    args = SimpleNamespace(project="test", pr=None, page=1, json=False)
    assert ci_cli.cmd_ci_status(args) == 0
    assert f"recorded: {ts}" in capsys.readouterr().out


def test_watch_json_without_once_prints_one_line_per_pass(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(premerge, "Premerge", Recorder())
    passes = [[{"pr": 1, "action": "tested", "verdict": "green", "exit_code": 0,
                "detail": "green: 9 leg(s)"}],
              [{"pr": 1, "action": "unchanged", "verdict": "green", "exit_code": 0,
                "detail": ""}]]

    class FW(_FW):
        def loop(self, report=None):
            for actions in passes:
                report(actions)
            raise KeyboardInterrupt
    monkeypatch.setattr(watch, "Watcher", FW)
    assert run("watch", "--json", "--root", str(tmp_path)) == 0
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == passes


def test_no_ci_module_reaches_into_another_modules_private_names():
    import ast
    import pathlib

    from mindhive import ci
    found = []
    for path in sorted(pathlib.Path(ci.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                found += [f"{path.name}: from {'.' * node.level}{node.module or ''} "
                          f"import {alias.name}"
                          for alias in node.names if alias.name.startswith("_")]
    assert found == []
