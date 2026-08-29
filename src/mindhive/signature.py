# src/mindhive/signature.py
"""Map a raw error string to a canonical root-cause SIGNATURE.

Pure, deterministic, stdlib-only. The signature — not raw stderr — is what
recurrence, dedup, and rules key off. Two failures with the same root cause
(same fix) must share one signature; genuinely different bugs must not.
"""
import hashlib
import re

_SLUG = re.compile(r"[^a-z0-9]+")

# Import failures quote the name ("No module named 'tools'"); `python -m
# <missing>` does not ("No module named pyflakes"). Matching only the quoted
# form sent every `python -m` miss into the raw: fallback.
_NO_MODULE = re.compile(r"No module named '?([A-Za-z_][\w.]*)'?")


def slug(text, max_len: int = 48) -> str:
    """Lowercase-hyphenated bounded slug for a signature component (e.g. the
    context stem of a judgment signature). Never empty."""
    s = _SLUG.sub("-", str(text or "").lower()).strip("-")
    return s[:max_len].rstrip("-") or "unknown"


def slug_id(text, max_len: int = 40) -> str:
    """Collision-resistant signature component: a readable slug prefix plus a
    short content hash. Two distinct texts never share a component — even when
    their slugs would truncate to the same prefix, or when the text is
    non-latin and slugs to nothing (which would otherwise collapse every such
    text onto the shared ``unknown`` bucket). Deterministic and stdlib-only."""
    raw = str(text or "")
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
    prefix = _SLUG.sub("-", raw.lower()).strip("-")[:max_len].rstrip("-")
    return f"{prefix}-{h}" if prefix else h
_PS_TERM = re.compile(r"The term '([^']+)'")
_PS_NOTRECOG = re.compile(r"'([^']+)' is not recognized")
_EXIT_CODE = re.compile(r"exit code (\d+)", re.I)

# A Python traceback's own header is no more diagnostic than "Exit code <n>":
# the line that TERMINATES the traceback is. Folding in `Traceback (most recent
# call last):` as the detail line collapsed every uncaught Python exception
# onto ONE signature — raw:37c59e9cae, 42 occurrences across 17 sessions,
# holding AssertionError, KeyError, ImportError, AttributeError,
# FileNotFoundError, JSONDecodeError and PermissionError alike.
_TB_HEADER = re.compile(r"^\s*Traceback \(most recent call last\):")
_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*)\s*(?::\s*(.*))?$")

# The name is not in that module. One root cause (it was assumed, never
# checked) and one fix, whichever of the two forms Python reports it in:
# `cannot import name 'X' from 'Y'` or `module 'Y' has no attribute 'X'`.
_CANNOT_IMPORT = re.compile(r"cannot import name '[^']+' from '([A-Za-z_][\w.]*)'")
_NO_ATTR = re.compile(r"module '([A-Za-z_][\w.]*)' has no attribute")


def _mask(line: str) -> str:
    """Volatile bits out: absolute paths and digit runs (line numbers, PIDs), so
    the same error at a different line or path still collapses together."""
    norm = str(line or "").strip().lower()
    norm = re.sub(r"[a-z]:\\[^\s]+|/[^\s]+", "PATH", norm)
    return re.sub(r"\d+", "N", norm)


def _hash(norm: str) -> str:
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def _exception_line(text) -> str:
    """The line that TERMINATES the last traceback in ``text``, or "".

    A traceback's frames are indented and its exception line is not, so the
    first unindented line after a header ends that traceback. Scanning on to
    the last one picks the exception that actually escaped (CPython prints the
    cause first, the raised exception last), and stopping at each exception
    line keeps interleaved stdout — before or after the traceback — out of the
    signature. Output truncated mid-traceback yields "" so the caller falls
    back instead of signing on a frame it happened to end on.
    """
    found, in_tb = "", False
    for line in str(text or "").splitlines():
        if _TB_HEADER.match(line):
            in_tb = True
        elif in_tb and line.strip() and not line[:1].isspace():
            found, in_tb = line.strip(), False
    return found


def signature(error_text) -> str:
    t = str(error_text or "")

    m = _NO_MODULE.search(t)
    if m:
        top = m.group(1).split(".")[0]          # collapse submodules to package root
        return f"pymodule:{top}"

    # Sibling of pymodule: the package imports fine, the NAME in it does not
    # exist. Different fix (use a name that is really there vs. install the
    # package), so a different family.
    m = _CANNOT_IMPORT.search(t) or _NO_ATTR.search(t)
    if m:
        return "pysymbol:" + m.group(1).split(".")[0]

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

    # Windows-locked git-worktree removal: `git worktree remove` prints
    # "error: failed to delete '<path>': Permission denied" when a process,
    # editor, or AV still holds a handle inside the tree. One root cause, one fix
    # (remove --force + retry, never cd into the doomed tree), so it earns its own
    # signature instead of vanishing into the raw: first-line-hash bucket where it
    # was previously invisible and unfixable.
    if "failed to delete" in low and "permission denied" in low:
        return "git:worktree-locked"

    # Fallback: hash the first line with volatile bits (numbers, paths) masked so
    # the same error with different line numbers/paths still collapses together.
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    first = lines[0] if lines else ""

    # An uncaught Python exception is named by the line that ENDS its
    # traceback — not by the harness's "Exit code 1" header, and not by the
    # traceback's own header. Keying off that line (type kept readable, message
    # masked like any other detail) splits the old mega-bucket into its real
    # families without minting a fresh signature per path or line number.
    exc = _exception_line(t)
    if exc:
        em = _EXC_LINE.match(exc)
        if em:
            kind = em.group(1).split(".")[-1]      # json.decoder.X -> X
            return "pyexc:%s:%s" % (slug(kind), _hash(_mask(em.group(2) or "")))

    # "Exit code <n>" is a HEADER, not a diagnosis. Two bugs previously merged
    # every such failure into a single unfixable bucket (raw:e5637cbd18, 375
    # occurrences — 49.3% of all captured failures across four projects):
    # masking every digit run made "Exit code 1" and "Exit code 49" identical,
    # and hashing only the first line discarded the line that names the failure.
    # So: keep the code verbatim, and fold in the masked detail line.
    m = _EXIT_CODE.match(first)
    if m:
        rest = first[m.end():].strip()
        detail = rest or (lines[1] if len(lines) > 1 else "")
        return "raw:" + _hash("exit code %s|%s" % (m.group(1), _mask(detail)))

    return "raw:" + _hash(_mask(first))
