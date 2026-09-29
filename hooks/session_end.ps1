# SPDX-License-Identifier: Apache-2.0
# session_end — log end + refresh briefing/graph/native sync. Hang-proof, never blocks.
if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }   # headless reflection session: no recursion, no phantom events
$ErrorActionPreference = "SilentlyContinue"
# Resolve the interpreter through the shared library instead of hard-coding one
# operator's venv. Every step is fail-open and the failure is SILENCE: a hook
# registered in ~/.claude/settings.json fires in every session on the machine,
# so a library that is missing, unparseable, or finds no interpreter must leave
# the session exactly as it found it. `lib.ps1` explains the resolution order.
# The child's stderr is discarded for the same reason. The old shim named ONE
# venv and could assume `mindhive` was installed in it; tier 3 can resolve a
# bare `python` off PATH that has no `mindhive` at all, and a hook that prints
# `No module named mindhive` on every fire is not the silent no-op this
# contract promises. Safe here specifically because success is never judged by
# `$?` -- these shims exit 0 unconditionally.
$lib = Join-Path $PSScriptRoot "lib.ps1"
if (-not (Test-Path -LiteralPath $lib -PathType Leaf)) { exit 0 }
try { . $lib } catch { exit 0 }
if (-not (Get-Command Get-MindhivePython -ErrorAction SilentlyContinue)) { exit 0 }
$py = Get-MindhivePython
if (-not $py) { exit 0 }
# The id names a ledger segment, so it is whitelisted before it is exported:
# `Get-MindhiveSessionId` answers `unknown` for anything that is not a plain
# filename-safe token. The POSIX family has enforced this since it was written;
# this side took `.session_id` raw until now.
$sid = Get-MindhiveSessionId (Get-MindhivePayload)
$env:MINDHIVE_SESSION = $sid
try {
    & $py -m mindhive append session_end "session closed" 2>$null | Out-Null
    # Adaptive work injection (replenish.py) runs BEFORE the autoclose below,
    # and therefore before the projections rebuild. The ordering is load-bearing
    # and silent when wrong: injected steps added AFTER the plan is closed still
    # exist, but the plan is already interrupted, so notice() never renders them
    # and the next session never sees the work.
    & $py -m mindhive replenish run --session $sid 2>$null | Out-Null
    # Close the session's auto-synced roadmap (roadmap.py autoclose) BEFORE the
    # projections rebuild: all steps done -> complete (leaves every view);
    # unfinished -> interrupt, so the plan's creation context rides STATE.md
    # into the next session.
    & $py -m mindhive roadmap autoclose --session $sid 2>$null | Out-Null
    # Queue a thermo-nuclear review of the source files touched this session BEFORE
    # rebuilding projections, so the pending-review notice lands in STATE.md (which
    # the next session_start injects). Cheap, LLM-free; the review itself runs in
    # the next interactive session. See docs/adr/0004-*.
    & $py -m mindhive review queue --session $sid 2>$null | Out-Null
    # Queue the mandatory change-log gate the same way: unlogged source files
    # become a pending marker whose notice rides STATE.md until `hive log
    # <category> "…"` records the entry (see changelog.py).
    & $py -m mindhive log queue --session $sid 2>$null | Out-Null
    & $py -m mindhive project 2>$null | Out-Null
    & $py -m mindhive briefing 2>$null | Out-Null
    & $py -m mindhive graph build 2>$null | Out-Null
    & $py -m mindhive sync 2>$null | Out-Null   # refresh native MEMORY.md (auto-load) for this folder
} catch {}
# Auto-reflection (KTD3): turn recurring failures into prevention rules —
# spawned DETACHED (same Start-Process fire-and-forget pattern session_start
# uses for `plugins autoscan`) so this hook never waits on a headless Claude
# call. The command itself is cooldown-gated (6h) and batch-bounded (3), and
# its spawned Claude sessions carry MINDHIVE_HEADLESS=1 so hooks can't re-fire.
Start-MindhiveDetached $py @('-m','mindhive','rules','reflect','--limit','3','--cooldown-hours','6')
exit 0
