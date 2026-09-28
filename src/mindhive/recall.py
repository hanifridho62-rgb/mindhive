# SPDX-License-Identifier: Apache-2.0
# src/mindhive/recall.py
import math
import re

from . import paths

# Word-boundary tokens. Substring matching (the old behavior) let "taker" match
# "caretaker" and "real" match "really", surfacing unrelated ledger entries;
# tokenizing on word boundaries kills that whole class of false positive.
_WORD = re.compile(r"[a-z0-9]+")

# Common English function words carry no relevance signal. Left unfiltered, each
# one contributed a full point, so a query like "real fee tier maker taker"
# scored governance-config docs on incidental "for"/"the"/"is" hits.
_STOPWORDS = frozenset("""
a an and are as at be been but by can did do does for from had has have if in
into is it its no nor not of off on or our so than that the their then there
these they this to was were what when which who will with you your
""".split())

# Threshold: keep the single best hit always, then drop the tail scoring below
# max(top * RATIO, FLOOR). Relative-with-floor means a genuinely-weak-but-best
# result still surfaces while the long noise tail is cut.
_THRESHOLD_RATIO = 0.15
_THRESHOLD_FLOOR = 0.5

# A query term that appears in a ledger entry's `type`/`tags` frontmatter is a
# stronger signal than an incidental body mention — weight it up.
_TAG_BOOST = 1.5


def _tokenize(text: str) -> list:
    return _WORD.findall(text.lower())


def _query_terms(query: str) -> list:
    toks = _tokenize(query)
    kept = [t for t in toks if t not in _STOPWORDS]
    # Degenerate all-stopword query: fall back to the raw tokens so the caller
    # still gets a best-effort answer instead of an empty result.
    return kept or toks


def _frontmatter_boost_tokens(text: str):
    """Return (boost_tokens, body). boost_tokens are the tokens drawn from the
    ledger entry's `type:` and `tags:` frontmatter — the fields worth boosting.
    Generic header keys (ts, session) are deliberately excluded so a query for
    "session" doesn't match every entry."""
    if not text.startswith("---"):
        return set(), text
    end = text.find("\n---", 3)
    if end == -1:
        return set(), text
    header = text[3:end]
    body = text[end + 4:]
    boost = set()
    for line in header.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        if key.strip().lower() in ("type", "tags"):
            boost.update(_tokenize(val))
    return boost, body


def recall(query, project=None, semantic=False, limit=10, force=False):
    if semantic:
        from . import vectors
        # EVENT vectors first, files as the fallback. A file-level hit points at
        # LEDGER/decisions.md and shows 200 characters of it; an event-level hit
        # IS the decision, with its timestamp and session. The fallback is not
        # decoration -- the event index is empty until `hive embed` has run
        # once, and returning nothing there would look like "no such memory"
        # rather than "not indexed yet".
        if vectors.indexed_event_count(project) > 0:
            hits = vectors.semantic_recall_events(query, project=project,
                                                  limit=limit, force=force)
            if hits:
                return hits
        return vectors.semantic_recall(query, project=project, limit=limit,
                                       force=force)
    ldir = paths.project_dir(project) / "LEDGER"
    if not ldir.exists():
        return []
    terms = _query_terms(query)
    if not terms:
        return []

    # First pass: read each entry once, tokenize body + boost fields, and count
    # document-frequency so rarity weighting can down-weight terms that appear in
    # many entries (a distinctive term should outrank a ubiquitous one).
    docs = []
    df = {}
    for f in sorted(ldir.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        boost_tokens, body = _frontmatter_boost_tokens(text)
        match_tokens = set(_tokenize(body)) | boost_tokens
        docs.append((f, text, match_tokens, boost_tokens))
        for t in match_tokens:
            df[t] = df.get(t, 0) + 1
    n = len(docs)

    def idf(t: str) -> float:
        # Smoothed IDF: always positive; rarer terms score higher.
        return math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0

    results = []
    for f, text, match_tokens, boost_tokens in docs:
        score = 0.0
        for t in terms:
            if t in match_tokens:
                w = idf(t)
                if t in boost_tokens:
                    w *= _TAG_BOOST
                score += w
        if score > 0:
            snippet = " ".join(text.split())[:200]
            # Bare ledger filename (not full path) matches vectors.semantic_recall
            # — one consistent shape for both modes.
            results.append({"file": f.name, "snippet": snippet, "score": round(score, 6)})

    # Highest weighted overlap first; ties keep filename (chronological) order,
    # which sorted() gave us and Python's stable sort preserves.
    results.sort(key=lambda r: r["score"], reverse=True)
    if results:
        cut = max(results[0]["score"] * _THRESHOLD_RATIO, _THRESHOLD_FLOOR)
        # Always keep the single best hit; drop the weak tail below the cut.
        results = [r for i, r in enumerate(results) if i == 0 or r["score"] >= cut]
    return results[:limit]
