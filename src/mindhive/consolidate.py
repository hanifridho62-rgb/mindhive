# src/mindhive/consolidate.py
"""Memory consolidation — the "dreaming" pass.

Periodically merge near-duplicate semantic memories so long-lived namespaces
don't drown their own briefings in reworded repeats. Append-only discipline
throughout: nothing in the ledger is edited or deleted. A consolidation
appends one ``memory_consolidated`` event per duplicate cluster naming the
surviving event and the timestamps it absorbs; projections (briefing →
MEMORY.md) then exclude the absorbed originals, which stay on disk in the
chain as the archive. Bounded batch + cooldown marker + LIVE_LOCK guard,
mirroring the reflect pattern."""
import json
import time

from . import dedupe, events as ev_mod, guard, paths, projector

CONSOLIDATE_TYPES = ev_mod.SEMANTIC_TYPES - {"state_set"}
DEFAULT_LIMIT = 3            # clusters merged per pass
DEFAULT_COOLDOWN_HOURS = 6.0
_MARKER = "consolidate_marker.json"


def absorbed_ts(evs) -> set:
    """Timestamps of events absorbed by prior consolidations."""
    out: set = set()
    for e in evs:
        if e.type == "memory_consolidated" and isinstance(e.payload, dict):
            out.update(e.payload.get("absorbed_ts") or [])
    return out


def plan_consolidation(evs, limit=DEFAULT_LIMIT) -> list[dict]:
    """Duplicate clusters worth merging, newest survivor first. Each entry is
    ``{type, survivor_ts, text, absorbed_ts}``; events a prior consolidation
    already absorbed never re-enter a cluster."""
    done = absorbed_ts(evs)
    reps: list[tuple] = []          # (type, token_set, cluster_dict)
    clusters: list[dict] = []
    for e in reversed(evs):         # newest-first, like the briefing
        if e.type not in CONSOLIDATE_TYPES or e.ts in done:
            continue
        text = e.payload.get("text", "") if isinstance(e.payload, dict) else str(e.payload)
        toks = dedupe.normalize(text)
        match = None
        for r_type, r_toks, cluster in reps:
            if r_type == e.type and dedupe.similar(toks, r_toks):
                match = cluster
                break
        if match is not None:
            match["absorbed_ts"].append(e.ts)
        else:
            cluster = {"type": e.type, "survivor_ts": e.ts,
                       "text": text, "absorbed_ts": []}
            reps.append((e.type, toks, cluster))
            clusters.append(cluster)
    real = [c for c in clusters if c["absorbed_ts"]]
    return real[:max(0, int(limit))]


def _marker_path():
    return paths.store_home() / _MARKER


def _load_marker() -> dict:
    p = _marker_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_marker(marker: dict) -> None:
    p = _marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(marker), encoding="utf-8")


def consolidate(project=None, limit=DEFAULT_LIMIT,
                cooldown_hours=DEFAULT_COOLDOWN_HOURS, force=False,
                now_fn=time.time) -> dict:
    """One bounded dreaming pass for *project*. Returns
    ``{consolidated, absorbed}`` (or ``{skipped: reason}``)."""
    guard.ensure_safe(force=force)   # LIVE_LOCK: never burst CPU while trading
    name = project or paths.detect_project()
    marker = _load_marker()
    last = float(marker.get(name) or 0.0)
    now = now_fn()
    if cooldown_hours and last and (now - last) < cooldown_hours * 3600:
        return {"skipped": "cooldown", "last_run": last}
    evs = ev_mod.read_all(project)
    clusters = plan_consolidation(evs, limit=limit)
    for c in clusters:
        ev_mod.append_event("memory_consolidated", {
            "type": c["type"], "survivor_ts": c["survivor_ts"],
            "text": dedupe_headline(c["text"]),
            "absorbed_ts": c["absorbed_ts"],
            "count": len(c["absorbed_ts"]) + 1,
        }, project=project)
    marker[name] = now
    _save_marker(marker)
    if clusters:
        projector.project(project)   # rewritten digest reaches the next session
    return {"consolidated": len(clusters),
            "absorbed": sum(len(c["absorbed_ts"]) for c in clusters)}


def dedupe_headline(text: str, cap: int = 200) -> str:
    """First line of the surviving text, whitespace-collapsed and capped."""
    first = " ".join(str(text or "").split())
    return first[:cap]
