# SPDX-License-Identifier: Apache-2.0
# pre_tool_use — the ENFORCE rung of MindHive's nudge->enforce ladder. Reads the
# PreToolUse payload on stdin and, when an *enabled* enforcement matcher fires,
# emits a deny decision on stdout so Claude must use the effective method. Every
# other case prints nothing and exits 0 (allow). Fail-open + hang-proof: any
# error allows the tool. stdout IS the hook's decision channel, so this script
# must never write anything to it except the Python decision JSON.
if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }   # headless reflection session: never self-block
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
    if (-not [Console]::IsInputRedirected) { exit 0 }   # no payload -> allow
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    $out = ($raw | & $py -m mindhive hook pre_tool_use 2>$null)
    if ($out) { Write-Output $out }                     # deny decision -> Claude; empty -> allow
} catch {}
exit 0
