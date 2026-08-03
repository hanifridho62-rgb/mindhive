# src/mindhive/rotation/occupancy.py
"""True context occupancy, read out of a Claude Code session transcript.

The Stop hook payload hands us a `transcript_path` and nothing about size, so
the size has to come from the transcript itself: a JSONL file where each
assistant turn carries the usage the API reported for it.

Two properties this file exists to guarantee.

**Totality.** Every call returns an `Occupancy`. A missing file, a directory, a
half-written line, invalid UTF-8, a row that is a bare string — all of them are
a typed unknown with a stable `reason`, never a raise. This runs at the end of
every agent turn; an exception here is a traceback between the user and their
answer, and (worse) a hook that looks broken enough to disable.

**Unknown is not zero.** A transcript we cannot read reports `tokens=None`, not
0. Zero means "the context is empty" and would suppress rotation silently
forever, which is the failure mode you never notice.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from ..loop import context as ctx_mod


@dataclass(frozen=True)
class Occupancy:
    """What the transcript says about the session's current context size.

    `reason` is a stable rule id (`ok`, `missing_file`, `unreadable`, `no_usage`)
    and is what callers and tests branch on — the prose in logs is free to change.
    """
    tokens: int | None
    model: str | None
    reason: str

    @property
    def known(self) -> bool:
        return self.tokens is not None


_UNKNOWN_MISSING = Occupancy(None, None, "missing_file")
_UNKNOWN_UNREADABLE = Occupancy(None, None, "unreadable")
_UNKNOWN_NO_USAGE = Occupancy(None, None, "no_usage")


def _int(value) -> int:
    """A usage field, coerced. A non-numeric field contributes 0 rather than
    poisoning the whole reading — one weird field should cost us that field's
    tokens, not the entire measurement."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def read_occupancy(path) -> Occupancy:
    """Occupancy of the newest MAIN-SESSION assistant turn in the transcript.

    Sidechain rows are skipped: `isSidechain: true` marks a subagent's turn, and
    a subagent has its own context window. Counting it would rotate the parent
    session because a child filled up — and subagents routinely burn far more
    context than the session that spawned them.

    Scans newest-first and stops at the first usable row, so cost is bounded by
    how far back the last assistant turn is rather than by file size. These
    files reach multiple MB.
    """
    if path is None:
        return _UNKNOWN_MISSING
    p = Path(path)
    try:
        if not p.is_file():
            return _UNKNOWN_MISSING
        # errors="ignore" for the same reason harvest.py uses it: a transcript
        # can carry bytes that are not valid UTF-8, and a measurement is worth
        # more than byte-perfect fidelity we do not need.
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return _UNKNOWN_UNREADABLE

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue                       # truncated tail, or mid-write garbage
        if not isinstance(row, dict):
            continue
        if row.get("type") != "assistant" or row.get("isSidechain"):
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue                       # an assistant row can predate its usage
        tokens = ctx_mod.occupancy_from(
            _int(usage.get("input_tokens")),
            _int(usage.get("cache_creation_input_tokens")),
            _int(usage.get("cache_read_input_tokens")),
        )
        model = message.get("model")
        return Occupancy(tokens, model if isinstance(model, str) else None, "ok")

    return _UNKNOWN_NO_USAGE
