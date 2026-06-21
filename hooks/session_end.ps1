# session_end — log end + refresh briefing/graph/native sync. Hang-proof, never blocks.
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
    & $py -m mindhive append session_end "session closed" | Out-Null
    & $py -m mindhive project | Out-Null
    & $py -m mindhive briefing | Out-Null
    & $py -m mindhive graph build | Out-Null
    & $py -m mindhive sync | Out-Null   # refresh native MEMORY.md (auto-load) for this folder
} catch {}
exit 0
