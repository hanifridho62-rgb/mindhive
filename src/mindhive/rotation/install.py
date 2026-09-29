# SPDX-License-Identifier: Apache-2.0
# src/mindhive/rotation/install.py
"""Idempotent registration of the Stop hook in a Claude Code settings.json.

This is the one part of the feature that reaches outside the repo and edits a
file the user's whole toolchain depends on. Corrupting it does not break
rotation — it breaks Claude Code. So the order is fixed and non-negotiable:

    read -> parse -> refuse if unparseable -> back up -> mutate -> write

"Refuse if unparseable" is the load-bearing step. If the JSON does not parse we
cannot know what is in there, and a repair would be a guess written over the
user's configuration. Reporting `malformed` and touching nothing is the only
honest option.

Registration does NOT activate the hook. The hook is inert unless
`MINDHIVE_ROTATION` is set, and `hive session run` sets that only for the
children it spawns. So installing this is safe: it changes what a supervised
session does, and leaves every other session on the machine exactly as it was.

**Rotation is no longer Windows-only, and this is where that stopped being
true.** This module used to register `powershell -NoProfile -File hooks/stop.ps1`
unconditionally and warn, on macOS or Linux, that "rotation is Windows-only until
a POSIX shim exists" — because at the time every shim in `hooks/` was PowerShell.
`hooks/stop.sh` and its eight siblings exist now, so that command was registering
an interpreter the platform does not have while a working shim sat next to it,
and the warning had become a statement about the past rather than about the
machine it was printed on.

`default_command` now picks the shim that matches the platform, and
`platform_warning` asks the FILESYSTEM whether that shim is present instead of
comparing `os.name` to a constant. Two consequences worth stating: the answer
stays honest if a future platform gains a shim (or loses one) without this
docstring being updated, and the warning now means "the shim for your platform is
missing", which is actionable, rather than "you are not on Windows", which was
not.

It still warns rather than refuses. Editing a settings.json that will be read on
another machine is legitimate, and the honest answer to "this may not do what you
want" is to say so.
"""
import re
import json
import os
import shutil
from pathlib import Path

STAGE = "Stop"

# `os.name` of the platform whose shims are PowerShell. Retained as the name of a
# PLATFORM rather than of a capability: it selects which shim to register, and no
# longer decides on its own whether rotation can work — `platform_warning` asks
# the filesystem that.
SHIM_OS = "nt"

# The Stop shim per platform, and how to invoke it. `sh` rather than executing
# the file directly: the shims carry `#!/bin/sh` and the mode bit survives git on
# POSIX, but a settings.json is also edited from Windows for a POSIX machine and
# an explicit interpreter is what makes that registration correct either way.
#
# The shim path is QUOTED. Unquoted, a checkout under `C:/My Repo/mindhive`
# registers `-File C:/My Repo/.../stop.ps1`, PowerShell reads the argument as
# `C:/My` and fails -- and because this is the Stop stage, that failure is
# printed at the end of every agent turn, in every project on the machine. The
# six stages registered by hand in `~/.claude/settings.json` are unquoted; this
# deliberately does not copy them, because they were written for one machine
# whose path happens to have no space in it and this function writes settings
# for machines it has never seen.
#
# `-ExecutionPolicy Bypass`, for the same reason. A Windows CLIENT's default
# execution policy is `Restricted`: with no scope ever set, `powershell -File`
# refuses to load ANY script, prints a SecurityError on stderr and exits 1 --
# on every hook fire, before a line of the fail-open shim runs. Measured on 5.1
# against `hooks/stop.ps1`: `-ExecutionPolicy Restricted` printed `cannot be
# loaded because running scripts is disabled on this system` and exited 1;
# `Bypass` exited 0. The flag applies to that one process and changes no
# machine setting. The one machine this used to work on has `CurrentUser =
# RemoteSigned`, which is why the omission was never seen. A registration
# written before the flag existed still EXECUTES this shim, so `install`
# reports it `already_registered` rather than adding a second entry; on a
# machine where the policy was never set it never worked, and the README says
# to re-spell it by hand.
_SHIMS = {
    "nt": ("hooks/stop.ps1",
           'powershell -NoProfile -ExecutionPolicy Bypass -File "{shim}"'),
    "posix": ("hooks/stop.sh", 'sh "{shim}"'),
}


def _shim_for(os_name: str | None = None):
    """``(relative path, command template)`` for *os_name*, defaulting to this
    machine. Anything that is neither `nt` nor `posix` is treated as POSIX,
    because that is what every non-Windows platform Python reports actually is."""
    os_name = os.name if os_name is None else os_name
    return _SHIMS.get(os_name, _SHIMS["posix"])


def _repo_root(repo=None) -> Path:
    return Path(repo) if repo else Path(__file__).resolve().parents[3]


# A bare token, or a quoted one with the quotes removed. `shlex` is deliberately
# not used: it has POSIX and Windows dialects that disagree about backslashes,
# and the only job here is to recover the path out of a command line this module
# itself wrote in one of two known spellings.
_TOKEN_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|(\S+)')


# The interpreters a Stop registration runs its shim through, by the name they
# are invoked as: casefolded, with any `.exe` dropped.
_PS_HOSTS = frozenset({"powershell", "pwsh"})
_SH_HOSTS = frozenset({"sh", "bash", "dash", "zsh"})
# The parameters of `powershell.exe` and `pwsh`, by full name, split by what
# follows them. A command line is parsed only as far as these name it: an
# unknown or ambiguous parameter, or a positional word, leaves it unparsed.
_PS_COMMAND_PARAMS = frozenset({"-command", "-commandwithargs",
                                "-encodedcommand"})
_PS_VALUE_PARAMS = frozenset({
    "-configurationname", "-custompipename", "-executionpolicy",
    "-inputformat", "-outputformat", "-psconsolefile", "-settingsfile",
    "-version", "-windowstyle", "-workingdirectory",
})
_PS_SWITCH_PARAMS = frozenset({
    "-help", "-interactive", "-login", "-mta", "-noexit", "-nologo",
    "-noninteractive", "-noprofile", "-noprofileloadtime", "-sta",
})
_PS_PARAMS = (_PS_COMMAND_PARAMS | _PS_VALUE_PARAMS | _PS_SWITCH_PARAMS
              | {"-file"})
# The documented short names. Any other spelling counts only as the unique
# prefix of one full name, which is how PowerShell itself resolves it.
_PS_ALIASES = {
    "-c": "-command", "-cwa": "-commandwithargs", "-e": "-encodedcommand",
    "-ec": "-encodedcommand", "-enc": "-encodedcommand",
    "-ep": "-executionpolicy", "-ex": "-executionpolicy", "-f": "-file",
    "-if": "-inputformat", "-l": "-login", "-noe": "-noexit", "-nol": "-nologo",
    "-noni": "-noninteractive", "-nop": "-noprofile", "-o": "-outputformat",
    "-of": "-outputformat", "-v": "-version", "-w": "-windowstyle",
    "-wd": "-workingdirectory",
}
# Long options of those shells that consume the following word.
_SH_ARG_OPTIONS = frozenset({"--rcfile", "--init-file"})


def _norm_path(value, os_name) -> str:
    """One path in the spelling two registrations are compared in, on the
    platform that RUNS them. Windows ignores case and takes either separator;
    POSIX does neither, so folding there made `/repo/HOOKS/STOP.SH` -- another
    file, or no file -- read as this checkout's registered shim."""
    text = str(value or "")
    if os_name == "nt":
        return text.replace("\\", "/").rstrip("/").casefold()
    return text.rstrip("/")


# The file names a Stop registration runs, one per shim family, casefolded. A
# pre-filter only: whether two paths are the same FILE is `_same_target`'s
# question, asked in the running platform's own spelling.
_SHIM_NAMES = frozenset(Path(rel).name.casefold() for rel, _t in _SHIMS.values())


def _tokens(command) -> list:
    """*command* as words with their quotes removed. An empty quoted word stays
    an empty word, so it still occupies its position."""
    words = []
    for m in _TOKEN_RE.finditer(command):
        words.append(next(g for g in m.groups() if g is not None))
    return words


def _ps_parameter(word):
    """The full PowerShell parameter name *word* spells -- exactly, by a
    documented short name, or as the unique prefix of one -- or None."""
    flag = word.casefold()
    if len(flag) < 2 or not flag.startswith("-"):
        return None
    if flag in _PS_PARAMS:
        return flag
    if flag in _PS_ALIASES:
        return _PS_ALIASES[flag]
    hits = [name for name in _PS_PARAMS if name.startswith(flag)]
    return hits[0] if len(hits) == 1 else None


def _executed_script(tokens):
    """The script *tokens* EXECUTE, or None when this cannot name one.

    The rule used to be whether ANY word named a Stop shim, so a command that
    only mentioned one -- `echo <shim>`, `powershell -Command Get-Content
    <shim>`, `sh -c cat <shim>` -- counted as a registration, and `install`
    answered `already_registered` for a hook that never runs.

    Parsed: `powershell` or `pwsh` with known parameters before `-File
    <script>`; `sh`, `bash`, `dash` or `zsh` with options before the script;
    and a script run directly. Anything else names no script, and is matched
    only by its exact string."""
    if not tokens:
        return None
    head = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if head.endswith(".exe"):
        head = head[:-len(".exe")]
    if head in _PS_HOSTS:
        i = 1
        while i < len(tokens):
            name = _ps_parameter(tokens[i])
            if name is None or name in _PS_COMMAND_PARAMS:
                # A command string follows, or a positional word (the command
                # on Windows PowerShell, the file on pwsh), or a parameter this
                # table cannot resolve: nothing after it is parsed as a path.
                return None
            if name == "-file":
                return tokens[i + 1] if i + 1 < len(tokens) else None
            i += 2 if name in _PS_VALUE_PARAMS else 1
        return None
    if head in _SH_HOSTS:
        i = 1
        while i < len(tokens):
            word = tokens[i]
            if word == "--":
                return tokens[i + 1] if i + 1 < len(tokens) else None
            if word in _SH_ARG_OPTIONS:
                i += 2
            elif word.startswith("--"):
                i += 1
            elif len(word) > 1 and word[0] in "-+":
                if "c" in word[1:] or "s" in word[1:]:
                    return None     # `-c` runs a string, `-s` reads stdin
                i += 2 if word[-1] in "oO" else 1
            else:
                return word
        return None
    return tokens[0]


def _target(command, os_name):
    """The Stop shim *command* executes, in `_norm_path` spelling, or None.

    Total, and a non-string runs NOTHING. A malformed settings.json can hold a
    list or an object under `command`, and this used to `str()` it: the repr
    quotes each element, the token pattern strips the quotes, and a JSON array
    that merely contained the shim path matched. So an entry Claude Code cannot
    run blocked the registration of one it can."""
    if not isinstance(command, str):
        return None
    script = _executed_script(_tokens(command))
    if not script:
        return None
    path = _norm_path(script, os_name)
    return path if path.rsplit("/", 1)[-1].casefold() in _SHIM_NAMES else None


def _same_target(wanted, existing, os_name) -> bool:
    """Whether two `_target` answers name one file: the same spelling, or --
    when the settings belong to THIS platform -- the same file on disk, so a
    link to the shim is the registration it runs."""
    if not wanted or not existing:
        return False
    if wanted == existing:
        return True
    if os_name != os.name:
        return False    # another platform's paths: this filesystem cannot answer
    if not (os.path.isabs(wanted) and os.path.isabs(existing)):
        # A relative path resolves against the directory Claude Code runs the
        # hook from -- every project on the machine -- not against the one
        # `install-hook` happens to run from, where it may well be this shim.
        return False
    try:
        return os.path.samefile(wanted, existing)
    except (OSError, ValueError):
        return False

_PLATFORM_WARNING = (
    "registered, but rotation cannot fire here: the Stop shim for this platform "
    "({os_name!r}) should be at {shim} and that file is missing. The Stop hook "
    "will be invoked and do nothing."
)


def platform_warning(os_name: str | None = None, repo: str | None = None) -> str:
    """The caveat for this platform, or "" when the shim is actually present.

    Asks the FILESYSTEM rather than comparing `os.name` to a constant. The old
    form answered "you are not on Windows", which stopped being the right
    question the day `hooks/stop.sh` landed and would have gone on being printed
    forever. This answers "the shim you need is not there", which is both
    currently true and still true after the next platform is added."""
    rel, _template = _shim_for(os_name)
    shim = _repo_root(repo) / rel
    if shim.is_file():
        return ""
    return _PLATFORM_WARNING.format(
        os_name=os.name if os_name is None else os_name, shim=shim.as_posix())


def default_command(python: str | None = None, repo: str | None = None,
                    os_name: str | None = None) -> str:
    """The command string registered for the hook, matching the platform.

    Registers `hooks/stop.ps1` through `powershell` on Windows and
    `hooks/stop.sh` through `sh` everywhere else. It used to register the
    PowerShell form unconditionally, which on macOS or Linux wrote a command the
    machine could not run into the user's settings.json.

    `os_name` is injected for the same reason `install` takes it: the POSIX
    answer has to be testable on Windows and the Windows answer on the Linux CI
    runner, or half the behaviour is only checked by whoever happens to run the
    suite on the right box."""
    rel, template = _shim_for(os_name)
    shim = (_repo_root(repo) / rel).as_posix()
    return template.format(shim=shim)


def _backup(path: Path) -> Path | None:
    """Copy beside the original, never over a previous backup — the second run
    of a broken installer must not overwrite the last good copy."""
    for n in range(1, 100):
        candidate = path.with_name(path.name + f".bak-{n}")
        if not candidate.exists():
            shutil.copy2(path, candidate)
            return candidate
    return None


def install(settings_path, command: str | None = None,
            *, dry_run: bool = False, os_name: str | None = None) -> dict:
    """Add the Stop hook to *settings_path* unless it is already there.

    Returns `{"ok": bool, "reason": str, ...}` where `reason` is a stable id:
    `installed`, `already_registered`, `malformed`, `missing`, or `bad_hooks`
    -- a `hooks` that is not an object, a Stop stage that is not a list, or a
    matcher group whose own `hooks` is present and not a list.
    A successful result on a platform without shims also carries
    `platform_warning` — see the module docstring.

    `os_name` is injected so the non-Windows answer is testable ON Windows, and
    the Windows answer testable on the Linux CI runner. A platform caveat that
    only one of the two runners can reach is a caveat nobody checks. It selects
    the DEFAULT COMMAND too, so an explicit `command` still overrides both.
    """
    result = _install(settings_path, command, dry_run=dry_run, os_name=os_name)
    warning = platform_warning(os_name)
    if warning and result.get("ok"):
        # Failure results are left alone: a malformed settings.json is the whole
        # answer, and a second sentence about platforms buries it.
        return dict(result, platform_warning=warning)
    return result


def _install(settings_path, command: str | None, *, dry_run: bool,
             os_name: str | None = None) -> dict:
    path = Path(settings_path)
    os_name = os.name if os_name is None else os_name
    command = command or default_command(os_name=os_name)

    if not path.is_file():
        return {"ok": False, "reason": "missing", "path": str(path)}

    original = path.read_text(encoding="utf-8")
    try:
        settings = json.loads(original)
    except (json.JSONDecodeError, ValueError):
        # Deliberately no repair attempt. See the module docstring.
        return {"ok": False, "reason": "malformed", "path": str(path)}
    if not isinstance(settings, dict):
        return {"ok": False, "reason": "malformed", "path": str(path)}

    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        # Coercing this would silently discard whatever is there.
        return {"ok": False, "reason": "bad_hooks", "path": str(path)}

    stage = hooks.get(STAGE, [])
    if not isinstance(stage, list):
        return {"ok": False, "reason": "bad_hooks", "path": str(path)}

    registered = []
    for group in stage:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks", [])
        if not isinstance(entries, list):
            # `group.get("hooks") or []` iterated whatever was there: an int or
            # a bool raised TypeError out of `install-hook`, and null, a string
            # or an object was passed over and the hook appended beside a group
            # Claude Code cannot read. Refused like a Stop stage that is not a
            # list: an answer about what this file holds would be a guess.
            return {"ok": False, "reason": "bad_hooks", "path": str(path)}
        # Only a `command` hook runs a command. A `prompt` hook carrying the
        # same text under `command` runs nothing, and matching it reported the
        # Stop shim registered while nothing would ever execute it.
        registered += [h.get("command") for h in entries
                       if isinstance(h, dict) and h.get("type") == "command"]
    if command in registered:
        return {"ok": True, "reason": "already_registered", "path": str(path),
                "matched": "command"}
    # Then the same question asked of the SHIM rather than the string. The
    # module docstring's first line is "Idempotent registration of the Stop
    # hook" and the CLI help says "idempotent", and an exact string comparison
    # delivers that only for as long as the default command never changes its
    # spelling. It changed: quoting the path (so a checkout under `C:/My Repo`
    # registers one argument instead of three) made every settings.json written
    # by an earlier release stop matching, so the next `install-hook` APPENDED a
    # second Stop entry instead of reporting `already_registered` -- two
    # PowerShell processes and two `hive hook stop` children at the end of every
    # agent turn, in every project, and nothing errors so nobody finds out.
    #
    # Comparing the script a command EXECUTES, rather than its string, is also
    # the right rule on its own merits: two commands that run the same Stop shim
    # run it twice. It covers the spellings `_executed_script` parses. A wrapper
    # it does not parse (`-Command`, `sh -c`, `cmd /c`, `env`) is matched by its
    # exact string only, so a respelled one can still be registered twice -- a
    # visible duplicate, the failure this errs toward, and never a hook reported
    # registered that does not run.
    #
    # The target compared is the one the command BEING INSTALLED runs, not this
    # checkout's shim. An explicit `command` overrides the default -- a worktree
    # registers the MAIN checkout's shim, because its own copy disappears with
    # the worktree -- and comparing against this checkout answered that
    # registration `already_registered` as soon as this checkout's was present,
    # writing nothing. A command that runs no recognisable shim is matched by
    # its exact string above and nothing else.
    wanted = _target(command, os_name)
    if wanted and any(_same_target(wanted, _target(existing, os_name), os_name)
                      for existing in registered):
        return {"ok": True, "reason": "already_registered", "path": str(path),
                "matched": "shim"}

    # The nested matcher-group shape every existing stage in this file uses.
    # No `matcher` key: Stop fires on every occurrence and does not accept one.
    stage = list(stage) + [{"hooks": [{"type": "command", "command": command}]}]
    hooks = dict(hooks)
    hooks[STAGE] = stage
    settings = dict(settings)
    settings["hooks"] = hooks

    if dry_run:
        return {"ok": True, "reason": "would_install", "path": str(path)}

    backup = _backup(path)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "reason": "installed", "path": str(path),
            "backup": str(backup) if backup else None}
