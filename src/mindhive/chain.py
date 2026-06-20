# src/mindhive/chain.py
import hashlib
import json
from . import paths, events


def compute_chain(evs) -> dict:
    prev = "0" * 64
    head = prev
    for ev in evs:
        material = prev + events.canonical_json(events.to_dict(ev))
        head = hashlib.sha256(material.encode("utf-8")).hexdigest()
        prev = head
    return {"count": len(evs), "head": head}


def write_chain(project=None) -> dict:
    c = compute_chain(events.read_all(project))
    pdir = paths.project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "chain.json").write_text(events.canonical_json(c), encoding="utf-8")
    return c


def verify(project=None) -> dict:
    try:
        actual = compute_chain(events.read_all(project))
    except Exception as e:
        return {"ok": False, "reason": f"read error: {e}"}
    p = paths.project_dir(project) / "chain.json"
    if not p.exists():
        return {"ok": False, "reason": "no chain.json (run: hive project)", "actual": actual}
    stored = json.loads(p.read_text(encoding="utf-8"))
    return {"ok": stored == actual, "stored": stored, "actual": actual}
