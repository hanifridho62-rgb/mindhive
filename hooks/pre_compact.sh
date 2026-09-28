#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# pre_compact — mark compaction + fold reminder. Hang-proof, never blocks.
if [ "${MINDHIVE_HEADLESS:-}" = "1" ]; then exit 0; fi   # headless reflection session: no recursion, no phantom events
MINDHIVE_HOOK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 0
export MINDHIVE_HOOK_DIR
MINDHIVE_LIB=$MINDHIVE_HOOK_DIR/lib.sh
[ -r "$MINDHIVE_LIB" ] || exit 0
( . "$MINDHIVE_LIB" ) >/dev/null 2>&1 || exit 0   # `.` is a special builtin: its failure kills the shell before any `||` runs, so prove the library loads in a subshell first
. "$MINDHIVE_LIB" 2>/dev/null
raw=$(mindhive_payload)
py=$(mindhive_python) || py=""
if [ -n "$py" ]; then
    MINDHIVE_SESSION=$(mindhive_session_id "$py" "$raw")
    export MINDHIVE_SESSION
    "$py" -m mindhive append compaction "pre-compaction snapshot" >/dev/null 2>&1
fi
# Printed even when no interpreter could be found, and that asymmetry is
# deliberate: the reminder is the half a person acts on and it costs nothing to
# emit, while the ledger row is the half that needs a working backend.
echo "MIND HIVE: before compaction, fold this session -> append decision/caveat/next_step + a state_set, then run: hive project"
exit 0
