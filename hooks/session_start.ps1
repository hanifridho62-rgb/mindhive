# hooks/session_start.ps1 — print STATE.md, log session start. Never blocks.
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try {
    & $py -m mindhive append session_start "session opened" | Out-Null
    & $py -m mindhive state
} catch {}
exit 0
