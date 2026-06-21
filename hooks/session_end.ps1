# hooks/session_end.ps1 — log end + rebuild projections.
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try {
    & $py -m mindhive append session_end "session closed" | Out-Null
    & $py -m mindhive project | Out-Null
    & $py -m mindhive briefing | Out-Null
    & $py -m mindhive graph build | Out-Null
    & $py -m mindhive sync | Out-Null   # refresh native MEMORY.md (auto-load) for this folder
} catch {}
exit 0
