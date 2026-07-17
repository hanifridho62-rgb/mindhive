# src/mindhive/hygiene.py
"""Store hygiene: report per-namespace size/health, and prune junk safely.

Diagnostic-first by design. `scan`/`report` never mutate the store. `prune`
deletes only what an explicit target selects, refuses to touch a registered
namespace without `force`, and has no wildcard "delete everything" path.
"""
import shutil

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


def scan() -> list:
    """Per-namespace health rows, largest first. Pure read — no mutation."""
    known = paths.known_projects()
    home = paths.store_home()
    rows = []
    for name in paths.list_namespaces():
        d = home / name
        graph = d / "graph.json"
        graph_bytes = graph.stat().st_size if graph.exists() else 0
        rows.append({
            "namespace": name,
            "bytes": _dir_size(d),
            "graph_bytes": graph_bytes,
            "registered": name in known,
            "graph_oversized": graph_bytes >= GRAPH_BLOB_WARN_BYTES,
        })
    rows.sort(key=lambda r: r["bytes"], reverse=True)
    return rows


def report() -> str:
    rows = scan()
    if not rows:
        return "(no namespaces on disk yet)"
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
    return "\n".join(lines)


def prune(target=None, orphans=False, graph_blobs=False, force=False) -> dict:
    """Delete what the selectors target. Returns a dict describing what was
    removed, or {"refused": reason} when a guard blocks the operation. Never
    deletes a registered namespace's data unless force=True."""
    known = paths.known_projects()
    home = paths.store_home()
    namespaces = paths.list_namespaces()

    # Confine an explicit target to a real namespace on disk. This single check
    # blocks four ways the raw target could escape the intended blast radius:
    #   - path traversal ("../../x") and absolute paths ("C:/Windows") — `home /
    #     target` would otherwise resolve outside the store and rmtree it;
    #   - non-namespace dirs sharing the store root ("downloads", "dist");
    #   - casing/format variants of a registered name ("Fabio" vs "fabio") that
    #     miss the normalized `known` guard yet hit the real dir on a
    #     case-insensitive filesystem.
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
        targets = [n for n in namespaces if n not in known]
    elif target:
        targets = [target]
    else:
        targets = []

    # Guard: never delete a registered namespace without an explicit override.
    protected = [n for n in targets if n in known and not force]
    if protected:
        return {"refused": f"registered namespace(s) {protected}; pass --force to override"}

    if graph_blobs:
        # Delete only the graph.json/graph.html blobs, leaving the event log and
        # projections intact. Scope to the selected namespaces, or all when none
        # were named — the in-loop registered-skip is the last line of defense
        # for the scope-all case (the bulk guard above already refused when the
        # registry was unreadable).
        scope = targets or namespaces
        removed = []
        for name in scope:
            if name in known and not force:
                continue
            for blob in _GRAPH_BLOBS:
                p = home / name / blob
                if p.exists():
                    freed = p.stat().st_size
                    p.unlink()
                    removed.append({"namespace": name, "blob": blob, "bytes": freed})
        return {"removed_blobs": removed}

    removed = []
    for name in targets:
        d = home / name
        if d.is_dir():
            freed = _dir_size(d)
            shutil.rmtree(d)
            removed.append({"namespace": name, "bytes": freed})
    return {"removed_namespaces": removed}
