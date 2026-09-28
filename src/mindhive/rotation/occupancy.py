# SPDX-License-Identifier: Apache-2.0
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


# How much of the end of a transcript to read before falling back to the whole
# file. Comfortably larger than any single turn, small enough that reading it on
# every turn is free. Measured against real transcripts: 23 MB files exist.
TAIL_BYTES = 262_144

_UNKNOWN_MISSING = Occupancy(None, None, "missing_file")
_UNKNOWN_UNREADABLE = Occupancy(None, None, "unreadable")
_UNKNOWN_NO_USAGE = Occupancy(None, None, "no_usage")


def _int(value) -> int:
    """A usage field, coerced. A non-numeric field contributes 0 rather than
    poisoning the whole reading — one weird field should cost us that field's
    tokens, not the entire measurement."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def read_occupancy(path, *, tail_bytes: int = TAIL_BYTES) -> Occupancy:
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
        size = p.stat().st_size
    except OSError:
        return _UNKNOWN_UNREADABLE

    # Tail first. These files reach 23 MB in real use and this runs at the end of
    # every single turn; reading the whole thing would allocate all of it and
    # split it into ~100k strings each time, on the one path that has to be fast.
    # The newest assistant turn is at the end by construction, so the tail is
    # almost always enough — and when it is not, the full read below still runs,
    # which keeps this an optimisation rather than a truncation.
    if tail_bytes and size > tail_bytes:
        found = _scan(_read_tail(p, tail_bytes))
        if found is not None:
            return found

    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return _UNKNOWN_UNREADABLE
    return _scan(text.splitlines()) or _UNKNOWN_NO_USAGE


def _read_tail(p: Path, tail_bytes: int) -> list[str]:
    """The last *tail_bytes* of the file, as whole lines.

    The window lands mid-line essentially always, so the first fragment is
    dropped — a half-line cannot be valid JSON anyway, but dropping it makes
    that a decision rather than a lucky accident."""
    try:
        with p.open("rb") as fh:
            fh.seek(-tail_bytes, 2)
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="ignore").splitlines()
    return lines[1:] if lines else []


def _scan(lines) -> Occupancy | None:
    """Newest usable assistant row in *lines*, or None if there isn't one."""
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

    return None
