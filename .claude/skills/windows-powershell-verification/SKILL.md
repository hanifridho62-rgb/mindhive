---
name: windows-powershell-verification
description: >-
  Verification loop for PowerShell work on this machine: console/elevation
  state, static parse audit, 5.1 dialect audit, real execution gated on
  exit codes, byte-level encoding audit, and side-effect accounting. Run
  before calling any .ps1 change or Windows automation done.
origin: mindhive
pack: windows-powershell
role: verification
kind: verification
---

# Windows PowerShell Verification Loop

Proving a PowerShell change works on Windows PowerShell 5.1, in the console
it will really run in — elevated or not — before anyone calls it done. The
loop moves from free static checks to real execution to side-effect
accounting, so the expensive phases only run over scripts that can parse.

## When to Activate

- A `.ps1` script or inline PowerShell workflow was written or modified
- Automation touched scheduled tasks, services, ports, registry keys, or
  machine settings
- A script is about to be handed to an operator who may run it elevated
- A "works on my console" report needs to become reproducible evidence
- Encoding-sensitive files (JSON, configs, source) were written from PowerShell
- A script must behave sanely in BOTH elevated and non-elevated consoles
- Documentation or skills embedding PowerShell snippets changed and needs auditing

## Phase 1: Console and elevation state

Record what kind of console the evidence comes from. The `Test-IsElevated`
function from the sibling patterns file is the probe; the engine version and
policy state complete the picture:

```powershell
$PSVersionTable.PSVersion            # must report 5.1.x — 7+ results do not
                                     # transfer backwards
Test-IsElevated                      # True/False goes into the report header
Get-ExecutionPolicy -List            # any GroupPolicy scope set? note it
[System.Text.Encoding]::Default.WebName   # the ANSI codepage in play
```

Interpretation rules:

- Evidence gathered on PowerShell 7+ says nothing about 5.1 — the operators
  that 7 accepts and 5.1 parser-errors on are precisely what Phase 3 hunts.
- If `MachinePolicy` or `UserPolicy` shows a value, Group Policy owns the
  execution policy and no script-side override will work; record it and
  hand the finding to the operator instead of fighting it.
- If the change is admin-scope (task registration, service control, HKLM:)
  and the console is NOT elevated, stop here — running the later phases
  would test the refusal path, not the change.

## Phase 2: Static parse audit

Before running anything, prove every changed script *parses* on 5.1. The
tokenizer surfaces parser errors without executing a single statement:

```powershell
$changed = git diff --name-only HEAD | Where-Object { $_ -like '*.ps1' }
foreach ($file in $changed) {
    $tokens = $null; $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        (Resolve-Path $file).Path, [ref]$tokens, [ref]$parseErrors) | Out-Null
    if ($parseErrors.Count -gt 0) {
        $parseErrors | ForEach-Object {
            Write-Warning ("{0}:{1} {2}" -f $file,
                $_.Extent.StartLineNumber, $_.Message)
        }
    } else {
        Write-Output "$file : parses clean"
    }
}
```

This is the phase that catches a stray `&&` or `?.` copied in from a 7+
snippet, at zero risk — a parse error here is a guaranteed runtime failure
there. Any parse error is an automatic stop: nothing downstream can produce
evidence about a script the engine cannot read.

## Phase 3: Dialect and anti-pattern audit

Mechanical scan of every changed `.ps1` for the constructs 5.1 rejects,
misinterprets, or that this machine has recorded as data-loss traps:

```powershell
foreach ($file in $changed) {
    # 7+-only operators (parse errors — should be gone after Phase 2, but
    # inline strings and here-docs can hide them from the tokenizer):
    Select-String -Path $file -Pattern '&&|\|\||\?\?|\?\.' |
        Select-Object Path, LineNumber, Line
    # native stderr redirects (the false-failure trap):
    Select-String -Path $file -Pattern '\b(git|py|gh|node|npm)\b[^\r\n]*2>' |
        Select-Object Path, LineNumber, Line
    # $? used to judge a native command (exit codes are the contract):
    Select-String -Path $file -Pattern '\$\?\s*(-eq|-and|\))' -Context 1,0 |
        Select-Object Path, LineNumber, Line
    # unguarded file creation (the truncation trap):
    Select-String -Path $file -Pattern 'New-Item[^\r\n]*-Force' |
        Select-Object Path, LineNumber, Line
    # encoding-blind writes (ANSI mangling of non-ASCII):
    Select-String -Path $file -Pattern 'Set-Content(?![^\r\n]*-Encoding)' |
        Select-Object Path, LineNumber, Line
}
```

Every hit needs either a fix or a written justification, and the
justification standard is concrete: a `New-Item -Force` on a *directory* is
legal; on a file it is the zero-byte truncation bug. A `$?` right after a
*cmdlet* is fine; after `git` it is the lie the patterns file documents. A
clean pass here means the script will parse AND not silently misreport.

## Phase 4: Real execution, gated on exit codes

Run the actual script in a fresh, non-interactive 5.1 process — the
environment it will really face — and judge only by exit codes:

```powershell
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File .\changed-script.ps1
if ($LASTEXITCODE -ne 0) {
    throw "script failed in a clean console: exit $LASTEXITCODE"
}
```

Why each flag is load-bearing:

- `-NoProfile`: profiles hide missing-dependency bugs by preloading modules
  and variables the target machine will not have. A script that only works
  with your profile is not done.
- `-NonInteractive`: any `Read-Host`, credential prompt, or un-suppressed
  confirmation hangs forever in scheduled/automated contexts — here it
  errors immediately instead, which is the finding you want.
- `-ExecutionPolicy Bypass`: process-scope only; the run must not depend on
  a persistent policy the target machine may not share.

For scripts with an admin path, the contract has TWO halves and both get
exercised when consoles allow:

```powershell
# Non-elevated console: the refusal path IS a feature. Expect a clear
# throw naming the elevation requirement — record its exact message.
powershell -NoProfile -NonInteractive -File .\task-admin.ps1
"non-elevated exit: $LASTEXITCODE"     # documented non-zero expected

# Elevated console: the real path. Same command, exit 0 expected.
```

A script that limps half-way through as a standard user before dying on
access-denied fails this phase even if the elevated run passes — the
patterns file's check-first rule exists precisely so refusal happens before
any mutation.

## Phase 5: Encoding audit — trust bytes, not cmdlet names

Any file the change wrote gets a byte-level probe. The patterns file names
the three 5.1 encoding defaults; this phase proves which one actually
happened:

```powershell
function Test-Utf8Bom {
    param([string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
        return 'UTF-16LE BOM (Out-File default — most parsers choke)'
    }
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and
        $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        return 'UTF-8 with BOM (acceptable; strip for strict parsers)'
    }
    return 'no BOM (UTF-8 clean or ANSI — spot-check non-ASCII content)'
}

Test-Utf8Bom -Path $writtenFile
```

For files consumed by git, Python, or JSON parsers the target is BOM-free
UTF-8; a UTF-16LE result here is an automatic fail regardless of how the
script "looked". The no-BOM verdict is NOT automatically a pass — ANSI
mangling is invisible at the BOM level, so any file that should carry
non-ASCII gets one content check:

```powershell
# Round-trip proof: read as UTF-8 and look for replacement/mojibake
# artifacts where the known non-ASCII content should be.
$roundTrip = [System.IO.File]::ReadAllText($writtenFile,
    [System.Text.Encoding]::UTF8)
$roundTrip | Select-String -Pattern 'ü|é|—' -Quiet   # expected chars present?
```

## Phase 6: Environment side-effect accounting

Confirm the change did exactly the side effects it claimed — no more, no
fewer — using the same in-box cmdlets the patterns file wires. The
technique is a before/after snapshot around the Phase 4 run:

```powershell
# BEFORE the run (capture):
$tasksBefore = Get-ScheduledTask -TaskName 'MindHive*' -ErrorAction SilentlyContinue |
    Select-Object TaskName, State
$portBefore  = $null -ne (Get-NetTCPConnection -LocalPort 54461 `
    -State Listen -ErrorAction SilentlyContinue)

# AFTER the run (compare):
$tasksAfter = Get-ScheduledTask -TaskName 'MindHive*' -ErrorAction SilentlyContinue |
    Select-Object TaskName, State
Compare-Object $tasksBefore $tasksAfter -Property TaskName, State
$portAfter = $null -ne (Get-NetTCPConnection -LocalPort 54461 `
    -State Listen -ErrorAction SilentlyContinue)
if ($portBefore -and -not $portAfter) {
    Write-Warning 'the dashboard port stopped listening during the run'
}
```

Findings this phase exists to catch: a task left `Disabled` that was
`Ready`, a port gone quiet, a service the script restarted "temporarily"
and never brought back, a registry value written at machine scope when the
change claimed user scope. Automation that "worked" while breaking the
standing environment did not work. For task-touching changes, close with
last-run evidence:

```powershell
Get-ScheduledTask -TaskName 'MindHiveSync' -ErrorAction SilentlyContinue |
    Get-ScheduledTaskInfo |
    Select-Object LastRunTime, LastTaskResult, NextRunTime
```

`LastTaskResult` is an exit code and 0 means the task's LAST real run
succeeded — registering a task that fails on first fire is the most common
"done but broken" outcome in scheduled automation.

## Interpreting common failure signatures

The recurring signatures this loop surfaces on this machine, with their
mechanisms — so a report names causes, not symptoms:

```text
exit 9009 from a "python" call
  -> the WindowsApps alias fired: the command resolved to the Store stub,
     which spawns cleanly (no ENOENT!) and exits 9009. Fix the resolution
     (patterns file: Resolve-MindHivePython); note that candidate loops
     that only advance on ENOENT will break on this stub instead of
     trying the next interpreter.

"the term '...' is not recognized" only in the clean console
  -> the profile was supplying the module/alias; the dependency is real
     and must be imported or resolved explicitly in the script.

works elevated, "Access is denied" as standard user, PARTIAL changes on disk
  -> mutation started before the rights check; move the elevation guard
     ahead of the first side effect (check-first rule) and add the
     refusal-path test to Phase 4.

LastTaskResult 0x1 / 267011 (0x41303) on a registered task
  -> 0x1: the action ran and failed (usually the interpreter/path problem
     above, inside the task context — SYSTEM has a different PATH).
     0x41303: the task has never run yet — registration evidence only;
     fire it once before reporting.

ConvertFrom-Json fails on a file another tool wrote fine
  -> Phase 5 territory: a BOM or UTF-16 write upstream; probe bytes
     before blaming the JSON.

script "passed" but $? was consulted after a native command
  -> the false-success mirror of the stderr trap: $? reflects the last
     POWERSHELL statement, not the native exit; the dialect audit's $?
     sweep exists for exactly this.
```

When a signature is not in this table, capture the full transcript with
`Start-Transcript` in the clean-console run and attach it — the transcript
of a `-NoProfile` run is reproducible evidence a description never is.

## Stop conditions

- Phase 1: admin-scope change on a non-elevated console → stop; the later
  phases would measure the refusal path only.
- Phase 2: any parse error → stop; the script cannot be read where it will
  run, and every later phase would be vacuous.
- Phase 3: an unjustified hit on the truncation or stderr traps → stop and
  fix; these are recorded data-loss/false-failure bugs, not style.
- Phase 4: non-zero exit in the clean console, or a standard-user run that
  mutated anything before refusing → stop.
- Phase 5: UTF-16LE bytes in a file another tool must parse → stop and
  rewrite the write path.
- Phase 6: any side-effect delta the change did not claim → stop; account
  for it or revert it.

## Output Template

```
POWERSHELL 5.1 VERIFICATION
===========================
Phase 1  console state    [RECORDED]   5.1.x, elevated=<bool>, policy=<scopes>, cp=<codepage>
Phase 2  static parse     [PASS/FAIL]  <n> files, <m> parse errors
Phase 3  dialect audit    [PASS/FAIL]  hits: ops=<n> stderr=<n> $?=<n> force=<n> enc=<n> (fixed/justified)
Phase 4  clean execution  [PASS/FAIL]  exit <code> (std), exit <code> (elevated/N-A), refusal msg: <quote/N-A>
Phase 5  encoding bytes   [PASS/FAIL]  <file>: <BOM verdict> (+ round-trip <ok/N-A>)
Phase 6  side effects     [PASS/WARN]  tasks Δ=<n>, port 54461 <state>, unclaimed deltas: <n>

Overall: [READY / NOT READY]

Notes:
- <justified -Force hits, accepted warnings, consoles not available, GP-owned policy>
```

## Worked example: a task-registration script

The reference shape of a passing run for a script that registers the
MindHive sync task:

```text
Phase 1  5.1.26100, elevated=True, policy=(Process:Bypass, rest:Undefined)
Phase 2  1 file, parses clean
Phase 3  1 hit: New-Item -Force on log DIRECTORY — justified inline
Phase 4  std console: exit 1, refusal "needs an elevated console" (quoted)
         elevated:    exit 0
Phase 5  written config.json: no BOM; round-trip ok
Phase 6  tasks Δ=+1 (MindHiveSync, Ready), LastTaskResult=0 after first
         fire; port 54461 listening before and after
Overall  READY
```

The two details reviewers miss most often are both here: the refusal
message is *quoted as evidence* (the non-elevated contract is tested, not
assumed), and the task was actually fired once so `LastTaskResult` proves
the registered action runs — registration alone proves only that XML landed.

## Scope pairing with the other loops

This loop verifies PowerShell *as PowerShell*. Two adjacent loops pick up
where it stops, and hook shims are the case where all three meet:

```text
A .ps1 that is a harness hook shim
  -> THIS loop: phases 1-5 (parse, dialect, clean-console run, encoding).
  -> the harness loop: registration + live-fire (a shim can be perfect
     5.1 and still never be called).
  -> the python loop: the handler the shim dispatches into.

A .ps1 that automates Windows around the toolchain (tasks, ports, services)
  -> THIS loop end to end; phase 6's side-effect accounting is the star.

Inline PowerShell inside documentation or skills
  -> phases 2-3 only (parse + dialect), run against extracted snippets;
     there is nothing to execute or account for, but a doc teaching a
     7+-only operator to a 5.1 machine is a real defect this catches.
```

Evidence hygiene for every run: pair each exit code with the exact command
line that produced it (copy-paste, not paraphrase), keep the
`Start-Transcript` output of clean-console runs beside the report, and
state the console (std/elevated) per line of evidence — unattributed
evidence from mixed consoles is how elevation bugs hide in reports.

## Quick Reference

| Mechanic | Use |
|---|---|
| `$PSVersionTable.PSVersion` + `Test-IsElevated` | pin the console the evidence came from |
| `Get-ExecutionPolicy -List` GP check | MachinePolicy/UserPolicy set → report, don't fight |
| `Language.Parser::ParseFile` | prove 5.1 can read the script before running anything |
| `Select-String` dialect sweeps | 7+ ops, native `2>`, `$?`-on-native, forced creation, encoding-blind writes |
| `powershell -NoProfile -NonInteractive -File` | the clean-console execution gate |
| both-consoles contract | refusal message quoted (std) + exit 0 (elevated) |
| `Test-Utf8Bom` + round-trip read | prove bytes, then prove the non-ASCII survived |
| before/after `Compare-Object` snapshot | only the claimed side effects happened |
| `Get-ScheduledTaskInfo` LastTaskResult | the task RAN, not merely registered |
| exit 9009 signature | the WindowsApps alias fired; fix resolution, not the script |
| 0x41303 vs 0x1 task results | never-ran vs ran-and-failed — different fixes entirely |
| `Start-Transcript` in clean runs | reproducible evidence a prose description never is |
| per-line console attribution | std vs elevated named on every piece of evidence |
| snippet extraction for docs | inline PowerShell in prose still gets parse + dialect phases |
| refusal-before-mutation check | a partial standard-user change fails even if elevated passes |
| GroupPolicy-owned policy scopes | report and hand off; no script-side override exists |
