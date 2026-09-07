#!/bin/sh
# stop — smart-zone session rotation detector.
#
# Fires at the end of every agent turn. It measures this session's true context
# occupancy from the transcript and, when that crosses the smart-zone boundary,
# writes the ROTATION_PENDING artifact that `hive session run` picks up after
# the child exits.
#
# Inert unless MINDHIVE_ROTATION is set. That gate lives in Python
# (mindhive.rotation.hook), NOT here, and this shim must not grow one of its
# own: registering this hook and switching it on are deliberately separate
# steps, and a second gate written in a second language is a second thing to
# get wrong.
#
# Never blocks and never fails loudly: stdout is the decision channel, so
# silence is the normal output and any error must also be silence. Exit 0
# unconditionally.
#
# "Any error must also be silence" includes the CHILD's stderr. Claude Code
# surfaces hook failures to the user, so a backend that cannot answer — an
# interpreter that moved, or a `mindhive` build predating the `stop` stage —
# would otherwise print a failure line at the end of every turn in every
# session on the machine. The feature not working yet is fine; announcing that
# per-turn is not.
#
# It must NEVER emit a block decision: for a Stop hook, `block` means "do not
# stop — keep working", which would pin a full session open instead of rotating
# it, the exact opposite of the intent. Rotation is signalled by the
# ROTATION_PENDING artifact, because a Stop hook cannot end a session at all.
if [ "${MINDHIVE_HEADLESS:-}" = "1" ]; then exit 0; fi   # headless child: no recursion
MINDHIVE_HOOK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 0
export MINDHIVE_HOOK_DIR
MINDHIVE_LIB=$MINDHIVE_HOOK_DIR/lib.sh
[ -r "$MINDHIVE_LIB" ] || exit 0
( . "$MINDHIVE_LIB" ) >/dev/null 2>&1 || exit 0   # `.` is a special builtin: its failure kills the shell before any `||` runs, so prove the library loads in a subshell first
. "$MINDHIVE_LIB" 2>/dev/null
raw=$(mindhive_payload)
[ -n "$raw" ] || exit 0
py=$(mindhive_python) || exit 0
[ -n "$py" ] || exit 0
printf '%s' "$raw" | "$py" -m mindhive hook stop 2>/dev/null
exit 0
