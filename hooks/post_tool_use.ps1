# post_tool_use — capture file_touched / command_run. Hang-proof, never blocks.
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try {
    if (-not [Console]::IsInputRedirected) { exit 0 }   # no payload -> nothing to do
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    $j = $raw | ConvertFrom-Json
    if ($j.session_id) { $env:MINDHIVE_SESSION = $j.session_id }
    $tool = $j.tool_name
    if ($tool -in @("Edit", "Write", "NotebookEdit")) {
        $path = $j.tool_input.file_path
        if ($path) { & $py -m mindhive append file_touched $path --tags auto | Out-Null }
    } elseif ($tool -in @("Bash", "PowerShell")) {
        $cmd = $j.tool_input.command
        if ($cmd) {
            $short = $cmd.Substring(0, [Math]::Min(180, $cmd.Length))
            & $py -m mindhive append command_run $short --tags auto | Out-Null
        }
    }
} catch {}
exit 0
