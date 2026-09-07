#!/bin/sh
# pre_tool_use — the ENFORCE rung of MindHive's nudge->enforce ladder. Reads the
# PreToolUse payload on stdin and, when an *enabled* enforcement matcher fires,
# emits a deny decision on stdout so Claude must use the effective method. Every
# other case prints nothing and exits 0 (allow). Fail-open + hang-proof: any
# error allows the tool. stdout IS the hook's decision channel, so this script
# must never write anything to it except the Python decision JSON.
if [ "${MINDHIVE_HEADLESS:-}" = "1" ]; then exit 0; fi   # headless reflection session: never self-block
MINDHIVE_HOOK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 0
export MINDHIVE_HOOK_DIR
MINDHIVE_LIB=$MINDHIVE_HOOK_DIR/lib.sh
[ -r "$MINDHIVE_LIB" ] || exit 0
( . "$MINDHIVE_LIB" ) >/dev/null 2>&1 || exit 0   # `.` is a special builtin: its failure kills the shell before any `||` runs, so prove the library loads in a subshell first
. "$MINDHIVE_LIB" 2>/dev/null
raw=$(mindhive_payload)
[ -n "$raw" ] || exit 0                                  # no payload -> allow
py=$(mindhive_python) || exit 0
[ -n "$py" ] || exit 0
# stderr is discarded, not merged: a traceback on the decision channel would be
# read as a malformed decision, and this hook's whole contract is that a
# failure ALLOWS.
out=$(printf '%s' "$raw" | "$py" -m mindhive hook pre_tool_use 2>/dev/null)   # a decision printed before a non-zero exit is still a decision; the .ps1 forwards it, so this must too
if [ -n "$out" ]; then printf '%s\n' "$out"; fi          # deny decision -> Claude; empty -> allow
exit 0
