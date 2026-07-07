# src/mindhive/loop/contract.py
import re
from dataclasses import dataclass

_EXIT_RE = re.compile(r"EXIT_SIGNAL:\s*(true|false)", re.IGNORECASE)
_STATUS_RE = re.compile(r"STATUS:\s*(COMPLETE|IN_PROGRESS)", re.IGNORECASE)
_PROGRESS_RE = re.compile(r"progress:\s*(true|false)", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"summary:\s*(.+)", re.IGNORECASE)

# Natural-language completion heuristics for the dual-condition exit gate:
# each pattern is one distinct signal; the gate can demand a minimum count so
# a lone structured field can't end a run the prose doesn't corroborate.
_COMPLETION_PATTERNS = (
    _STATUS_RE,                                                    # STATUS: COMPLETE (counted only when COMPLETE)
    re.compile(r"\ball\s+(?:the\s+)?tasks?\b[^.\n]{0,60}\b(?:complete[d]?|done|finished)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:everything|all\s+(?:the\s+)?work|the\s+entire\s+task)\s+"
               r"(?:is|has\s+been|was)\s+(?:done|complete[d]?|finished|verified)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:nothing|no\s+(?:work|tasks?|items?))\s+"
               r"(?:is\s+)?(?:left|remains?|remaining)\b", re.IGNORECASE),
    re.compile(r"\ball\s+(?:checklist|checkbox|fix[\s_-]?plan)\s+items?\s+"
               r"(?:are\s+)?(?:checked|complete[d]?|done)\b", re.IGNORECASE),
)


def count_completion_heuristics(text: str) -> int:
    """Number of DISTINCT completion patterns present in the reply text."""
    count = 0
    for pat in _COMPLETION_PATTERNS:
        m = pat.search(text)
        if pat is _STATUS_RE:
            if m and m.group(1).upper() == "COMPLETE":
                count += 1
        elif m:
            count += 1
    return count


def _to_bool(m):
    return None if m is None else (m.group(1).lower() == "true")


@dataclass
class LoopStatus:
    summary: str | None
    progress: bool | None
    status: str | None            # "COMPLETE" | "IN_PROGRESS" | None
    exit_signal: bool | None      # explicit only; None if the field was absent
    has_completion_signal: bool   # derived per Ralph's precedence
    heuristic_count: int = 0      # distinct natural-language completion signals


def parse_status(text: str) -> LoopStatus:
    exit_signal = _to_bool(_EXIT_RE.search(text))
    status_m = _STATUS_RE.search(text)
    status = status_m.group(1).upper() if status_m else None
    progress = _to_bool(_PROGRESS_RE.search(text))
    summary_m = _SUMMARY_RE.search(text)
    summary = summary_m.group(1).strip() if summary_m else None

    # Precedence: explicit EXIT_SIGNAL is authoritative. Only when it is absent
    # does STATUS: COMPLETE infer a completion signal.
    if exit_signal is not None:
        has_completion_signal = exit_signal
    else:
        has_completion_signal = (status == "COMPLETE")

    return LoopStatus(summary=summary, progress=progress, status=status,
                      exit_signal=exit_signal,
                      has_completion_signal=has_completion_signal,
                      heuristic_count=count_completion_heuristics(text))
