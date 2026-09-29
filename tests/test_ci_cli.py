# SPDX-License-Identifier: Apache-2.0
"""`hive ci breaker` (blueprint P1f).

The command exists because the state does. An adversarial review found that
QUARANTINED was reachable, that `IntegrationBreaker.manual_reset` was its only
exit, and that `manual_reset` had no caller anywhere in `src/` -- so the verdict
told the operator to run `hive ci breaker reset` and argparse answered "invalid
choice". The recovery left was deleting the sidecar by hand.

Two properties carry the weight here:

* **`status` does not move the circuit.** `check()` is a transition, not a
  question: an OPEN circuit whose cooldown has elapsed writes HALF_OPEN and
  appends an event as it answers. A status command built on it would change
  what it was asked to report.
* **`reset` is total.** From any state, including CLOSED, so a script does not
  have to know the state before asking for one.
"""
import json
import time

import pytest

from mindhive import __main__ as cli_mod
from mindhive import events, models
from mindhive.ci import cli as ci_cli
from mindhive.orchestrator import breaker as brk


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MINDHIVE_EVENT_BACKEND", raising=False)
    return tmp_path


def _args(**kw):
    from types import SimpleNamespace
    kw.setdefault("project", "p")
    kw.setdefault("json", False)
    return SimpleNamespace(**kw)


def _quarantine(project="p"):
    """Three trips inside the window: the state that has no cooldown out."""
    now = {"t": 1_000_000.0}
    b = brk.IntegrationBreaker(project=project, clock=lambda: now["t"])
    for _ in range(3):                       # trip 1
        b.record_failure("boom")
    for _ in range(2):                       # a probe, then trips 2 and 3
        now["t"] += b.COOLDOWN_PERIOD_S + 1.0
        b.check()
        b.record_failure("boom")
    return b


# --- the parser is really wired -------------------------------------------------------

def test_the_group_is_on_the_live_parser_with_both_verbs():
    parser = cli_mod._build_parser()
    got = parser.parse_args(["ci", "breaker", "status", "--project", "p", "--json"])
    assert got.func is ci_cli.cmd_ci_breaker_status
    assert (got.project, got.json) == ("p", True)
    reset = parser.parse_args(["ci", "breaker", "reset"])
    assert reset.func is ci_cli.cmd_ci_breaker_reset
    assert (reset.project, reset.json) == (None, False)


def test_a_hostile_project_is_refused_at_the_keyboard():
    """`cliparse.project_arg`, like every other `--project` on the surface."""
    with pytest.raises(SystemExit):
        cli_mod._build_parser().parse_args(["ci", "breaker", "status",
                                            "--project", "../../x"])


def test_the_group_is_required_to_name_a_verb():
    for argv in (["ci"], ["ci", "breaker"]):
        with pytest.raises(SystemExit):
            cli_mod._build_parser().parse_args(argv)


# --- status reads, and only reads ------------------------------------------------------

def test_status_on_an_untouched_project_says_so(capsys):
    code = ci_cli.cmd_ci_breaker_status(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (report["state"], report["exists"]) == (models.CLOSED, False)
    assert report["cooldown_left_s"] == 0
    assert report["path"].endswith(brk.STATE_FILENAME)


def test_status_never_moves_an_open_circuit_whose_cooldown_has_elapsed(capsys):
    """DISCRIMINATING, and stamped in the real epoch so the default clock the
    command uses agrees with it (that epoch is itself a round-2 fix).

    The cooldown here has ELAPSED, which is exactly when `check()` transitions:
    built on it this test fails twice over -- the state on disk becomes
    HALF_OPEN and a `state_transition` event appears -- a question that answered
    by changing its subject."""
    stale = time.time() - (brk.IntegrationBreaker.COOLDOWN_PERIOD_S + 100.0)
    b = brk.IntegrationBreaker(project="p", clock=lambda: stale)
    for _ in range(3):
        b.record_failure("boom")
    assert b.state().state == models.OPEN
    before = len(events.read_all("p"))

    code = ci_cli.cmd_ci_breaker_status(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert code == 1                                     # OPEN: a script can gate
    assert report["state"] == models.OPEN
    assert report["cooldown_left_s"] == 0                # elapsed, and unmoved
    assert brk.IntegrationBreaker(project="p").state().state == models.OPEN
    assert len(events.read_all("p")) == before
    # and `check` really would have moved it, which is what makes this a test
    assert brk.IntegrationBreaker(project="p").check().state == models.HALF_OPEN


def test_status_names_the_cooldown_still_to_wait(capsys):
    """The other side of the same field: a trip the default clock sees as
    FRESH still has its whole cooldown ahead of it."""
    b = brk.IntegrationBreaker(project="p")           # the real, persisted epoch
    for _ in range(3):
        b.record_failure("boom")
    ci_cli.cmd_ci_breaker_status(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert report["cooldown_left_s"] == pytest.approx(
        brk.IntegrationBreaker.COOLDOWN_PERIOD_S, abs=30.0)


def test_status_exits_two_while_quarantined(capsys):
    _quarantine()
    code = ci_cli.cmd_ci_breaker_status(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["state"] == models.QUARANTINED
    assert report["quarantine_reason"]
    assert report["trips_in_window"] == brk.IntegrationBreaker.MAX_TRIPS_IN_WINDOW


def test_the_human_report_names_the_record_and_the_reason(capsys):
    _quarantine()
    ci_cli.cmd_ci_breaker_status(_args())
    out = capsys.readouterr().out
    assert f"state: {models.QUARANTINED}" in out
    assert "quarantined:" in out
    assert brk.STATE_FILENAME in out


# --- reset is the exit, from anywhere --------------------------------------------------

def test_reset_is_the_way_out_of_quarantine(capsys):
    """The finding, closed: before this the only recovery was deleting
    `integration_breaker.json` by hand."""
    _quarantine()
    code = ci_cli.cmd_ci_breaker_reset(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (report["was"], report["state"]) == (models.QUARANTINED, models.CLOSED)
    assert report["changed"] is True
    assert report["trips_in_window"] == 0 and report["quarantine_reason"] is None
    # and a fresh process agrees, which is the whole point of the sidecar
    after = brk.IntegrationBreaker(project="p")
    assert after.check().allowed is True


def test_reset_of_a_closed_circuit_is_a_named_no_op(capsys):
    code = ci_cli.cmd_ci_breaker_reset(_args(json=True))
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (report["was"], report["state"]) == (models.CLOSED, models.CLOSED)
    assert report["changed"] is False


def test_the_human_reset_says_which_way_it_went(capsys):
    _quarantine()
    ci_cli.cmd_ci_breaker_reset(_args())
    out = capsys.readouterr().out
    assert f"{models.QUARANTINED} -> {models.CLOSED}" in out
    ci_cli.cmd_ci_breaker_reset(_args())
    assert "nothing to reset" in capsys.readouterr().out


def test_the_verdict_names_a_command_that_now_runs():
    """The other half of the finding: the refusal text told the operator to run
    something argparse answered `invalid choice` to."""
    named = _quarantine().check().reason
    assert "hive ci breaker reset" in named
    argv = named.split("jalankan ", 1)[1].split(" setelah", 1)[0].split()
    assert argv[0] == "hive"
    got = cli_mod._build_parser().parse_args(argv[1:])
    assert got.func is ci_cli.cmd_ci_breaker_reset


# --- the group costs nothing to offer ---------------------------------------------------

def test_the_package_init_imports_nothing():
    import ast
    from pathlib import Path
    src = Path(ci_cli.__file__).resolve().parent / "__init__.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    assert [n for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))] == []


def test_building_the_parser_does_not_import_the_orchestrator():
    """`hive --help` must not pay for the orchestrator package: the breaker is
    imported inside the handlers, not at module load."""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(ci_cli.__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [a.name for n in top for a in n.names]
    assert "orchestrator" not in " ".join(names)
    # `run`, `status` and `hook-install` need four more names, all stdlib.
    # Nothing from this package beyond `cliparse` and `paths`, and nothing
    # that reaches the store, the runner or git: those are imported inside
    # the handlers that use them.
    assert sorted(names) == ["Path", "cliparse", "json", "os", "paths",
                             "stat", "sys"]


# --- `hive ci run` ----------------------------------------------------------------------


class _FakeRun:
    """Stands in for `NativeCIRunner`, recording how it was built and called."""

    built = []
    calls = []
    result = None

    def __init__(self, repo_root, project=None, **kw):
        type(self).built.append({"repo_root": str(repo_root), "project": project})

    def run_pr_gate(self, pr_id, branch, auto_merge=False):
        type(self).calls.append({"pr_id": pr_id, "branch": branch,
                                 "auto_merge": auto_merge})
        return type(self).result


@pytest.fixture
def fake_runner(monkeypatch, tmp_path):
    from mindhive.ci import runner as rn
    _FakeRun.built, _FakeRun.calls = [], []
    _FakeRun.result = rn.CIRunResult(pr_id=7, branch="b", ok=True, exit_code=0,
                                     counts={"passed": 12},
                                     report_text="gate2_suite: exit 0")
    monkeypatch.setattr(rn, "NativeCIRunner", _FakeRun)
    root = tmp_path / "root"
    root.mkdir()
    return _FakeRun, root


def test_run_is_on_the_live_parser(fake_runner):
    parser = cli_mod._build_parser()
    got = parser.parse_args(["ci", "run", "--pr", "7", "--branch", "feat/x",
                             "--merge", "--json"])
    assert got.func is ci_cli.cmd_ci_run
    assert (got.pr, got.branch, got.merge, got.json) == (7, "feat/x", True, True)
    bare = parser.parse_args(["ci", "run", "--branch", "b"])
    assert (bare.pr, bare.merge, bare.json) == (0, False, False)


def test_run_exits_zero_on_green_and_one_on_red(fake_runner, capsys):
    fake, root = fake_runner
    assert ci_cli.cmd_ci_run(_args(pr=7, branch="b", merge=False, root=str(root),
                                   project="p")) == 0
    fake.result.ok = False
    fake.result.exit_code = 1
    assert ci_cli.cmd_ci_run(_args(pr=7, branch="b", merge=False, root=str(root),
                                   project="p")) == 1


def test_run_passes_the_pr_the_branch_and_the_merge_flag(fake_runner):
    fake, root = fake_runner
    ci_cli.cmd_ci_run(_args(pr=7, branch="feat/x", merge=True, root=str(root),
                            project="p"))
    assert fake.calls == [{"pr_id": 7, "branch": "feat/x", "auto_merge": True}]
    assert fake.built[0]["project"] == "p"
    assert fake.built[0]["repo_root"] == str(root)


def test_run_reads_the_branch_from_git_when_none_was_given(fake_runner,
                                                            monkeypatch):
    fake, root = fake_runner
    from mindhive.ci import runner as rn
    monkeypatch.setattr(rn, "_default_runner",
                        lambda argv, **kw: (0, "feature/from-git\n", ""))
    ci_cli.cmd_ci_run(_args(pr=0, branch=None, merge=False, root=str(root),
                            project="p"))
    assert fake.calls[0]["branch"] == "feature/from-git"


def test_run_refuses_rather_than_guessing_a_branch(fake_runner, monkeypatch,
                                                    capsys):
    """A CI run against the wrong ref is worse than one that did not start."""
    fake, root = fake_runner
    from mindhive.ci import runner as rn
    monkeypatch.setattr(rn, "_default_runner",
                        lambda argv, **kw: (128, "", "fatal: not a repository"))
    assert ci_cli.cmd_ci_run(_args(pr=0, branch=None, merge=False,
                                   root=str(root), project="p")) == 2
    assert fake.calls == []
    assert "branch" in capsys.readouterr().err


def test_run_json_prints_the_whole_result(fake_runner, capsys):
    fake, root = fake_runner
    ci_cli.cmd_ci_run(_args(pr=7, branch="b", merge=False, root=str(root),
                            project="p", json=True))
    got = json.loads(capsys.readouterr().out)
    assert got["ok"] is True and got["counts"] == {"passed": 12}
    assert got["verified_by"] == "python"
    assert got["merge_state"] in ("merged", "pending_operator", "refused",
                                  "skipped")


# --- `hive ci status` -------------------------------------------------------------------


def _record(pr, code, project="p", scope="ci"):
    from mindhive import verify
    payload = {"text": f"hive ci: PR #{pr}", "argv": ["python", "-m", "pytest"],
               "exit_code": code, "counts": {"passed": 1}, "excerpt": "",
               "verified_by": verify.VERIFIED_BY, "scope": scope, "pr_id": pr,
               "gate": "gate2_suite", "branch": f"b{pr}", "commit_sha": "",
               "mode": "offline", "merge_state": "skipped"}
    events.append_event("verification", payload, tags=["verify"], project=project)


def test_status_on_a_store_with_no_ci_run_says_so(capsys):
    assert ci_cli.cmd_ci_status(_args(pr=None, page=1)) == 2
    assert "no" in capsys.readouterr().out.lower()


def test_status_prints_the_last_ci_run_and_exits_on_its_colour(capsys):
    _record(7, 0)
    assert ci_cli.cmd_ci_status(_args(pr=None, page=1)) == 0
    _record(8, 1)
    assert ci_cli.cmd_ci_status(_args(pr=None, page=1)) == 1
    assert "8" in capsys.readouterr().out


def test_status_ignores_a_verification_that_is_not_a_ci_run(capsys):
    """A `hive selftest` run is a verification too, and reporting it as the
    last CI run would tell the operator a gate passed that never ran."""
    _record(7, 0, scope="full")
    assert ci_cli.cmd_ci_status(_args(pr=None, page=1)) == 2


def test_status_can_be_asked_about_one_pr(capsys):
    _record(7, 1)
    _record(8, 0)
    assert ci_cli.cmd_ci_status(_args(pr=7, page=1)) == 1
    out = capsys.readouterr().out
    assert "b7" in out and "b8" not in out


def test_status_pages_back_through_the_runs(capsys):
    _record(7, 1)
    _record(8, 0)
    assert ci_cli.cmd_ci_status(_args(pr=None, page=2)) == 1
    assert "b7" in capsys.readouterr().out


def test_status_json_is_the_payload(capsys):
    _record(7, 0)
    ci_cli.cmd_ci_status(_args(pr=None, page=1, json=True))
    got = json.loads(capsys.readouterr().out)
    assert got["scope"] == "ci" and got["pr_id"] == 7


# --- `hive ci hook-install` ---------------------------------------------------------------


def _repo(tmp_path, name="repo"):
    import subprocess
    root = tmp_path / name
    root.mkdir(parents=True)
    for argv in (["init", "-q", "-b", "master"],
                 ["config", "user.email", "ci@example.invalid"],
                 ["config", "user.name", "ci"]):
        subprocess.run(["git", *argv], cwd=str(root), capture_output=True)
    (root / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(root), capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=str(root),
                   capture_output=True)
    return root


def test_hook_install_writes_an_executable_posix_pre_push(tmp_path, capsys):
    root = _repo(tmp_path)
    assert ci_cli.cmd_ci_hook_install(_args(root=str(root), force=False)) == 0
    hook = root / ".git" / "hooks" / "pre-push"
    body = hook.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/sh\n")
    assert 'hive ci run --branch "$branch"' in body
    assert "\r\n" not in body, "a CRLF hook is not executable by sh"
    import os
    import stat as stat_mod
    if os.name != "nt":
        assert os.stat(hook).st_mode & stat_mod.S_IXUSR
    assert str(hook) in capsys.readouterr().out


def test_hook_install_asks_for_the_execute_bit_on_every_platform(tmp_path,
                                                                  monkeypatch):
    """Windows `os.stat` reports no x bit whatever `chmod` was handed, so the
    assertion above cannot run here — and the bit is the whole difference
    between an installed hook and a file git ignores. Pin the REQUEST."""
    import stat as stat_mod
    root = _repo(tmp_path)
    asked = []
    monkeypatch.setattr(ci_cli.os, "chmod",
                        lambda p, mode: asked.append((str(p), mode)))
    assert ci_cli.cmd_ci_hook_install(_args(root=str(root), force=False)) == 0
    assert len(asked) == 1
    path, mode = asked[0]
    assert path.endswith("pre-push")
    for bit in (stat_mod.S_IXUSR, stat_mod.S_IXGRP, stat_mod.S_IXOTH):
        assert mode & bit, oct(mode)


def test_a_chmod_the_platform_refuses_does_not_lose_the_hook(tmp_path,
                                                              monkeypatch):
    def refuse(*a, **k):
        raise OSError(1, "not supported")

    root = _repo(tmp_path)
    monkeypatch.setattr(ci_cli.os, "chmod", refuse)
    assert ci_cli.cmd_ci_hook_install(_args(root=str(root), force=False)) == 0
    assert (root / ".git" / "hooks" / "pre-push").exists()


def test_hook_install_does_not_clobber_a_hook_someone_else_wrote(tmp_path,
                                                                  capsys):
    root = _repo(tmp_path)
    hook = root / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    assert ci_cli.cmd_ci_hook_install(_args(root=str(root), force=False)) == 1
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert "--force" in capsys.readouterr().out


def test_hook_install_force_replaces_it(tmp_path):
    root = _repo(tmp_path)
    hook = root / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    assert ci_cli.cmd_ci_hook_install(_args(root=str(root), force=True)) == 0
    assert "hive ci run" in hook.read_text(encoding="utf-8")


def test_hook_install_from_a_linked_worktree_writes_to_the_repos_own_hooks(
        tmp_path):
    """`.git` in a worktree is a FILE, and git reads hooks from the COMMON
    dir. Writing `<worktree>/.git/hooks/pre-push` would install a hook that
    never runs, and `.git` being a file means the write fails outright."""
    import subprocess
    root = _repo(tmp_path)
    wt = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", "--detach", str(wt)],
                   cwd=str(root), capture_output=True)
    assert (wt / ".git").is_file()
    assert ci_cli.cmd_ci_hook_install(_args(root=str(wt), force=False)) == 0
    assert (root / ".git" / "hooks" / "pre-push").exists()


def test_hook_install_outside_a_repository_says_so(tmp_path, capsys):
    assert ci_cli.cmd_ci_hook_install(_args(root=str(tmp_path), force=False)) == 2
    assert "git" in capsys.readouterr().err.lower()


# --- the GitHub workflow is manual-only now (RP III.2) --------------------------------


def _workflow_on_block() -> list:
    """The `on:` block of `.github/workflows/ci.yml`, as its own lines.

    Parsed by hand rather than with PyYAML: the dev extra is pytest alone, so
    a test that imports yaml is a test that errors on a fresh checkout — the
    exact shape of green-badge-says-nothing this file exists to prevent.
    """
    from pathlib import Path as _P
    root = _P(cli_mod.__file__).resolve().parents[2]
    lines = (root / ".github/workflows/ci.yml").read_text(
        encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "on:")
    block = []
    for ln in lines[start + 1:]:
        if ln.strip() and not ln.startswith((" ", "\t")):
            break
        block.append(ln)
    return [ln for ln in block if ln.strip() and not ln.strip().startswith("#")]


def test_github_workflow_is_manual_only():
    block = _workflow_on_block()
    joined = "\n".join(block)
    assert "workflow_dispatch:" in joined
    assert "tags:" in joined
    assert "pull_request" not in joined, block
    assert "branches:" not in joined, block


def test_the_workflow_says_where_the_daily_gate_moved_to():
    from pathlib import Path as _P
    root = _P(cli_mod.__file__).resolve().parents[2]
    text = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "hive ci" in text
