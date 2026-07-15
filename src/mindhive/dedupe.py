# src/mindhive/dedupe.py
"""Deterministic, meaning-aware deduplication of memory items.

Pure Python — no model, no randomness, no time-dependent branching. The same
input always yields the same output. "Meaning" similarity is approximated by
token-set overlap (Jaccard) on normalized words, which is order-independent so
reworded versions of the same note still match.

This is display-time only: it never mutates the append-only ledger. Callers
collapse a newest-first list into representatives; the original events remain
verbatim on disk.
"""
from __future__ import annotations

import re

# A small, fixed stopword set. Kept deliberately short so the overlap measure
# reflects the meaningful words of a memo rather than grammar glue. Fixed (not
# learned) to preserve determinism.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "so", "of", "to",
    "in", "on", "at", "for", "with", "as", "by", "is", "are", "was", "were",
    "be", "been", "it", "its", "this", "that", "these", "those", "i", "you",
    "we", "they", "do", "does", "did", "not", "no", "use", "using", "user",
})

_WORD = re.compile(r"[a-z0-9]+")

DEFAULT_THRESHOLD = 0.85


def text_fingerprint(text) -> str:
    """Short, deterministic content hash of a memo's normalized text. Used to
    identify a specific event beyond its second-resolution timestamp (two
    events can share a ts; ts+type+fingerprint is unambiguous)."""
    import hashlib
    norm = " ".join(str(text or "").lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def normalize(text) -> frozenset:
    """Lowercase, strip punctuation, drop stopwords; return the token set.

    Returns a frozenset of meaningful word tokens. Empty/whitespace input (or
    text consisting only of stopwords) yields an empty set.
    """
    if not text:
        return frozenset()
    toks = _WORD.findall(str(text).lower())
    return frozenset(t for t in toks if t not in _STOPWORDS)


def similar(a, b, threshold: float = DEFAULT_THRESHOLD) -> bool:
    """True when token sets *a* and *b* overlap at >= threshold (Jaccard).

    Both empty → only similar if the underlying strings are identical, handled
    by the caller via exact-text fallback; here two empty sets are NOT similar
    so that blank memos never collapse together.
    """
    if not a and not b:
        return False
    union = a | b
    if not union:
        return False
    return (len(a & b) / len(union)) >= threshold


def collapse(items, threshold: float = DEFAULT_THRESHOLD) -> list:
    """Collapse near-duplicate memory items, keeping the newest of each cluster.

    *items* is a list of dicts each carrying at least ``type``, ``project`` and
    ``text`` (``ts`` optional), assumed **newest-first**. Two items merge only
    when they share the same ``(type, project)`` AND are :func:`similar` (or are
    byte-identical text). The first item seen for a cluster — the newest, given
    newest-first input — becomes the representative.

    Returns a new list of representative dicts in input order. Each gains a
    ``similar_count`` key: the number of older near-duplicates it absorbed. The
    input dicts are not mutated.
    """
    reps = []          # list of (key, token_set, norm_text, rep_dict)
    out = []
    for item in items:
        # Change-log entries are an audit trail: two similarly-worded entries
        # are genuinely distinct changes and must never collapse into one row
        # (mirrors the CONSOLIDATE_TYPES exemption in consolidate.py).
        if item.get("type") == "code_change":
            rep = dict(item)
            rep["similar_count"] = 0
            out.append(rep)
            continue
        text = item.get("text", "") or ""
        key = (item.get("type"), item.get("project"))
        toks = normalize(text)
        norm_text = " ".join((text or "").lower().split())
        match = None
        for r_key, r_toks, r_norm, r_dict in reps:
            if r_key != key:
                continue
            if similar(toks, r_toks, threshold) or (norm_text and norm_text == r_norm):
                match = r_dict
                break
        if match is not None:
            match["similar_count"] = match.get("similar_count", 0) + 1
            continue
        rep = dict(item)
        rep["similar_count"] = 0
        reps.append((key, toks, norm_text, rep))
        out.append(rep)
    return out
