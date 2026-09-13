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
# Resolve the interpreter through the shared library instead of hard-coding one
# operator's venv. Every step is fail-open and the failure is SILENCE: a hook
# registered in ~/.claude/settings.json fires in every session on the machine,
# so a library that is missing, unparseable, or finds no interpreter must leave
# the session exactly as it found it. `lib.ps1` explains the resolution order.
# The child's stderr is discarded for the same reason. The old shim named ONE
# venv and could assume `mindhive` was installed in it; tier 3 can resolve a
# bare `python` off PATH that has no `mindhive` at all, and a hook that prints
# `No module named mindhive` on every fire is not the silent no-op this
# contract promises. Safe here specifically because success is never judged by
# `$?` -- these shims exit 0 unconditionally.
$lib = Join-Path $PSScriptRoot "lib.ps1"
if (-not (Test-Path -LiteralPath $lib -PathType Leaf)) { exit 0 }
try { . $lib } catch { exit 0 }
if (-not (Get-Command Get-MindhivePython -ErrorAction SilentlyContinue)) { exit 0 }
$py = Get-MindhivePython
if (-not $py) { exit 0 }
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
