# SPDX-License-Identifier: Apache-2.0
# pre_compact — mark compaction + fold reminder. Hang-proof, never blocks.
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
#
# The reminder at the bottom of this file is the exception to the fail-open
# SILENCE the other shims keep: it is a message to a human and it needs no
# interpreter, so the resolution gate must not be allowed to swallow it. Only
# the LEDGER half is conditional -- when nothing resolves, the compaction event
# is lost (fail-open, as designed) and the instruction still prints.
$py = $null
$lib = Join-Path $PSScriptRoot "lib.ps1"
if (Test-Path -LiteralPath $lib -PathType Leaf) {
    try { . $lib } catch {}
    if (Get-Command Get-MindhivePython -ErrorAction SilentlyContinue) {
        $py = Get-MindhivePython
    }
}
# The id names a ledger segment, so it is whitelisted before it is exported:
# `Get-MindhiveSessionId` answers `unknown` for anything that is not a plain
# filename-safe token. The POSIX family has enforced this since it was written;
# this side took `.session_id` raw until now.
if ($py) {
    $sid = Get-MindhiveSessionId (Get-MindhivePayload)
    $env:MINDHIVE_SESSION = $sid
    try { & $py -m mindhive append compaction "pre-compaction snapshot" 2>$null | Out-Null } catch {}
}
Write-Output "MIND HIVE: before compaction, fold this session -> append decision/caveat/next_step + a state_set, then run: hive project"
exit 0
