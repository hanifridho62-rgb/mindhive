# pre_tool_use — the ENFORCE rung of MindHive's nudge->enforce ladder. Reads the
# PreToolUse payload on stdin and, when an *enabled* enforcement matcher fires,
# emits a deny decision on stdout so Claude must use the effective method. Every
# other case prints nothing and exits 0 (allow). Fail-open + hang-proof: any
# error allows the tool. stdout IS the hook's decision channel, so this script
# must never write anything to it except the Python decision JSON.
if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }   # headless reflection session: never self-block
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try {
    if (-not [Console]::IsInputRedirected) { exit 0 }   # no payload -> allow
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    $out = ($raw | & $py -m mindhive hook pre_tool_use)
    if ($out) { Write-Output $out }                     # deny decision -> Claude; empty -> allow
} catch {}
exit 0
