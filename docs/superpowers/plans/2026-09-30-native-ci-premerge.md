# Native Pre-merge CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `hive ci premerge|watch|conflicts|legs` — every `.github/workflows/ci.yml` job run locally against the merge a PR would make, with the verdict recorded in the ledger and posted to the PR as free GitHub commit statuses, and a guarded merge that lands only the tree that was tested.

**Architecture:** Eight small modules under `src/mindhive/ci/`, each with one job, all starting processes through one injectable seam (`proc.run`) so every test drives them with a fake. `legs` is the ci.yml catalog plus a probe of this machine; `preview` is `git merge-tree`; `legrun` runs one leg in a fresh single-commit repository and a fresh venv; `github` is every `gh` call; `premerge` orchestrates; `watch` polls; `premerge_cli` registers the verbs on the existing `ci` group.

**Tech Stack:** Python 3.11+ stdlib only (subprocess, tarfile, concurrent.futures, argparse), git ≥ 2.38 (`merge-tree --write-tree`), `gh`, WSL (`wsl.exe --exec bash -l -s`), pytest.

Spec: `docs/superpowers/specs/2026-09-30-native-ci-premerge-design.md`.

**Conventions for every task.**

- Worktree: `C:/Users/hanif/mindhive/.claude/worktrees/cli-login-status-detection-cfec62` (call it `WT`). Run tests with the worktree's `src` first:
  `PYTHONPATH=$WT/src HF_HUB_OFFLINE=1 C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest <files> -q -p no:cacheprovider` (from `WT`).
- A block headed **File: `<path>` (complete)** is the whole file. The helper in Task 0 writes each block into place, so a task starts from exactly the code the plan shows; fixes made during a task land in the tree, which is authoritative after that.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Nothing is pushed and no PR is opened without the owner's say-so (CLAUDE.md).

**Where this plan departs from the spec, on purpose.**

- `ci/proc.py` is an eighth module the spec's table does not list: the one door to a child process, so the authorship guard counts one spawn site and every other module stays testable through `proc.run`.
- Fetched refs go under `refs/mindhive-ci/<run tag>/`, one directory per run, deleted when the run ends -- not `refs/remotes/origin/pr/<N>`, which two concurrent runs would share and nothing would ever clean up.
- Exit 3 is added for `--merge`: green tests whose merge did not happen are neither red (1) nor a check that could not be made (2). `hive ci status` judges a pre-merge record by its verdict, so exit 3 reads green there.
- The merge guard reads the run's own result, not the sidecar: the run that just tested the head is the freshest record there is. `watch` alone reads the sidecar.
- `watch` also tests in full a PR that was red for a conflict alone once it merges cleanly (no leg ever ran on it), and retries an `error` verdict when the base moves.
- `HF_HUB_OFFLINE=1` is set for the browser leg only, the one leg that runs the operator's interpreter; the venv legs match CI, which does not set it.
- The leg repository is committed with `git add -A -f`: every exported file was tracked upstream, and a `.gitignore` in the tree must not drop one that was force-added there.

## File structure

| path | status | responsibility |
|---|---|---|
| `src/mindhive/ci/proc.py` | new | the only door to a child process: captured or logged, capped, env scrubbed |
| `src/mindhive/ci/legs.py` | new | `Leg` catalog mirroring ci.yml, `select`, `Probe` (what this machine can run) |
| `src/mindhive/ci/preview.py` | new | `merge_tree`, `merge_preview`, `conflict_matrix` |
| `src/mindhive/ci/github.py` | new | `GitHub`: pr, open_prs, slug, post_status, merge, merged |
| `src/mindhive/ci/legrun.py` | new | `leg_env`, `LegResult`, `LegRunner` (native + WSL executors), `POSIX_LEG_SCRIPT` |
| `src/mindhive/ci/premerge.py` | new | `decide`, `render`, `PremergeResult`, `Premerge` (run, record, sidecar, guarded merge) |
| `src/mindhive/ci/watch.py` | new | `Watcher` (one pass, loop) |
| `src/mindhive/ci/premerge_cli.py` | new | `premerge`, `watch`, `conflicts`, `legs` verbs |
| `src/mindhive/ci/cli.py` | modify | call `premerge_cli.register(group)`; `_print_ci` prints the verdict and a legs tally; `status` judges a pre-merge run by its verdict (`_green`) |
| `tests/test_ci_proc.py` … `tests/test_ci_premerge_cli.py` | new | one test file per module |
| `tests/test_advice_authorship.py` | modify | `ci/proc.py` joins `SPAWNERS` (agent) and `SPAWN_SITES` (2, 1) |
| `tests/test_commands_reexport.py` | modify | the CLI shape snapshot and `LEAF_HANDLERS` gain the four verbs |
| `README.md` | modify | CLI list, CI section, `MINDHIVE_CI_PG_DSN` in the env table |
| `docs/session-legend.md` | modify | a row for the pre-merge modules |
| `CHANGELOG.md` | modify | `[Unreleased]` / `### Added` |
| `CLAUDE.md` | modify | the CI of record is `hive ci premerge` while Actions is billing-blocked |
| `.github/workflows/ci.yml` | modify | header comment only: its jobs run locally through `hive ci premerge` |

---

### Task 0: the extraction helper

**Files:**
- Create: `C:/Users/hanif/AppData/Local/Temp/claude/C--Users-hanif-mindhive--claude-worktrees-cli-login-status-detection-cfec62/908a43bd-b278-46df-88a4-3997f71e3ad9/scratchpad/extract.py` (scratchpad, not the repo)

- [ ] **Step 1: write it**

```python
"""Write every **File: `path` (complete)** block of the plan into the worktree.
Usage: extract.py PLAN WORKTREE [path ...] -- only the named paths when given."""
import re
import sys
from pathlib import Path

plan, root, wanted = Path(sys.argv[1]), Path(sys.argv[2]), set(sys.argv[3:])
text = plan.read_text(encoding="utf-8")
blocks = re.findall(r"\*\*File: `([^`]+)`\*\* \(complete\)\n\n```python\n(.*?)\n```\n",
                    text, flags=re.S)
seen = set()
for rel, body in blocks:
    if wanted and rel not in wanted:
        continue
    if rel in seen:
        sys.exit(f"{rel} appears twice in the plan")
    seen.add(rel)
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {rel} ({body.count(chr(10)) + 1} lines)")
missing = wanted - seen
if missing:
    sys.exit(f"not in the plan: {sorted(missing)}")
```

- [ ] **Step 2: check it finds every block**

Run: `python extract.py <plan> <tmpdir>` into a scratch directory.
Expected: one `wrote` line per complete file in this plan, no error.

---

### Task 1: `proc` — the one door to a child process

**Files:**
- Create: `src/mindhive/ci/proc.py`
- Create: `tests/test_ci_proc.py`
- Modify: `tests/test_advice_authorship.py` (`SPAWNERS`, `SPAWN_SITES`)

- [ ] **Step 1: write the failing test**

**File: `tests/test_ci_proc.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.proc` -- the one door every pre-merge CI child goes through.

What carries the weight: a captured child answers its code and both streams; a
logged child writes into its file and nothing else; a cap returns promptly and
says so, even when the child left a grandchild running; stdin is empty unless
given; and no child -- captured or logged, inheriting or handed an environment
-- sees the SOP secret or the token.
"""
import json
import os
import sys
import time

from mindhive import advice
from mindhive.ci import proc

SECRET, TOKEN = advice.SECRET_ENV, advice.TOKEN_ENV
FAKE = "fake-sop-secret-for-this-test-only"
NAMES = ("import json, os; print(json.dumps(sorted("
         "k for k in os.environ if k.startswith('MINDHIVE_'))))")


def test_a_captured_child_answers_its_code_and_both_streams():
    rc, out, err = proc.run([sys.executable, "-c",
                             "import sys; print('out'); "
                             "print('err', file=sys.stderr); sys.exit(3)"])
    assert (rc, out.strip(), err.strip()) == (3, "out", "err")


def test_stdin_reaches_the_child_as_bytes_or_as_text():
    code = "import sys; print(sys.stdin.buffer.read().decode().upper())"
    assert proc.run([sys.executable, "-c", code], stdin=b"abc")[1].strip() == "ABC"
    assert proc.run([sys.executable, "-c", code], stdin="xyz")[1].strip() == "XYZ"


def test_without_stdin_the_child_reads_an_empty_one_not_the_console():
    rc, out, _ = proc.run([sys.executable, "-c",
                           "import sys; print(repr(sys.stdin.read()))"],
                          timeout_s=60)
    assert rc == 0 and out.strip() == "''"


def test_a_missing_program_is_a_code_and_a_reason_never_a_raise(tmp_path):
    rc, out, err = proc.run([str(tmp_path / "no-such-program")])
    assert rc == 1 and out == ""
    assert "no-such-program" in err


def test_a_captured_child_past_its_cap_answers_124_promptly():
    started = time.monotonic()
    rc, _, err = proc.run([sys.executable, "-c", "import time; time.sleep(60)"],
                          timeout_s=1)
    assert rc == proc.TIMEOUT_EXIT
    assert "1s cap" in err
    assert time.monotonic() - started < 30


def test_a_logged_child_writes_both_streams_into_its_file(tmp_path):
    log = tmp_path / "deep" / "leg.log"
    rc, out, err = proc.run([sys.executable, "-c",
                             "import sys; print('to-out'); "
                             "print('to-err', file=sys.stderr); sys.exit(1)"],
                            log=log)
    assert (rc, out, err) == (1, "", "")
    text = log.read_text(encoding="utf-8")
    assert "to-out" in text and "to-err" in text


def test_logged_steps_append_so_a_leg_keeps_one_log(tmp_path):
    log = tmp_path / "leg.log"
    proc.run([sys.executable, "-c", "print('one')"], log=log)
    proc.run([sys.executable, "-c", "print('two')"], log=log)
    assert log.read_text(encoding="utf-8").split() == ["one", "two"]


def test_a_logged_child_reads_the_stdin_it_is_given(tmp_path):
    log = tmp_path / "leg.log"
    rc, _, _ = proc.run([sys.executable, "-c",
                         "import sys; print(sys.stdin.read()[::-1])"],
                        log=log, stdin="abc")
    assert rc == 0
    assert log.read_text(encoding="utf-8").strip() == "cba"


def test_a_logged_child_past_its_cap_is_killed_with_the_grandchild_it_left(tmp_path):
    """The shape `subprocess.run` gets wrong on Windows: a grandchild still
    holding what the parent waits on. Here the output is a file and the tree
    is held, so the cap returns in seconds, not when the grandchild is done."""
    log = tmp_path / "leg.log"
    code = ("import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
            "time.sleep(120)")
    started = time.monotonic()
    rc, _, err = proc.run([sys.executable, "-c", code], log=log, timeout_s=3)
    assert rc == proc.TIMEOUT_EXIT
    assert "3s cap" in err
    assert time.monotonic() - started < 60


def test_no_child_sees_the_sop_secret_or_the_token(monkeypatch, tmp_path):
    monkeypatch.setenv(SECRET, FAKE)
    monkeypatch.setenv(TOKEN, FAKE)
    handed = dict(os.environ)
    for env in (None, handed):
        rc, out, err = proc.run([sys.executable, "-c", NAMES], env=env)
        assert rc == 0, err
        seen = json.loads(out)
        assert "MINDHIVE_HOME" in seen            # canary: the environment DID arrive
        assert SECRET not in seen and TOKEN not in seen
    log = tmp_path / "leg.log"
    assert proc.run([sys.executable, "-c", NAMES], env=handed, log=log)[0] == 0
    seen = json.loads(log.read_text(encoding="utf-8"))
    assert "MINDHIVE_HOME" in seen and SECRET not in seen and TOKEN not in seen
    assert FAKE in handed.values()                # the caller's own copy is untouched
```

- [ ] **Step 2: run it and watch it fail**

Run: `pytest tests/test_ci_proc.py -q`
Expected: collection error, `ImportError: cannot import name 'proc' from 'mindhive.ci'`.

- [ ] **Step 3: write the module**

**File: `src/mindhive/ci/proc.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/proc.py
"""The one door every pre-merge CI child goes through (spec 2026-09-30).

`legs`, `preview`, `github`, `legrun` and `premerge` start processes only by
calling `run`, so a test replaces ONE callable and sees every argv a run would
spawn -- the `FakeRunner` pattern `tests/test_ci_runner.py` set -- and the scrub
below cannot be forgotten by a caller.

Two shapes, chosen by *log*:

* ``log=None``, captured: stdout and stderr come back as text. For the short
  probes (git, gh, wsl, the interpreter checks), which print little and exit.
* ``log=<path>``, logged: the child writes into that file, stderr merged, and
  only the exit code comes back. For the long steps (venv, pip, pytest). A file
  has no reader to hold open, so a grandchild that outlives a killed pytest
  cannot wedge the wait -- the shape `subprocess.run` gets wrong on Windows,
  where the second `communicate()` after a timeout has no cap. The child heads
  its own process group and is held (`orchestrator.driver`), so the cap takes
  the whole tree, and the error text says what the kill reached.

Every child is code under test or a tool acting on it, and what it prints is
recorded, so every environment passes through
`advice.scrub_agent_env(withhold_secret=True)`: neither the SOP process token
nor the secret reaches it (the rule `ci/runner.py` set, PR #136 review P2).

Never raises: a missing executable, a timeout and a crash all come back as a
non-zero code with the reason in the error text.
"""
import subprocess
from pathlib import Path

from .. import advice as authorship

__all__ = ["TIMEOUT_EXIT", "run"]

#: What a capped child answers: the code `verify.TIMEOUT_EXIT` and `claude_cli`
#: use, and the one GNU `timeout` exits with inside a WSL leg.
TIMEOUT_EXIT = 124


def _text(data) -> str:
    if not data:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def _where(cwd):
    return None if cwd is None else str(cwd)


def run(argv, *, cwd=None, env=None, timeout_s=None, stdin=None, log=None) -> tuple:
    """``(returncode, stdout, stderr)`` for one child.

    *stdout* is ``""`` when *log* names a file, because the output went there.
    *stdin* is text or bytes fed to the child; without it the child reads an
    empty stdin, never the console's, which `wsl.exe` would sit waiting on."""
    argv = [str(a) for a in argv]
    env = authorship.scrub_agent_env(env, withhold_secret=True)
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    if log is not None:
        return _logged(argv, Path(log), cwd, env, timeout_s, data)
    feed = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
    try:
        proc = subprocess.run(argv, cwd=_where(cwd), env=env, capture_output=True,
                              timeout=timeout_s, **feed)
    except subprocess.TimeoutExpired as exc:
        return (TIMEOUT_EXIT, _text(exc.stdout),
                f"{argv[0]}: killed at the {timeout_s:g}s cap")
    except Exception as exc:                            # noqa: BLE001
        return 1, "", f"{argv[0]}: {exc}"
    return proc.returncode, _text(proc.stdout), _text(proc.stderr)


def _logged(argv, log, cwd, env, timeout_s, data) -> tuple:
    from ..orchestrator import driver          # lazy: `hive --help` pays nothing
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        out = open(log, "ab")
    except OSError as exc:
        return 1, "", f"{argv[0]}: cannot open {log}: {exc}"
    with out:
        try:
            proc = subprocess.Popen(
                argv, cwd=_where(cwd), env=env, stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                **driver.isolation_kwargs())
        except Exception as exc:                        # noqa: BLE001
            return 1, "", f"{argv[0]}: {exc}"
        driver.hold_tree(proc)
        try:
            if data is not None:
                try:
                    proc.stdin.write(data)
                    proc.stdin.close()
                except OSError:
                    pass      # the child left before reading; its code says why
            return proc.wait(timeout=timeout_s), "", ""
        except subprocess.TimeoutExpired:
            kill = driver.terminate_process_tree(proc)
            return (TIMEOUT_EXIT, "",
                    f"{argv[0]}: killed at the {timeout_s:g}s cap "
                    f"({kill.how}: {kill.reason})")
        finally:
            driver.release_tree(proc)
```

- [ ] **Step 4: register the new spawning module with the authorship guard**

In `tests/test_advice_authorship.py`, `SPAWNERS` gains, after the `"ci/runner.py": "agent",` entry and its comment:

```python
    # The pre-merge CI's one door (spec 2026-09-30): every child it starts is
    # the branch under test or a tool acting on it, and what it prints is
    # recorded, so `proc.run` scrubs token AND secret for every call.
    "ci/proc.py": "agent",
```

and `SPAWN_SITES` gains, after `"ci/runner.py": (1, 1),`:

```python
    # Two sites, one scrub (spec 2026-09-30): `subprocess.run` for the captured
    # probes and `subprocess.Popen` for the logged, tree-held long steps; the
    # single `scrub_agent_env(..., withhold_secret=True)` guards both.
    "ci/proc.py": (2, 1),
```

- [ ] **Step 5: run the tests and watch them pass**

Run: `pytest tests/test_ci_proc.py tests/test_advice_authorship.py -q`
Expected: all pass.

- [ ] **Step 6: commit**

```bash
git add src/mindhive/ci/proc.py tests/test_ci_proc.py tests/test_advice_authorship.py
git commit -m "feat(ci): proc.run, the one scrubbed, capped door for pre-merge children"
```

---

### Task 2: `legs` — the ci.yml catalog and the machine probe

**Files:**
- Create: `src/mindhive/ci/legs.py`
- Create: `tests/test_ci_legs.py`

- [ ] **Step 1: write the failing test**

**File: `tests/test_ci_legs.py`** (complete)

```python
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
          "MHCI tool tar\nMHCI tool git\nMHCI tool timeout\nMHCI tool pip\n"
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


def machine(pylist=PY_LIST, wsl=(0, WSL_OK, ""), playwright=(0, "", "")):
    return [(lambda a: a[:2] == ["py", "-0p"], (0, pylist, "")),
            (_is_check, lambda a: (0, _version_of(a[0]) + "\n", "")),
            (lambda a: "bash" in a and "-s" in a, wsl),
            (lambda a: a[1:] == ["-c", "import playwright"], playwright)]


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


def test_a_wsl_without_a_needed_tool_says_which():
    rules = machine(wsl=(0, WSL_OK.replace("MHCI tool timeout\n", ""), ""))
    a = _probe(Fake(*rules)).check(_leg("ubuntu-py3.12"))
    assert not a.ok and a.reason == "WSL lacks timeout"


def test_node_needs_node_and_browser_needs_playwright():
    probe = _probe(Fake(*machine(playwright=(1, "", "ModuleNotFoundError"))), node=None)
    assert probe.check(_leg("node")).reason == "`node` is not on PATH"
    b = probe.check(_leg("browser"))
    assert not b.ok and "cannot import playwright" in b.reason


def test_pg_runs_only_against_the_throwaway_dsn():
    real = {"PATH": "x", "MINDHIVE_PG_DSN_ADMIN": "postgresql://real/mirror"}
    assert not _probe(Fake(*machine()), environ=real).check(_leg("pg")).ok
    throwaway = {"PATH": "x", legs.PG_DSN_ENV: "postgresql://ci@localhost/throwaway"}
    assert _probe(Fake(*machine()), environ=throwaway).check(_leg("pg")).ok


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
```

- [ ] **Step 2: run it and watch it fail**

Run: `pytest tests/test_ci_legs.py -q`
Expected: `ImportError: cannot import name 'legs' from 'mindhive.ci'`.

- [ ] **Step 3: write the module**

**File: `src/mindhive/ci/legs.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/legs.py
"""The leg catalog: every `.github/workflows/ci.yml` job as something this
machine can run, and what it can run of it (spec 2026-09-30).

A LEG is one ci.yml job, or one cell of the `test` job's OS x Python matrix.
`CATALOG` is data, and `tests/test_ci_legs.py` parses ci.yml against it in both
directions, so a job added there without a leg here -- or a leg left behind
after its job is removed -- fails the suite instead of silently never running.

`Probe` answers what THIS machine can run, and why not when it cannot:

* A windows leg needs the interpreter the Python launcher registers for its
  version. `py -0p` lists every PEP 514 install, uv's `Astral/CPython3.11.16`
  included -- which `py -3.11` does NOT resolve (measured) -- so the listing is
  parsed rather than asked one version at a time. Each hit is then run once, to
  prove it is the version it claims and can make a venv with pip.
* An ubuntu leg runs in WSL on Windows and natively on Linux. WSL is asked
  through a LOGIN shell: uv puts 3.11 and 3.14 in `~/.local/bin`, which only
  the login profile adds to PATH (measured: a plain `bash -c` sees neither).
* `node` needs `node` on PATH; `browser` needs the interpreter running `hive`
  to import `playwright`; `pg` needs `MINDHIVE_CI_PG_DSN`.

A leg the machine cannot run is UNAVAILABLE with a reason, and unavailable is
never success: `premerge` names it in the verdict instead of counting it.
"""
import fnmatch
import os
import re
import shutil
import sys
from dataclasses import dataclass

from . import proc

__all__ = ["CATALOG", "HOST_LOCAL", "HOST_POSIX", "HOST_WINDOWS", "PG_DSN_ENV",
           "TIERS", "TIER_BROWSER", "TIER_DEFAULT", "TIER_NODE", "TIER_PG",
           "Availability", "Leg", "Probe", "parse_py_list", "running_version",
           "select"]

#: Where a leg runs. WINDOWS: only on a Windows machine. POSIX: in WSL on
#: Windows, natively on Linux. LOCAL: on whatever machine `hive` runs on.
HOST_WINDOWS, HOST_POSIX, HOST_LOCAL = "windows", "posix", "local"
TIER_DEFAULT, TIER_NODE, TIER_BROWSER, TIER_PG = "default", "node", "browser", "pg"
TIERS = (TIER_DEFAULT, TIER_NODE, TIER_BROWSER, TIER_PG)
#: The THROWAWAY database the pg leg migrates and tests. Never the operator's
#: `MINDHIVE_PG_DSN_ADMIN`: that is the real mirror, and a migration from an
#: unmerged branch cannot be taken back.
PG_DSN_ENV = "MINDHIVE_CI_PG_DSN"
#: A cold WSL start was measured at 7.8 s; the cap is generous on purpose.
PROBE_TIMEOUT_S = 120.0
#: What the POSIX probe prints before each fact, so a login profile's own
#: banner can never be read as an answer.
_MARK = "MHCI"
POSIX_TOOLS = ("tar", "git", "timeout")
#: Run by each candidate interpreter: its version, and proof it can build a
#: venv with pip (a Debian python without python3-venv has no `ensurepip`).
_CHECK = "import sys, venv, ensurepip; print('%d.%d' % sys.version_info[:2])"
_PY_LINE = re.compile(r"^\s*-(?:V:)?(?P<tag>\S+)\s+(?:\*\s+)?(?P<path>\S.*?)\s*$")
_VERSION = re.compile(r"(\d+\.\d+)")


@dataclass(frozen=True)
class Leg:
    """One ci.yml job (or matrix cell) and how this machine runs it."""

    name: str        # the local name, and the commit-status context suffix
    job: str         # the ci.yml job id
    ci_name: str     # the job's display name on GitHub
    runs_on: str     # the ci.yml runner label: windows-latest | ubuntu-latest
    host: str        # HOST_WINDOWS | HOST_POSIX | HOST_LOCAL
    python: str      # "3.12"; "" means the interpreter running `hive`
    tier: str        # one of TIERS
    extras: str      # the `pip install -e .[...]` extras
    note: str = ""   # how the leg departs from its ci.yml job, when it does

    @property
    def pytest_args(self) -> tuple:
        """ci.yml's test command after `python -m pytest`, verbatim."""
        return ("-q",) if self.tier == TIER_DEFAULT else ("-m", self.tier, "-q")

    @property
    def clean_venv(self) -> bool:
        """A fresh venv with the job's extras -- every leg but `browser`."""
        return bool(self.python)


def _matrix(label: str, host: str, version: str) -> Leg:
    return Leg(name=f"{label}-py{version}", job="test",
               ci_name=f"test ({label}-latest, py{version})",
               runs_on=f"{label}-latest", host=host, python=version,
               tier=TIER_DEFAULT, extras="dev")


#: Longest first: a pool fed in this order finishes about as early as any order
#: can (a Windows suite ran ~25 min here, a Linux one ~15).
CATALOG = (
    *(_matrix("windows", HOST_WINDOWS, v) for v in ("3.11", "3.12", "3.14")),
    *(_matrix("ubuntu", HOST_POSIX, v) for v in ("3.11", "3.12", "3.14")),
    Leg(name="browser", job="browser-tests", ci_name="browser tier (T12)",
        runs_on="ubuntu-latest", host=HOST_LOCAL, python="",
        tier=TIER_BROWSER, extras="dev,browser",
        note="runs on this machine with the interpreter running hive, which "
             "must import playwright, so no Chromium is downloaded; ci.yml: "
             "the engine is the same one on every leg"),
    Leg(name="node", job="node-tests", ci_name="node tier-1 (paint guard)",
        runs_on="ubuntu-latest", host=HOST_LOCAL, python="3.12",
        tier=TIER_NODE, extras="dev",
        note="runs on this machine; ci.yml: these cases have no OS or "
             "Python dimension"),
    Leg(name="pg", job="pg-tests", ci_name="pg tier (Postgres 17)",
        runs_on="ubuntu-latest", host=HOST_LOCAL, python="3.12",
        tier=TIER_PG, extras="dev,pg",
        note=f"only against the throwaway database {PG_DSN_ENV} names; never "
             f"the operator's admin DSN, never ALTER ROLE"),
)


def select(spec=None, catalog=CATALOG) -> tuple:
    """The legs *spec* names -- comma-separated names or globs, like
    ``"windows-*,node"`` -- in catalog order; every leg when *spec* is empty.
    Raises ValueError naming whatever matched nothing."""
    if not spec:
        return tuple(catalog)
    wanted = [p.strip() for p in str(spec).split(",") if p.strip()]
    unknown = [p for p in wanted
               if not any(fnmatch.fnmatchcase(leg.name, p) for leg in catalog)]
    if unknown:
        raise ValueError(f"no leg matches {', '.join(unknown)}; legs are: "
                         + ", ".join(leg.name for leg in catalog))
    return tuple(leg for leg in catalog
                 if any(fnmatch.fnmatchcase(leg.name, p) for p in wanted))


def parse_py_list(text) -> dict:
    """``{"3.12": path}`` from `py -0p`, the first install listed winning per
    version (the launcher lists its preferred one first). Total: a line that is
    not an install is skipped."""
    found = {}
    for line in str(text or "").splitlines():
        m = _PY_LINE.match(line)
        if not m:
            continue
        v = _VERSION.search(m.group("tag"))
        if v and v.group(1) not in found:
            found[v.group(1)] = m.group("path")
    return found


def running_version() -> str:
    return "%d.%d" % sys.version_info[:2]


def _probe_script(versions) -> str:
    return "\n".join([
        f"for v in {' '.join(versions)}; do",
        f'  p=$(command -v "python$v" 2>/dev/null) && echo "{_MARK} py $v $p"',
        "done",
        *(f'command -v {tool} >/dev/null 2>&1 && echo "{_MARK} tool {tool}"'
          for tool in POSIX_TOOLS),
        f'python3 -m pip --version >/dev/null 2>&1 && echo "{_MARK} tool pip"',
        f'echo "{_MARK} ok"', ""])


@dataclass(frozen=True)
class Availability:
    """Whether this machine can run *leg*, with what, and why not."""

    leg: Leg
    ok: bool
    python: str = ""   # an interpreter path (inside WSL for a `wsl` leg)
    reason: str = ""   # why not, when not ok
    via: str = ""      # "native" | "wsl"


class Probe:
    """What this machine can run of the catalog. Each question -- `py -0p`,
    each interpreter, WSL -- is asked at most once per probe."""

    def __init__(self, run=proc.run, platform=None, which=shutil.which,
                 environ=None, executable=None, wsl_distro=None):
        self._run = run
        self.platform = platform or sys.platform
        self._which = which
        self.environ = dict(os.environ if environ is None else environ)
        self.executable = executable or sys.executable
        self.wsl_distro = wsl_distro or ""
        self._native = None
        self._native_bad = {}
        self._posix = None
        self._playwright = None

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    def wsl_prefix(self) -> list:
        """The argv prefix that runs a program in WSL."""
        prefix = ["wsl.exe"]
        if self.wsl_distro:
            prefix += ["-d", self.wsl_distro]
        return prefix + ["--exec"]

    def wsl_env(self) -> dict:
        """What `wsl.exe` itself runs with: its own messages in UTF-8 rather
        than UTF-16, and nothing from this side forwarded (`WSLENV`)."""
        return {**self.environ, "WSL_UTF8": "1", "WSLENV": ""}

    # --- the answers ------------------------------------------------------------------

    def check(self, leg) -> Availability:
        need = self._needs(leg)
        if need:
            return Availability(leg, False, reason=need)
        if leg.host == HOST_WINDOWS and not self.windows:
            return Availability(leg, False, reason="a windows leg needs a Windows host")
        if leg.host == HOST_POSIX:
            if self.windows:
                return self._wsl_leg(leg)
            if not self.platform.startswith("linux"):
                return Availability(leg, False,
                                    reason="an ubuntu leg needs Linux, or WSL on Windows")
        if not leg.python:
            if not self._imports_playwright():
                return Availability(leg, False, reason=(
                    f"the interpreter running hive ({self.executable}) cannot "
                    f"import playwright: pip install -e .[browser]"))
            return Availability(leg, True, python=self.executable, via="native")
        path = self._natives().get(leg.python)
        if not path:
            return Availability(leg, False, reason=self._native_missing(leg.python))
        return Availability(leg, True, python=path, via="native")

    def _needs(self, leg) -> str:
        if leg.tier == TIER_PG and not str(self.environ.get(PG_DSN_ENV) or "").strip():
            return (f"no {PG_DSN_ENV}: the pg tier needs a throwaway "
                    f"Postgres 17 database")
        if leg.tier == TIER_NODE and not self._which("node",
                                                     path=self.environ.get("PATH")):
            return "`node` is not on PATH"
        return ""

    def _imports_playwright(self) -> bool:
        if self._playwright is None:
            rc, _, _ = self._run([self.executable, "-c", "import playwright"],
                                 timeout_s=PROBE_TIMEOUT_S)
            self._playwright = rc == 0
        return self._playwright

    # --- this machine's interpreters --------------------------------------------------

    def _natives(self) -> dict:
        if self._native is None:
            self._native = self._find_natives()
        return self._native

    def _find_natives(self) -> dict:
        versions = sorted({leg.python for leg in CATALOG if leg.python})
        if self.windows:
            rc, out, _ = self._run(["py", "-0p"], timeout_s=PROBE_TIMEOUT_S)
            found = parse_py_list(out) if rc == 0 else {}
            if not found:
                found = {running_version(): self.executable}
        else:
            found = {}
            for v in versions:
                path = self._which(f"python{v}", path=self.environ.get("PATH"))
                if path:
                    found[v] = path
        good = {}
        for v in versions:
            path = found.get(v)
            if not path:
                continue
            rc, out, err = self._run([path, "-c", _CHECK], timeout_s=PROBE_TIMEOUT_S)
            answer = (out or "").strip()
            if rc == 0 and answer == v:
                good[v] = path
            elif rc == 0:
                self._native_bad[v] = f"{path} is Python {answer}, not {v}"
            else:
                why = (err or out or f"exit {rc}").strip().splitlines()[-1:]
                self._native_bad[v] = (f"{path} cannot make a venv with pip: "
                                       f"{why[0] if why else rc}")
        return good

    def _native_missing(self, version) -> str:
        if version in self._native_bad:
            return self._native_bad[version]
        if self.windows:
            return f"no Python {version} found by `py -0p`"
        return f"no python{version} on PATH"

    # --- WSL --------------------------------------------------------------------------

    def _posix_answer(self) -> tuple:
        """``(pythons, tools, error)`` as WSL's login shell reports them."""
        if self._posix is None:
            versions = sorted({leg.python for leg in CATALOG if leg.host == HOST_POSIX})
            rc, out, err = self._run(self.wsl_prefix() + ["bash", "-l", "-s"],
                                     stdin=_probe_script(versions),
                                     env=self.wsl_env(), timeout_s=PROBE_TIMEOUT_S)
            pythons, tools, done = {}, set(), False
            for line in (out or "").splitlines():
                parts = line.strip().split(" ", 3)
                if len(parts) == 4 and parts[:2] == [_MARK, "py"]:
                    pythons[parts[2]] = parts[3]
                elif len(parts) == 3 and parts[:2] == [_MARK, "tool"]:
                    tools.add(parts[2])
                elif parts == [_MARK, "ok"]:
                    done = True
            error = "" if rc == 0 and done else (
                (err or "").strip() or (out or "").strip() or f"exit {rc}")[-300:]
            self._posix = (pythons, tools, error)
        return self._posix

    def _wsl_leg(self, leg) -> Availability:
        pythons, tools, error = self._posix_answer()
        if error:
            return Availability(leg, False, reason=f"WSL could not answer: {error}")
        missing = [tool for tool in POSIX_TOOLS if tool not in tools]
        if missing:
            return Availability(leg, False, reason="WSL lacks " + ", ".join(missing))
        path = pythons.get(leg.python)
        if not path:
            return Availability(leg, False,
                                reason=f"no python{leg.python} on WSL's login PATH")
        return Availability(leg, True, python=path, via="wsl")
```

- [ ] **Step 4: run the tests and watch them pass**

Run: `pytest tests/test_ci_legs.py -q`
Expected: all pass.

- [ ] **Step 5: commit**

```bash
git add src/mindhive/ci/legs.py tests/test_ci_legs.py
git commit -m "feat(ci): the ci.yml leg catalog, pinned to ci.yml, and the machine probe"
```

---

### Task 3: `preview` — the merge a PR would make, in the object store

**Files:**
- Create: `src/mindhive/ci/preview.py`
- Create: `tests/test_ci_preview.py`

- [ ] **Step 1: write the failing test**

**File: `tests/test_ci_preview.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.preview` -- the merge a PR would make, computed in the object store.

Real git throughout, on toy repositories under tmp_path: what is worth pinning
is what git ANSWERS -- the tree line, the exit code, the conflicted paths -- and
a fake would only restate what the module assumes. One test keeps a fake, for
the shape no repository can be made to produce on demand: exit 1 and no tree.
"""
import re
import subprocess

import pytest

from mindhive.ci import preview


def _git_version() -> tuple:
    try:
        out = subprocess.run(["git", "version"], capture_output=True, text=True).stdout
    except OSError:
        return (0, 0)
    m = re.search(r"(\d+)\.(\d+)", out or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


pytestmark = pytest.mark.skipif(_git_version() < (2, 38),
                                reason="git merge-tree --write-tree needs git >= 2.38")

IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def git(repo, *args) -> str:
    proc = subprocess.run(["git", *IDENT, *args], cwd=repo, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def commit(repo, files, message) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """root: a.txt, b.txt. master then changes b.txt. From root: `clean` adds
    c.txt; `clash` and `other` both rewrite a.txt's first line (they conflict
    with EACH OTHER, not with master); `against` rewrites b.txt (it conflicts
    with master). `ahead` is cut from the new master."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "master")
    root = commit(r, {"a.txt": "one\ntwo\n", "b.txt": "b\n"}, "root")
    heads = {}
    for name, files in (("clean", {"c.txt": "c\n"}),
                        ("clash", {"a.txt": "ONE\ntwo\n"}),
                        ("other", {"a.txt": "uno\ntwo\n"}),
                        ("against", {"b.txt": "bee\n"})):
        git(r, "checkout", "-q", "-b", name, root)
        heads[name] = commit(r, files, name)
    git(r, "checkout", "-q", "master")
    base = commit(r, {"b.txt": "b2\n"}, "master moves")
    git(r, "checkout", "-q", "-b", "ahead")
    heads["ahead"] = commit(r, {"d.txt": "d\n"}, "ahead")
    git(r, "checkout", "-q", "master")
    return {"path": r, "root": root, "base": base, **heads}


def test_a_clean_merge_is_committed_as_the_merge_github_would_make(repo):
    r = repo["path"]
    head_before, status_before = git(r, "rev-parse", "HEAD"), git(r, "status", "--porcelain")
    pv = preview.merge_preview(r, repo["base"], repo["clean"])
    assert pv.clean and pv.conflicts == () and pv.error == "" and not pv.up_to_date
    assert git(r, "rev-parse", f"{pv.tested_sha}^{{tree}}") == pv.tree
    assert git(r, "rev-list", "--parents", "-n", "1", pv.tested_sha).split()[1:] == [
        repo["base"], repo["clean"]]
    assert git(r, "log", "-1", "--format=%an <%ae>|%cn", pv.tested_sha) == (
        "mindhive-ci <mindhive-ci@localhost.invalid>|mindhive-ci")
    assert git(r, "ls-tree", "-r", "--name-only", pv.tree).split() == [
        "a.txt", "b.txt", "c.txt"]
    assert git(r, "show", f"{pv.tree}:b.txt") == "b2"       # the base's side kept
    # Nothing checked out moved: the merge happened in the object store.
    assert git(r, "rev-parse", "HEAD") == head_before
    assert git(r, "status", "--porcelain") == status_before


def test_a_head_that_already_holds_the_base_is_tested_as_itself(repo):
    pv = preview.merge_preview(repo["path"], repo["base"], repo["ahead"])
    assert pv.clean and pv.up_to_date and pv.tested_sha == repo["ahead"]
    assert pv.tree == git(repo["path"], "rev-parse", f"{repo['ahead']}^{{tree}}")


def test_a_conflict_names_its_files_and_nothing_is_committed(repo):
    pv = preview.merge_preview(repo["path"], repo["base"], repo["against"])
    assert not pv.clean and pv.conflicts == ("b.txt",)
    assert pv.tested_sha == "" and pv.error == ""


def test_a_commit_that_does_not_exist_is_an_error_never_a_conflict(repo):
    pv = preview.merge_preview(repo["path"], repo["base"], "0" * 40)
    assert not pv.clean and pv.conflicts == () and pv.error
    assert pv.error.startswith("git merge-tree: ")


def test_merge_tree_reads_the_tree_line_not_the_exit_code_alone():
    tree = "a" * 40
    seen = []

    def answering(answer):
        def run(argv, **kw):
            seen.append(list(argv))
            return answer
        return run

    assert preview.merge_tree(".", "x", "y", run=answering((0, tree + "\0", ""))) == (
        tree, (), "")
    assert seen[0] == ["git", "merge-tree", "--write-tree", "--name-only",
                       "--no-messages", "-z", "x", "y"]
    got = preview.merge_tree(".", "x", "y", run=answering(
        (1, f"{tree}\0p/q.py\0p/q.py\0r.txt\0", "")))
    assert got == (tree, ("p/q.py", "r.txt"), "")
    t, c, e = preview.merge_tree(".", "x", "y", run=answering(
        (1, "", "merge-tree: x - not something we can merge")))
    assert (t, c) == ("", ()) and "not something we can merge" in e
    t, c, e = preview.merge_tree(".", "x", "y", run=answering((128, "", "fatal: bad")))
    assert (t, c, e) == ("", (), "fatal: bad")


def test_the_conflict_matrix_names_base_conflicts_and_conflicting_pairs(repo):
    heads = {12: repo["clean"], 13: repo["clash"], 14: repo["other"],
             15: repo["against"]}
    got = preview.conflict_matrix(repo["path"], repo["base"], heads)
    assert got["errors"] == {}
    assert got["base"] == {12: [], 13: [], 14: [], 15: ["b.txt"]}
    assert got["pairs"] == [{"a": 13, "b": 14, "paths": ["a.txt"]}]


def test_the_conflict_matrix_reports_a_head_it_could_not_merge(repo):
    got = preview.conflict_matrix(repo["path"], repo["base"],
                                  {12: repo["clean"], 99: "0" * 40})
    assert got["base"] == {12: []}
    assert set(got["errors"]) == {"99"}
```

- [ ] **Step 2: run it and watch it fail**

Run: `pytest tests/test_ci_preview.py -q`
Expected: `ImportError: cannot import name 'preview' from 'mindhive.ci'`.

- [ ] **Step 3: write the module**

**File: `src/mindhive/ci/preview.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/preview.py
"""The merge a PR would make, computed without touching a checkout
(spec 2026-09-30).

`git merge-tree --write-tree` (git >= 2.38) merges two commits entirely in the
object store: no index, no working tree, no ref moved. It uses the same `ort`
strategy as GitHub's merge button, so the tree it answers is the tree a merge
commit would record -- the legs test THAT, which is what the Actions checkout
of `refs/pull/N/merge` gave CI, not the branch head alone.

Its answers are told apart by the TREE LINE, because the exit code alone cannot
do it (measured on git 2.53 and 2.43):

* exit 0 and ``<tree>\\0`` -- clean;
* exit 1 and ``<tree>\\0<path>\\0...`` -- conflicted, and the paths say where;
* exit 1 with no tree (``merge-tree: X - not something we can merge``), or any
  other exit -- an ERROR, which is never reported as a conflict.

A clean merge whose base is already inside the head needs no merge commit of
its own: GitHub's merge commit would carry the head's tree, so the head itself
is tested. Otherwise `git commit-tree` records the preview as an unreferenced
merge commit (parents base and head, identity `mindhive-ci`), so the run has
one sha to name and `git archive` one commit to export.
"""
import os
import re
from dataclasses import dataclass

from . import proc

__all__ = ["CI_IDENTITY", "Preview", "conflict_matrix", "merge_preview",
           "merge_tree"]

GIT = "git"
TIMEOUT_S = 300.0
CI_IDENTITY = {"GIT_AUTHOR_NAME": "mindhive-ci",
               "GIT_AUTHOR_EMAIL": "mindhive-ci@localhost.invalid",
               "GIT_COMMITTER_NAME": "mindhive-ci",
               "GIT_COMMITTER_EMAIL": "mindhive-ci@localhost.invalid"}
_OID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


@dataclass(frozen=True)
class Preview:
    """One merge preview. `clean` is True only when `tested_sha` names the
    commit whose tree the legs should test."""

    base_sha: str
    head_sha: str
    clean: bool = False
    conflicts: tuple = ()
    tree: str = ""
    tested_sha: str = ""
    up_to_date: bool = False
    error: str = ""


def _why(rc, out, err) -> str:
    text = (err or "").strip() or (out or "").strip()
    return text[-500:] if text else f"exit {rc}"


def merge_tree(repo_root, a, b, run=proc.run) -> tuple:
    """``(tree, conflicted_paths, error)`` for merging *b* into *a*.

    Exactly one of three shapes: ``(tree, (), "")`` clean, ``(tree, paths,
    "")`` conflicted, ``("", (), why)`` could not merge at all."""
    rc, out, err = run([GIT, "merge-tree", "--write-tree", "--name-only",
                        "--no-messages", "-z", str(a), str(b)],
                       cwd=repo_root, timeout_s=TIMEOUT_S)
    parts = (out or "").split("\0")
    tree = parts[0].strip()
    if rc in (0, 1) and _OID.match(tree):
        if rc == 0:
            return tree, (), ""
        paths = []
        for path in parts[1:]:
            if path and path not in paths:
                paths.append(path)
        # Exit 1 with a tree is a conflict even if no path came with it: a
        # conflict with no name is still not a clean merge.
        return tree, tuple(paths) or ("(unnamed conflict)",), ""
    return "", (), _why(rc, out, err)


def merge_preview(repo_root, base_sha, head_sha, run=proc.run, environ=None) -> Preview:
    """The merge of *head_sha* into *base_sha*, and the commit to test for it."""
    tree, conflicts, error = merge_tree(repo_root, base_sha, head_sha, run)
    if error:
        return Preview(base_sha, head_sha, error=f"git merge-tree: {error}")
    if conflicts:
        return Preview(base_sha, head_sha, conflicts=conflicts, tree=tree)
    rc, out, err = run([GIT, "merge-base", "--is-ancestor", base_sha, head_sha],
                       cwd=repo_root, timeout_s=TIMEOUT_S)
    if rc == 0:
        return Preview(base_sha, head_sha, clean=True, tree=tree,
                       tested_sha=head_sha, up_to_date=True)
    if rc != 1:
        return Preview(base_sha, head_sha, tree=tree,
                       error=f"git merge-base --is-ancestor: {_why(rc, out, err)}")
    env = {**(os.environ if environ is None else environ), **CI_IDENTITY}
    message = f"mindhive-ci merge preview of {head_sha[:12]} into {base_sha[:12]}"
    rc, out, err = run([GIT, "commit-tree", "--no-gpg-sign", "-p", base_sha,
                        "-p", head_sha, "-m", message, tree],
                       cwd=repo_root, env=env, timeout_s=TIMEOUT_S)
    sha = (out or "").strip()
    if rc != 0 or not _OID.match(sha):
        return Preview(base_sha, head_sha, tree=tree,
                       error=f"git commit-tree: {_why(rc, out, err)}")
    return Preview(base_sha, head_sha, clean=True, tree=tree, tested_sha=sha)


def conflict_matrix(repo_root, base_sha, heads, run=proc.run) -> dict:
    """Which open PRs conflict with the base, and with each other.

    *heads* maps a PR number to its head sha. The answer is
    ``{"base": {pr: [paths]}, "pairs": [{"a", "b", "paths"}], "errors": {}}``.
    A pair is merged head against head, from their own merge base: a pair that
    conflicts cannot both land as they stand, in either order."""
    out = {"base": {}, "pairs": [], "errors": {}}
    order = sorted(heads)
    for pr in order:
        _, paths, error = merge_tree(repo_root, base_sha, heads[pr], run)
        if error:
            out["errors"][str(pr)] = error
        else:
            out["base"][pr] = list(paths)
    ok = [pr for pr in order if pr in out["base"]]
    for i, a in enumerate(ok):
        for b in ok[i + 1:]:
            _, paths, error = merge_tree(repo_root, heads[a], heads[b], run)
            if error:
                out["errors"][f"{a}+{b}"] = error
            elif paths:
                out["pairs"].append({"a": a, "b": b, "paths": list(paths)})
    return out
```

- [ ] **Step 4: run the tests and watch them pass**

Run: `pytest tests/test_ci_preview.py -q`
Expected: all pass.

- [ ] **Step 5: commit**

```bash
git add src/mindhive/ci/preview.py tests/test_ci_preview.py
git commit -m "feat(ci): merge preview and conflict matrix, in the object store"
```

---

### Task 4: `github` — every `gh` call in one place

**Files:**
- Create: `src/mindhive/ci/github.py`
- Create: `tests/test_ci_github.py`

- [ ] **Step 1: write the failing test**

**File: `tests/test_ci_github.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.github` -- every `gh` call the pre-merge CI makes.

The merge is the one irreversible call, so its argv is pinned whole: a merge
commit pinned to the tested head, and not one flag more. Statuses never raise:
a PR that could not be annotated still has a verdict.
"""
import json

import pytest

from mindhive.ci import github

HEAD = "3" * 40
PR = {"number": 151, "state": "OPEN", "isDraft": False,
      "headRefName": "claude/x", "headRefOid": HEAD, "baseRefName": "master",
      "title": "t", "url": "https://example.invalid/pull/151"}
SLUG = ("gh", "repo", "view")


class Fake:
    """Answers `gh` by argv prefix; records every call and where it ran."""

    def __init__(self, *rules):
        self.rules = list(rules)
        self.calls = []

    def __call__(self, argv, cwd=None, env=None, timeout_s=None, stdin=None, log=None):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, "cwd": cwd})
        for prefix, answer in self.rules:
            if tuple(argv[:len(prefix)]) == prefix:
                return answer
        return 1, "", f"not scripted: {argv[:3]}"


def _fields(argv) -> dict:
    return dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-f")


def test_the_merge_is_a_merge_commit_pinned_to_the_tested_head_and_nothing_more():
    fake = Fake((("gh", "pr", "merge"), (0, "Merged", "")))
    assert github.GitHub("R", run=fake).merge(151, HEAD) == (0, "Merged")
    assert [c["argv"] for c in fake.calls] == [
        ["gh", "pr", "merge", "151", "--merge", "--match-head-commit", HEAD]]


def test_a_status_goes_to_the_statuses_api_clipped_to_what_github_takes():
    fake = Fake((SLUG, (0, "owner/repo\n", "")), (("gh", "api"), (0, "{}", "")))
    gh = github.GitHub("R", run=fake)
    assert gh.post_status("a" * 40, "success", "mindhive-ci/windows-py3.12",
                          "x" * 300) == ""
    api = fake.calls[-1]["argv"]
    assert api[:5] == ["gh", "api", "-X", "POST",
                       "repos/owner/repo/statuses/" + "a" * 40]
    fields = _fields(api)
    assert fields["state"] == "success"
    assert fields["context"] == "mindhive-ci/windows-py3.12"
    assert len(fields["description"]) == github.DESCRIPTION_MAX
    assert fields["description"].endswith("...")


def test_the_repository_slug_is_asked_once():
    fake = Fake((SLUG, (0, "owner/repo", "")), (("gh", "api"), (0, "{}", "")))
    gh = github.GitHub("R", run=fake)
    gh.post_status("a" * 40, "pending", "mindhive-ci", "one")
    gh.post_status("a" * 40, "success", "mindhive-ci", "two")
    assert sum(c["argv"][:3] == list(SLUG) for c in fake.calls) == 1


def test_a_status_that_fails_is_a_reason_never_a_raise():
    fake = Fake((SLUG, (0, "owner/repo", "")),
                (("gh", "api"), (1, "", "HTTP 403: Resource not accessible")))
    assert "403" in github.GitHub("R", run=fake).post_status(
        "a" * 40, "failure", "mindhive-ci", "red")
    fake = Fake((SLUG, (1, "", "gh: To get started with GitHub CLI, please run: gh auth login")))
    assert "gh auth login" in github.GitHub("R", run=fake).post_status(
        "a" * 40, "pending", "mindhive-ci", "")


def test_an_unknown_state_is_refused_before_anything_is_asked():
    fake = Fake()
    assert "not a commit status state" in github.GitHub("R", run=fake).post_status(
        "a" * 40, "neutral", "mindhive-ci", "")
    assert fake.calls == []


def test_pr_reads_its_fields_and_open_prs_come_back_in_number_order():
    fake = Fake((("gh", "pr", "view"), (0, json.dumps(PR), "")),
                (("gh", "pr", "list"),
                 (0, json.dumps([{**PR, "number": 9}, {**PR, "number": 3}]), "")))
    gh = github.GitHub("R", run=fake)
    assert gh.pr(151)["headRefOid"] == HEAD
    assert fake.calls[0]["argv"] == ["gh", "pr", "view", "151", "--json",
                                     github.PR_FIELDS]
    assert [p["number"] for p in gh.open_prs()] == [3, 9]
    assert fake.calls[1]["argv"] == ["gh", "pr", "list", "--state", "open",
                                     "--limit", "100", "--json", github.PR_FIELDS]


def test_a_read_that_fails_raises_with_ghs_own_words():
    fake = Fake((("gh", "pr", "view"),
                 (1, "", "GraphQL: Could not resolve to a PullRequest with the number of 999.")))
    with pytest.raises(github.GitHubError, match="Could not resolve"):
        github.GitHub("R", run=fake).pr(999)
    fake = Fake((("gh", "pr", "view"), (0, "not json", "")))
    with pytest.raises(github.GitHubError, match="not JSON"):
        github.GitHub("R", run=fake).pr(1)


def test_merged_reads_the_state_and_the_merge_commit():
    fake = Fake((("gh", "pr", "view"),
                 (0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": "m" * 40}}), "")))
    assert github.GitHub("R", run=fake).merged(151) == ("MERGED", "m" * 40)
    assert fake.calls[0]["argv"] == ["gh", "pr", "view", "151", "--json",
                                     "state,mergeCommit"]
    fake = Fake((("gh", "pr", "view"),
                 (0, json.dumps({"state": "OPEN", "mergeCommit": None}), "")))
    assert github.GitHub("R", run=fake).merged(151) == ("OPEN", "")


def test_ready_says_why_not():
    assert github.GitHub("R", run=Fake((("gh", "auth", "status"),
                                        (0, "", "Logged in")))).ready() == ""
    why = github.GitHub("R", run=Fake((("gh", "auth", "status"),
                                       (1, "", "You are not logged into any GitHub hosts.")))).ready()
    assert "not logged into" in why


def test_every_call_runs_in_the_repository():
    fake = Fake((SLUG, (0, "owner/repo", "")), (("gh", "api"), (0, "{}", "")),
                (("gh", "pr", "view"), (0, json.dumps(PR), "")))
    gh = github.GitHub("C:/repo", run=fake)
    gh.pr(1)
    gh.post_status("a" * 40, "pending", "mindhive-ci", "")
    assert {c["cwd"] for c in fake.calls} == {"C:/repo"}
```

- [ ] **Step 2: run it and watch it fail**

Run: `pytest tests/test_ci_github.py -q`
Expected: `ImportError: cannot import name 'github' from 'mindhive.ci'`.

- [ ] **Step 3: write the module**

**File: `src/mindhive/ci/github.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/github.py
"""Every `gh` call the pre-merge CI makes, in one place (spec 2026-09-30).

Commit statuses (`POST repos/{owner}/{repo}/statuses/{sha}`) are a plain REST
write: no Actions minutes, no runner, nothing billed -- which is the point
while the account's Actions jobs are refused for payment. They show on the PR
where the Actions checks used to, as `mindhive-ci` and `mindhive-ci/<leg>`.

The merge is the one irreversible call, so its argv is fixed here and nowhere
else: ``gh pr merge N --merge --match-head-commit <sha>``. A merge commit,
which is this repository's convention; the head pinned, so GitHub itself
refuses the merge when the head moved after it was tested; nothing that
rewrites the branch, deletes it, bypasses a rule or merges later.

Reads raise `GitHubError` in gh's own words. `post_status` never raises: a PR
that could not be annotated still has a verdict, and the caller records why.
"""
import json

from . import proc

__all__ = ["CONTEXT", "DESCRIPTION_MAX", "GH", "GitHub", "GitHubError",
           "PR_FIELDS", "STATES", "clip"]

GH = "gh"
#: The overall context; each leg posts `mindhive-ci/<leg>`.
CONTEXT = "mindhive-ci"
STATES = ("pending", "success", "failure", "error")
#: GitHub refuses a status description past 140 characters.
DESCRIPTION_MAX = 140
PR_FIELDS = "number,state,isDraft,headRefName,headRefOid,baseRefName,title,url"
TIMEOUT_S = 120.0
MERGE_TIMEOUT_S = 300.0


class GitHubError(RuntimeError):
    """A `gh` read that had to succeed did not."""


def clip(text, limit=DESCRIPTION_MAX) -> str:
    """*text* on one line, at most *limit* characters, ASCII ellipsis."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _json(out, what):
    try:
        return json.loads(out or "")
    except ValueError:
        raise GitHubError(f"{what}: not JSON: {clip(out, 200)}") from None


class GitHub:
    """`gh`, run in *repo_root* through one injectable *run*."""

    def __init__(self, repo_root, run=proc.run):
        self.repo_root = repo_root
        self._run = run
        self._slug = None

    def _gh(self, args, timeout_s=TIMEOUT_S) -> tuple:
        return self._run([GH, *args], cwd=self.repo_root, timeout_s=timeout_s)

    def _must(self, args, timeout_s=TIMEOUT_S) -> str:
        rc, out, err = self._gh(args, timeout_s)
        if rc != 0:
            why = (err or "").strip() or (out or "").strip() or f"exit {rc}"
            raise GitHubError(f"gh {' '.join(args[:2])}: {clip(why, 500)}")
        return out or ""

    def ready(self) -> str:
        """``""`` when `gh` is installed and logged in, else why not."""
        rc, out, err = self._gh(["auth", "status"])
        return "" if rc == 0 else clip((err or "").strip() or (out or "").strip()
                                       or f"gh auth status: exit {rc}", 300)

    def slug(self) -> str:
        """``owner/name`` of the repository, asked once."""
        if self._slug is None:
            slug = self._must(["repo", "view", "--json", "nameWithOwner",
                               "-q", ".nameWithOwner"]).strip()
            if slug.count("/") != 1:
                raise GitHubError(f"gh repo view: no owner/name in {slug!r}")
            self._slug = slug
        return self._slug

    def pr(self, number) -> dict:
        got = _json(self._must(["pr", "view", str(int(number)), "--json", PR_FIELDS]),
                    f"gh pr view {number}")
        if not isinstance(got, dict):
            raise GitHubError(f"gh pr view {number}: not an object")
        return got

    def open_prs(self) -> list:
        got = _json(self._must(["pr", "list", "--state", "open", "--limit", "100",
                                "--json", PR_FIELDS]), "gh pr list")
        if not isinstance(got, list):
            raise GitHubError("gh pr list: not a list")
        return sorted((p for p in got if isinstance(p, dict)),
                      key=lambda p: int(p.get("number") or 0))

    def post_status(self, sha, state, context=CONTEXT, description="") -> str:
        """Post one commit status. ``""`` when it landed, else why not."""
        if state not in STATES:
            return f"not a commit status state: {state!r}"
        try:
            slug = self.slug()
        except GitHubError as exc:
            return str(exc)
        rc, out, err = self._gh(["api", "-X", "POST", f"repos/{slug}/statuses/{sha}",
                                 "-f", f"state={state}", "-f", f"context={context}",
                                 "-f", f"description={clip(description)}"])
        if rc == 0:
            return ""
        why = (err or "").strip() or (out or "").strip() or f"exit {rc}"
        return f"status {context}={state} on {str(sha)[:12]}: {clip(why, 300)}"

    def merge(self, number, head_sha) -> tuple:
        """``(exit_code, gh's words)``. Never judged by the code alone: the
        caller reads the PR back with `merged`."""
        rc, out, err = self._gh(["pr", "merge", str(int(number)), "--merge",
                                 "--match-head-commit", str(head_sha)],
                                timeout_s=MERGE_TIMEOUT_S)
        return rc, ((out or "") + (err or "")).strip()

    def merged(self, number) -> tuple:
        """``(state, merge commit sha or "")`` as GitHub has it now."""
        got = _json(self._must(["pr", "view", str(int(number)), "--json",
                                "state,mergeCommit"]), f"gh pr view {number}")
        commit = got.get("mergeCommit") if isinstance(got, dict) else None
        state = str(got.get("state") or "") if isinstance(got, dict) else ""
        oid = str(commit.get("oid") or "") if isinstance(commit, dict) else ""
        return state, oid
```

- [ ] **Step 4: run the tests and watch them pass**

Run: `pytest tests/test_ci_github.py -q`
Expected: all pass.

- [ ] **Step 5: commit**

```bash
git add src/mindhive/ci/github.py tests/test_ci_github.py
git commit -m "feat(ci): gh calls for pre-merge: statuses, PR reads, the pinned merge"
```

---

### Task 5: `legrun` — one leg, run the way its ci.yml job runs it

**Files:**
- Create: `src/mindhive/ci/legrun.py`
- Create: `tests/test_ci_legrun.py`

- [ ] **Step 1: write the failing test**

**File: `tests/test_ci_legrun.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.legrun` -- one leg, run the way its ci.yml job runs it.

Most tests drive `LegRunner` through a fake `run` that answers each child the
way the real one would, and writes into the leg's log what the real child
would have printed -- so what is asserted is the order of the steps, their
argv, where they ran and what environment they got. Two tests use the real
thing: a toy repository run end to end through the host interpreter (export,
extract, fresh repository, pytest), and the WSL leg script under a real bash,
stopping at the first step it cannot do.
"""
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from mindhive.ci import legrun, legs

COMMIT = "c" * 40
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


def make_tar(path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tar:
        for name, text in (("pyproject.toml", "[project]\nname = 'toy'\n"),
                           ("src/toy/__init__.py", "")):
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _is_pytest(argv) -> bool:
    return "-m" in argv and argv[argv.index("-m") + 1:argv.index("-m") + 2] == ["pytest"]


class FakeRun:
    """Answers a leg's children; writes what they would print into the log."""

    def __init__(self, pytest=(0, "...\n3 passed in 0.10s\n"), fail=None,
                 script=("", 0), raises=None):
        self.pytest = pytest
        self.fail = fail
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
            say("ERROR: No matching distribution found for pytest>=8\n")
            return 1, "", ""
        if "archive" in argv:
            make_tar(argv[argv.index("-o") + 1])
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


def test_a_windows_leg_runs_its_jobs_steps_in_a_fresh_repo_and_venv(tmp_path):
    fake = FakeRun()
    the_leg = leg("windows-py3.12")
    res = runner(tmp_path, fake).run(the_leg, avail(the_leg), COMMIT)
    assert (res.state, res.step, res.exit_code, res.counts) == (
        "success", "pytest", 0, {"passed": 3})
    work = tmp_path / "work" / "windows-py3.12"
    venv, repo = work / "venv", work / "repo"
    py = str(venv / "Scripts" / "python.exe")
    archive = fake.call(lambda a: "archive" in a)
    assert archive["argv"][:4] == ["git", "-c", "core.autocrlf=true", "archive"]
    assert archive["argv"][-1] == COMMIT and archive["cwd"] == str(tmp_path / "origin")
    order = [fake.index(lambda a: "archive" in a),
             fake.index(lambda a: a[-2:] == ["init", "-q"]),
             fake.index(lambda a: a[1:] == ["config", "core.autocrlf", "true"]),
             fake.index(lambda a: a[1:] == ["add", "-A", "-f"]),
             fake.index(lambda a: "commit" in a),
             fake.index(lambda a: a == ["C:/py312/python.exe", "-m", "venv", str(venv)]),
             fake.index(lambda a: a == [py, "-m", "pip", "install", "--upgrade", "pip"]),
             fake.index(lambda a: a == [py, "-m", "pip", "install", "-e", ".[dev]"]),
             fake.index(lambda a: a == [py, "-m", "pytest", "-q"])]
    assert order == sorted(order)
    committed = fake.call(lambda a: "commit" in a)["argv"]
    assert "user.name=mindhive-ci" in committed and "--no-verify" in committed
    for c in fake.calls:
        if c["argv"][:2] == [py, "-m"] or c["argv"][0] == "git" and "archive" not in c["argv"]:
            assert c["cwd"] == str(repo), c["argv"]
    ran = fake.call(lambda a: a == [py, "-m", "pytest", "-q"])
    assert ran["env"]["PATH"].split(os.pathsep)[0] == str(venv / "Scripts")
    assert ran["env"]["MINDHIVE_HOME"] == str(work / "home")
    assert [name for name in GONE if name in ran["env"]] == []
    assert res.argv == (py, "-m", "pytest", "-q")
    assert not work.exists()                                  # removed after the run
    assert "3 passed" in Path(res.artifact).read_text(encoding="utf-8")


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


def test_the_pg_leg_migrates_the_throwaway_database_and_never_the_real_one(tmp_path):
    fake = FakeRun()
    environ = {**ENV, legs.PG_DSN_ENV: "postgresql://ci@localhost/throwaway"}
    the_leg = leg("pg")
    res = runner(tmp_path, fake, environ=environ).run(the_leg, avail(the_leg), COMMIT)
    assert res.state == "success"
    py = str(tmp_path / "work" / "pg" / "venv" / "Scripts" / "python.exe")
    assert fake.call(lambda a: a[:5] == [py, "-m", "pip", "install", "-e"])["argv"][-1] == (
        ".[dev,pg]")
    migrate = fake.call(lambda a: a == [py, "-m", "mindhive", "pg", "migrate"])
    assert migrate["env"]["MINDHIVE_PG_DSN_ADMIN"] == "postgresql://ci@localhost/throwaway"
    ran = fake.call(_is_pytest)
    assert ran["argv"] == [py, "-m", "pytest", "-m", "pg", "-q"]
    assert ran["env"]["MINDHIVE_PG_DSN_ADMIN"] == "postgresql://ci@localhost/throwaway"
    assert not any(REAL_DSN in (c["env"] or {}).values() for c in fake.calls)
    assert fake.index(lambda a: a[-2:] == ["pg", "migrate"]) < fake.index(_is_pytest)


def test_an_ubuntu_leg_on_linux_keeps_lf_and_uses_the_venvs_bin(tmp_path):
    fake = FakeRun()
    the_leg = leg("ubuntu-py3.12")
    res = runner(tmp_path, fake, windows=False).run(
        the_leg, avail(the_leg, python="/usr/bin/python3.12"), COMMIT)
    assert res.state == "success"
    assert fake.call(lambda a: "archive" in a)["argv"][:3] == [
        "git", "-c", "core.autocrlf=false"]
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

WSL_OK = ("::mindhive-ci:: step git-init\n::mindhive-ci:: step venv: ...\n"
          f"::mindhive-ci:: step pytest: {WSL_ROOT}-venv/bin/python -m pytest -q\n"
          "....\n5 passed in 1.00s\n::mindhive-ci:: pytest-exit 0\n")


def test_a_wsl_leg_ships_the_tree_on_stdin_and_runs_the_script_in_a_login_shell(tmp_path):
    fake = FakeRun(script=(WSL_OK, 0))
    the_leg = leg("ubuntu-py3.14")
    py = "/home/u/.local/bin/python3.14"
    res = runner(tmp_path, fake).run(the_leg, avail(the_leg, python=py, via="wsl"), COMMIT)
    assert (res.state, res.step, res.exit_code, res.counts) == (
        "success", "pytest", 0, {"passed": 5})
    assert fake.call(lambda a: "archive" in a)["argv"][:3] == [
        "git", "-c", "core.autocrlf=false"]
    extract = fake.call(lambda a: a[:3] == ["wsl.exe", "--exec", "tar"])
    assert extract["argv"] == ["wsl.exe", "--exec", "tar", "-x", "-C", WSL_ROOT]
    assert isinstance(extract["stdin"], bytes) and extract["stdin"][257:262] == b"ustar"
    script = fake.call(lambda a: "bash" in a)
    assert script["argv"] == ["wsl.exe", "--exec", "bash", "-l", "-s", "--", WSL_ROOT,
                              py, "dev", "5", "-q"]
    assert script["stdin"] == legrun.POSIX_LEG_SCRIPT
    assert script["env"] == {"WSLENV": "", "WSL_UTF8": "1"}
    assert fake.calls[-1]["argv"] == ["wsl.exe", "--exec", "rm", "-rf", "--", WSL_ROOT,
                                      WSL_ROOT + "-venv", WSL_ROOT + "-home"]
    assert res.argv == (py, "-m", "pytest", "-q")


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
    proc = subprocess.run([_bash(), "-s", "--", str(missing), "python3.99", "dev",
                           "60", "-q"],
                          input=legrun.POSIX_LEG_SCRIPT.encode("utf-8"),
                          capture_output=True, timeout=60)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == legrun.SETUP_FAILED_EXIT, (out, err)
    assert out.splitlines()[-1] == "::mindhive-ci:: setup-failed cd"
    assert "no such dir" in err            # the argument with spaces arrived whole


TOY_TEST = '''\
import os
import subprocess


def test_the_leg_is_a_fresh_repository_with_one_commit():
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "1"


def test_the_leg_environment_was_built_not_inherited():
    assert "MINDHIVE_PROJECT" not in os.environ
    assert "PYTEST_ADDOPTS" not in os.environ
    assert os.path.basename(os.environ["MINDHIVE_HOME"]) == "home"
'''


def _git(repo, *args) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.parametrize("runs_on,data", [("windows-latest", b"one\r\ntwo\r\n"),
                                          ("ubuntu-latest", b"one\ntwo\n")])
def test_a_real_leg_exports_commits_and_runs_the_suite_in_a_fresh_repo(
        tmp_path, runs_on, data):
    origin = tmp_path / "origin"
    (origin / "tests").mkdir(parents=True)
    for rel, text in (("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'),
                      ("data.txt", "one\ntwo\n"), ("tests/test_toy.py", TOY_TEST)):
        (origin / rel).write_text(text, encoding="utf-8", newline="\n")
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
    _git(origin, "init", "-q", "-b", "master")
    _git(origin, *ident, "add", "-A")
    _git(origin, *ident, "commit", "-q", "-m", "toy")
    commit = _git(origin, "rev-parse", "HEAD")
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
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "log", "-1", "--format=%an") == "mindhive-ci"
```

- [ ] **Step 2: run it and watch it fail**

Run: `pytest tests/test_ci_legrun.py -q`
Expected: `ImportError: cannot import name 'legrun' from 'mindhive.ci'`.

- [ ] **Step 3: write the module**

**File: `src/mindhive/ci/legrun.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/legrun.py
"""One leg, run the way its ci.yml job runs it (spec 2026-09-30).

**A fresh repository per leg**, not a linked worktree: `git archive` of the
tested commit, extracted, then `git init` + `add` + ONE commit under the
`mindhive-ci` identity. That is what an Actions checkout is -- one commit, no
history, no hooks, no other branch -- and it shares nothing with the
operator's repository, so a test that runs `git` in its root sees the tree
under test and nothing else.

**Line endings follow the runner.** A windows job's checkout is CRLF (git for
Windows ships `core.autocrlf=true`, as the runner image does) and an ubuntu
job's is LF. `git archive` honours `core.autocrlf` (measured), so the
conversion happens on export, and the leg repository keeps the same setting,
so `git status` in it is clean.

**Then the job's own steps**: a venv from the leg's interpreter, `pip install
--upgrade pip`, `pip install -e .[extras]`, the pg tier's `pg migrate`, and
the job's pytest command verbatim. The `browser` leg is the exception the
catalog documents: it runs the interpreter running `hive` -- which must
already import playwright -- with `PYTHONPATH` at the leg's `src`, so no
Chromium is downloaded per run.

**The environment is built, not inherited.** `leg_env` drops what would make
a leg test something other than what CI tests -- interpreter and pytest
overrides, every `MINDHIVE_*`, git's own variables -- and anything shaped like
a credential, then sets `MINDHIVE_HOME` inside the leg and puts the venv first
on PATH. A WSL leg does the same inside Linux (`POSIX_LEG_SCRIPT`), where
nothing from Windows arrives at all (`WSLENV=""`).

**Classification is pytest's own**: exit 0 success, 1 failure, anything else
error (2 interrupted, 3 internal error, 4 usage, 5 nothing collected, 124 the
cap). A step before pytest that fails makes the leg an ERROR at that step,
never a failure: the suite said nothing.

Every step appends to one log, kept as a runlog artifact; the leg's
directory is removed afterwards unless `keep`. `LegRunner.run` never raises.
"""
import os
import re
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import runlog, verify
from . import legs as legs_mod
from . import proc
from .runner import _rmtree_force

__all__ = ["FAILED_CAP", "LEG_TIMEOUT_S", "LegResult", "LegRunner", "MARK",
           "POSIX_LEG_SCRIPT", "SETUP_FAILED_EXIT", "STATES", "autocrlf_for",
           "classify", "extract", "leg_env", "venv_python"]

GIT = "git"
#: Every line the runner -- or the WSL script -- writes into a leg's log starts
#: with this, so a step can be found in the log and told apart from output.
MARK = "::mindhive-ci::"
STATES = ("success", "failure", "error")
#: pytest's cap per leg: ninety minutes, about four times the slowest suite
#: measured here (a Windows full suite ran 1403 s beside a second one).
LEG_TIMEOUT_S = 5400.0
#: One setup step: a venv, a pip install, a migration.
SETUP_TIMEOUT_S = 1800.0
#: Git plumbing and WSL housekeeping.
STEP_TIMEOUT_S = 600.0
SETUP_FAILED_EXIT = 97
FAILED_CAP = 20
CI_NAME = "mindhive-ci"
CI_EMAIL = "mindhive-ci@localhost.invalid"
COMMIT_MESSAGE = "mindhive-ci leg tree"
_DROP_PREFIXES = ("PYTHON", "PYTEST_", "MINDHIVE_", "MINDNETWORK_", "CLAUDE",
                  "ANTHROPIC_", "GIT_", "VIRTUAL_ENV", "CONDA_", "UV_")
_DROP_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY",
               "ACCESS_KEY", "PRIVATE_KEY", "CREDENTIAL")
_PYTEST_STEP = f"{MARK} step pytest"
_SETUP_FAILED = re.compile(r"^::mindhive-ci:: setup-failed (\S+)[ \t]*\r?$", re.M)
_PYTEST_EXIT = re.compile(r"^::mindhive-ci:: pytest-exit (-?\d+)[ \t]*\r?$", re.M)
_COUNT_ORDER = ("failed", "errors", "passed", "skipped", "xfailed", "xpassed",
                "deselected")

#: A WSL leg's whole job, fed to `bash -l -s -- ROOT PY EXTRAS CAP <pytest args>`
#: on stdin. A LOGIN shell, because uv's interpreters live in `~/.local/bin`,
#: which only the login profile puts on PATH (measured). The body is one
#: function called with `</dev/null`: bash reads a stdin script as it runs it,
#: so without that a step that reads stdin -- pip, a test's child -- would eat
#: the rest of the script. Windows paths (`/mnt/...`) come off PATH first, so a
#: Windows `python`, `git` or `pytest` can never answer for Linux.
POSIX_LEG_SCRIPT = r"""
mhci_mark() { printf '%s %s\n' '::mindhive-ci::' "$*"; }
mhci_fail() { mhci_mark "setup-failed $1"; exit 97; }
mhci_leg() {
    ROOT=$1 PY=$2 EXTRAS=$3 CAP=$4
    shift 4
    cd "$ROOT" || mhci_fail cd
    IFS=: read -r -a mhci_dirs <<< "$PATH"
    PATH=
    for d in "${mhci_dirs[@]}"; do
        case "$d" in /mnt/*) ;; *) PATH="${PATH:+$PATH:}$d" ;; esac
    done
    export PATH
    for name in $(compgen -e); do
        case "$name" in
            PYTHON*|PYTEST_*|MINDHIVE_*|MINDNETWORK_*|GIT_*|CLAUDE*|ANTHROPIC_*|VIRTUAL_ENV|CONDA_*|UV_*|*TOKEN*|*SECRET*|*PASSWORD*|*PASSWD*|*API_KEY*|*APIKEY*|*ACCESS_KEY*|*PRIVATE_KEY*|*CREDENTIAL*)
                unset "$name" ;;
        esac
    done
    export MINDHIVE_HOME="$ROOT-home" PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1
    mkdir -p "$MINDHIVE_HOME" || mhci_fail home
    mhci_mark "step git-init"
    git -c init.defaultBranch=master init -q \
        && git config core.autocrlf false \
        && git config core.safecrlf false \
        && git add -A -f \
        && git -c user.name=mindhive-ci -c user.email=mindhive-ci@localhost.invalid \
               -c commit.gpgsign=false -c core.hooksPath=/dev/null \
               commit -q --no-verify -m "mindhive-ci leg tree" \
        || mhci_fail git-init
    VENV="$ROOT-venv"
    mhci_mark "step venv: $PY -m venv $VENV"
    if ! "$PY" -m venv "$VENV"; then
        rm -rf "$VENV"
        { "$PY" -m venv --without-pip "$VENV" \
            && python3 -m pip --python "$VENV/bin/python" install -q pip; } \
            || mhci_fail venv
    fi
    export PATH="$VENV/bin:$PATH"
    mhci_mark "step pip-upgrade"
    "$VENV/bin/python" -m pip install --upgrade pip || mhci_fail pip-upgrade
    mhci_mark "step install"
    "$VENV/bin/python" -m pip install -e ".[$EXTRAS]" || mhci_fail install
    mhci_mark "step pytest: $VENV/bin/python -m pytest $*"
    timeout --kill-after=30 "$CAP" "$VENV/bin/python" -m pytest "$@"
    rc=$?
    mhci_mark "pytest-exit $rc"
    exit "$rc"
}
mhci_leg "$@" </dev/null
"""


def classify(rc) -> str:
    """pytest's exit code as a leg state."""
    return "success" if rc == 0 else "failure" if rc == 1 else "error"


def autocrlf_for(leg) -> str:
    """`core.autocrlf` for *leg*'s checkout: the runner's, not this machine's."""
    return "true" if str(leg.runs_on).startswith("windows") else "false"


def venv_python(venv, windows=None) -> Path:
    windows = (os.name == "nt") if windows is None else bool(windows)
    return Path(venv) / ("Scripts/python.exe" if windows else "bin/python")


def leg_env(environ, leg_dir, bin_dir=None) -> dict:
    """The environment a leg's children get: built from *environ*, never
    inherited whole. *environ* itself is not changed."""
    env = {}
    for name, value in dict(environ).items():
        upper = str(name).upper()
        if upper.startswith(_DROP_PREFIXES) or any(w in upper for w in _DROP_WORDS):
            continue
        env[name] = value
    env["MINDHIVE_HOME"] = str(Path(leg_dir) / "home")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    if bin_dir is not None:
        key = next((k for k in env if str(k).upper() == "PATH"), "PATH")
        prior = env.get(key, "")
        env[key] = str(bin_dir) + (os.pathsep + prior if prior else "")
    return env


def extract(tar_path, dest) -> None:
    """Unpack a `git archive` tar into *dest*, refusing any member that would
    land outside it -- the stdlib `data` filter where the interpreter has it."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as archive:
        if hasattr(tarfile, "data_filter"):
            archive.extractall(dest, filter="data")
            return
        root = dest.resolve()
        for member in archive.getmembers():
            target = (dest / member.name).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"archive member outside the leg: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"archive link refused: {member.name}")
        archive.extractall(dest)


def _read(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _append(path, text) -> None:
    with open(path, "ab") as fh:
        fh.write(str(text).encode("utf-8"))


def _last_line(text) -> str:
    """The last line of *text* that is output, not one of the runner's marks."""
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if line and not line.startswith(MARK):
            return line[:300]
    return ""


def _took(seconds) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def _count_text(counts) -> str:
    parts = []
    for key in _COUNT_ORDER:
        n = (counts or {}).get(key)
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            parts.append(f"{n} {'error' if key == 'errors' and n == 1 else key}")
    return ", ".join(parts)


@dataclass
class LegResult:
    """One leg's verdict. Every field is present on construction."""

    name: str
    job: str = ""
    ci_name: str = ""
    tier: str = ""
    state: str = "error"
    exit_code: int = 0
    counts: dict = field(default_factory=dict)
    failed_tests: tuple = ()
    duration_s: float = 0.0
    step: str = ""
    reason: str = ""
    argv: tuple = ()
    python: str = ""
    via: str = ""
    artifact: str = ""

    def summary(self) -> str:
        """One line, the shape a commit status description takes."""
        if self.state in ("success", "failure"):
            return f"{_count_text(self.counts) or self.state} in {_took(self.duration_s)}"
        return f"{self.step or 'leg'}: {self.reason or f'exit {self.exit_code}'}"

    def as_dict(self) -> dict:
        return {"name": self.name, "job": self.job, "ci_name": self.ci_name,
                "tier": self.tier, "state": self.state,
                "exit_code": int(self.exit_code), "counts": dict(self.counts),
                "failed_tests": list(self.failed_tests),
                "duration_s": round(self.duration_s, 3), "step": self.step,
                "reason": self.reason, "argv": list(self.argv),
                "python": self.python, "via": self.via,
                "artifact": self.artifact, "summary": self.summary()}


class LegRunner:
    """Runs the legs of one pre-merge run. Each `run` is independent of every
    other -- its own directory, log and artifact -- so a pool may call it from
    several threads at once."""

    def __init__(self, repo_root, work_root, *, run_id, run=proc.run, environ=None,
                 project=None, timeout_s=LEG_TIMEOUT_S,
                 wsl_prefix=("wsl.exe", "--exec"), wsl_env=None, keep=False,
                 clock=time.monotonic, windows=None):
        self.repo_root = Path(repo_root)
        self.work_root = Path(work_root)
        self.run_id = str(run_id)
        self._run = run
        self.environ = dict(os.environ if environ is None else environ)
        self.project = project
        self.timeout_s = float(timeout_s)
        self.wsl_prefix = list(wsl_prefix)
        self.wsl_env = dict(self.environ if wsl_env is None else wsl_env)
        self.keep = bool(keep)
        self._clock = clock
        self.windows = (os.name == "nt") if windows is None else bool(windows)

    def run(self, leg, availability, commit) -> LegResult:
        """Run *leg* against *commit* with what *availability* found. Never raises."""
        started = self._clock()
        res = LegResult(name=leg.name, job=leg.job, ci_name=leg.ci_name,
                        tier=leg.tier, python=availability.python,
                        via=availability.via)
        leg_dir = self.work_root / leg.name
        log = leg_dir / "leg.log"
        try:
            leg_dir.mkdir(parents=True, exist_ok=True)
            if availability.via == "wsl":
                self._wsl(leg, availability, str(commit), leg_dir, log, res)
            else:
                self._native(leg, availability, str(commit), leg_dir, log, res)
        except Exception as exc:                        # noqa: BLE001
            res.state, res.step = "error", res.step or "runner"
            res.reason = f"{type(exc).__name__}: {exc}"
        res.duration_s = self._clock() - started
        self._conclude(res, _read(log))
        if not self.keep:
            _rmtree_force(leg_dir)
        return res

    # --- steps ----------------------------------------------------------------------

    def _step(self, res, name, argv, *, log, cwd=None, env=None,
              timeout_s=STEP_TIMEOUT_S, stdin=None) -> bool:
        """One setup step. False -- with *res* an error AT *name* -- when it failed."""
        _append(log, f"{MARK} step {name}: {' '.join(str(a) for a in argv)}\n")
        rc, _, err = self._run(argv, cwd=cwd, env=env, timeout_s=timeout_s,
                               stdin=stdin, log=log)
        if err:
            _append(log, str(err).rstrip() + "\n")
        if rc == 0:
            return True
        res.state, res.step, res.exit_code = "error", name, int(rc)
        res.reason = _last_line(err) or _last_line(_read(log)) or f"exit {rc}"
        return False

    def _pytest(self, res, argv, *, log, cwd, env) -> None:
        res.argv = tuple(str(a) for a in argv)
        _append(log, f"{_PYTEST_STEP}: {' '.join(res.argv)}\n")
        rc, _, err = self._run(argv, cwd=cwd, env=env, timeout_s=self.timeout_s,
                               log=log)
        if err:
            _append(log, str(err).rstrip() + "\n")
        res.step, res.exit_code, res.state = "pytest", int(rc), classify(rc)
        if res.state == "error":
            res.reason = _last_line(err) or f"pytest exit {rc}"

    def _init_repo(self, res, repo, crlf, leg_dir, env, log) -> bool:
        hooks = leg_dir / "no-hooks"
        hooks.mkdir(exist_ok=True)
        steps = (
            [GIT, "-c", "init.defaultBranch=master", "init", "-q"],
            [GIT, "config", "core.autocrlf", crlf],
            [GIT, "config", "core.safecrlf", "false"],
            # -f: every file here was TRACKED; a .gitignore in the tree must
            # not drop one that was force-added upstream.
            [GIT, "add", "-A", "-f"],
            [GIT, "-c", f"user.name={CI_NAME}", "-c", f"user.email={CI_EMAIL}",
             "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={hooks}",
             "commit", "-q", "--no-verify", "-m", COMMIT_MESSAGE],
        )
        return all(self._step(res, "git-init", argv, log=log, cwd=repo, env=env)
                   for argv in steps)

    # --- a leg on this machine --------------------------------------------------------

    def _native(self, leg, avail, commit, leg_dir, log, res) -> None:
        crlf = autocrlf_for(leg)
        repo, tar = leg_dir / "repo", leg_dir / "tree.tar"
        (leg_dir / "home").mkdir(exist_ok=True)
        env = leg_env(self.environ, leg_dir)
        if not self._step(res, "archive",
                          [GIT, "-c", f"core.autocrlf={crlf}", "archive",
                           "--format=tar", "-o", str(tar), commit],
                          log=log, cwd=self.repo_root, env=env):
            return
        res.step = "extract"
        extract(tar, repo)
        tar.unlink()
        res.step = ""
        if not self._init_repo(res, repo, crlf, leg_dir, env, log):
            return
        if leg.clean_venv:
            venv = leg_dir / "venv"
            if not self._step(res, "venv", [avail.python, "-m", "venv", str(venv)],
                              log=log, cwd=leg_dir, env=env,
                              timeout_s=SETUP_TIMEOUT_S):
                return
            python = str(venv_python(venv, self.windows))
            env = leg_env(self.environ, leg_dir, bin_dir=Path(python).parent)
            for name, args in (("pip-upgrade", ["install", "--upgrade", "pip"]),
                               ("install", ["install", "-e", f".[{leg.extras}]"])):
                if not self._step(res, name, [python, "-m", "pip", *args],
                                  log=log, cwd=repo, env=env,
                                  timeout_s=SETUP_TIMEOUT_S):
                    return
        else:
            # Spelled as the probe found it: Path() would re-spell the
            # separators, and the record names the interpreter that answered.
            python = str(avail.python)
            env = leg_env(self.environ, leg_dir, bin_dir=Path(python).parent)
            env["PYTHONPATH"] = str(repo / "src")
            env["HF_HUB_OFFLINE"] = "1"
        if leg.tier == legs_mod.TIER_PG:
            env["MINDHIVE_PG_DSN_ADMIN"] = str(self.environ.get(legs_mod.PG_DSN_ENV) or "")
            if not self._step(res, "migrate",
                              [python, "-m", "mindhive", "pg", "migrate"],
                              log=log, cwd=repo, env=env, timeout_s=SETUP_TIMEOUT_S):
                return
        self._pytest(res, [python, "-m", "pytest", *leg.pytest_args],
                     log=log, cwd=repo, env=env)

    # --- a leg in WSL -----------------------------------------------------------------

    def _wsl(self, leg, avail, commit, leg_dir, log, res) -> None:
        tar = leg_dir / "tree.tar"
        env = leg_env(self.environ, leg_dir)
        if not self._step(res, "archive",
                          [GIT, "-c", "core.autocrlf=false", "archive",
                           "--format=tar", "-o", str(tar), commit],
                          log=log, cwd=self.repo_root, env=env):
            return
        data = tar.read_bytes()
        tar.unlink()
        rc, out, err = self._run(self.wsl_prefix + ["mktemp", "-d", "/tmp/mindhive-ci.XXXXXX"],
                                 env=self.wsl_env, timeout_s=STEP_TIMEOUT_S)
        root = (out or "").strip()
        if rc != 0 or not root.startswith("/tmp/mindhive-ci."):
            res.state, res.step, res.exit_code = "error", "mktemp", int(rc)
            res.reason = _last_line(err) or _last_line(out) or f"exit {rc}"
            return
        try:
            if not self._step(res, "extract", self.wsl_prefix + ["tar", "-x", "-C", root],
                              log=log, env=self.wsl_env, stdin=data):
                return
            res.argv = (avail.python, "-m", "pytest", *leg.pytest_args)
            argv = self.wsl_prefix + ["bash", "-l", "-s", "--", root, avail.python,
                                      leg.extras, str(int(self.timeout_s)),
                                      *leg.pytest_args]
            rc, _, err = self._run(argv, env=self.wsl_env, stdin=POSIX_LEG_SCRIPT,
                                   log=log, timeout_s=self.timeout_s + SETUP_TIMEOUT_S)
            if err:
                _append(log, str(err).rstrip() + "\n")
            self._judge_posix(res, rc, err, _read(log))
        finally:
            self._run(self.wsl_prefix + ["rm", "-rf", "--", root, f"{root}-venv",
                                         f"{root}-home"],
                      env=self.wsl_env, timeout_s=STEP_TIMEOUT_S)

    def _judge_posix(self, res, rc, err, text) -> None:
        failed = list(_SETUP_FAILED.finditer(text))
        exits = _PYTEST_EXIT.findall(text)
        if failed:
            last = failed[-1]
            res.state, res.step, res.exit_code = "error", last.group(1), int(rc)
            res.reason = _last_line(text[:last.start()]) or f"exit {rc}"
        elif exits:
            code = int(exits[-1])
            res.step, res.exit_code, res.state = "pytest", code, classify(code)
            if res.state == "error":
                res.reason = f"pytest exit {code}" + (
                    " -- killed at the cap" if code in (124, 137) else "")
        else:
            res.state, res.exit_code = "error", int(rc)
            res.step = "pytest" if _PYTEST_STEP in text else "wsl"
            res.reason = _last_line(err) or _last_line(text) or f"wsl.exe exit {rc}"

    # --- the record -------------------------------------------------------------------

    def _conclude(self, res, text) -> None:
        part = text.rsplit(_PYTEST_STEP, 1)[1] if _PYTEST_STEP in text else ""
        if part:
            res.counts = verify.parse_summary(part)
            res.failed_tests = tuple(
                line.strip() for line in part.splitlines()
                if line.startswith(("FAILED ", "ERROR ")))[:FAILED_CAP]
        if text.strip():
            try:
                res.artifact = runlog.write_once(f"premerge-{self.run_id}-{res.name}",
                                                 text, project=self.project) or ""
            except Exception:                           # noqa: BLE001
                res.artifact = ""       # the evidence is lost; the verdict is not
```

- [ ] **Step 4: run the tests and watch them pass**

Run: `pytest tests/test_ci_legrun.py -q`
Expected: all pass (the bash test skips where no `sh` is on PATH).

- [ ] **Step 5: prove the WSL fallback the script relies on, on the real WSL**

Run (Git Bash):
```bash
printf '%s' 'd=$(mktemp -d) && /usr/bin/python3.12 -m venv --without-pip "$d/v" && python3 -m pip --python "$d/v/bin/python" install -q pip && "$d/v/bin/python" -m pip --version; rm -rf "$d"' | WSL_UTF8=1 WSLENV= wsl.exe --exec bash -l -s
```
Expected: `pip <version> from /tmp/tmp.*/v/lib/python3.12/site-packages/pip (python 3.12)`.

- [ ] **Step 6: commit**

```bash
git add src/mindhive/ci/legrun.py tests/test_ci_legrun.py
git commit -m "feat(ci): legrun, each ci.yml leg in a fresh repo, venv and environment"
```

---

### Task 6: `premerge` — one PR, tested the way Actions tested it, then guarded

**Files:**
- Create: `src/mindhive/ci/premerge.py`
- Create: `tests/test_ci_premerge.py`

- [ ] **Step 1: write the failing test**

**File: `tests/test_ci_premerge.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.premerge` -- one PR tested the way Actions tested it.

Everything outside the module is a fake with the real one's contract: git
(fetch copies the remote's refs into the refspec's destination, rev-parse
reads them back, for-each-ref and `update-ref --stdin` manage them), gh (the
PR as GitHub answers it, statuses, the merge, the read-back), the probe, the
merge preview and the leg runner. What is asserted is what the module decides
and what it tells GitHub -- the ledger it writes is the real one, in the
test's own store.
"""
import itertools

import pytest

from mindhive import events
from mindhive.ci import legrun, legs, premerge
from mindhive.ci import preview as preview_mod

BASE, HEAD, NEWBASE, NEWHEAD, MERGED = ("1" * 40, "2" * 40, "3" * 40, "4" * 40,
                                        "5" * 40)
TREE, TREE2, TESTED, TESTED2 = "a" * 40, "b" * 40, "c" * 40, "d" * 40
NOW = "2026-09-30T04:05:06Z"


def pr_view(state="OPEN", draft=False, head=HEAD, base="master"):
    return {"number": 151, "state": state, "isDraft": draft,
            "headRefName": "claude/feature", "headRefOid": head,
            "baseRefName": base, "title": "a feature", "url": "https://x/151"}


class FakeGit:
    def __init__(self, fetch_rc=0, trees=None, local=None):
        self.remote = {"refs/heads/master": BASE, "refs/pull/151/head": HEAD,
                       "refs/pull/149/head": NEWHEAD}
        self.refs = {}
        self.local = dict(local or {"master": BASE})
        self.trees = dict(trees or {MERGED: TREE})
        self.fetch_rc = fetch_rc
        self.calls = []

    def __call__(self, argv, cwd=None, env=None, timeout_s=None, stdin=None, log=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        verb = argv[1]
        if verb == "fetch":
            if self.fetch_rc:
                return self.fetch_rc, "", "fatal: unable to access: Could not resolve host"
            for spec in argv[argv.index("origin") + 1:]:
                src, dst = spec.lstrip("+").split(":", 1)
                if src not in self.remote:
                    return 128, "", f"fatal: couldn't find remote ref {src}"
                self.refs[dst] = self.remote[src]
            return 0, "", ""
        if verb == "rev-parse":
            name, _, kind = argv[-1].partition("^{")
            if kind == "commit}":
                sha = self.refs.get(name) or self.local.get(name)
            else:
                sha = self.trees.get(name)
            return (0, sha + "\n", "") if sha else (1, "", "")
        if verb == "for-each-ref":
            return 0, "".join(r + "\n" for r in sorted(self.refs)
                              if r.startswith(argv[-1])), ""
        if verb == "update-ref":
            for line in (stdin or "").splitlines():
                assert line.startswith("delete "), line
                self.refs.pop(line[len("delete "):], None)
            return 0, "", ""
        return 1, "", f"git {verb}: not in this fake"

    def run_refs(self):
        return [r for r in self.refs if r.startswith(premerge.REF_ROOT + "/")]


class FakeGitHub:
    def __init__(self, prs=None, merge_answer=(0, "merged"),
                 merged_answer=("MERGED", MERGED)):
        self.prs = list(prs or [pr_view()])
        self.pr_calls = 0
        self.statuses, self.merges = [], []
        self.merge_answer, self.merged_answer = merge_answer, merged_answer

    def pr(self, number):
        assert number == 151
        answer = self.prs[min(self.pr_calls, len(self.prs) - 1)]
        self.pr_calls += 1
        return dict(answer)

    def post_status(self, sha, state, context="mindhive-ci", description=""):
        self.statuses.append((sha, state, context, description))
        return ""

    def merge(self, number, head_sha):
        self.merges.append((number, head_sha))
        return self.merge_answer

    def merged(self, number):
        return self.merged_answer


class NoGitHub:
    def __getattr__(self, name):
        raise AssertionError(f"branch mode must not touch GitHub ({name})")


class FakeProbe:
    def __init__(self, missing=None):
        self.missing = dict(missing or {})

    def check(self, leg):
        if leg.name in self.missing:
            return legs.Availability(leg, False, reason=self.missing[leg.name])
        return legs.Availability(leg, True, python=f"py{leg.python or '-host'}",
                                 via="native")

    def wsl_prefix(self):
        return ["wsl.exe", "--exec"]

    def wsl_env(self):
        return {"WSLENV": ""}


class Legs:
    """The leg-runner factory: each leg answers the state it is told to."""

    def __init__(self, states=None, raises=(), on_run=None):
        self.states, self.raises, self.on_run = dict(states or {}), set(raises), on_run
        self.runs = []

    def __call__(self, run_tag):
        outer = self

        class _Runner:
            def run(self, leg, avail, commit):
                outer.runs.append((run_tag, leg.name, commit))
                if outer.on_run:
                    outer.on_run()
                if leg.name in outer.raises:
                    raise RuntimeError("the leg runner broke")
                state = outer.states.get(leg.name, "success")
                return legrun.LegResult(
                    name=leg.name, job=leg.job, ci_name=leg.ci_name, tier=leg.tier,
                    state=state, exit_code={"success": 0, "failure": 1}.get(state, 2),
                    counts={"passed": 10} if state == "success" else {"failed": 1, "passed": 9},
                    duration_s=61.0, step="pytest")
        return _Runner()


class Previews:
    def __init__(self, answers=None, tree_of=None, raises=False):
        self.answers = dict(answers or {})
        self.tree_of = tree_of or (lambda base: TREE)
        self.raises = raises
        self.asked = []

    def __call__(self, base, head):
        self.asked.append((base, head))
        if self.raises:
            raise RuntimeError("merge-tree went away")
        if (base, head) in self.answers:
            return self.answers[(base, head)]
        return preview_mod.Preview(base, head, clean=True, tree=self.tree_of(base),
                                   tested_sha=TESTED)


class Rig:
    def __init__(self, tmp_path, *, git=None, gh=None, probe=None, legs_=None,
                 previews=None, **kw):
        self.git = git or FakeGit()
        self.gh = gh or FakeGitHub()
        self.legs = legs_ or Legs()
        self.previews = previews or Previews()
        self.said = []
        self.pm = premerge.Premerge(tmp_path / "repo", project="test", run=self.git,
                                    environ={}, github=self.gh,
                                    probe=probe or FakeProbe(), leg_runner=self.legs,
                                    previewer=self.previews, now_iso=lambda: NOW,
                                    work_root=tmp_path / "work", say=self.said.append,
                                    **kw)


def recorded():
    return [e.payload for e in events.read_all("test")
            if e.type == "verification" and (e.payload or {}).get("gate") == "premerge"]


def leg_result(name, state="success"):
    leg = legs.select(name)[0]
    return legrun.LegResult(name=name, tier=leg.tier, state=state)


# --- the verdict --------------------------------------------------------------------


def test_the_verdict_table():
    clean = preview_mod.Preview(BASE, HEAD, clean=True, tree=TREE, tested_sha=TESTED)
    ok = [leg_result("windows-py3.12"), leg_result("node")]
    assert premerge.decide(preview_mod.Preview(BASE, HEAD, error="git merge-tree: boom"),
                           ok) == ("error", 2, "git merge-tree: boom")
    conflicted = preview_mod.Preview(BASE, HEAD, conflicts=("a.py", "b.py"), tree=TREE)
    assert premerge.decide(conflicted, []) == (
        "red", 1, "conflicts with the base in 2 file(s): a.py, b.py")
    assert premerge.decide(clean, [leg_result("windows-py3.12", "failure"),
                                   leg_result("node", "error")]) == (
        "red", 1, "red: windows-py3.12 (failure), node (error)")
    assert premerge.decide(clean, [leg_result("node")]) == (
        "error", 2, "no full-suite leg ran on this machine")
    assert premerge.decide(clean, ok) == ("green", 0, "green: 2 leg(s) passed")
    assert premerge.decide(clean, ok, [{"name": "pg", "reason": "no dsn"}]) == (
        "green", 0, "green: 2 leg(s) passed; not run here: pg")
    many = preview_mod.Preview(BASE, HEAD, conflicts=tuple(f"f{i}.py" for i in range(7)))
    assert premerge.decide(many, [])[2].endswith("f4.py and 2 more")


# --- a green run ------------------------------------------------------------------------


def test_a_green_run_posts_pending_then_each_leg_then_the_verdict_on_the_head(tmp_path):
    rig = Rig(tmp_path)
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12,node")
    assert (res.verdict, res.exit_code, res.reason) == ("green", 0, "green: 2 leg(s) passed")
    assert (res.base_sha, res.head_sha, res.tested_sha, res.tree) == (BASE, HEAD, TESTED, TREE)
    assert rig.previews.asked == [(BASE, HEAD)]
    assert [(name, commit) for _, name, commit in rig.legs.runs] in (
        [("windows-py3.12", TESTED), ("node", TESTED)],
        [("node", TESTED), ("windows-py3.12", TESTED)])
    posted = rig.gh.statuses
    assert {sha for sha, *_ in posted} == {HEAD}
    assert posted[0] == (HEAD, "pending", "mindhive-ci",
                         "testing the merge into master on 2 leg(s)")
    assert posted[1:3] == [(HEAD, "pending", "mindhive-ci/windows-py3.12", "queued"),
                           (HEAD, "pending", "mindhive-ci/node", "queued")]
    assert set(posted[3:5]) == {(HEAD, "success", "mindhive-ci/windows-py3.12", "10 passed in 1m01s"),
                                (HEAD, "success", "mindhive-ci/node", "10 passed in 1m01s")}
    assert posted[5:] == [(HEAD, "success", "mindhive-ci", "green: 2 leg(s) passed")]
    assert [leg.name for leg in res.legs] == ["windows-py3.12", "node"]   # catalog order
    assert any(line.startswith("node: success") for line in rig.said)
    assert res.merge_state == "" and rig.gh.merges == []
    assert "verdict: GREEN (exit 0) -- green: 2 leg(s) passed" in res.report_text


def test_the_run_is_recorded_once_and_remembered_for_watch(tmp_path):
    Rig(tmp_path).pm.run_pr(151, legs_spec="windows-py3.12")
    rows = recorded()
    assert len(rows) == 1
    row = rows[0]
    assert row["argv"] == ["hive", "ci", "premerge", "151", "--legs", "windows-py3.12"]
    assert (row["exit_code"], row["verdict"], row["verified_by"]) == (0, "green", "python")
    assert (row["scope"], row["gate"], row["mode"], row["pr_id"]) == ("ci", "premerge", "github", 151)
    assert (row["commit_sha"], row["base_sha"], row["tested_sha"], row["tree"]) == (
        HEAD, BASE, TESTED, TREE)
    assert row["merge_state"] == "skipped" and row["counts"] == {"passed": 10}
    assert [leg["name"] for leg in row["legs"]] == ["windows-py3.12"]
    assert "verdict: GREEN" in row["excerpt"] and row["artifact"]
    kept = premerge.load_sidecar("test")["prs"]["151"]
    assert (kept["head_sha"], kept["base_sha"], kept["tree"], kept["verdict"]) == (
        HEAD, BASE, TREE, "green")
    assert kept["legs"] == {"windows-py3.12": "success"} and kept["base_ref"] == "master"


# --- red, unavailable, quiet ------------------------------------------------------------


def test_a_conflict_is_red_at_once_and_no_leg_runs(tmp_path):
    conflicted = preview_mod.Preview(BASE, HEAD, conflicts=("src/mindhive/x.py",), tree=TREE)
    rig = Rig(tmp_path, previews=Previews({(BASE, HEAD): conflicted}))
    res = rig.pm.run_pr(151)
    assert (res.verdict, res.exit_code) == ("red", 1)
    assert res.conflicts == ("src/mindhive/x.py",) and rig.legs.runs == []
    assert rig.gh.statuses == [(HEAD, "failure", "mindhive-ci",
                                "conflicts with the base in 1 file(s): src/mindhive/x.py")]
    assert "conflicts (1): src/mindhive/x.py" in res.report_text


def test_a_leg_this_machine_cannot_run_is_named_and_gets_no_status(tmp_path):
    why = "no MINDHIVE_CI_PG_DSN: the pg tier needs a throwaway Postgres 17 database"
    rig = Rig(tmp_path, probe=FakeProbe({"pg": why}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12,pg")
    assert res.verdict == "green" and res.reason == "green: 1 leg(s) passed; not run here: pg"
    assert res.unavailable == [{"name": "pg", "reason": why}]
    contexts = [ctx for _, _, ctx, _ in rig.gh.statuses]
    assert "mindhive-ci/windows-py3.12" in contexts        # canary: statuses were posted
    assert "mindhive-ci/pg" not in contexts
    assert "not run on this machine:\n  pg: " + why in res.report_text


def test_no_status_means_no_status(tmp_path):
    rig = Rig(tmp_path, post_statuses=False)
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12")
    assert res.verdict == "green" and rig.gh.statuses == []
    assert recorded()[0]["argv"][-1] == "--no-status"


def test_a_fetch_that_fails_is_an_error_and_nothing_runs_or_posts(tmp_path):
    rig = Rig(tmp_path, git=FakeGit(fetch_rc=128))
    res = rig.pm.run_pr(151)
    assert (res.verdict, res.exit_code) == ("error", 2)
    assert res.reason.startswith("git fetch origin: fatal: unable to access")
    assert rig.legs.runs == [] and rig.gh.statuses == [] and rig.previews.asked == []


@pytest.mark.parametrize("case", ["green", "conflict", "preview-raises"])
def test_the_runs_refs_are_dropped_whatever_happened(tmp_path, case):
    previews = {"green": Previews(),
                "conflict": Previews({(BASE, HEAD): preview_mod.Preview(
                    BASE, HEAD, conflicts=("a.py",), tree=TREE)}),
                "preview-raises": Previews(raises=True)}[case]
    rig = Rig(tmp_path, previews=previews)
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12")
    assert rig.git.run_refs() == []
    assert any(argv[1] == "update-ref" for argv in rig.git.calls)
    if case == "preview-raises":
        assert (res.verdict, res.exit_code) == ("error", 2)
        assert res.reason == "premerge broke: RuntimeError: merge-tree went away"
        assert rig.gh.statuses[-1] == (HEAD, "error", "mindhive-ci", res.reason)


def test_a_leg_runner_that_raises_is_a_red_leg_not_a_crash(tmp_path):
    rig = Rig(tmp_path, legs_=Legs(raises={"node"}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12,node")
    assert (res.verdict, res.reason) == ("red", "red: node (error)")
    node = res.legs[1]
    assert (node.state, node.step) == ("error", "runner")
    assert (HEAD, "error", "mindhive-ci/node",
            "runner: RuntimeError: the leg runner broke") in rig.gh.statuses


# --- the guarded merge ----------------------------------------------------------------


def test_the_merge_is_the_pinned_merge_commit_and_is_read_back(tmp_path):
    rig = Rig(tmp_path)
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
    assert rig.gh.merges == [(151, HEAD)]
    assert (res.merge_state, res.merge_commit, res.exit_code) == ("merged", MERGED, 0)
    assert res.landed_tree_matches is True
    assert "merge: merged as 555555555555; the landed tree is the tested tree" in res.report_text
    assert recorded()[0]["merge_state"] == "merged"


@pytest.mark.parametrize("prs,why", [
    ([pr_view(), pr_view(head=NEWHEAD)],
     "the head moved to 444444444444 after 222222222222 was tested"),
    ([pr_view(draft=True)], "the PR is a draft"),
    ([pr_view(), pr_view(state="CLOSED")], "the PR is CLOSED"),
])
def test_a_merge_is_refused_when_the_pr_is_not_the_one_tested(tmp_path, prs, why):
    rig = Rig(tmp_path, gh=FakeGitHub(prs=prs))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
    assert res.verdict == "green" and rig.gh.merges == []
    assert (res.merge_state, res.exit_code) == (f"refused: {why}", premerge.EXIT_NOT_MERGED)


def test_a_base_that_moved_to_a_different_merge_is_tested_again(tmp_path):
    git = FakeGit(trees={MERGED: TREE2})
    rig = Rig(tmp_path, git=git,
              legs_=Legs(on_run=lambda: git.remote.update({"refs/heads/master": NEWBASE})),
              previews=Previews({(NEWBASE, HEAD): preview_mod.Preview(
                  NEWBASE, HEAD, clean=True, tree=TREE2, tested_sha=TESTED2)}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
    assert res.rounds == 2
    assert [commit for _, _, commit in rig.legs.runs] == [TESTED, TESTED2]
    assert rig.gh.merges == [(151, HEAD)]
    assert (res.merge_state, res.exit_code, res.landed_tree_matches) == ("merged", 0, True)
    assert (res.base_sha, res.tree) == (NEWBASE, TREE2)
    assert any("the base moved to 333333333333 after 111111111111 was tested and "
               "the merge changed; testing again" == note for note in res.notes)
    assert len({tag for tag, _, _ in rig.legs.runs}) == 2      # one leg dir per round


def test_allow_base_drift_merges_an_untested_tree_and_says_so(tmp_path):
    git = FakeGit(trees={MERGED: TREE2})
    rig = Rig(tmp_path, git=git,
              legs_=Legs(on_run=lambda: git.remote.update({"refs/heads/master": NEWBASE})),
              previews=Previews({(NEWBASE, HEAD): preview_mod.Preview(
                  NEWBASE, HEAD, clean=True, tree=TREE2, tested_sha=TESTED2)}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True, allow_base_drift=True)
    assert res.rounds == 1 and rig.gh.merges == [(151, HEAD)]
    assert res.merge_state == "merged" and res.landed_tree_matches is False
    assert any("--allow-base-drift merged a tree this run did not test" in n for n in res.notes)
    assert any("carries a tree other than the tested" in n for n in res.notes)


def test_a_base_that_never_stops_moving_is_refused_after_the_last_round(tmp_path):
    git = FakeGit()
    moves = itertools.count(100)
    rig = Rig(tmp_path, git=git,
              legs_=Legs(on_run=lambda: git.remote.update(
                  {"refs/heads/master": f"{next(moves):040x}"})),
              previews=Previews(tree_of=lambda base: "t" + base[1:]))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
    assert res.rounds == premerge.MAX_ROUNDS and rig.gh.merges == []
    assert res.merge_state == ("refused: the base kept moving; 3 rounds tested "
                               "without landing")
    assert res.exit_code == premerge.EXIT_NOT_MERGED


def test_a_merge_github_did_not_make_exits_3_in_ghs_words(tmp_path):
    rig = Rig(tmp_path, gh=FakeGitHub(
        merge_answer=(1, "GraphQL: Base branch was modified. Review and try the merge again."),
        merged_answer=("OPEN", "")))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
    assert res.merge_state == ("failed: gh pr merge exit 1: GraphQL: Base branch was "
                               "modified. Review and try the merge again.")
    assert res.exit_code == premerge.EXIT_NOT_MERGED and res.merge_commit == ""


def test_a_landed_tree_other_than_the_tested_one_is_reported(tmp_path):
    rig = Rig(tmp_path, git=FakeGit(trees={MERGED: TREE2}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
    assert res.merge_state == "merged" and res.landed_tree_matches is False
    assert "the landed tree is NOT the tested tree" in res.report_text


# --- branch mode, and the checks before anything runs ------------------------------------


@pytest.mark.parametrize("fetch_rc", [0, 128])
def test_branch_mode_tests_the_merge_locally_and_never_touches_github(tmp_path, fetch_rc):
    git = FakeGit(fetch_rc=fetch_rc, local={"master": BASE, "claude/feature": HEAD})
    rig = Rig(tmp_path, git=git, gh=NoGitHub())
    res = rig.pm.run_branch("claude/feature", legs_spec="windows-py3.12")
    assert (res.verdict, res.mode, res.base_sha, res.head_sha) == (
        "green", "offline", BASE, HEAD)
    assert git.run_refs() == []
    if fetch_rc:
        assert res.notes[0].endswith("; tested against the local master")
    assert premerge.load_sidecar("test")["branches"]["claude/feature"]["verdict"] == "green"
    assert recorded()[0]["argv"] == ["hive", "ci", "premerge", "--branch",
                                     "claude/feature", "--base", "master",
                                     "--legs", "windows-py3.12"]


def test_branch_mode_names_a_branch_git_does_not_know(tmp_path):
    rig = Rig(tmp_path, gh=NoGitHub())
    res = rig.pm.run_branch("no/such-branch")
    assert (res.verdict, res.exit_code) == ("error", 2)
    assert res.reason == "no commit for the branch no/such-branch"


def test_a_pr_that_is_not_open_is_an_error_before_any_fetch(tmp_path):
    rig = Rig(tmp_path, gh=FakeGitHub(prs=[pr_view(state="MERGED")]))
    res = rig.pm.run_pr(151)
    assert (res.verdict, res.exit_code) == ("error", 2)
    assert res.reason == "PR #151 is MERGED, not OPEN"
    assert rig.git.calls == [] and recorded() == []


def test_an_unknown_leg_is_an_error_naming_the_legs(tmp_path):
    rig = Rig(tmp_path)
    res = rig.pm.run_pr(151, legs_spec="nope")
    assert res.exit_code == 2 and res.reason.startswith("no leg matches nope; legs are: ")
    assert rig.gh.pr_calls == 0 and rig.git.calls == []


# --- the pieces watch and conflicts use ---------------------------------------------------


def test_fetch_base_and_fetch_heads_leave_no_ref_behind(tmp_path):
    rig = Rig(tmp_path)
    assert rig.pm.fetch_base("master") == (BASE, "")
    assert rig.pm.fetch_heads([151, 149], "master") == (BASE, {151: HEAD, 149: NEWHEAD}, "")
    assert rig.git.run_refs() == []
    base, heads, error = rig.pm.fetch_heads([151, 7], "master")
    assert error.startswith("git fetch origin: fatal: couldn't find remote ref refs/pull/7/head")


def test_the_sidecar_survives_a_corrupt_file_and_a_write_that_fails(tmp_path, monkeypatch):
    premerge.sidecar_path("test").write_text("{not json", encoding="utf-8")
    assert premerge.load_sidecar("test") == {"version": 1, "prs": {}, "branches": {}}
    assert premerge.remember("test", 151, {"verdict": "green"}) is True
    assert premerge.load_sidecar("test")["prs"] == {"151": {"verdict": "green"}}

    def refuse(path, text):
        raise OSError("disk full")
    monkeypatch.setattr(premerge.paths, "write_text_atomic", refuse)
    assert premerge.remember("test", 152, {"verdict": "red"}) is False


def test_the_default_leg_runner_runs_under_the_work_root_with_the_probes_wsl(tmp_path):
    pm = premerge.Premerge(tmp_path / "repo", project="test", environ={"A": "1"},
                           github=FakeGitHub(), probe=FakeProbe(),
                           work_root=tmp_path / "work", timeout_s=77, keep=True)
    runner = pm._runner_for("pr151-x-r1")
    assert isinstance(runner, legrun.LegRunner)
    assert (runner.work_root, runner.run_id, runner.timeout_s, runner.keep) == (
        tmp_path / "work" / "pr151-x-r1", "pr151-x-r1", 77.0, True)
    assert (runner.wsl_prefix, runner.wsl_env) == (["wsl.exe", "--exec"], {"WSLENV": ""})
    assert premerge.Premerge(tmp_path / "repo", github=FakeGitHub(),
                             probe=FakeProbe()).work_root == (
        tmp_path / "repo" / ".worktrees" / "premerge")
```

- [ ] **Step 2: run it and watch it fail**

Run: `pytest tests/test_ci_premerge.py -q`
Expected: `ImportError: cannot import name 'premerge' from 'mindhive.ci'`.

- [ ] **Step 3: write the module**

**File: `src/mindhive/ci/premerge.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/premerge.py
"""`hive ci premerge` -- the pre-merge check GitHub Actions ran, run on this
machine (spec 2026-09-30).

The account's Actions jobs are refused for payment, so no PR here can go
green on GitHub's runners. This runs the same jobs -- ci.yml's matrix and
tiers, pinned to ci.yml by `tests/test_ci_legs.py` -- against the same thing
Actions tested: the MERGE a PR would make, not its head alone.

One run, in order:

1. **Fetch** the base branch and `refs/pull/N/head` from `origin` into refs of
   the run's own (`refs/mindhive-ci/<tag>/...`), so nothing the operator has
   checked out, stashed or branched is read or moved. They are deleted when
   the run ends, whatever happened.
2. **Preview the merge** in the object store (`preview.merge_preview`). A
   conflict is red at once: no leg runs on a merge that cannot be made.
3. **Probe** each leg (`legs.Probe`). A leg this machine cannot run is named,
   never counted -- the verdict says what was not run here.
4. **Run the legs**, a few at a time (`legrun.LegRunner`), each in a fresh
   repository of the tested commit with its own venv and environment.
5. **Decide** (`decide`) and **post commit statuses** on the PR's head:
   `mindhive-ci` for the verdict, `mindhive-ci/<leg>` per leg. A status is a
   plain REST write -- nothing billed, no runner.
6. **Record** one `verification` event (`scope: "ci"`, `gate: "premerge"`)
   with the legs, their counts and the report as a runlog artifact, and
   remember the tested head, base and tree in `state/ci_premerge.json` for
   `hive ci watch`.

**`--merge` is guarded**, because a merge cannot be taken back. After a green
verdict the PR is read again: still OPEN, not a draft, and its head the head
that was tested. The base is fetched again; if it moved, the merge is
previewed again, and a merge other than the tested one is tested again (at
most `MAX_ROUNDS` rounds) -- unless `--allow-base-drift` accepts a clean but
untested tree, which the report then says. Then `gh pr merge N --merge
--match-head-commit <head>` (`github.GitHub.merge`), and the PR is read back:
only GitHub answering MERGED makes the merge state `merged`. The landed merge
commit's tree is compared with the tested tree, and a difference is reported.

Exit codes: 0 green; 1 red (a conflict, or a leg that did not pass); 2 the
check could not be made (fetch, preview, no full-suite leg runnable); 3 green,
but `--merge` did not merge -- the report says why.
"""
import concurrent.futures
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import events, paths, runlog, verify
from . import github as gh_mod
from . import legrun
from . import legs as legs_mod
from . import preview as preview_mod
from . import proc
from .runner import SCOPE, _rmtree_force

__all__ = ["DEFAULT_JOBS", "EXIT_NOT_MERGED", "GATE", "MAX_ROUNDS", "OVERALL_STATE",
           "Premerge", "PremergeResult", "REF_ROOT", "REMOTE", "SIDECAR", "decide",
           "load_sidecar", "remember", "render", "sidecar_path", "sum_counts"]

GIT = "git"
REMOTE = "origin"
#: Every ref a run makes lives under here, in a directory named by the run's
#: tag, and is deleted when the run ends.
REF_ROOT = "refs/mindhive-ci"
GATE = "premerge"
#: Rounds a guarded merge may test before it gives up on a base that keeps moving.
MAX_ROUNDS = 3
DEFAULT_JOBS = 3
EXIT_NOT_MERGED = 3
SIDECAR = "ci_premerge.json"
WORK_DIRNAME = ".worktrees"
FETCH_TIMEOUT_S = 600.0
GIT_TIMEOUT_S = 120.0
#: The overall commit status each verdict posts.
OVERALL_STATE = {"green": "success", "red": "failure", "error": "error"}
_OID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_NOT_STAMP = re.compile(r"[^0-9TZ]")


def _why(rc, out, err) -> str:
    text = (err or "").strip() or (out or "").strip()
    return text.splitlines()[-1][:300] if text else f"exit {rc}"


def _short(sha) -> str:
    return str(sha or "")[:12] or "(none)"


def decide(preview, legs, unavailable=()) -> tuple:
    """``(verdict, exit_code, reason)``: green 0, red 1, error 2.

    Red is anything a merge would carry: a conflict, or a leg that did not
    pass -- one whose setup failed included, since the suite did not pass
    there either. Error is a check that could not be made. Green needs at
    least one full-suite (default tier) leg: the node or browser tier alone is
    not a verdict on the suite."""
    if preview is None or preview.error:
        return "error", 2, (preview.error if preview is not None else "no merge preview")
    if preview.conflicts:
        shown = ", ".join(preview.conflicts[:5])
        more = len(preview.conflicts) - 5
        return "red", 1, (f"conflicts with the base in {len(preview.conflicts)} "
                          f"file(s): {shown}" + (f" and {more} more" if more > 0 else ""))
    bad = [leg for leg in legs if leg.state != "success"]
    if bad:
        return "red", 1, "red: " + ", ".join(f"{leg.name} ({leg.state})" for leg in bad)
    if not any(leg.tier == legs_mod.TIER_DEFAULT for leg in legs):
        return "error", 2, "no full-suite leg ran on this machine"
    reason = f"green: {len(legs)} leg(s) passed"
    names = [u.get("name", "") if isinstance(u, dict) else str(u) for u in unavailable]
    if names:
        reason += "; not run here: " + ", ".join(names)
    return "green", 0, reason


def _merge_refusal(pr, tested_head) -> str:
    """Why a green PR must not be merged now, or ``""``."""
    state = str(pr.get("state") or "")
    if state != "OPEN":
        return f"the PR is {state or 'in no state gh reported'}"
    if pr.get("isDraft"):
        return "the PR is a draft"
    head = str(pr.get("headRefOid") or "")
    if head != tested_head:
        return f"the head moved to {_short(head)} after {_short(tested_head)} was tested"
    return ""


@dataclass
class PremergeResult:
    """One pre-merge run. Every field is present on construction."""

    pr_id: int = 0
    branch: str = ""
    base_ref: str = ""
    mode: str = "github"
    argv: list = field(default_factory=list)
    base_sha: str = ""
    head_sha: str = ""
    tested_sha: str = ""
    tree: str = ""
    conflicts: tuple = ()
    up_to_date: bool = False
    legs: list = field(default_factory=list)
    unavailable: list = field(default_factory=list)
    verdict: str = "error"
    exit_code: int = 2
    reason: str = ""
    rounds: int = 0
    duration_s: float = 0.0
    status_errors: list = field(default_factory=list)
    merge_state: str = ""
    merge_commit: str = ""
    landed_tree_matches: object = None
    notes: list = field(default_factory=list)
    report_text: str = ""

    def as_dict(self) -> dict:
        return {"pr_id": self.pr_id, "branch": self.branch, "base_ref": self.base_ref,
                "mode": self.mode, "argv": list(self.argv), "base_sha": self.base_sha,
                "head_sha": self.head_sha, "tested_sha": self.tested_sha,
                "tree": self.tree, "conflicts": list(self.conflicts),
                "up_to_date": self.up_to_date,
                "legs": [leg.as_dict() for leg in self.legs],
                "unavailable": [dict(u) for u in self.unavailable],
                "verdict": self.verdict, "exit_code": int(self.exit_code),
                "reason": self.reason, "rounds": self.rounds,
                "duration_s": round(self.duration_s, 3),
                "status_errors": list(self.status_errors),
                "merge_state": self.merge_state, "merge_commit": self.merge_commit,
                "landed_tree_matches": self.landed_tree_matches,
                "notes": list(self.notes)}


def sum_counts(legs) -> dict:
    """Every leg's pytest counts, added up."""
    total = {}
    for leg in legs:
        for key, n in (leg.counts or {}).items():
            if isinstance(n, int) and not isinstance(n, bool):
                total[key] = total.get(key, 0) + n
    return total


def render(res) -> str:
    """The report `hive ci premerge` prints and the artifact keeps."""
    subject = f"PR #{res.pr_id} ({res.branch})" if res.pr_id else f"branch {res.branch}"
    lines = [f"hive ci premerge: {subject}"
             + (f" into {res.base_ref}" if res.base_ref else "")]
    if res.base_sha or res.head_sha:
        lines.append(f"base {_short(res.base_sha)}  head {_short(res.head_sha)}"
                     + (f"  merge tree {_short(res.tree)}" if res.tree else ""))
    if res.tested_sha:
        what = ("the head itself: the base is already in it" if res.up_to_date
                else "the merge commit a merge would make")
        lines.append(f"tested: {_short(res.tested_sha)} ({what})")
    if res.conflicts:
        lines.append(f"conflicts ({len(res.conflicts)}): " + ", ".join(res.conflicts))
    if res.legs:
        width = max(len(leg.name) for leg in res.legs)
        lines.append("legs:")
        for leg in res.legs:
            lines.append(f"  {leg.name:<{width}}  {leg.state:<7}  {leg.summary()}")
            if leg.state != "success":
                lines.extend(f"      {test}" for test in leg.failed_tests[:5])
                if leg.artifact:
                    lines.append(f"      log: {leg.artifact}")
    if res.unavailable:
        lines.append("not run on this machine:")
        lines.extend(f"  {u.get('name')}: {u.get('reason')}" for u in res.unavailable)
    lines.append(f"verdict: {res.verdict.upper()} (exit {res.exit_code}) -- {res.reason}")
    if res.merge_state:
        tail = ""
        if res.landed_tree_matches is True:
            tail = "; the landed tree is the tested tree"
        elif res.landed_tree_matches is False:
            tail = "; the landed tree is NOT the tested tree"
        lines.append(f"merge: {res.merge_state}"
                     + (f" as {_short(res.merge_commit)}" if res.merge_commit else "")
                     + tail)
    if res.status_errors:
        lines.append(f"statuses: {len(res.status_errors)} could not be posted; "
                     f"first: {res.status_errors[0]}")
    lines.extend(f"note: {note}" for note in res.notes)
    return "\n".join(lines)


# --- what `watch` reads ------------------------------------------------------------------


def sidecar_path(project=None) -> Path:
    return paths.state_dir(project) / SIDECAR


def load_sidecar(project=None) -> dict:
    """The last tested head, base and tree per PR (and per branch). Total: a
    missing or unreadable file is an empty one."""
    empty = {"version": 1, "prs": {}, "branches": {}}
    try:
        data = json.loads(sidecar_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    for bucket in ("prs", "branches"):
        if not isinstance(data.get(bucket), dict):
            data[bucket] = {}
    data["version"] = 1
    return data


def remember(project, key, entry, bucket="prs") -> bool:
    """Record *entry* under *key*. Never raises: False when it could not."""
    try:
        data = load_sidecar(project)
        data[bucket][str(key)] = dict(entry)
        paths.write_text_atomic(sidecar_path(project),
                                json.dumps(data, indent=2, sort_keys=True,
                                           ensure_ascii=False) + "\n")
        return True
    except Exception:                                   # noqa: BLE001
        return False


# --- the run -------------------------------------------------------------------------------


class Premerge:
    """Pre-merge runs for one repository. Every collaborator is injectable;
    the defaults are the real ones."""

    def __init__(self, repo_root, *, project=None, run=proc.run, environ=None,
                 github=None, probe=None, leg_runner=None, previewer=None,
                 clock=time.monotonic, now_iso=events.now_iso, work_root=None,
                 jobs=DEFAULT_JOBS, timeout_s=legrun.LEG_TIMEOUT_S,
                 post_statuses=True, keep=False, say=None):
        self.repo_root = Path(repo_root)
        self.project = project
        self.spawn = run
        self.environ = dict(os.environ if environ is None else environ)
        self.github = github if github is not None else gh_mod.GitHub(self.repo_root, run=run)
        self.probe = probe if probe is not None else legs_mod.Probe(run=run,
                                                                     environ=self.environ)
        self._leg_runner = leg_runner
        self._previewer = previewer
        self._clock = clock
        self._now_iso = now_iso
        self.work_root = (Path(work_root) if work_root
                          else self.repo_root / WORK_DIRNAME / "premerge")
        self.jobs = max(1, int(jobs))
        self.timeout_s = float(timeout_s)
        self.post_statuses = bool(post_statuses)
        self.keep = bool(keep)
        self._say = say or (lambda text: None)

    # --- the two entry points -------------------------------------------------------------

    def run_pr(self, number, *, legs_spec=None, merge=False,
               allow_base_drift=False) -> PremergeResult:
        """Test PR *number*; with *merge*, merge it when green. Never raises."""
        started = self._clock()
        number = int(number)
        argv = ["hive", "ci", GATE, str(number)]
        if legs_spec:
            argv += ["--legs", str(legs_spec)]
        for flag, on in (("--merge", merge), ("--allow-base-drift", allow_base_drift),
                         ("--no-status", not self.post_statuses)):
            if on:
                argv.append(flag)
        res = PremergeResult(pr_id=number, mode="github", argv=argv)
        try:
            chosen = legs_mod.select(legs_spec)
            pr = self.github.pr(number)
        except (ValueError, gh_mod.GitHubError) as exc:
            res.reason = str(exc)
            return self._finish(res, started, None)
        res.branch = str(pr.get("headRefName") or "")
        res.base_ref = str(pr.get("baseRefName") or "")
        state = str(pr.get("state") or "")
        if state != "OPEN":
            res.reason = f"PR #{number} is {state or 'in no state gh reported'}, not OPEN"
            return self._finish(res, started, None)
        tag = self._tag(f"pr{number}")
        try:
            for _ in range(MAX_ROUNDS):
                res.rounds += 1
                self._round(res, tag, chosen, number)
                if not merge or res.verdict != "green":
                    break
                if self._guarded_merge(res, tag, number, allow_base_drift) != "retest":
                    break
            else:
                res.merge_state = (f"refused: the base kept moving; {MAX_ROUNDS} rounds "
                                   f"tested without landing")
        except Exception as exc:                        # noqa: BLE001
            res.verdict, res.exit_code = "error", 2
            res.reason = f"premerge broke: {type(exc).__name__}: {exc}"
            if self.post_statuses and res.head_sha:
                self._status(res, "error", gh_mod.CONTEXT, res.reason)
        finally:
            self._drop(tag)
        if merge and res.verdict == "green" and res.merge_state != "merged":
            res.exit_code = EXIT_NOT_MERGED
        return self._finish(res, started, tag)

    def run_branch(self, branch, *, base="master", legs_spec=None) -> PremergeResult:
        """Test the merge of a local *branch* into *base*: no statuses, no merge."""
        started = self._clock()
        argv = ["hive", "ci", GATE, "--branch", str(branch), "--base", str(base)]
        if legs_spec:
            argv += ["--legs", str(legs_spec)]
        res = PremergeResult(branch=str(branch), base_ref=str(base), mode="offline",
                             argv=argv)
        try:
            chosen = legs_mod.select(legs_spec)
        except ValueError as exc:
            res.reason = str(exc)
            return self._finish(res, started, None)
        tag = self._tag("branch")
        try:
            base_sha, error = self._fetch_base(tag, base)
            if error:
                res.notes.append(f"{error}; tested against the local {base}")
                base_sha = self._rev(base)
            res.base_sha, res.head_sha = base_sha, self._rev(branch)
            if not res.base_sha:
                res.reason = f"no commit for the base {base}"
            elif not res.head_sha:
                res.reason = f"no commit for the branch {branch}"
            else:
                res.rounds = 1
                self._test(res, f"{tag}-r1", chosen, statuses=False)
        except Exception as exc:                        # noqa: BLE001
            res.verdict, res.exit_code = "error", 2
            res.reason = f"premerge broke: {type(exc).__name__}: {exc}"
        finally:
            self._drop(tag)
        return self._finish(res, started, tag)

    # --- what `watch` and `conflicts` use ---------------------------------------------------

    def fetch_base(self, base_ref="master") -> tuple:
        """``(sha, error)``: *base_ref* as origin has it now."""
        tag = self._tag("watch")
        try:
            return self._fetch_base(tag, base_ref)
        finally:
            self._drop(tag)

    def fetch_heads(self, numbers, base_ref="master") -> tuple:
        """``(base_sha, {pr: head_sha}, error)`` in one fetch. The objects stay
        in the store after the refs are dropped, for `preview.conflict_matrix`."""
        tag = self._tag("conflicts")
        refs = f"{REF_ROOT}/{tag}"
        try:
            specs = [f"+refs/heads/{base_ref}:{refs}/base"]
            specs += [f"+refs/pull/{int(n)}/head:{refs}/pr{int(n)}" for n in numbers]
            error = self._fetch(specs)
            if error:
                return "", {}, error
            heads = {int(n): self._rev(f"{refs}/pr{int(n)}") for n in numbers}
            base = self._rev(f"{refs}/base")
            missing = [f"#{n}" for n, sha in heads.items() if not sha]
            if not base or missing:
                return base, heads, ("git fetch: nothing resolves for "
                                     + ", ".join(([base_ref] if not base else []) + missing))
            return base, heads, ""
        finally:
            self._drop(tag)

    # --- one round ----------------------------------------------------------------------------

    def _round(self, res, tag, chosen, number) -> None:
        res.base_sha = res.head_sha = res.tested_sha = res.tree = ""
        res.conflicts, res.up_to_date, res.legs, res.unavailable = (), False, [], []
        res.verdict, res.exit_code, res.reason = "error", 2, ""
        refs = f"{REF_ROOT}/{tag}"
        error = self._fetch([f"+refs/heads/{res.base_ref}:{refs}/base",
                             f"+refs/pull/{number}/head:{refs}/head"])
        if not error:
            res.base_sha, res.head_sha = self._rev(f"{refs}/base"), self._rev(f"{refs}/head")
            if not (res.base_sha and res.head_sha):
                error = f"git fetch {REMOTE}: the fetched base or head does not resolve"
        if error:
            res.reason = error
            return
        self._test(res, f"{tag}-r{res.rounds}", chosen, statuses=self.post_statuses)

    def _test(self, res, run_tag, chosen, *, statuses) -> None:
        pv = self._preview(res.base_sha, res.head_sha)
        res.tree, res.conflicts = pv.tree, tuple(pv.conflicts)
        res.tested_sha, res.up_to_date = pv.tested_sha, pv.up_to_date
        runnable = []
        if pv.clean:
            for leg in chosen:
                avail = self.probe.check(leg)
                if avail.ok:
                    runnable.append((leg, avail))
                else:
                    res.unavailable.append({"name": leg.name, "reason": avail.reason})
        post = statuses and bool(res.head_sha)
        if runnable:
            self._say(f"testing {_short(pv.tested_sha)} on {len(runnable)} leg(s)")
            if post:
                self._status(res, "pending", gh_mod.CONTEXT,
                             f"testing the merge into {res.base_ref} on "
                             f"{len(runnable)} leg(s)")
                for leg, _ in runnable:
                    self._status(res, "pending", f"{gh_mod.CONTEXT}/{leg.name}", "queued")
        res.legs = self._run_legs(run_tag, runnable, pv.tested_sha, res, post)
        res.verdict, res.exit_code, res.reason = decide(pv, res.legs, res.unavailable)
        if post:
            self._status(res, OVERALL_STATE[res.verdict], gh_mod.CONTEXT, res.reason)

    def _runner_for(self, run_tag):
        if self._leg_runner is not None:
            return self._leg_runner(run_tag)
        return legrun.LegRunner(self.repo_root, self.work_root / run_tag, run_id=run_tag,
                                run=self.spawn, environ=self.environ,
                                project=self.project, timeout_s=self.timeout_s,
                                wsl_prefix=self.probe.wsl_prefix(),
                                wsl_env=self.probe.wsl_env(), keep=self.keep)

    def _run_legs(self, run_tag, runnable, commit, res, post) -> list:
        """Run *runnable* a few at a time. Statuses and progress are posted
        from this thread as each leg finishes, never from a worker."""
        if not runnable:
            return []
        runner = self._runner_for(run_tag)
        results = []
        try:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(self.jobs, len(runnable))) as pool:
                futures = {pool.submit(runner.run, leg, avail, commit): leg
                           for leg, avail in runnable}
                for future in concurrent.futures.as_completed(futures):
                    leg = futures[future]
                    try:
                        out = future.result()
                    except Exception as exc:            # noqa: BLE001
                        out = legrun.LegResult(name=leg.name, job=leg.job,
                                               ci_name=leg.ci_name, tier=leg.tier,
                                               state="error", step="runner",
                                               reason=f"{type(exc).__name__}: {exc}")
                    results.append(out)
                    self._say(f"{out.name}: {out.state} -- {out.summary()}")
                    if post:
                        self._status(res, out.state, f"{gh_mod.CONTEXT}/{out.name}",
                                     out.summary())
        finally:
            if not self.keep:
                _rmtree_force(self.work_root / run_tag)
        order = {leg.name: i for i, leg in enumerate(legs_mod.CATALOG)}
        return sorted(results, key=lambda r: order.get(r.name, len(order)))

    # --- the guarded merge --------------------------------------------------------------------

    def _guarded_merge(self, res, tag, number, allow_base_drift) -> str:
        """Merge a green PR, or say why not: ``"merged"``, ``"refused"``,
        ``"failed"``, or ``"retest"`` when the base moved to a different merge."""
        try:
            pr = self.github.pr(number)
        except gh_mod.GitHubError as exc:
            res.merge_state = f"refused: could not read the PR again: {exc}"
            return "refused"
        why = _merge_refusal(pr, res.head_sha)
        if why:
            res.merge_state = f"refused: {why}"
            return "refused"
        base_now, error = self._fetch_base(tag, res.base_ref)
        if error:
            res.merge_state = f"refused: {error}"
            return "refused"
        if base_now != res.base_sha:
            moved = (f"the base moved to {_short(base_now)} after "
                     f"{_short(res.base_sha)} was tested")
            pv = self._preview(base_now, res.head_sha)
            if pv.error:
                res.merge_state = f"refused: {moved}, and the new merge failed: {pv.error}"
                return "refused"
            if pv.conflicts or pv.tree != res.tree:
                if pv.conflicts or not allow_base_drift:
                    res.notes.append(f"{moved} and the merge changed; testing again")
                    return "retest"
                res.notes.append(f"{moved}; --allow-base-drift merged a tree this run "
                                 f"did not test")
            else:
                res.notes.append(f"{moved}; the merge tree is the one tested")
        rc, text = self.github.merge(number, res.head_sha)
        try:
            state, oid = self.github.merged(number)
        except gh_mod.GitHubError as exc:
            state, oid = "", ""
            res.notes.append(f"could not read the PR back after the merge: {exc}")
        if state != "MERGED":
            res.merge_state = f"failed: gh pr merge exit {rc}: {gh_mod.clip(text, 300)}"
            return "failed"
        res.merge_state, res.merge_commit = "merged", oid
        res.landed_tree_matches = self._landed_tree_matches(tag, res, oid)
        if res.landed_tree_matches is False:
            res.notes.append(f"the merge commit {_short(oid)} carries a tree other than "
                             f"the tested {_short(res.tree)}")
        return "merged"

    def _landed_tree_matches(self, tag, res, oid):
        """True or False once the landed commit is here to read; None if not."""
        if not _OID.match(oid or ""):
            return None
        _, error = self._fetch_base(tag, res.base_ref)
        if error:
            return None
        rc, out, _ = self._git(["rev-parse", "--verify", "--quiet", f"{oid}^{{tree}}"])
        tree = (out or "").strip()
        return (tree == res.tree) if rc == 0 and tree else None

    # --- git ---------------------------------------------------------------------------------

    def _git(self, args, timeout_s=GIT_TIMEOUT_S, stdin=None) -> tuple:
        return self.spawn([GIT, *args], cwd=self.repo_root, timeout_s=timeout_s,
                          stdin=stdin)

    def _fetch(self, specs) -> str:
        rc, out, err = self._git(["fetch", "--no-tags", "--quiet", REMOTE, *specs],
                                 timeout_s=FETCH_TIMEOUT_S)
        return "" if rc == 0 else f"git fetch {REMOTE}: {_why(rc, out, err)}"

    def _fetch_base(self, tag, base_ref) -> tuple:
        ref = f"{REF_ROOT}/{tag}/base"
        error = self._fetch([f"+refs/heads/{base_ref}:{ref}"])
        if error:
            return "", error
        sha = self._rev(ref)
        return (sha, "") if sha else ("", f"git fetch {REMOTE}: {ref} does not resolve")

    def _rev(self, name) -> str:
        rc, out, _ = self._git(["rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"])
        sha = (out or "").strip()
        return sha if rc == 0 and _OID.match(sha) else ""

    def _drop(self, tag) -> None:
        """Delete every ref under this run's tag. Best effort, never raises."""
        try:
            rc, out, _ = self._git(["for-each-ref", "--format=%(refname)",
                                    f"{REF_ROOT}/{tag}/"])
            doomed = [line.strip() for line in (out or "").splitlines() if line.strip()]
            if rc == 0 and doomed:
                self._git(["update-ref", "--stdin"],
                          stdin="".join(f"delete {ref}\n" for ref in doomed))
        except Exception:                               # noqa: BLE001
            pass

    def _preview(self, base, head):
        if self._previewer is not None:
            return self._previewer(base, head)
        return preview_mod.merge_preview(self.repo_root, base, head, run=self.spawn,
                                         environ=self.environ)

    def _status(self, res, state, context, description) -> None:
        why = self.github.post_status(res.head_sha, state, context, description)
        if why:
            res.status_errors.append(why)

    def _tag(self, prefix) -> str:
        return f"{prefix}-{_NOT_STAMP.sub('', self._now_iso())}-{os.getpid()}"

    # --- the record ---------------------------------------------------------------------------

    def _finish(self, res, started, tag) -> PremergeResult:
        """Report, and -- for a run that reached git -- record and remember."""
        res.duration_s = max(0.0, self._clock() - started)
        res.reason = res.reason or "the check could not be made"
        res.report_text = render(res)
        if tag:
            self._record(res, tag)
            self._remember(res)
            res.report_text = render(res)
        return res

    def _record(self, res, tag) -> None:
        """One `verification` event. Never raises: a ledger that cannot be
        written must not turn a verdict into a crash; the report says so."""
        try:
            artifact = runlog.write_once(f"premerge-{tag}", res.report_text,
                                         project=self.project) or ""
        except Exception:                               # noqa: BLE001
            artifact = ""
        subject = f"PR #{res.pr_id}" if res.pr_id else f"branch {res.branch}"
        payload = {
            "text": (f"hive ci premerge: {subject} {res.verdict.upper()} "
                     f"(exit {int(res.exit_code)}): {res.reason}"),
            "argv": list(res.argv),
            "exit_code": int(res.exit_code),
            "counts": sum_counts(res.legs),
            "excerpt": res.report_text[:runlog.EXCERPT_CHARS],
            "verified_by": verify.VERIFIED_BY,
            "scope": SCOPE, "gate": GATE, "mode": res.mode,
            "pr_id": res.pr_id, "branch": res.branch, "base_ref": res.base_ref,
            "base_sha": res.base_sha, "commit_sha": res.head_sha,
            "tested_sha": res.tested_sha, "tree": res.tree,
            "verdict": res.verdict, "reason": res.reason,
            "conflicts": list(res.conflicts),
            "legs": [leg.as_dict() for leg in res.legs],
            "unavailable": [dict(u) for u in res.unavailable],
            "merge_state": res.merge_state or "skipped",
            "merge_commit": res.merge_commit,
            "landed_tree_matches": res.landed_tree_matches,
            "rounds": res.rounds, "duration_s": round(res.duration_s, 3),
        }
        if artifact:
            payload["artifact"] = artifact
        try:
            verify.validate_stored(payload)
            events.append_event("verification", payload,
                                tags=["verify", "auto", SCOPE, GATE],
                                project=self.project)
        except Exception as exc:                        # noqa: BLE001
            res.notes.append(f"the run was not recorded: {type(exc).__name__}: {exc}")

    def _remember(self, res) -> None:
        if not res.head_sha:
            return
        entry = {"head_sha": res.head_sha, "base_ref": res.base_ref,
                 "base_sha": res.base_sha, "tree": res.tree,
                 "tested_sha": res.tested_sha, "verdict": res.verdict,
                 "exit_code": int(res.exit_code), "ts": self._now_iso(),
                 "conflicts": list(res.conflicts),
                 "legs": {leg.name: leg.state for leg in res.legs}}
        ok = (remember(self.project, res.pr_id, entry) if res.pr_id
              else remember(self.project, res.branch, entry, bucket="branches"))
        if not ok:
            res.notes.append(f"{SIDECAR} could not be written; `hive ci watch` will "
                             f"test this head again")
```

- [ ] **Step 4: run the tests and watch them pass**

Run: `pytest tests/test_ci_premerge.py -q`
Expected: all pass.

- [ ] **Step 5: commit**

```bash
git add src/mindhive/ci/premerge.py tests/test_ci_premerge.py
git commit -m "feat(ci): premerge, the merge a PR would make tested on every leg, then a guarded merge"
```

---

### Task 7: `watch` — keep every open PR's verdict current, never merge

**Files:**
- Create: `src/mindhive/ci/watch.py`
- Create: `tests/test_ci_watch.py`

- [ ] **Step 1: write the failing test**

**File: `tests/test_ci_watch.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.watch` -- one pass over the open PRs, and the loop around it.

`Premerge` and gh are fakes; the sidecar is the real one in the test's own
store, because what `watch` decides is a function of what `premerge` last
remembered."""
import pytest

from mindhive.ci import github as gh_mod
from mindhive.ci import premerge, watch

BASE, HEAD, NEWBASE, NEWERBASE = "1" * 40, "2" * 40, "3" * 40, "6" * 40
TREE, TREE2 = "a" * 40, "b" * 40


def pr(number=151, head=HEAD, draft=False, base="master"):
    return {"number": number, "state": "OPEN", "isDraft": draft,
            "headRefName": f"claude/f{number}", "headRefOid": head, "baseRefName": base}


class FakeGitHub:
    def __init__(self, prs=None, fail=None):
        self.prs = list(prs if prs is not None else [pr()])
        self.fail = list(fail or [])
        self.statuses = []

    def open_prs(self):
        if self.fail:
            raise self.fail.pop(0)
        return [dict(p) for p in self.prs]

    def post_status(self, sha, state, context="mindhive-ci", description=""):
        self.statuses.append((sha, state, context, description))
        return ""


class FakePremerge:
    def __init__(self, gh, base=BASE, fetch_error="", verdict="green"):
        self.github, self.project, self.post_statuses = gh, "test", True
        self.base, self.fetch_error, self.verdict = base, fetch_error, verdict
        self.runs, self.fetches = [], []

    def run_pr(self, number, *, legs_spec=None, merge=False, allow_base_drift=False):
        self.runs.append((number, legs_spec, merge))
        return premerge.PremergeResult(pr_id=number, verdict=self.verdict,
                                       exit_code={"green": 0, "red": 1}.get(self.verdict, 2),
                                       reason=f"{self.verdict}: 9 leg(s)")

    def fetch_base(self, base_ref="master"):
        self.fetches.append(base_ref)
        return ("", self.fetch_error) if self.fetch_error else (self.base, "")


class MergeTree:
    def __init__(self, answer=(TREE, (), "")):
        self.answer, self.asked = answer, []

    def __call__(self, a, b):
        self.asked.append((a, b))
        return self.answer


def tested(verdict="green", conflicts=(), **extra):
    entry = {"head_sha": HEAD, "base_ref": "master", "base_sha": BASE, "tree": TREE,
             "tested_sha": "c" * 40, "verdict": verdict, "exit_code": 0,
             "conflicts": list(conflicts), "legs": {"windows-py3.12": "success"}}
    entry.update(extra)
    premerge.remember("test", 151, entry)
    return entry


def watcher(pm, merge_tree=None, **kw):
    said = []
    w = watch.Watcher(pm, say=said.append, merge_tree=merge_tree or MergeTree(), **kw)
    return w, said


def test_a_head_it_never_tested_gets_a_full_run_and_never_a_merge():
    pm = FakePremerge(FakeGitHub())
    w, said = watcher(pm, legs_spec="windows-*")
    assert w.once() == [{"pr": 151, "action": "tested", "verdict": "green",
                         "exit_code": 0, "detail": "green: 9 leg(s)"}]
    assert pm.runs == [(151, "windows-*", False)]
    assert said == ["PR #151: tested -- GREEN: green: 9 leg(s)"]


def test_a_new_head_on_a_known_pr_is_tested_again():
    tested()
    pm = FakePremerge(FakeGitHub([pr(head="7" * 40)]))
    w, _ = watcher(pm)
    assert w.once()[0]["action"] == "tested" and pm.runs == [(151, None, False)]


def test_the_same_head_on_the_same_base_is_left_alone():
    tested()
    pm = FakePremerge(FakeGitHub())
    tree = MergeTree()
    w, said = watcher(pm, merge_tree=tree)
    assert w.once()[0]["action"] == "unchanged"
    assert pm.runs == [] and tree.asked == []
    assert said == ["PR #151: unchanged since its last test (green)"]


def test_a_base_that_moved_but_still_merges_keeps_its_verdict():
    tested()
    pm = FakePremerge(FakeGitHub(), base=NEWBASE)
    tree = MergeTree((TREE2, (), ""))
    w, _ = watcher(pm, merge_tree=tree)
    action = w.once()[0]
    assert (action["action"], action["verdict"]) == ("base-moved", "green")
    assert tree.asked == [(NEWBASE, HEAD)] and pm.runs == []
    kept = premerge.load_sidecar("test")["prs"]["151"]
    assert (kept["checked_base_sha"], kept["checked_tree"]) == (NEWBASE, TREE2)
    assert (kept["base_sha"], kept["tree"]) == (BASE, TREE)        # what was TESTED
    assert w.once()[0]["action"] == "unchanged" and len(tree.asked) == 1


def test_a_base_that_moved_into_a_conflict_turns_the_pr_red_at_once():
    tested()
    gh = FakeGitHub()
    pm = FakePremerge(gh, base=NEWBASE)
    w, said = watcher(pm, merge_tree=MergeTree((TREE2, ("src/a.py",), "")))
    action = w.once()[0]
    assert (action["action"], action["verdict"]) == ("conflict", "red")
    assert gh.statuses == [(HEAD, "failure", "mindhive-ci",
                            "conflicts with master at 333333333333: src/a.py")]
    kept = premerge.load_sidecar("test")["prs"]["151"]
    assert (kept["verdict"], kept["conflicts"], kept["checked_base_sha"]) == (
        "red", ["src/a.py"], NEWBASE)
    assert pm.runs == []
    assert said == ["PR #151: RED -- conflicts with master at 333333333333: src/a.py"]


def test_no_status_is_posted_for_a_conflict_when_statuses_are_off():
    tested()
    gh = FakeGitHub()
    pm = FakePremerge(gh, base=NEWBASE)
    pm.post_statuses = False
    w, _ = watcher(pm, merge_tree=MergeTree((TREE2, ("src/a.py",), "")))
    assert w.once()[0]["action"] == "conflict" and gh.statuses == []


def test_a_conflict_that_clears_gets_the_full_run_it_never_had():
    tested(verdict="red", conflicts=["src/a.py"], checked_base_sha=NEWBASE)
    pm = FakePremerge(FakeGitHub(), base=NEWERBASE)
    w, _ = watcher(pm, merge_tree=MergeTree((TREE2, (), "")))
    assert w.once()[0]["action"] == "tested" and pm.runs == [(151, None, False)]


def test_an_error_verdict_is_tried_again_when_the_base_moves_and_only_then():
    tested(verdict="error")
    pm = FakePremerge(FakeGitHub())
    w, _ = watcher(pm)
    assert w.once()[0]["action"] == "unchanged" and pm.runs == []
    pm.base = NEWBASE
    assert w.once()[0]["action"] == "tested" and pm.runs == [(151, None, False)]


def test_a_merge_tree_that_cannot_answer_gets_a_full_run():
    tested()
    pm = FakePremerge(FakeGitHub(), base=NEWBASE)
    w, _ = watcher(pm, merge_tree=MergeTree(("", (), "fatal: bad object")))
    assert w.once()[0]["action"] == "tested"


def test_a_base_that_cannot_be_fetched_is_an_error_not_a_verdict():
    tested()
    pm = FakePremerge(FakeGitHub(), fetch_error="git fetch origin: Could not resolve host")
    w, said = watcher(pm)
    assert w.once() == [{"pr": 151, "action": "error",
                         "detail": "git fetch origin: Could not resolve host"}]
    assert said == ["PR #151: could not check -- git fetch origin: Could not resolve host"]


@pytest.mark.parametrize("skip,expected", [(True, ["skipped", "tested"]),
                                           (False, ["tested", "tested"])])
def test_drafts_are_skipped_only_when_asked(skip, expected):
    pm = FakePremerge(FakeGitHub([pr(149, head="8" * 40, draft=True), pr(151)]))
    w, _ = watcher(pm, skip_drafts=skip)
    assert [a["action"] for a in w.once()] == expected


def test_gh_that_cannot_list_the_prs_is_one_error_action():
    pm = FakePremerge(FakeGitHub(fail=[gh_mod.GitHubError("gh pr list: not logged in")]))
    w, _ = watcher(pm)
    assert w.once() == [{"pr": 0, "action": "error", "detail": "gh pr list: not logged in"}]


def test_the_loop_survives_a_failing_pass_and_sleeps_between_passes():
    pm = FakePremerge(FakeGitHub(prs=[], fail=[RuntimeError("the network went away")]))
    slept = []
    w, said = watcher(pm, sleep=slept.append, interval_s=42)
    assert w.loop(passes=2) == 2
    assert said == ["watch: a pass failed: RuntimeError: the network went away"]
    assert slept == [42.0]
```

- [ ] **Step 2: run it and watch it fail**

Run: `pytest tests/test_ci_watch.py -q`
Expected: `ImportError: cannot import name 'watch' from 'mindhive.ci'`.

- [ ] **Step 3: write the module**

**File: `src/mindhive/ci/watch.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/watch.py
"""`hive ci watch` -- keep every open PR's pre-merge verdict current
(spec 2026-09-30).

A pass reads the open PRs and, for each, what `premerge` last tested
(`state/ci_premerge.json`):

* **a head it never tested** -- a new PR, or a push -- is tested in full
  (`Premerge.run_pr`, never with `--merge`);
* **the same head on the same base** is left alone;
* **the same head on a base that moved** is checked with one `git merge-tree`
  -- seconds, not the hour a full run costs. A merge that now conflicts turns
  the PR red at once, with a `mindhive-ci` failure status naming the files. A
  merge that is still clean keeps its verdict -- as a PR's checks survive a
  push to its base on GitHub -- and `premerge --merge` re-checks the tree
  before it merges anyway. A PR that was red for a conflict alone and now
  merges cleanly is tested in full: no leg ever ran on it. A PR whose last
  check could not be made is tried again when the base moves.

It never merges: that is `hive ci premerge N --merge`, asked for by name.
`--once` makes one pass, for a scheduled task. Without it the watch loops,
sleeping `--interval` seconds between passes; a pass that fails is reported
and the loop goes on.
"""
import time

from .. import events
from . import github as gh_mod
from . import premerge as premerge_mod
from . import preview as preview_mod

__all__ = ["DEFAULT_INTERVAL_S", "Watcher", "describe"]

DEFAULT_INTERVAL_S = 300.0


def describe(action) -> str:
    """One line for one action, the way the watch prints it."""
    n = action.get("pr")
    subject = f"PR #{n}" if n else "watch"
    kind, detail = action.get("action"), action.get("detail") or ""
    verdict = str(action.get("verdict") or "")
    if kind == "tested":
        return f"{subject}: tested -- {verdict.upper()}: {detail}"
    if kind == "conflict":
        return f"{subject}: RED -- {detail}"
    if kind == "base-moved":
        return (f"{subject}: the base moved and it still merges cleanly; "
                f"the {verdict} verdict stands")
    if kind == "unchanged":
        return f"{subject}: unchanged since its last test ({verdict})"
    if kind == "skipped":
        return f"{subject}: skipped ({detail})"
    return f"{subject}: could not check -- {detail}"


class Watcher:
    """Passes over the open PRs through one `premerge.Premerge`."""

    def __init__(self, premerge, *, github=None, project=None, legs_spec=None,
                 skip_drafts=False, interval_s=DEFAULT_INTERVAL_S, sleep=time.sleep,
                 say=None, merge_tree=None):
        self.premerge = premerge
        self.github = github if github is not None else premerge.github
        self.project = premerge.project if project is None else project
        self.legs_spec = legs_spec
        self.skip_drafts = bool(skip_drafts)
        self.interval_s = float(interval_s)
        self._sleep = sleep
        self._say = say or (lambda text: None)
        self._merge_tree = merge_tree or (
            lambda a, b: preview_mod.merge_tree(premerge.repo_root, a, b,
                                                run=premerge.spawn))

    def once(self) -> list:
        """One pass. Returns what it did, one action per open PR."""
        try:
            prs = self.github.open_prs()
        except gh_mod.GitHubError as exc:
            action = {"pr": 0, "action": "error", "detail": str(exc)}
            self._say(describe(action))
            return [action]
        known = premerge_mod.load_sidecar(self.project)["prs"]
        actions = []
        for pr in prs:
            n = int(pr.get("number") or 0)
            if self.skip_drafts and pr.get("isDraft"):
                action = {"pr": n, "action": "skipped", "detail": "a draft"}
            else:
                last = known.get(str(n)) or {}
                same = (str(pr.get("headRefOid") or "") == last.get("head_sha")
                        and str(pr.get("baseRefName") or "") == last.get("base_ref"))
                action = self._recheck(n, pr, last) if same else self._test(n)
            self._say(describe(action))
            actions.append(action)
        return actions

    def loop(self, passes=None) -> int:
        """Pass after pass, *passes* times or for ever. Returns the passes made."""
        done = 0
        while passes is None or done < passes:
            try:
                self.once()
            except Exception as exc:                    # noqa: BLE001
                self._say(f"watch: a pass failed: {type(exc).__name__}: {exc}")
            done += 1
            if passes is None or done < passes:
                self._sleep(self.interval_s)
        return done

    def _test(self, n) -> dict:
        res = self.premerge.run_pr(n, legs_spec=self.legs_spec)
        return {"pr": n, "action": "tested", "verdict": res.verdict,
                "exit_code": res.exit_code, "detail": res.reason}

    def _recheck(self, n, pr, last) -> dict:
        base_ref = str(pr.get("baseRefName") or "")
        base_now, error = self.premerge.fetch_base(base_ref)
        if error:
            return {"pr": n, "action": "error", "detail": error}
        if base_now == (last.get("checked_base_sha") or last.get("base_sha")):
            return {"pr": n, "action": "unchanged", "verdict": last.get("verdict"),
                    "detail": ""}
        if last.get("verdict") == "error":
            return self._test(n)
        head = str(last.get("head_sha") or "")
        tree, conflicts, error = self._merge_tree(base_now, head)
        if error:
            return self._test(n)
        if conflicts:
            return self._conflict(n, base_ref, base_now, head, conflicts, last)
        if last.get("verdict") == "red" and last.get("conflicts"):
            return self._test(n)
        premerge_mod.remember(self.project, n, {**last, "checked_base_sha": base_now,
                                                "checked_tree": tree})
        return {"pr": n, "action": "base-moved", "verdict": last.get("verdict"),
                "detail": f"{base_ref} at {base_now[:12]}"}

    def _conflict(self, n, base_ref, base_now, head, conflicts, last) -> dict:
        shown = ", ".join(conflicts[:5])
        if len(conflicts) > 5:
            shown += f" and {len(conflicts) - 5} more"
        detail = f"conflicts with {base_ref} at {base_now[:12]}: {shown}"
        if self.premerge.post_statuses:
            self.github.post_status(head, "failure", gh_mod.CONTEXT, detail)
        premerge_mod.remember(self.project, n, {
            **last, "verdict": "red", "exit_code": 1, "conflicts": list(conflicts),
            "checked_base_sha": base_now, "ts": events.now_iso()})
        return {"pr": n, "action": "conflict", "verdict": "red", "detail": detail}
```

- [ ] **Step 4: run the tests and watch them pass**

Run: `pytest tests/test_ci_watch.py -q`
Expected: all pass.

- [ ] **Step 5: commit**

```bash
git add src/mindhive/ci/watch.py tests/test_ci_watch.py
git commit -m "feat(ci): watch, re-test a PR when its head moves and re-check it when its base does"
```

---

### Task 8: the four verbs, wired into `hive ci`

**Files:**
- Create: `src/mindhive/ci/premerge_cli.py`
- Create: `tests/test_ci_premerge_cli.py`
- Modify: `src/mindhive/ci/cli.py` (`register`, `_print_ci`, `cmd_ci_status`)
- Modify: `tests/test_commands_reexport.py` (`SURFACE['ci']`, `LEAF_HANDLERS`)

- [ ] **Step 1: write the failing test**

**File: `tests/test_ci_premerge_cli.py`** (complete)

```python
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
])
def test_premerge_refuses_a_request_it_cannot_honour(monkeypatch, capsys, argv, why):
    rec = Recorder()
    monkeypatch.setattr(premerge, "Premerge", rec)
    assert run("premerge", *argv) == 2
    assert why in capsys.readouterr().err and rec.made == []


def test_premerge_exits_with_the_runs_code_and_prints_its_report(monkeypatch, capsys, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(premerge, "Premerge", rec)
    code = run("premerge", "151", "--legs", "windows-*", "--merge", "--jobs", "2",
               "--timeout", "60", "--keep", "--root", str(tmp_path), "--project", "test")
    assert code == 3 and capsys.readouterr().out.strip() == "THE REPORT"
    root, kw = rec.made[0]
    assert str(root) == str(tmp_path)
    assert (kw["project"], kw["jobs"], kw["timeout_s"], kw["post_statuses"], kw["keep"]) == (
        "test", 2, 60.0, True, True)
    assert rec.calls == [("pr", 151, {"legs_spec": "windows-*", "merge": True,
                                      "allow_base_drift": False})]


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
```

- [ ] **Step 2: run it and watch it fail**

Run: `pytest tests/test_ci_premerge_cli.py -q`
Expected: `ImportError: cannot import name 'premerge_cli' from 'mindhive.ci'`.

- [ ] **Step 3: write the verbs**

**File: `src/mindhive/ci/premerge_cli.py`** (complete)

```python
# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/premerge_cli.py
"""The pre-merge verbs of `hive ci` (spec 2026-09-30): `premerge`, `watch`,
`conflicts` and `legs`.

Registered by `ci.cli.register`, beside the verbs `hive ci` already had. The
modules the verbs run are imported inside each handler, so `hive --help`
pays for argparse and nothing else -- the `ci.cli` convention.

Exit codes are for scripts and scheduled tasks:

* `premerge`: 0 green; 1 red; 2 the check could not be made; 3 green, but
  `--merge` did not merge.
* `watch --once`: 0 every PR it looked at is green; 1 one is red; 2 one
  could not be checked.
* `conflicts`: 0 none; 1 a PR conflicts with its base or with another PR; 2
  the check could not be made.
* `legs`: 0 at least one full-suite leg can run here; 2 none can.
"""
import json
import sys

from .. import cliparse

__all__ = ["cmd_ci_conflicts", "cmd_ci_legs", "cmd_ci_premerge", "cmd_ci_watch",
           "register"]

#: `premerge.DEFAULT_JOBS`, `legrun.LEG_TIMEOUT_S` and `watch.DEFAULT_INTERVAL_S`,
#: spelled here so registering imports nothing; a test holds them equal.
JOBS, TIMEOUT_S, INTERVAL_S = 3, 5400.0, 300.0


def _progress(text) -> None:
    print(text, file=sys.stderr, flush=True)


def _premerge(args, *, statuses):
    from . import premerge
    from .cli import _root_of
    return premerge.Premerge(_root_of(args), project=getattr(args, "project", None),
                             jobs=args.jobs, timeout_s=args.timeout,
                             post_statuses=statuses and not args.no_status,
                             keep=args.keep, work_root=args.work_root, say=_progress)


def cmd_ci_premerge(args) -> int:
    """Test one PR (or a local branch) the way GitHub CI would."""
    branch = (args.branch or "").strip()
    refusal = ""
    if bool(args.pr) == bool(branch):
        refusal = "give exactly one of a PR number or --branch"
    elif branch and args.merge:
        refusal = "--merge needs a PR number: a local branch is never merged"
    elif args.allow_base_drift and not args.merge:
        refusal = "--allow-base-drift only means something with --merge"
    if refusal:
        print(f"hive ci premerge: {refusal}", file=sys.stderr)
        return 2
    runner = _premerge(args, statuses=bool(args.pr))
    if args.pr:
        res = runner.run_pr(args.pr, legs_spec=args.legs, merge=args.merge,
                            allow_base_drift=args.allow_base_drift)
    else:
        res = runner.run_branch(branch, base=args.base, legs_spec=args.legs)
    if args.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(res.report_text)
    return int(res.exit_code)


def cmd_ci_watch(args) -> int:
    """Keep every open PR's verdict current. Never merges."""
    from . import watch
    watcher = watch.Watcher(_premerge(args, statuses=True), legs_spec=args.legs,
                            skip_drafts=args.skip_drafts, interval_s=args.interval,
                            say=_progress)
    if not args.once:
        try:
            watcher.loop()
        except KeyboardInterrupt:
            pass
        return 0
    actions = watcher.once()
    if args.json:
        print(json.dumps(actions, ensure_ascii=False, sort_keys=True))
    verdicts = {a.get("verdict") for a in actions}
    if "red" in verdicts:
        return 1
    if "error" in verdicts or any(a.get("action") == "error" for a in actions):
        return 2
    return 0


def cmd_ci_conflicts(args) -> int:
    """Which open PRs into --base conflict with it, and with each other."""
    from . import github as gh_mod
    from . import premerge, preview
    from .cli import _root_of
    root = _root_of(args)
    runner = premerge.Premerge(root, post_statuses=False)
    try:
        prs = [p for p in runner.github.open_prs()
               if str(p.get("baseRefName") or "") == args.base]
    except gh_mod.GitHubError as exc:
        print(f"hive ci conflicts: {exc}", file=sys.stderr)
        return 2
    numbers = sorted(int(p.get("number") or 0) for p in prs)
    base_sha, matrix = "", {"base": {}, "pairs": [], "errors": {}}
    if numbers:
        base_sha, heads, error = runner.fetch_heads(numbers, args.base)
        if error:
            print(f"hive ci conflicts: {error}", file=sys.stderr)
            return 2
        matrix = preview.conflict_matrix(root, base_sha, heads, run=runner.spawn)
    if args.json:
        print(json.dumps({"base": args.base, "base_sha": base_sha, "prs": numbers,
                          "against_base": {str(k): v for k, v in matrix["base"].items()},
                          "pairs": matrix["pairs"], "errors": matrix["errors"]},
                         ensure_ascii=False, sort_keys=True))
    else:
        _print_matrix(args.base, base_sha, numbers, matrix)
    if matrix["errors"]:
        return 2
    return 1 if any(matrix["base"].values()) or matrix["pairs"] else 0


def _print_matrix(base, base_sha, numbers, matrix) -> None:
    if not numbers:
        print(f"no open PR targets {base}")
        return
    print(f"open PRs into {base} at {base_sha[:12]}: "
          + " ".join(f"#{n}" for n in numbers))
    print(f"against {base}:")
    for n in numbers:
        if n in matrix["base"]:
            found = matrix["base"][n]
            print(f"  #{n}  " + ("CONFLICTS: " + ", ".join(found) if found else "clean"))
    if matrix["pairs"]:
        print("pairs that cannot both land as they stand:")
        for pair in matrix["pairs"]:
            print(f"  #{pair['a']} + #{pair['b']}: " + ", ".join(pair["paths"]))
    else:
        print("pairs: none conflict")
    for key, why in sorted(matrix["errors"].items()):
        print(f"  could not check {key}: {why}")


def cmd_ci_legs(args) -> int:
    """The ci.yml legs, and whether this machine can run each."""
    from . import legs
    try:
        chosen = legs.select(args.legs)
    except ValueError as exc:
        print(f"hive ci legs: {exc}", file=sys.stderr)
        return 2
    probe = legs.Probe()
    rows = []
    for leg in chosen:
        avail = probe.check(leg)
        rows.append({"name": leg.name, "job": leg.ci_name, "tier": leg.tier,
                     "ok": avail.ok, "via": avail.via, "python": avail.python,
                     "reason": avail.reason, "note": leg.note})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
    else:
        width = max(len(r["name"]) for r in rows)
        for r in rows:
            what = (f"ready ({r['via']}: {r['python']})" if r["ok"]
                    else f"NOT RUNNABLE: {r['reason']}")
            print(f"{r['name']:<{width}}  {what}")
    return 0 if any(r["ok"] and r["tier"] == legs.TIER_DEFAULT for r in rows) else 2


def _common(p) -> None:
    p.add_argument("--legs", default=None,
                   help="comma-separated leg names or globs (default: every leg; "
                        "see `hive ci legs`)")
    p.add_argument("--jobs", type=int, default=JOBS,
                   help=f"legs run at once (default {JOBS})")
    p.add_argument("--timeout", type=float, default=TIMEOUT_S,
                   help=f"seconds each leg's pytest may run (default {TIMEOUT_S:.0f})")
    p.add_argument("--no-status", action="store_true",
                   help="post no commit status to GitHub")
    p.add_argument("--keep", action="store_true",
                   help="keep each leg's directory for inspection")
    p.add_argument("--work-root", default=None,
                   help="where the legs run (default <repo>/.worktrees/premerge)")
    p.add_argument("--root", default=None,
                   help="the repository (default: the one this runs in)")
    p.add_argument("--project", type=cliparse.project_arg, default=None,
                   help="the MindHive namespace the run is recorded in")


def register(group) -> None:
    """Add the four verbs to the `hive ci` subparser set *group*."""
    pm = group.add_parser(
        "premerge", help="test a PR the way GitHub CI did -- every ci.yml leg, on "
                         "this machine -- and post the verdict as commit statuses")
    pm.add_argument("pr", nargs="?", type=int, default=None, help="the PR number")
    pm.add_argument("--branch", default=None,
                    help="test a local branch instead: no statuses, never merged")
    pm.add_argument("--base", default="master",
                    help="the base a --branch merges into (default master)")
    _common(pm)
    pm.add_argument("--merge", action="store_true",
                    help="when green, merge: a merge commit, the tested head pinned")
    pm.add_argument("--allow-base-drift", action="store_true",
                    help="with --merge: merge even when the base moved to a merge "
                         "tree this run did not test")
    pm.add_argument("--json", action="store_true", help="print the result as JSON")
    pm.set_defaults(func=cmd_ci_premerge)

    w = group.add_parser(
        "watch", help="test each open PR whose head moved, re-check each whose "
                      "base moved; never merges")
    _common(w)
    w.add_argument("--once", action="store_true",
                   help="one pass, then exit (for a scheduled task)")
    w.add_argument("--interval", type=float, default=INTERVAL_S,
                   help=f"seconds between passes (default {INTERVAL_S:.0f})")
    w.add_argument("--skip-drafts", action="store_true", help="leave draft PRs alone")
    w.add_argument("--json", action="store_true",
                   help="with --once: print what the pass did as JSON")
    w.set_defaults(func=cmd_ci_watch)

    c = group.add_parser(
        "conflicts", help="which open PRs conflict with their base, and with each other")
    c.add_argument("--base", default="master",
                   help="the base branch whose PRs are compared (default master)")
    c.add_argument("--root", default=None,
                   help="the repository (default: the one this runs in)")
    c.add_argument("--json", action="store_true", help="print the matrix as JSON")
    c.set_defaults(func=cmd_ci_conflicts)

    lg = group.add_parser(
        "legs", help="the ci.yml legs, and whether this machine can run each")
    lg.add_argument("--legs", default=None, help="comma-separated names or globs")
    lg.add_argument("--json", action="store_true", help="print the legs as JSON")
    lg.set_defaults(func=cmd_ci_legs)
```

- [ ] **Step 4: wire the verbs into `hive ci`, and make `status` read a pre-merge run**

In `src/mindhive/ci/cli.py`, at the end of `register`, after the `reset.set_defaults(func=cmd_ci_breaker_reset)` line:

```python

    from . import premerge_cli      # lazy: its handlers import what they run
    premerge_cli.register(group)
```

Replace `_print_ci` with (the three new blocks are the verdict line, the legs line, and `_green`):

```python
def _print_ci(row) -> None:
    print(f"PR #{row.get('pr_id', 0)} on {row.get('branch') or '(no branch)'}")
    print(f"{row.get('gate') or 'unknown gate'}: exit {row.get('exit_code')}")
    if row.get("verdict"):
        print(f"verdict: {row['verdict']}"
              + (f" -- {row['reason']}" if row.get("reason") else ""))
    print(f"mode: {row.get('mode') or 'unknown'}, "
          f"merge: {row.get('merge_state') or 'unknown'}")
    counts = row.get("counts") or {}
    if counts:
        print("counts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    legs = [leg for leg in row.get("legs") or [] if isinstance(leg, dict)]
    if legs:
        tally = {}
        for leg in legs:
            state = str(leg.get("state") or "unknown")
            tally[state] = tally.get(state, 0) + 1
        missing = [str(u.get("name")) for u in row.get("unavailable") or []
                   if isinstance(u, dict)]
        print("legs: " + ", ".join(f"{n} {s}" for s, n in sorted(tally.items()))
              + ("; not run: " + ", ".join(missing) if missing else ""))
    if row.get("commit_sha"):
        print(f"commit: {row['commit_sha'][:12]}")
    if row.get("artifact"):
        print(f"artifact: {row['artifact']}")


def _green(row) -> bool:
    """A pre-merge run is judged by its verdict: its exit 3 is a green run whose
    `--merge` did not happen, and calling that red would misreport the tests."""
    if row.get("gate") == "premerge":
        return row.get("verdict") == "green"
    return int(row.get("exit_code") or 0) == 0
```

and in `cmd_ci_status` replace

```python
    return 0 if int(row.get("exit_code") or 0) == 0 else 1
```

with

```python
    return 0 if _green(row) else 1
```

In `tests/test_commands_reexport.py`, `SURFACE['ci']` becomes:

```python
 # The native pre-merge CI (spec 2026-09-30) added `premerge`, `watch`,
 # `conflicts` and `legs`, registered by `ci.premerge_cli`.
 'ci': {'run': {}, 'status': {}, 'hook-install': {},
        'breaker': {'status': {}, 'reset': {}},
        'premerge': {}, 'watch': {}, 'conflicts': {}, 'legs': {}},
```

and `LEAF_HANDLERS` gains, beside the other `ci` rows:

```python
 'ci conflicts': 'cmd_ci_conflicts',
 'ci legs': 'cmd_ci_legs',
 'ci premerge': 'cmd_ci_premerge',
 'ci watch': 'cmd_ci_watch',
```

- [ ] **Step 5: run the tests and watch them pass**

Run: `pytest tests/test_ci_premerge_cli.py tests/test_ci_cli.py tests/test_commands_reexport.py tests/test_cli_arg_wiring.py -q`
Expected: all pass.

- [ ] **Step 6: commit**

```bash
git add src/mindhive/ci/premerge_cli.py src/mindhive/ci/cli.py tests/test_ci_premerge_cli.py tests/test_commands_reexport.py
git commit -m "feat(ci): hive ci premerge, watch, conflicts and legs; status reads a pre-merge verdict"
```

---

### Task 9: documentation

**Files:**
- Modify: `README.md` (the `hive ci` bullets, `## CI`, `## Environment variables`)
- Modify: `docs/session-legend.md` (the `ci/` row)
- Modify: `CHANGELOG.md` (`## [Unreleased]` / `### Added`)
- Modify: `CLAUDE.md` (`## Any change here must land green`)
- Modify: `.github/workflows/ci.yml` (the header comment only)

- [ ] **Step 1: README, the command list**

After the `hive ci breaker reset` bullet, add:

```markdown
- `hive ci premerge <PR> [--merge] [--allow-base-drift] [--legs L] [--jobs N] [--timeout S] [--no-status] [--keep] [--json]` — the **pre-merge CI** GitHub Actions used to be, on this machine: every `ci.yml` leg (the 3 × 2 OS × Python matrix, the node, browser and pg tiers) run against the MERGE the PR would make, each in a fresh repository with its own venv, and the verdict posted on the PR as commit statuses (`mindhive-ci`, `mindhive-ci/<leg>`). Exit 0 green, 1 red, 2 it could not be checked, 3 green but `--merge` did not merge. `--merge` re-reads the PR, re-checks the base and merges with `gh pr merge --merge --match-head-commit <tested head>`; `--branch B [--base M]` tests a local branch instead, with no statuses and no merge
- `hive ci watch [--once] [--interval S] [--skip-drafts] [...]` — keep every open PR's verdict current: a new head is tested in full, a moved base is re-checked with one `git merge-tree` and a new conflict turns the PR red at once. Never merges
- `hive ci conflicts [--base M] [--json]` — which open PRs conflict with their base, and which pairs cannot both land as they stand. Exit 1 when any does
- `hive ci legs [--legs L] [--json]` — the `ci.yml` legs and whether this machine can run each, with the reason when it cannot
```

- [ ] **Step 2: README, `## CI`**

Replace the section's last paragraph (the one beginning "`.github/workflows/ci.yml` is **`workflow_dispatch` + `push: tags: ["v*"]`**") with:

````markdown
`.github/workflows/ci.yml` is **`workflow_dispatch` + `push: tags: ["v*"]`**
only, and while the account's Actions jobs are refused for payment it cannot
run at all. Its jobs run here instead:

### Pre-merge CI on this machine (`hive ci premerge`)

`hive ci premerge <PR>` does what the Actions checkout of `refs/pull/N/merge`
did, without a runner or a bill:

1. fetches the base and `refs/pull/N/head` into refs of its own
   (`refs/mindhive-ci/<run>/…`, deleted afterwards) — nothing checked out is
   read or moved;
2. computes the merge with `git merge-tree --write-tree` (git ≥ 2.38) — a
   conflict is red at once, and no leg runs;
3. runs each `ci.yml` leg in a fresh repository of the merge (one commit, no
   hooks, CRLF for a windows leg and LF for an ubuntu one, as the runners
   check out) with a fresh venv, `pip install -e .[extras]` and the job's own
   pytest command — windows legs through `py -0p`'s interpreters, ubuntu legs
   in WSL (a login shell, so uv's interpreters are found), a few at a time;
4. posts `mindhive-ci/<leg>` and the overall `mindhive-ci` as commit statuses
   on the PR's head — a plain REST write, nothing billed;
5. records one `verification` (`scope: "ci"`, `gate: "premerge"`) with every
   leg's counts, and keeps the report and each leg's log as runlog artifacts.

A leg this machine cannot run is **named, never counted**: the verdict says
`not run here: pg`. `hive ci legs` shows which legs can run and why the others
cannot. Two legs depart from their `ci.yml` job, and say so in `hive ci legs
--json`: the `browser` leg runs the interpreter running `hive` (which must
import playwright) against the leg's own `src`, so no Chromium is downloaded
per run, and the `pg` leg runs only against the throwaway database named by
`MINDHIVE_CI_PG_DSN` — never the operator's `MINDHIVE_PG_DSN_ADMIN`.

`--merge` merges a green PR, guarded: the PR must still be open, not a draft,
and at the tested head; a base that moved is previewed again and a different
merge is tested again (up to three rounds) unless `--allow-base-drift`; the
merge is `gh pr merge N --merge --match-head-commit <head>`, and only GitHub
reading back `MERGED` counts. The landed commit's tree is compared with the
tested tree.

To keep every open PR's verdict current, run the watch — by hand, or from a
scheduled task you register yourself (MIND HIVE never registers one):

```sh
hive ci watch                  # loops; Ctrl+C to stop
hive ci watch --once           # one pass, for a scheduled task
schtasks /Create /TN "MindHive CI watch" /SC MINUTE /MO 30 /TR "<venv>\Scripts\hive.exe ci watch --once --root <repo> --project <namespace>"
```
````

- [ ] **Step 3: README, `## Environment variables`**

Add a row, in alphabetical order (after `MINDHIVE_BRIEF`):

```markdown
| `MINDHIVE_CI_PG_DSN` | libpq DSN; unset | `ci/legs.py`, `ci/legrun.py` | the THROWAWAY Postgres 17 database the `pg` leg of `hive ci premerge` migrates and tests; unset, the pg leg is named as not runnable here. Never set it to the real mirror: a migration from an unmerged branch cannot be taken back |
```

and in the `MINDHIVE_PG_DSN_ADMIN` row, extend "read by" with `, ci/legrun.py (set inside the pg leg to MINDHIVE_CI_PG_DSN's value, never read)`.

- [ ] **Step 4: `docs/session-legend.md`, the `ci/` row**

Change the row's first cell to `` `ci/` (`__init__.py`, `cli.py`, `runner.py`, `proc.py`, `legs.py`, `preview.py`, `github.py`, `legrun.py`, `premerge.py`, `watch.py`, `premerge_cli.py`) `` and append to the row's second cell, before its closing `|`:

```markdown
**The pre-merge CI** (spec 2026-09-30) is the Actions matrix run here, because the account's Actions jobs are refused for payment: `proc.py` is its only door to a child (captured or logged to a file, capped with the tree killed at the cap, the SOP secret and token withheld); `legs.py` is the catalog of `ci.yml`'s jobs, pinned to the YAML by `tests/test_ci_legs.py`, and the probe of what this machine can run; `preview.py` computes the merge a PR would make in the object store; `github.py` holds every `gh` call, the pinned `gh pr merge --merge --match-head-commit` included; `legrun.py` runs one leg in a fresh repository with its own venv and a built, not inherited, environment; `premerge.py` fetches into `refs/mindhive-ci/<run>/`, runs the legs, posts commit statuses, guards the merge and records one `verification` with `gate: "premerge"`; `watch.py` keeps the open PRs' verdicts current and never merges; `premerge_cli.py` registers the four verbs from `cli.register`.
```

- [ ] **Step 5: CHANGELOG**

Under `## [Unreleased]` / `### Added`, first:

```markdown
- **Native pre-merge CI — `hive ci premerge`, `watch`, `conflicts`, `legs`**
  (spec `docs/superpowers/specs/2026-09-30-native-ci-premerge-design.md`):
  the `ci.yml` jobs run on the operator's machine while the account's Actions
  jobs are refused for payment. Each leg tests the merge a PR would make, in a
  fresh repository and venv; the verdict is posted as `mindhive-ci` commit
  statuses and recorded as a `verification` with `gate: "premerge"`;
  `--merge` merges a green PR guarded by a re-read, a re-checked base and a
  pinned head. `hive ci status` judges a pre-merge run by its verdict and
  names its legs. New modules `ci/proc.py`, `ci/legs.py`, `ci/preview.py`,
  `ci/github.py`, `ci/legrun.py`, `ci/premerge.py`, `ci/watch.py`,
  `ci/premerge_cli.py`; new variable `MINDHIVE_CI_PG_DSN`.
```

- [ ] **Step 6: CLAUDE.md, `## Any change here must land green`**

Append to the section:

```markdown
While GitHub Actions is billing-blocked, the CI of record for a PR is
`hive ci premerge <PR>`: it runs `ci.yml`'s legs locally against the PR's
merge and posts the verdict as `mindhive-ci` commit statuses. A PR is green
when `mindhive-ci` is green on its head; a leg it could not run here is named
in the verdict and is not a pass. Merge only on the owner's word, and then
through `hive ci premerge <PR> --merge`, which re-checks before it merges.
```

- [ ] **Step 7: `.github/workflows/ci.yml`, the header comment**

After the header's last comment line ("browser tier actually exist."), add:

```yaml
# While Actions is billing-blocked, `hive ci premerge <PR>` runs these same
# jobs on the operator's machine against the PR's merge and posts the verdict
# as `mindhive-ci` commit statuses; `tests/test_ci_legs.py` pins its leg
# catalog to the jobs below, so a job added here fails that test until it has
# a leg.
```

- [ ] **Step 8: run the doc gates**

Run: `pytest tests/test_env_registry.py tests/test_docs_parity.py tests/test_ci_legs.py -q`
Expected: all pass.

- [ ] **Step 9: commit**

```bash
git add README.md docs/session-legend.md CHANGELOG.md CLAUDE.md .github/workflows/ci.yml
git commit -m "docs(ci): the native pre-merge CI in README, legend, changelog and CLAUDE.md"
```

---

### Task 10: verification, the change-log entry, and the first real run

- [ ] **Step 1: the full suite, detached**

The full suite takes about 25 minutes on this machine, longer than one tool call may block. Write the runner into the scratchpad and start it detached, then wait on its `.done` file:

```bash
SP=<scratchpad>
WT=C:/Users/hanif/mindhive/.claude/worktrees/cli-login-status-detection-cfec62
cat > "$SP/full_suite.cmd" <<EOF
@echo off
cd /d $WT
set PYTHONPATH=$WT/src
set HF_HUB_OFFLINE=1
C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider > "$SP\full_suite.log" 2>&1
echo %ERRORLEVEL% > "$SP\full_suite.done"
EOF
```

Expected tail of `full_suite.log`: `N passed, M skipped, K deselected` with no `failed` and no `error`, and `full_suite.done` holding `0`. Every skip must be one that is skipped on master too.

- [ ] **Step 2: the change-log entry**

```bash
hive log major_change "Native pre-merge CI: hive ci premerge/watch/conflicts/legs run ci.yml's legs locally against a PR's merge, post mindhive-ci commit statuses and record a premerge verification, so a billing-blocked Actions no longer blocks merging" --files src/mindhive/ci/proc.py src/mindhive/ci/legs.py src/mindhive/ci/preview.py src/mindhive/ci/github.py src/mindhive/ci/legrun.py src/mindhive/ci/premerge.py src/mindhive/ci/watch.py src/mindhive/ci/premerge_cli.py src/mindhive/ci/cli.py tests/test_ci_proc.py tests/test_ci_legs.py tests/test_ci_preview.py tests/test_ci_github.py tests/test_ci_legrun.py tests/test_ci_premerge.py tests/test_ci_watch.py tests/test_ci_premerge_cli.py tests/test_advice_authorship.py tests/test_commands_reexport.py
```

- [ ] **Step 3: dogfood, read-only first**

```bash
hive ci legs
hive ci conflicts
```

Expected: every windows and ubuntu leg `ready` (ubuntu through WSL), `browser` and `node` ready, `pg` NOT RUNNABLE naming `MINDHIVE_CI_PG_DSN`; exit 0.

- [ ] **Step 4: dogfood, one real PR, no merge**

```bash
hive ci premerge 151 --project mindhive
```

Expected: the report with eight legs run and `pg` named as not run here, statuses `mindhive-ci` and `mindhive-ci/<leg>` visible on the PR (`gh pr checks 151`), one `verification` with `gate: "premerge"` (`hive ci status --pr 151`). Never `--merge` without the owner's word.

- [ ] **Step 5: review, then ask before pushing**

Run `compound-engineering:ce-code-review` on the branch diff, fix what it confirms, re-run the affected tests and the full suite. Then ask the owner before `git push -u origin claude/native-ci-premerge` and before opening the PR.
