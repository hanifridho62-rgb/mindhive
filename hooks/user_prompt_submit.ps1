# SPDX-License-Identifier: Apache-2.0
# user_prompt_submit — the prompt-quality gate. Reads the UserPromptSubmit
# payload on stdin and, when the typed prompt is underspecified AND trained
# trigger rules exist (rules/triggers.json), emits additionalContext JSON on
# stdout so the agent front-loads the proven skill (plan/tests/review) instead
# of improvising on a vague instruction. Every other case prints nothing and
# exits 0. Additive-only (never blocks a prompt), fail-open + hang-proof: any
# error means no injection. stdout IS the hook's context channel, so this
# script must never write anything to it except the Python context JSON.
if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }   # headless reflection session: never self-trigger
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
    if (-not [Console]::IsInputRedirected) { exit 0 }   # no payload -> silent
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    $out = ($raw | & $py -m mindhive hook user_prompt_submit 2>$null)
    if ($out) { Write-Output $out }                     # context -> Claude; empty -> silent
} catch {}
exit 0
