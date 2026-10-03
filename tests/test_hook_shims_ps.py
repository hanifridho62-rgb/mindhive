# tests/test_hook_shims_ps.py
"""The PowerShell hook shims — the family that used to hard-code one operator's
interpreter, and the guard that keeps it portable.

## What was wrong

Every `hooks/*.ps1` and `circle.ps1` opened with

    $py = "C:\\Users\\hanif\\mindhive\\.venv\\Scripts\\python.exe"

so on any other machine, user, or venv layout the interpreter did not exist and
the hook did nothing. Nothing *reported* that: the shims are fail-open by
design, so a clone got a working-looking install with no telemetry capture and
no session rotation at all. `hooks/lib.sh` had already refused to repeat that on
the POSIX side; `hooks/lib.ps1` is that refusal ported back, and this suite is
what keeps it honest.

## Why the assertions are shaped around failure

These hooks are registered in `~/.claude/settings.json`, which means they fire
in EVERY session on the machine, in every project. The project's CLAUDE.md puts
it plainly: a bug here "does not break one feature, it breaks all work
everywhere, and it breaks it from inside the tool you would use to fix it." So
most of what follows asserts what a shim must NOT do — exit non-zero, print on a
decision channel — driven by inputs designed to break it: no library, an
unparseable library, no interpreter anywhere, a directory where an interpreter
should be, and a PATH offering only the Microsoft Store redirector.

## The canaries

Two assertions exist only to falsify a resolver that always answers nothing, and
a partition that always drops everything:
`test_a_real_interpreter_on_path_is_accepted` and
`test_the_shims_dispatch_the_expected_stage`. Without them, a `Get-MindhivePython`
that returned `$null` unconditionally would pass every refusal test in the file.

## The parse test earns its place

`test_every_powershell_file_parses` caught a real defect while this was written.
These files carry no BOM, so PowerShell 5.1 decodes them as CP1252: an em dash
(`E2 80 94`) becomes three characters ending in RIGHT DOUBLE QUOTATION MARK —
which PowerShell accepts as a string TERMINATOR. Inside a comment that is
cosmetic mojibake; inside a quoted string it ends the string early and the whole
file stops parsing, which would have made every hook on the machine a no-op at
once. A grep for smart quotes would not have found it; parsing does.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "hooks"
LIB = HOOKS / "lib.ps1"

# `powershell.exe` is Windows PowerShell 5.1 — the edition the shims are written
# for and the one Claude Code invokes them with. `pwsh` (7+) is deliberately not
# accepted as a substitute: the dialect rules these files obey (no `&&`, no
# `?:`) exist precisely because 5.1 rejects them, so testing on 7 would prove
# the wrong thing.
POWERSHELL = shutil.which("powershell")

windows_only = pytest.mark.skipif(
    POWERSHELL is None or sys.platform != "win32",
    reason="Windows PowerShell 5.1 not available")

PAYLOAD = json.dumps({"session_id": "ps-test-1", "tool_name": "Bash",
                      "tool_input": {"command": "git status"},
                      "tool_response": {"stdout": "ok"}})

# Same split `tests/test_hook_shims.py` uses: stdout is a channel Claude Code
# reads for these, and `pre_compact` prints one fixed human line.
DECISION_STAGES = ("pre_tool_use", "user_prompt_submit", "stop")
SILENT_STAGES = ("post_tool_use", "post_tool_use_failure", "autosync",
                 "session_end")
REMINDER = "MIND HIVE: before compaction, fold this session"


# --- helpers -------------------------------------------------------------------

# The shims that do real work whether or not a payload arrives, and the argv
# fragment each one's child is called with. The other five have nothing to do
# without a payload and must spawn no child at all.
WORK_WITHOUT_PAYLOAD = {
    "autosync": "mindhive autosync",
    "pre_compact": "append compaction",
    "session_start": "append session_start",
    # `--session unknown` rather than `append session_end`: it proves the
    # payload read ANSWERED (with nothing) instead of never being reached.
    "session_end": "--session unknown",
}

# What a developer's own shell may carry that changes what a shim does.
# `MINDHIVE_HEADLESS` is the dangerous one: every shim's first executable line
# is `if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }`, so a suite run from a
# headless session (the auto-reflection path sets it) would watch every shim
# exit before anything under test ran, and every "no child was spawned"
# assertion would pass for that reason alone.
STRIPPED_ENV = ("MINDHIVE_PYTHON", "MINDHIVE_HOOK_DIR", "MINDHIVE_HOOK_WALK_LIMIT",
                "MINDHIVE_HEADLESS", "MINDHIVE_SESSION", "MINDHIVE_ROTATION")


def ps_shims() -> list:
    return sorted(p.stem for p in HOOKS.glob("*.ps1") if p.name != "lib.ps1")


def clean_env(env=None) -> dict:
    """`os.environ` minus every variable in `STRIPPED_ENV`, plus *env*. The one
    place a child environment is built, so no test can quietly bypass it."""
    base = dict(os.environ)
    for k in STRIPPED_ENV:
        base.pop(k, None)
    base.update(env or {})
    return base


def ps_quote(value) -> str:
    """*value* as a single-quoted PowerShell literal. Nothing is escaped inside
    one, so a backslash stays a backslash and only `'` needs doubling."""
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(script: str, env=None, stdin: str = "") -> subprocess.CompletedProcess:
    """Run one `.ps1` under 5.1 with a clean profile, never inheriting a
    MINDHIVE_* variable from the developer's own shell."""
    return subprocess.run([POWERSHELL, "-NoProfile", "-File", str(script)],
                          input=stdin, capture_output=True, text=True,
                          env=clean_env(env), timeout=120)


def run_ps_on_a_console(script, env=None, timeout=60) -> subprocess.CompletedProcess:
    """Run one `.ps1` whose stdin is a real CONSOLE: neither a pipe nor NUL.

    Every other runner here gives the child a pipe (`input=`) or `DEVNULL`, and
    `[Console]::IsInputRedirected` reports BOTH as redirected. NUL is a
    character device, but .NET's `GetConsoleMode` probe fails on it. So the
    hang guard's one real case, a shim run by hand from a terminal, had no test
    that could reach it. `CREATE_NEW_CONSOLE` without `STARTF_USESTDHANDLES`
    (nothing passed for stdin/stdout/stderr) gives the child a fresh console on
    all three standard handles, which is exactly that case. `SW_HIDE` stops the
    window from flashing.

    The price is that stdout goes to that hidden console, not to a pipe, so a
    probe run this way has to report through a FILE. A read that blocks is
    caught by *timeout*: `subprocess.run` kills the child and raises
    `TimeoutExpired`."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                       # SW_HIDE
    return subprocess.run([POWERSHELL, "-NoProfile", "-File", str(script)],
                          env=clean_env(env), timeout=timeout,
                          creationflags=subprocess.CREATE_NEW_CONSOLE,
                          startupinfo=si)


def eval_ps(body: str, env=None, tmp_path=None, lib=None) -> subprocess.CompletedProcess:
    """Dot-source a copy of the library and run *body* against it."""
    script = Path(tmp_path) / "probe.ps1"
    script.write_text(
        '. "%s"\n%s\n' % (str(lib or LIB).replace("\\", "\\\\"), body),
        encoding="ascii")
    return run_ps(script, env=env)


def fake_exe(path: Path) -> Path:
    """A real, executable file standing in for an interpreter. A copy of a
    system binary rather than a text file, because the resolver's job is to
    decide WHETHER to hand a path to `&`, and a fixture that could never be
    executed would let a broken check pass."""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(r"C:\Windows\System32\cmd.exe", path)
    return path


def stub_interpreter(tmp_path: Path, log: Path) -> Path:
    """An 'interpreter' that records the argv it was called with, for
    `MINDHIVE_PYTHON`, which takes any runnable extension. It cannot stand in
    for a PATH interpreter: PATH yields only a `.exe` (see `logging_python`)."""
    stub = tmp_path / "stub.cmd"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("@echo off\r\necho %%* >> \"%s\"\r\n" % log, encoding="ascii")
    return stub


# The line `printing_interpreter` puts on stdout, up to the argv it appends.
CHILD_STDOUT = '"reason":"child stdout: '


def printing_interpreter(tmp_path: Path, log: Path) -> Path:
    """An 'interpreter' that records its argv AND prints a decision-shaped line
    on its stdout.

    `stub_interpreter` writes only to a FILE, so it can never put anything on a
    shim's stdout. That left the whole class "the child printed, and the shim
    must swallow it" unasserted: `| Out-Null` could be deleted from any silent
    shim with the suite green. The printed line ends with the argv, so a
    failure says WHICH call leaked."""
    stub = tmp_path / "printing_stub.cmd"
    stub.write_text(
        "@echo off\r\n"
        "echo %%* >> \"%s\"\r\n"
        "echo {\"decision\":\"block\",\"reason\":\"child stdout: %%*\"}\r\n" % log,
        encoding="ascii")
    return stub


def logging_python(tmp_path: Path, log: Path):
    """A REAL `python.exe` for PATH whose `-m mindhive` appends its argv to
    *log*. Answers `(bin_dir, env)`: put *bin_dir* on PATH and merge *env*.

    PATH yields only a `.exe`, so `stub_interpreter`'s `.cmd` cannot play a
    PATH interpreter, and a copy of `cmd.exe` would start a shell. The base
    CPython this suite runs on is used in place -- its own directory, so its
    DLLs resolve -- with a fake `mindhive` package first on PYTHONPATH whose
    `__main__` does nothing but record the call."""
    base = Path(sys.base_prefix) / "python.exe"
    if not base.is_file():
        pytest.skip("no python.exe under sys.base_prefix (%s)" % sys.base_prefix)
    if "\\windowsapps\\" in str(base).lower():
        pytest.skip("the base interpreter is a Store install, which PATH refuses")
    pkg = tmp_path / "fake_site" / "mindhive"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text(
        "import sys\n"
        "with open(%r, 'a', encoding='utf-8') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n" % str(log),
        encoding="utf-8")
    return base.parent, {"PYTHONPATH": str(pkg.parent)}


# --- the parse gate ------------------------------------------------------------

@windows_only
def test_every_powershell_file_parses(tmp_path):
    """Every `.ps1` in the repo is syntactically valid 5.1.

    This is the assertion that caught the CP1252 smart-quote hazard described in
    the module docstring. It is cheap and it covers the whole family, including
    files no other test in here executes."""
    targets = sorted(HOOKS.glob("*.ps1")) + [REPO / "circle.ps1"]
    failures = []
    for f in targets:
        script = tmp_path / ("parse_%s.ps1" % f.stem)
        script.write_text(
            '$errs = $null\n'
            '[void][System.Management.Automation.Language.Parser]::ParseFile('
            '"%s", [ref]$null, [ref]$errs)\n'
            'if ($errs -and $errs.Count -gt 0) { $errs | ForEach-Object '
            '{ Write-Output $_.Message }; exit 1 }\n'
            'exit 0\n' % str(f).replace("\\", "\\\\"),
            encoding="ascii")
        r = run_ps(script)
        if r.returncode != 0:
            failures.append("%s: %s" % (f.name, r.stdout.strip()[:200]))
    assert failures == [], "PowerShell parse failures: " + " | ".join(failures)


# --- Get-MindhivePython: the resolution order ----------------------------------

@windows_only
def test_mindhive_python_is_the_operators_override(tmp_path):
    exe = fake_exe(tmp_path / "custom" / "python.exe")
    r = eval_ps("Write-Output (Get-MindhivePython)", tmp_path=tmp_path,
                env={"MINDHIVE_PYTHON": str(exe)})
    assert r.stdout.strip() == str(exe)


@windows_only
def test_a_directory_at_mindhive_python_is_refused(tmp_path):
    """`-PathType Leaf`, not a bare `Test-Path`. A directory passes the bare
    form, and an operator's typo would otherwise become the interpreter every
    hook execs — the same reason `lib.sh` tests `-f` as well as `-x`."""
    d = tmp_path / "notaninterpreter"
    d.mkdir()
    repo = tmp_path / "repo"
    exe = fake_exe(repo / ".venv" / "Scripts" / "python.exe")
    hooks = repo / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    r = eval_ps("Write-Output (Get-MindhivePython)", tmp_path=tmp_path,
                env={"MINDHIVE_PYTHON": str(d), "MINDHIVE_HOOK_DIR": str(hooks)})
    assert r.stdout.strip() == str(exe)


@windows_only
def test_the_nearest_venv_above_the_hook_is_found(tmp_path):
    """The walk-up reaches a `.venv` several levels above the hooks directory --
    the shape of a git worktree under `<repo>\\.claude\\worktrees\\<name>` with
    no `.venv` of its own.

    This asserts WHICH PATH is returned, and nothing about what that
    interpreter imports. It used to say the walk-up "is what makes a git
    worktree work", which is false: the parent repo's editable venv imports the
    PARENT checkout's `src`, so a worktree's hooks run the parent's `mindhive`,
    not the worktree's branch. `hooks/lib.ps1` step 2 states the real contract."""
    repo = tmp_path / "repo"
    exe = fake_exe(repo / ".venv" / "Scripts" / "python.exe")
    deep = repo / ".claude" / "worktrees" / "wt" / "hooks"
    deep.mkdir(parents=True)
    r = eval_ps("Write-Output (Get-MindhivePython)", tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": str(deep)})
    assert r.stdout.strip() == str(exe)


@windows_only
def test_the_walk_is_bounded(tmp_path):
    """A hook must never walk to the filesystem root on a machine whose layout
    is not what we expect."""
    repo = tmp_path / "repo"
    fake_exe(repo / ".venv" / "Scripts" / "python.exe")
    deep = repo / "a" / "b" / "c" / "d" / "hooks"
    deep.mkdir(parents=True)
    r = eval_ps("$p = Get-MindhivePython; if ($p) { Write-Output $p } "
                "else { Write-Output 'NONE' }",
                tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": str(deep),
                     "MINDHIVE_HOOK_WALK_LIMIT": "2",
                     "PATH": r"C:\Windows\System32;C:\Windows"})
    assert r.stdout.strip() == "NONE"


@windows_only
@pytest.mark.parametrize("value", ["", "abc", "-3"])
def test_a_junk_walk_limit_falls_back_to_the_default(tmp_path, value):
    """`lib.sh` guards this because a shell error would land on the hook's
    stderr, which is what Claude Code shows when it decides a hook failed. The
    PowerShell half keeps the same contract: junk means the default.

    `0` is NOT junk, and this list used to contain it, asserting `0 -> 8`.
    `lib.sh` resets the limit for `''|*[!0-9]*|??????????*` -- empty, holding
    a non-digit, or a run of ten or more digits -- and `0` is none of those,
    so it passes the filter and `while [ "$_mh_n" -lt 0 ]` skips the walk. The
    old parametrisation
    therefore guaranteed the two families DISAGREED about the one value an
    operator would set to switch the walk off.
    `test_a_zero_walk_limit_means_do_not_walk` holds the corrected contract."""
    r = eval_ps("Write-Output (Get-MindhiveWalkLimit)", tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_WALK_LIMIT": value})
    assert r.stdout.strip() == "8"


@windows_only
def test_a_zero_walk_limit_means_do_not_walk(tmp_path):
    """`0` is a VALUE: "do not walk". An operator sets it when the nearest
    `.venv` belongs to a different project, or when an image has to resolve
    strictly from `MINDHIVE_PYTHON` or PATH. `lib.sh` has always honoured it,
    and this returned 8 and walked anyway."""
    r = eval_ps("Write-Output (Get-MindhiveWalkLimit)", tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_WALK_LIMIT": "0"})
    assert r.returncode == 0, r.stderr
    assert r.stderr.strip() == "", r.stderr
    assert r.stdout.strip() == "0", r.stdout


@windows_only
def test_a_zero_walk_limit_skips_a_reachable_venv_and_falls_to_path(tmp_path):
    """The limit is only worth pinning through the resolver it controls. A
    `Get-MindhiveWalkLimit` that answered `0` while `Get-MindhivePython` ignored
    it would still pass the test above.

    DISCRIMINATING in one fixture. With a limit of 2 the fixture `.venv` is in
    range and beats PATH. That is the canary: it proves the `.venv` is
    reachable, so the PATH answer under `0` comes from the limit and not from
    a fixture the walk could never have reached. Under `0` the same tree
    resolves the PATH interpreter, just as `lib.sh`'s zero-iteration loop
    does."""
    repo = tmp_path / "repo"
    venv = fake_exe(repo / ".venv" / "Scripts" / "python.exe")
    hooks = repo / "hooks"
    hooks.mkdir(parents=True)
    good = fake_exe(tmp_path / "realbin" / "python.exe")
    probe = ("$p = Get-MindhivePython; if ($p) { Write-Output $p } "
             "else { Write-Output 'NONE' }")
    path = "%s;C:\\Windows\\System32;C:\\Windows" % good.parent

    walked = eval_ps(probe, tmp_path=tmp_path,
                     env={"MINDHIVE_HOOK_DIR": str(hooks),
                          "MINDHIVE_HOOK_WALK_LIMIT": "2", "PATH": path})
    assert walked.stdout.strip() == str(venv), walked.stdout

    r = eval_ps(probe, tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": str(hooks),
                     "MINDHIVE_HOOK_WALK_LIMIT": "0", "PATH": path})
    assert r.stdout.strip() == str(good), r.stdout


@windows_only
def test_a_windowsapps_python_on_path_is_refused(tmp_path):
    """The Microsoft Store redirector is not a missing file: it spawns cleanly,
    prints an install prompt, and exits 49. A candidate loop that only rejects
    what does not exist would ACCEPT it, and every hook would silently do
    nothing with an interpreter that never ran a line."""
    wapps = tmp_path / "WindowsApps"
    fake_exe(wapps / "python.exe")
    hooks = tmp_path / "iso" / "hooks"
    hooks.mkdir(parents=True)
    r = eval_ps("$p = Get-MindhivePython; if ($p) { Write-Output $p } "
                "else { Write-Output 'NONE' }",
                tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": str(hooks),
                     "MINDHIVE_HOOK_WALK_LIMIT": "1",
                     "PATH": "%s;C:\\Windows\\System32;C:\\Windows" % wapps})
    assert r.stdout.strip() == "NONE"


@windows_only
def test_a_real_interpreter_on_path_is_accepted(tmp_path):
    """THE CANARY for every refusal above. A `Get-MindhivePython` that returned
    nothing unconditionally would satisfy the walk-bound and Store-stub tests
    and be completely broken; this is the assertion it cannot pass."""
    good = tmp_path / "realbin"
    exe = fake_exe(good / "python.exe")
    hooks = tmp_path / "iso" / "hooks"
    hooks.mkdir(parents=True)
    r = eval_ps("$p = Get-MindhivePython; if ($p) { Write-Output $p } "
                "else { Write-Output 'NONE' }",
                tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": str(hooks),
                     "MINDHIVE_HOOK_WALK_LIMIT": "1",
                     "PATH": "%s;C:\\Windows\\System32;C:\\Windows" % good})
    assert r.stdout.strip() == str(exe)


@windows_only
def test_an_interpreter_in_a_quoted_path_entry_is_found(tmp_path):
    """A PATH entry may be written `"C:\\Program Files\\Py"`, quotes included:
    cmd.exe strips them, the MSYS runtime strips them for `lib.sh`, and
    `Get-Command` -- which the PATH tier used to lean on -- does not. So an
    interpreter that sat only in such an entry was found by one family and not
    the other, and on a machine with no venv that was every PowerShell hook
    silently doing nothing (measured by review). The tier walks `$env:PATH`
    itself now and strips the quotes."""
    good = tmp_path / "quoted bin"
    exe = fake_exe(good / "python.exe")
    hooks = tmp_path / "iso" / "hooks"
    hooks.mkdir(parents=True)
    r = eval_ps("Write-Output ('[' + (Get-MindhivePython) + ']')", tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": str(hooks),
                     "MINDHIVE_HOOK_WALK_LIMIT": "1",
                     "PATH": '"%s";C:\\Windows\\System32;C:\\Windows' % good})
    assert r.stderr.strip() == "", r.stderr
    assert r.stdout.strip() == "[%s]" % exe, r.stdout


def _missing_drive() -> str:
    for letter in "ZYXWVUTSRQ":
        if not os.path.exists(letter + ":\\"):
            return letter
    pytest.skip("every drive letter from Q to Z is mapped on this machine")


@windows_only
@pytest.mark.parametrize("preference", ["Stop", "Continue", "SilentlyContinue"])
def test_a_hook_dir_on_a_missing_drive_neither_throws_nor_prints(tmp_path, preference):
    """`Join-Path` resolves its drive through the provider and throws for one
    that does not exist, and the venv walk called it outside any try. So a
    `MINDHIVE_HOOK_DIR` on a drive that is not there ended `circle.ps1`, under
    its `Stop` preference, with a raw DriveNotFoundException in place of
    circle's own message, and under the default `Continue` the record went to
    stderr (measured by review). The contract is "never throws": the walk
    gives up on that directory and PATH still answers, silently, under every
    preference a caller sets."""
    exe = fake_exe(tmp_path / "realbin" / "python.exe")
    script = Path(tmp_path) / "drive.ps1"
    script.write_text(
        "$ErrorActionPreference = '%s'\n. \"%s\"\n"
        "Write-Output ('[' + (Get-MindhivePython) + ']')\nWrite-Output 'REACHED_END'\n"
        % (preference, str(LIB).replace("\\", "\\\\")), encoding="ascii")
    r = run_ps(script, env={"MINDHIVE_HOOK_DIR": _missing_drive() + r":\no\such\hooks",
                            "MINDHIVE_HOOK_WALK_LIMIT": "2",
                            "PATH": "%s;C:\\Windows\\System32;C:\\Windows" % exe.parent})
    assert r.returncode == 0, r.stderr
    assert r.stderr.strip() == "", r.stderr
    assert r.stdout.split() == ["[%s]" % exe, "REACHED_END"], r.stdout


@windows_only
def test_a_msys_spelled_hook_dir_finds_the_venv(tmp_path):
    """`MINDHIVE_HOOK_DIR` in Git Bash's `/c/...` spelling is read as drive C,
    as `MINDHIVE_PYTHON` already was, so a value copied out of that shell names
    one place in both families. The rewrite used to apply to the override
    only: the walk started nowhere and fell through to PATH (measured by
    review). PATH holds no interpreter here, so a walk that still started
    nowhere answers nothing rather than the venv."""
    venv = fake_exe(tmp_path / "repo" / ".venv" / "Scripts" / "python.exe")
    hooks = tmp_path / "repo" / "hooks"
    hooks.mkdir(parents=True)
    win = str(hooks)
    assert win[1:3] == ":\\", f"not a drive-letter path: {win}"
    msys = "/" + win[0].lower() + "/" + win[3:].replace("\\", "/")
    r = eval_ps("Write-Output ('[' + (Get-MindhivePython) + ']')", tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": msys, "MINDHIVE_HOOK_WALK_LIMIT": "2",
                     "PATH": r"C:\Windows\System32;C:\Windows"})
    assert r.stdout.strip() == "[%s]" % venv, r.stdout


@windows_only
def test_circle_reports_a_missing_interpreter_as_one_line_and_exits_1(tmp_path):
    """`circle.ps1` is a command someone typed, so a missing interpreter is
    REPORTED. It reported through `Write-Error`, which under the file's own
    `$ErrorActionPreference = "Stop"` is itself a terminating error: the `exit
    1` after it never ran, the message arrived wrapped in an error record with
    an `At line:` trailer, and run from an open session `$LASTEXITCODE` was
    never set (measured by review). Now it is one line on stderr and an explicit
    exit 1 -- under `-File`, and under `-Command`, where the exit code is the
    thing a caller reads."""
    circle = REPO / "circle.ps1"
    env = {"MINDHIVE_HOOK_WALK_LIMIT": "0",
           "MINDHIVE_PYTHON": str(tmp_path / "absent.exe"),
           "PATH": r"C:\Windows\System32;C:\Windows"}
    r = run_ps(circle, env=env)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert r.stdout.strip() == "", r.stdout
    assert r.stderr.startswith("circle: no usable Python found."), r.stderr
    assert "At line" not in r.stderr and "CategoryInfo" not in r.stderr, r.stderr

    wrapped = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command",
         "& '%s'; Write-Output ('CODE=' + $LASTEXITCODE)" % str(circle).replace("'", "''")],
        capture_output=True, text=True, env=clean_env(env), timeout=120)
    assert "CODE=1" in wrapped.stdout, (wrapped.stdout, wrapped.stderr)
    assert wrapped.stderr.startswith("circle: no usable Python found."), wrapped.stderr


@windows_only
def test_windowed_prefers_pythonw_but_falls_back(tmp_path):
    """`autosync.ps1` asks for the windowless interpreter so no console flashes
    on a scheduled run. It is a PREFERENCE: a venv without `pythonw.exe` still
    resolves, because a visible window is cosmetic and no sync at all is not."""
    repo = tmp_path / "repo"
    hooks = repo / "hooks"
    hooks.mkdir(parents=True)
    console = fake_exe(repo / ".venv" / "Scripts" / "python.exe")
    r = eval_ps("Write-Output (Get-MindhivePython -Windowed)", tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": str(hooks)})
    assert r.stdout.strip() == str(console)

    windowless = fake_exe(repo / ".venv" / "Scripts" / "pythonw.exe")
    r = eval_ps("Write-Output (Get-MindhivePython -Windowed)", tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": str(hooks)})
    assert r.stdout.strip() == str(windowless)


@windows_only
@pytest.mark.parametrize("leaf", ["python.exe", "python3.exe"])
def test_a_bin_layout_venv_holding_an_exe_is_found(tmp_path, leaf):
    """An MSYS2 or MinGW Python makes `.venv\\bin\\python.exe`, and often
    `python3.exe`. `lib.sh` resolved both all along -- Git Bash's
    `[ -f bin/python ]` and `[ -f bin/python3 ]` are true for the `.exe` files
    -- while no leaf here named either, so this family skipped the venv and ran
    whatever PATH held. `bin\\python.exe` got its leaf first. `python3.exe` is
    parametrised in with the unified leaf order (`Scripts\\python.exe`,
    `bin\\python3.exe`, `bin\\python.exe`, in both families), because a lone
    `bin\\python3.exe` still fell through to PATH here -- measured by a parity
    probe. PATH holds a DIFFERENT real interpreter, so the answer says which
    tier won.

    `-Windowed` builds its own leaf list, so it is asserted on the same tree:
    it takes the layout's `pythonw.exe` once that exists, and the console leaf
    over PATH until then."""
    repo = tmp_path / "repo"
    hooks = repo / "hooks"
    hooks.mkdir(parents=True)
    console = fake_exe(repo / ".venv" / "bin" / leaf)
    other = fake_exe(tmp_path / "realbin" / "python.exe")
    env = {"MINDHIVE_HOOK_DIR": str(hooks), "MINDHIVE_HOOK_WALK_LIMIT": "2",
           "PATH": "%s;C:\\Windows\\System32;C:\\Windows" % other.parent}
    probe = ("Write-Output ('CONSOLE:' + (Get-MindhivePython))\n"
             "Write-Output ('WINDOWED:' + (Get-MindhivePython -Windowed))")

    lines = eval_ps(probe, tmp_path=tmp_path, env=env).stdout.splitlines()
    assert "CONSOLE:%s" % console in lines, lines
    assert "WINDOWED:%s" % console in lines, lines

    windowless = fake_exe(repo / ".venv" / "bin" / "pythonw.exe")
    lines = eval_ps(probe, tmp_path=tmp_path, env=env).stdout.splitlines()
    assert "CONSOLE:%s" % console in lines, lines
    assert "WINDOWED:%s" % windowless in lines, lines


# --- Get-MindhiveExecutable: a leaf Windows can actually RUN --------------------

@windows_only
@pytest.mark.parametrize("leaf", ["python3", "python"])
def test_an_extensionless_venv_leaf_does_not_shadow_a_path_interpreter(tmp_path, leaf):
    """A POSIX-layout venv on a Windows filesystem (`.venv\\bin\\python3`,
    created from WSL or Git Bash, no `Scripts\\`) used to WIN tier 2.
    `Get-MindhiveExecutable` asked only `Test-Path -PathType Leaf`, so it
    returned a file `&` refuses to run. PowerShell execs by PATHEXT, and an
    extensionless path is "not an operable program". Each shim's
    `SilentlyContinue` swallows that refusal, so all nine hooks became
    permanent silent no-ops on a machine whose PATH interpreter, one tier
    down, worked.

    The fixture is a real PE (a copy of `cmd.exe`) under the extensionless
    name, and the probe first proves `&` refuses it. A text file there would
    let a check that only asked "is this a binary" pass for the wrong reason.
    `-Windowed` (autosync's call) walks the same leaves, so it is asserted too.

    CANARY at the end: add a Windows-layout `.venv\\Scripts\\python.exe` to the
    SAME tree, with the same limit and PATH, and it wins. So the PATH answer
    above means the leaf was refused, not that tier 2 was out of reach."""
    repo = tmp_path / "repo"
    posix = fake_exe(repo / ".venv" / "bin" / leaf)
    hooks = repo / "hooks"
    hooks.mkdir(parents=True)
    good = fake_exe(tmp_path / "realbin" / "python.exe")
    env = {"MINDHIVE_HOOK_DIR": str(hooks), "MINDHIVE_HOOK_WALK_LIMIT": "2",
           "PATH": "%s;C:\\Windows\\System32;C:\\Windows" % good.parent}
    r = eval_ps(
        "try { & %s /c exit 0 | Out-Null; Write-Output 'LEAF:RAN' } "
        "catch { Write-Output 'LEAF:REFUSED' }\n"
        "Write-Output ('CONSOLE:' + (Get-MindhivePython))\n"
        "Write-Output ('WINDOWED:' + (Get-MindhivePython -Windowed))"
        % ps_quote(posix), tmp_path=tmp_path, env=env)
    lines = r.stdout.splitlines()
    assert "LEAF:REFUSED" in lines, \
        "the fixture leaf is runnable, so it proves nothing: %r" % r.stdout
    assert "CONSOLE:%s" % good in lines, r.stdout
    assert "WINDOWED:%s" % good in lines, r.stdout

    scripts = fake_exe(repo / ".venv" / "Scripts" / "python.exe")
    r = eval_ps("Write-Output (Get-MindhivePython)", tmp_path=tmp_path, env=env)
    assert r.stdout.strip() == str(scripts), r.stdout


@windows_only
def test_an_extensionless_mindhive_python_is_refused(tmp_path):
    """The same unrunnable leaf typed by hand: `MINDHIVE_PYTHON` copied from a
    POSIX machine, naming a `python3` with no extension. That is why the check
    lives in `Get-MindhiveExecutable`, which all three tiers share, and not in
    the venv leaf list.

    CANARY in the same fixture: `python3.exe` beside it, named by the same
    variable, still beats PATH. So the override tier is live, and the refusal
    comes from the extension."""
    odd = fake_exe(tmp_path / "custom" / "python3")
    good = fake_exe(tmp_path / "realbin" / "python.exe")
    hooks = tmp_path / "iso" / "hooks"
    hooks.mkdir(parents=True)
    env = {"MINDHIVE_HOOK_DIR": str(hooks), "MINDHIVE_HOOK_WALK_LIMIT": "1",
           "PATH": "%s;C:\\Windows\\System32;C:\\Windows" % good.parent}
    probe = "Write-Output (Get-MindhivePython)"

    r = eval_ps(probe, tmp_path=tmp_path, env=dict(env, MINDHIVE_PYTHON=str(odd)))
    assert r.stdout.strip() == str(good), r.stdout

    named = fake_exe(tmp_path / "custom" / "python3.exe")
    r = eval_ps(probe, tmp_path=tmp_path, env=dict(env, MINDHIVE_PYTHON=str(named)))
    assert r.stdout.strip() == str(named), r.stdout


@windows_only
def test_a_shim_beside_a_posix_only_venv_still_dispatches_through_path(tmp_path):
    """The defect measured where it did the damage: in the SHIM. Before the
    fix, `post_tool_use.ps1` in a checkout whose only venv was POSIX-layout
    resolved `.venv\\bin\\python3`, `&` refused it inside the shim's `try`, and
    the hook exited 0 having done nothing. From outside, that is
    indistinguishable from a hook working normally.

    DISCRIMINATING by dispatch, not by exit code: the PATH interpreter is a
    real `python.exe` that records its `-m mindhive` argv (`logging_python`),
    and the assertion is that it ran. It used to be a `python.cmd` stub; PATH
    yields only a `.exe` now, so that stub would be passed over and this would
    fail for a reason that has nothing to do with the venv leaf it is about.
    No `MINDHIVE_HOOK_DIR` either, so the walk starts from the shim's real
    `$PSScriptRoot`, the way Claude Code runs it.

    CANARY at the end, and it is load-bearing. The PATH dispatch is only
    evidence if that walk really reaches `repo\\.venv`: a library that lost its
    `$PSScriptRoot` capture walks from the cwd instead, never sees the POSIX
    leaf, dispatches through PATH all the same, and this test would pass with
    the defect back in. So a Windows-layout leaf added to the same tree, probed
    through the COPIED library with the same limit and PATH, must win."""
    repo = tmp_path / "repo"
    fake_exe(repo / ".venv" / "bin" / "python3")
    dest = _copy_family(repo)
    log = tmp_path / "argv.log"
    realbin, pyenv = logging_python(tmp_path, log)
    r = run_ps(dest / "post_tool_use.ps1", stdin=PAYLOAD,
               env=dict(pyenv, MINDHIVE_HOOK_WALK_LIMIT="2",
                        PATH="%s;C:\\Windows\\System32;C:\\Windows" % realbin))
    assert r.returncode == 0, r.stderr
    seen = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    assert "hook post_tool_use" in seen, \
        "the shim resolved nothing runnable and did nothing; argv log: %r" % seen

    scripts = fake_exe(repo / ".venv" / "Scripts" / "python.exe")
    r = eval_ps("Write-Output (Get-MindhivePython)", tmp_path=tmp_path,
                lib=dest / "lib.ps1",
                env={"MINDHIVE_HOOK_WALK_LIMIT": "2",
                     "PATH": "%s;C:\\Windows\\System32;C:\\Windows" % realbin})
    assert r.stdout.strip() == str(scripts), \
        "the walk from the library's own directory never reached repo\\.venv, " \
        "so the PATH dispatch above proves nothing: %r" % r.stdout


# A stock Windows PATHEXT. Spelled out rather than inherited so no row of the
# table depends on what the machine running the suite has added (a Python
# installer can append `.PY`, for one).
STOCK_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"


@windows_only
def test_runnable_means_an_extension_windows_will_exec(tmp_path):
    """`Test-MindhiveRunnable` is `lib.sh`'s `-x`, the Windows way: PATHEXT
    decides what `&` will run, not a mode bit. It is driven directly here, one
    PowerShell process for the whole table, because the resolver tests above
    only reach the cases their fixtures happen to build.

    The rows that earn their place:
      * `.venv\\bin\\python3`: a DOTTED directory above an extensionless leaf.
        `GetExtension` reads only the last segment; a check that accepted any
        `.` in the path would pass it.
      * PATHEXT removed -> the built-in `.COM;.EXE;.BAT;.CMD`. `.vbs` is in a
        stock PATHEXT and not in the fallback, so it distinguishes the
        fallback from "whatever was there before", and `.exe` still passing
        proves a fallback exists at all.
      * a custom PATHEXT: the variable is READ, not assumed. `.foo` passes and
        `.exe` does not; case is ignored and an empty `;;` entry is skipped."""
    cases = [
        # (path, PATHEXT -- None means removed, expected)
        (r"C:\py\python.exe", STOCK_PATHEXT, True),
        (r"C:\py\PYTHON.EXE", STOCK_PATHEXT, True),
        (r"C:\py\python.cmd", STOCK_PATHEXT, True),
        (r"C:\py\python.vbs", STOCK_PATHEXT, True),
        (r"C:\repo\.venv\bin\python3", STOCK_PATHEXT, False),
        (r"C:\repo\.venv\bin\python", STOCK_PATHEXT, False),
        ("", STOCK_PATHEXT, False),
        (r"C:\py\python.exe", None, True),
        (r"C:\py\python.bat", None, True),
        (r"C:\py\python.vbs", None, False),
        (r"C:\py\python.foo", ".FOO;.BAR", True),
        (r"C:\py\python.exe", ".FOO;.BAR", False),
        (r"C:\py\python.EXE", ".exe;;", True),
    ]
    body = []
    for i, (path, pathext, _) in enumerate(cases):
        if pathext is None:
            body.append("Remove-Item Env:PATHEXT -ErrorAction SilentlyContinue")
        else:
            body.append("$env:PATHEXT = %s" % ps_quote(pathext))
        body.append("Write-Output ('{0}|{1}|{2}' -f %d, (Test-MindhiveRunnable %s), "
                    "$env:PATHEXT)" % (i, ps_quote(path)))
    r = eval_ps("\n".join(body), tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stderr.strip() == "", r.stderr
    got = {}
    for line in r.stdout.splitlines():
        idx, verdict, seen_pathext = line.split("|", 2)
        got[int(idx)] = (verdict, seen_pathext)
    wrong = []
    for i, (path, pathext, expect) in enumerate(cases):
        verdict, seen_pathext = got.get(i, ("<missing>", ""))
        if pathext is None:
            assert seen_pathext == "", "PATHEXT was not really removed: %r" % seen_pathext
        if verdict != str(expect):
            wrong.append("%r under PATHEXT=%r -> %s (want %s)"
                         % (path, pathext, verdict, expect))
    assert wrong == [], "; ".join(wrong)


# --- Get-MindhiveSessionId: the whitelist --------------------------------------

@windows_only
def test_a_real_session_id_survives(tmp_path):
    sid = "39151937-fd17-48c0-bb12-fd5e596b1c1f"
    r = eval_ps("Write-Output (Get-MindhiveSessionId '{\"session_id\":\"%s\"}')" % sid,
                tmp_path=tmp_path)
    assert r.stdout.strip() == sid


@windows_only
@pytest.mark.parametrize("raw", [
    '{"session_id":"../../x"}',            # traversal
    '{"session_id":"C:\\\\evil"}',         # absolute path
    '{"session_id":"a/b"}',                # separator
    '{"session_id":"a*b"}',                # wildcard the Windows kernel refuses
    '{"session_id":"a?b"}',
    '{"session_id":"a|b"}',
    '{"session_id":"a:b"}',
    '{"session_id":"-flag"}',              # id shaped like an option
    '{"session_id":".."}',
    '{"session_id":""}',
    '{"session_id":{"a":1}}',              # nested object
    '{"session_id":5}',                    # number
    '{}',                                  # key absent
    'not json at all',
    '',
])
def test_a_malformed_session_id_becomes_unknown(tmp_path, raw):
    """The id names a ledger segment (`events.append_event` writes
    `<events>/<session_id>.jsonl`), so anything that is not a plain filename-safe
    token is refused at the boundary. Claude Code's own ids are UUIDs, so nothing
    real is ever rejected; this is for the day something else writes this stdin."""
    script = Path(tmp_path) / "sid.ps1"
    script.write_text(
        '. "%s"\n$raw = [Console]::In.ReadToEnd()\n'
        'Write-Output (Get-MindhiveSessionId $raw)\n'
        % str(LIB).replace("\\", "\\\\"), encoding="ascii")
    r = run_ps(script, stdin=raw)
    assert r.stdout.strip() == "unknown", r.stdout


@windows_only
def test_a_session_id_with_a_trailing_newline_is_refused(tmp_path):
    """`\\z`, not `$`. In .NET `$` also matches immediately BEFORE a final
    newline, so a `$`-anchored check would let an id carrying a line break reach
    the ledger — and a line break is what forges a second row."""
    r = eval_ps(
        "$o = @{ session_id = \"abc`n\" } | ConvertTo-Json -Compress; "
        "Write-Output (Get-MindhiveSessionId $o)", tmp_path=tmp_path)
    assert r.stdout.strip() == "unknown"


@windows_only
def test_an_over_long_session_id_is_refused(tmp_path):
    r = eval_ps("$o = @{ session_id = ('x' * 200) } | ConvertTo-Json -Compress; "
                "Write-Output (Get-MindhiveSessionId $o)", tmp_path=tmp_path)
    assert r.stdout.strip() == "unknown"


# --- the shims themselves: fail-open under every breakage ----------------------

def assert_quiet(stem, r):
    """Fail-open means silent on BOTH channels -- with one documented exception.

    `pre_compact` prints a fixed sentence addressed to a human. It needs no
    interpreter, so its contract is "the reminder and nothing else" rather than
    "nothing at all". Gating that line behind the resolver was a regression
    this branch introduced; these three tests asserted the regression, which is
    why they had to change with it."""
    assert r.returncode == 0, (stem, r.stderr)
    assert r.stderr.strip() == "", (stem, r.stderr)
    out = r.stdout.strip()
    if stem == "pre_compact":
        assert REMINDER in out, (stem, r.stdout)
        assert len(out.splitlines()) == 1, (stem, r.stdout)
    else:
        assert out == "", (stem, r.stdout)


def _copy_family(tmp_path, with_lib=True, lib_text=None):
    dest = tmp_path / "hooks"
    dest.mkdir(parents=True, exist_ok=True)
    for f in HOOKS.glob("*.ps1"):
        if f.name == "lib.ps1":
            continue
        shutil.copy(f, dest / f.name)
    if with_lib:
        if lib_text is None:
            shutil.copy(LIB, dest / "lib.ps1")
        else:
            (dest / "lib.ps1").write_text(lib_text, encoding="ascii")
    return dest


@windows_only
@pytest.mark.parametrize("stem", ps_shims())
def test_a_shim_with_no_library_is_a_silent_no_op(tmp_path, stem):
    dest = _copy_family(tmp_path, with_lib=False)
    assert_quiet(stem, run_ps(dest / (stem + ".ps1"), stdin=PAYLOAD))


@windows_only
@pytest.mark.parametrize("stem", ps_shims())
def test_a_shim_with_an_unparseable_library_is_a_silent_no_op(tmp_path, stem):
    """A library that cannot even be parsed is the case a bare `. $lib` would
    turn into a visible error at the end of every turn in every session."""
    dest = _copy_family(tmp_path, lib_text="function Broken { if ( {\r\n")
    assert_quiet(stem, run_ps(dest / (stem + ".ps1"), stdin=PAYLOAD))


@windows_only
@pytest.mark.parametrize("stem", ps_shims())
def test_a_shim_with_no_interpreter_anywhere_is_a_silent_no_op(tmp_path, stem):
    dest = _copy_family(tmp_path)
    assert_quiet(stem, run_ps(
        dest / (stem + ".ps1"), stdin=PAYLOAD,
        env={"MINDHIVE_PYTHON": r"C:\definitely\not\here.exe",
             "MINDHIVE_HOOK_WALK_LIMIT": "1",
             "PATH": r"C:\Windows\System32;C:\Windows"}))


@windows_only
@pytest.mark.parametrize("stem", ps_shims())
def test_an_empty_payload_costs_no_child_process(tmp_path, stem, monkeypatch):
    """REPLACES a test that proved nothing. It was called
    `test_no_shim_blocks_when_stdin_is_not_redirected`, and it set
    `MINDHIVE_PYTHON` to a path that does not exist -- so every shim exited at
    `if (-not $py)` and never reached the stdin guard it claimed to exercise.
    Its docstring was wrong too: `subprocess` with no `input=` INHERITS stdin,
    it does not close it.

    What is actually checkable is stated here instead: with a resolvable
    interpreter and a CLOSED stdin (`DEVNULL`, which .NET reports as
    redirected), a shim must finish and must not spend a child process on an
    empty payload. The stub records every invocation, so an empty log is a
    real assertion rather than an absence of evidence.

    The environment is built by `clean_env`, and the test asserts
    `MINDHIVE_HEADLESS` is absent from it. It used to be
    `{**os.environ, ...}`, the one runner in the file that bypassed the strip.
    Every shim's first line exits on `MINDHIVE_HEADLESS=1`, so run from a
    headless session the shim never reached the guard, and "no child" was
    true for that reason alone. The four shims in `WORK_WITHOUT_PAYLOAD` are the
    canary in the same fixture: they MUST dispatch, which a shim that exited on
    its first line cannot do.

    `MINDHIVE_HEADLESS=1` is SET in this process before the env is built, so
    the strip is exercised on every run, not only on the day the suite happens
    to run from a headless session. In an ordinary shell the variable is absent
    anyway, and the absence assert would be true of `clean_env` with the strip
    deleted.

    The terminal case, `IsInputRedirected` false, is
    `test_no_shim_blocks_on_a_real_console_stdin`."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = stub_interpreter(tmp_path, log)
    monkeypatch.setenv("MINDHIVE_HEADLESS", "1")
    env = clean_env({"MINDHIVE_PYTHON": str(stub)})
    assert "MINDHIVE_HEADLESS" not in env
    r = subprocess.run([POWERSHELL, "-NoProfile", "-File",
                        str(dest / (stem + ".ps1"))],
                       capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL, env=env)
    assert r.returncode == 0, r.stderr
    seen = log.read_text(encoding="ascii", errors="replace") if log.exists() else ""
    if stem in WORK_WITHOUT_PAYLOAD:
        assert WORK_WITHOUT_PAYLOAD[stem] in seen, \
            "%s never dispatched, so the shim exited before its work: %r" % (stem, seen)
        return
    assert seen.strip() == "", "%s spawned a child for an empty payload" % stem


@windows_only
@pytest.mark.parametrize("stem", [s for s in ps_shims()
                                  if s not in ("autosync", "circle")])
def test_every_stdin_reading_shim_carries_the_hang_guard(stem):
    """The guard asserted in the SOURCE, as a cheap first line of defence.
    `[Console]::In.ReadToEnd()` on an unredirected stdin blocks forever, so
    every shim that reads a payload must check `IsInputRedirected` first --
    directly, or via `Get-MindhivePayload`.

    A presence check and nothing more. For the three shims that delegate
    (`session_start`, `session_end`, `pre_compact`) it is satisfied by the CALL,
    so it cannot see whether `Get-MindhivePayload` still guards the read:
    deleting the check inside `lib.ps1` left this green. It used to claim
    structural coverage of the terminal case, which it cannot provide. The
    behaviour is asserted by
    `test_get_mindhive_payload_returns_empty_on_a_console_stdin` and
    `test_no_shim_blocks_on_a_real_console_stdin`, which run the code with a
    real console on stdin."""
    src = (HOOKS / (stem + ".ps1")).read_text(encoding="utf-8", errors="replace")
    if "ReadToEnd" not in src and "Get-MindhivePayload" not in src:
        pytest.skip("%s reads no payload" % stem)
    assert ("IsInputRedirected" in src) or ("Get-MindhivePayload" in src), stem


@windows_only
def test_get_mindhive_payload_returns_empty_on_a_console_stdin(tmp_path):
    """The hang guard, executed. `Get-MindhivePayload` must answer "" when stdin
    is a terminal, not wait on a read nobody is going to answer.

    Nothing could fail it before. Every runner in this file supplies stdin as
    a pipe or `DEVNULL`, both of which .NET reports as redirected, so replacing
    the guarded read with a bare `[Console]::In.ReadToEnd()` kept the suite
    green. This runs the library with a real, hidden console on stdin instead.

    The probe records `IsInputRedirected` BEFORE the call, and the test requires
    it to be False. A launcher that quietly gave the child a redirected stdin
    would make "it did not block" true by construction, the same way the test
    `test_an_empty_payload_costs_no_child_process` replaced proved nothing."""
    out = tmp_path / "payload.txt"
    script = tmp_path / "console_probe.ps1"
    script.write_text(
        ". %s\n"
        "Set-Content -LiteralPath %s -Encoding ascii "
        "-Value ('redirected=' + [Console]::IsInputRedirected)\n"
        "$p = Get-MindhivePayload\n"
        "Add-Content -LiteralPath %s -Encoding ascii -Value ('payload=[' + $p + ']')\n"
        % (ps_quote(LIB), ps_quote(out), ps_quote(out)), encoding="ascii")

    def recorded():
        return (out.read_text(encoding="ascii", errors="replace").splitlines()
                if out.exists() else [])

    try:
        r = run_ps_on_a_console(script)
    except subprocess.TimeoutExpired:
        pytest.fail("Get-MindhivePayload blocked on a console stdin; "
                    "probe recorded %r" % recorded())
    lines = recorded()
    assert r.returncode == 0, lines
    assert lines[:1] == ["redirected=False"], \
        "the launcher did not give the child a console stdin, " \
        "so this proves nothing: %r" % lines
    assert lines[1:] == ["payload=[]"], lines


@windows_only
@pytest.mark.parametrize("stem", ps_shims())
def test_no_shim_blocks_on_a_real_console_stdin(tmp_path, stem):
    """The case the hang guard exists for, end to end: a shim run by hand from
    a terminal. A guard missing from a shim (the inline
    `if (-not [Console]::IsInputRedirected)`) or from `Get-MindhivePayload`
    blocks until the timeout kills it.

    Unlike the test `test_an_empty_payload_costs_no_child_process` replaced,
    the interpreter RESOLVES (a logging stub), so every shim gets past
    `if (-not $py)` and actually reaches its read. Two expectations, both
    discriminating:
      * a shim in `WORK_WITHOUT_PAYLOAD` must get past the read and dispatch.
        A shim that exited early would pass "did not block" and fail this.
      * the rest have nothing to do without a payload and must spawn no child.

    `run_ps_on_a_console` is proven to hand over a console stdin by
    `test_get_mindhive_payload_returns_empty_on_a_console_stdin`, which uses the
    same launcher and records `IsInputRedirected`. A shim's own stdout lands on
    the hidden console, so the argv log is the evidence here."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = stub_interpreter(tmp_path, log)

    def seen():
        return log.read_text(encoding="ascii", errors="replace") if log.exists() else ""

    try:
        r = run_ps_on_a_console(dest / (stem + ".ps1"),
                                env={"MINDHIVE_PYTHON": str(stub)})
    except subprocess.TimeoutExpired:
        pytest.fail("%s blocked reading a console stdin; argv log %r" % (stem, seen()))
    assert r.returncode == 0
    if stem in WORK_WITHOUT_PAYLOAD:
        assert WORK_WITHOUT_PAYLOAD[stem] in seen(), \
            "%s never got past its payload read: %r" % (stem, seen())
    else:
        assert seen().strip() == "", \
            "%s spawned a child with no payload: %r" % (stem, seen())


# --- the canary: the shims really do dispatch ----------------------------------

@windows_only
@pytest.mark.parametrize("stem,expect", [
    ("post_tool_use", "hook post_tool_use"),
    ("post_tool_use_failure", "hook post_tool_use_failure"),
    ("pre_tool_use", "hook pre_tool_use"),
    ("user_prompt_submit", "hook user_prompt_submit"),
    ("stop", "hook stop"),
    ("session_start", "append session_start"),
    ("session_end", "append session_end"),
    ("pre_compact", "append compaction"),
    ("autosync", "autosync"),
])
def test_the_shims_dispatch_the_expected_stage(tmp_path, stem, expect):
    """THE CANARY for every silence assertion above. All of them are satisfied
    by a family that does nothing at all; this is the one that is not.

    Run against a stub that records its argv rather than the real CLI: the
    shim's job is dispatch, and a suite paying ~1s of real CLI startup per
    assertion is one nobody runs."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = stub_interpreter(tmp_path, log)
    r = run_ps(dest / (stem + ".ps1"), stdin=PAYLOAD,
               env={"MINDHIVE_PYTHON": str(stub)})
    assert r.returncode == 0, r.stderr
    seen = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    assert expect in seen, "%s did not dispatch %r; argv log: %r" % (stem, expect, seen)


@windows_only
def test_the_session_stamping_shims_pass_the_whitelisted_id(tmp_path):
    """A hostile id in the payload must not reach the CLI's `--session`."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = stub_interpreter(tmp_path, log)
    run_ps(dest / "session_end.ps1",
           stdin=json.dumps({"session_id": "../../pwned"}),
           env={"MINDHIVE_PYTHON": str(stub)})
    seen = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    assert "pwned" not in seen
    assert "--session unknown" in seen, seen


@windows_only
def test_pre_compact_still_prints_its_one_human_line(tmp_path):
    """`pre_compact` is the one shim whose stdout is meant for a person. A
    blanket "every shim is silent" rule would have deleted it."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = stub_interpreter(tmp_path, log)
    r = run_ps(dest / "pre_compact.ps1", stdin=PAYLOAD,
               env={"MINDHIVE_PYTHON": str(stub)})
    assert r.returncode == 0
    assert REMINDER in r.stdout


@windows_only
def test_stop_never_emits_a_block_decision(tmp_path):
    """For a Stop hook, `block` means *do not stop — keep working*: it would pin
    a full session open instead of rotating it, which is the exact opposite of
    the intent. CLAUDE.md states this as a rule; this asserts it."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = stub_interpreter(tmp_path, log)
    for env in ({"MINDHIVE_PYTHON": str(stub)},
                {"MINDHIVE_PYTHON": r"C:\definitely\not\here.exe",
                 "MINDHIVE_HOOK_WALK_LIMIT": "1"}):
        r = run_ps(dest / "stop.ps1", stdin=PAYLOAD, env=env)
        assert r.returncode == 0
        assert "block" not in r.stdout.lower()


# --- stdout discipline: a child that PRINTS ------------------------------------
#
# `stop` forwards its child's stdout by design (that is how its advice reaches
# Claude Code), so `test_stop_never_emits_a_block_decision` can only catch a
# `block` the shim writes itself, never one its child writes. That half of the
# rule belongs to the Python side. What a shim CAN get wrong is below: forwarding
# output from a call whose stdout is not a channel at all, or swallowing output
# from one whose stdout is the only channel it has.

@windows_only
@pytest.mark.parametrize("stem", SILENT_STAGES)
def test_a_silent_stage_prints_nothing_even_when_the_child_does(tmp_path, stem):
    """These stages have no decision channel, so Claude Code would read anything
    they print as if it were one, and `post_tool_use` fires on EVERY tool call.

    `SILENT_STAGES` was defined in this file and used nowhere. Every child was
    `stub_interpreter`, which writes to a FILE, so no test ever ran a shim whose
    child printed, and `| Out-Null` could be deleted from any of these with the
    suite unchanged. The POSIX sibling has asserted this all along
    (`tests/test_hook_shims.py`, `FAKE_STDOUT`).

    The log assertion keeps the empty stdout honest: it proves the child RAN,
    so the silence is the shim swallowing output, not output that was never
    produced. `test_a_decision_stage_passes_the_childs_stdout_through` proves
    the stub's line does reach a shim's stdout when nothing swallows it."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = printing_interpreter(tmp_path, log)
    r = run_ps(dest / (stem + ".ps1"), stdin=PAYLOAD,
               env={"MINDHIVE_PYTHON": str(stub)})
    assert r.returncode == 0, r.stderr
    seen = log.read_text(encoding="ascii", errors="replace") if log.exists() else ""
    assert "-m mindhive" in seen, \
        "%s never ran its child, so empty stdout proves nothing: %r" % (stem, seen)
    assert r.stdout == "", "%s put %r on stdout" % (stem, r.stdout)


@windows_only
@pytest.mark.parametrize("stem", DECISION_STAGES)
def test_a_decision_stage_passes_the_childs_stdout_through(tmp_path, stem):
    """CANARY for every printing-child assertion in this section, and a
    contract of its own: for these stages stdout IS the channel, so the child's
    answer has to reach Claude Code. Without it, a printing stub whose line
    never reached any shim's stdout would make every silence assertion pass
    vacuously.

    `stop` is included. Its rotation advice reaches Claude Code on this channel
    and no other, so an `| Out-Null` copied over from a silent sibling would
    make rotation a silent no-op with every other test in this file green. The
    stub's line happens to be decision-shaped; here it is only a marker.
    Whether `hook stop` may ever print `block` is the Python side's rule, and
    `test_stop_never_emits_a_block_decision` covers what the shim writes
    itself."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = printing_interpreter(tmp_path, log)
    r = run_ps(dest / (stem + ".ps1"), stdin=PAYLOAD,
               env={"MINDHIVE_PYTHON": str(stub)})
    assert r.returncode == 0, r.stderr
    assert CHILD_STDOUT + "-m mindhive hook " + stem in r.stdout, r.stdout


@windows_only
def test_session_start_forwards_its_context_but_swallows_the_append(tmp_path):
    """`session_start`'s stdout is the context injected into the new session,
    so it is neither silent nor a plain pass-through. `directives` and `state`
    must reach it; the `append session_start` bookkeeping call must not. Only a
    child that PRINTS can tell those apart, and the argv on each printed line
    says which call is which."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = printing_interpreter(tmp_path, log)
    r = run_ps(dest / "session_start.ps1", stdin=PAYLOAD,
               env={"MINDHIVE_PYTHON": str(stub)})
    assert r.returncode == 0, r.stderr
    seen = log.read_text(encoding="ascii", errors="replace") if log.exists() else ""
    assert "append session_start" in seen, seen
    # The BRIEF forms, which keep the hook's stdout under the session-start cap
    # (`mindhive.sessionstart`). The closing quote pins the whole argv, so a
    # shim that went back to the full forms fails here.
    assert CHILD_STDOUT + '-m mindhive directives --brief"' in r.stdout, r.stdout
    assert CHILD_STDOUT + '-m mindhive state --brief"' in r.stdout, r.stdout
    assert CHILD_STDOUT + "-m mindhive append" not in r.stdout, r.stdout


@windows_only
def test_pre_compact_prints_only_its_reminder_even_when_the_child_prints(tmp_path):
    """`pre_compact`'s stdout is exactly one line, the reminder to a person.
    The ledger half runs a child, and whatever that child prints must not ride
    along with the reminder."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = printing_interpreter(tmp_path, log)
    r = run_ps(dest / "pre_compact.ps1", stdin=PAYLOAD,
               env={"MINDHIVE_PYTHON": str(stub)})
    assert r.returncode == 0, r.stderr
    seen = log.read_text(encoding="ascii", errors="replace") if log.exists() else ""
    assert "append compaction" in seen, seen
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1 and REMINDER in lines[0], r.stdout


# --- regressions: the defects an adversarial review found in this branch -------
#
# Every test below pins a fix for a bug that shipped in the first cut of this
# work. They are grouped rather than scattered because they share one shape:
# each one is a way for the hook family to fail SILENTLY and TOTALLY on a
# machine that is not the author's, which is the exact class of bug the branch
# exists to remove.

@windows_only
def test_the_store_stub_does_not_shadow_a_real_interpreter(tmp_path):
    """THE serious one. Windows ships the App Execution Alias redirector in
    `%LOCALAPPDATA%\\Microsoft\\WindowsApps`, and on a default profile that
    directory sits on PATH AHEAD of a per-user Python install. The first cut
    took `@(Get-Command ...)[0]`, found the stub, refused it -- correctly -- and
    then answered `$null` without looking at the real interpreter one entry
    further down. Refusing the stub has to be a SKIP, not a STOP.

    Reproduced against the pre-fix loop before this was written: same fixture,
    `$null`."""
    stub = fake_exe(tmp_path / "WindowsApps" / "python.exe")
    real = fake_exe(tmp_path / "realbin" / "python.exe")
    r = eval_ps("Write-Output (Get-MindhivePython)", tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": str(tmp_path / "nowhere"),
                     "MINDHIVE_HOOK_WALK_LIMIT": "1",
                     "PATH": "%s;%s;C:\\Windows\\System32"
                             % (stub.parent, real.parent)})
    got = r.stdout.strip()
    assert got == str(real), "resolved %r (stub at %s)" % (got, stub)


@windows_only
def test_a_store_stub_alone_still_resolves_to_nothing(tmp_path):
    """CANARY for the test above: if `-All` had simply stopped refusing the
    stub, that test would still pass. This one fails unless the refusal is
    intact."""
    stub = fake_exe(tmp_path / "WindowsApps" / "python.exe")
    r = eval_ps("Write-Output ('[' + (Get-MindhivePython) + ']')",
                tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_DIR": str(tmp_path / "nowhere"),
                     "MINDHIVE_HOOK_WALK_LIMIT": "1",
                     "PATH": "%s;C:\\Windows\\System32" % stub.parent})
    assert r.stdout.strip() == "[]", r.stdout


@windows_only
@pytest.mark.parametrize("junk", ["99999999999999999999", "9999999999", "",
                                  "abc", "12abc", "-4", "3.5"])
def test_a_junk_walk_limit_falls_back_instead_of_throwing(tmp_path, junk):
    """An open-ended digit run sent a value past 2147483647 into `[int]`, and a
    failed cast is quiet only under the shims' `SilentlyContinue` (measured on
    5.1): `eval_ps` runs under the default `Continue`, where it printed its
    error record on stderr, and under `circle.ps1`'s `Stop` it ended the
    script. So the one input this guard exists to absorb reached the user's
    screen. The digit run is bounded now; ten digits is the first length past
    the bound.

    `0` is not junk and is not in this list: see
    `test_a_zero_walk_limit_means_do_not_walk`."""
    r = eval_ps("Write-Output (Get-MindhiveWalkLimit)", tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_WALK_LIMIT": junk})
    assert r.returncode == 0, r.stderr
    assert r.stderr.strip() == "", r.stderr
    assert r.stdout.strip() == "8", r.stdout


@windows_only
def test_a_real_walk_limit_is_still_honoured(tmp_path):
    """CANARY: without this, a `Get-MindhiveWalkLimit` hard-wired to 8 would
    pass every case above."""
    r = eval_ps("Write-Output (Get-MindhiveWalkLimit)", tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_WALK_LIMIT": "3"})
    assert r.stdout.strip() == "3", r.stdout


@windows_only
def test_the_session_id_key_is_matched_case_sensitively(tmp_path):
    """PowerShell property access is case-INSENSITIVE, so `$parsed.session_id`
    accepted a payload carrying `Session_ID` that `hooks/lib.sh` and the Python
    side both ignore. Three whitelists that are meant to agree have to agree
    about the KEY, not only about which values pass it."""
    r = eval_ps("Write-Output (Get-MindhiveSessionId '{\"Session_ID\":\"sneaky\"}')",
                tmp_path=tmp_path)
    assert r.stdout.strip() == "unknown", r.stdout


@windows_only
def test_a_key_that_only_looks_like_session_id_is_not_read(tmp_path):
    """`-ceq` is case-sensitive but LINGUISTIC: under it a key carrying a soft
    hyphen (U+00AD) compares equal to `session_id`, while `lib.sh`'s
    `.get("session_id")` and the Python side see a different key (measured by
    review). The comparison is ordinal now, so the three agree about the key on
    every byte, not only on case. The key is built inside PowerShell, so the
    probe file stays ASCII."""
    r = eval_ps("$raw = '{\"session' + [char]0xAD + '_id\":\"abc123\"}'; "
                "Write-Output (Get-MindhiveSessionId $raw)", tmp_path=tmp_path)
    assert r.stdout.strip() == "unknown", r.stdout


@windows_only
def test_a_library_that_parses_but_defines_nothing_is_a_silent_no_op(tmp_path):
    """The `Get-Command Get-MindhivePython` guard in every shim had no test at
    all: the two library fixtures were "absent" and "unparseable", and neither
    reaches it. A library that parses cleanly and simply defines nothing is the
    case that does -- a half-written or truncated `lib.ps1`."""
    dest = _copy_family(tmp_path, lib_text="# a library with no functions\r\n")
    for stem in ps_shims():
        assert_quiet(stem, run_ps(dest / (stem + ".ps1"), stdin=PAYLOAD))


@windows_only
def test_a_checkout_path_containing_a_bracket_still_finds_the_library(tmp_path):
    """`Test-Path $lib` treats `[` and `]` as a wildcard character class, so a
    clone under `C:/src/[wip]/mindhive` answered $false for a file sitting
    right there and every shim in the family became a no-op -- the whole
    integration dead, with nothing printed anywhere.

    DISCRIMINATING by dispatch, not by exit code: a shim that silently does
    nothing also exits 0, so the assertion is that the interpreter was actually
    invoked."""
    weird = tmp_path / "s[wip]rc"
    weird.mkdir()
    dest = _copy_family(weird)
    log = weird / "argv.log"
    stub = stub_interpreter(weird, log)
    r = run_ps(dest / "post_tool_use.ps1", stdin=PAYLOAD,
               env={"MINDHIVE_PYTHON": str(stub)})
    assert r.returncode == 0, r.stderr
    assert log.exists(), "the library under a bracketed path was never found"
    assert "hook post_tool_use" in log.read_text(encoding="ascii")


@windows_only
def test_pre_compact_still_reminds_the_human_with_no_interpreter(tmp_path):
    """A REGRESSION this branch introduced and this test pins. The reminder
    needs no interpreter -- it is a sentence addressed to a person -- but the
    new resolution gate exited before printing it, so on any machine where
    nothing resolved the operator silently stopped being told to fold the
    session. Only the LEDGER half may be conditional."""
    dest = _copy_family(tmp_path, with_lib=False)
    r = run_ps(dest / "pre_compact.ps1", stdin=PAYLOAD)
    assert r.returncode == 0, r.stderr
    assert REMINDER in r.stdout, r.stdout

    dest2 = _copy_family(tmp_path / "second")
    r2 = run_ps(dest2 / "pre_compact.ps1", stdin=PAYLOAD,
                env={"MINDHIVE_PYTHON": r"C:\definitely\not\here.exe",
                     "MINDHIVE_HOOK_WALK_LIMIT": "1",
                     "PATH": r"C:\Windows\System32;C:\Windows"})
    assert r2.returncode == 0, r2.stderr
    assert REMINDER in r2.stdout, r2.stdout


# --- Resolve-MindHivePython: the MEB's name for Get-MindhivePython (Bug 1) -----
#
# MEB HOOK-02, spec 10.13.1 and the `windows-powershell-patterns` skill call the
# resolver `Resolve-MindHivePython`; this library shipped it as
# `Get-MindhivePython`, and every shim calls that. The binding plan answers with
# a delegating function, not a second resolver: spec 10.13.1's standalone body
# disagrees with `Get-MindhivePython` on most of the tiers pinned above (a bare
# `Test-Path` that accepts a directory, a fixed five-level walk from
# `$MyInvocation`, `python.exe` handed back when nothing resolves), and two
# implementations of one contract is the drift this library was written to
# end. Each test compares the two names on ONE fixture and asserts the file as
# well, so two functions broken the same way cannot pass as agreement.

def _windowed_venv(tmp_path):
    """A venv whose `Scripts\\` holds both leaves, so `-Windowed` changes the
    answer. Answers `(console, windowless, env)`. `MINDHIVE_PYTHON` stays
    unset: the override wins before the leaf list is even built, so under it
    `-Windowed` changes nothing and no comparison could see the switch."""
    repo = tmp_path / "repo"
    console = fake_exe(repo / ".venv" / "Scripts" / "python.exe")
    windowless = fake_exe(repo / ".venv" / "Scripts" / "pythonw.exe")
    hooks = repo / "hooks"
    hooks.mkdir(parents=True)
    env = {"MINDHIVE_HOOK_DIR": str(hooks), "MINDHIVE_HOOK_WALK_LIMIT": "2",
           "PATH": r"C:\Windows\System32;C:\Windows"}
    return console, windowless, env


@windows_only
def test_resolve_mindhive_python_answers_exactly_what_get_mindhive_python_does(tmp_path):
    """One probe, four settings, both names, and each row's file asserted too:
      * `override` -- `MINDHIVE_PYTHON` names a runnable file, which beats the
        venv the walk would also reach.
      * `override-is-a-directory` -- spec 10.13.1's bare `Test-Path` takes the
        directory; both names must refuse it and walk on to the venv.
      * `venv` -- no override, so the walk answers.
      * `nothing` -- no override, no venv in reach, no interpreter on PATH:
        both answer NOTHING. Spec 10.13.1 returns `python.exe` here, a bare
        name `&` would look up at call time -- the Store redirector included --
        where `$null` lets every shim's `if (-not $py)` exit 0 in silence."""
    repo = tmp_path / "repo"
    venv = fake_exe(repo / ".venv" / "Scripts" / "python.exe")
    hooks = repo / "hooks"
    hooks.mkdir(parents=True)
    override = fake_exe(tmp_path / "custom" / "python.exe")
    folder = tmp_path / "notaninterpreter"
    folder.mkdir()
    isolated = tmp_path / "iso" / "hooks"
    isolated.mkdir(parents=True)
    rows = [
        # (id, MINDHIVE_PYTHON -- None means unset, MINDHIVE_HOOK_DIR, expected)
        ("override", override, hooks, str(override)),
        ("override-is-a-directory", folder, hooks, str(venv)),
        ("venv", None, hooks, str(venv)),
        ("nothing", tmp_path / "absent.exe", isolated, ""),
    ]
    body = []
    for row, value, hook_dir, _ in rows:
        if value is None:
            body.append("Remove-Item Env:MINDHIVE_PYTHON -ErrorAction SilentlyContinue")
        else:
            body.append("$env:MINDHIVE_PYTHON = %s" % ps_quote(value))
        body.append("$env:MINDHIVE_HOOK_DIR = %s" % ps_quote(hook_dir))
        body.append("Write-Output ('%s|' + (Get-MindhivePython) + '|' + "
                    "(Resolve-MindHivePython))" % row)
    r = eval_ps("\n".join(body), tmp_path=tmp_path,
                env={"MINDHIVE_HOOK_WALK_LIMIT": "2",
                     "PATH": r"C:\Windows\System32;C:\Windows"})
    assert r.stderr.strip() == "", r.stderr
    got = [line.split("|") for line in r.stdout.splitlines() if line.strip()]
    assert got == [[row, want, want] for row, _, _, want in rows], r.stdout


@windows_only
def test_resolve_mindhive_python_forwards_windowed(tmp_path):
    """`-Windowed` reaches `Get-MindhivePython`: the windowless interpreter
    `autosync.ps1` asks for is the same file under either name.

    CANARY in the same probe: `Resolve-DroppingWindowed`, a delegation that
    forgets its arguments, is asked for `-Windowed` and must answer the CONSOLE
    leaf. That proves this fixture tells the two leaves apart, so the assertion
    on `Resolve-MindHivePython -Windowed` is one a dropped switch would fail."""
    console, windowless, env = _windowed_venv(tmp_path)
    r = eval_ps("function Resolve-DroppingWindowed { Get-MindhivePython }\n"
                "Write-Output ('GET|' + (Get-MindhivePython -Windowed))\n"
                "Write-Output ('RESOLVE|' + (Resolve-MindHivePython -Windowed))\n"
                "Write-Output ('DROPPED|' + (Resolve-DroppingWindowed -Windowed))",
                tmp_path=tmp_path, env=env)
    assert r.stderr.strip() == "", r.stderr
    assert r.stdout.splitlines() == ["GET|%s" % windowless,
                                     "RESOLVE|%s" % windowless,
                                     "DROPPED|%s" % console], r.stdout


@windows_only
def test_resolve_mindhive_python_honours_an_explicit_windowed_false(tmp_path):
    """`-Windowed:$false` means the console interpreter, exactly as it does for
    `Get-MindhivePython`; a caller that computes the switch
    (`-Windowed:$scheduled`) depends on it.

    The plan's one-liner, `function Resolve-MindHivePython { Get-MindhivePython
    @args }`, answers `pythonw.exe` here on 5.1 (measured while this was
    written): splatting `$args` re-binds `-Windowed:` as the bare, PRESENT
    switch and passes the `$false` on as a stray positional, which
    `Get-MindhivePython` -- no `[CmdletBinding()]` -- collects into its own
    `$args` without a word. The `$true` and bare rows are the canary: they
    prove the windowless leaf is reachable, so the console answer on the
    `$false` row is the switch being honoured, not a leaf the walk never saw."""
    console, windowless, env = _windowed_venv(tmp_path)
    r = eval_ps("Write-Output ('FALSE|' + (Resolve-MindHivePython -Windowed:$false))\n"
                "Write-Output ('TRUE|' + (Resolve-MindHivePython -Windowed:$true))\n"
                "Write-Output ('BARE|' + (Resolve-MindHivePython -Windowed))",
                tmp_path=tmp_path, env=env)
    assert r.stderr.strip() == "", r.stderr
    assert r.stdout.splitlines() == ["FALSE|%s" % console,
                                     "TRUE|%s" % windowless,
                                     "BARE|%s" % windowless], r.stdout


@windows_only
def test_resolve_mindhive_python_is_a_function_the_library_defines(tmp_path):
    """Dot-sourcing `lib.ps1` defines the name, and as a FUNCTION, the shape
    the plan specifies: a `Set-Alias` to `Get-MindhivePython` would answer
    `Alias` here."""
    r = eval_ps("Write-Output (Get-Command Resolve-MindHivePython).CommandType",
                tmp_path=tmp_path)
    assert r.stderr.strip() == "", r.stderr
    assert r.stdout.strip() == "Function", r.stdout


# --- session_start checks the plugin cache (spec Bug 8) -------------------------
#
# The `.ps1` half of the same section in `tests/test_hook_shims.py`: Claude Code
# runs a plugin from its copy under `~/.claude/plugins/cache/`, and
# `mindhive plugins cache-check --hook` says so -- one line, only on drift --
# when that copy is not this checkout's. `session_start.ps1` must run it after
# `state`, forward its line the way it forwards `state`'s, and keep a failure of
# it off the hook's stderr.

CACHE_CHECK = "-m mindhive plugins cache-check --hook"
# The brief forms the hook prints, as in `tests/test_hook_shims.py`.
DIRECTIVES = "-m mindhive directives --brief"
STATE = "-m mindhive state --brief"


def session_start_interpreter(tmp_path: Path, log: Path) -> Path:
    """An 'interpreter' for `session_start`: each call appends its argv to *log*
    and prints `child stdout: <argv>`, so the hook's stdout shows which calls
    it forwarded, in order. With `STUB_FAIL` set, the call whose fourth
    argument equals it prints `child stderr: <argv>` on stderr and exits 3
    instead. Each redirection LEADS its `echo`, so an argv ending in a digit
    (`--max-age-hours 24`) cannot be read as a handle number, and no trailing
    space reaches the log."""
    stub = tmp_path / "session_start_stub.cmd"
    stub.write_text(
        "@echo off\r\n"
        ">>\"%s\" echo %%*\r\n"
        "if defined STUB_FAIL if \"%%4\"==\"%%STUB_FAIL%%\" (\r\n"
        "    >&2 echo child stderr: %%*\r\n"
        "    exit /b 3\r\n"
        ")\r\n"
        "echo child stdout: %%*\r\n" % log,
        encoding="ascii")
    return stub


def _calls(log: Path) -> list:
    return (log.read_text(encoding="ascii", errors="replace").splitlines()
            if log.exists() else [])


@windows_only
def test_session_start_checks_the_plugin_cache_right_after_state(tmp_path):
    """The check is the very next call after `state`, and its answer reaches
    the hook's stdout -- the new session's context -- right after the state it
    qualifies, the way `state`'s own answer does. The stub prints for EVERY
    call, so the exact stdout also pins what is not forwarded: the `append`
    bookkeeping and the detached background calls."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = session_start_interpreter(tmp_path, log)
    r = run_ps(dest / "session_start.ps1", stdin=PAYLOAD,
               env={"MINDHIVE_PYTHON": str(stub)})
    assert r.returncode == 0, r.stderr
    calls = _calls(log)
    assert STATE in calls, calls
    at = calls.index(STATE)
    assert calls[at + 1:at + 2] == [CACHE_CHECK], calls
    assert r.stdout.splitlines() == ["child stdout: " + DIRECTIVES,
                                     "child stdout: " + STATE,
                                     "child stdout: " + CACHE_CHECK], r.stdout


@windows_only
def test_a_failing_plugin_cache_check_leaves_session_start_quiet_and_exit_0(tmp_path):
    """`--hook` is fail-open on the Python side, and the shim does not lean on
    that: a check that dies loudly -- a line on stderr, exit 3 -- costs the
    session its one line and nothing else. The hook exits 0, prints nothing on
    stderr, and forwards exactly what it forwards without the check. The log
    proves the check RAN, so the quiet is the shim's doing rather than a call
    that never happened, and the stub run bare proves the failure is loud."""
    dest = _copy_family(tmp_path)
    log = tmp_path / "argv.log"
    stub = session_start_interpreter(tmp_path, log)
    env = {"MINDHIVE_PYTHON": str(stub), "STUB_FAIL": "cache-check"}
    r = run_ps(dest / "session_start.ps1", stdin=PAYLOAD, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stderr == "", r.stderr
    assert CACHE_CHECK in _calls(log), _calls(log)
    assert r.stdout.splitlines() == ["child stdout: " + DIRECTIVES,
                                     "child stdout: " + STATE], r.stdout
    bare = subprocess.run([str(stub)] + CACHE_CHECK.split(), capture_output=True,
                          text=True, env=clean_env(env), timeout=30)
    assert (bare.returncode, bare.stdout, bare.stderr) == (
        3, "", "child stderr: " + CACHE_CHECK + "\n")
