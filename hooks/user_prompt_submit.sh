#!/bin/sh
# user_prompt_submit — the prompt-quality gate. Reads the UserPromptSubmit
# payload on stdin and, when the typed prompt is underspecified AND trained
# trigger rules exist (rules/triggers.json), emits additionalContext JSON on
# stdout so the agent front-loads the proven skill (plan/tests/review) instead
# of improvising on a vague instruction. Every other case prints nothing and
# exits 0. Additive-only (never blocks a prompt), fail-open + hang-proof: any
# error means no injection. stdout IS the hook's context channel, so this
# script must never write anything to it except the Python context JSON.
if [ "${MINDHIVE_HEADLESS:-}" = "1" ]; then exit 0; fi   # headless reflection session: never self-trigger
MINDHIVE_HOOK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 0
export MINDHIVE_HOOK_DIR
MINDHIVE_LIB=$MINDHIVE_HOOK_DIR/lib.sh
[ -r "$MINDHIVE_LIB" ] || exit 0
( . "$MINDHIVE_LIB" ) >/dev/null 2>&1 || exit 0   # `.` is a special builtin: its failure kills the shell before any `||` runs, so prove the library loads in a subshell first
. "$MINDHIVE_LIB" 2>/dev/null
raw=$(mindhive_payload)
[ -n "$raw" ] || exit 0                                  # no payload -> silent
py=$(mindhive_python) || exit 0
[ -n "$py" ] || exit 0
out=$(printf '%s' "$raw" | "$py" -m mindhive hook user_prompt_submit 2>/dev/null)   # context printed before a non-zero exit is still context; the .ps1 forwards it, so this must too
if [ -n "$out" ]; then printf '%s\n' "$out"; fi          # context -> Claude; empty -> silent
exit 0
