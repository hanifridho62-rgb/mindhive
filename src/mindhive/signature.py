# src/mindhive/signature.py
"""Map a raw error string to a canonical root-cause SIGNATURE.

Pure, deterministic, stdlib-only. The signature — not raw stderr — is what
recurrence, dedup, and rules key off. Two failures with the same root cause
(same fix) must share one signature; genuinely different bugs must not.
"""
import hashlib
import re

_SLUG = re.compile(r"[^a-z0-9]+")

_NO_MODULE = re.compile(r"No module named '([^']+)'")


def slug(text, max_len: int = 48) -> str:
    """Lowercase-hyphenated bounded slug for a signature component (e.g. the
    context stem of a judgment signature). Never empty."""
    s = _SLUG.sub("-", str(text or "").lower()).strip("-")
    return s[:max_len].rstrip("-") or "unknown"
_PS_TERM = re.compile(r"The term '([^']+)'")
_PS_NOTRECOG = re.compile(r"'([^']+)' is not recognized")


def signature(error_text) -> str:
    t = str(error_text or "")

    m = _NO_MODULE.search(t)
    if m:
        top = m.group(1).split(".")[0]          # collapse submodules to package root
        return f"pymodule:{top}"

    m = _PS_TERM.search(t) or _PS_NOTRECOG.search(t)
    if m:
        return f"pscmd:{m.group(1)}"

    if "unterminated string literal" in t:
        return "pysyntax:unterminated-string"
    if "SyntaxError" in t:
        return "pysyntax:other"

    # Real behavioral families (the dominant recurring failures in practice —
    # see the Plan 1 corpus-replay findings), matched case-insensitively.
    low = t.lower()
    if "file has not been read yet" in low:
        return "edit:not-read"
    if "string to replace not found" in low:
        return "edit:string-not-found"
    if "ripgrep search timed out" in low:
        return "tool:ripgrep-timeout"
    if "exceeds maximum allowed size" in low:
        return "read:file-too-large"

    # Fallback: hash the first line with volatile bits (numbers, paths) masked so
    # the same error with different line numbers/paths still collapses together.
    first = next((ln for ln in t.splitlines() if ln.strip()), "")
    norm = first.strip().lower()
    norm = re.sub(r"[a-z]:\\[^\s]+|/[^\s]+", "PATH", norm)
    norm = re.sub(r"\d+", "N", norm)
    return "raw:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]
