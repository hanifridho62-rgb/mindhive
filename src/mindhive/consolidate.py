# src/mindhive/consolidate.py
"""Memory consolidation — the "dreaming" pass.

Periodically merge near-duplicate semantic memories so long-lived namespaces
don't drown their own briefings in reworded repeats. Append-only discipline
throughout: nothing in the ledger is edited or deleted. A consolidation
appends one ``memory_consolidated`` event per duplicate cluster naming the
surviving event and precise identity keys for the events it absorbs;
projections (briefing → MEMORY.md) then exclude the absorbed originals, which
stay on disk in the chain as the archive. Bounded batch + cooldown marker +
LIVE_LOCK guard, mirroring the reflect pattern.

Protected content: events tagged ``pinned`` never enter a cluster — neither
as survivor nor as absorbed member — matching the rule-decay precedent that
automated pruning cannot touch human-protected content (ADR-0007). Polarity
is a hard cluster splitter: a negated memo ("do NOT deploy X") never merges
with its non-negated twin, since token-set similarity is blind to negation."""
import json
import re
import time

from . import dedupe, events as ev_mod, guard, paths, projector

# code_change is exempt: it is an audit trail (see changelog.py), and two
# similar-sounding change-log entries are genuinely distinct changes — the
# dreaming pass must never absorb one into another. learning is exempt for the
# same reason: each Self-Reliant PRD is a distinct build artifact, and two
# PRDs about the same goal read similar while prescribing different builds.
# sop is exempt because the registry fold (sop.py) replays EVERY lifecycle
# event — absorbing one would silently rewrite the current SOP set. roadmap
# is exempt for the same reason: roadmap.fold replays every set/step/
# interrupt event, and absorbing one would silently rewrite a plan's progress.
# mindnet_task / mindnet_phase are exempt for the same replay reason: both
# folds replay EVERY lifecycle event, so absorbing one would silently rewrite a
# task's dependencies or a phase's evidence trail.
CONSOLIDATE_TYPES = ev_mod.SEMANTIC_TYPES - {"state_set", "code_change",
                                             "learning", "sop", "roadmap",
                                             "mindnet_task", "mindnet_phase"}
DEFAULT_LIMIT = 3            # clusters merged per pass
DEFAULT_COOLDOWN_HOURS = 6.0
_MARKER = "consolidate_marker.json"

_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|none|don'?t|do\s+not|won'?t|refuse[sd]?|forbid(?:den)?|"
    r"avoid|stop)\b", re.IGNORECASE)


def _polarity(text) -> bool:
    """True when the memo carries negation/prohibition language."""
    return bool(_NEGATION_RE.search(str(text or "")))


def _event_key(e, text=None):
    """Precise identity of one event: second-resolution timestamps collide,
    so the key is (ts, type, content-fingerprint)."""
    if text is None:
        text = e.payload.get("text", "") if isinstance(e.payload, dict) else str(e.payload)
    return (e.ts, e.type, dedupe.text_fingerprint(text))


def absorbed_keys(evs) -> set:
    """Identity keys of events absorbed by prior consolidations. Precise
    (ts, type, fingerprint) tuples from ``absorbed`` payloads, plus bare
    timestamp strings from legacy ``absorbed_ts`` payloads (pre-fingerprint
    events written by the first iteration of this feature)."""
    out: set = set()
    for e in evs:
        if e.type != "memory_consolidated" or not isinstance(e.payload, dict):
            continue
        for entry in e.payload.get("absorbed") or []:
            if isinstance(entry, (list, tuple)) and len(entry) == 3:
                out.add(tuple(entry))
        out.update(e.payload.get("absorbed_ts") or [])   # legacy shape
    return out


def is_absorbed(e, keys, text=None) -> bool:
    """Whether *e* was absorbed by a prior consolidation (precise key, with
    the legacy bare-ts shape honored for old ledgers)."""
    return _event_key(e, text=text) in keys or e.ts in keys


def plan_consolidation(evs, limit=DEFAULT_LIMIT) -> list[dict]:
    """Duplicate clusters worth merging, newest survivor first. Each entry is
    ``{type, survivor_ts, text, absorbed}`` where absorbed holds precise
    (ts, type, fingerprint) keys. Pinned events and already-absorbed events
    never enter a cluster; opposite-polarity memos never merge."""
    done = absorbed_keys(evs)
    reps: list[tuple] = []          # (type, polarity, token_set, cluster_dict)
    clusters: list[dict] = []
    for e in reversed(evs):         # newest-first, like the briefing
        if e.type not in CONSOLIDATE_TYPES:
            continue
        if "pinned" in (getattr(e, "tags", None) or []):
            continue                 # protected content is never auto-touched
        text = e.payload.get("text", "") if isinstance(e.payload, dict) else str(e.payload)
        if is_absorbed(e, done, text=text):
            continue
        toks = dedupe.normalize(text)
        neg = _polarity(text)
        match = None
        for r_type, r_neg, r_toks, cluster in reps:
            if r_type == e.type and r_neg == neg and dedupe.similar(toks, r_toks):
                match = cluster
                break
        if match is not None:
            key = _event_key(e, text=text)
            if key != match["_survivor_key"]:
                match["absorbed"].append(list(key))
        else:
            cluster = {"type": e.type, "survivor_ts": e.ts, "text": text,
                       "absorbed": [], "_survivor_key": _event_key(e, text=text)}
            reps.append((e.type, neg, toks, cluster))
            clusters.append(cluster)
    real = []
    for c in clusters:
        if c["absorbed"]:
            c.pop("_survivor_key", None)
            real.append(c)
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
    # Atomic temp+replace: concurrent passes for different projects must not
    # tear each other's cooldown entries with a partial write.
    p = _marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(marker), encoding="utf-8")
    tmp.replace(p)


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
            "absorbed": c["absorbed"],
            "count": len(c["absorbed"]) + 1,
        }, project=project)
    marker[name] = now
    _save_marker(marker)
    if clusters:
        projector.project(project)   # rewritten digest reaches the next session
    return {"consolidated": len(clusters),
            "absorbed": sum(len(c["absorbed"]) for c in clusters)}


def dedupe_headline(text: str, cap: int = 200) -> str:
    """First line of the surviving text, whitespace-collapsed and capped."""
    first = " ".join(str(text or "").split())
    return first[:cap]
