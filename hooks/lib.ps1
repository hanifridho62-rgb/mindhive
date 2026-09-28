# SPDX-License-Identifier: Apache-2.0
# hooks/lib.ps1 — what every PowerShell hook shim needs, and nothing else.
#
# Dot-sourced, never executed. The PowerShell half of `hooks/lib.sh`, and it
# exists for the reason that file's header names: every `.ps1` shim used to open
# with
#
#     $py = "C:\Users\hanif\mindhive\.venv\Scripts\python.exe"
#
# which is correct on exactly one machine, for one username, for one checkout.
# `lib.sh` refused to repeat that on the POSIX side; this is that refusal ported
# back, so a clone anywhere gets working telemetry and session rotation instead
# of nine silent no-ops.
#
# `tests/test_hook_shims.py` asserts `.ps1` <-> `.sh` parity and names this file
# and `lib.sh` as the two sourced-not-executed exceptions;
# `tests/test_hook_shims_ps.py` drives the functions below directly, with the
# inputs designed to break them.
#
# ## The order, and why the venv beats PATH
#
# The same order as `mindhive_python`, naming the same file for one tree,
# deliberately -- two families implementing one contract drift the moment the
# rules differ, and the parity tests in `tests/test_hook_shims.py` run one
# fixture tree through both. Where this file reads a value differently, it is
# to reach the answer Git Bash reaches natively, and the code says so:
#
#   1. $env:MINDHIVE_PYTHON, when it names an existing FILE whose extension
#      PATHEXT lists (`Resolve-MindhiveOverride`). The operator's override; the
#      `.ps1` family had no equivalent before this. `/c/...` reads as `C:\...`,
#      and a name that does not exist is retried with `.exe`, as the MSYS
#      runtime does for `lib.sh`.
#   2. The nearest `.venv` above this file, probing `Scripts\python.exe`, then
#      `bin\python3.exe`, then `bin\python.exe` -- `lib.sh`'s order (see the
#      leaf list below). In an ordinary checkout that venv has `mindhive`
#      installed EDITABLE against the checkout itself, so it imports the code
#      the hook ships with. A PATH interpreter would import whatever it happens
#      to find, or nothing.
#
#      A git worktree is NOT that case. A checkout under
#      `<repo>\.claude\worktrees\<name>` with no `.venv` of its own walks up
#      to `<repo>\.venv`, whose editable install points at `<repo>\src` -- so
#      the worktree's hooks run the PARENT checkout's `mindhive`, never the
#      worktree's branch. It is still the right fallback (no interpreter can
#      import a worktree that has no venv), but a hook registered from a
#      worktree does not preview that worktree's code: a stage the parent's
#      branch lacks fails on the discarded stderr and the shim exits 0 in
#      silence. To run a worktree's own code, give it its own `.venv`
#      (`pip install -e .` inside the worktree); the walk finds the nearer one
#      first. `MINDHIVE_PYTHON` pointed at the parent's interpreter does not
#      help -- that interpreter imports the parent checkout wherever it is
#      named from.
#   3. `python3`, then `python`, from PATH — as a last resort, and only a
#      `.exe` there (see the PATH tier below).
#
# ## Why a PATH hit under WindowsApps is REFUSED
#
# With App Execution Aliases on — the Windows default — a bare `python` or
# `python3` resolves to `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`, the
# Microsoft Store redirector. It is not a missing file: it spawns cleanly,
# prints an install prompt, and exits 49. So a candidate loop that only rejects
# what does not exist would ACCEPT it and every hook would silently do nothing
# with an interpreter that never ran a line. `lib.sh`'s `mindhive_is_store_stub`
# and `src/mindhive/paths.py`'s `resolve_interpreter` (`_ALIAS_DIR`) refuse it on
# the same grounds; this is the third statement of one rule, and the three must
# agree.
#
# ## PowerShell 5.1 dialect
#
# No `&&`, `||`, `?:`, `??` or `?.` — they are 7+-only and parse-error the whole
# file in 5.1, which would make every hook on the machine a no-op at once. No
# stderr redirect of a native exe either: 5.1 wraps each emitted stderr line as
# a NativeCommandError and flips `$?` to false on a command that exited 0.
# Nothing here judges success by `$?`.

Set-StrictMode -Off

# Where this library lives, captured at dot-source time. `$env:MINDHIVE_HOOK_DIR`
# overrides it, mirroring `lib.sh` and giving the suite a seam to point the
# walk-up at a fixture tree. It is read through `ConvertFrom-MindhiveMsysPath`
# like `MINDHIVE_PYTHON`, so a `/c/...` value copied out of Git Bash names the
# same directory in both families.
$script:MindhiveLibDir = $PSScriptRoot

# How far up to look for the venv. Deep enough for a worktree under
# `<repo>\.claude\worktrees\<name>\hooks`, bounded so a hook can never walk to
# the filesystem root on a machine where the layout is not what we expect.
# Anything but a run of digits — unset, empty, `abc` — is the default, for the
# same reason `lib.sh` guards it: a hook's stderr is what Claude Code shows the
# user when it decides the hook failed.
#
# `0` is a VALUE, not junk: it means "do not walk", which is what an operator
# sets when the nearest `.venv` belongs to a different project or when the
# image resolves strictly from `MINDHIVE_PYTHON` or PATH. `lib.sh` has always
# honoured it -- `0` passes its `*[!0-9]*` junk filter and `while [ $n -lt 0 ]`
# runs zero times -- and this returned 8 and walked anyway. One contract, two
# answers, in the file whose whole reason for existing is that the two families
# must not drift.
#
# The run is BOUNDED at nine digits rather than open-ended, so `[int]` never
# sees a value past 2147483647. What a failed cast does depends on the caller's
# `$ErrorActionPreference`, measured on 5.1 under each one a caller here sets:
# under the shims' `SilentlyContinue` it is recorded in `$Error`, prints
# nothing, and an open-ended `[0-9]+` guard fell through to 8; under the default
# `Continue` the same fall-through also printed the error record on stderr,
# which Claude Code shows the user; under `circle.ps1`'s `Stop` it threw and
# ended the script. Nine digits gives all three one answer, the one `lib.sh`'s
# `??????????*` gives: ten digits or more is junk, and junk is 8.
function Get-MindhiveWalkLimit {
    $raw = $env:MINDHIVE_HOOK_WALK_LIMIT
    if ($raw -match '^[0-9]{1,9}\z') {
        return [int]$raw
    }
    return 8
}

# True when *Path* sits in the Windows App Execution Alias directory.
function Test-MindhiveStoreStub {
    param([string]$Path)
    if (-not $Path) { return $false }
    return ($Path.ToLowerInvariant().Replace('\', '/')) -like '*/windowsapps/*'
}

# A path in Git Bash's `/<drive>/...` spelling rewritten to `<DRIVE>:\...`;
# anything else comes back as it came. Git Bash maps `/c/` to drive C in either
# letter case, and an operator who copies a path out of that shell pastes this
# form. Both values that enter from the environment as a path --
# `MINDHIVE_PYTHON` and `MINDHIVE_HOOK_DIR` -- are read through here, so one
# value names one place in both families. It used to be applied to the
# override only, and a `/c/`-spelled hook dir started the walk nowhere.
function ConvertFrom-MindhiveMsysPath {
    param([string]$Value)
    if ($Value -match '^/([A-Za-z])/(.*)\z') {
        return $Matches[1].ToUpperInvariant() + ':\' + $Matches[2].Replace('/', '\')
    }
    return $Value
}

# An interpreter path when it is really a file Windows can RUN, or $null.
#
# `-PathType Leaf` and not a bare `Test-Path`, because a DIRECTORY passes the
# bare form and an operator's typo would otherwise become the interpreter every
# hook execs — `lib.sh` tests `-f` as well as `-x` for the same reason.
#
# `-ErrorAction Stop`, so a path the filesystem refuses -- an illegal character,
# a drive that does not exist -- throws INTO the catch under every preference a
# caller sets. A non-terminating error is caught by nothing: under the shims'
# `SilentlyContinue` it was quiet, but under the default `Continue` the record
# went to stderr, which is what Claude Code shows the user for a hook.
#
# And then the extension, which is `-x`'s real counterpart here. `lib.sh` asks
# whether the OS will execute the file; on Windows that question is answered by
# PATHEXT, not by a mode bit, and `&` refuses any path whose extension is not
# in it. The refusal is SILENT under each shim's
# `$ErrorActionPreference = 'SilentlyContinue'` — no output, no exit code, no
# error record — so accepting an unrunnable leaf did not degrade one hook, it
# turned all nine into permanent no-ops while a working interpreter sat one
# tier further down. That is the exact outcome this library exists to remove,
# reintroduced by the resolver itself.
#
# It is checked HERE rather than in the venv leaf list because all three tiers
# need it: `MINDHIVE_PYTHON=/usr/bin/python3` is the same mistake typed by hand.
function Test-MindhiveRunnable {
    param([string]$Path)
    $ext = [System.IO.Path]::GetExtension($Path)
    if (-not $ext) { return $false }
    $pathext = $env:PATHEXT
    if (-not $pathext) { $pathext = '.COM;.EXE;.BAT;.CMD' }
    foreach ($known in $pathext.Split(';')) {
        if ($known -and $ext -ieq $known.Trim()) { return $true }
    }
    return $false
}

function Get-MindhiveExecutable {
    param([string]$Path)
    if (-not $Path) { return $null }
    try {
        if ((Test-Path -LiteralPath $Path -PathType Leaf -ErrorAction Stop) `
            -and (Test-MindhiveRunnable $Path)) { return $Path }
    } catch {}
    return $null
}

# The operator's `MINDHIVE_PYTHON`, read the way `lib.sh` reads it under Git
# Bash, or $null.
#
# Two spellings resolved there and were refused here, so one value named an
# interpreter in one family and nothing in the other (the parity probe measured
# both):
#
#   * `/c/Users/...`, the MSYS drive form a Git Bash user copies straight out
#     of their shell. Git Bash maps `/<letter>/` to that drive in either letter
#     case; here it is rewritten to `C:\Users\...` before anything is probed.
#   * a name that does not exist, where `<name>.exe` does. The MSYS runtime
#     retries such a name with `.exe`, so `C:\py\python` and `C:\py\python3.12`
#     both resolve there (measured). ONLY a name that does not exist: Git Bash
#     refuses a DIRECTORY named `python` even with `python.exe` beside it
#     (measured), and so does this, rather than quietly running the file next
#     door.
#
# Everything else keeps the rule all three tiers share -- a runnable leaf or
# nothing -- so a directory and a non-runnable extension are still refused. A
# `.cmd` or `.bat` named here IS accepted, unlike on PATH: here the operator
# chose the file, where PATH only offers one.
function Resolve-MindhiveOverride {
    param([string]$Value)
    if (-not $Value) { return $null }
    $Value = ConvertFrom-MindhiveMsysPath $Value
    $found = Get-MindhiveExecutable $Value
    if ($found) { return $found }
    $exists = $true
    try { $exists = Test-Path -LiteralPath $Value -ErrorAction Stop } catch {}
    if ($exists) { return $null }
    return Get-MindhiveExecutable ($Value + '.exe')
}

# The first usable interpreter, or $null. Never throws.
#
# -Windowed asks for the windowless `pythonw.exe` that `autosync.ps1` uses so no
# console flashes on a scheduled run. It is a PREFERENCE, not a requirement: the
# `pythonw.exe` of either layout (`Scripts\`, then `bin\`) is probed first, and
# a venv with neither falls back to the console leaves in their usual order
# rather than answering nothing, because a visible window is a cosmetic problem
# and no sync at all is not. An extensionless POSIX venv offers neither (see
# the leaf list below), so `-Windowed` walks past it to PATH exactly as the
# console form does.
function Get-MindhivePython {
    param([switch]$Windowed)

    $found = Resolve-MindhiveOverride $env:MINDHIVE_PYTHON
    if ($found) { return $found }

    $dir = ConvertFrom-MindhiveMsysPath $env:MINDHIVE_HOOK_DIR
    if (-not $dir) { $dir = $script:MindhiveLibDir }
    if (-not $dir) { $dir = (Get-Location).ProviderPath }

    # ONE order in both families, so one tree gets one answer:
    # `Scripts\python.exe`, then `bin\python3.exe`, then `bin\python.exe`.
    # `lib.sh` probes `Scripts/python.exe`, `bin/python3`, `bin/python`, and
    # Git Bash answers those last two for the `.exe` files named here. The
    # orders used to differ, and the parity probe measured three layouts that
    # got two answers: `Scripts\` beside `bin\python.exe` (sh took `bin`, this
    # took `Scripts`), `bin\python3.exe` beside `bin\python.exe` (sh took
    # `python3`, this `python`), and a lone `bin\python3.exe` (sh took it; this
    # had no leaf for it and fell to PATH). `Scripts\` is the layout CPython's
    # `venv` creates on Windows; `bin\` is the one an MSYS2 or MinGW Python
    # creates, with real `.exe` files in it.
    #
    # Every leaf names its `.exe`. `Get-MindhiveExecutable` requires a PATHEXT
    # extension -- `&` cannot exec anything else -- and `GetExtension` reads only
    # the last segment, so the dotted `.venv` above does not count. An
    # extensionless `bin\python3` or `bin\python` is a WSL ELF binary or a
    # broken symlink on a Windows filesystem; it could never be returned, so it
    # is not listed, and such a venv falls through to PATH.
    $leaves = @('Scripts\python.exe', 'bin\python3.exe', 'bin\python.exe')
    if ($Windowed) {
        $leaves = @('Scripts\pythonw.exe', 'bin\pythonw.exe') + $leaves
    }

    $limit = Get-MindhiveWalkLimit
    $n = 0
    while ($dir -and $n -lt $limit) {
        foreach ($leaf in $leaves) {
            # Built as a STRING, not with `Join-Path`. That cmdlet resolves the
            # drive through the provider and THROWS for one that does not
            # exist -- a `MINDHIVE_HOOK_DIR` of `Z:\...` -- and it was called
            # outside any try, in a function whose contract is that it never
            # throws: under `circle.ps1`'s `Stop` that was a raw
            # DriveNotFoundException in place of circle's own message.
            $found = Get-MindhiveExecutable ($dir.TrimEnd('\', '/') + '\.venv\' + $leaf)
            if ($found) { return $found }
        }
        $parent = $null
        # `-Path`, deliberately, and NOT `-LiteralPath`: the two do not both
        # bind alongside `-Parent` in 5.1 ("Parameter set cannot be resolved"),
        # so the literal form throws, the catch below swallows it and the walk
        # silently stops one level up. It is safe as it stands because
        # Split-Path here is string manipulation rather than a filesystem
        # probe -- `Split-Path -Path 'C:\src\s[wip]rc\hooks' -Parent` returns
        # `C:\src\s[wip]rc` intact. `Test-Path` is the one that globs, and
        # that is where `-LiteralPath` is load-bearing.
        try { $parent = Split-Path -Path $dir -Parent -ErrorAction Stop } catch {}
        if (-not $parent) { break }
        if ($parent -eq $dir) { break }
        $dir = $parent
        $n = $n + 1
    }

    # 3. PATH, with the Store redirector refused. An explicit walk of
    #    `$env:PATH`, entry by entry, the way `lib.sh` walks `$PATH`:
    #
    #    * EVERY entry, not the first hit -- that is what makes the Store
    #      refusal a SKIP instead of a STOP. Windows ships the redirector in
    #      `%LOCALAPPDATA%\Microsoft\WindowsApps`, which sits on PATH ahead of
    #      a per-user install on a default profile, so stopping at the first
    #      `python.exe` answered $null on a machine that had a real interpreter
    #      one entry further down. Silent and total, which is the failure mode
    #      this whole library exists to remove.
    #    * Name-major, `lib.sh`'s loop order: a `python3.exe` anywhere on PATH
    #      beats a `python.exe` anywhere.
    #    * Quotes stripped. An entry written `"C:\Program Files\Py"` is legal
    #      -- cmd.exe strips the quotes, and so does the MSYS runtime for
    #      `lib.sh` -- but `Get-Command`, which this tier used to lean on, does
    #      not, so an interpreter that sat only in such an entry was found by
    #      one family and not the other (measured by review). On a machine with
    #      no venv that was every PowerShell hook silently doing nothing.
    #    * Only a `.exe`. `Get-Command` answered every PATHEXT match, so a
    #      `python.cmd` or `python.bat` ahead of a real `python.exe` used to win
    #      here -- and a `.cmd` or `.bat` runs through `cmd.exe`, which
    #      re-parses the arguments it is handed and can be any wrapper at all.
    #      `lib.sh` takes neither: it asks PATH for `<name>` and `<name>.exe`,
    #      and Git Bash neither completes `python` to `python.cmd` nor reports
    #      one executable (measured). The override (tier 1) still takes any
    #      runnable extension, because there the operator named the file. The
    #      one asymmetry left is deliberate: under Git Bash `lib.sh` also takes
    #      an extensionless `#!` script, which that shell CAN exec and this
    #      cannot; refusing it there to match here would cost a `.sh` user a
    #      working interpreter.
    #    * An EMPTY entry is skipped, not read as the current directory. POSIX
    #      gives an empty PATH element that meaning and `lib.sh` honours it;
    #      Windows does not, and a hook that execs whatever `python.exe` sits
    #      in Claude Code's working directory would be a PATH hijack the
    #      platform itself does not perform.
    #
    #    `Get-MindhiveExecutable` probes under `-ErrorAction Stop` inside its
    #    try, so an entry holding a character the filesystem refuses is skipped
    #    in silence rather than printed under `Continue`.
    $entries = @()
    if ($env:PATH) { $entries = $env:PATH.Split(';') }
    foreach ($name in @('python3', 'python')) {
        foreach ($entry in $entries) {
            $base = $entry.Trim().Trim('"').TrimEnd('\', '/')
            if (-not $base) { continue }
            $hit = Get-MindhiveExecutable ($base + '\' + $name + '.exe')
            if ($hit -and (-not (Test-MindhiveStoreStub $hit))) { return $hit }
        }
    }

    return $null
}

# `Resolve-MindHivePython` is the name MEB HOOK-02, spec 10.13.1 and the
# `windows-powershell-patterns` skill give this resolver. It is a NAME, not a
# second resolver. Spec 10.13.1's standalone body differs from the one above
# on every tier -- a bare `Test-Path` that accepts a directory; a five-level
# walk that starts from `$MyInvocation.MyCommand.Path`, which is `$null`
# inside a function; `python.exe` alone on PATH; no `-Windowed`; and, when
# nothing resolves, the bare name `python.exe`, which `&` would look up at
# call time, Store redirector included -- and two bodies for one contract
# drift the moment one of them is edited.
#
# The switch is declared and passed on by value rather than splatted with
# `@args`: in 5.1, `@args` re-binds `-Windowed:$false` as the bare, PRESENT
# switch and hands the `$false` on as a stray positional that
# `Get-MindhivePython` swallows, so an explicit `$false` answered
# `pythonw.exe` (measured).
function Resolve-MindHivePython {
    param([switch]$Windowed)
    Get-MindhivePython -Windowed:$Windowed
}

# The characters a `session_id` may contain. It names a ledger segment:
# `events.append_event` writes `<events>/<session_id>.jsonl`, so a value with a
# separator, a `..`, or a drive letter in it is a path, not an id. The Python
# side quarantines such a value at the segment path (`events.segment_name`);
# this refuses it a step earlier, at the boundary where it enters the system.
#
# `\z` and not `$`: in .NET `$` also matches immediately BEFORE a trailing
# newline, so an id ending in a line break would pass a `$`-anchored check and
# reach the ledger carrying one.
$script:MindhiveSessionIdPattern = '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\z'

# The `session_id` out of a Claude hook payload, or `unknown`.
#
# `lib.sh` shells out to the interpreter for this because POSIX sh cannot parse
# JSON and a hand-rolled regex over it is wrong the first time a value carries
# an escaped quote. PowerShell has `ConvertFrom-Json`, so the parse costs no
# process. Only the three session-stamping shims call this, once per session,
# so the process would be affordable; it is not spent because the two parsers
# can only disagree about which of two SAFE answers a malformed payload gets.
# 5.1's `ConvertFrom-Json` accepts single-quoted strings and unquoted keys that
# `json.load` rejects (that family answers `unknown`, this one the id, which
# still passed the whitelist), and it throws on `session_id` beside
# `Session_ID` (this family answers `unknown`, that one the id). Neither can
# let a value the whitelist refuses through, which is the property that
# matters.
#
# The payload is machine-generated by Claude Code, whose ids are UUIDs, so
# nothing real is ever refused; the whitelist is there for the day something
# other than Claude Code writes to this stdin.
function Get-MindhiveSessionId {
    param([string]$Raw)
    if (-not $Raw) { return 'unknown' }
    $value = $null
    # The key is matched ORDINALLY. Bare `$parsed.session_id` is
    # case-insensitive in PowerShell, so a payload carrying `Session_ID` was
    # read here and ignored by `lib.sh` and the Python side; `-ceq` fixed the
    # case but compares LINGUISTICALLY, so a key carrying a soft hyphen
    # (U+00AD) still matched `session_id` here and nowhere else (measured by
    # review). The three whitelists have to agree about which key they are
    # reading, byte for byte, not only about which values they accept.
    try {
        $parsed = $Raw | ConvertFrom-Json
        if ($parsed) {
            $prop = @($parsed.PSObject.Properties |
                      Where-Object { [string]::Equals($_.Name, 'session_id',
                                                       [System.StringComparison]::Ordinal) })
            if ($prop.Count -gt 0) { $value = $prop[0].Value }
        }
    } catch {
        return 'unknown'
    }
    # A nested object, an array or a number answers `unknown` rather than being
    # stringified: a hashtable renders as a type name, which would pass the
    # whitelist and become a segment filename.
    if ($value -isnot [string]) { return 'unknown' }
    if ($value -cmatch $script:MindhiveSessionIdPattern) { return $value }
    return 'unknown'
}

# The payload from stdin, or "" when stdin is a terminal.
#
# The guard is what makes these hooks hang-proof: `[Console]::IsInputRedirected`
# is the PowerShell spelling of `[ ! -t 0 ]`, and without it a shim run by hand
# blocks forever on a read no one is going to answer.
function Get-MindhivePayload {
    try {
        if ([Console]::IsInputRedirected) { return [Console]::In.ReadToEnd() }
    } catch {}
    return ''
}

# Start a detached, silent background process. The PowerShell half of
# `mindhive_detach`, for work that must never delay the session it is attached
# to. Never throws.
function Start-MindhiveDetached {
    param([string]$FilePath, [string[]]$ArgumentList)
    try {
        Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WindowStyle Hidden
    } catch {}
}
