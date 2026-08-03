# stop — smart-zone session rotation detector.
#
# Fires at the end of every agent turn. It measures this session's true context
# occupancy from the transcript and, when that crosses the smart-zone boundary,
# writes the ROTATION_PENDING artifact that `hive session run` picks up after
# the child exits.
#
# Inert unless MINDHIVE_ROTATION is set — registering this hook and switching it
# on are deliberately separate steps, because a Stop hook is global and a bad
# one would stall every Claude Code session on this machine, not just one.
#
# Never blocks and never fails loudly: stdout is the decision channel, so silence
# is the normal output and any error must also be silence. Exit 0 unconditionally.
if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }   # headless child: no recursion
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
try {
    if ([Console]::IsInputRedirected) {
        $raw = [Console]::In.ReadToEnd()
        if ($raw) { $raw | & $py -m mindhive hook stop }
    }
} catch {}
exit 0
