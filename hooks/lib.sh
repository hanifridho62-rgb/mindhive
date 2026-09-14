# hooks/lib.sh — what every POSIX hook shim needs, and nothing else.
#
# Sourced, never executed. `tests/test_hook_shims.py` asserts that every
# `hooks/*.ps1` has a `.sh` sibling and the reverse, so this file is the one
# exception on the POSIX side and the test names it explicitly.
#
# ## Why the interpreter is RESOLVED, in both families
#
# Every `.ps1` shim used to open with
#
#     $py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
#
# which is correct on exactly one machine, for one username, for one checkout.
# This library was written so the POSIX family would not repeat that, and
# `hooks/lib.ps1` has since ported the same resolver back: no shim in either
# family names an interpreter path any more, and
# `test_no_shim_hard_codes_an_interpreter_path` fails on a hard-code in either
# spelling. So this resolves, in a fixed order, and answers nothing when it
# cannot — the caller then exits 0 and the hook is a silent no-op, which is the
# only acceptable failure for a hook.
#
# ## The order, and why the venv beats PATH
#
# `lib.ps1`'s `Get-MindhivePython` follows the same order and names the same
# file for one tree, and the parity tests in `tests/test_hook_shims.py` run one
# fixture tree through both libraries to keep it that way:
#
#   1. $MINDHIVE_PYTHON, when it is an executable file. The operator's
#      override, honoured by both families because the environment a hook runs
#      in is the operator's own. Under Git Bash the MSYS runtime reads a
#      `C:\...` and a `/c/...` spelling alike, and retries a name that does not
#      exist with `.exe` (measured: `.../python` and `.../python3.12` resolve
#      to their `.exe`; a DIRECTORY named `python` beside `python.exe` does
#      not). `lib.ps1` does both explicitly, so one value means one file.
#   2. The nearest `.venv` above this file, probing `Scripts/python.exe`, then
#      `bin/python3`, then `bin/python`. `mindhive` is installed EDITABLE in
#      it, so in a plain checkout it is the interpreter that imports the code
#      the hook belongs to. A PATH interpreter would import whatever it happens
#      to find, or nothing. One order in both families, so one tree gets one
#      answer: `Scripts/` is the layout CPython's `venv` creates on Windows,
#      where bash can exec the `.exe`, and on POSIX a
#      `.venv/Scripts/python.exe` never exists, so there this is the same
#      `bin/` probe it always was. `lib.ps1` names the last two
#      `bin\python3.exe` and `bin\python.exe`, because `&` cannot run an
#      extensionless file; Git Bash answers `bin/python3` and `bin/python`
#      for exactly those `.exe` files.
#   3. `python3`, then `python`, from PATH — as a last resort.
#
# The walk up from this file (rather than `git rev-parse --git-common-dir`)
# keeps the resolution to pure shell. `post_tool_use` fires on EVERY tool call,
# and a process spawn there is a tax on every tool call in every session; the
# measured cost of a spawn on this machine is ~64 ms, and a `git` call to find
# a directory the filesystem already knows about is not worth paying for it.
# The walk also reaches a git worktree's parent venv: a checkout under
# `<repo>/.claude/worktrees/<name>` walks up to `<repo>/.venv`. That venv's
# editable install imports `<repo>/src`, NOT the worktree's, so a hook run from
# a worktree executes the main checkout's `mindhive`. It is still the right
# fallback — a worktree with no `.venv` of its own has no interpreter that could
# import it — but it does not preview the worktree's code.
#
# ## Why a PATH hit under WindowsApps is REFUSED
#
# On Windows with App Execution Aliases on — the default — a bare `python` or
# `python3` resolves to `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`, the
# Microsoft Store redirector. It is not a missing file: it spawns cleanly,
# prints an install prompt, and exits 49. So a candidate loop that only rejects
# what does not exist would ACCEPT it and every hook would silently do nothing
# with an interpreter that never ran a line. `src/mindhive/paths.py`'s
# `resolve_interpreter` refuses it on the same grounds, for the same reason.

# How far up to look for the venv. Deep enough for a worktree under
# `<repo>/.claude/worktrees/<name>/hooks`, bounded so a hook can never walk to
# the filesystem root on a machine where the layout is not what we expect.
# Anything but a run of digits — unset, empty, `abc` — is the default, because
# `[ "$n" -lt abc ]` is an error on the hook's stderr that also skips the walk
# (bash's `sh` prints "integer expression expected", dash prints "Illegal
# number"), and a hook's stderr is what Claude Code shows the user when it
# decides the hook failed. `0` IS a run of digits and means "do not walk" — what
# an operator sets when the nearest `.venv` belongs to another project;
# `lib.ps1` reads `0` the same way, and the parity tests hold it there.
# A run of ten or more digits is the default too (`??????????*`), because shells
# do not agree about a number that size. Measured with twenty nines: bash's
# `sh` (Git for Windows) prints "integer expression expected" and skips the
# walk, while dash prints nothing and `[ 0 -lt 99999999999999999999 ]` is simply
# true, so the walk would stop only at the filesystem root. Other shells may
# differ again. The pattern answers 8 before `[` ever sees the number, in every
# shell, and it matches `lib.ps1`, which bounds the run at nine digits.
case "${MINDHIVE_HOOK_WALK_LIMIT:-}" in
    ''|*[!0-9]*|??????????*) MINDHIVE_HOOK_WALK_LIMIT=8 ;;
esac

# True when *path* sits in the Windows App Execution Alias directory.
mindhive_is_store_stub() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        */windowsapps/*) return 0 ;;
        *) return 1 ;;
    esac
}

# Echo the first usable interpreter, or nothing. Never fails the caller.
mindhive_python() {
    _mh_candidate=""

    # `-f` as well as `-x`: a directory passes `-x` on its own, and an
    # operator's typo would otherwise be the interpreter every hook execs.
    if [ -n "${MINDHIVE_PYTHON:-}" ] && [ -f "${MINDHIVE_PYTHON}" ] \
       && [ -x "${MINDHIVE_PYTHON}" ]; then
        printf '%s\n' "${MINDHIVE_PYTHON}"
        return 0
    fi

    # 2. the nearest .venv above this file. The physical cwd, not `.`, when
    #    the caller set nothing: `dirname .` is `.`, so a literal dot would end
    #    the walk after a single probe.
    _mh_dir=${MINDHIVE_HOOK_DIR:-$(pwd -P)}
    _mh_n=0
    while [ "$_mh_n" -lt "$MINDHIVE_HOOK_WALK_LIMIT" ]; do
        for _mh_candidate in "$_mh_dir/.venv/Scripts/python.exe" \
                             "$_mh_dir/.venv/bin/python3" \
                             "$_mh_dir/.venv/bin/python"; do
            if [ -f "$_mh_candidate" ] && [ -x "$_mh_candidate" ]; then
                printf '%s\n' "$_mh_candidate"
                return 0
            fi
        done
        _mh_parent=$(dirname "$_mh_dir")
        [ "$_mh_parent" = "$_mh_dir" ] && break
        _mh_dir=$_mh_parent
        _mh_n=$((_mh_n + 1))
    done

    # 3. PATH, with the Store redirector refused.
    #
    #    EVERY entry on PATH, not just `command -v`'s first hit -- that is what
    #    makes the refusal a SKIP rather than a STOP. Windows ships the App
    #    Execution Alias redirector in `%LOCALAPPDATA%/Microsoft/WindowsApps`,
    #    which sits on PATH ahead of a per-user install on a default profile,
    #    and a Windows user running Claude Code through Git Bash or WSL
    #    inherits that PATH. `command -v python3` returned the stub, the stub
    #    was refused, `command -v python` returned the SAME directory's stub,
    #    and the real interpreter one entry further down was never looked at --
    #    nine silent no-op hooks on a machine that had a working interpreter
    #    all along, which is the exact failure this library exists to remove.
    #    `lib.ps1` gets it right with `Get-Command -All`; this is that rule in
    #    POSIX sh, so the parity suite can finally compare the two.
    #
    #    Name-major, matching `lib.ps1`'s loop order: a `python3` anywhere on
    #    PATH beats a `python` anywhere. That is the existing behaviour and the
    #    one the shims were measured against.
    #
    #    `.exe` as well as the bare name, for a runtime that does not translate
    #    names the way Git Bash does. Measured under Git Bash: for a directory
    #    holding only `python3.exe`, `[ -f "$dir/python3" ]` is already true
    #    and `command -v python3` answers `$dir/python3`, so there the second
    #    candidate never decides anything. It is for a shell that reads the
    #    name literally and meets a Windows interpreter, which is a `.exe`.
    #
    #    The one asymmetry with `lib.ps1` is deliberate: under Git Bash the
    #    bare name also matches an extensionless `#!` script, which this shell
    #    CAN exec and PowerShell cannot, so `lib.ps1` passes such a file over.
    #    Refusing here a file the shell can run, to match a shell that cannot,
    #    would cost a `.sh` user a working interpreter.
    #
    #    Split on `:` by parameter expansion, not `for _mh_dir in $PATH` under
    #    `IFS=:`. The unquoted expansion GLOBS every element against the
    #    filesystem, so an entry spelled `[ab]` answered from a directory named
    #    `a` that is not on PATH at all; and restoring `IFS` from a saved copy
    #    turned an UNSET `IFS` into an EMPTY one, which switches word splitting
    #    off for a caller that runs this outside `$(...)` (both measured by
    #    review). Expansion does neither and touches nothing of the caller's.
    [ -n "${PATH:-}" ] || return 1
    for _mh_name in python3 python; do
        _mh_rest="${PATH}:"
        while [ -n "$_mh_rest" ]; do
            _mh_dir=${_mh_rest%%:*}
            _mh_rest=${_mh_rest#*:}
            # An empty PATH element means the current directory, per POSIX.
            [ -n "$_mh_dir" ] || _mh_dir=.
            for _mh_candidate in "$_mh_dir/$_mh_name" "$_mh_dir/$_mh_name.exe"; do
                if [ -f "$_mh_candidate" ] && [ -x "$_mh_candidate" ] \
                   && ! mindhive_is_store_stub "$_mh_candidate"; then
                    printf '%s\n' "$_mh_candidate"
                    return 0
                fi
            done
        done
    done

    return 1
}

# The `session_id` out of a Claude hook payload, or `unknown`.
#
# Through the interpreter rather than `sed`, and the reason is item 14 in
# TODOS.md: `jq` is not installed on this machine, and a hand-rolled regex over
# JSON is wrong the first time a value carries an escaped quote. The
# interpreter is already required for the hook to do anything at all, so using
# it costs nothing that was not already being paid.
#
# The value names a ledger segment: `events.append_event` writes to
# `<events>/<session_id>.jsonl`. The Python side guards that name itself --
# `events.segment_name` quarantines an id outside the same whitelist, or one
# that spells a reserved name, to `unsafe-<sha256[:16]>.jsonl` -- but this
# refuses it a step earlier, so a hostile id never reaches a child's
# environment or argv at all. Only a string of filename-safe characters that
# does not open with `-` is accepted, and anything else — a nested object, a
# number, a path with separators in it, an id shaped like an option — becomes
# `unknown`. The payload is
# machine-generated by Claude Code, whose ids are UUIDs, so nothing real is
# ever refused; the whitelist is there for the day something other than Claude
# Code writes to this stdin.
#
# It is enforced TWICE: inside the `-c` program, and again here on whatever the
# interpreter printed. The first alone holds only if the interpreter printed
# nothing else — a `sitecustomize` line or a warning on stdout would otherwise
# become the id.
mindhive_session_id() {
    _mh_py=${1:-}
    _mh_raw=${2:-}
    _mh_sid=""
    if [ -n "$_mh_py" ] && [ -n "$_mh_raw" ]; then
        _mh_sid=$(printf '%s' "$_mh_raw" | "$_mh_py" -c 'import json, re, sys
try:
    got = json.load(sys.stdin).get("session_id")
except Exception:
    got = None
if not isinstance(got, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", got):
    got = "unknown"
print(got)' 2>/dev/null) || _mh_sid=""
    fi
    case "$_mh_sid" in
        ''|-*|*[!A-Za-z0-9._-]*) _mh_sid="unknown" ;;
    esac
    [ "${#_mh_sid}" -le 128 ] || _mh_sid="unknown"
    printf '%s\n' "$_mh_sid"
}

# Read the payload from stdin, or nothing when stdin is a terminal.
#
# The guard is what makes these hooks hang-proof: `[ ! -t 0 ]` is the POSIX
# spelling of `[Console]::IsInputRedirected`, and without it a shim run by hand
# blocks forever on a `read` that no one is going to answer.
mindhive_payload() {
    if [ -t 0 ]; then
        return 0
    fi
    cat 2>/dev/null || true
}

# Start a detached, silent background job. The POSIX half of the
# `Start-Process -WindowStyle Hidden` the two session hooks use for work that
# must never delay the session it is attached to.
mindhive_detach() {
    if command -v nohup >/dev/null 2>&1; then
        nohup "$@" >/dev/null 2>&1 &
    else
        "$@" >/dev/null 2>&1 &
    fi
    return 0
}
