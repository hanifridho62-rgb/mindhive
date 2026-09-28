# SPDX-License-Identifier: Apache-2.0
"""Is the plugin Claude Code loads the plugin this checkout holds? (Bug 8)

Claude Code does not read a plugin from its repository. It copies the plugin
into `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` when it is
installed or updated, records that copy in `plugins/installed_plugins.json`,
and loads the copy in every session. When the checkout moves on and the copy
does not, sessions run instructions the repository no longer contains -- and
nothing says so.

A version compare cannot see the common case. Claude Code names the cache
directory after the manifest version, so a checkout that changed its skills
without bumping `.claude-plugin/plugin.json`, and a copy taken before the
change, both claim the same version. `cache_status` therefore compares a
content hash of the plugin's components -- the manifest plus the `skills`,
`commands`, `agents`, `hooks/hooks.json` and `.mcp.json` Claude Code loads --
as well as the version, and names the files that differ.

Repair goes through Claude Code's own `claude plugin update`, never by writing
into the cache: the registry and the copy are Claude Code's to keep in step.
`hive plugins cache-check --hook` is what the session-start hooks run. It is
fail-open -- any fault prints nothing and exits 0 -- and its automatic sync is
opt-in through `MINDHIVE_PLUGIN_AUTOSYNC`, because the hook fires in every
session on the machine, and a default that rewrites the user's plugin install
from whichever project happens to be open is not one to switch on silently.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from . import plugins

__all__ = ["PLUGIN_COMPONENTS", "AUTOSYNC_ENV", "SYNC_TIMEOUT_S", "DIFF_LIST_CAP",
           "DRIFT_STATES", "plugin_tree_digest", "plugin_tree_hash",
           "cache_status", "session_notice", "sync_command", "sync_cache",
           "autosync_enabled"]

# What Claude Code loads from a plugin: its manifest and the default component
# locations. Everything else in the checkout (src/, tests/, the MindHive hook
# scripts under hooks/) is copied into the cache too, but no session reads it.
PLUGIN_COMPONENTS = (".claude-plugin/plugin.json", "skills", "commands", "agents",
                     "hooks/hooks.json", ".mcp.json")

AUTOSYNC_ENV = "MINDHIVE_PLUGIN_AUTOSYNC"

# Inside the 60 s Claude Code gives a hook. At 45 s this module ends the
# update's whole process tree itself (`_run_bounded`), so an opted-in sync at
# session start comes back with its own "TimeoutExpired" line instead of holding
# the hook until Claude Code's limit kills it. Pinned in tests/test_plugin_cache.py.
SYNC_TIMEOUT_S = 45.0

# The file lists in a status are for a person to read; the counts stay exact.
DIFF_LIST_CAP = 20

# The states a sync can repair, and the only ones a session is told about.
DRIFT_STATES = ("diverged", "missing_on_disk")

_TRUTHY = {"1", "true", "yes", "on"}
_SKIP_DIRS = {"__pycache__"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}
_UNREADABLE = "unreadable"
_DIFF_FIELDS = ("only_in_repo", "only_in_cache", "changed")


def _component_files(root: Path):
    for component in PLUGIN_COMPONENTS:
        path = root / component
        if path.is_file():
            yield path
        elif path.is_dir():
            for f in sorted(path.rglob("*")):
                rel = f.relative_to(root)
                if (f.is_file() and not _SKIP_DIRS.intersection(rel.parts)
                        and f.suffix not in _SKIP_SUFFIXES):
                    yield f


def plugin_tree_digest(root) -> dict[str, str]:
    """`{relative/posix/path: sha256}` for every component file under `root`.

    Line endings are normalised to LF before hashing: a checkout on Windows and
    a copy of it are the same instructions whichever way git wrote the newlines.
    A file that cannot be read is recorded as unreadable rather than dropped, so
    it shows up as a difference instead of silently leaving the comparison."""
    root = Path(root)
    if not root.is_dir():
        return {}
    digest = {}
    for f in _component_files(root):
        rel = f.relative_to(root).as_posix()
        try:
            data = f.read_bytes()
        except OSError:
            digest[rel] = _UNREADABLE
            continue
        digest[rel] = hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()
    return digest


def _tree_hash(digest: dict[str, str]) -> str:
    if not digest:
        return ""
    body = "".join(f"{rel}\0{digest[rel]}\n" for rel in sorted(digest))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def plugin_tree_hash(root) -> str:
    """One sha256 over the sorted `(path, file digest)` pairs; "" for no tree."""
    return _tree_hash(plugin_tree_digest(root))


def _manifest(root: Path) -> dict:
    data = plugins._read_json(root / ".claude-plugin" / "plugin.json")
    return data if isinstance(data, dict) else {}


def _marketplace_paths(claude_dir) -> dict[str, str]:
    data = plugins._read_json(
        plugins._claude_dir(claude_dir) / "plugins" / "known_marketplaces.json")
    out = {}
    if isinstance(data, dict):
        for name, entry in data.items():
            source = entry.get("source") if isinstance(entry, dict) else None
            path = source.get("path") if isinstance(source, dict) else None
            if isinstance(path, str) and path:
                out[name] = path
    return out


def _same_path(a, b) -> bool:
    try:
        return (os.path.normcase(str(Path(a).resolve()))
                == os.path.normcase(str(Path(b).resolve())))
    except (OSError, ValueError):
        return False


def _pick_install(name: str, root: Path, claude_dir):
    """The registry entry Claude Code loads for plugin `name`.

    The install whose marketplace is this very checkout wins; otherwise the
    newest version of the plugin under any marketplace."""
    candidates = []
    for key, entries in plugins._read_registry(claude_dir).items():
        plugin, _, market = str(key).rpartition("@")
        if not plugin:
            plugin, market = market, ""
        if plugin != name or not isinstance(entries, list):
            continue
        entry = plugins._newest_entry(entries)
        if entry is not None:
            candidates.append((str(key), market, entry))
    if not candidates:
        return None
    markets = _marketplace_paths(claude_dir)
    for candidate in candidates:
        market = candidate[1]
        if market in markets and _same_path(markets[market], root):
            return candidate
    return max(candidates,
               key=lambda c: plugins._semver_key(str(c[2].get("version", ""))))


def _diff(repo: dict, cache: dict) -> dict[str, list[str]]:
    shared = set(repo) & set(cache)
    return {"only_in_repo": sorted(set(repo) - set(cache)),
            "only_in_cache": sorted(set(cache) - set(repo)),
            "changed": sorted(r for r in shared if repo[r] != cache[r])}


def cache_status(repo_root=None, claude_dir=None) -> dict:
    """Compare the plugin in `repo_root` (default: this checkout) with the copy
    Claude Code's registry under `claude_dir` (default: ~/.claude) points at.

    `state` is one of `in_sync`, `diverged` (a version or a component file
    differs), `missing_on_disk` (the registry names a directory that is gone),
    `not_installed` (the registry has no such plugin) or `no_manifest` (the
    checkout is not a plugin). Nothing here raises for an absent or corrupt
    file; that is a state, reported with its `reason`. `repo_root` and
    `claude_dir` record what was compared, so `sync_cache` can compare the
    same two again."""
    root = Path(repo_root) if repo_root else plugins.checkout_root()
    manifest = _manifest(root)
    name = manifest.get("name") if isinstance(manifest.get("name"), str) else ""
    version = manifest.get("version")
    status = {"state": "no_manifest", "plugin": name, "key": "", "scope": "",
              "repo_root": str(root),
              "claude_dir": str(plugins._claude_dir(claude_dir)),
              "repo_version": version if isinstance(version, str) else "",
              "cache_version": "", "repo_hash": "", "cache_hash": "",
              "install_path": "", "only_in_repo": [], "only_in_cache": [],
              "changed": [], "diff_counts": {f: 0 for f in _DIFF_FIELDS},
              "reason": ""}
    if not name:
        status["reason"] = ("no plugin manifest with a name at "
                            f"{root / '.claude-plugin' / 'plugin.json'}")
        return status
    repo_digest = plugin_tree_digest(root)
    status["repo_hash"] = _tree_hash(repo_digest)
    picked = _pick_install(name, root, claude_dir)
    if picked is None:
        status.update(state="not_installed",
                      reason=f"{name} is not in Claude Code's plugin registry")
        return status
    key, _market, entry = picked
    raw_path = str(entry.get("installPath") or "")
    status.update(key=key, scope=str(entry.get("scope") or "user"),
                  cache_version=str(entry.get("version") or ""),
                  install_path=raw_path)
    if not raw_path or not Path(raw_path).is_dir():
        status.update(state="missing_on_disk",
                      reason=f"the registry points {key} at {raw_path or '(nothing)'}, "
                             "which is not a directory")
        return status
    cache_digest = plugin_tree_digest(Path(raw_path))
    status["cache_hash"] = _tree_hash(cache_digest)
    diff = _diff(repo_digest, cache_digest)
    for field in _DIFF_FIELDS:
        status["diff_counts"][field] = len(diff[field])
        status[field] = diff[field][:DIFF_LIST_CAP]
    drifted = (any(diff.values())
               or status["repo_version"] != status["cache_version"])
    status["state"] = "diverged" if drifted else "in_sync"
    if drifted:
        status["reason"] = ("the copy Claude Code loads differs from this checkout"
                            if any(diff.values()) else
                            "the registry records another version than the manifest")
    return status


def _fix(status: dict) -> str:
    key = status.get("key") or status.get("plugin") or "<plugin>@<marketplace>"
    return (f"sync: claude plugin update {key} (restart Claude Code to load it), "
            f"or set {AUTOSYNC_ENV}=1 to sync at session start")


def session_notice(status: dict) -> str:
    """One line for the session-start context when the cache drifted, else ""."""
    state = status.get("state")
    if state not in DRIFT_STATES:
        return ""
    key = status.get("key") or status.get("plugin") or "plugin"
    if state == "missing_on_disk":
        return (f"[mindhive] plugin cache missing: Claude Code's registry points {key} "
                f"at {status.get('install_path') or '(nothing)'}, which does not "
                f"exist -- {_fix(status)}")
    counts = status.get("diff_counts") or {}
    parts = []
    for label, field in (("only in the cache", "only_in_cache"),
                         ("only in this checkout", "only_in_repo"),
                         ("changed", "changed")):
        n = int(counts.get(field) or 0)
        if n:
            shown = list(status.get(field) or [])[:3]
            more = f" +{n - len(shown)} more" if n > len(shown) else ""
            parts.append(f"{n} {label} ({', '.join(shown)}{more})")
    drift = "; ".join(parts) or "the recorded version differs"
    return (f"[mindhive] plugin cache out of date: this checkout has "
            f"{status.get('plugin') or key} {status.get('repo_version') or '?'} "
            f"(tree {str(status.get('repo_hash') or '')[:8] or '?'}), Claude Code "
            f"loads {status.get('cache_version') or '?'} "
            f"(tree {str(status.get('cache_hash') or '')[:8] or '?'}) from "
            f"{status.get('install_path') or '?'} -- {drift} -- {_fix(status)}")


def sync_command(status: dict) -> list[str]:
    """Claude Code's own update for the install `status` names."""
    return ["claude", "plugin", "update", str(status.get("key") or ""),
            "--scope", str(status.get("scope") or "user")]


def _run_bounded(argv, *, timeout, **popen_kw):
    """`subprocess.run`'s contract for the one CLI call this module makes, with
    the timeout holding for the call's whole process TREE.

    `subprocess.run` kills only its direct child on a timeout and then, on
    Windows, drains the pipes with no bound of its own. `claude` resolves to
    `claude.CMD` on this machine, whose body runs the CLI as a grandchild of
    cmd.exe holding the inherited pipes -- so a 2 s timeout came back after
    the grandchild's own 30 s (measured; tests/test_plugin_cache.py). Here the
    child heads its own process group and is held (a Job Object on Windows),
    `orchestrator.driver.terminate_process_tree` takes the tree on the timeout,
    and `driver.reap` bounds what is left of the wait. The environment is
    inherited, not scrubbed: this is the CLI's own plugin flow, no session and
    no model (a TOOL spawn in tests/test_advice_authorship.py)."""
    from .orchestrator import driver        # lazy: only a real spawn pays for it
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **popen_kw,
                            **driver.isolation_kwargs())
    driver.hold_tree(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:               # the timeout, or an interrupt
            driver.terminate_process_tree(proc)
            driver.reap(proc, 0.0)
            raise
    finally:
        driver.release_tree(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _confirmed(status: dict, recheck) -> tuple[bool, str]:
    """``(ok, reason)`` for an update that exited 0, read off the copy itself."""
    def again():
        return cache_status(repo_root=status.get("repo_root") or None,
                            claude_dir=status.get("claude_dir") or None)
    try:
        after = (recheck or again)()
    except Exception as exc:                            # noqa: BLE001 -- never raises
        return False, ("claude plugin update exited 0, but the copy could not be "
                       f"compared again ({type(exc).__name__}: {exc})")
    after = after if isinstance(after, dict) else {}
    if after.get("state") == "in_sync":
        return True, ""
    why = f" ({after['reason']})" if after.get("reason") else ""
    return False, ("claude plugin update exited 0, but the copy Claude Code loads "
                   f"is still {after.get('state') or 'unknown'}{why}")


def sync_cache(status: dict, *, run=None, which=None,
               timeout_s: float = SYNC_TIMEOUT_S, recheck=None) -> dict:
    """Run `claude plugin update <key> --scope <scope>` when the cache drifted.

    Returns `{ok, argv, exit, output, reason}` and never raises: a missing CLI,
    a timeout, a spawn error or a non-zero exit is a reported failure. A status
    with nothing to repair spawns nothing -- `ok` is True only for `in_sync`.

    Exit 0 is not the claim either. `ok` is True only when *recheck* -- by
    default `cache_status` over the same checkout and config dir *status* was
    taken from -- answers `in_sync`: the update moves a plugin to its latest
    VERSION, and the drift this module exists for is often same-version."""
    state = status.get("state")
    if state not in DRIFT_STATES:
        ok = state == "in_sync"
        return {"ok": ok, "argv": [], "exit": None, "output": "",
                "reason": ("in sync; nothing to do" if ok else
                           f"nothing to sync: {state} ({status.get('reason') or ''})")}
    argv = sync_command(status)
    exe = (which or shutil.which)("claude")
    if not exe:
        return {"ok": False, "argv": argv, "exit": None, "output": "",
                "reason": "no `claude` executable on PATH -- install Claude Code "
                          f"or run `{' '.join(argv)}` by hand"}
    argv = [exe] + argv[1:]
    spawn = run or _run_bounded
    try:
        proc = spawn(argv, text=True, encoding="utf-8", errors="replace",
                     timeout=timeout_s, shell=False)
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        return {"ok": False, "argv": argv, "exit": None, "output": "",
                "reason": f"{type(exc).__name__}: {exc}"}
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        ok, reason = _confirmed(status, recheck)
    else:
        ok, reason = False, f"claude plugin update exited {proc.returncode}"
    return {"ok": ok, "argv": argv, "exit": proc.returncode, "output": output[-2000:],
            "reason": reason}


def autosync_enabled(env=None) -> bool:
    """True only when `MINDHIVE_PLUGIN_AUTOSYNC` is set to a yes-word."""
    source = os.environ if env is None else env
    return str(source.get(AUTOSYNC_ENV, "")).strip().lower() in _TRUTHY
