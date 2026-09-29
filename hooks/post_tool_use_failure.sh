#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# post_tool_use_failure — structured live failure capture. Appends one
# tool_failed event with a machine-readable {tool, category, retryable} payload
# per failing tool call, so failures are parseable at capture time instead of
# hashing into unreflectable raw:* incidents. Same thin fail-safe pipe shape as
# post_tool_use.sh: parsing lives in Python
# (mindhive.hookcapture.record_post_tool_use_failure), errors land in
# hooks_errors.log, and this script can never block the session.
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
printf '%s' "$raw" | "$py" -m mindhive hook post_tool_use_failure >/dev/null 2>&1
exit 0
