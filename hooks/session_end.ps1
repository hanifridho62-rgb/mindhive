# hooks/session_end.ps1 — log end + rebuild projections.
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try {
    & $py -m mindhive append session_end "session closed" | Out-Null
    & $py -m mindhive project | Out-Null
} catch {}
exit 0
