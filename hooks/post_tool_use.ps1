# post_tool_use — capture file_touched / command_run. Hang-proof, never blocks.
# Parsing now lives in Python (mindhive.hookcapture): it is unit-tested and
# records any failure to hooks_errors.log instead of dropping it silently, so a
# renamed Claude payload field can't stop capture without a trace. This hook is
# just a thin, fail-safe pipe of the raw stdin payload.
if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }   # headless reflection session: no recursion, no phantom events
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try {
    if (-not [Console]::IsInputRedirected) { exit 0 }   # no payload -> nothing to do
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    $raw | & $py -m mindhive hook post_tool_use | Out-Null
} catch {}
exit 0
