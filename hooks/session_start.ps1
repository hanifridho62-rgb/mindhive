# session_start — inject STATE.md into the new session; stamp session id.
# Hang-proof (only reads stdin if piped) and never blocks the session.
if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }   # headless reflection session: no recursion, no phantom events
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
    & $py -m mindhive directives
    & $py -m mindhive state
} catch {}
# Skill suggestions (R9) arrive via the SUGGESTED SKILLS block embedded in STATE.md above — no extra call here.
# Auto-refresh the plugin/skill catalog in the background — detached and
# fire-and-forget so it never delays session start. 'autoscan' is stale-gated:
# it only does real work when the catalog is missing or older than 24h, so on a
# warm store this is a near-instant no-op. Manual 'hive plugins scan' / the
# dashboard buttons still force a full rescan anytime.
try { Start-Process -FilePath $py -ArgumentList '-m','mindhive','plugins','autoscan' -WindowStyle Hidden } catch {}
exit 0
