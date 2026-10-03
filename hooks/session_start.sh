#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# session_start — inject the directives and state briefs into the new session; stamp session id.
# Hang-proof (only reads stdin if piped) and never blocks the session.
if [ "${MINDHIVE_HEADLESS:-}" = "1" ]; then exit 0; fi   # headless reflection session: no recursion, no phantom events
MINDHIVE_HOOK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 0
export MINDHIVE_HOOK_DIR
MINDHIVE_LIB=$MINDHIVE_HOOK_DIR/lib.sh
[ -r "$MINDHIVE_LIB" ] || exit 0
( . "$MINDHIVE_LIB" ) >/dev/null 2>&1 || exit 0   # `.` is a special builtin: its failure kills the shell before any `||` runs, so prove the library loads in a subshell first
. "$MINDHIVE_LIB" 2>/dev/null
raw=$(mindhive_payload)
py=$(mindhive_python) || exit 0
[ -n "$py" ] || exit 0
MINDHIVE_SESSION=$(mindhive_session_id "$py" "$raw")
export MINDHIVE_SESSION
"$py" -m mindhive append session_start "session opened" >/dev/null 2>&1
# The BRIEF forms: everything this hook prints has to stay under the
# 6,000-character session-start cap (`mindhive.sessionstart`), or Claude Code
# persists it to a file and the session sees a 2 KB preview. The full texts are
# `hive directives` and `hive state`.
"$py" -m mindhive directives --brief 2>/dev/null
"$py" -m mindhive state --brief 2>/dev/null
# Skill suggestions (R9) arrive via the SUGGESTED SKILLS block of the full state, which `sync` embeds in MEMORY.md — no extra call here.
# Tell the session when the plugin Claude Code loads has drifted from this
# checkout (spec Bug 8). It prints only on drift, exits 0 even when the check
# itself breaks, and syncs only when MINDHIVE_PLUGIN_AUTOSYNC=1. Inline rather
# than detached like the two below: what it prints is session context.
"$py" -m mindhive plugins cache-check --hook 2>/dev/null
# Auto-refresh the plugin/skill catalog in the background — detached and
# fire-and-forget so it never delays session start. 'autoscan' is stale-gated:
# it only does real work when the catalog is missing or older than 24h, so on a
# warm store this is a near-instant no-op. Manual 'hive plugins scan' / the
# dashboard buttons still force a full rescan anytime.
mindhive_detach "$py" -m mindhive plugins autoscan
# Keep the prompt-quality trigger rules fresh the same way: detached and
# stale-gated (24h), so the UserPromptSubmit gate always has a recent manifest
# without ever training inline with a prompt.
mindhive_detach "$py" -m mindhive triggers train --max-age-hours 24
exit 0
