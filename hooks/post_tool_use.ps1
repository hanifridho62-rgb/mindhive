# SPDX-License-Identifier: Apache-2.0
# post_tool_use — capture file_touched / command_run. Hang-proof, never blocks.
# Parsing now lives in Python (mindhive.hookcapture): it is unit-tested and
# records any failure to hooks_errors.log instead of dropping it silently, so a
# renamed Claude payload field can't stop capture without a trace. This hook is
# just a thin, fail-safe pipe of the raw stdin payload.
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
try {
    if (-not [Console]::IsInputRedirected) { exit 0 }   # no payload -> nothing to do
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    $raw | & $py -m mindhive hook post_tool_use 2>$null | Out-Null
} catch {}
exit 0
