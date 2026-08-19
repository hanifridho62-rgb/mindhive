# src/mindhive/autosync.py
# Hook-free automation: harvest transcripts -> rebuild projections -> sync native MEMORY.md.
# Driven by a Windows Scheduled Task (see hooks/autosync.ps1).
import json
from . import paths, harvest, projector, graph, sync


def _registry_path():
    return paths.store_home() / "autosync.json"


def load_registry() -> list:
    p = _registry_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def register(project, transcripts, folder) -> list:
    reg = load_registry()
    reg = [e for e in reg if e.get("project") != project]
    reg.append({"project": project, "transcripts": transcripts, "folder": folder})
    paths.store_home().mkdir(parents=True, exist_ok=True)
    _registry_path().write_text(
        json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    return reg


def _refresh_mirror(project) -> bool:
    """Bring the Postgres read mirror level with the ledger. Best-effort.

    THIS IS WHAT MAKES THE MIRROR FOLLOW THE LEDGER. Nothing on the append path
    writes to Postgres — a single-row seam cannot, because the module on that
    path holds a role-level read-only credential — so the mirror advances here,
    on a schedule, reloading only the segments whose bytes moved.

    Three properties, in the order of what they prevent:

    1. **Costs one `os.environ.get` on a machine with no Postgres.** The admin
       DSN is absent from a normal environment, which is the common case and must
       stay free.
    2. **Cannot fail a sync.** A projection that was written and a MEMORY.md that
       was refreshed are not undone by a mirror that could not be reached; the
       import is inside the guard so a missing driver is a False, not a crash.
    3. **Silent is fine, invisible is not.** The return value rides the per-project
       result row, and a mirror that did not advance still reports itself through
       `mirror_health`'s `stale` — which now carries the repair command."""
    import os
    if not (os.environ.get("MINDHIVE_PG_DSN_ADMIN") or "").strip():
        return False
    try:
        from . import pgmirror
        out = pgmirror.refresh_changed(project)
        return bool(out.get("verified", {}).get("ok"))
    except Exception:
        return False


def run_all(now=None) -> list:
    """Auto-discover and capture EVERY project from Claude transcripts, then
    rebuild projections + graph for each project that changed this run. Write the
    native MEMORY.md only for REGISTERED projects (the registry is the native-sync
    allow-list; it is no longer required for capture).

    ``skip_captured=True`` keeps capture safe alongside the live hooks (no
    double-count); ``known_only=False`` lets brand-new projects be discovered
    without prior registration.
    """
    cu = harvest.catch_up(now=now, skip_captured=True, known_only=False)
    folders = {e["project"]: e["folder"]
               for e in load_registry() if e.get("project") and e.get("folder")}
    results = []
    for proj in sorted(cu.get("projects", {}).keys()):
        info = cu["projects"][proj]
        try:
            if proj in folders:
                # sync.sync runs projector.project + briefing + writes MEMORY.md
                sync.sync(folders[proj], project=proj)
            else:
                projector.project(proj)   # store capture only, no external file
            graph.build(proj, semantic=False)
            results.append({"project": proj,
                            "new_events": info.get("new_events", 0),
                            "synced": proj in folders,
                            "mirrored": _refresh_mirror(proj)})
        except Exception as e:  # one bad project must not abort the rest
            results.append({"project": proj, "error": str(e)})
    return results
