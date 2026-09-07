# tests/test_hook_shims.py
"""The POSIX hook shims — TODOS item 6, and the drift guard that keeps them honest.

`hooks/` held nine `.ps1` files and zero `.sh`, so session rotation, telemetry
capture and SOP injection were Windows-only. This suite covers the POSIX family
that closes that, and it is written against the one property that matters more
than any feature in it.

## Why these tests are shaped around failure, not function

A Claude Code hook registered in `~/.claude/settings.json` fires in EVERY
session on the machine, in every project. The project's CLAUDE.md puts it
plainly: a bug here "does not break one feature, it breaks all work
everywhere, and it breaks it from inside the tool you would use to fix it."

So the assertions below are mostly about what a shim must NOT do — exit
non-zero, print on a decision channel, block on a read, leak a child's stderr —
and they are driven by inputs designed to break it: no payload, garbage
payload, no interpreter, an interpreter that is really the Windows Store
redirector, a library that is missing or does not parse.

One of those was found by review after the first green run, and it is the
reason the preamble reads the way it does: `.` is a special builtin, and a
failed `.` ends a non-interactive shell BEFORE the `|| exit 0` on the same line
is reached (measured: exit 1 under bash's sh, 2 under dash). For the Stop hook
2 means "keep working"; for PreToolUse it means "deny". So every shim proves
the library loads in a subshell before it sources it, and
`test_a_shim_with_no_library_is_a_silent_no_op` is what holds that in place.

## The parity test is the one that keeps this from rotting

Two families implementing one contract drift the moment somebody edits one of
them. `test_every_powershell_hook_has_a_posix_sibling` and
`test_the_two_families_call_the_same_stage` fail on that drift rather than
letting the POSIX half quietly fall a release behind.

## The fake interpreter

Most tests run the shims against a stub `python` that records its argv instead
of the real `python -m mindhive`, because the shim's job is dispatch, not what
the CLI does with the payload — and a suite that paid ~1s of real CLI startup
per assertion would be one nobody runs. `-c` is delegated to the real
interpreter so `mindhive_session_id`'s JSON parsing is exercised for real.
`test_the_capture_hook_really_appends_an_event` closes the loop end to end
against the actual CLI.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "hooks"

# `lib.sh` is sourced, never invoked, so it is the one POSIX file with no
# PowerShell sibling. Named here rather than pattern-matched so adding a second
# un-paired file is a deliberate edit to this list.
NOT_A_SHIM = {"lib.sh"}

# Which stage each shim hands to `mindhive hook`. Four shims call ordinary
# `hive` commands instead and are listed as None.
STAGE = {
    "post_tool_use": "post_tool_use",
    "post_tool_use_failure": "post_tool_use_failure",
    "pre_tool_use": "pre_tool_use",
    "user_prompt_submit": "user_prompt_submit",
    "stop": "stop",
    "autosync": None,
    "pre_compact": None,
    "session_start": None,
    "session_end": None,
}

# Stages whose stdout IS a channel Claude Code reads. Everything else must be
# silent, and `pre_compact` prints one fixed human line.
DECISION_STAGES = ("pre_tool_use", "user_prompt_submit", "stop")
SILENT_STAGES = ("post_tool_use", "post_tool_use_failure", "autosync",
                 "session_end")

PAYLOAD = ('{"session_id":"sh-test-1","tool_name":"Bash",'
           '"tool_input":{"command":"git status"},'
           '"tool_response":{"stdout":"ok"}}')

REMINDER = "MIND HIVE: before compaction, fold this session"

sh_missing = pytest.mark.skipif(shutil.which("sh") is None,
                                reason="no POSIX sh on this machine")

# `subprocess` resolves a bare program name through the PATH of the env it is
# GIVEN (`os.get_exec_path(env)`), so a test that hands a shim a private PATH
# would lose `sh` itself on POSIX. Resolve it once, through the real PATH.
SH = shutil.which("sh") or "sh"


# --- helpers -------------------------------------------------------------------

def shims() -> list:
    """Every `.sh` in `hooks/` that is a shim, by stem."""
    return sorted(p.stem for p in HOOKS.glob("*.sh")
                  if p.name not in NOT_A_SHIM)


def _coreutils_dir() -> str:
    """Where `cat` lives. The shims use real binaries, so a test that empties
    PATH entirely breaks the shim before it reaches the code under test."""
    found = shutil.which("cat")
    return str(Path(found).parent) if found else "/usr/bin"


def _path_without_python(tmp_path) -> str:
    """A PATH holding the three binaries the shims exec -- `cat`, `dirname`,
    `tr` -- and no python at all.

    `_coreutils_dir()` alone is not that on Linux: `cat` lives in /usr/bin and
    so does python3, so every "no interpreter" scenario built on it found the
    system interpreter and proved nothing. Measured on Ubuntu 24.04 (dash):
    the directory-in-MINDHIVE_PYTHON test answered `/usr/bin/python3`, rc=0,
    while the same test passed on Windows only because Git's `usr/bin` ships
    no python. So on POSIX the tools are linked into a private directory; on
    Windows Git's `usr/bin` is used as before, and the test names the reason
    if that directory ever grows an interpreter."""
    if os.name == "nt":
        tools = _coreutils_dir()
        for name in ("python", "python3"):
            if shutil.which(name, path=tools):
                pytest.skip(f"{tools} carries {name}; cannot build a "
                            "python-free PATH here")
        return tools
    tools = tmp_path / "tools-without-python"
    tools.mkdir()
    for name in ("cat", "dirname", "tr"):
        found = shutil.which(name)
        if found is None:
            pytest.skip(f"no `{name}` on this machine")
        (tools / name).symlink_to(found)
    return str(tools)


def run_shim(name, payload=PAYLOAD, env=None, stdin=True, timeout=60):
    """Run one shim under `sh` and answer its `CompletedProcess`.

    `timeout` is a real assertion in disguise: a shim that blocks on a read is
    a shim that hangs every session on the machine, so the timeout expiring is
    a test failure rather than a slow test."""
    child = dict(os.environ)
    child.pop("MINDHIVE_HEADLESS", None)
    # Never let a test arm rotation. The stop hook is inert without it and must
    # stay that way while a suite is driving it.
    child.pop("MINDHIVE_ROTATION", None)
    child.update(env or {})
    return subprocess.run(
        [SH, str(HOOKS / f"{name}.sh")],
        input=(payload if stdin else ""), text=True, shell=False,
        capture_output=True, timeout=timeout, cwd=str(REPO), env=child)


def probe(script, env=None):
    """Source `lib.sh` and run `script` against it, with the operator override
    and the walk limit cleared so an ambient value cannot decide the result."""
    child = dict(os.environ)
    for key in ("MINDHIVE_PYTHON", "MINDHIVE_HOOK_WALK_LIMIT", "MINDHIVE_HEADLESS"):
        child.pop(key, None)
    child.update(env or {})
    return subprocess.run([SH, "-c", script], capture_output=True, text=True,
                          shell=False, cwd=str(REPO), env=child, timeout=30)


@pytest.fixture
def fake_python(tmp_path):
    """A stub interpreter that logs its argv, and the log path.

    `-c` is delegated to the real interpreter: `mindhive_session_id` parses the
    payload's JSON that way, and stubbing it would test the stub."""
    log = tmp_path / "argv.log"
    exe = tmp_path / "fake-python"
    # Two records per call. The joined line is what most tests read; the
    # `arg:` lines preserve the argv BOUNDARIES, which is the only way to prove
    # a crafted session id arrived as one argument rather than as flags of its
    # own (`test_a_payload_that_tries_to_inject_a_flag_is_carried_as_data`).
    exe.write_text(
        "#!/bin/sh\n"
        'printf "call %s\\n" "$*" >> "$FAKE_LOG"\n'
        'for a in "$@"; do printf "arg %s\\n" "$a" >> "$FAKE_LOG"; done\n'
        'printf "env %s\\n" "${MINDHIVE_SESSION-<unset>}" >> "$FAKE_LOG"\n'
        'if [ "$1" = "-c" ]; then shift; exec "$FAKE_REAL_PY" -c "$@"; fi\n'
        'if [ -n "$FAKE_STDOUT" ]; then printf "%s\\n" "$FAKE_STDOUT"; fi\n'
        'if [ -n "$FAKE_STDERR" ]; then printf "%s\\n" "$FAKE_STDERR" >&2; fi\n'
        'exit ${FAKE_EXIT:-0}\n',
        encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    return {"exe": str(exe), "log": log,
            "env": {"MINDHIVE_PYTHON": str(exe), "FAKE_LOG": str(log),
                    "FAKE_REAL_PY": sys.executable}}


def logged(fake) -> list:
    """The joined command line of every call the shim made."""
    return [ln[5:] for ln in _lines(fake) if ln.startswith("call ")]


def argv_of(fake) -> list:
    """Every individual argument, boundaries intact."""
    return [ln[4:] for ln in _lines(fake) if ln.startswith("arg ")]


def session_env_of(fake) -> list:
    """The MINDHIVE_SESSION each child saw in its environment."""
    return [ln[4:] for ln in _lines(fake) if ln.startswith("env ")]


def code_lines(name) -> list:
    """A shim's lines with the commentary removed: whole-line comments dropped,
    and a trailing `   # …` stripped. Static sweeps read these so a word in a
    comment ("the source files touched") cannot trip a check meant for code."""
    out = []
    for ln in (HOOKS / name).read_text(encoding="utf-8").splitlines():
        if ln.lstrip().startswith("#"):
            continue
        out.append(ln.split("   #")[0])
    return out


def _lines(fake) -> list:
    try:
        return [ln for ln in fake["log"].read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    except OSError:
        return []


# --- the drift guard -----------------------------------------------------------

def test_every_powershell_hook_has_a_posix_sibling():
    """TODOS item 6, asserted rather than described.

    This is the test that stops the POSIX family from quietly falling behind:
    a tenth `.ps1` added later fails here until its `.sh` exists."""
    ps1 = {p.stem for p in HOOKS.glob("*.ps1")}
    sh = set(shims())
    assert ps1 - sh == set(), f"PowerShell hooks with no POSIX sibling: {sorted(ps1 - sh)}"


def test_every_posix_shim_has_a_powershell_sibling():
    """The other direction, so the two families cannot diverge either way."""
    ps1 = {p.stem for p in HOOKS.glob("*.ps1")}
    sh = set(shims())
    assert sh - ps1 == set(), f"POSIX shims with no PowerShell sibling: {sorted(sh - ps1)}"


def test_there_are_nine_shims_and_one_library():
    assert len(shims()) == 9
    assert (HOOKS / "lib.sh").is_file()


def test_the_stage_table_covers_every_shim():
    """A shim added without a row here would be silently untested below."""
    assert sorted(STAGE) == shims()


# --- the file itself -----------------------------------------------------------

@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_shim_keeps_lf_endings(name):
    """`#!/bin/sh` followed by a carriage return is `bad interpreter: /bin/sh^M`.

    This repo is developed on Windows with `core.autocrlf=true`, so without the
    `*.sh text eol=lf` rule in `.gitattributes` every Linux checkout — the
    ubuntu CI legs included — would get shims that cannot start."""
    raw = (HOOKS / f"{name}.sh").read_bytes()
    assert b"\r" not in raw, f"{name}.sh carries CR bytes"


def test_the_library_keeps_lf_endings():
    assert b"\r" not in (HOOKS / "lib.sh").read_bytes()


def test_gitattributes_pins_shell_scripts_to_lf():
    """The rule above only holds because this line exists."""
    text = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in text


def test_the_shims_carry_the_executable_bit_in_git():
    """Claude Code runs a hook by its path. A shim committed as 100644 is
    `Permission denied` on every Linux checkout, and nothing else in this file
    would notice: `sh -n` and `sh file` both work on a non-executable file.

    Read from the INDEX rather than the working tree, because on Windows the
    working tree cannot hold the bit and only the index carries it to CI."""
    got = subprocess.run(["git", "ls-files", "-s", "--", "hooks/"],
                         capture_output=True, text=True, shell=False, cwd=str(REPO))
    if got.returncode != 0 or not got.stdout.strip():
        pytest.skip("hooks/ is not under git here")
    modes = {}
    for ln in got.stdout.splitlines():
        parts = ln.split(maxsplit=3)
        if len(parts) == 4:
            modes[parts[3].strip()] = parts[0]
    for name in shims():
        assert modes.get(f"hooks/{name}.sh") == "100755", (
            f"hooks/{name}.sh is {modes.get(f'hooks/{name}.sh')} in the index")
    assert modes.get("hooks/lib.sh") == "100644", "lib.sh is sourced, never run"


@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_shim_declares_the_posix_shell(name):
    first = (HOOKS / f"{name}.sh").read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/bin/sh", f"{name}.sh opens with {first!r}"


@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_shim_carries_no_bashism_this_suite_knows_about(name):
    """Cheap static sweep. `#!/bin/sh` is dash on Debian and Ubuntu, which is
    where the ubuntu CI legs run, and every construct below is bash-only."""
    text = "\n".join(code_lines(f"{name}.sh"))
    for bad in ("[[ ", "((", "local ", "source ", "<<<", "=~", "echo -e"):
        assert bad not in text, f"{name}.sh uses the bashism {bad!r}"


@sh_missing
@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_shim_parses(name):
    got = subprocess.run([SH, "-n", str(HOOKS / f"{name}.sh")],
                         capture_output=True, text=True, shell=False)
    assert got.returncode == 0, got.stderr


@sh_missing
def test_the_library_parses():
    got = subprocess.run([SH, "-n", str(HOOKS / "lib.sh")],
                         capture_output=True, text=True, shell=False)
    assert got.returncode == 0, got.stderr


# --- the guards ----------------------------------------------------------------

@sh_missing
@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_headless_child_does_nothing_at_all(name, fake_python):
    """`MINDHIVE_HEADLESS=1` is what stops a reflection session spawned BY a
    hook from firing the same hooks again. Silent, exit 0, no child."""
    env = dict(fake_python["env"])
    env["MINDHIVE_HEADLESS"] = "1"
    got = run_shim(name, env=env)
    assert got.returncode == 0
    assert got.stdout == ""
    assert logged(fake_python) == [], "a headless run still spawned an interpreter"


@sh_missing
@pytest.mark.parametrize("name", sorted(STAGE))
@pytest.mark.parametrize("payload", ["", "not json at all", "{", "null", "[]"],
                         ids=["empty", "prose", "truncated", "null", "array"])
def test_a_shim_exits_zero_on_any_payload(name, payload, fake_python):
    """Fail-open against the whole space of things stdin can carry.

    A hook that exits non-zero is a hook Claude Code reports as failed, in
    every session, on every turn."""
    got = run_shim(name, payload=payload, env=fake_python["env"])
    assert got.returncode == 0, f"{name}.sh exited {got.returncode}: {got.stderr}"


@sh_missing
@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_shim_with_no_stdin_neither_hangs_nor_fails(name, fake_python):
    """The hang-proofness guard. `[ ! -t 0 ]` is the POSIX spelling of
    `[Console]::IsInputRedirected`; without it a shim blocks forever on a read
    nobody is going to answer, and `run_shim`'s timeout is what catches that."""
    got = run_shim(name, payload="", env=fake_python["env"], timeout=30)
    assert got.returncode == 0


@sh_missing
@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_shim_survives_an_interpreter_that_fails(name, fake_python):
    """The child exiting non-zero and printing to stderr must not become the
    hook's failure. Claude Code surfaces hook failures to the user, so a noisy
    backend would put a red line at the end of every turn."""
    env = dict(fake_python["env"])
    env["FAKE_EXIT"] = "3"
    env["FAKE_STDERR"] = "ModuleNotFoundError: No module named 'mindhive'"
    got = run_shim(name, env=env)
    assert got.returncode == 0, f"{name}.sh exited {got.returncode}"
    assert "ModuleNotFoundError" not in got.stdout
    assert "ModuleNotFoundError" not in got.stderr, (
        f"{name}.sh leaked the child's stderr to the user")


@sh_missing
@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_shim_with_no_interpreter_is_a_silent_no_op(name, tmp_path):
    """The resolver finding nothing must degrade to doing nothing.

    PATH keeps the binaries the shims genuinely exec (`cat`, `dirname`,
    `tr`) because emptying it would break the shim before the code under test
    ran -- and it must hold no python, or on Linux the shim finds
    /usr/bin/python3 and the scenario under test never happens."""
    env = {"MINDHIVE_PYTHON": str(tmp_path / "absent"),
           "MINDHIVE_HOOK_WALK_LIMIT": "0",
           "PATH": _path_without_python(tmp_path)}
    got = run_shim(name, env=env)
    assert got.returncode == 0
    if name != "pre_compact":
        assert got.stdout == "", f"{name}.sh printed with no interpreter"
    else:
        # The one deliberate exception: the reminder is the half a person acts
        # on, and it needs no working backend.
        assert REMINDER in got.stdout


# --- the library guard ---------------------------------------------------------

def _copy_shim(name, into, lib=None):
    """One shim in a directory of its own, beside `lib` as its lib.sh, or
    beside nothing when `lib` is None."""
    into.mkdir(parents=True, exist_ok=True)
    dst = into / f"{name}.sh"
    dst.write_bytes((HOOKS / f"{name}.sh").read_bytes())
    if lib is not None:
        (into / "lib.sh").write_text(lib, encoding="utf-8", newline="\n")
    return dst


def run_file(path, payload=PAYLOAD, env=None, timeout=60):
    """`run_shim` for a shim that is not in `hooks/`."""
    child = dict(os.environ)
    for key in ("MINDHIVE_HEADLESS", "MINDHIVE_ROTATION"):
        child.pop(key, None)
    child.update(env or {})
    return subprocess.run([SH, str(path)], input=payload, text=True, shell=False,
                          capture_output=True, timeout=timeout, cwd=str(REPO),
                          env=child)


@sh_missing
@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_shim_with_no_library_is_a_silent_no_op(name, tmp_path, fake_python):
    """`.` is a SPECIAL builtin. When the file it names is missing, a
    non-interactive shell exits on the spot — the `|| exit 0` on the same line
    never runs. Measured here: `. missing || exit 0` exits 1 under bash's sh and
    a file that does not parse exits 2 under dash. 2 from a Stop hook is
    "block: keep working"; from PreToolUse it is "deny"; from UserPromptSubmit
    it erases the prompt. A hooks directory copied without its library, or a
    settings.json pointing at a stale worktree, is how this happens for real."""
    shim = _copy_shim(name, tmp_path / "alone")
    got = run_file(shim, env=fake_python["env"])
    assert got.returncode == 0, (
        f"{name}.sh exited {got.returncode} with no lib.sh: {got.stderr!r}")
    assert got.stdout == ""
    assert got.stderr == "", got.stderr
    assert logged(fake_python) == []


@sh_missing
@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_shim_with_a_broken_library_is_a_silent_no_op(name, tmp_path, fake_python):
    """The same guard against a library that exists but does not parse."""
    shim = _copy_shim(name, tmp_path / "broken", lib="this is ( not sh\n")
    got = run_file(shim, env=fake_python["env"])
    assert got.returncode == 0, (
        f"{name}.sh exited {got.returncode} with a broken lib.sh: {got.stderr!r}")
    assert got.stdout == ""
    assert got.stderr == "", got.stderr
    assert logged(fake_python) == []


@sh_missing
@pytest.mark.parametrize("name", sorted(STAGE))
def test_the_library_guard_is_the_first_thing_after_the_headless_check(name):
    """Static: nothing may run between the headless guard and the proof that
    the library loads, because anything there runs unguarded."""
    body = code_lines(f"{name}.sh")
    body = [ln.strip() for ln in body if ln.strip()]
    assert body[0].startswith('if [ "${MINDHIVE_HEADLESS:-}" = "1" ]'), body[0]
    assert body[1].startswith("MINDHIVE_HOOK_DIR=$(CDPATH= cd --"), body[1]
    assert body[1].endswith("&& pwd -P) || exit 0"), body[1]
    assert body[2] == "export MINDHIVE_HOOK_DIR", body[2]
    assert body[3] == "MINDHIVE_LIB=$MINDHIVE_HOOK_DIR/lib.sh", body[3]
    assert body[4] == '[ -r "$MINDHIVE_LIB" ] || exit 0', body[4]
    assert body[5].startswith('( . "$MINDHIVE_LIB" ) >/dev/null 2>&1 || exit 0'), body[5]
    assert body[6] == '. "$MINDHIVE_LIB" 2>/dev/null', body[6]


# --- the resolver's edges ------------------------------------------------------

def _fake_venv(repo, log=None):
    """A `.venv/bin/python3` under `repo` that exits 0, logging its argv to
    `log` when one is given."""
    venv = repo / ".venv" / "bin"
    venv.mkdir(parents=True)
    target = venv / "python3"
    body = "#!/bin/sh\n"
    if log is not None:
        body += f"printf 'ran %s\\n' \"$*\" >> '{log.as_posix()}'\n"
    target.write_text(body + "exit 0\n", encoding="utf-8", newline="\n")
    target.chmod(0o755)
    return target


@sh_missing
@pytest.mark.parametrize("limit", ["abc", "", "8x", " 8", "-1"],
                         ids=["word", "empty", "suffixed", "padded", "negative"])
def test_a_non_numeric_walk_limit_is_silently_the_default(limit, tmp_path):
    """`[ "$n" -lt abc ]` prints "integer expression expected" on stderr and
    skips the walk. A hook's stderr is what Claude Code shows the user when it
    decides the hook failed, so both halves matter: no noise, and the venv
    still found."""
    deep = tmp_path / "repo" / "hooks"
    deep.mkdir(parents=True)
    target = _fake_venv(tmp_path / "repo")
    script = (f'MINDHIVE_HOOK_DIR="{deep.as_posix()}"; export MINDHIVE_HOOK_DIR; '
              f'. "{(HOOKS / "lib.sh").as_posix()}"; mindhive_python')
    got = probe(script, env={"MINDHIVE_HOOK_WALK_LIMIT": limit})
    assert got.returncode == 0
    assert got.stderr == "", got.stderr
    assert got.stdout.strip() == target.as_posix()


@sh_missing
def test_a_directory_in_mindhive_python_is_not_an_interpreter(tmp_path):
    """`[ -x dir ]` is TRUE. On its own it would make an operator's typo the
    interpreter every hook execs, and every hook a silent no-op."""
    script = (f'MINDHIVE_HOOK_DIR="{tmp_path.as_posix()}"; export MINDHIVE_HOOK_DIR; '
              f'. "{(HOOKS / "lib.sh").as_posix()}"; mindhive_python; echo "rc=$?"')
    got = probe(script, env={"MINDHIVE_PYTHON": str(tmp_path),
                             "MINDHIVE_HOOK_WALK_LIMIT": "0",
                             "PATH": _path_without_python(tmp_path)})
    assert got.stdout.strip() == "rc=1", got.stdout


@sh_missing
def test_the_resolver_walks_from_the_physical_cwd_when_nothing_is_set(tmp_path):
    """`dirname .` is `.`, so a literal-dot default would end the walk after
    one probe. With nothing set the walk starts from the real cwd."""
    deep = tmp_path / "repo" / "a" / "b"
    deep.mkdir(parents=True)
    _fake_venv(tmp_path / "repo")
    script = f'unset MINDHIVE_HOOK_DIR; . "{(HOOKS / "lib.sh").as_posix()}"; mindhive_python'
    child = dict(os.environ)
    for key in ("MINDHIVE_PYTHON", "MINDHIVE_HOOK_WALK_LIMIT", "MINDHIVE_HOOK_DIR"):
        child.pop(key, None)
    got = subprocess.run([SH, "-c", script], capture_output=True, text=True,
                         shell=False, cwd=str(deep), env=child, timeout=30)
    assert got.returncode == 0, got.stderr
    # `pwd -P` spells the drive its own way (`/c/...` under Git Bash), so the
    # comparison is on the tail the walk actually produced.
    assert got.stdout.strip().endswith("/repo/.venv/bin/python3"), got.stdout


@sh_missing
def test_the_venv_is_found_through_a_symlinked_hooks_dir(tmp_path):
    """Claude Code runs the shim by the path in settings.json. If that path
    reaches `hooks/` through a symlink, the walk must start from where the
    file really is (`pwd -P`), or it climbs the link's parents instead and
    never meets the venv."""
    repo = tmp_path / "repo"
    (repo / "hooks").mkdir(parents=True)
    for p in HOOKS.glob("*.sh"):
        (repo / "hooks" / p.name).write_bytes(p.read_bytes())
    log = tmp_path / "ran.log"
    _fake_venv(repo, log=log)
    link = tmp_path / "elsewhere" / "hooks"
    link.parent.mkdir()
    try:
        os.symlink(repo / "hooks", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a directory symlink here")
    got = run_file(link / "post_tool_use.sh",
                   env={"MINDHIVE_PYTHON": str(tmp_path / "absent"),
                        "PATH": _path_without_python(tmp_path)})
    assert got.returncode == 0, got.stderr
    assert log.exists() and "hook post_tool_use" in log.read_text(encoding="utf-8"), (
        "the resolver never reached the venv beside the real hooks dir")


# --- the Windows Store redirector ----------------------------------------------

@sh_missing
def test_a_path_python_under_windowsapps_is_refused(tmp_path):
    """The App Execution Alias is not a missing file — it spawns, prints an
    install prompt, and exits 49. A resolver that only rejects what does not
    exist would accept it, and every hook would silently do nothing with an
    interpreter that never ran a line."""
    alias = tmp_path / "WindowsApps"
    alias.mkdir()
    for name in ("python", "python3"):
        stub = alias / name
        stub.write_text("#!/bin/sh\necho 'Python was not found; run without "
                        "arguments to install from the Microsoft Store'\nexit 49\n",
                        encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    env = {"MINDHIVE_PYTHON": str(tmp_path / "absent"),
           "MINDHIVE_HOOK_WALK_LIMIT": "0",
           "PATH": f"{alias}{os.pathsep}{_path_without_python(tmp_path)}"}
    got = run_shim("post_tool_use", env=env)
    assert got.returncode == 0
    assert "Microsoft Store" not in got.stdout, (
        "the Store redirector was accepted as an interpreter")
    assert "Microsoft Store" not in got.stderr


@sh_missing
def test_the_resolver_answers_nothing_for_a_store_alias(tmp_path):
    """The predicate itself, so the case-folding is pinned."""
    script = (
        f'MINDHIVE_HOOK_DIR="{HOOKS.as_posix()}"; export MINDHIVE_HOOK_DIR; '
        f'. "{(HOOKS / "lib.sh").as_posix()}"; '
        'for p in /c/Users/u/AppData/Local/Microsoft/WindowsApps/python.exe '
        '/c/Users/u/appdata/local/microsoft/windowsapps/python3 '
        '/usr/bin/python3; do '
        'if mindhive_is_store_stub "$p"; then echo "STUB $p"; '
        'else echo "REAL $p"; fi; done')
    got = probe(script)
    assert got.returncode == 0, got.stderr
    lines = got.stdout.split()
    assert got.stdout.count("STUB") == 2, got.stdout
    assert got.stdout.count("REAL") == 1, got.stdout
    assert lines[-2] == "REAL", "a normal interpreter was called a stub"


@sh_missing
def test_the_resolver_prefers_the_checkouts_own_venv(tmp_path):
    """`mindhive` is installed EDITABLE in that venv, so it is the only
    interpreter that can import the checkout the hook belongs to. A PATH
    interpreter would import whatever it happens to find, or nothing."""
    deep = tmp_path / "repo" / ".claude" / "worktrees" / "wt" / "hooks"
    deep.mkdir(parents=True)
    venv = tmp_path / "repo" / ".venv" / "bin"
    venv.mkdir(parents=True)
    target = venv / "python3"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    target.chmod(0o755)
    script = (f'MINDHIVE_HOOK_DIR="{deep.as_posix()}"; export MINDHIVE_HOOK_DIR; '
              f'. "{(HOOKS / "lib.sh").as_posix()}"; mindhive_python')
    got = probe(script)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == target.as_posix(), (
        "the walk did not reach the venv above a worktree checkout")


# --- stdout discipline ---------------------------------------------------------

@sh_missing
@pytest.mark.parametrize("name", SILENT_STAGES)
def test_a_silent_stage_prints_nothing_even_when_the_child_does(name, fake_python):
    """These stages have no decision channel. Anything they print would be read
    by Claude Code as if it were one."""
    env = dict(fake_python["env"])
    env["FAKE_STDOUT"] = '{"decision": "block", "reason": "should never surface"}'
    got = run_shim(name, env=env)
    assert got.returncode == 0
    assert got.stdout == "", f"{name}.sh put {got.stdout!r} on stdout"


@sh_missing
@pytest.mark.parametrize("name", ["pre_tool_use", "user_prompt_submit"])
def test_a_decision_stage_passes_the_childs_answer_through(name, fake_python):
    env = dict(fake_python["env"])
    env["FAKE_STDOUT"] = '{"decision": "deny", "reason": "use the effective method"}'
    got = run_shim(name, env=env)
    assert got.returncode == 0
    assert '"decision": "deny"' in got.stdout


@sh_missing
@pytest.mark.parametrize("name", ["pre_tool_use", "user_prompt_submit"])
def test_a_decision_stage_forwards_a_decision_even_when_the_child_fails(name, fake_python):
    """Parity with the `.ps1`, which emits `$out` whatever the exit status. A
    deny the child printed before dying is still a deny; dropping it would
    turn an enforcement failure into a silent allow."""
    env = dict(fake_python["env"])
    env["FAKE_STDOUT"] = '{"decision": "deny", "reason": "printed, then crashed"}'
    env["FAKE_EXIT"] = "1"
    got = run_shim(name, env=env)
    assert got.returncode == 0
    assert '"decision": "deny"' in got.stdout, got.stdout


@sh_missing
@pytest.mark.parametrize("name", ["pre_tool_use", "user_prompt_submit"])
def test_a_decision_stage_that_gets_nothing_says_nothing(name, fake_python):
    """Empty means allow, and an empty line is not empty."""
    got = run_shim(name, env=fake_python["env"])
    assert got.returncode == 0
    assert got.stdout == ""


@sh_missing
def test_pre_compact_prints_its_reminder_once(fake_python):
    got = run_shim("pre_compact", env=fake_python["env"])
    assert got.returncode == 0
    assert got.stdout.count(REMINDER) == 1


@sh_missing
def test_the_stop_shim_never_emits_a_block_decision():
    """For a Stop hook, `block` means "do not stop — keep working", which would
    pin a full session open instead of rotating it. Rotation is signalled by the
    ROTATION_PENDING artifact, because a Stop hook cannot end a session at all.

    Asserted as TEXT because the value must not appear anywhere in the shim: the
    shim is a pipe, and the only way it could ever emit one is if somebody wrote
    it in."""
    text = (HOOKS / "stop.sh").read_text(encoding="utf-8")
    assert '"block"' not in text
    body = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("block" in ln for ln in body), (
        "the stop shim mentions block outside its commentary")


@sh_missing
def test_the_stop_shim_carries_no_rotation_gate_of_its_own():
    """The opt-in gate lives in Python (`mindhive.rotation.hook`). A second gate
    in a second language is a second thing to get wrong, and a shim that checked
    `MINDHIVE_ROTATION` itself could silence a rotation the Python side wanted."""
    body = [ln for ln in (HOOKS / "stop.sh").read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")]
    assert not any("MINDHIVE_ROTATION" in ln for ln in body)


# --- dispatch ------------------------------------------------------------------

@sh_missing
@pytest.mark.parametrize("name", [n for n, s in STAGE.items() if s])
def test_a_shim_dispatches_to_its_own_stage(name, fake_python):
    """The parity test proves a sibling EXISTS; this proves it is the right one.

    A copy-paste that left `post_tool_use_failure.sh` calling `post_tool_use`
    would pass every other test in this file."""
    got = run_shim(name, env=fake_python["env"])
    assert got.returncode == 0
    calls = [ln for ln in logged(fake_python) if "-m mindhive hook" in ln]
    assert calls, f"{name}.sh never invoked `mindhive hook`"
    assert calls[0].endswith(f"hook {STAGE[name]}"), (
        f"{name}.sh dispatched {calls[0]!r}")


@sh_missing
def test_the_two_families_call_the_same_stage():
    """Read the stage out of BOTH files and compare. This is what catches a
    rename applied to one family and not the other."""
    for name, stage in STAGE.items():
        if not stage:
            continue
        for suffix in (".sh", ".ps1"):
            text = (HOOKS / f"{name}{suffix}").read_text(encoding="utf-8")
            body = [ln for ln in text.splitlines()
                    if not ln.lstrip().startswith("#")]
            hits = [ln for ln in body if "mindhive hook" in ln]
            assert hits, f"{name}{suffix} names no stage"
            assert f"hook {stage}" in " ".join(hits), (
                f"{name}{suffix} does not dispatch to {stage}")


@sh_missing
def test_session_end_keeps_the_load_bearing_order(fake_python):
    """`replenish` before `roadmap autoclose` before the projections rebuild.

    The ordering is load-bearing and silent when wrong: steps injected AFTER
    the plan is closed still exist, but the plan is already interrupted, so
    `notice()` never renders them and the next session never sees the work."""
    got = run_shim("session_end", env=fake_python["env"])
    assert got.returncode == 0
    calls = logged(fake_python)
    order = []
    for want in ("replenish run", "roadmap autoclose", "review queue",
                 "log queue", "-m mindhive project", "-m mindhive briefing",
                 "graph build", "-m mindhive sync"):
        hit = [i for i, ln in enumerate(calls) if want in ln]
        assert hit, f"session_end.sh never ran {want!r}"
        order.append(hit[0])
    assert order == sorted(order), f"session_end.sh ran its steps out of order: {calls}"


@sh_missing
@pytest.mark.parametrize("name", ["session_start", "session_end", "pre_compact"])
def test_the_session_id_reaches_every_child(name, fake_python):
    """It names a ledger segment. Two children sharing one segment is what tore
    the seven corrupt rows `events._append_lock` was written for.

    The three shims that stamp it export MINDHIVE_SESSION rather than passing
    `--session` to every call, so the environment each child saw is the
    assertion — the `-c` call that PARSES the id is the one child allowed to
    run before it is known."""
    got = run_shim(name, env=fake_python["env"])
    assert got.returncode == 0
    seen = session_env_of(fake_python)
    assert seen, "no child ran"
    assert seen[-1] == "sh-test-1", seen
    assert all(s in ("sh-test-1", "<unset>", "") for s in seen), seen


@sh_missing
@pytest.mark.parametrize("payload", [
    "not json", "{", "[]", "null",
    '{"session_id": null}',
    '{"session_id": 42}',
    '{"session_id": {"a": 1}}',
    '{"session_id": ""}',
    '{"session_id": "a b"}',
    '{"session_id": "../../escaped"}',
    '{"session_id": "with/slash"}',
    '{"session_id": "with\\\\backslash"}',
    '{"session_id": "' + "x" * 129 + '"}',
    '{"session_id": "--evil"}',
    '{"session_id": "-x"}',
    '{"session_id": "-"}',
    '{"session_id": "-leading-dash"}',
], ids=["prose", "truncated", "array", "null", "null-id", "number", "object",
        "empty", "space", "traversal", "slash", "backslash", "overlong",
        "long-flag", "short-flag", "dash", "leading-dash"])
def test_an_unusable_session_id_becomes_unknown(payload, fake_python):
    """`unknown`, never the raw value: the id names a file on disk with no
    further checks in `events.append_event`, so a nested object would become a
    segment called `{'a': 1}.jsonl` and a path would walk out of the store.
    A leading `-` is refused too, so no id can read as an option to the child
    that receives it after `--session`.

    The whitelist can refuse nothing real — Claude Code's ids are UUIDs."""
    got = run_shim("session_end", payload=payload, env=fake_python["env"])
    assert got.returncode == 0
    assert "unknown" in session_env_of(fake_python)[-1:], _lines(fake_python)
    args = argv_of(fake_python)
    assert "unknown" in args
    for bad in ("{'a': 1}", "a b", "../../escaped", "with/slash", "42", "x" * 129,
                "--evil", "-x", "-", "-leading-dash"):
        assert bad not in args, f"the raw id {bad!r} reached a child's argv"


@sh_missing
@pytest.mark.parametrize("chatter", [
    "abc\nzzz", "warning: site\nsh-test-1", " sh-test-1", "sh-test-1 extra",
], ids=["two-lines", "warning-first", "leading-space", "trailing-word"])
def test_a_chatty_interpreter_cannot_smuggle_a_session_id(tmp_path, chatter):
    """The whitelist inside the `-c` program holds only if the interpreter
    printed nothing else. A `sitecustomize` line or a warning on stdout would
    otherwise become the id, so the shell re-checks whatever came back."""
    exe = tmp_path / "chatty"
    exe.write_text("#!/bin/sh\nprintf '%s\\n' \"$CHATTER\"\n",
                   encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    script = (f'MINDHIVE_HOOK_DIR="{tmp_path.as_posix()}"; export MINDHIVE_HOOK_DIR; '
              f'. "{(HOOKS / "lib.sh").as_posix()}"; '
              f"mindhive_session_id '{exe.as_posix()}' '{PAYLOAD}'")
    got = probe(script, env={"CHATTER": chatter})
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "unknown", got.stdout


@sh_missing
def test_a_uuid_session_id_is_accepted_verbatim(fake_python):
    """The other half of the whitelist: the real shape passes untouched."""
    uuid = "b0b172ca-80bb-4976-be29-fed9f7f17a94"
    got = run_shim("session_end", payload=f'{{"session_id": "{uuid}"}}',
                   env=fake_python["env"])
    assert got.returncode == 0
    assert session_env_of(fake_python)[-1] == uuid
    assert uuid in argv_of(fake_python)


@sh_missing
def test_a_payload_that_tries_to_inject_a_flag_is_refused(fake_python):
    """The payload is attacker-influenced in principle: whatever writes to this
    stdin chooses the id. One that reads as an option must never reach a child
    as a new flag. The whitelist refuses it outright — and the second line of
    defence, that every shim quotes `"$MINDHIVE_SESSION"`, is asserted
    separately below so it survives a future loosening of the first."""
    hostile = '{"session_id": "--project=other --limit 99"}'
    got = run_shim("session_end", payload=hostile, env=fake_python["env"])
    assert got.returncode == 0
    args = argv_of(fake_python)
    assert "unknown" in args
    assert not any(a.startswith("--project") for a in args), args
    assert "--limit" not in args
    assert "99" not in args


@pytest.mark.parametrize("name", sorted(STAGE))
def test_the_session_id_is_always_quoted_into_argv(name):
    """Defence in depth behind the whitelist. Unquoted, `$MINDHIVE_SESSION`
    undergoes field splitting and a crafted id becomes extra arguments."""
    for ln in code_lines(f"{name}.sh"):
        if "$MINDHIVE_SESSION" not in ln:
            continue
        if ln.strip().startswith(("MINDHIVE_SESSION=", "export MINDHIVE_SESSION")):
            continue
        assert '"$MINDHIVE_SESSION"' in ln, f"{name}.sh: unquoted session id in {ln!r}"


# --- end to end ----------------------------------------------------------------

@sh_missing
def test_the_capture_hook_really_appends_an_event(tmp_path):
    """The one test that runs the real CLI. Everything above proves the shim
    dispatches; this proves the dispatch lands.

    Against a scratch `MINDHIVE_HOME`, so it can never touch the real store."""
    home = tmp_path / "store"
    home.mkdir()
    env = {"MINDHIVE_HOME": str(home), "MINDHIVE_PROJECT": "shimtest",
           "MINDHIVE_PYTHON": sys.executable,
           "PYTHONPATH": str(REPO / "src")}
    got = run_shim("post_tool_use", env=env, timeout=120)
    assert got.returncode == 0, got.stderr
    assert got.stdout == ""
    written = list(home.rglob("*.jsonl"))
    assert written, f"no event was appended; stderr was {got.stderr!r}"
    body = "\n".join(p.read_text(encoding="utf-8") for p in written)
    assert "sh-test-1" in body
