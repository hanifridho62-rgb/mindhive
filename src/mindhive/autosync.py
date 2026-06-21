# src/mindhive/autosync.py
# Hook-free automation: harvest transcripts -> rebuild projections -> sync native MEMORY.md.
# Driven by a Windows Scheduled Task (see hooks/autosync.ps1).
import json
from . import paths, harvest, projector, briefing, graph, sync


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


def run_all() -> list:
    results = []
    for entry in load_registry():
        proj = entry.get("project")
        try:
            h = harvest.harvest(entry["transcripts"], proj)
            projector.project(proj)
            briefing.briefing(proj)
            graph.build(proj, semantic=False)
            sync.sync(entry["folder"], project=proj)
            results.append({"project": proj, "harvest": h})
        except Exception as e:  # one bad entry must not abort the rest
            results.append({"project": proj, "error": str(e)})
    return results
