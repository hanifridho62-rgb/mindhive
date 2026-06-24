# autosync.ps1 — run by Windows Task Scheduler (hook-free memory automation).
# Harvests Claude Code transcripts -> rebuilds projections -> syncs native MEMORY.md
# for every registered project. Never blocks; safe to run on a schedule.
# Uses pythonw.exe (windowless) so no console window flashes on each run.
$ErrorActionPreference = "SilentlyContinue"
& "C:\Users\hanif\mindhive\.venv\Scripts\pythonw.exe" -m mindhive autosync | Out-Null
exit 0
