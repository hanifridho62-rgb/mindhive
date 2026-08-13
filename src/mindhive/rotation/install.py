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

**Rotation is Windows-only, and this is where that becomes visible.** The
command registered here runs `hooks/stop.ps1` through `powershell`, and every
shim in `hooks/` is PowerShell — nine `.ps1` files and no POSIX equivalent. On
macOS or Linux the registration itself succeeds (it is a JSON edit, and JSON does
not care), and then the hook can never fire, so the session never rotates and
nothing ever says why. `install` reports that as `platform_warning` rather than
leaving the user to discover it as an absence. It warns rather than refuses:
editing a settings.json that will be read on another machine is legitimate, and
the honest answer to "this may not do what you want" is to say so.
"""
import json
import os
import shutil
from pathlib import Path

STAGE = "Stop"

# `os.name` of the only platform whose shims exist. Compared against, never
# assumed: the check has to be readable on the machine that fails it.
SHIM_OS = "nt"

_PLATFORM_WARNING = (
    "registered, but rotation cannot fire on this platform: every hook shim in "
    "hooks/ is PowerShell (.ps1) and this is os.name={os_name!r}, not "
    f"{SHIM_OS!r}. The Stop hook will be invoked and do nothing. Rotation is "
    "Windows-only until a POSIX shim exists."
)


def platform_warning(os_name: str | None = None) -> str:
    """The caveat for this platform, or "" when the shims can actually run."""
    os_name = os.name if os_name is None else os_name
    if os_name == SHIM_OS:
        return ""
    return _PLATFORM_WARNING.format(os_name=os_name)


def default_command(python: str | None = None, repo: str | None = None) -> str:
    """The command string registered for the hook.

    Mirrors the shape the six existing stages already use in this file — a
    PowerShell shim under the repo's hooks/ directory, invoked with -NoProfile."""
    root = Path(repo) if repo else Path(__file__).resolve().parents[3]
    shim = (root / "hooks" / "stop.ps1").as_posix()
    return f"powershell -NoProfile -File {shim}"


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
    `installed`, `already_registered`, `malformed`, `missing`, or `bad_hooks`.
    A successful result on a platform without shims also carries
    `platform_warning` — see the module docstring.

    `os_name` is injected so the non-Windows answer is testable ON Windows, and
    the Windows answer testable on the Linux CI runner. A platform caveat that
    only one of the two runners can reach is a caveat nobody checks.
    """
    result = _install(settings_path, command, dry_run=dry_run)
    warning = platform_warning(os_name)
    if warning and result.get("ok"):
        # Failure results are left alone: a malformed settings.json is the whole
        # answer, and a second sentence about platforms buries it.
        return dict(result, platform_warning=warning)
    return result


def _install(settings_path, command: str | None, *, dry_run: bool) -> dict:
    path = Path(settings_path)
    command = command or default_command()

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

    if any(command == h.get("command")
           for group in stage if isinstance(group, dict)
           for h in (group.get("hooks") or []) if isinstance(h, dict)):
        return {"ok": True, "reason": "already_registered", "path": str(path)}

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
