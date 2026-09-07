#!/bin/sh
# autosync — the POSIX half of autosync.ps1, for a cron entry rather than a
# Windows Task Scheduler action. Harvests Claude Code transcripts, rebuilds the
# projections, and syncs the native MEMORY.md for every registered project.
# Never blocks; safe to run on a schedule.
#
# The .ps1 uses pythonw.exe so no console window flashes on each run. There is
# no windowless interpreter to pick on POSIX and no window to suppress, so this
# just redirects.
if [ "${MINDHIVE_HEADLESS:-}" = "1" ]; then exit 0; fi   # headless child: no recursion
MINDHIVE_HOOK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 0
export MINDHIVE_HOOK_DIR
MINDHIVE_LIB=$MINDHIVE_HOOK_DIR/lib.sh
[ -r "$MINDHIVE_LIB" ] || exit 0
( . "$MINDHIVE_LIB" ) >/dev/null 2>&1 || exit 0   # `.` is a special builtin: its failure kills the shell before any `||` runs, so prove the library loads in a subshell first
. "$MINDHIVE_LIB" 2>/dev/null
py=$(mindhive_python) || exit 0
[ -n "$py" ] || exit 0
"$py" -m mindhive autosync >/dev/null 2>&1
exit 0
