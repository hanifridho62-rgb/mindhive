# pre_compact — mark compaction + fold reminder. Hang-proof, never blocks.
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
$sid = "unknown"
try {
    if ([Console]::IsInputRedirected) {
        $raw = [Console]::In.ReadToEnd()
        if ($raw) { $sid = ($raw | ConvertFrom-Json).session_id }
    }
} catch {}
if (-not $sid) { $sid = "unknown" }
$env:MINDHIVE_SESSION = $sid
try { & $py -m mindhive append compaction "pre-compaction snapshot" | Out-Null } catch {}
Write-Output "MIND HIVE: before compaction, fold this session -> append decision/caveat/next_step + a state_set, then run: hive project"
exit 0
