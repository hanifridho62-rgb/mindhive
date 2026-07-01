# circle.ps1 — run the MindHive autonomous loop ("hive loop") from anywhere.
# This is the reliable, surface-independent equivalent of typing /circle: it puts
# the Claude Code CLI on PATH (auto-detecting the desktop app's bundled claude.exe),
# scaffolds .hive-loop/ on first run, then drives the loop. Any args are passed
# through, e.g.:  .\circle.ps1 --dry-run   /   .\circle.ps1 --live
# A real run also auto-opens the web dashboard so you can watch it live; pass
# --no-dashboard (or set MINDHIVE_LOOP_DASHBOARD=0) to skip it.
$ErrorActionPreference = "Stop"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"

# 1. Find a `claude` CLI for the loop to drive. Prefer one already on PATH;
#    otherwise use the newest claude.exe bundled with the Claude desktop app.
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    $ccRoot = Join-Path $env:APPDATA "Claude\claude-code"
    $bundled = Get-ChildItem $ccRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName "claude.exe") } |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($bundled) {
        $env:PATH = "$($bundled.FullName);$env:PATH"
        Write-Host "circle: using bundled Claude CLI at $($bundled.FullName)"
    } else {
        Write-Warning "circle: no 'claude' CLI found on PATH or in the desktop bundle; the loop can scaffold/dry-run but cannot iterate."
    }
}

# 2. First-run scaffold in the CURRENT directory.
if (-not (Test-Path ".hive-loop")) {
    Write-Host "circle: scaffolding .hive-loop/ in $($PWD.Path)"
    & $py -m mindhive loop init
    Write-Host "circle: edit .hive-loop\PROMPT.md and fix_plan.md, then re-run."
    return
}

# 3. Drive the loop (passing through --dry-run / --live / --force / etc.).
& $py -m mindhive loop @args
