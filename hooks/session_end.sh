#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# session_end — log end + refresh briefing/graph/native sync. Hang-proof, never blocks.
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
"$py" -m mindhive append session_end "session closed" >/dev/null 2>&1
# Adaptive work injection (replenish.py) runs BEFORE the autoclose below, and
# therefore before the projections rebuild. The ordering is load-bearing and
# silent when wrong: injected steps added AFTER the plan is closed still exist,
# but the plan is already interrupted, so notice() never renders them and the
# next session never sees the work.
"$py" -m mindhive replenish run --session "$MINDHIVE_SESSION" >/dev/null 2>&1
# Close the session's auto-synced roadmap (roadmap.py autoclose) BEFORE the
# projections rebuild: all steps done -> complete (leaves every view);
# unfinished -> interrupt, so the plan's creation context rides STATE.md into
# the next session.
"$py" -m mindhive roadmap autoclose --session "$MINDHIVE_SESSION" >/dev/null 2>&1
# Queue a thermo-nuclear review of the source files touched this session BEFORE
# rebuilding projections, so the pending-review notice lands in STATE.md (which
# the next session_start injects). Cheap, LLM-free; the review itself runs in
# the next interactive session. See docs/adr/0004-*.
"$py" -m mindhive review queue --session "$MINDHIVE_SESSION" >/dev/null 2>&1
# Queue the mandatory change-log gate the same way: unlogged source files become
# a pending marker whose notice rides STATE.md until `hive log <category> "…"`
# records the entry (see changelog.py).
"$py" -m mindhive log queue --session "$MINDHIVE_SESSION" >/dev/null 2>&1
"$py" -m mindhive project >/dev/null 2>&1
"$py" -m mindhive briefing >/dev/null 2>&1
"$py" -m mindhive graph build >/dev/null 2>&1
"$py" -m mindhive sync >/dev/null 2>&1   # refresh native MEMORY.md (auto-load) for this folder
# Auto-reflection (KTD3): turn recurring failures into prevention rules —
# spawned DETACHED (the same fire-and-forget pattern session_start uses for
# `plugins autoscan`) so this hook never waits on a headless Claude call. The
# command itself is cooldown-gated (6h) and batch-bounded (3), and its spawned
# Claude sessions carry MINDHIVE_HEADLESS=1 so hooks cannot re-fire.
mindhive_detach "$py" -m mindhive rules reflect --limit 3 --cooldown-hours 6
exit 0
