# session_start — inject STATE.md into the new session; stamp session id.
# Hang-proof (only reads stdin if piped) and never blocks the session.
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
    & $py -m mindhive append session_start "session opened" 2>$null | Out-Null
    & $py -m mindhive directives 2>$null
    & $py -m mindhive state 2>$null
} catch {}
# Skill suggestions (R9) arrive via the SUGGESTED SKILLS block embedded in STATE.md above — no extra call here.
# Auto-refresh the plugin/skill catalog in the background — detached and
# fire-and-forget so it never delays session start. 'autoscan' is stale-gated:
# it only does real work when the catalog is missing or older than 24h, so on a
# warm store this is a near-instant no-op. Manual 'hive plugins scan' / the
# dashboard buttons still force a full rescan anytime.
Start-MindhiveDetached $py @('-m','mindhive','plugins','autoscan')
# Keep the prompt-quality trigger rules fresh the same way: detached and
# stale-gated (24h), so the UserPromptSubmit gate always has a recent manifest
# without ever training inline with a prompt.
Start-MindhiveDetached $py @('-m','mindhive','triggers','train','--max-age-hours','24')
exit 0
