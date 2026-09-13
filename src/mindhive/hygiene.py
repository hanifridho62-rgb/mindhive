# src/mindhive/hygiene.py
"""Store hygiene: report per-namespace size/health, and prune junk safely.

Diagnostic-first by design. `scan`/`report` never mutate the store. `prune`
deletes only what an explicit target selects, refuses to touch a registered
namespace without `force`, and has no wildcard "delete everything" path.
"""
import os
import shutil
import stat

from . import paths

# A graph.json above this is flagged as bloat in the report (the live store had
# 116MB and 57MB blobs). Advisory only — flagging never deletes.
GRAPH_BLOB_WARN_BYTES = 25 * 1024 * 1024

_GRAPH_BLOBS = ("graph.json", "graph.html")


def _dir_size(path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024.0


# --- which registered project a directory IS, not how it is spelled ------------
#
# Registration is a NAME in autosync.json, deletion acts on a DIRECTORY, and the
# two drift apart in three ways this module has to see through:
#
# * `known_projects` normalizes every registry entry, so a project registered as
#   `My_Proj` is known as `my-proj` -- while its directory keeps `My_Proj`, the
#   name every read and write uses, because `namespace_name` does not normalize.
#   `prune(orphans=True)` compared the directory's name with the normalized set,
#   found no match, and deleted a registered project's ledger.
# * A case-insensitive volume (NTFS, APFS) makes `Fabio` and `fabio` one
#   directory.
# * A link (a symlink, or a Windows junction) is a second name for one directory
#   on any platform.
#
# So a directory counts as registered when its name is, when its normalized name
# is, or when it IS a registered namespace's directory by (device, inode) -- the
# one question only the filesystem can answer.


def _identity(path):
    """``(st_dev, st_ino)`` of *path*, links followed, or None when it cannot be
    read or the volume reports no inode (`st_ino` 0)."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def _registered_ids(known) -> dict:
    """``{directory identity: registered name}`` for every registered namespace
    whose directory exists."""
    ids = {}
    for name in sorted(known):
        ident = _identity(paths.project_dir(name))
        if ident is not None:
            ids.setdefault(ident, name)
    return ids


def _registered_as(name, known, ids):
    """The registered name the namespace directory *name* belongs to, or None."""
    if name in known:
        return name
    slug = paths.normalize(name)
    if slug in known:
        return slug
    ident = _identity(paths.project_dir(name))
    return ids.get(ident) if ident is not None else None


def _is_link(path) -> bool:
    """Whether *path* itself is a symlink or a Windows junction (not followed)."""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    reparse = getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    return (bool(reparse)
            and getattr(st, "st_reparse_tag", None) == stat.IO_REPARSE_TAG_MOUNT_POINT)


def registered(names) -> dict:
    """``{name: bool}``: whether each namespace directory belongs to a registered
    project, by the rules above. `hive projects` asks this too, so the listing
    and the prune cannot disagree about what is protected."""
    known = paths.known_projects()
    ids = _registered_ids(known)
    return {name: _registered_as(name, known, ids) is not None for name in names}


def unlisted_note(names) -> str:
    """One line naming the namespace-shaped directories no name reaches
    (`paths.unlisted_namespaces`), or "" when there are none.

    `repr`, because the difference is usually invisible otherwise: `abc.` beside
    `abc`, a trailing space, the hash of a quarantine slot."""
    names = list(names or [])
    if not names:
        return ""
    shown = ", ".join(repr(n) for n in names[:6])
    if len(names) > 6:
        shown += f", and {len(names) - 6} more"
    return (f"{len(names)} unlisted directory(ies) no project name reaches -- "
            f"never listed or pruned, inspect by hand: {shown}")


def scan() -> list:
    """Per-namespace health rows, largest first. Pure read — no mutation."""
    known = paths.known_projects()
    ids = _registered_ids(known)
    rows = []
    for name in paths.list_namespaces():
        d = paths.project_dir(name)
        graph = d / "graph.json"
        graph_bytes = graph.stat().st_size if graph.exists() else 0
        rows.append({
            "namespace": name,
            "bytes": _dir_size(d),
            "graph_bytes": graph_bytes,
            "registered": _registered_as(name, known, ids) is not None,
            "graph_oversized": graph_bytes >= GRAPH_BLOB_WARN_BYTES,
        })
    rows.sort(key=lambda r: r["bytes"], reverse=True)
    return rows


def report() -> str:
    rows = scan()
    note = unlisted_note(paths.unlisted_namespaces())
    if not rows:
        return note or "(no namespaces on disk yet)"
    lines = []
    total = 0
    for r in rows:
        total += r["bytes"]
        flags = []
        flags.append("registered" if r["registered"] else "UNREGISTERED")
        if r["graph_oversized"]:
            flags.append(f"graph {human_bytes(r['graph_bytes'])} OVERSIZED")
        lines.append(f"  {human_bytes(r['bytes']):>9}  {r['namespace']:32} "
                     f"[{', '.join(flags)}]")
    unregistered = sum(1 for r in rows if not r["registered"])
    lines.append(f"\n{len(rows)} namespace(s), {human_bytes(total)} total; "
                 f"{unregistered} unregistered.")
    lines.append("Prune with: hive gc --prune --orphans  |  "
                 "hive gc --prune --graph-blobs  |  hive gc --prune <name>")
    if note:
        lines.append(note)
    return "\n".join(lines)


def prune(target=None, orphans=False, graph_blobs=False, force=False) -> dict:
    """Delete what the selectors target. Returns a dict describing what was
    removed, or {"refused": reason} when a guard blocks the operation. Never
    deletes a registered namespace's data unless force=True, and never deletes
    through a link."""
    known = paths.known_projects()
    ids = _registered_ids(known)
    namespaces = paths.list_namespaces()

    # Confine an explicit target to a real namespace on disk. This single check
    # blocks four ways the raw target could escape the intended blast radius:
    #   - path traversal ("../../x") and absolute paths ("C:/Windows") — a join
    #     onto the store would otherwise resolve outside it and rmtree that;
    #   - non-namespace dirs sharing the store root ("downloads", "dist");
    #   - casing/format variants of a registered name ("Fabio" vs "fabio") that
    #     miss the normalized `known` guard yet hit the real dir on a
    #     case-insensitive filesystem.
    # `list_namespaces` holds canonical names only, so a directory no name
    # reaches (`abc.` beside `abc` on Windows, a quarantine slot) is no target.
    if target is not None:
        target = paths.normalize(target)
        if target not in namespaces:
            return {"refused": f"'{target}' is not a namespace on disk; "
                               "run `hive gc` to see the list"}

    # Bulk deletion (orphans, or graph-blobs across all namespaces) is only safe
    # when the registry is positively readable. An empty `known` set is
    # ambiguous: a MISSING or CORRUPT autosync.json also yields an empty set, and
    # then every namespace looks like an orphan and gets wiped. Refuse rather
    # than guess — the user can register a project, name an explicit target, or
    # pass --force to accept the risk.
    bulk = orphans or (graph_blobs and target is None)
    if bulk and not known and not force:
        return {"refused": "registry empty or unreadable — refusing bulk prune "
                           "(every namespace would look unregistered). Register a "
                           "project, name an explicit namespace, or use --force."}

    if orphans:
        targets = [n for n in namespaces if _registered_as(n, known, ids) is None]
    elif target:
        targets = [target]
    else:
        targets = []

    # Guard: never delete a registered namespace without an explicit override.
    protected = [n for n in targets
                 if not force and _registered_as(n, known, ids) is not None]
    if protected:
        return {"refused": f"registered namespace(s) {protected}; pass --force to override"}

    # A link is refused before ANYTHING is deleted, whatever `force` says.
    # `shutil.rmtree` raises on a symlink or a junction, so a bulk prune deleted
    # every orphan sorted before one and then stopped on a traceback; and a
    # graph-blob prune through one unlinks a file in whatever directory it points
    # at, inside the store or not.
    scope = (targets or namespaces) if graph_blobs else targets
    links = [n for n in scope if _is_link(paths.project_dir(n))]
    if links:
        return {"refused": f"namespace(s) {links} are links (symlink or junction), "
                           "not directories; remove the link itself by hand"}

    if graph_blobs:
        # Delete only the graph.json/graph.html blobs, leaving the event log and
        # projections intact. Scope to the selected namespaces, or all when none
        # were named — the in-loop registered-skip is the last line of defense
        # for the scope-all case (the bulk guard above already refused when the
        # registry was unreadable).
        removed = []
        for name in scope:
            if not force and _registered_as(name, known, ids) is not None:
                continue
            d = paths.project_dir(name)
            for blob in _GRAPH_BLOBS:
                p = d / blob
                if p.exists():
                    freed = p.stat().st_size
                    p.unlink()
                    removed.append({"namespace": name, "blob": blob, "bytes": freed})
        return {"removed_blobs": removed}

    removed = []
    for name in targets:
        d = paths.project_dir(name)
        if d.is_dir():
            freed = _dir_size(d)
            shutil.rmtree(d)
            removed.append({"namespace": name, "bytes": freed})
    return {"removed_namespaces": removed}
