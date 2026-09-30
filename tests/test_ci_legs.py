# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.legs` -- the ci.yml jobs as legs, and what this machine can run.

The parity tests read `.github/workflows/ci.yml` itself, with a parser small
enough to trust and a canary proving it sees a job nobody told it about. The
probe tests drive `Probe` with a fake shaped by what was MEASURED on the
owner's machine on 2026-09-30: the `py -0p` lines, and the WSL login shell's
answer (uv's 3.11 and 3.14 live in `~/.local/bin`, on a login PATH only).
"""
import re
from pathlib import Path

import pytest

from mindhive.ci import legs

REPO = Path(__file__).resolve().parents[1]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"

#: `py -0p` on the owner's machine, verbatim.
PY_LIST = (
    " -V:3.14 *        C:\\Users\\hanif\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe\n"
    " -V:3.12          C:\\Users\\hanif\\AppData\\Local\\Programs\\Python\\Python312\\python.exe\n"
    " -V:Astral/CPython3.11.16 C:\\Users\\hanif\\AppData\\Roaming\\uv\\python\\"
    "cpython-3.11.16-windows-x86_64-none\\python.exe\n")

#: What the WSL probe script prints, behind a login banner a profile may add.
WSL_OK = ("Welcome to Ubuntu (a banner a login profile might print)\n"
          "MHCI py 3.11 /home/u/.local/bin/python3.11\n"
          "MHCI py 3.12 /usr/bin/python3.12\n"
          "MHCI py 3.14 /home/u/.local/bin/python3.14\n"
          "MHCI tool git\nMHCI tool timeout\nMHCI tool wslpath\nMHCI tool pip\n"
          "MHCI ok\n")


def _leg(name):
    return legs.select(name)[0]


# --- ci.yml, parsed ------------------------------------------------------------------


def parse_ci_yml(text) -> dict:
    """``{job_id: {"name", "runs_on", "pythons", "marker", "extras"}}``.

    Line-anchored on the workflow's own indentation -- jobs at two spaces, a
    job's own keys at four -- because a YAML library is not a dependency, and
    a regex over the whole file would read a step's `name:` as the job's.
    Comment lines are skipped: ci.yml's comments quote commands
    (`pip install -e .[browser]`) that belong to the NEXT job."""
    jobs, job, in_jobs = {}, None, False
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            job = jobs.setdefault(m.group(1), {"name": "", "runs_on": [],
                                               "pythons": [], "marker": None,
                                               "extras": ""})
            continue
        if job is None:
            continue
        if m := re.match(r"^    name:\s*(.+?)\s*$", line):
            job["name"] = m.group(1)
        elif m := re.match(r"^    runs-on:\s*(.+?)\s*$", line):
            if "${{" not in m.group(1):
                job["runs_on"].append(m.group(1))
        elif m := re.match(r"^\s+os:\s*\[(.+)\]\s*$", line):
            job["runs_on"] += [v.strip().strip("\"'") for v in m.group(1).split(",")]
        elif m := re.match(r"^\s+python-version:\s*(.+?)\s*$", line):
            if "${{" not in m.group(1):
                job["pythons"] += [v.strip().strip("\"'")
                                   for v in m.group(1).strip("[]").split(",")]
        elif m := re.search(r"python -m pytest -m (\w+) -q", line):
            job["marker"] = m.group(1)
        elif m := re.search(r"pip install -e \.\[([\w,]+)\]", line):
            job["extras"] = m.group(1)
    return jobs


@pytest.fixture(scope="module")
def ci_jobs():
    return parse_ci_yml(CI_YML.read_text(encoding="utf-8"))


def test_the_parser_sees_a_job_nobody_told_it_about():
    """Canary: without it, "every job has a leg" could pass on a parser that
    finds no job at all."""
    text = CI_YML.read_text(encoding="utf-8") + (
        "\n  canary-job:\n    name: canary\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: python -m pytest -m canary -q\n")
    jobs = parse_ci_yml(text)
    assert jobs["canary-job"]["marker"] == "canary"
    assert jobs["canary-job"]["runs_on"] == ["ubuntu-latest"]


def test_every_ci_job_has_a_leg_and_every_leg_a_ci_job(ci_jobs):
    assert len(ci_jobs) >= 4
    assert set(ci_jobs) == {leg.job for leg in legs.CATALOG}


def test_the_matrix_legs_are_exactly_the_matrix_cells(ci_jobs):
    matrix = ci_jobs["test"]
    cells = {(label, v) for label in matrix["runs_on"] for v in matrix["pythons"]}
    assert len(cells) == 6
    assert {(leg.runs_on, leg.python) for leg in legs.CATALOG
            if leg.job == "test"} == cells


def test_each_leg_carries_its_jobs_name_tier_extras_and_runner(ci_jobs):
    for leg in legs.CATALOG:
        job = ci_jobs[leg.job]
        name = (job["name"].replace("${{ matrix.os }}", leg.runs_on)
                .replace("${{ matrix.python-version }}", leg.python))
        assert leg.ci_name == name, leg.name
        assert leg.tier == (job["marker"] or legs.TIER_DEFAULT), leg.name
        assert leg.extras == job["extras"], leg.name
        assert leg.runs_on in job["runs_on"], leg.name
        if leg.python and leg.job != "test":
            assert job["pythons"] == [leg.python], leg.name


def parse_checkouts(text) -> list:
    """The `with:` of each `actions/checkout` step, in file order, as ``{key:
    value}`` -- read by indentation, as `parse_ci_yml` reads the jobs."""
    steps, step, indent, keyed = [], None, 0, False
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = re.match(r"^(\s*)- uses:\s*actions/checkout@\S+\s*$", line)
        if m:
            step, indent, keyed = {}, len(m.group(1)), False
            steps.append(step)
            continue
        if step is None:
            continue
        if len(line) - len(line.lstrip()) <= indent:       # the next step, or job
            step = None
        elif re.match(r"^\s+with:\s*$", line):
            keyed = True
        elif keyed and (m := re.match(r"^\s+([\w-]+):\s*(.+?)\s*$", line)):
            step[m.group(1)] = m.group(2).strip("\"'")
    return steps


def test_the_checkout_parser_sees_a_step_that_fetches_one_commit():
    """Canary: "every checkout fetches the history" must not pass on a parser
    that finds no checkout, or none without the key."""
    text = ("jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v5\n"
            "        with:\n          fetch-depth: 0\n      - run: x\n"
            "  b:\n    steps:\n      - uses: actions/checkout@v5\n      - run: y\n")
    assert parse_checkouts(text) == [{"fetch-depth": "0"}, {}]


def test_every_job_checks_out_the_whole_history_as_every_leg_has_it(ci_jobs):
    """A leg's HEAD is the commit under test with its whole history readable
    (`legrun`), so a job that checked out one commit would be red where the
    leg is green: `tests/test_implementation_plan.py` reads a commit from the
    history."""
    steps = parse_checkouts(CI_YML.read_text(encoding="utf-8"))
    assert len(steps) == len(ci_jobs)                     # one per job
    assert [step.get("fetch-depth") for step in steps] == ["0"] * len(ci_jobs)


def test_a_leg_runs_its_jobs_test_command_verbatim():
    assert _leg("windows-py3.12").pytest_args == ("-q",)
    assert _leg("ubuntu-py3.14").pytest_args == ("-q",)
    assert _leg("node").pytest_args == ("-m", "node", "-q")
    assert _leg("browser").pytest_args == ("-m", "browser", "-q")
    assert _leg("pg").pytest_args == ("-m", "pg", "-q")


def test_leg_names_are_unique_and_every_departure_is_written_down():
    names = [leg.name for leg in legs.CATALOG]
    assert len(names) == len(set(names))
    for leg in legs.CATALOG:
        if leg.host == legs.HOST_LOCAL or not leg.python:
            assert leg.note, leg.name
        else:
            assert leg.note == "", leg.name


# --- select ---------------------------------------------------------------------------


def test_select_takes_names_and_globs_in_catalog_order():
    got = [leg.name for leg in legs.select("node,windows-*")]
    assert got == ["windows-py3.11", "windows-py3.12", "windows-py3.14", "node"]
    assert legs.select(None) == legs.CATALOG == legs.select("")


def test_select_names_what_matched_nothing():
    with pytest.raises(ValueError) as excinfo:
        legs.select("windows-py3.12,ubuntu-py9.9")
    message = str(excinfo.value)
    assert message.startswith("no leg matches ubuntu-py9.9;")
    assert "windows-py3.12" in message.split(";", 1)[1]       # the list of legs


# --- py -0p ---------------------------------------------------------------------------


def test_py_list_is_parsed_including_an_install_python_org_did_not_make():
    assert legs.parse_py_list(PY_LIST) == {
        "3.14": "C:\\Users\\hanif\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe",
        "3.12": "C:\\Users\\hanif\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
        "3.11": "C:\\Users\\hanif\\AppData\\Roaming\\uv\\python\\"
                "cpython-3.11.16-windows-x86_64-none\\python.exe"}


def test_py_list_keeps_the_first_install_per_version_and_skips_junk():
    text = ("Installed Pythons found by py Launcher for Windows\n"
            " -3.12-64 *     C:\\A\\python.exe\n -3.12-32  C:\\B\\python.exe\n\n")
    assert legs.parse_py_list(text) == {"3.12": "C:\\A\\python.exe"}
    assert legs.parse_py_list(None) == {} == legs.parse_py_list("")


# --- the probe ------------------------------------------------------------------------


class Fake:
    """Answers the probe's children from rules; records every call."""

    def __init__(self, *rules):
        self.rules = list(rules)
        self.calls = []

    def __call__(self, argv, cwd=None, env=None, timeout_s=None, stdin=None, log=None):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, "env": env, "stdin": stdin})
        for pred, answer in self.rules:
            if pred(argv):
                return answer(argv) if callable(answer) else answer
        return 1, "", f"not scripted: {argv[:3]}"


def _version_of(path) -> str:
    for version, key in (("3.14", "3.14"), ("3.12", "312"), ("3.11", "3.11")):
        if key in path:
            return version
    return ""


def _is_check(argv) -> bool:
    return len(argv) == 3 and argv[1] == "-c" and "ensurepip" in argv[2]


def _is_chromium(argv) -> bool:
    return len(argv) == 3 and argv[1] == "-c" and "chromium.launch()" in argv[2]


def machine(pylist=PY_LIST, wsl=(0, WSL_OK, ""), playwright=(0, "", "")):
    return [(lambda a: a[:2] == ["py", "-0p"], (0, pylist, "")),
            (_is_check, lambda a: (0, _version_of(a[0]) + "\n", "")),
            (lambda a: "bash" in a and "-s" in a, wsl),
            (_is_chromium, playwright)]


def _probe(fake, platform="win32", node="C:\\node\\node.exe", environ=None,
           executable="C:\\venv\\Scripts\\python.exe", wsl_distro=None):
    return legs.Probe(run=fake, platform=platform,
                      which=lambda name, path=None: node if name == "node" else None,
                      environ={"PATH": "x"} if environ is None else environ,
                      executable=executable, wsl_distro=wsl_distro)


def test_on_the_owners_machine_every_leg_but_pg_can_run():
    probe = _probe(Fake(*machine()))
    got = {leg.name: probe.check(leg) for leg in legs.CATALOG}
    assert {name for name, a in got.items() if not a.ok} == {"pg"}
    assert got["windows-py3.11"].python.endswith(
        "cpython-3.11.16-windows-x86_64-none\\python.exe")
    assert got["windows-py3.11"].via == "native"
    assert got["ubuntu-py3.14"].python == "/home/u/.local/bin/python3.14"
    assert got["ubuntu-py3.14"].via == "wsl"
    assert got["browser"].python == "C:\\venv\\Scripts\\python.exe"
    assert got["node"].python.endswith("Python312\\python.exe")
    assert legs.PG_DSN_ENV in got["pg"].reason


def test_each_question_is_asked_once_per_probe():
    fake = Fake(*machine())
    probe = _probe(fake)
    for leg in legs.CATALOG:
        probe.check(leg)
        probe.check(leg)
    assert sum(c["argv"][:2] == ["py", "-0p"] for c in fake.calls) == 1
    assert sum("bash" in c["argv"] for c in fake.calls) == 1
    assert sum(_is_check(c["argv"]) for c in fake.calls) == 3


def test_the_wsl_question_is_a_login_shell_fed_on_stdin_forwarding_nothing():
    fake = Fake(*machine())
    _probe(fake).check(_leg("ubuntu-py3.12"))
    call = next(c for c in fake.calls if "bash" in c["argv"])
    assert call["argv"] == ["wsl.exe", "--exec", "bash", "-l", "-s"]
    assert "command -v" in call["stdin"] and "python$v" in call["stdin"]
    assert call["env"]["WSLENV"] == "" and call["env"]["WSL_UTF8"] == "1"


def test_a_named_distro_is_passed_to_wsl():
    fake = Fake(*machine())
    _probe(fake, wsl_distro="Ubuntu-24.04").check(_leg("ubuntu-py3.12"))
    call = next(c for c in fake.calls if "bash" in c["argv"])
    assert call["argv"][:4] == ["wsl.exe", "-d", "Ubuntu-24.04", "--exec"]


def test_a_missing_interpreter_is_named_not_guessed():
    only_314 = PY_LIST.splitlines()[0] + "\n"
    a = _probe(Fake(*machine(pylist=only_314))).check(_leg("windows-py3.11"))
    assert not a.ok
    assert a.reason == "no Python 3.11 found by `py -0p`"


def test_an_interpreter_that_is_not_the_version_it_claims_is_refused():
    rules = machine()
    rules.insert(0, (lambda a: _is_check(a) and "Python312" in a[0], (0, "3.9\n", "")))
    a = _probe(Fake(*rules)).check(_leg("windows-py3.12"))
    assert not a.ok
    assert "is Python 3.9, not 3.12" in a.reason


def test_a_wsl_that_cannot_answer_takes_every_ubuntu_leg_with_its_own_words():
    rules = machine(wsl=(1, "", "Windows Subsystem for Linux has no installed distributions."))
    probe = _probe(Fake(*rules))
    for leg in legs.select("ubuntu-*"):
        a = probe.check(leg)
        assert not a.ok and "no installed distributions" in a.reason
    assert probe.check(_leg("windows-py3.12")).ok           # canary: only WSL is down


@pytest.mark.parametrize("tool", ["timeout", "wslpath"])
def test_a_wsl_without_a_needed_tool_says_which(tool):
    rules = machine(wsl=(0, WSL_OK.replace(f"MHCI tool {tool}\n", ""), ""))
    a = _probe(Fake(*rules)).check(_leg("ubuntu-py3.12"))
    assert not a.ok and a.reason == f"WSL lacks {tool}"


def test_node_needs_node_and_browser_needs_a_chromium_that_launches():
    missing = "playwright._impl._errors.Error: Executable doesn't exist at C:\\ms-playwright"
    fake = Fake(*machine(playwright=(1, "", "Traceback (most recent call last):\n" + missing)))
    probe = _probe(fake, node=None)
    assert probe.check(_leg("node")).reason == "`node` is not on PATH"
    b = probe.check(_leg("browser"))
    assert not b.ok and "cannot launch playwright's Chromium" in b.reason
    assert missing in b.reason and "playwright install chromium" in b.reason
    probe.check(_leg("browser"))
    assert sum(_is_chromium(c["argv"]) for c in fake.calls) == 1   # asked once


PG_CI = {legs.PG_DSN_ENV: "postgresql://postgres@localhost:55432/mindhive",
         legs.PG_RO_DSN_ENV: "postgresql://mindhive_reader@localhost:55432/mindhive"}


def test_pg_runs_only_against_a_throwaway_cluster_named_twice():
    def check(**env):
        return _probe(Fake(*machine()), environ={"PATH": "x", **env}).check(_leg("pg"))
    assert not check(MINDHIVE_PG_DSN_ADMIN="postgresql://real/mirror").ok
    assert check(**PG_CI).ok
    admin_only = check(**{legs.PG_DSN_ENV: PG_CI[legs.PG_DSN_ENV]})
    assert not admin_only.ok
    assert admin_only.reason.startswith(f"no {legs.PG_RO_DSN_ENV}: ")
    same = check(**PG_CI, MINDHIVE_PG_DSN_ADMIN=PG_CI[legs.PG_DSN_ENV])
    assert not same.ok
    assert same.reason.startswith(f"{legs.PG_DSN_ENV} is the operator's own DSN")
    same_ro = check(**PG_CI, MINDHIVE_PG_DSN_RO=PG_CI[legs.PG_RO_DSN_ENV])
    assert not same_ro.ok
    assert same_ro.reason.startswith(f"{legs.PG_RO_DSN_ENV} is the operator's own DSN")


def test_without_the_launcher_the_running_interpreter_still_counts():
    rules = [(lambda a: a[:2] == ["py", "-0p"], (1, "", "py: not found")),
             (_is_check, lambda a: (0, legs.running_version() + "\n", ""))]
    probe = _probe(Fake(*rules))
    mine = f"windows-py{legs.running_version()}"
    if mine in {leg.name for leg in legs.CATALOG}:
        assert probe.check(_leg(mine)).python == "C:\\venv\\Scripts\\python.exe"
    assert all(not probe.check(leg).ok for leg in legs.select("windows-*")
               if leg.name != mine)


def test_on_linux_the_ubuntu_legs_run_natively_and_the_windows_ones_do_not():
    fake = Fake((_is_check, lambda a: (0, a[0][-4:] + "\n", "")))
    probe = legs.Probe(
        run=fake, platform="linux",
        which=lambda n, path=None: f"/usr/bin/{n}" if n.startswith("python3.1") else None,
        environ={"PATH": "/usr/bin"}, executable="/v/bin/python")
    a = probe.check(_leg("ubuntu-py3.12"))
    assert a.ok and a.via == "native" and a.python == "/usr/bin/python3.12"
    w = probe.check(_leg("windows-py3.12"))
    assert not w.ok and w.reason == "a windows leg needs a Windows host"
    assert not any("wsl.exe" in c["argv"] for c in fake.calls)


def test_a_wsl_probe_that_exits_0_without_its_sentinel_is_not_an_answer():
    rules = machine(wsl=(0, "MHCI py 3.12 /usr/bin/python3.12\nMHCI tool git\n", ""))
    a = _probe(Fake(*rules)).check(_leg("ubuntu-py3.12"))
    assert not a.ok and a.reason.startswith("WSL could not answer:")

def test_an_interpreter_that_cannot_make_a_venv_with_pip_says_why():
    rules = machine()
    rules.insert(0, (lambda a: _is_check(a) and "Python312" in a[0],
                     (1, "", "No module named ensurepip")))
    probe = _probe(Fake(*rules))
    a = probe.check(_leg("windows-py3.12"))
    assert not a.ok and a.reason.endswith("cannot make a venv with pip: No module named ensurepip")
    assert probe.check(_leg("windows-py3.11")).ok                  # canary

def test_a_wsl_without_the_legs_python_is_named_not_papered_over():
    wsl = WSL_OK.replace("MHCI py 3.12 /usr/bin/python3.12\n", "")
    probe = _probe(Fake(*machine(wsl=(0, wsl, ""))))
    a = probe.check(_leg("ubuntu-py3.12"))
    assert not a.ok and a.reason == "no python3.12 on WSL's login PATH"
    assert probe.check(_leg("ubuntu-py3.14")).ok                   # canary

def test_every_legs_pytest_args_are_its_jobs_command_verbatim():
    job, cmds = None, {}
    for line in CI_YML.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            job = m.group(1)
            continue
        m = re.search(r"run: python -m pytest((?: \S+)*)\s*$", line)
        if job and m:
            cmds[job] = tuple(m.group(1).split())
    assert len(cmds) == 4                                                # canary: every job seen
    for leg in legs.CATALOG:
        assert leg.pytest_args == cmds[leg.job], leg.name
