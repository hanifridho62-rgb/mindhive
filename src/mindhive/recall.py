# src/mindhive/recall.py
from . import paths


def recall(query, project=None, semantic=False, limit=10, force=False):
    if semantic:
        from . import vectors
        return vectors.semantic_recall(query, project=project, limit=limit, force=force)
    ldir = paths.project_dir(project) / "LEDGER"
    if not ldir.exists():
        return []
    terms = [t.lower() for t in query.split()]
    results = []
    for f in sorted(ldir.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        low = text.lower()
        if all(t in low for t in terms):
            snippet = " ".join(text.split())[:200]
            results.append({"file": str(f), "snippet": snippet})
    return results[:limit]
