# hooks/lib.sh — what every POSIX hook shim needs, and nothing else.
#
# Sourced, never executed. `tests/test_hook_shims.py` asserts that every
# `hooks/*.ps1` has a `.sh` sibling and the reverse, so this file is the one
# exception on the POSIX side and the test names it explicitly.
#
# ## Why the interpreter is RESOLVED here and hard-coded there
#
# Every `.ps1` shim opens with
#
#     $py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
#
# which is correct on exactly one machine, for one username, for one checkout.
# A POSIX family written the same way would be a family that only ever ran
# here, which is the opposite of the point. So this resolves, in a fixed order,
# and answers nothing when it cannot — the caller then exits 0 and the hook is
# a silent no-op, which is the only acceptable failure for a hook.
#
# ## The order, and why the venv beats PATH
#
#   1. $MINDHIVE_PYTHON, when it is an executable file. The operator's
#      override — the `.ps1` family has no equivalent, and it is honoured
#      because the environment a hook runs in is the operator's own.
#   2. The nearest `.venv` above this file. `mindhive` is installed EDITABLE in
#      it, so it is the only interpreter that can import the checkout the hook
#      belongs to. A PATH interpreter would import whatever it happens to find,
#      or nothing.
#   3. `python3`, then `python`, from PATH — as a last resort.
#
# The walk up from this file (rather than `git rev-parse --git-common-dir`)
# keeps the resolution to pure shell. `post_tool_use` fires on EVERY tool call,
# and a process spawn there is a tax on every tool call in every session; the
# measured cost of a spawn on this machine is ~64 ms, and a `git` call to find
# a directory the filesystem already knows about is not worth paying for it.
# The walk also handles a git worktree for free: a checkout under
# `<repo>/.claude/worktrees/<name>` walks up to `<repo>/.venv`.
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
# `[ "$n" -lt abc ]` would print "integer expression expected" on the hook's
# stderr and skip the walk, and a hook's stderr is what Claude Code shows the
# user when it decides the hook failed.
case "${MINDHIVE_HOOK_WALK_LIMIT:-}" in
    ''|*[!0-9]*) MINDHIVE_HOOK_WALK_LIMIT=8 ;;
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
        for _mh_candidate in "$_mh_dir/.venv/bin/python3" \
                             "$_mh_dir/.venv/bin/python" \
                             "$_mh_dir/.venv/Scripts/python.exe"; do
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

    # 3. PATH, with the Store redirector refused
    for _mh_name in python3 python; do
        _mh_candidate=$(command -v "$_mh_name" 2>/dev/null) || _mh_candidate=""
        if [ -n "$_mh_candidate" ] && [ -f "$_mh_candidate" ] \
           && ! mindhive_is_store_stub "$_mh_candidate"; then
            printf '%s\n' "$_mh_candidate"
            return 0
        fi
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
# `<events>/<session_id>.jsonl` with no further checks. So only a string of
# filename-safe characters that does not open with `-` is accepted, and
# anything else — a nested object, a number, a path with separators in it, an
# id shaped like an option — becomes `unknown`. The payload is
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
