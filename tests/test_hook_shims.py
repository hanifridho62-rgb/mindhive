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
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "hooks"

# `lib.sh` and `lib.ps1` are sourced, never invoked: each is its family's shared
# library, and the two are siblings of each other rather than of any shim. Named
# rather than pattern-matched so adding a third un-paired file is a deliberate
# edit to this list.
NOT_A_SHIM = {"lib.sh", "lib.ps1"}

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

# The one line `session_start` writes on stderr when it fails open, up to what
# failed. `tests/test_hook_shims_ps.py` pins the same text for the `.ps1` twin.
FAILED_OPEN = "mindhive session_start: failed open: "

sh_missing = pytest.mark.skipif(shutil.which("sh") is None,
                                reason="no POSIX sh on this machine")

# `subprocess` resolves a bare program name through the PATH of the env it is
# GIVEN (`os.get_exec_path(env)`), so a test that hands a shim a private PATH
# would lose `sh` itself on POSIX. Resolve it once, through the real PATH.
SH = shutil.which("sh") or "sh"

# Windows PowerShell 5.1, for the cross-family parity tests that run one fixture
# through BOTH libraries. Same rule as `tests/test_hook_shims_ps.py`: `pwsh` (7+)
# is not a substitute, because the dialect these files obey exists for 5.1.
POWERSHELL = shutil.which("powershell")
ps_missing = pytest.mark.skipif(POWERSHELL is None or sys.platform != "win32",
                                reason="Windows PowerShell 5.1 not available")


# --- helpers -------------------------------------------------------------------

def shims() -> list:
    """Every `.sh` in `hooks/` that is a shim, by stem."""
    return sorted(p.stem for p in HOOKS.glob("*.sh")
                  if p.name not in NOT_A_SHIM)


def ps_shims() -> list:
    """Every `.ps1` in `hooks/` that is a shim, by stem. The PowerShell family
    grew its own shared library (`lib.ps1`) when the hard-coded interpreter came
    out of it, so this side needs the same exclusion the POSIX side always had."""
    return sorted(p.stem for p in HOOKS.glob("*.ps1")
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


def assert_failed_open(name, got, cause):
    """A shim's stderr after it failed open: empty for every shim but
    `session_start`, which names its failure in exactly ONE line carrying
    *cause*. Claude Code drops a SessionStart hook's transcript row only when
    its content, stdout and stderr are all empty, and `hive spend` reads a
    typed session whose MindHive SessionStart left no row as a headless child;
    the line keeps the row."""
    if name != "session_start":
        assert got.stderr == "", (name, got.stderr)
        return
    lines = got.stderr.splitlines()
    assert len(lines) == 1, (name, got.stderr)
    assert lines[0].startswith(FAILED_OPEN), (name, got.stderr)
    assert cause in lines[0], (name, cause, got.stderr)


# --- the drift guard -----------------------------------------------------------

def test_every_powershell_hook_has_a_posix_sibling():
    """TODOS item 6, asserted rather than described.

    This is the test that stops the POSIX family from quietly falling behind:
    a tenth `.ps1` added later fails here until its `.sh` exists."""
    ps1 = set(ps_shims())
    sh = set(shims())
    assert ps1 - sh == set(), f"PowerShell hooks with no POSIX sibling: {sorted(ps1 - sh)}"


def test_every_posix_shim_has_a_powershell_sibling():
    """The other direction, so the two families cannot diverge either way."""
    ps1 = set(ps_shims())
    sh = set(shims())
    assert sh - ps1 == set(), f"POSIX shims with no PowerShell sibling: {sorted(sh - ps1)}"


def test_there_are_nine_shims_and_one_library_per_family():
    assert len(shims()) == 9
    assert len(ps_shims()) == 9
    assert (HOOKS / "lib.sh").is_file()
    assert (HOOKS / "lib.ps1").is_file()


# The two spellings of a hard-coded interpreter. The first is the Windows one
# this branch removed (`C:\Users\...\python.exe`). The second is the same defect
# written for the POSIX family -- `/home/alice/.venv/bin/python3`,
# `/Users/bob/...`, `/usr/local/bin/python3`, `~/.venv/bin/python`, or Git
# Bash's `/c/Users/.../python.exe` -- which has neither a drive letter nor a
# `.exe`, so the first pattern alone could never fail a `.sh` shim.
#
# Both shapes end on the same LEAF: `python`, an optional version (`3`,
# `3.12`), an optional `w`, an optional `.exe`, then the end of the word. The
# first cut of the Windows shape required `python[a-z]*\.exe`, so it could not
# see `C:\Python312\python3.exe` (the `3` is not `[a-z]`) or an extensionless
# `& "C:\Python312\python"`, which PowerShell runs through PATHEXT all the same.
#
# The POSIX shape is anchored at the START of a word (line start, whitespace,
# `=`, a quote, `(`, a backtick, or `;` `|` `&`) so `"$_mh_dir/.venv/bin/python3"`
# -- a path relative to a variable, which is exactly how `lib.sh` probes the
# venv -- is not an absolute path and does not match. The leaf must be the
# interpreter itself, so `/usr/bin/env python3` does not match either, and the
# end-of-word rule keeps a directory like `/srv/python-tools` out.
_DRIVE_LETTER_INTERPRETER = re.compile(
    r"[A-Za-z]:[\\/](?:[^\r\n\"'`;|&<>]*[\\/])?python[0-9.]*w?(?:\.exe)?"
    r"(?=$|[\s\"'`;|&)<>])", re.IGNORECASE)
_ABSOLUTE_POSIX_INTERPRETER = re.compile(
    r"(?:^|[\s=\"'(`;|&])~?/(?:[^\s\"'`;|&()<>]*/)?python[0-9.]*w?(?:\.exe)?"
    r"(?=$|[\s\"'`;|&)<>])", re.IGNORECASE)
HARD_CODE_SHAPES = (_DRIVE_LETTER_INTERPRETER, _ABSOLUTE_POSIX_INTERPRETER)


def hard_codes_an_interpreter(line: str) -> bool:
    """True when *line* names an interpreter by an absolute path."""
    return any(shape.search(line) for shape in HARD_CODE_SHAPES)


def hard_code_offenders(files) -> list:
    """`name:line: text` for every code line in *files* that hard-codes an
    interpreter. Whole-line comments are skipped: the libraries QUOTE the old
    line to explain why it is gone."""
    offenders = []
    for f in files:
        if not f.is_file():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8",
                                             errors="replace").splitlines(), 1):
            bare = line.strip()
            if bare.startswith("#"):
                continue
            if hard_codes_an_interpreter(line):
                offenders.append(f"{f.name}:{i}: {bare[:90]}")
    return offenders


def test_no_shim_hard_codes_an_interpreter_path():
    """The defect this whole family was rewritten to end.

    An absolute path to one operator's venv is correct on exactly one machine,
    and the failure it causes elsewhere is SILENT -- the hook runs, finds no
    interpreter, and every session on that machine loses telemetry and rotation
    with nothing printed. Asserted over BOTH families and over `circle.ps1`, and
    written against the shape rather than one username, so a different
    operator's hard-code fails too.

    Review found the first cut of this could not fail a `.sh` file at all: its
    one pattern needed a drive letter AND `.exe`, so
    `py=/home/alice/mindhive/.venv/bin/python3` in a POSIX shim passed. The
    matcher is proven against synthetic lines in the two tests below rather
    than by trusting that the pattern reads right."""
    targets = list(HOOKS.glob("*.ps1")) + list(HOOKS.glob("*.sh"))
    targets.append(REPO / "circle.ps1")
    assert len([t for t in targets if t.suffix == ".sh"]) >= 10, (
        "the scan no longer reads the POSIX family")
    # `hard_code_offenders` skips a path that is not a file, so a moved
    # `circle.ps1` would otherwise leave this guard reading nothing, green.
    assert [t.name for t in targets if not t.is_file()] == [], "a scan target is gone"
    offenders = hard_code_offenders(targets)
    assert offenders == [], "hard-coded interpreter path(s): " + "; ".join(offenders)


@pytest.mark.parametrize("line", [
    'py=/home/alice/mindhive/.venv/bin/python3',
    'py="/Users/bob/src/mindhive/.venv/bin/python"',
    '/usr/local/bin/python3 -m mindhive hook stop',
    'printf \'%s\' "$raw" | /opt/venv/bin/python3.12 -m mindhive hook post_tool_use',
    "exec '/usr/bin/python3' -m mindhive sync",
    'py=~/.venv/bin/python3',
    'py=$(printf \'/home/alice/.venv/bin/pythonw\')',
    'cd /srv/mindhive;/usr/bin/python3 -m mindhive sync',
    '/usr/bin/python3</dev/null',
    'py=/c/Users/hanif/mindhive/.venv/Scripts/python.exe',
    '$py = "C:\\Users\\hanif\\mindhive\\.venv\\Scripts\\python.exe"',
    "$py = 'D:/tools/py/pythonw.exe'",
    '$py = "C:\\Python312\\python3.exe"',
    "& 'C:\\Program Files (x86)\\Python\\python' -m mindhive sync",
], ids=["posix-home", "posix-macos-quoted", "posix-usr-local", "posix-piped",
        "posix-exec-single-quoted", "posix-tilde", "posix-subshell",
        "posix-after-semicolon", "posix-redirected",
        "gitbash-drive", "windows-original", "windows-forward-slash",
        "windows-python3-exe", "windows-no-extension"])
def test_the_hard_code_matcher_catches_both_families(line):
    """Every spelling of the defect, POSIX first because that is the half the
    first cut of the guard could not see."""
    assert hard_codes_an_interpreter(line), f"missed a hard-code: {line!r}"


@pytest.mark.parametrize("line", [
    '        for _mh_candidate in "$_mh_dir/.venv/Scripts/python.exe" \\',
    '                             "$_mh_dir/.venv/bin/python"; do',
    '            for _mh_candidate in "$_mh_dir/$_mh_name" "$_mh_dir/$_mh_name.exe"; do',
    'py=$(mindhive_python) || exit 0',
    'printf \'%s\' "$raw" | "$py" -m mindhive hook post_tool_use >/dev/null 2>&1',
    '#!/usr/bin/env python3',
    "    $leaves = @('Scripts\\python.exe', 'bin\\python3.exe', 'bin\\python.exe')",
    'see https://docs.python.org/3/using/windows.html',
    'command -v python3 >/dev/null 2>&1',
    "    $py = Join-Path $root '.venv\\Scripts\\python.exe'",
    "Set-Location 'C:\\src\\python-tools'",
    'cd /srv/python-tools && ls',
], ids=["lib-scripts-probe", "lib-bin-probe", "lib-path-scan", "shim-resolve",
        "shim-dispatch", "env-shebang", "ps-leaves", "url", "command-v",
        "ps-relative-leaf", "windows-pythonic-dir", "posix-pythonic-dir"])
def test_the_hard_code_matcher_leaves_resolved_paths_alone(line):
    """CANARY for the test above: a matcher that flagged every line mentioning
    `python` would pass it and fail the real scan on `lib.sh`'s own venv probe.
    These are lines the resolver really contains, or near neighbours of them."""
    assert not hard_codes_an_interpreter(line), f"flagged a resolved path: {line!r}"


def test_the_scan_reports_a_posix_hard_code_in_a_shim_file(tmp_path):
    """The matcher above is only half the guard; this proves the scan the real
    test runs actually applies it to a `.sh` file, and still skips a comment
    that quotes the old line -- on a synthetic shim, never a real one."""
    shim = tmp_path / "session_start.sh"
    shim.write_text(
        "#!/bin/sh\n"
        "# py=/home/alice/mindhive/.venv/bin/python3   (the old way, quoted)\n"
        "py=/home/alice/mindhive/.venv/bin/python3\n"
        '"$py" -m mindhive sync\n',
        encoding="utf-8", newline="\n")
    clean = tmp_path / "post_tool_use.sh"
    clean.write_bytes((HOOKS / "post_tool_use.sh").read_bytes())
    got = hard_code_offenders([shim, clean])
    assert len(got) == 1, got
    assert got[0].startswith("session_start.sh:3:"), got


# --- shipped instruction text: no one operator's paths -------------------------
#
# The plugin ships prose that Claude FOLLOWS: `commands/*.md`, the skills under
# `skills/`, the procedures under `scripts/`, and the README. Six of those told
# every installation to run `C:\Users\hanif\mindhive\.venv\Scripts\python.exe`
# and one registered a hook into that user's `settings.json` -- the one-machine
# hard-code the shims were rewritten to remove, moved into text another user's
# Claude acts on. The scan reads every line, headings included (a Markdown `#`
# is not a comment), and matches the SHAPE of a per-user directory rather than
# one username. `TODOS.md` and `docs/` quote the old hard-code as history and
# are deliberately not scanned.
SHIPPED_TEXT_TREES = ("commands", "skills", "scripts")

# `<drive>:\Users\<name>\` in either slash (doubled backslashes too, as JSON
# escapes them), Git Bash's `/c/Users/<name>/`, and `/home/<name>/` or macOS's
# `/Users/<name>/` where they start a path rather than sit inside a URL. A name
# opening with `<`, `%` or `$` is a placeholder or a variable, not a user.
_PER_USER_PATH = re.compile(
    r"[A-Za-z]:[\\/]+Users[\\/]+(?![<%$])[^\\/\s\"'`<>|]+[\\/]"
    r"|(?<![\w.-])/[A-Za-z]/Users/(?![<%$])[^/\s\"'`<>|]+/"
    r"|(?<![\w.:/~-])/(?:home|Users)/(?![<%$])[^/\s\"'`<>|]+/",
    re.IGNORECASE)


def names_a_per_user_path(line: str) -> bool:
    """True when *line* names a directory inside one user's profile."""
    return _PER_USER_PATH.search(line) is not None


def shipped_text_files() -> list:
    """The README and every Markdown file under `SHIPPED_TEXT_TREES`."""
    files = [REPO / "README.md"]
    for tree in SHIPPED_TEXT_TREES:
        files.extend(sorted((REPO / tree).rglob("*.md")))
    return files


def per_user_path_offenders(files) -> list:
    """`path:line: text` for EVERY line in *files* that names a per-user path.
    No line is skipped, unlike `hard_code_offenders`: a Markdown heading starts
    with `#` and is shipped text all the same."""
    offenders = []
    for f in files:
        try:
            label = f.relative_to(REPO).as_posix()
        except ValueError:
            label = f.name
        for i, line in enumerate(f.read_text(encoding="utf-8",
                                             errors="replace").splitlines(), 1):
            if names_a_per_user_path(line):
                offenders.append(f"{label}:{i}: {line.strip()[:90]}")
    return offenders


def test_no_shipped_instruction_text_names_one_users_path():
    """Text Claude follows must not send every installation to one operator's
    checkout. It is the defect the hook family was rewritten to end, one layer
    up: on any other machine the path does not exist, and what fails is
    whatever the model was told to run with it.

    Guarded against reading nothing: every tree must contribute a file, so a
    renamed directory cannot leave this green by scanning an empty list."""
    files = shipped_text_files()
    assert (REPO / "README.md").is_file(), "the README is gone"
    for tree in SHIPPED_TEXT_TREES:
        assert any(f.is_relative_to(REPO / tree) for f in files), (
            f"the scan reads nothing under {tree}/")
    offenders = per_user_path_offenders(files)
    assert offenders == [], "per-user path(s) in shipped text: " + "; ".join(offenders)


@pytest.mark.parametrize("line", [
    "`C:\\Users\\hanif\\mindhive\\.venv\\Scripts\\python.exe -m mindhive …`",
    'hive session install-hook "C:/Users/hanif/.claude/settings.json"',
    "(or `/plugin marketplace add C:\\Users\\hanif\\mindhive` to install)",
    '"cwd": "C:\\\\Users\\\\hanif\\\\mindhive"',
    "c:\\users\\alice\\src\\mindhive",
    "D:\\Users\\ops\\mindhive\\.venv",
    "py=/c/Users/hanif/mindhive/.venv/Scripts/python.exe",
    "cd /home/alice/src/mindhive",
    "open /Users/bob/mindhive/README.md",
    "## Installed at /home/carol/mindhive/",
], ids=["windows-venv", "windows-forward-slash", "windows-plugin-add",
        "json-escaped", "lower-case", "other-drive", "gitbash-drive",
        "linux-home", "macos-users", "markdown-heading"])
def test_the_per_user_path_matcher_catches_every_spelling(line):
    """Every spelling the scan exists for. The first three are the shapes of
    lines the shipped text really carried before this test was written."""
    assert names_a_per_user_path(line), f"missed a per-user path: {line!r}"


@pytest.mark.parametrize("line", [
    "`<checkout>\\.venv\\Scripts\\python.exe -m mindhive …` on Windows,",
    "`<checkout>/.venv/bin/python -m mindhive …` elsewhere, where `<checkout>` is the",
    'hive session install-hook "$HOME/.claude/settings.json"     # POSIX',
    'hive session install-hook "$env:USERPROFILE\\.claude\\settings.json"',
    'hive session install-hook "%USERPROFILE%\\.claude\\settings.json"',
    "type %USERPROFILE%\\.mindhive\\<project>\\ROTATION_PENDING",
    "C:\\Users\\<you>\\mindhive",
    "delete the `\"Stop\"` block from `~/.claude/settings.json`",
    "/plugin marketplace add hanifridho62-rgb/Lintester-project",
    "https://github.com/hanifridho62-rgb/Lintester-project/actions/workflows/ci.yml",
    "see https://example.com/home/docs/ and https://example.com/Users/list/",
    "profiles live under C:\\Users\\ on Windows",
], ids=["checkout-windows", "checkout-posix", "posix-home-var", "ps-userprofile",
        "cmd-userprofile", "cmd-type", "placeholder-name", "tilde",
        "marketplace-slug", "github-url", "url-paths", "bare-users-dir"])
def test_the_per_user_path_matcher_leaves_portable_text_alone(line):
    """CANARY for the test above: a matcher that flagged every mention of
    `Users` or `home` would pass it and then fail the real scan on the portable
    wording that replaced the hard-codes. These are lines the shipped text
    really contains now, or near neighbours of them."""
    assert not names_a_per_user_path(line), f"flagged portable text: {line!r}"


def test_the_shipped_text_scan_reports_a_planted_per_user_path(tmp_path):
    """The matcher is half the guard. This proves the scan the real test runs
    applies it to a Markdown file, reads a `#` heading line instead of skipping
    it as a comment, and stays silent on the real README beside it."""
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "# Demo\n"
        "\n"
        "Run `<checkout>/.venv/bin/python -m mindhive state`.\n"
        "## C:\\Users\\alice\\mindhive\\.venv\\Scripts\\python.exe\n",
        encoding="utf-8", newline="\n")
    readme = tmp_path / "README.md"
    readme.write_bytes((REPO / "README.md").read_bytes())
    got = per_user_path_offenders([skill, readme])
    assert len(got) == 1, got
    assert got[0].startswith("SKILL.md:4:"), got


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


# Constructs `#!/bin/sh` does not promise. `sh` is dash on Debian and Ubuntu,
# which is where the ubuntu CI legs run, and each of these is bash-only or left
# unspecified by POSIX. `((` is the arithmetic COMMAND; `$((`, arithmetic
# EXPANSION, is POSIX and `lib.sh`'s walk counter uses it, so `bashisms_in`
# reads past it rather than reporting it.
BASHISMS = ("[[ ", "((", "local ", "source ", "<<<", "=~", "echo -e")


def bashisms_in(lines) -> list:
    """Which of `BASHISMS` appear in *lines*, in the tuple's order."""
    text = "\n".join(lines).replace("$((", "")
    return [bad for bad in BASHISMS if bad in text]


@pytest.mark.parametrize("name", sorted(STAGE))
def test_a_shim_carries_no_bashism_this_suite_knows_about(name):
    """Cheap static sweep over each shim's code lines."""
    assert bashisms_in(code_lines(f"{name}.sh")) == [], f"{name}.sh"


def test_the_library_carries_no_bashism_this_suite_knows_about():
    """The same sweep over `lib.sh`, which every shim sources and which the
    per-shim sweep above never reads: `NOT_A_SHIM` keeps it out of `STAGE`.
    It is where the family's shared code lives -- `extract_json_field`
    included -- so a bashism here reaches all nine hooks at once."""
    assert bashisms_in(code_lines("lib.sh")) == []


def test_the_bashism_sweep_reports_what_it_names_and_passes_posix_arithmetic():
    """CANARY for the two sweeps above. Their silence means something only if
    the sweep can speak: it must report `local` and the `((` command, and it
    must pass `$((...))`, or the library -- which counts its walk with one --
    could never be swept at all."""
    assert bashisms_in(["f() {", "    local x=1", "    (( x++ ))", "}"]) == [
        "((", "local "]
    assert bashisms_in(["_mh_n=$((_mh_n + 1))"]) == []


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
    assert got.stderr == "", got.stderr
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
    if name == "session_start":
        # Its own line instead: the first failing call and its exit code.
        assert_failed_open(name, got, cause="mindhive append session_start exited 3")


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
    assert_failed_open(name, got, cause="no Python interpreter resolved")


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
    assert_failed_open(name, got, cause="lib.sh is missing")
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
    assert_failed_open(name, got, cause="lib.sh does not load")
    assert logged(fake_python) == []


# A guard in `session_start.sh` that names its failure before it exits.
_REPORTED_EXIT = re.compile(r'\|\| \{ session_start_failed_open "[^"]*"; exit 0; \}')


@sh_missing
@pytest.mark.parametrize("name", sorted(STAGE))
def test_the_library_guard_is_the_first_thing_after_the_headless_check(name):
    """Static: nothing may run between the headless guard and the proof that
    the library loads, because anything there runs unguarded.

    `session_start` alone defines its failure reporter in between -- a
    definition, which runs nothing -- and has every guard name its failure
    before it exits, so no bare `|| exit 0` is left in it. Read with those two
    normalised away, its preamble must be the same as every other shim's."""
    body = code_lines(f"{name}.sh")
    body = [ln.strip() for ln in body if ln.strip()]
    if name == "session_start":
        assert body[1].startswith("session_start_failed_open() {"), body[1]
        assert [ln for ln in body if "|| exit 0" in ln] == [], body
        body = [body[0]] + [_REPORTED_EXIT.sub("|| exit 0", ln) for ln in body[2:]]
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


# `lib.sh`'s junk filter used to be `''|*[!0-9]*`, so ANY run of digits passed
# it: twenty nines reached `[ "$n" -lt 99999999999999999999 ]`, which under
# bash's `sh` prints "integer expression expected" on the hook's stderr and
# skips the walk (dash prints nothing and the comparison is simply true, so the
# walk runs to the filesystem root), and ten zeros meant "do not walk" while
# `lib.ps1`, which bounds the run at nine digits, read them as the default.
# Measured by review (#25), and per shell since. The filter's
# `??????????*` now reads a run of ten or more as the default, and the
# `overlong` and `ten-zeros` rows below are its regression guards.


@sh_missing
@pytest.mark.parametrize("limit", [
    "abc", "", "8x", " 8", "-1", "99999999999999999999", "0000000000",
], ids=["word", "empty", "suffixed", "padded", "negative", "overlong",
        "ten-zeros"])
def test_a_non_numeric_walk_limit_is_silently_the_default(limit, tmp_path):
    """`[ "$n" -lt abc ]` is an error on stderr that also skips the walk --
    "integer expression expected" under bash's `sh`, "Illegal number" under
    dash. A hook's stderr is what Claude Code shows the user when it decides
    the hook failed, so both halves matter: no noise, and the venv still found.

    `overlong` and `ten-zeros` are digit runs the filter used to let THROUGH:
    the first to that same noise under bash's `sh` (dash says nothing and the
    comparison is simply true), the second to no walk at all."""
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


def _store_alias_dir(root) -> Path:
    """A `WindowsApps` directory whose `python` and `python3` behave like the
    App Execution Alias: they run, print the Store prompt, and exit 49."""
    alias = root / "WindowsApps"
    alias.mkdir(parents=True)
    for name in ("python", "python3"):
        stub = alias / name
        stub.write_text("#!/bin/sh\necho 'Python was not found; run without "
                        "arguments to install from the Microsoft Store'\nexit 49\n",
                        encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    return alias


def _interpreter_dir(root, dirname, name="python3", log=None) -> Path:
    """A PATH directory holding one working 'interpreter' called *name*."""
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    exe = d / name
    body = "#!/bin/sh\n"
    if log is not None:
        body += f"printf 'ran %s\\n' \"$*\" >> '{log.as_posix()}'\n"
    exe.write_text(body + "exit 0\n", encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    return d


def resolve_from_path(tmp_path, dirs):
    """`mindhive_python` with the override unset and the walk switched off, so
    PATH -- *dirs*, then a python-free tools dir -- is the only thing deciding.
    Answers (rc, printed) where *printed* is what the resolver wrote.

    PATH is handed over in the platform's own notation. Under Git Bash the
    MSYS runtime rewrites a Windows-form PATH to POSIX form as `sh` starts
    (`C:\\...\\Temp` becomes `/tmp`), so the answer is compared by its tail."""
    script = (f'MINDHIVE_HOOK_DIR="{(tmp_path / "nowhere").as_posix()}"; '
              'export MINDHIVE_HOOK_DIR; '
              f'. "{(HOOKS / "lib.sh").as_posix()}"; mindhive_python; echo "rc=$?"')
    path = os.pathsep.join([str(d) for d in dirs] + [_path_without_python(tmp_path)])
    got = probe(script, env={"MINDHIVE_HOOK_WALK_LIMIT": "0", "PATH": path})
    assert got.stderr == "", got.stderr
    lines = got.stdout.strip().splitlines()
    assert lines and lines[-1].startswith("rc="), got.stdout
    return int(lines[-1][3:]), "\n".join(lines[:-1]).strip()


def _tail(answer: str) -> str:
    """A resolved path in one spelling: forward slashes, lower case, no `.exe`.
    Git Bash's stat() answers `python3` for a file that is `python3.exe`, and
    PowerShell spells the same file with backslashes."""
    got = answer.strip().replace("\\", "/").lower()
    return got[:-4] if got.endswith(".exe") else got


@sh_missing
@pytest.mark.parametrize("name", ["python3", "python"])
def test_a_store_stub_ahead_of_a_real_interpreter_does_not_shadow_it(tmp_path, name):
    """The refusal of the Store redirector must be a SKIP, not a STOP.

    On a default Windows profile `%LOCALAPPDATA%/Microsoft/WindowsApps` sits on
    PATH AHEAD of a per-user Python, and a Windows user running Claude Code
    through Git Bash inherits that PATH. The first cut took `command -v`'s
    first hit, refused the stub, and answered nothing -- measured by review:
    PATH=<stub>:<real> gave rc=1 and nine silent no-op hooks, while `lib.ps1`
    on the same box skipped the stub and resolved. `name` covers both the
    interpreter the stub shadows by the same name and the one it shadows by a
    name the loop reaches second."""
    alias = _store_alias_dir(tmp_path)
    real = _interpreter_dir(tmp_path, "realbin", name)
    rc, printed = resolve_from_path(tmp_path, [alias, real])
    assert rc == 0, f"a Store stub ahead of {real} left no interpreter"
    assert _tail(printed).endswith(f"/{tmp_path.name}/realbin/{name}".lower()), printed


@sh_missing
def test_a_store_stub_alone_on_path_still_resolves_to_nothing(tmp_path):
    """CANARY for the test above. A scan that simply stopped refusing the stub
    would pass it by answering the stub; this is the case it cannot pass."""
    alias = _store_alias_dir(tmp_path)
    rc, printed = resolve_from_path(tmp_path, [alias])
    assert rc == 1, f"the Store redirector was accepted: {printed!r}"
    assert printed == ""


@sh_missing
def test_a_real_interpreter_alone_on_path_is_found(tmp_path):
    """CANARY: the stub-ahead fixture minus the stub. Without it a resolver that
    ignored PATH entirely would satisfy the stub-alone test."""
    real = _interpreter_dir(tmp_path, "realbin")
    rc, printed = resolve_from_path(tmp_path, [real])
    assert rc == 0
    assert _tail(printed).endswith(f"/{tmp_path.name}/realbin/python3".lower()), printed


@sh_missing
def test_a_path_dir_holding_only_python3_exe_is_found(tmp_path):
    """A directory whose only interpreter is `python3.exe` must still be found
    by the PATH scan that replaced `command -v`, or the fix for the Store stub
    could cost a Windows user their interpreter.

    On POSIX this pins the explicit `$name.exe` candidate. Under Git Bash it
    cannot: the runtime answers `[ -f "$dir/python3" ]` (and `command -v
    python3`) for a lone `python3.exe` -- measured -- so the bare candidate
    already finds it and dropping `.exe` stays green there. On Windows this is
    a reachability canary, not a pin."""
    d = tmp_path / "exeonly"
    d.mkdir()
    exe = d / "python3.exe"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    rc, printed = resolve_from_path(tmp_path, [d])
    assert rc == 0, "a directory holding only python3.exe was not searched"
    assert _tail(printed).endswith(f"/{tmp_path.name}/exeonly/python3".lower()), printed


@sh_missing
def test_a_path_entry_spelled_like_a_glob_is_read_literally(tmp_path):
    """`for dir in $PATH` under `IFS=:` GLOBS each element against the
    filesystem, so an entry `[ab]` answered from a directory named `a` that is
    not on PATH at all (measured by review). The walk splits with parameter
    expansion now, which never globs: the interpreter in the literal `[ab]`
    directory is the answer, and `a`'s -- planted so that the glob has
    something to match -- is never looked at."""
    literal = _interpreter_dir(tmp_path, "[ab]")
    _interpreter_dir(tmp_path, "a")
    rc, printed = resolve_from_path(tmp_path, [literal])
    assert rc == 0, "the [ab] entry was not searched"
    assert _tail(printed).endswith(f"/{tmp_path.name}/[ab]/python3".lower()), printed


@sh_missing
def test_the_path_walk_leaves_the_callers_ifs_alone(tmp_path):
    """Restoring `IFS` from a saved copy turns an UNSET `IFS` into an EMPTY one,
    which switches word splitting off for whoever runs `mindhive_python`
    outside `$(...)` (measured by review: three fields before the call, one
    after). Every shipped shim uses the subshell form, so the leak had no
    victim yet; the walk no longer touches `IFS` at all, and `${IFS+set}` is
    the spelling that tells unset from empty."""
    real = _interpreter_dir(tmp_path, "realbin")
    script = (f'. "{(HOOKS / "lib.sh").as_posix()}"; unset IFS; '
              'mindhive_python >/dev/null; x="a b c"; set -- $x; '
              'echo "fields=$#"; echo "ifs=${IFS+set}"')
    got = probe(script, env={"MINDHIVE_HOOK_WALK_LIMIT": "0",
                             "MINDHIVE_HOOK_DIR": (tmp_path / "nowhere").as_posix(),
                             "PATH": os.pathsep.join([str(real),
                                                      _path_without_python(tmp_path)])})
    assert got.stderr == "", got.stderr
    assert got.stdout.split() == ["fields=3", "ifs="], got.stdout


@sh_missing
def test_the_shim_runs_the_real_interpreter_behind_a_store_stub(tmp_path):
    """End to end through a real shim: the interpreter found behind the stub is
    the one the hook actually runs, not just the one the resolver prints."""
    log = tmp_path / "ran.log"
    alias = _store_alias_dir(tmp_path)
    real = _interpreter_dir(tmp_path, "realbin", log=log)
    env = {"MINDHIVE_PYTHON": str(tmp_path / "absent"),
           "MINDHIVE_HOOK_WALK_LIMIT": "0",
           "PATH": os.pathsep.join([str(alias), str(real),
                                    _path_without_python(tmp_path)])}
    got = run_shim("post_tool_use", env=env)
    assert got.returncode == 0, got.stderr
    assert "Microsoft Store" not in got.stdout + got.stderr
    assert log.exists() and "-m mindhive hook post_tool_use" in log.read_text(
        encoding="utf-8"), "the shim never ran the interpreter behind the stub"


# --- cross-family parity: the two libraries ------------------------------------
#
# The shim-level parity tests above compare STEMS and STAGE names, and both
# libraries are in `NOT_A_SHIM`, so until these existed nothing compared what
# `lib.sh` and `lib.ps1` DO. Review found two contracts that had already
# drifted behind docstrings claiming parity: `MINDHIVE_HOOK_WALK_LIMIT=0` (sh:
# do not walk; ps: junk, walk 8) and a Store stub ahead of a real interpreter
# (ps: skip it; sh: stop). A later parity probe measured three more: the venv
# leaf order, which extensions PATH may yield, and how `MINDHIVE_PYTHON` may be
# spelled. Each test below runs ONE fixture tree through both resolvers and
# asserts they name the same file.

def _cmd_exe_copy(path: Path) -> Path:
    """A real Windows executable at *path*. Both families accept it -- `lib.ps1`
    on the PATHEXT extension, Git Bash's `-x` on `.exe` -- and it is a file
    that could genuinely be run, so a resolver cannot pass by accepting junk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                             "System32", "cmd.exe"), path)
    return path


def _sh_quote(value) -> str:
    """*value* as one single-quoted POSIX sh word."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _both_resolve(tmp_path, hook_dirs, sh_path, ps_path, walk_limit, overrides=None):
    """Resolve once per hook dir through each library under the same settings.
    Answers `(sh_answers, ps_answers)`, one raw path (or "") per hook dir.

    *overrides*, when given, is one `MINDHIVE_PYTHON` value per hook dir, set
    inside each probe just before that resolution -- so a whole table of
    spellings costs one process per family rather than one per row."""
    def clean(extra):
        child = dict(os.environ)
        for key in ("MINDHIVE_PYTHON", "MINDHIVE_HOOK_DIR", "MINDHIVE_HOOK_WALK_LIMIT",
                    "MINDHIVE_HEADLESS", "MINDHIVE_SESSION", "MINDHIVE_ROTATION"):
            child.pop(key, None)
        child.update(extra)
        return child

    sh_script = f'. "{(HOOKS / "lib.sh").as_posix()}"; '
    for i, d in enumerate(hook_dirs):
        if overrides is not None:
            sh_script += (f"MINDHIVE_PYTHON={_sh_quote(overrides[i])}; "
                          "export MINDHIVE_PYTHON; ")
        sh_script += (f'MINDHIVE_HOOK_DIR="{d.as_posix()}"; export MINDHIVE_HOOK_DIR; '
                      'printf "[%s]\\n" "$(mindhive_python)"; ')
    sh_got = subprocess.run([SH, "-c", sh_script], capture_output=True, text=True,
                            shell=False, cwd=str(REPO), timeout=60,
                            env=clean({"MINDHIVE_HOOK_WALK_LIMIT": walk_limit,
                                       "PATH": sh_path}))

    ps_script = tmp_path / "parity_probe.ps1"
    body = '. "%s"\n' % (HOOKS / "lib.ps1")
    for i, d in enumerate(hook_dirs):
        if overrides is not None:
            body += "$env:MINDHIVE_PYTHON = '%s'\n" % str(overrides[i]).replace("'", "''")
        body += ("$env:MINDHIVE_HOOK_DIR = '%s'\n"
                 "Write-Output ('[' + (Get-MindhivePython) + ']')\n" % d)
    ps_script.write_text(body, encoding="ascii")
    ps_got = subprocess.run([POWERSHELL, "-NoProfile", "-File", str(ps_script)],
                            capture_output=True, text=True, timeout=120,
                            env=clean({"MINDHIVE_HOOK_WALK_LIMIT": walk_limit,
                                       "PATH": ps_path}))

    answers = []
    for fam, got in (("lib.sh", sh_got), ("lib.ps1", ps_got)):
        assert got.returncode == 0, f"{fam} probe exited {got.returncode}: {got.stderr}"
        assert got.stderr.strip() == "", f"{fam} wrote to stderr: {got.stderr!r}"
        lines = [ln.strip() for ln in got.stdout.splitlines() if ln.strip()]
        assert len(lines) == len(hook_dirs), f"{fam}: {got.stdout!r}"
        answers.append([ln[1:-1] for ln in lines])
    return answers[0], answers[1]


def _which_fixture(tmp_path, answer, fixtures) -> str:
    """The label of the fixture *answer* names, `NONE` for no answer, or
    `OTHER:<path>` -- so a failure says which file each family picked."""
    if not answer.strip():
        return "NONE"
    got = _tail(answer)
    for label, path in fixtures.items():
        rel = path.relative_to(tmp_path).as_posix()
        if got.endswith(_tail(f"/{tmp_path.name}/{rel}")):
            return label
    return f"OTHER:{answer}"


@sh_missing
@ps_missing
@pytest.mark.parametrize("limit, expected", [
    ("0", ["NONE", "NONE"]),
    ("3", ["venv", "NONE"]),
    ("", ["venv", "venv"]),
    ("abc", ["venv", "venv"]),
    ("99999999999999999999", ["venv", "venv"]),
    ("0000000000", ["venv", "venv"]),
], ids=["zero", "three", "empty", "word", "overlong", "ten-zeros"])
def test_both_libraries_read_the_walk_limit_the_same_way(tmp_path, limit, expected):
    """One contract, asserted across the family boundary rather than described.

    The venv sits two levels above one hook dir and four above the other, so
    the pair of answers tells `0` (no walk), `3` (reaches only the near one)
    and the default 8 (reaches both) apart. `lib.ps1` used to read `0` as junk
    and walk 8 while `lib.sh` honoured it -- measured by review, and pinned as
    intended by a PowerShell test whose docstring claimed the families agreed.

    `expected` is asserted as well as equality, so two libraries broken the
    same way cannot pass as parity. Digit runs longer than nine used to diverge
    (sh: stderr noise for twenty nines, no walk for ten zeros; ps: the default).
    Both families now read them as the default, and the last two rows hold
    that."""
    repo = tmp_path / "repo"
    venv = _cmd_exe_copy(repo / ".venv" / "Scripts" / "python.exe")
    near = repo / "a" / "hooks"
    far = repo / "a" / "b" / "c" / "hooks"
    near.mkdir(parents=True)
    far.mkdir(parents=True)
    sh_answers, ps_answers = _both_resolve(
        tmp_path, [near, far],
        sh_path=_path_without_python(tmp_path),
        ps_path=r"C:\Windows\System32;C:\Windows",
        walk_limit=limit)
    fixtures = {"venv": venv}
    sh_labels = [_which_fixture(tmp_path, a, fixtures) for a in sh_answers]
    ps_labels = [_which_fixture(tmp_path, a, fixtures) for a in ps_answers]
    assert sh_labels == ps_labels, (
        f"MINDHIVE_HOOK_WALK_LIMIT={limit!r}: lib.sh {sh_labels} vs lib.ps1 {ps_labels}")
    assert sh_labels == expected, f"both libraries agree on the wrong answer: {sh_labels}"


@sh_missing
@ps_missing
@pytest.mark.parametrize("on_path, expected", [
    (["stub", "real"], "real"),
    (["stub"], "NONE"),
    (["real"], "real"),
], ids=["stub-ahead", "stub-alone", "real-only"])
def test_both_libraries_skip_a_store_stub_the_same_way(tmp_path, on_path, expected):
    """The Store stub AHEAD of a real interpreter, through both resolvers.

    `lib.ps1` walked every `Get-Command -All` match; `lib.sh` took `command -v`'s
    first and stopped. The same two PATH directories gave the real interpreter
    on one family and nothing on the other. `stub-alone` and `real-only` are the
    canaries that keep `stub-ahead` from passing by accepting the stub or by
    ignoring PATH."""
    dirs = {"stub": _cmd_exe_copy(tmp_path / "WindowsApps" / "python.exe").parent,
            "real": _cmd_exe_copy(tmp_path / "realbin" / "python.exe").parent}
    chosen = [dirs[k] for k in on_path]
    sh_answers, ps_answers = _both_resolve(
        tmp_path, [tmp_path / "nowhere"],
        sh_path=os.pathsep.join([str(d) for d in chosen]
                                + [_path_without_python(tmp_path)]),
        ps_path=";".join([str(d) for d in chosen] + [r"C:\Windows\System32"]),
        walk_limit="1")
    fixtures = {"stub": dirs["stub"] / "python.exe", "real": dirs["real"] / "python.exe"}
    sh_label = _which_fixture(tmp_path, sh_answers[0], fixtures)
    ps_label = _which_fixture(tmp_path, ps_answers[0], fixtures)
    assert sh_label == ps_label, f"PATH {on_path}: lib.sh {sh_label} vs lib.ps1 {ps_label}"
    assert sh_label == expected, f"both libraries agree on the wrong answer: {sh_label}"


@sh_missing
@ps_missing
@pytest.mark.parametrize("layout, expected", [
    (("Scripts/python.exe", "bin/python.exe"), "Scripts/python.exe"),
    (("bin/python3.exe", "bin/python.exe"), "bin/python3.exe"),
    (("bin/python3.exe",), "bin/python3.exe"),
], ids=["dual-layout", "bin-python3-beside-python", "bin-python3-only"])
def test_both_libraries_take_the_same_venv_leaf(tmp_path, layout, expected):
    """One venv, one interpreter: `Scripts/python.exe`, then `bin/python3`, then
    `bin/python`, in both families.

    The orders used to differ, and a parity probe measured every row here
    giving two answers: the dual layout resolved `bin/python` in `lib.sh` and
    `Scripts\\python.exe` in `lib.ps1`; `bin\\python3.exe` beside
    `bin\\python.exe` resolved `bin/python3` against `bin\\python.exe`; and a
    lone `bin\\python3.exe` resolved in `lib.sh` while `lib.ps1`, with no leaf
    for it, fell through.

    A DIFFERENT real interpreter sits on PATH for both families, so a leaf the
    walk misses shows up as `path` rather than as a silent `NONE`. `expected`
    is asserted as well as equality, so two libraries broken the same way
    cannot pass as parity."""
    repo = tmp_path / "repo"
    fixtures = {leaf: _cmd_exe_copy(repo / ".venv" / leaf) for leaf in layout}
    fixtures["path"] = _cmd_exe_copy(tmp_path / "realbin" / "python.exe")
    hooks = repo / "hooks"
    hooks.mkdir(parents=True)
    realbin = fixtures["path"].parent
    sh_answers, ps_answers = _both_resolve(
        tmp_path, [hooks],
        sh_path=os.pathsep.join([str(realbin), _path_without_python(tmp_path)]),
        ps_path=";".join([str(realbin), r"C:\Windows\System32", r"C:\Windows"]),
        walk_limit="2")
    sh_label = _which_fixture(tmp_path, sh_answers[0], fixtures)
    ps_label = _which_fixture(tmp_path, ps_answers[0], fixtures)
    assert sh_label == ps_label, f"{layout}: lib.sh {sh_label} vs lib.ps1 {ps_label}"
    assert sh_label == expected, f"both libraries agree on the wrong answer: {sh_label}"


@sh_missing
@ps_missing
@pytest.mark.parametrize("on_path, expected", [
    (["wrappers", "exe"], "exe"),
    (["wrappers"], "NONE"),
], ids=["wrappers-ahead-of-exe", "wrappers-alone"])
def test_both_libraries_take_only_an_exe_from_path(tmp_path, on_path, expected):
    """From PATH an interpreter is `python3`/`python` or its `.exe`, never a
    `.cmd` or `.bat` wrapper, in both families.

    `lib.ps1` took any PATHEXT match `Get-Command` offered, so a directory
    holding a `python.cmd` ahead of a real `python.exe` gave it the wrapper --
    which runs through `cmd.exe` and re-parses its arguments -- while `lib.sh`,
    asking PATH only for `<name>` and `<name>.exe`, took the `.exe`. A parity
    probe measured exactly that. `python3.bat` is planted beside `python.cmd`
    so that either extension, if still accepted, would win the name-major scan.

    `wrappers-alone` is the canary: a `lib.ps1` that merely RANKED a `.exe`
    above a wrapper would pass `wrappers-ahead-of-exe` and still answer one
    here."""
    wrappers = tmp_path / "wrapbin"
    wrappers.mkdir()
    (wrappers / "python3.bat").write_bytes(b"@echo off\r\nexit /b 0\r\n")
    (wrappers / "python.cmd").write_bytes(b"@echo off\r\nexit /b 0\r\n")
    exe = _cmd_exe_copy(tmp_path / "exebin" / "python.exe")
    dirs = {"wrappers": wrappers, "exe": exe.parent}
    chosen = [dirs[k] for k in on_path]
    sh_answers, ps_answers = _both_resolve(
        tmp_path, [tmp_path / "nowhere"],
        sh_path=os.pathsep.join([str(d) for d in chosen]
                                + [_path_without_python(tmp_path)]),
        ps_path=";".join([str(d) for d in chosen] + [r"C:\Windows\System32"]),
        walk_limit="0")
    fixtures = {"python3.bat": wrappers / "python3.bat",
                "python.cmd": wrappers / "python.cmd", "exe": exe}
    sh_label = _which_fixture(tmp_path, sh_answers[0], fixtures)
    ps_label = _which_fixture(tmp_path, ps_answers[0], fixtures)
    assert sh_label == ps_label, f"PATH {on_path}: lib.sh {sh_label} vs lib.ps1 {ps_label}"
    assert sh_label == expected, f"both libraries agree on the wrong answer: {sh_label}"


@sh_missing
@ps_missing
def test_both_libraries_read_mindhive_python_spellings_the_same_way(tmp_path):
    """`MINDHIVE_PYTHON` names one file in both families, however a Windows
    operator spells it.

    Under Git Bash the MSYS runtime does two things for `lib.sh` that `lib.ps1`
    did not: it reads `/c/...` as drive C, in either letter case, and it
    retries a name that does not exist with `.exe`. A parity probe measured the
    gap: an extensionless path and a `/c/` path each gave the override in
    `lib.sh` and the venv in `lib.ps1`.

    One table, one process per family. The rows that need a word:
      * `windows` is the CANARY -- the plain spelling resolves in both, so the
        override tier is live and every other row tests only a spelling.
      * `dotted-name` is `.../python3.12`, whose `.12` is no extension anyone
        meant; Git Bash completes it to `python3.12.exe` (measured).
      * `directory-shadowing-exe` names a DIRECTORY `python` with a
        `python.exe` beside it. Git Bash does not complete a name that exists
        (measured), so both families refuse it and walk to the venv, rather
        than one of them quietly running the file next door."""
    override = _cmd_exe_copy(tmp_path / "override" / "python.exe")
    dotted = _cmd_exe_copy(tmp_path / "override" / "python3.12.exe")
    shadowed = _cmd_exe_copy(tmp_path / "shadowed" / "python.exe")
    (tmp_path / "shadowed" / "python").mkdir()
    venv = _cmd_exe_copy(tmp_path / "repo" / ".venv" / "Scripts" / "python.exe")
    hooks = tmp_path / "repo" / "hooks"
    hooks.mkdir(parents=True)

    win = str(override)
    assert win[1:3] == ":\\", f"not a drive-letter path: {win}"

    def posix(value, upper=False):
        drive = value[0].upper() if upper else value[0].lower()
        return "/" + drive + "/" + value[3:].replace("\\", "/")

    rows = [
        # (id, MINDHIVE_PYTHON, the fixture both families must name)
        ("windows", win, "override"),
        ("windows-no-exe", win[:-4], "override"),
        ("posix-drive", posix(win), "override"),
        ("posix-drive-no-exe", posix(win[:-4]), "override"),
        ("posix-upper-drive", posix(win, upper=True), "override"),
        ("dotted-name", str(dotted)[:-4], "dotted"),
        ("directory-shadowing-exe", str(shadowed)[:-4], "venv"),
    ]
    sh_answers, ps_answers = _both_resolve(
        tmp_path, [hooks] * len(rows),
        sh_path=_path_without_python(tmp_path),
        ps_path=r"C:\Windows\System32;C:\Windows",
        walk_limit="2",
        overrides=[value for _, value, _ in rows])
    fixtures = {"override": override, "dotted": dotted,
                "shadowed-exe": shadowed, "venv": venv}
    wrong = []
    for (row, value, expected), sh_answer, ps_answer in zip(rows, sh_answers, ps_answers):
        sh_label = _which_fixture(tmp_path, sh_answer, fixtures)
        ps_label = _which_fixture(tmp_path, ps_answer, fixtures)
        if not sh_label == ps_label == expected:
            wrong.append(f"{row} ({value}): lib.sh {sh_label}, lib.ps1 {ps_label}, "
                         f"want {expected}")
    assert wrong == [], "; ".join(wrong)


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
    """`unknown`, never the raw value: the id names a ledger segment. The
    Python side would quarantine a hostile one (`events.segment_name` maps it
    to `unsafe-<sha256[:16]>.jsonl`), but the shim refuses it before any child
    sees it, so a nested object or a path never reaches an environment or an
    argv at all. A leading `-` is refused too, so no id can read as an option
    to the child that receives it after `--session`.

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
    """The shell whitelist in `mindhive_session_id` judges what the
    interpreter PRINTED, not what the payload held. A `sitecustomize` line or a
    warning on stdout would otherwise become the id, so whatever comes back
    through `extract_json_field` is checked in the shell, whole."""
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


# --- extract_json_field: JSON without jq (MEB HOOK-01, spec Bug 10) ------------
#
# The spec's Bug 10 is a `jq` call that fails SILENTLY wherever `jq` is not
# installed -- this machine included (TODOS item 14). MEB HOOK-01's answer is
# `extract_json_field <py> <field> <json>`: the payload goes to the resolved
# interpreter's stdin and one top-level value comes back, or nothing, with rc 0
# either way, because a hook that exits non-zero is a hook Claude Code reports
# as failed. `mindhive_session_id` reads the id through it and keeps its own
# whitelist on whatever comes back.
#
# Every probe hands the helper `sys.executable`: the helper takes its
# interpreter as an argument, and finding one is `mindhive_python`'s job,
# pinned above.

_SOURCE_LIB = f'. "{(HOOKS / "lib.sh").as_posix()}"; '
_PY = _sh_quote(sys.executable)
SESSION_ABC = '{"session_id":"abc"}'


def _field_probe(field, raw) -> str:
    """A probe script that reads *field* out of *raw* the way a shim would --
    interpreter, field, payload, captured by `$(...)` -- and prints
    `[<value>] rc=<status>`. `rc=$?` after `got=$(...)` is the substitution's
    own status, which POSIX gives an assignment that has no command name."""
    return (_SOURCE_LIB
            + f"got=$(extract_json_field {_PY} {_sh_quote(field)} {_sh_quote(raw)}); "
            + 'rc=$?; printf "[%s] rc=%s\\n" "$got" "$rc"')


def _jq_trap(tmp_path):
    """A `jq` first on PATH that records each call and answers `abc`, the value
    every probe below asks for. An implementation leaning on jq therefore gets
    the RIGHT answer from it -- here, and on a CI image that ships a real jq
    alike -- so only the record can tell. Answers `(path, log)`."""
    bindir = tmp_path / "jqbin"
    bindir.mkdir()
    log = tmp_path / "jq.log"
    jq = bindir / "jq"
    jq.write_text("#!/bin/sh\n"
                  f"printf 'jq %s\\n' \"$*\" >> '{log.as_posix()}'\n"
                  "cat >/dev/null\n"
                  "printf 'abc\\n'\n",
                  encoding="utf-8", newline="\n")
    jq.chmod(0o755)
    return os.pathsep.join([str(bindir), os.environ.get("PATH", "")]), log


@sh_missing
@pytest.mark.parametrize("field, raw, value", [
    ("session_id", SESSION_ABC, "abc"),
    ("tool_name", '{"session_id": "abc", "tool_name": "Bash", "cwd": "/x"}', "Bash"),
], ids=["session-id", "named-field"])
def test_extract_json_field_answers_the_named_top_level_string(field, raw, value):
    """The MEB contract's own example, `'{"session_id":"abc"}'` -> `abc`: the
    bare value, with no JSON quotes around it and rc 0. `named-field` asks for
    a key that is neither first nor `session_id`, so a helper that ignored its
    `<field>` argument, or answered the first value it met, prints the wrong
    string."""
    got = probe(_field_probe(field, raw))
    assert got.stderr == "", got.stderr
    assert got.stdout == f"[{value}] rc=0\n", got.stdout


@sh_missing
@pytest.mark.parametrize("raw", [
    '{"session_id": "abc"',
    '{"session_id": "abc"} and then prose',
    "not json at all",
    "",
], ids=["truncated", "trailing-garbage", "prose", "empty"])
def test_extract_json_field_answers_nothing_with_rc_0_for_corrupt_json(raw):
    """Corrupt JSON is no value, not a failure: the hook that asked still exits
    0. `truncated` and `trailing-garbage` each carry a well-formed
    `"session_id": "abc"` pair, so a pattern match over the text -- the
    approach this library's own commentary calls wrong the first time a value
    carries an escaped quote -- answers `abc` where a parser answers nothing."""
    got = probe(_field_probe("session_id", raw))
    assert got.stderr == "", got.stderr
    assert got.stdout == "[] rc=0\n", got.stdout


@sh_missing
@pytest.mark.parametrize("raw", [
    '[{"session_id": "abc"}]',
    '{"other": "session_id"}',
    '{"nested": {"session_id": "abc"}}',
    '{"session_id": null}',
], ids=["array", "missing", "nested-only", "null"])
def test_extract_json_field_answers_nothing_with_rc_0_without_a_top_level_value(raw):
    """Well-formed JSON with no top-level value under the key. `array` and
    `nested-only` hide a `session_id` one level down and `missing` carries the
    key's NAME as a value, so a search through the document, rather than a
    lookup on its top level, answers `abc` or `session_id` for them. `null` is
    the MEB's own `val is not None` branch: nothing, not the text `None`."""
    got = probe(_field_probe("session_id", raw))
    assert got.stderr == "", got.stderr
    assert got.stdout == "[] rc=0\n", got.stdout


@sh_missing
def test_extract_json_field_round_trips_spaces_quotes_and_unicode():
    """A string value comes back byte for byte, every hazard a shell adds
    included: doubled, leading and trailing spaces (a capture keeps them, only
    trailing newlines go); an escaped double quote and a single quote; a
    literal `$HOME`; a backslash, which dash's `echo` would read as an escape
    where `printf '%s'` does not; and non-ASCII twice over, as JSON escapes and
    as raw UTF-8, both of which must come back as the same bytes. A pattern
    match hands the escapes back as written, a trim or a word split loses the
    spaces, and a payload spliced into program text rather than piped to stdin
    breaks on the quote.

    The comparison is made INSIDE the shell, on bytes, and reports `SAME` or
    the bytes that came back, so the test process's own decoding of the
    child's stdout (cp1252 on this machine) takes no part in it.

    The payload and the expected value reach the shell through its
    environment, the way a shim holds its payload in a variable, and not
    through the script text. On Windows `subprocess` joins argv into one
    command line and the MSYS runtime splits it back for Git Bash's `sh`,
    reading a doubled backslash inside a quoted argument as a single one:
    spliced into the script, the JSON escape for the backslash in `C:\\new`
    arrived as `\\n`, a newline (measured). A hook's payload comes in on stdin
    and never meets that layer.

    `PYTHONIOENCODING=utf-8` pins the interpreter's stdio to what every POSIX
    locale gives Python 3.7+ (PEP 538/540) and what this machine's own
    environment sets. Unset, a Windows pipe speaks the ANSI code page:
    `sys.stdout.write` raises on the CJK characters inside the helper's `try`
    and the answer is nothing, rc 0 (measured) -- the next test's contract,
    not a round trip."""
    raw = ('{"v": "  two  spaces, \\"quoted\\", it\'s $HOME, C:\\\\new, '
           'caf\\u00e9 = café, \\u65e5\\u672c = 日本  "}')
    want = '  two  spaces, "quoted", it\'s $HOME, C:\\new, café = café, 日本 = 日本  '
    script = (_SOURCE_LIB
              + f'got=$(extract_json_field {_PY} v "$MH_RAW"); rc=$?; '
              + 'if [ "$got" = "$MH_WANT" ]; then echo "SAME rc=$rc"; '
              + 'else printf "DIFF rc=%s:" "$rc"; printf "%s" "$got" | od -An -tx1; fi')
    got = probe(script, env={"PYTHONIOENCODING": "utf-8",
                             "MH_RAW": raw, "MH_WANT": want})
    assert got.stderr == "", got.stderr
    assert got.stdout.strip() == "SAME rc=0", got.stdout


@sh_missing
def test_extract_json_field_answers_nothing_for_a_value_it_cannot_write():
    """A value the interpreter's stdout cannot encode is no value: nothing,
    rc 0, and no traceback on the hook's stderr -- never a `?`-mangled or
    re-escaped string a caller would take for the real one.
    `PYTHONIOENCODING=ascii` makes `café` unwritable on every platform, and the
    payload itself is pure ASCII (`\\u00e9`), so it is the WRITE that fails,
    inside the helper's `try`, and not the read."""
    got = probe(_field_probe("v", '{"v": "caf\\u00e9"}'),
                env={"PYTHONIOENCODING": "ascii"})
    assert got.stderr == "", got.stderr
    assert got.stdout == "[] rc=0\n", got.stdout


# A field name built to close the `get('` it would be spliced into and run a
# statement of its own, one that leaves `PWNED` in the working directory.
HOSTILE_FIELD = "x'); open('PWNED','w').close() #"


def _probe_in(cwd, script):
    """`probe`, run from *cwd*, which the helper's interpreter inherits: a
    statement that creates a relative file creates it there, never in the
    checkout."""
    child = dict(os.environ)
    for key in ("MINDHIVE_PYTHON", "MINDHIVE_HOOK_WALK_LIMIT", "MINDHIVE_HEADLESS"):
        child.pop(key, None)
    return subprocess.run([SH, "-c", script], capture_output=True, text=True,
                          shell=False, cwd=str(cwd), env=child, timeout=30)


@sh_missing
def test_extract_json_field_never_runs_a_field_name_as_code(tmp_path):
    """The field name is data. Spliced into the program the way the MEB's text
    does it (`data.get('$field_name', '')`), `HOSTILE_FIELD` ends the string
    and runs `open('PWNED','w')` -- and the helper still prints nothing and
    exits 0, so the marker file is the only witness. Handed over as an
    argument, it is only an odd key: absent, so nothing, rc 0."""
    got = _probe_in(tmp_path, _field_probe(HOSTILE_FIELD, SESSION_ABC))
    assert got.stderr == "", got.stderr
    assert got.stdout == "[] rc=0\n", got.stdout
    assert not (tmp_path / "PWNED").exists(), "the field name ran as code"


@sh_missing
def test_the_pwned_marker_records_a_field_name_spliced_into_the_program(tmp_path):
    """CANARY for the test above. A helper that splices `<field>` the MEB's
    way, run on the same name from the same directory, must leave the marker
    behind -- so its absence above means the name never ran, not that the
    statement could not have created the file there."""
    spliced = ("extract_json_field() { printf '%s' \"$3\" | \"$1\" -c "
               "\"import sys, json; data = json.load(sys.stdin); "
               "val = data.get('$2', '')\" 2>/dev/null; }; ")
    got = _probe_in(tmp_path, _SOURCE_LIB + spliced
                    + f"extract_json_field {_PY} {_sh_quote(HOSTILE_FIELD)} "
                    + _sh_quote(SESSION_ABC))
    assert got.returncode == 0, got.stderr
    assert (tmp_path / "PWNED").exists(), (got.stdout, got.stderr)


@sh_missing
def test_neither_json_reader_in_the_library_runs_jq(tmp_path):
    """Bug 10's defect is a DEPENDENCY, not a wrong answer. Where `jq` is
    missing -- this machine, a stock Windows box -- a jq-based read fails in
    silence; where it is installed, as on the ubuntu CI image, it answers
    correctly and hides the dependency. So both readers run with a recording
    `jq` first on PATH, and the assertion is that nothing called it."""
    path, log = _jq_trap(tmp_path)
    script = (_SOURCE_LIB
              + f"printf '[%s]\\n' \"$(extract_json_field {_PY} session_id "
              + f"{_sh_quote(SESSION_ABC)})\"; "
              + f"mindhive_session_id {_PY} {_sh_quote(SESSION_ABC)}")
    got = probe(script, env={"PATH": path})
    assert got.stderr == "", got.stderr
    assert got.stdout.split() == ["[abc]", "abc"], got.stdout
    assert not log.exists(), "jq was called: " + log.read_text(encoding="utf-8")


@sh_missing
def test_the_jq_trap_records_a_jq_based_extract_json_field(tmp_path):
    """CANARY for the test above. The same trap, with `extract_json_field`
    replaced by the shape spec 10.14.3 retires -- `jq -r '.<field> // empty'`
    -- must record the call. Without it, a trap `sh` never reached (a PATH it
    did not read, a stub it could not run) would leave the log empty for every
    implementation and the test above green by construction. The value that
    comes back is the RIGHT one, which is why the log and not the answer is
    what the test above asserts."""
    path, log = _jq_trap(tmp_path)
    script = (_SOURCE_LIB
              + 'extract_json_field() { printf "%s" "$3" | jq -r ".$2 // empty"; }; '
              + f"printf '[%s]\\n' \"$(extract_json_field {_PY} session_id "
              + f"{_sh_quote(SESSION_ABC)})\"")
    got = probe(script, env={"PATH": path})
    assert got.stdout.split() == ["[abc]"], (got.stdout, got.stderr)
    assert log.exists(), "the recording jq never ran, so its silence proves nothing"
    assert log.read_text(encoding="utf-8").splitlines() == ["jq -r .session_id // empty"]


@sh_missing
def test_mindhive_session_id_reads_the_id_through_extract_json_field(tmp_path):
    """The plan's wiring, asserted: `mindhive_session_id` asks
    `extract_json_field` for `session_id`, handing it the interpreter and the
    raw payload in the MEB's order. A shell function is looked up when it is
    CALLED, so one defined after the library is sourced stands in for the
    helper in this probe. Both the helper and an inline parse print `abc` for
    this payload; a stand-in that answers something else is the only way to
    see which of them did the reading."""
    seen = tmp_path / "helper_args.txt"
    script = (_SOURCE_LIB
              + "extract_json_field() { "
              + f"printf '%s\\n' \"$1\" \"$2\" \"$3\" > '{seen.as_posix()}'; "
              + "printf '%s' via-helper; }; "
              + f"mindhive_session_id {_PY} {_sh_quote(SESSION_ABC)}")
    got = probe(script)
    assert got.stderr == "", got.stderr
    assert got.stdout == "via-helper\n", (
        "mindhive_session_id parsed the payload itself instead of reading it "
        f"through extract_json_field: {got.stdout!r}")
    assert seen.read_text(encoding="utf-8").splitlines() == [
        sys.executable, "session_id", SESSION_ABC]


@sh_missing
@pytest.mark.parametrize("returned", [
    "../../escaped", "with/slash", "a b", "-flag", "x" * 129, "{'a': 1}",
    "abc\nzzz", "abc\n", "", "..", ".x", "_x",
], ids=["traversal", "slash", "space", "flag", "overlong", "object-repr",
        "two-lines", "trailing-newline", "empty", "dot-dot", "dot-led",
        "underscore-led"])
def test_mindhive_session_id_whitelists_whatever_extract_json_field_returns(returned):
    """The whitelist judges the helper's ANSWER. The payload is a clean `abc`,
    so an implementation that bypassed the helper passes no row below; the
    stand-in helper then returns each shape an id must never take.
    `object-repr` is what Python's `str()` makes of a JSON object, and
    `trailing-newline` is the one a plain `$(...)` capture would trim into a
    valid-looking `abc` before the whitelist ever saw it."""
    script = (_SOURCE_LIB
              + f"extract_json_field() {{ printf '%s' {_sh_quote(returned)}; }}; "
              + f"mindhive_session_id {_PY} {_sh_quote(SESSION_ABC)}")
    got = probe(script)
    assert got.stderr == "", got.stderr
    assert got.stdout == "unknown\n", got.stdout


@sh_missing
@pytest.mark.parametrize("status, want", [(0, "abc"), (1, "unknown")],
                         ids=["read-succeeded", "read-failed"])
def test_mindhive_session_id_keeps_nothing_a_failed_read_printed(status, want):
    """A read that exits non-zero answers `unknown`, whatever it printed
    first: the `|| _mh_sid=""` the inline parse always carried, kept across
    the delegation. Both rows print the same clean `abc`, and `read-succeeded`
    is the canary that the whitelist keeps it, so only the exit status decides
    `read-failed`. The helper's status is its interpreter's, non-zero only when
    the interpreter itself failed -- missing, unrunnable, killed -- which is
    exactly when nothing it printed can be trusted."""
    script = (_SOURCE_LIB
              + f"extract_json_field() {{ printf '%s' abc; return {status}; }}; "
              + f"mindhive_session_id {_PY} {_sh_quote(SESSION_ABC)}")
    got = probe(script)
    assert got.stderr == "", got.stderr
    assert got.stdout == f"{want}\n", got.stdout


@sh_missing
@pytest.mark.parametrize("value", [
    "42", "1.5", "true", "false", "Infinity", '"abc\\n"', '"a\\u0000b"',
], ids=["int", "float", "true", "false", "infinity", "trailing-newline", "nul"])
def test_a_session_id_the_shell_would_misread_is_unknown(value):
    """Held by the `-c` program this family used to run, which refused a
    non-string and whitelisted inside Python, and pinned here because a
    delegation that judges only the helper's printed text loses every row --
    measured on this machine with the MEB helper verbatim behind the
    unchanged shell whitelist: `str()` prints `42`, `1.5`, `True`, `False` and
    `inf`, each a valid-looking id; `$(...)` trims `abc` plus a newline to
    `abc`; and it drops the NUL from `a`, NUL, `b`, leaving `ab` and printing
    bash's "ignored null byte" warning on the hook's stderr.

    `lib.ps1`'s `Get-MindhiveSessionId` answers `unknown` for all seven
    (measured), so this is also the two families agreeing."""
    raw = '{"session_id": %s}' % value
    got = probe(_SOURCE_LIB + f"mindhive_session_id {_PY} {_sh_quote(raw)}")
    assert got.stderr == "", got.stderr
    assert got.stdout == "unknown\n", got.stdout


@sh_missing
@pytest.mark.parametrize("value", ["42", "True", "inf"])
def test_a_string_session_id_that_only_looks_numeric_is_kept(value):
    """CANARY for the test above: the refusal is about the JSON TYPE, not the
    spelling. These tokens, as strings, are ordinary ids today in this family
    and in `lib.ps1` (measured), so a fix that refused anything shaped like a
    number or a boolean would pass the test above and fail here."""
    raw = '{"session_id": "%s"}' % value
    got = probe(_SOURCE_LIB + f"mindhive_session_id {_PY} {_sh_quote(raw)}")
    assert got.stderr == "", got.stderr
    assert got.stdout == f"{value}\n", got.stdout


@sh_missing
@pytest.mark.parametrize("value", [".", "..", ".x", "_", "_x"],
                         ids=["dot", "dot-dot", "dot-led", "underscore",
                              "underscore-led"])
def test_a_session_id_opening_with_a_dot_or_underscore_is_unknown(value):
    """A letter or a digit first: the rule `events.SESSION_ID_RE` and
    `lib.ps1` state, and the one the `-c` program this family used to run
    enforced inside Python. Behind a helper that answers any string, only the
    shell whitelist is left to enforce it, and that refused a leading `-`
    alone -- so all five rows, `..` among them, came back as themselves where
    the inline parse answered `unknown` (measured; `lib.ps1` answers `unknown`
    for all five)."""
    raw = '{"session_id": "%s"}' % value
    got = probe(_SOURCE_LIB + f"mindhive_session_id {_PY} {_sh_quote(raw)}")
    assert got.stderr == "", got.stderr
    assert got.stdout == "unknown\n", got.stdout


@sh_missing
@pytest.mark.parametrize("value", ["x.", "a..b", "a_b", "0.x"])
def test_a_dot_or_underscore_after_the_first_character_is_kept(value):
    """CANARY for the test above: the rule is about the FIRST character, not
    about dots or underscores. Each of these is an id before and after the
    delegation, in this family and in `lib.ps1` (measured), so a fix that
    refused every `.` or `_`, or every `..`, would pass the test above and
    fail here."""
    raw = '{"session_id": "%s"}' % value
    got = probe(_SOURCE_LIB + f"mindhive_session_id {_PY} {_sh_quote(raw)}")
    assert got.stderr == "", got.stderr
    assert got.stdout == f"{value}\n", got.stdout


# --- session_start checks the plugin cache (spec Bug 8) -------------------------
#
# Claude Code runs a plugin from its COPY under `~/.claude/plugins/cache/`, so a
# copy older than this checkout runs instructions the repository no longer
# holds, and nothing says so. `mindhive plugins cache-check --hook` is the check
# in its session-start form: one line on drift, nothing otherwise, exit 0
# whatever happens. The shim's share is to run it after `state`, forward its
# line into the new session's context the way it forwards `state`'s, and keep
# any failure of it off the hook's stderr. `tests/test_hook_shims_ps.py` holds
# the same three properties for `session_start.ps1`.

CACHE_CHECK = "-m mindhive plugins cache-check --hook"
# The BRIEF forms, never the full ones: the whole of the hook's stdout has to
# stay under the session-start cap (`mindhive.sessionstart`, 6,000 characters)
# for Claude Code to deliver it rather than persist it to a file and show the
# session a 2 KB preview. `tests/test_sessionstart.py` locks the cap itself.
DIRECTIVES = "-m mindhive directives --brief"
STATE = "-m mindhive state --brief"


def _session_start_python(tmp_path, fail=""):
    """A stub interpreter for `session_start`. Every `-m` call appends its argv
    to a log and prints `child stdout: <argv>`, so the hook's stdout shows
    which calls it forwarded, in order; `-c` goes to the real interpreter, as
    with `fake_python`. A call whose argv contains *fail* prints
    `child stderr: <argv>` on stderr and exits 3 instead. Answers
    `(env, log)`."""
    log = tmp_path / "calls.log"
    exe = tmp_path / "session-start-python"
    exe.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then shift; exec "$STUB_REAL_PY" -c "$@"; fi\n'
        f"printf '%s\\n' \"$*\" >> '{log.as_posix()}'\n"
        'if [ -n "$STUB_FAIL" ]; then\n'
        '    case "$*" in\n'
        "        *\"$STUB_FAIL\"*) printf 'child stderr: %s\\n' \"$*\" >&2; exit 3 ;;\n"
        "    esac\n"
        "fi\n"
        "printf 'child stdout: %s\\n' \"$*\"\n",
        encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    return ({"MINDHIVE_PYTHON": str(exe), "STUB_REAL_PY": sys.executable,
             "STUB_FAIL": fail}, log)


@sh_missing
def test_session_start_checks_the_plugin_cache_right_after_state(tmp_path):
    """The check is the very next call after `state`, and its answer reaches
    the hook's stdout -- the new session's context -- right after the state it
    qualifies, the way `state`'s own answer does. The stub prints for EVERY
    call, so the exact stdout also pins what is not forwarded: the `append`
    bookkeeping and the detached background calls."""
    env, log = _session_start_python(tmp_path)
    got = run_shim("session_start", env=env)
    assert got.returncode == 0, got.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert STATE in calls, calls
    at = calls.index(STATE)
    assert calls[at + 1:at + 2] == [CACHE_CHECK], calls
    assert got.stdout.splitlines() == [f"child stdout: {DIRECTIVES}",
                                       f"child stdout: {STATE}",
                                       f"child stdout: {CACHE_CHECK}"], got.stdout


@sh_missing
def test_a_failing_plugin_cache_check_leaves_session_start_quiet_and_exit_0(tmp_path):
    """`--hook` is fail-open on the Python side, and the shim does not lean on
    that: a check that dies loudly -- a line on stderr, exit 3 -- costs the
    session its one line and nothing else. The hook exits 0, prints nothing on
    stderr, and forwards exactly what it forwards without the check. The log
    proves the check RAN, so the quiet is the shim's doing rather than a call
    that never happened, and the stub run bare proves the failure is loud."""
    env, log = _session_start_python(tmp_path, fail="plugins cache-check")
    got = run_shim("session_start", env=env)
    assert got.returncode == 0, got.stderr
    assert got.stderr == "", got.stderr
    assert CACHE_CHECK in log.read_text(encoding="utf-8").splitlines()
    assert got.stdout.splitlines() == [f"child stdout: {DIRECTIVES}",
                                       f"child stdout: {STATE}"], got.stdout
    bare = subprocess.run([SH, env["MINDHIVE_PYTHON"], *CACHE_CHECK.split()],
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, **env})
    assert (bare.returncode, bare.stdout, bare.stderr) == (
        3, "", f"child stderr: {CACHE_CHECK}\n")


# --- session_start names its failure on stderr when it fails open ---------------
#
# The POSIX half of the same section in `tests/test_hook_shims_ps.py`, which
# says why: Claude Code keeps a SessionStart hook's transcript row only when its
# content, stdout or stderr is non-empty, and `hive spend` files a typed session
# whose MindHive SessionStart left no row as a headless child. One line on
# stderr, the first failure only; never on stdout, never in a headless child.

@sh_missing
@pytest.mark.parametrize("fail, forwarded", [
    ("append session_start", [DIRECTIVES, STATE, CACHE_CHECK]),
    ("directives --brief", [STATE, CACHE_CHECK]),
    ("state --brief", [DIRECTIVES, CACHE_CHECK]),
], ids=["append", "directives", "state"])
def test_a_failing_mindhive_call_is_named_in_one_stderr_line(tmp_path, fail, forwarded):
    """Each checked call failing alone: the hook exits 0, names that call and
    its exit code in one line, keeps the child's own stderr discarded, and
    still forwards every other call's context."""
    env, _ = _session_start_python(tmp_path, fail=fail)
    got = run_shim("session_start", env=env)
    assert got.returncode == 0, got.stderr
    assert got.stderr.splitlines() == [f"{FAILED_OPEN}mindhive {fail} exited 3"], got.stderr
    assert got.stdout.splitlines() == [f"child stdout: {c}" for c in forwarded], got.stdout


@sh_missing
def test_only_the_first_failure_is_named(tmp_path):
    """Every call fails: still exactly ONE line, naming the first, while the
    log shows the later calls ran all the same."""
    env, log = _session_start_python(tmp_path, fail="-m mindhive")
    got = run_shim("session_start", env=env)
    assert got.returncode == 0, got.stderr
    assert got.stderr.splitlines() == [
        f"{FAILED_OPEN}mindhive append session_start exited 3"], got.stderr
    assert got.stdout == "", got.stdout
    assert STATE in log.read_text(encoding="utf-8").splitlines()


@sh_missing
def test_a_session_start_that_succeeds_says_nothing_on_stderr(tmp_path):
    """The line marks a failure, so a run that succeeds writes nothing on
    stderr. CANARY: the same fixture with one call failing does write the
    line, so this silence is a verdict rather than a shim that cannot speak."""
    env, _ = _session_start_python(tmp_path)
    ok = run_shim("session_start", env=env)
    assert ok.returncode == 0, ok.stderr
    assert ok.stderr == "", ok.stderr
    bad = run_shim("session_start", env={**env, "STUB_FAIL": "state --brief"})
    assert len(bad.stderr.splitlines()) == 1, bad.stderr


@sh_missing
def test_a_headless_session_start_says_nothing_where_it_would_fail_open(tmp_path):
    """`MINDHIVE_HEADLESS=1` still exits on the first line, before the line
    could be written: no call, no stdout, no stderr. It runs first, so no
    background call of the canary can reach the log. CANARY: the same fixture
    without the flag names its failure."""
    env, log = _session_start_python(tmp_path, fail="-m mindhive")
    quiet = run_shim("session_start", env={**env, "MINDHIVE_HEADLESS": "1"})
    assert (quiet.returncode, quiet.stdout, quiet.stderr) == (0, "", ""), quiet
    assert not log.exists(), log.read_text(encoding="utf-8")
    loud = run_shim("session_start", env=env)
    assert loud.returncode == 0, loud.stderr
    assert len(loud.stderr.splitlines()) == 1, loud.stderr
