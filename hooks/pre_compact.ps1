# hooks/pre_compact.ps1 — mark compaction + remind Claude to run the fold.
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try { & $py -m mindhive append compaction "pre-compaction snapshot" | Out-Null } catch {}
Write-Output "MIND HIVE: before compaction, fold this session -> append decision/caveat/next_step + a state_set, then run: hive project"
exit 0
