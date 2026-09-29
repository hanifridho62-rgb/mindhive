# SPDX-License-Identifier: Apache-2.0
# src/mindhive/core/dreaming.py
"""The Dreaming Pass (blueprint P1j, MEB CTX-02): semantic de-duplication of
the vector memory at a cosine similarity of `DREAMING_THRESHOLD` = 0.88,
inclusive, as a pure function over whatever the caller hands it.

`dreaming_pass(items)` walks *items* in the order given -- the caller's
recency order, newest first, is the one that matters, because the FIRST of
two near-duplicates is the one kept -- and drops every item at least
`threshold` similar to an item already kept. It returns `(kept, dropped)`,
both in input order, and never mutates its input. The similarity is a
parameter; `default_similarity` asks the pair for vectors first (a
`.vector` attribute or a `"vector"` key -> `cosine_similarity`) and falls
back to a token-set Jaccard over the item's text (`.text`, `"text"`, or
the item itself when it is a `str`), so a text-only corpus works without
an encoder and a vector corpus is compared the way the index compares it.

Not `dedupe.similar`: that is the LEDGER's 0.85 Jaccard over projection
rows and stays where it is. This is the vector memory's rule at the
blueprint's number, and `adaptivetail` (P1c) re-exports it as the pass
that runs before a rollover.
"""
import math
import re

__all__ = ["DREAMING_THRESHOLD", "cosine_similarity", "jaccard_similarity", "tokens",
           "default_similarity", "dreaming_pass"]

DREAMING_THRESHOLD = 0.88
_TOKEN = re.compile(r"[a-z0-9]+")


def cosine_similarity(a, b) -> float:
    """`a.b / (|a||b|)` over two equal-length sequences of numbers, clamped
    to [-1, 1] against rounding; 0.0 when either has zero norm (nothing to
    be similar to); `ValueError` for unequal lengths or a non-finite
    component."""
    a = list(a)
    b = list(b)
    if len(a) != len(b):
        raise ValueError(f"panjang vektor berbeda: {len(a)} vs {len(b)}")
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("komponen vektor harus terhingga (bukan NaN/inf)")
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / math.sqrt(na * nb)))


def tokens(text) -> frozenset:
    """Lower-cased `[a-z0-9]+` runs of *text*, as a set."""
    return frozenset(_TOKEN.findall(str(text).lower()))


def jaccard_similarity(a, b) -> float:
    """`|a & b| / |a | b|` over two token sets; two empty sets are 0.0 (two
    texts that say nothing are not the same memory)."""
    a = frozenset(a)
    b = frozenset(b)
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _vector_of(item):
    v = getattr(item, "vector", None)
    if v is None and isinstance(item, dict):
        v = item.get("vector")
    return v


def _text_of(item) -> str:
    if isinstance(item, str):
        return item
    t = getattr(item, "text", None)
    if t is None and isinstance(item, dict):
        t = item.get("text")
    return "" if t is None else str(t)


def default_similarity(a, b) -> float:
    """Cosine over the two vectors when BOTH items carry one, else Jaccard
    over the tokens of their text."""
    va, vb = _vector_of(a), _vector_of(b)
    if va is not None and vb is not None:
        return cosine_similarity(va, vb)
    return jaccard_similarity(tokens(_text_of(a)), tokens(_text_of(b)))


def dreaming_pass(items, *, similarity=None, threshold: float = DREAMING_THRESHOLD):
    """`(kept, dropped)`: *items* in input order, each dropped when
    `similarity(item, kept_item) >= threshold` for some item kept before it.
    *similarity* is called as `(candidate, kept)`; the default is
    `default_similarity`."""
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold harus dalam [0, 1]: {threshold!r}")
    sim = similarity or default_similarity
    kept, dropped = [], []
    for item in items:
        if any(sim(item, k) >= threshold for k in kept):
            dropped.append(item)
        else:
            kept.append(item)
    return kept, dropped
