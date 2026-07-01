# src/mindhive/signature.py
"""Map a raw error string to a canonical root-cause SIGNATURE.

Pure, deterministic, stdlib-only. The signature — not raw stderr — is what
recurrence, dedup, and rules key off. Two failures with the same root cause
(same fix) must share one signature; genuinely different bugs must not.
"""
import hashlib
import re

_NO_MODULE = re.compile(r"No module named '([^']+)'")
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

    # Fallback: hash the first line with volatile bits (numbers, paths) masked so
    # the same error with different line numbers/paths still collapses together.
    first = next((ln for ln in t.splitlines() if ln.strip()), "")
    norm = first.strip().lower()
    norm = re.sub(r"[a-z]:\\[^\s]+|/[^\s]+", "PATH", norm)
    norm = re.sub(r"\d+", "N", norm)
    return "raw:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]
