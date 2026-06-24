# src/mindhive/loop/contract.py
import re
from dataclasses import dataclass

_EXIT_RE = re.compile(r"EXIT_SIGNAL:\s*(true|false)", re.IGNORECASE)
_STATUS_RE = re.compile(r"STATUS:\s*(COMPLETE|IN_PROGRESS)", re.IGNORECASE)
_PROGRESS_RE = re.compile(r"progress:\s*(true|false)", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"summary:\s*(.+)", re.IGNORECASE)


def _to_bool(m):
    return None if m is None else (m.group(1).lower() == "true")


@dataclass
class LoopStatus:
    summary: str | None
    progress: bool | None
    status: str | None            # "COMPLETE" | "IN_PROGRESS" | None
    exit_signal: bool | None      # explicit only; None if the field was absent
    has_completion_signal: bool   # derived per Ralph's precedence


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
                      has_completion_signal=has_completion_signal)
