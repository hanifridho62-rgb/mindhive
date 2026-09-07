#!/bin/sh
# post_tool_use — capture file_touched / command_run. Hang-proof, never blocks.
# Parsing lives in Python (mindhive.hookcapture): it is unit-tested and records
# any failure to hooks_errors.log instead of dropping it silently, so a renamed
# Claude payload field cannot stop capture without a trace. This shim is just a
# thin, fail-safe pipe of the raw stdin payload.
#
# Stdout is suppressed, matching the `| Out-Null` in post_tool_use.ps1: this
# stage has no decision channel, and anything printed here would reach Claude
# as if it were one.
if [ "${MINDHIVE_HEADLESS:-}" = "1" ]; then exit 0; fi   # headless reflection session: no recursion, no phantom events
MINDHIVE_HOOK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 0
export MINDHIVE_HOOK_DIR
MINDHIVE_LIB=$MINDHIVE_HOOK_DIR/lib.sh
[ -r "$MINDHIVE_LIB" ] || exit 0
( . "$MINDHIVE_LIB" ) >/dev/null 2>&1 || exit 0   # `.` is a special builtin: its failure kills the shell before any `||` runs, so prove the library loads in a subshell first
. "$MINDHIVE_LIB" 2>/dev/null
raw=$(mindhive_payload)
[ -n "$raw" ] || exit 0                                  # no payload -> nothing to do
py=$(mindhive_python) || exit 0
[ -n "$py" ] || exit 0
printf '%s' "$raw" | "$py" -m mindhive hook post_tool_use >/dev/null 2>&1
exit 0
