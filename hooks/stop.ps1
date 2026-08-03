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
#
# "Any error must also be silence" includes the CHILD's stderr. Claude Code
# surfaces hook failures to the user, so a backend that cannot answer — an
# interpreter that moved, or a `mindhive` build predating the `stop` stage,
# which is every build until this branch merges — would otherwise print a
# failure line at the end of every turn in every session on the machine. The
# feature not working yet is fine; announcing that per-turn is not.
if ($env:MINDHIVE_HEADLESS -eq "1") { exit 0 }   # headless child: no recursion
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { exit 0 }
try {
    if ([Console]::IsInputRedirected) {
        $raw = [Console]::In.ReadToEnd()
        # 2>$null is safe here specifically because success is never judged by
        # $? — this script exits 0 unconditionally, so the NativeCommandError
        # that a stderr redirect induces in PowerShell 5.1 has nothing to break.
        if ($raw) { $raw | & $py -m mindhive hook stop 2>$null }
    }
} catch {}
exit 0
