# circle.ps1 — run the MindHive autonomous loop ("hive loop") from anywhere.
# This is the reliable, surface-independent equivalent of typing /circle: it puts
# the Claude Code CLI on PATH (auto-detecting the desktop app's bundled claude.exe),
# scaffolds .hive-loop/ on first run, then drives the loop. Any args are passed
# through, e.g.:  .\circle.ps1 --dry-run   /   .\circle.ps1 --live
# A real run also auto-opens the web dashboard so you can watch it live; pass
# --no-dashboard (or set MINDHIVE_LOOP_DASHBOARD=0) to skip it.
$ErrorActionPreference = "Stop"

# The interpreter is RESOLVED, not hard-coded — see hooks/lib.ps1 for the order.
# Unlike a hook, this is a command someone typed, so a missing interpreter is
# REPORTED rather than swallowed: silence is the only safe failure for something
# that fires in every session on the machine, and the wrong one for a foreground
# tool whose user is waiting to be told why nothing happened.
#
# Every string below is ASCII, deliberately. These files carry no BOM, so
# PowerShell 5.1 decodes them as CP1252: an em dash (E2 80 94) becomes the
# three characters a-circumflex, euro, RIGHT DOUBLE QUOTATION MARK -- and
# PowerShell accepts that last one as a string TERMINATOR. In a comment that
# is cosmetic mojibake; inside a quoted string it ends the string early and
# the whole file stops parsing. tests/test_hook_shims_ps.py parses every
# .ps1 with the real PowerShell parser so this cannot come back silently.
#
# `[Console]::Error.WriteLine`, not `Write-Error`. Under this file's `Stop`
# preference `Write-Error` is itself a terminating error, so the `exit 1` after
# it never ran: with `-File` the process still ended 1, but only as an unhandled
# error wrapped in a record with an `At line:` trailer, and run as `.\circle.ps1`
# from an open session `$LASTEXITCODE` was never set at all (measured by
# review). One line on stderr and an explicit exit is what a caller can read.
$lib = Join-Path $PSScriptRoot "hooks\lib.ps1"
if (-not (Test-Path -LiteralPath $lib -PathType Leaf)) {
    [Console]::Error.WriteLine("circle: hooks\lib.ps1 is missing from $PSScriptRoot - cannot resolve a Python interpreter.")
    exit 1
}
. $lib
$py = Get-MindhivePython
if (-not $py) {
    [Console]::Error.WriteLine("circle: no usable Python found. Looked at MINDHIVE_PYTHON, the nearest .venv above " +
                               "$PSScriptRoot, then PATH. Create the venv (python -m venv .venv; .venv\Scripts\pip install -e .) " +
                               "or set MINDHIVE_PYTHON to an interpreter with mindhive installed.")
    exit 1
}

# 1. Find a `claude` CLI for the loop to drive. Prefer one already on PATH;
#    otherwise use the newest claude.exe bundled with the Claude desktop app.
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    $ccRoot = Join-Path $env:APPDATA "Claude\claude-code"
    $bundled = Get-ChildItem $ccRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "claude.exe") } |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($bundled) {
        $env:PATH = "$($bundled.FullName);$env:PATH"
        Write-Host "circle: using bundled Claude CLI at $($bundled.FullName)"
    } else {
        Write-Warning "circle: no 'claude' CLI found on PATH or in the desktop bundle; the loop can scaffold/dry-run but cannot iterate."
    }
}

# 2. First-run scaffold in the CURRENT directory.
if (-not (Test-Path -LiteralPath ".hive-loop")) {
    Write-Host "circle: scaffolding .hive-loop/ in $($PWD.Path)"
    & $py -m mindhive loop init
    Write-Host "circle: edit .hive-loop\PROMPT.md and fix_plan.md, then re-run."
    return
}

# 3. Drive the loop (passing through --dry-run / --live / --force / etc.).
& $py -m mindhive loop @args
