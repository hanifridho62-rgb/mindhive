---
name: windows-powershell-patterns
description: >-
  Windows PowerShell 5.1 automation patterns for this machine: dialect rules
  that parse on 5.1, native exit-code discipline, encoding traps, elevation
  detection, execution-policy handling, and standard-module workflows for
  ports, tasks, and files. Use when writing or reviewing any PowerShell.
origin: mindhive
pack: windows-powershell
role: patterns
kind: patterns
---

# Windows PowerShell 5.1 Patterns

Automation that survives Windows PowerShell 5.1 and local administrative
reality. Everything below was verified on this machine — the same console
that operates the MindHive toolchain (interpreter resolution, the dashboard
port, the auto-resume scheduled task).

## When to Activate

- Writing or editing any `.ps1` script or inline PowerShell command
- Automating anything that may need administrator rights (services, tasks,
  ports below 1024, machine-scope settings)
- A PowerShell snippet failed with a parser error, a false "failure", or a
  mangled file encoding
- Choosing between PowerShell and Git Bash for a command on this machine
- Scripting around the Python launcher, scheduled tasks, or TCP ports

## Interpreter resolution: `py`, never bare `python`

On this machine bare `python` resolves to the Microsoft Store stub under
`WindowsApps` — it exits 9009 with "Python was not found" while *looking*
like an interpreter. Resolve deliberately, with standard cmdlets only:

```powershell
function Resolve-MindHivePython {
    # Prefer the py launcher; verify what a candidate actually is before use.
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $py) { return $py.Source }
    $candidate = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $candidate -and
        $candidate.Source -notlike '*\WindowsApps\*') {
        return $candidate.Source
    }
    throw 'No real Python found: py absent and python is the Store stub.'
}
```

`Get-Command` is the truth source (`(Get-Command x).Source`), and the
`WindowsApps` path test is what separates a real interpreter from the stub.
The verification file's environment phase calls exactly this function.

## Dialect: what 5.1 refuses to parse

Pipeline chain operators and modern null operators are PowerShell 7+. On 5.1
they are parser errors that kill the whole statement:

```powershell
# BROKEN on 5.1 — parser error, nothing runs:
#   git fetch && git rebase
#   $name = $user ?? 'anonymous'
#   $len  = $text?.Length

# 5.1 equivalents:
git fetch
if ($?) { git rebase }                       # success-gated sequencing

$name = if ($null -eq $user) { 'anonymous' } else { $user }
$len  = if ($null -eq $text) { 0 } else { $text.Length }
```

Also 5.1-specific: `$null` goes on the LEFT of comparisons (`$null -eq $x` —
the right-side form silently does collection filtering), and
`ConvertFrom-Json` returns `PSCustomObject` with no `-AsHashtable` option.

## Native commands: judge by `$LASTEXITCODE`, never `$?`

For native executables (`git`, `py`, `gh`) the automatic variable `$?` lies
under common conditions; the exit code does not:

```powershell
py -m pytest -q
if ($LASTEXITCODE -ne 0) {
    throw "suite failed with exit code $LASTEXITCODE"
}
```

The `$?` trap has a precise mechanism, verified empirically here: redirecting
a native command's stderr (`2>&1`, `2>$null`, or `2>file`) makes 5.1 wrap
every stderr line in a NativeCommandError record and flip `$?` to false even
when the command exited 0. git and gh write *progress* to stderr on success,
so the false-failure fires in completely normal use:

```powershell
# BROKEN pattern — reports failure on a successful clone:
git clone $repo 2>&1
if (-not $?) { throw 'clone failed' }        # $? is false; exit code was 0

# CORRECT — never redirect native stderr; gate on the exit code:
git clone $repo
if ($LASTEXITCODE -ne 0) { throw "clone failed ($LASTEXITCODE)" }
```

Reserve `2>$null` for *cmdlets*, where it means what it says. The
verification file's dialect audit greps changed scripts for exactly these
two anti-patterns.

## Encoding: three different defaults, one safe path

Windows PowerShell 5.1 writes three different encodings depending on which
cmdlet you used — and downstream parsers (git, JSON/YAML readers, Python)
care:

```powershell
# Out-File / `>`      -> UTF-16LE with BOM   (breaks most parsers outright)
# Set-Content         -> ANSI codepage       (mangles any non-ASCII)
# Out-File -Encoding utf8 -> UTF-8 WITH BOM  (best 5.1 can do natively)

# Safe default when another tool will read the file:
$text | Out-File -FilePath $path -Encoding utf8

# BOM-free UTF-8 on 5.1 requires .NET directly (standard library, no module):
[System.IO.File]::WriteAllText($path, $text,
    (New-Object System.Text.UTF8Encoding $false))
```

The verification file ships a `Test-Utf8Bom` byte-probe for auditing what a
script actually produced — trust bytes, not intentions.

## File creation: guard, never force

`New-Item -Force` against an EXISTING file silently truncates it to zero
bytes — verified here with real data loss (19 bytes → 0). The guard is
`Test-Path`, always:

```powershell
if (-not (Test-Path -LiteralPath $path)) {
    New-Item -ItemType File -Path $path | Out-Null
}

# Same discipline for directories (-Force is safe ONLY for -ItemType
# Directory, but the guarded form documents intent and costs nothing):
if (-not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Path $dir | Out-Null
}
```

`-LiteralPath` (not `-Path`) whenever the value could contain `[` or `]` —
wildcard expansion on user paths is a classic silent mismatch. Destructive
cmdlets get explicit intent flags in automation: `Remove-Item -Recurse
-Force -Confirm:$false` says *exactly* what it will do; an unadorned
`Remove-Item` in a non-interactive script can hang on a confirmation prompt
that nothing will ever answer.

## Elevation: detect, decide, and only then act

Administrative environments are a first-class case, not an afterthought.
Detection is two lines of standard .NET, no modules:

```powershell
function Test-IsElevated {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
```

The decision pattern: check first, fail with a clear message or explicitly
re-launch — never let an access-denied surface halfway through a change:

```powershell
if (-not (Test-IsElevated)) {
    # Option A: refuse loudly, telling the operator exactly what to do.
    throw 'This step registers a scheduled task and needs an elevated console.'

    # Option B (deliberate, visible re-launch — the ONLY sanctioned way):
    # Start-Process powershell -Verb RunAs -ArgumentList
    #     '-NoProfile','-ExecutionPolicy','Bypass','-File',$scriptPath
}
```

Rule: elevation is requested for the *step that needs it*, never assumed for
the whole script, and anything that mutates machine scope (services,
HKLM:, machine PATH, scheduled tasks running as SYSTEM) sits behind the
check. The verification loop's first phase records the elevation state of
the console it ran in, so a "worked here, failed there" report always shows
whether rights were the difference.

## Execution policy: session scope, not machine scope

Scripts meet `Restricted` or `AllSigned` policies on locked-down machines.
Read the whole stack, then use the narrowest override that works:

```powershell
Get-ExecutionPolicy -List          # every scope: MachinePolicy ... Process

# Narrowest: this process only, no persistent change, no admin needed:
powershell -NoProfile -ExecutionPolicy Bypass -File .\task.ps1

# Persistent for the current user (still not machine-wide):
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
```

Never set `-Scope LocalMachine` from automation — that is an operator
decision, needs elevation, and outlives the script that did it. If
MachinePolicy/UserPolicy (Group Policy scopes) are set, no override works;
report it instead of fighting it.

## Standard-module workflows on this machine

Everything below ships with Windows — no gallery installs, which matters on
admin boxes where `Install-Module` is often blocked.

```powershell
# Is the MindHive dashboard actually listening on its stable port?
Get-NetTCPConnection -LocalPort 54461 -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, OwningProcess

# Which process owns it (join to the PID):
$conn = Get-NetTCPConnection -LocalPort 54461 -State Listen -ErrorAction SilentlyContinue
if ($null -ne $conn) { Get-Process -Id $conn.OwningProcess }
```

```powershell
# Scheduled tasks (the auto-resume timer lives here). Reading is unprivileged;
# Register/Unregister for another user or SYSTEM needs the elevation check.
Get-ScheduledTask -TaskName 'MindHive*' -ErrorAction SilentlyContinue |
    Select-Object TaskName, State

if (Test-IsElevated) {
    $action  = New-ScheduledTaskAction -Execute 'py' -Argument '-m mindhive sync'
    $trigger = New-ScheduledTaskTrigger -Daily -At 09:00
    Register-ScheduledTask -TaskName 'MindHiveSync' -Action $action `
        -Trigger $trigger -Force
}
```

```powershell
# Cmdlet errors: promote to terminating and handle; a bare
# -ErrorAction SilentlyContinue still reports exit 1 to a calling harness.
try {
    Stop-Process -Id $conn.OwningProcess -ErrorAction Stop -Confirm:$false
} catch {
    Write-Warning "could not stop owner process: $($_.Exception.Message)"
}
```

## The robust script skeleton

Every operational `.ps1` in this environment opens the same way — strictness
on, errors terminating, cleanup guaranteed, exit code honest:

```powershell
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TargetPath,
    [switch]$WhatIfMode
)

Set-StrictMode -Version 2.0          # 5.1-safe; unset variables now throw
$ErrorActionPreference = 'Stop'      # cmdlet errors terminate by default

try {
    if (-not (Test-Path -LiteralPath $TargetPath)) {
        throw "target does not exist: $TargetPath"
    }
    # ... the work, native calls still gated on $LASTEXITCODE ...
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
} finally {
    # cleanup that must happen on BOTH paths (temp files, transcripts)
}
```

Why each line earns its place: `Set-StrictMode` turns the classic silent
`$tagretPath` typo into an immediate throw; `$ErrorActionPreference='Stop'`
means a failed cmdlet cannot be sailed past (native commands are unaffected
— their gate stays `$LASTEXITCODE`); the explicit `exit 0` / `exit 1` is
what schedulers and the verification loop's execution phase read;
`[CmdletBinding()]` gives `-Verbose`/`-ErrorAction` plumbing for free. A
`$WhatIfMode` switch that prints intended actions without performing them
is the cheap rehearsal mode for anything destructive.

## Registry and environment-variable scope

Both come in user scope (yours, unprivileged) and machine scope (everyone's,
elevation required) — and automation must name which it means:

```powershell
# Registry: PSDrive prefixes, never raw HKEY_ strings.
Get-ItemProperty -Path 'HKCU:\Software\MindHive' -ErrorAction SilentlyContinue

# HKCU writes: unprivileged, still guarded:
if (-not (Test-Path 'HKCU:\Software\MindHive')) {
    New-Item -Path 'HKCU:\Software\MindHive' | Out-Null
}
Set-ItemProperty -Path 'HKCU:\Software\MindHive' -Name 'Port' -Value 54461

# HKLM writes: machine scope — behind the elevation check, always:
if (Test-IsElevated) {
    Set-ItemProperty -Path 'HKLM:\SOFTWARE\MindHive' -Name 'Port' -Value 54461
} else {
    throw 'HKLM write requires an elevated console.'
}
```

```powershell
# Environment variables: three scopes, three different tools.
$env:MINDHIVE_PROJECT = 'hanif'          # process scope — this session only

[Environment]::SetEnvironmentVariable(    # user scope — persists, no admin
    'MINDHIVE_ECC_SKILLS', 'C:/Users/hanif/ECC/skills', 'User')

# 'Machine' scope needs elevation AND a reason — config that one user's
# tooling reads belongs at User scope; reserve Machine for genuinely
# system-wide values, behind Test-IsElevated like every HKLM write.
```

One trap worth its own line: persisted scopes do not update already-running
processes — a `User`-scope write is invisible to the current console until
a new one starts, so scripts that set-then-use must ALSO set `$env:` process
scope.

## Services: read freely, mutate deliberately

```powershell
Get-Service -Name 'Spooler' | Select-Object Name, Status, StartType

# Mutation is admin-scope on most services — guard, act, verify:
if (-not (Test-IsElevated)) {
    throw 'service control requires an elevated console.'
}
try {
    Restart-Service -Name 'SomeService' -ErrorAction Stop
} catch {
    throw "restart failed: $($_.Exception.Message)"
}
(Get-Service -Name 'SomeService').Status   # prove Running, don't assume
```

The verify-after-mutate line is the pattern: `Restart-Service` returning is
not the same as the service being `Running`, and the verification loop's
side-effect phase compares exactly these before/after states.

## Multi-line text and argument passing

```powershell
# Here-strings for anything multi-line; single-quoted = zero interpolation
# (closing '@ MUST be at column 0):
$commitMessage = @'
Fix the task trigger.
Literal $variables stay literal in this form.
'@

# Arguments that PowerShell would parse as its own syntax: stop-parsing token.
git log --% --format=%H -n 1

# Absolute paths everywhere. The calling harness resets the working directory
# between invocations, so `cd` in one command means nothing to the next.
```

## Anti-patterns with recorded incidents

```text
Forcing file creation                -> a 19-byte file became 0 bytes;
                                        guard with Test-Path, always.
Redirecting native stderr            -> $? false on exit 0 (verified:
                                        same command, both states).
Trusting bare `python`               -> exit 9009 from the WindowsApps
                                        alias; resolve via py first.
Machine-scope policy/env writes      -> outlive the script, need admin,
                                        and surprise every other user;
                                        process/user scope unless the
                                        change IS machine-wide.
Prompts in automation                -> Read-Host and un-suppressed
                                        confirms hang scheduled runs
                                        forever; -NonInteractive surfaces
                                        them as errors instead.
```

## Quick Reference

| Mechanic | Use |
|---|---|
| `Resolve-MindHivePython` / `(Get-Command x).Source` | find the real interpreter; reject the WindowsApps stub |
| `if ($?) { }` sequencing | 5.1 has no `&&`/`\|\|`; gate on the previous statement |
| `$LASTEXITCODE -ne 0` | the only honest success test for native commands |
| never redirect native stderr | avoids NativeCommandError false failures on exit 0 |
| `Out-File -Encoding utf8` / `UTF8Encoding $false` | with-BOM native default vs BOM-free via .NET |
| `Test-Path` before `New-Item` | forcing an existing file truncates it to zero bytes |
| `Test-IsElevated` | WindowsPrincipal/IsInRole check before any admin-scope step |
| `-ExecutionPolicy Bypass` (process scope) | narrowest policy override; machine scope is off-limits |
| `Get-NetTCPConnection` / `Get-ScheduledTask` | in-box modules for ports and tasks; no gallery installs |
| `@'...'@` here-strings + `--%` | literal multi-line text; stop-parsing for hostile args |
