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
        # OR-match with a relevance score = number of distinct query terms
        # present. AND-match (the old behavior) returned nothing unless EVERY
        # term appeared, silently dropping partial matches; scoring lets callers
        # take the top hits and threshold the tail instead.
        score = sum(1 for t in terms if t in low)
        if score:
            snippet = " ".join(text.split())[:200]
            # Return the bare ledger filename (not the full path) to match
            # vectors.semantic_recall — one consistent shape for both modes.
            results.append({"file": f.name, "snippet": snippet, "score": score})
    # Highest overlap first; ties keep filename (chronological) order, which
    # sorted() already gives us and Python's sort is stable, so it's preserved.
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]
