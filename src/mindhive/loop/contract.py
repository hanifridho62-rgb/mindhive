# SPDX-License-Identifier: Apache-2.0
# src/mindhive/loop/contract.py
import re
from dataclasses import dataclass

# (?![|\w]) refuses the unfilled template literal "true|false" — an echoed,
# blank contract block must never read as a filled-in signal.
_EXIT_RE = re.compile(r"EXIT_SIGNAL:\s*(true|false)(?![|\w])", re.IGNORECASE)
_STATUS_RE = re.compile(r"STATUS:\s*(COMPLETE|IN_PROGRESS)(?![|\w])", re.IGNORECASE)
_PROGRESS_RE = re.compile(r"progress:\s*(true|false)(?![|\w])", re.IGNORECASE)
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
    """Number of DISTINCT completion patterns present in the reply text.
    STATUS is judged on its LAST occurrence (the real LOOP_STATUS block), so
    an echo of the instruction template can't count a phantom COMPLETE."""
    count = 0
    for pat in _COMPLETION_PATTERNS:
        if pat is _STATUS_RE:
            m = None
            for m in pat.finditer(text):
                pass
            if m is not None and m.group(1).upper() == "COMPLETE":
                count += 1
        elif pat.search(text):
            count += 1
    return count


def _to_bool(m):
    return None if m is None else (m.group(1).lower() == "true")


def _last(pattern, text):
    """LAST regex match in the text, or None. The LOOP_STATUS block is the
    final thing in a reply, so the last occurrence is authoritative — earlier
    matches are usually the agent quoting the contract instruction or
    discussing the fields in prose, and must not override the real block."""
    last = None
    for m in pattern.finditer(text):
        last = m
    return last


@dataclass
class LoopStatus:
    summary: str | None
    progress: bool | None
    status: str | None            # "COMPLETE" | "IN_PROGRESS" | None
    exit_signal: bool | None      # explicit only; None if the field was absent
    has_completion_signal: bool   # derived per Ralph's precedence
    heuristic_count: int = 0      # distinct natural-language completion signals


def parse_status(text: str) -> LoopStatus:
    exit_signal = _to_bool(_last(_EXIT_RE, text))
    status_m = _last(_STATUS_RE, text)
    status = status_m.group(1).upper() if status_m else None
    progress = _to_bool(_last(_PROGRESS_RE, text))
    summary_m = _last(_SUMMARY_RE, text)
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
