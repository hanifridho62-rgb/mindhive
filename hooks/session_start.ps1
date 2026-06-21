# session_start — inject STATE.md into the new session; stamp session id.
# Hang-proof (only reads stdin if piped) and never blocks the session.
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
try {
    & $py -m mindhive append session_start "session opened" | Out-Null
    & $py -m mindhive state
} catch {}
exit 0
