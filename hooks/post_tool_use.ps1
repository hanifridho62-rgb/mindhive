# hooks/post_tool_use.ps1 — record file_touched / command_run deterministically.
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try {
    $raw = [Console]::In.ReadToEnd()
    $j = $raw | ConvertFrom-Json
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
