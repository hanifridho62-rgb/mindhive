# post_tool_use_failure — structured live failure capture. Appends one
# tool_failed event with a machine-readable {tool, category, retryable}
# payload per failing tool call, so failures are parseable at capture time
# instead of hashing into unreflectable raw:* incidents. Same thin fail-safe
# pipe shape as post_tool_use.ps1: parsing lives in Python
# (mindhive.hookcapture.record_post_tool_use_failure), errors land in
# hooks_errors.log, and this script can never block the session.
if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }   # headless reflection session: no recursion, no phantom events
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try {
    if (-not [Console]::IsInputRedirected) { exit 0 }   # no payload -> nothing to do
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    $raw | & $py -m mindhive hook post_tool_use_failure | Out-Null
} catch {}
exit 0
