# SPDX-License-Identifier: Apache-2.0
"""Context usage for work sessions (blueprint BAB 5.3.3 and 5.5): the verbose
``[USAGE: …]`` report and the figures the Oversee context bar draws.

The bar reads the native MEMORY.md `sync` writes for a namespace: its
LF-normalised bytes (`context.memory_pressure`) at
`worksessions.CHARS_PER_TOKEN` characters per token against the 200 000-token
window, with `models.SAFE_CONTEXT_BUDGET_TOKENS` (160 000, MEB §2.5) as the
safe line.

Finding that file is the delicate part. It sits at
``~/.claude/projects/<encode_folder(folder)>/memory/MEMORY.md``, keyed by the
FOLDER sync ran in, and the encoding is lossy (``:``, ``\\``, ``/``, ``_`` and a
literal ``-`` all become ``-``), so a folder name cannot be decoded back into a
namespace. `memory_path_for_project` therefore asks the source every sync
writer itself uses first -- the autosync registry's ``project -> folder`` row,
encoded exactly as `sync.native_memory_path` encodes it. Only an unregistered
namespace falls back to the encoded names, and there a candidate is refused
when a LONGER known namespace also ends it (``c-users-x-web-app`` belongs to
``web-app``, not to ``app``), when it is the registered folder of another
project, or when it is a linked worktree's folder (``…-claude-worktrees-v1``
belongs to its main repo's namespace, never to ``v1`` -- and no worktree is ever
a known namespace, so the longer-namespace rule cannot catch it); among the
candidates that survive -- folders that differ only above the basename, so all
this namespace's -- the most recently written digest wins. A namespace with no readable digest reports
``measured: False``, and the bar draws nothing: "we could not measure" must not
read as "0 % used".
"""
from __future__ import annotations

from pathlib import Path

from . import worksessions as ws

# A linked worktree's folder, encoded and normalised (``…\.claude\worktrees\b``
# -> ``…-claude-worktrees-b``). `paths.detect_project` collapses a worktree onto
# its main repo, so such a folder is never the namespace its basename spells.
_WORKTREE_SEGMENT = "-claude-worktrees-"


def make_usage_report_tag(tokens_used: int, cap: int, trim_lines: int = 0,
                          trim_bytes: int = 0) -> str:
    """The verbose BAB 5.3.3 form, verbatim: ``[USAGE: 48,250 tokens / 100,000
    cap | Occupancy: 48.25% | Trim: 14 lines / 1,280 bytes dropped from digest
    tail]``; ``Trim: none`` when nothing was dropped."""
    pct = ws.occupancy_pct(tokens_used, cap)
    trim = (f"{trim_lines} lines / {trim_bytes:,} bytes dropped from digest tail"
            if trim_lines or trim_bytes else "none")
    return (f"[USAGE: {tokens_used:,} tokens / {cap:,} cap | "
            f"Occupancy: {pct:.2f}% | Trim: {trim}]")


def usage_tag_from_pressure(tokens_used: int, cap: int, pressure: dict) -> str:
    """The verbose tag from a `context.memory_pressure` dict: the line overflow
    (``over``) is what the loader drops, ``over_bytes`` the byte overflow; a
    digest that is not truncated reports ``Trim: none``."""
    if not pressure.get("truncated"):
        return make_usage_report_tag(tokens_used, cap)
    return make_usage_report_tag(tokens_used, cap,
                                 trim_lines=int(pressure.get("over") or 0),
                                 trim_bytes=int(pressure.get("over_bytes") or 0))


def memory_path_index(home=None) -> list:
    """``[(normalised folder name, MEMORY.md path, mtime)]`` for every native
    digest under ``~/.claude/projects``. One directory walk, so a HIVE MIND
    request that asks about sixty namespaces pays for it once."""
    from . import paths
    root = (Path(home) if home is not None else Path.home()) / ".claude" / "projects"
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return []
    out = []
    for d in dirs:
        cand = d / "memory" / "MEMORY.md"
        try:
            mtime = cand.stat().st_mtime
        except OSError:
            continue
        out.append((paths.normalize(d.name), cand, mtime))
    return out


def memory_path_for_project(project: str | None = None, *, home=None, index=None,
                            registry=None, namespaces=None):
    """The native MEMORY.md `sync` writes for *project*, or None (see the module
    docstring for the order). *index*, *registry* (``autosync.load_registry()``
    rows) and *namespaces* (``paths.event_namespaces()``) may be supplied so a
    caller resolving many namespaces reads each source once."""
    from . import autosync, paths, sync
    name = paths.normalize(project) if project else paths.detect_project()
    rows = [r for r in (autosync.load_registry() if registry is None else registry)
            if isinstance(r, dict) and r.get("folder")]
    for row in rows:
        if row.get("project") == name:
            p = sync.native_memory_path(str(row["folder"]), home=home)
            return p if p.is_file() else None
    claimed = {paths.normalize(sync.encode_folder(str(r["folder"]))) for r in rows}
    known = paths.event_namespaces() if namespaces is None else namespaces
    longer = [n for n in known if n != name and n.endswith("-" + name)]
    best, best_mtime = None, -1.0
    for tail, cand, mtime in (memory_path_index(home) if index is None else index):
        if not (tail == name or tail.endswith("-" + name)):
            continue
        if any(tail == n or tail.endswith("-" + n) for n in longer):
            continue                      # a longer namespace's folder, not ours
        if tail in claimed or _WORKTREE_SEGMENT in tail:
            continue                      # another project's registered folder, or a
                                          # linked worktree (its repo's namespace)
        if mtime > best_mtime:
            best, best_mtime = cand, mtime
    return best


def usage_for_project(project: str | None = None, *, home=None, path=None, index=None,
                      registry=None, namespaces=None,
                      budget: int = ws.CONTEXT_BUDGET_TOKENS) -> dict:
    """The context-occupancy figures the Oversee bar draws: the digest's
    LF-normalised bytes, tokens at four characters each, occupancy against
    *budget* (200 000), and the MEB safe line. ``measured`` is True only when a
    digest was found and read; otherwise every figure is zero."""
    from . import context, models
    p = (Path(path) if path is not None
         else memory_path_for_project(project, home=home, index=index,
                                      registry=registry, namespaces=namespaces))
    out = {"context_budget_tokens": budget,
           "safe_budget_tokens": models.SAFE_CONTEXT_BUDGET_TOKENS,
           "bytes": 0, "tokens_est": 0, "occupancy_pct": 0.0, "lines": 0,
           "truncated": False, "measured": False, "path": str(p) if p is not None else ""}
    if p is None:
        return out
    pressure = context.memory_pressure(p, project=project)
    if not pressure.get("readable"):
        return out
    tokens_est = int(pressure["bytes"]) // ws.CHARS_PER_TOKEN
    out.update(bytes=int(pressure["bytes"]), tokens_est=tokens_est,
               occupancy_pct=round(tokens_est / max(1, budget) * 100, 2),
               lines=int(pressure["lines"]), truncated=bool(pressure["truncated"]),
               measured=True)
    return out


__all__ = ["make_usage_report_tag", "usage_tag_from_pressure", "memory_path_index",
           "memory_path_for_project", "usage_for_project"]
