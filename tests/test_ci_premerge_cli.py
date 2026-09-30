# SPDX-License-Identifier: Apache-2.0
"""`hive ci premerge | watch | conflicts | legs` -- parsing, dispatch, exit
codes and output. The modules behind the verbs are swapped for fakes at the
names the handlers look up, so what runs here is the handler and argparse."""
import json
from types import SimpleNamespace

import pytest

from mindhive import __main__ as cli_mod
from mindhive import events
from mindhive.ci import cli as ci_cli
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
               "mode": "github", "merge_state": "refused: the PR is a draft",
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
    events.append_event("verification", {**payload, "verdict": "red", "exit_code": 1},
                        project="test")
    assert ci_cli.cmd_ci_status(args) == 1
