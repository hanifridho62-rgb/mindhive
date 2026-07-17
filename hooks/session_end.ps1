# session_end — log end + refresh briefing/graph/native sync. Hang-proof, never blocks.
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
    & $py -m mindhive append session_end "session closed" | Out-Null
    # Queue a thermo-nuclear review of the source files touched this session BEFORE
    # rebuilding projections, so the pending-review notice lands in STATE.md (which
    # the next session_start injects). Cheap, LLM-free; the review itself runs in
    # the next interactive session. See docs/adr/0004-*.
    & $py -m mindhive review queue --session $sid | Out-Null
    # Queue the mandatory change-log gate the same way: unlogged source files
    # become a pending marker whose notice rides STATE.md until `hive log
    # <category> "…"` records the entry (see changelog.py).
    & $py -m mindhive log queue --session $sid | Out-Null
    & $py -m mindhive project | Out-Null
    & $py -m mindhive briefing | Out-Null
    & $py -m mindhive graph build | Out-Null
    & $py -m mindhive sync | Out-Null   # refresh native MEMORY.md (auto-load) for this folder
} catch {}
# Auto-reflection (KTD3): turn recurring failures into prevention rules —
# spawned DETACHED (same Start-Process fire-and-forget pattern session_start
# uses for `plugins autoscan`) so this hook never waits on a headless Claude
# call. The command itself is cooldown-gated (6h) and batch-bounded (3), and
# its spawned Claude sessions carry MINDHIVE_HEADLESS=1 so hooks can't re-fire.
try { Start-Process -FilePath $py -ArgumentList '-m','mindhive','rules','reflect','--limit','3','--cooldown-hours','6' -WindowStyle Hidden } catch {}
exit 0
