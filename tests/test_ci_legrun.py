# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.legrun` -- one leg, run the way its ci.yml job runs it.

Most tests drive `LegRunner` through a fake `run` that answers each child the
way the real one would, and writes into the leg's log what the real child
would have printed -- so what is asserted is the order of the steps, their
argv, where they ran and what environment they got. Some use the real thing: a
toy repository run end to end through the host interpreter (a fresh repository
whose HEAD is the toy's commit, its history borrowed, then pytest), and the WSL
leg script under a real bash, stopping at the first step it cannot do.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive.ci import legrun, legs

COMMIT = "c" * 40
#: What `git rev-parse --git-path objects` answers for the operator's repository.
OBJECTS = "C:/origin/.git/objects"
REAL_DSN = "postgresql://owner@localhost/real-mirror"
ENV = {"PATH": "C:/Windows/system32", "SYSTEMROOT": "C:/Windows", "TEMP": "C:/t",
       "PYTHONPATH": "C:/elsewhere/src", "PYTHONHOME": "C:/py", "VIRTUAL_ENV": "C:/v",
       "PYTEST_ADDOPTS": "-x", "MINDHIVE_PROJECT": "mindhive",
       "MINDHIVE_PG_DSN_ADMIN": REAL_DSN, "GIT_DIR": "C:/elsewhere/.git",
       "CLAUDECODE": "1", "ANTHROPIC_API_KEY": "sk-fake", "GITHUB_TOKEN": "ghp_fake",
       "MY_SECRET": "s", "DB_PASSWORD": "p", "gh_token": "lower-case"}
GONE = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTEST_ADDOPTS",
        "MINDHIVE_PROJECT", "MINDHIVE_PG_DSN_ADMIN", "GIT_DIR", "CLAUDECODE",
        "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "MY_SECRET", "DB_PASSWORD", "gh_token")
WSL_ROOT = "/tmp/mindhive-ci.AbC123"


def _is_pytest(argv) -> bool:
    return "-m" in argv and argv[argv.index("-m") + 1:argv.index("-m") + 2] == ["pytest"]


class FakeRun:
    """Answers a leg's children; writes what they would print into the log."""

    def __init__(self, pytest=(0, "...\n3 passed in 0.10s\n"), fail=None,
                 script=("", 0), raises=None,
                 fail_text="ERROR: No matching distribution found for pytest>=8\n"):
        self.pytest = pytest
        self.fail = fail
        self.fail_text = fail_text
        self.script = script
        self.raises = raises
        self.calls = []

    def __call__(self, argv, cwd=None, env=None, timeout_s=None, stdin=None, log=None):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, "cwd": None if cwd is None else str(cwd),
                           "env": env, "stdin": stdin, "log": log})
        line = " ".join(argv)

        def say(text):
            if log is not None and text:
                with open(log, "ab") as fh:
                    fh.write(text.encode("utf-8"))

        if self.raises and self.raises in line:
            raise RuntimeError("the runner itself broke")
        if self.fail and self.fail in line:
            say(self.fail_text)             # a captured child's stderr comes back
            return 1, "", ("" if log is not None else self.fail_text)
        if "--git-path" in argv:
            return 0, OBJECTS + "\n", ""
        if "reset" in argv and "--hard" in argv and cwd is not None:
            # What a checkout leaves behind: the tree, in the leg's repository.
            Path(cwd, "pyproject.toml").write_text("[project]\nname = 'toy'\n",
                                                   encoding="utf-8")
            return 0, "", ""
        if "mktemp" in argv:
            return 0, WSL_ROOT + "\n", ""
        if "bash" in argv:
            text, rc = self.script
            say(text)
            return rc, "", ""
        if _is_pytest(argv):
            rc, text = self.pytest
            say(text)
            cap = "python: killed at the 5s cap (job: the job object took the whole tree)"
            return rc, "", cap if rc == 124 else ""
        return 0, "", ""

    def argvs(self) -> list:
        return [c["argv"] for c in self.calls]

    def call(self, pred) -> dict:
        return next(c for c in self.calls if pred(c["argv"]))

    def index(self, pred) -> int:
        return next(i for i, c in enumerate(self.calls) if pred(c["argv"]))


def runner(tmp_path, fake, *, windows=True, environ=ENV, keep=False):
    return legrun.LegRunner(tmp_path / "origin", tmp_path / "work", run_id="pr1-t",
                            run=fake, environ=environ, project="test", timeout_s=5.0,
                            wsl_prefix=["wsl.exe", "--exec"],
                            wsl_env={"WSLENV": "", "WSL_UTF8": "1"},
                            keep=keep, windows=windows)


def leg(name):
    return legs.select(name)[0]


def avail(the_leg, python="C:/py312/python.exe", via="native"):
    return legs.Availability(the_leg, True, python=python, via=via)


# --- the pieces ---------------------------------------------------------------------


def test_pytests_own_exit_code_decides_the_state():
    assert [legrun.classify(rc) for rc in (0, 1, 2, 3, 4, 5, 124, -9)] == [
        "success", "failure", "error", "error", "error", "error", "error", "error"]


def test_the_leg_environment_is_built_not_inherited(tmp_path):
    env = legrun.leg_env(ENV, tmp_path / "leg", bin_dir=tmp_path / "venv" / "bin")
    assert [name for name in GONE if name in env] == []
    assert env["SYSTEMROOT"] == "C:/Windows" and env["TEMP"] == "C:/t"   # canary
    assert env["MINDHIVE_HOME"] == str(tmp_path / "leg" / "home")
    assert env["PATH"] == str(tmp_path / "venv" / "bin") + os.pathsep + "C:/Windows/system32"
    assert env["PIP_NO_INPUT"] == "1" and env["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
    assert ENV["PYTHONPATH"] == "C:/elsewhere/src"      # the caller's copy is untouched


def test_line_endings_follow_the_runner_the_job_names():
    assert legrun.autocrlf_for(leg("windows-py3.12")) == "true"
    assert legrun.autocrlf_for(leg("ubuntu-py3.12")) == "false"
    assert legrun.autocrlf_for(leg("node")) == "false"


# --- a native leg, step by step ------------------------------------------------------


def test_a_windows_leg_checks_out_the_commit_itself_then_runs_its_jobs_steps(tmp_path):
    fake = FakeRun()
    the_leg = leg("windows-py3.12")
    res = runner(tmp_path, fake).run(the_leg, avail(the_leg), COMMIT)
    assert (res.state, res.step, res.exit_code, res.counts) == (
        "success", "pytest", 0, {"passed": 3})
    work = tmp_path / "work" / "windows-py3.12"
    venv, repo = work / "venv", work / "repo"
    py = str(venv / "Scripts" / "python.exe")
    named = fake.call(lambda a: "--git-path" in a)
    assert named["argv"] == ["git", "rev-parse", "--path-format=absolute",
                             "--git-path", "objects"]
    assert named["cwd"] == str(tmp_path / "origin")
    order = [fake.index(lambda a: "--git-path" in a),
             fake.index(lambda a: a[-2:] == ["init", "-q"]),
             fake.index(lambda a: a[1:] == ["config", "core.autocrlf", "true"]),
             fake.index(lambda a: a[1:] == ["config", "core.longpaths", "true"]),
             fake.index(lambda a: a[1:] == ["config", "core.hooksPath",
                                            str(work / "no-hooks")]),
             fake.index(lambda a: a[1:] == ["reset", "-q", "--hard", COMMIT]),
             fake.index(lambda a: a == ["C:/py312/python.exe", "-m", "venv", str(venv)]),
             fake.index(lambda a: a == [py, "-m", "pip", "install", "--upgrade", "pip"]),
             fake.index(lambda a: a == [py, "-m", "pip", "install", "-e", ".[dev]"]),
             fake.index(lambda a: a == [py, "-m", "pytest", "-q"])]
    assert order == sorted(order)
    # The commit itself, checked out: nothing exported, added or committed.
    assert not any({"archive", "add", "commit", "update-index"} & set(a)
                   for a in fake.argvs())
    for c in fake.calls:
        if c["argv"][:2] == [py, "-m"] or c["argv"][0] == "git" and "--git-path" not in c["argv"]:
            assert c["cwd"] == str(repo), c["argv"]
    ran = fake.call(lambda a: a == [py, "-m", "pytest", "-q"])
    assert ran["env"]["PATH"].split(os.pathsep)[0] == str(venv / "Scripts")
    assert ran["env"]["MINDHIVE_HOME"] == str(work / "home")
    assert [name for name in GONE if name in ran["env"]] == []
    assert res.argv == (py, "-m", "pytest", "-q")
    assert not work.exists()                                  # removed after the run
    assert "3 passed" in Path(res.artifact).read_text(encoding="utf-8")


def test_the_leg_reads_the_operators_history_through_alternates(tmp_path):
    fake = FakeRun()
    the_leg = leg("windows-py3.12")
    runner(tmp_path, fake, keep=True).run(the_leg, avail(the_leg), COMMIT)
    info = tmp_path / "work" / "windows-py3.12" / "repo" / ".git" / "objects" / "info"
    assert (info / "alternates").read_bytes() == (OBJECTS + "\n").encode("utf-8")
    assert [p.name for p in info.iterdir()] == ["alternates"]


@pytest.mark.parametrize("via", ["native", "wsl"])
def test_an_object_store_git_cannot_name_is_an_error_before_any_checkout(tmp_path, via):
    fake = FakeRun(fail="--git-path", fail_text="fatal: not a git repository\n")
    the_leg = leg("ubuntu-py3.12" if via == "wsl" else "windows-py3.12")
    res = runner(tmp_path, fake).run(the_leg, avail(the_leg, via=via), COMMIT)
    assert (res.state, res.step, res.exit_code) == ("error", "objects", 1)
    assert res.reason == "fatal: not a git repository"
    assert fake.argvs() == [["git", "rev-parse", "--path-format=absolute",
                             "--git-path", "objects"]]


def test_a_commit_git_cannot_check_out_is_an_error_at_checkout(tmp_path):
    fake = FakeRun(fail="reset -q --hard", fail_text=f"fatal: bad object {COMMIT}\n")
    the_leg = leg("windows-py3.12")
    res = runner(tmp_path, fake).run(the_leg, avail(the_leg), COMMIT)
    assert (res.state, res.step, res.exit_code) == ("error", "checkout", 1)
    assert res.reason == f"fatal: bad object {COMMIT}"
    assert not any(_is_pytest(a) or "venv" in a for a in fake.argvs())



def test_a_red_suite_is_a_failure_with_its_failed_tests_named(tmp_path):
    out = ("F..E\nFAILED tests/test_x.py::test_y - assert 1 == 2\n"
           "ERROR tests/test_z.py::test_w - fixture 'db' not found\n"
           "1 failed, 2 passed, 1 error in 0.20s\n")
    the_leg = leg("windows-py3.11")
    res = runner(tmp_path, FakeRun(pytest=(1, out))).run(the_leg, avail(the_leg), COMMIT)
    assert (res.state, res.exit_code) == ("failure", 1)
    assert res.counts == {"failed": 1, "passed": 2, "errors": 1}
    assert res.failed_tests == ("FAILED tests/test_x.py::test_y - assert 1 == 2",
                                "ERROR tests/test_z.py::test_w - fixture 'db' not found")
    assert res.summary().startswith("1 failed, 1 error, 2 passed in ")


def test_a_setup_step_that_fails_is_an_error_at_that_step_and_no_suite_runs(tmp_path):
    fake = FakeRun(fail="pip install -e")
    the_leg = leg("windows-py3.14")
    res = runner(tmp_path, fake).run(the_leg, avail(the_leg), COMMIT)
    assert (res.state, res.step, res.exit_code) == ("error", "install", 1)
    assert res.reason == "ERROR: No matching distribution found for pytest>=8"
    assert not any(_is_pytest(a) for a in fake.argvs())
    assert res.summary() == "install: " + res.reason


def test_a_suite_that_passed_no_test_proved_nothing(tmp_path):
    the_leg = leg("windows-py3.12")
    res = runner(tmp_path, FakeRun(pytest=(0, "ssss\n4 skipped in 0.10s\n"))).run(
        the_leg, avail(the_leg), COMMIT)
    assert (res.state, res.step, res.exit_code) == ("error", "pytest", 0)
    assert res.counts == {"skipped": 4}
    assert res.summary() == "pytest: no test passed (4 skipped)"
    unread = runner(tmp_path, FakeRun(pytest=(0, "the summary went missing\n"))).run(
        the_leg, avail(the_leg), COMMIT)
    assert (unread.state, unread.counts) == ("success", {})   # absent is not zero


def test_a_suite_past_its_cap_is_an_error_that_says_so(tmp_path):
    the_leg = leg("windows-py3.12")
    res = runner(tmp_path, FakeRun(pytest=(124, "....\n"))).run(
        the_leg, avail(the_leg), COMMIT)
    assert (res.state, res.step, res.exit_code) == ("error", "pytest", 124)
    assert "5s cap" in res.reason


def test_the_browser_leg_runs_the_hive_interpreter_against_the_legs_own_src(tmp_path):
    fake = FakeRun(pytest=(0, "2 passed in 1.00s\n"))
    the_leg = leg("browser")
    host = "C:/venv/Scripts/python.exe"
    res = runner(tmp_path, fake).run(the_leg, avail(the_leg, python=host), COMMIT)
    assert res.state == "success"
    assert not any("venv" in a or "pip" in a for a in fake.argvs())
    ran = fake.call(_is_pytest)
    assert ran["argv"] == [host, "-m", "pytest", "-m", "browser", "-q"]
    work = tmp_path / "work" / "browser"
    assert ran["env"]["PYTHONPATH"] == str(work / "repo" / "src")
    assert ran["env"]["HF_HUB_OFFLINE"] == "1"
    assert ran["env"]["PATH"].split(os.pathsep)[0] == str(Path(host).parent)


CI_ADMIN = "postgresql://ci@localhost:55432/mindhive"
CI_READER = "postgresql://mindhive_reader@localhost:55432/mindhive"
PG_ENV = {**ENV, legs.PG_DSN_ENV: CI_ADMIN, legs.PG_RO_DSN_ENV: CI_READER}


def _is_cluster_check(argv) -> bool:
    return argv[1:2] == ["-c"] and argv[2:3] == [legrun.PG_CLUSTER_CHECK]


def test_the_pg_leg_migrates_the_throwaway_cluster_and_never_the_real_one(tmp_path):
    fake = FakeRun()
    the_leg = leg("pg")
    res = runner(tmp_path, fake, environ=PG_ENV).run(the_leg, avail(the_leg), COMMIT)
    assert res.state == "success"
    py = str(tmp_path / "work" / "pg" / "venv" / "Scripts" / "python.exe")
    install = fake.index(lambda a: a[:5] == [py, "-m", "pip", "install", "-e"])
    assert fake.calls[install]["argv"][-1] == ".[dev,pg]"
    check = fake.index(_is_cluster_check)
    migrate = fake.index(lambda a: a == [py, "-m", "mindhive", "pg", "migrate"])
    assert install < check < migrate < fake.index(_is_pytest)
    assert fake.calls[check]["argv"][0] == py
    for step in (check, migrate, fake.index(_is_pytest)):
        env = fake.calls[step]["env"]
        assert (env["MINDHIVE_PG_DSN_ADMIN"], env["MINDHIVE_PG_DSN_RO"]) == (
            CI_ADMIN, CI_READER)
    assert fake.call(_is_pytest)["argv"] == [py, "-m", "pytest", "-m", "pg", "-q"]
    assert not any(REAL_DSN in (c["env"] or {}).values() for c in fake.calls)


def test_a_cluster_the_leg_does_not_own_stops_it_before_the_migration(tmp_path):
    why = "the cluster also holds mindhive_live: the pg leg needs a Postgres cluster of its own"
    fake = FakeRun(fail="pg_database", fail_text=why + "\n")
    the_leg = leg("pg")
    res = runner(tmp_path, fake, environ=PG_ENV).run(the_leg, avail(the_leg), COMMIT)
    assert (res.state, res.step, res.reason) == ("error", "cluster-check", why)
    assert not any(a[-2:] == ["pg", "migrate"] for a in fake.argvs())
    assert not any(_is_pytest(a) for a in fake.argvs())


def test_the_cluster_check_is_python_that_asks_postgres_for_every_other_database():
    compile(legrun.PG_CLUSTER_CHECK, "<cluster-check>", "exec")
    assert "FROM pg_database WHERE NOT datistemplate" in legrun.PG_CLUSTER_CHECK
    assert "current_database(), 'postgres'" in legrun.PG_CLUSTER_CHECK
    assert 'os.environ["MINDHIVE_PG_DSN_ADMIN"]' in legrun.PG_CLUSTER_CHECK


def test_an_ubuntu_leg_on_linux_keeps_lf_and_uses_the_venvs_bin(tmp_path):
    fake = FakeRun()
    the_leg = leg("ubuntu-py3.12")
    res = runner(tmp_path, fake, windows=False).run(
        the_leg, avail(the_leg, python="/usr/bin/python3.12"), COMMIT)
    assert res.state == "success"
    assert fake.call(lambda a: a[1:3] == ["config", "core.autocrlf"])["argv"][3] == "false"
    py = str(tmp_path / "work" / "ubuntu-py3.12" / "venv" / "bin" / "python")
    assert fake.call(_is_pytest)["argv"] == [py, "-m", "pytest", "-q"]


def test_a_runner_that_breaks_is_an_error_never_a_raise(tmp_path):
    the_leg = leg("windows-py3.12")
    res = runner(tmp_path, FakeRun(raises="-m venv")).run(the_leg, avail(the_leg), COMMIT)
    assert res.state == "error"
    assert "RuntimeError: the runner itself broke" in res.reason
    assert not (tmp_path / "work" / "windows-py3.12").exists()


def test_keep_leaves_the_leg_for_inspection(tmp_path):
    the_leg = leg("windows-py3.12")
    runner(tmp_path, FakeRun(), keep=True).run(the_leg, avail(the_leg), COMMIT)
    assert (tmp_path / "work" / "windows-py3.12" / "repo" / "pyproject.toml").is_file()


# --- a WSL leg ------------------------------------------------------------------------

WSL_OK = ("::mindhive-ci:: step checkout\n::mindhive-ci:: step venv: ...\n"
          f"::mindhive-ci:: step pytest: {WSL_ROOT}-venv/bin/python -m pytest -q\n"
          "....\n5 passed in 1.00s\n::mindhive-ci:: pytest-exit 0\n")


def test_a_wsl_leg_runs_the_script_in_a_login_shell_on_the_commit_and_its_store(tmp_path):
    fake = FakeRun(script=(WSL_OK, 0))
    the_leg = leg("ubuntu-py3.14")
    py = "/home/u/.local/bin/python3.14"
    res = runner(tmp_path, fake).run(the_leg, avail(the_leg, python=py, via="wsl"), COMMIT)
    assert (res.state, res.step, res.exit_code, res.counts) == (
        "success", "pytest", 0, {"passed": 5})
    assert fake.call(lambda a: "--git-path" in a)["cwd"] == str(tmp_path / "origin")
    assert not any("archive" in a or "tar" in a for a in fake.argvs())
    script = fake.call(lambda a: "bash" in a)
    assert script["argv"] == ["wsl.exe", "--exec", "bash", "-l", "-s", "--", WSL_ROOT,
                              COMMIT, OBJECTS, py, "dev", "5", "-q"]
    assert script["stdin"] == legrun.POSIX_LEG_SCRIPT
    assert script["env"] == {"WSLENV": "", "WSL_UTF8": "1"}
    assert fake.calls[-1]["argv"] == ["wsl.exe", "--exec", "rm", "-rf", "--", WSL_ROOT,
                                      WSL_ROOT + "-venv", WSL_ROOT + "-home"]
    assert res.argv == (py, "-m", "pytest", "-q")


def test_keep_leaves_a_wsl_legs_tree_in_wsl_and_says_where(tmp_path):
    fake = FakeRun(script=(WSL_OK, 0))
    the_leg = leg("ubuntu-py3.12")
    res = runner(tmp_path, fake, keep=True).run(
        the_leg, avail(the_leg, python="python3.12", via="wsl"), COMMIT)
    assert res.state == "success"
    assert not any("rm" in a for a in fake.argvs())
    log = Path(res.artifact).read_text(encoding="utf-8")
    assert f"::mindhive-ci:: kept {WSL_ROOT} {WSL_ROOT}-venv {WSL_ROOT}-home" in log


def test_a_wsl_setup_failure_names_its_step_and_the_tree_is_still_removed(tmp_path):
    text = ("::mindhive-ci:: step venv: python3.12 -m venv /tmp/x-venv\n"
            "Error: ensurepip is not available\n"
            "::mindhive-ci:: setup-failed venv\n")
    fake = FakeRun(script=(text, legrun.SETUP_FAILED_EXIT))
    the_leg = leg("ubuntu-py3.12")
    res = runner(tmp_path, fake).run(
        the_leg, avail(the_leg, python="/usr/bin/python3.12", via="wsl"), COMMIT)
    assert (res.state, res.step, res.exit_code) == ("error", "venv", 97)
    assert res.reason == "Error: ensurepip is not available"
    assert fake.calls[-1]["argv"][2:4] == ["rm", "-rf"]


def test_a_wsl_that_never_ran_the_script_is_an_error_at_wsl(tmp_path):
    fake = FakeRun(script=("", 1))
    the_leg = leg("ubuntu-py3.11")
    res = runner(tmp_path, fake).run(
        the_leg, avail(the_leg, python="/home/u/.local/bin/python3.11", via="wsl"), COMMIT)
    assert (res.state, res.step, res.exit_code) == ("error", "wsl", 1)
    assert res.reason == "wsl.exe exit 1"
    assert fake.calls[-1]["argv"][2:4] == ["rm", "-rf"]


def test_a_red_suite_in_wsl_is_a_failure(tmp_path):
    text = (f"::mindhive-ci:: step pytest: {WSL_ROOT}-venv/bin/python -m pytest -q\n"
            "FAILED tests/test_a.py::test_b - boom\n1 failed, 4 passed in 2.00s\n"
            "::mindhive-ci:: pytest-exit 1\n")
    the_leg = leg("ubuntu-py3.12")
    res = runner(tmp_path, FakeRun(script=(text, 1))).run(
        the_leg, avail(the_leg, python="/usr/bin/python3.12", via="wsl"), COMMIT)
    assert (res.state, res.step, res.exit_code) == ("failure", "pytest", 1)
    assert res.failed_tests == ("FAILED tests/test_a.py::test_b - boom",)


# --- the real thing -------------------------------------------------------------------


def _bash():
    sh = shutil.which("sh")
    return shutil.which("bash", path=str(Path(sh).parent)) if sh else None


@pytest.mark.skipif(_bash() is None, reason="no POSIX sh on this machine")
def test_the_leg_script_stops_at_the_first_step_it_cannot_do(tmp_path):
    missing = tmp_path / "no such dir"
    proc = subprocess.run([_bash(), "-s", "--", str(missing), COMMIT, OBJECTS,
                           "python3.99", "dev", "60", "-q"],
                          input=legrun.POSIX_LEG_SCRIPT.encode("utf-8"),
                          capture_output=True, timeout=60)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == legrun.SETUP_FAILED_EXIT, (out, err)
    assert out.splitlines()[-1] == "::mindhive-ci:: setup-failed cd"
    assert "no such dir" in err            # the argument with spaces arrived whole


@pytest.mark.skipif(_bash() is None, reason="no POSIX sh on this machine")
def test_the_leg_script_unsets_what_leg_env_drops_whatever_its_case():
    """A WSL leg cleans its own environment inside Linux. It must drop exactly
    what `leg_env` drops on Windows -- the same lists, matched the same way --
    or a lower-case `gh_token` in a login profile reaches the tests."""
    script = legrun.POSIX_LEG_SCRIPT.replace('mhci_leg "$@" </dev/null',
                                             "mhci_scrub; compgen -e")
    assert script != legrun.POSIX_LEG_SCRIPT
    env = {**ENV, "PATH": os.environ.get("PATH", ""), "KEEP_ME": "1",
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:/Windows")}
    proc = subprocess.run([_bash(), "-s"], input=script.encode("utf-8"), env=env,
                          capture_output=True, timeout=60)
    names = set(proc.stdout.decode("utf-8", "replace").split())
    assert proc.returncode == 0, proc.stderr
    assert "KEEP_ME" in names                            # canary: the scrub is selective
    assert not names & set(GONE), names & set(GONE)
    assert not names & {n for n in names if n.upper() == "GH_TOKEN"}


TOY_TEST = """\
import os
import subprocess


def test_the_leg_holds_the_commit_under_test_and_its_history():
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "2"


def test_the_leg_environment_was_built_not_inherited():
    assert "MINDHIVE_PROJECT" not in os.environ
    assert "PYTEST_ADDOPTS" not in os.environ
    assert os.path.basename(os.environ["MINDHIVE_HOME"]) == "home"
"""


def _git(repo, *args) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _toy_origin(tmp_path):
    """Two commits; the second -- the one a leg tests -- holds a toy project.
    ``(origin, first, second)``."""
    origin = tmp_path / "origin"
    (origin / "tests").mkdir(parents=True)
    for rel, text in (("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'),
                      ("data.txt", "one\ntwo\n"), ("tests/test_toy.py", TOY_TEST),
                      ("run.sh", "#!/bin/sh\necho ok\n")):
        (origin / rel).write_text(text, encoding="utf-8", newline="\n")
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
    _git(origin, "init", "-q", "-b", "master")
    _git(origin, *ident, "commit", "-q", "--allow-empty", "-m", "first")
    first = _git(origin, "rev-parse", "HEAD")
    _git(origin, *ident, "add", "-A")
    _git(origin, "update-index", "--chmod=+x", "run.sh")
    _git(origin, *ident, "commit", "-q", "-m", "toy")
    return origin, first, _git(origin, "rev-parse", "HEAD")


@pytest.mark.parametrize("runs_on,data", [("windows-latest", b"one\r\ntwo\r\n"),
                                          ("ubuntu-latest", b"one\ntwo\n")])
def test_a_real_leg_checks_out_the_commit_with_its_history_and_runs_the_suite(
        tmp_path, runs_on, data):
    origin, first, commit = _toy_origin(tmp_path)
    refs = _git(origin, "for-each-ref")
    toy = legs.Leg(name="toy", job="test", ci_name="toy", runs_on=runs_on,
                   host=legs.HOST_LOCAL, python="", tier=legs.TIER_DEFAULT,
                   extras="dev", note="a toy leg")
    environ = {**os.environ, "MINDHIVE_PROJECT": "leaky",
               "PYTEST_ADDOPTS": "-x --no-such-flag"}
    res = legrun.LegRunner(origin, tmp_path / "work", run_id="toy-1", environ=environ,
                           project="test", keep=True, timeout_s=120).run(
        toy, legs.Availability(toy, True, python=sys.executable, via="native"), commit)
    log = Path(res.artifact).read_text(encoding="utf-8") if res.artifact else ""
    assert res.state == "success", (res.step, res.reason, log)
    assert res.counts.get("passed") == 2
    repo = tmp_path / "work" / "toy" / "repo"
    assert (repo / "data.txt").read_bytes() == data
    # The commit itself, with its history readable -- what `fetch-depth: 0`
    # checks out -- never a one-commit copy of its tree.
    assert _git(repo, "rev-parse", "HEAD") == commit
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    assert _git(repo, "cat-file", "-t", first) == "commit"
    # Tracked files only: the leg's own pytest writes `tests/__pycache__/`, as
    # CI's does. What this proves is that the checkout's line endings and the
    # repository's autocrlf agree, so no tracked file reads as modified.
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    # The modes are the tree's own, on every OS: a file the tree holds 100755
    # is so in the leg's index, and one it holds 100644 stays so.
    assert _git(repo, "ls-files", "-s", "run.sh").split()[0] == "100755"
    assert _git(repo, "ls-files", "-s", "data.txt").split()[0] == "100644"
    # The operator's repository was only read.
    assert _git(origin, "for-each-ref") == refs
    assert _git(origin, "status", "--porcelain") == ""


@pytest.mark.skipif(_bash() is None, reason="no POSIX sh on this machine")
def test_the_leg_script_checks_out_the_commit_with_its_history_first(tmp_path):
    origin, first, commit = _toy_origin(tmp_path)
    objects = _git(origin, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    root = tmp_path / "leg"
    root.mkdir()
    proc = subprocess.run([_bash(), "-s", "--", root.as_posix(), commit, objects,
                           "python3.99", "dev", "60", "-q"],
                          input=legrun.POSIX_LEG_SCRIPT.encode("utf-8"),
                          capture_output=True, timeout=120)
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == legrun.SETUP_FAILED_EXIT, (out, proc.stderr)
    assert out.splitlines()[-1] == "::mindhive-ci:: setup-failed venv"   # past the checkout
    assert _git(root, "rev-parse", "HEAD") == commit
    assert _git(root, "rev-list", "--count", "HEAD") == "2"
    assert _git(root, "cat-file", "-t", first) == "commit"
    assert _git(root, "status", "--porcelain") == ""
    assert (root / "data.txt").read_bytes() == b"one\ntwo\n"          # LF, as ubuntu has it



# --- what the runner refuses, and what it does when a step cannot -------------------


@pytest.mark.skipif(_bash() is None, reason="no POSIX sh on this machine")
def test_the_leg_script_scrubs_the_environment_before_it_builds_anything(tmp_path):
    root = tmp_path / "leg"
    root.mkdir()
    (root / "f.txt").write_text("x", encoding="utf-8")
    shim = tmp_path / "shim"
    shim.mkdir()
    dump = tmp_path / "env.out"
    (shim / "git").write_text(f'#!/bin/sh\nenv > "{dump.as_posix()}"\nexit 0\n',
                              encoding="utf-8", newline="\n")
    os.chmod(shim / "git", 0o755)
    env = {**os.environ, "PATH": shim.as_posix() + os.pathsep + os.environ["PATH"],
           "GITHUB_TOKEN": "t", "gh_token": "t", "npm_config_token": "t",
           "MINDHIVE_PROJECT": "leaky", "PYTHONPATH": "/elsewhere", "KEEP_ME": "1"}
    proc = subprocess.run([_bash(), "-s", "--", root.as_posix(), COMMIT, OBJECTS,
                           "python3.99", "dev", "60", "-q"], input=legrun.POSIX_LEG_SCRIPT.encode("utf-8"),
                          capture_output=True, env=env, timeout=120)
    assert proc.returncode == legrun.SETUP_FAILED_EXIT, proc.stderr    # stops at the venv
    names = {line.split("=", 1)[0] for line in dump.read_text(encoding="utf-8").splitlines()}
    assert "KEEP_ME" in names and "MINDHIVE_HOME" in names             # canary
    leaked = names & {"GITHUB_TOKEN", "gh_token", "npm_config_token", "MINDHIVE_PROJECT",
                      "PYTHONPATH"}
    assert leaked == set()

@pytest.mark.skipif(_bash() is None, reason="no POSIX sh on this machine")
def test_the_leg_script_takes_windows_mnt_directories_off_path(tmp_path):
    root = tmp_path / "leg"
    root.mkdir()
    (root / "f.txt").write_text("x", encoding="utf-8")
    shim = tmp_path / "shim"
    shim.mkdir()
    dump = tmp_path / "env.out"
    (shim / "git").write_text(f'#!/bin/sh\nenv > "{dump.as_posix()}"\nexit 0\n',
                              encoding="utf-8", newline="\n")
    os.chmod(shim / "git", 0o755)
    env = {**os.environ, "PATH": "/mnt/c/Windows/System32" + os.pathsep + shim.as_posix() + os.pathsep
           + os.environ["PATH"]}
    proc = subprocess.run([_bash(), "-s", "--", root.as_posix(), COMMIT, OBJECTS,
                           "python3.99", "dev", "60", "-q"], input=legrun.POSIX_LEG_SCRIPT.encode("utf-8"),
                          capture_output=True, env=env, timeout=120)
    assert proc.returncode == legrun.SETUP_FAILED_EXIT, proc.stderr
    seen = [l for l in dump.read_text(encoding="utf-8").splitlines() if l.startswith("PATH=")]
    assert seen and any(shim.name in part for part in seen[0][5:].split(":"))   # canary
    assert not any(part.startswith("/mnt/") for part in seen[0][5:].split(":"))


def _wsl_run(tmp_path, fake, name="ubuntu-py3.12"):
    the_leg = leg(name)
    return runner(tmp_path, fake).run(
        the_leg, avail(the_leg, python="/usr/bin/python3.12", via="wsl"),
        COMMIT)

@pytest.mark.parametrize("answer,exit_code,reason", [
    ((1, "", "mktemp: failed to create directory"), 1, "mktemp: failed to create directory"),
    ((0, "wsl: a banner, not a path" + chr(10), ""), 0, "wsl: a banner, not a path")])
def test_a_wsl_that_cannot_make_its_temp_dir_is_an_error_at_mktemp(tmp_path, answer, exit_code, reason):
    base = FakeRun(script=(WSL_OK, 0))

    def run(argv, **kw):
        if "mktemp" in [str(a) for a in argv]:
            return answer
        return base(argv, **kw)
    res = _wsl_run(tmp_path, run)
    assert (res.state, res.step, res.exit_code) == ("error", "mktemp", exit_code)
    assert res.reason == reason
    assert not any("bash" in c["argv"] or "rm" in c["argv"] for c in base.calls)

@pytest.mark.parametrize("code", [124, 137])
def test_a_wsl_suite_killed_by_timeout_says_so(tmp_path, code):
    text = f"::mindhive-ci:: step pytest: x\n....\n::mindhive-ci:: pytest-exit {code}\n"
    res = _wsl_run(tmp_path, FakeRun(script=(text, code)))
    assert (res.state, res.step, res.exit_code) == ("error", "pytest", code)
    assert res.reason == f"pytest exit {code} -- killed at the cap"

def test_leg_env_drops_every_credential_shape_it_names(tmp_path):
    secrets = {"AWS_ACCESS_KEY_ID": "a", "GOOGLE_APPLICATION_CREDENTIALS": "g",
               "SSH_PRIVATE_KEY": "k", "STRIPE_APIKEY": "s", "DB_PASSWD": "d",
               "CONDA_PREFIX": "c", "UV_CACHE_DIR": "u", "MINDNETWORK_X": "m"}
    env = legrun.leg_env({"PATH": "p", "SYSTEMROOT": "C:/Windows", **secrets}, tmp_path)
    assert env["SYSTEMROOT"] == "C:/Windows"                             # canary
    assert set(env) & set(secrets) == set()

def test_a_runlog_that_cannot_be_written_loses_the_evidence_not_the_verdict(tmp_path, monkeypatch):
    the_leg = leg("windows-py3.12")
    text = "....\n1 passed in 0.10s\n"
    kept = runner(tmp_path / "a", FakeRun(pytest=(0, text))).run(
        the_leg, avail(the_leg), COMMIT)
    assert kept.state == "success" and kept.artifact                      # canary: it is written

    def refuse(*args, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(legrun.runlog, "write_once", refuse)
    lost = runner(tmp_path / "b", FakeRun(pytest=(0, text))).run(
        the_leg, avail(the_leg), COMMIT)
    assert (lost.state, lost.step, lost.exit_code) == ("success", "pytest", 0)
    assert lost.artifact == ""
