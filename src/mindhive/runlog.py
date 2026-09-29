# SPDX-License-Identifier: Apache-2.0
# src/mindhive/runlog.py
"""Run-artifact store: the durable home for one run's full output.

Output durability is a persistence problem, not a display problem. Parallel work
in this environment discarded produced text on three of its four exit paths — a
regex-captured line for loop iterations, a 2000-char excerpt over a volatile
in-memory ring for successful workspace runs, and nothing at all for failed ones.
This module is the one place that keeps everything:

    ~/.mindhive/<project>/runs/<run_key>.txt

``run_key`` is ``ws-<agent_id>-<n>`` for workspace runs (``n`` is a monotonic
per-agent run counter — one ``send()`` is one run and one artifact) and
``loop-<run_id>-<iter>`` for loop iterations. Deterministic, collision-free, and
greppable.

**Not the ledger, deliberately.** Events are canonical JSON on a hash chain
feeding projections, briefings, recall, and the graph; multi-kilobyte transcripts
there would violate the memo-hygiene SOP and blow the context budgets context.py
exists to police. The event keeps a bounded excerpt plus the artifact path; the
artifact keeps the rest.

**Artifacts are evidence, not projections.** They are explicitly NOT rebuildable
from the ledger — the same status enrich.json and context_budget.json hold.
Losing one loses evidence and breaks nothing: every read here is total (missing,
unreadable, or unwritable degrades to ``None``, never a raise) and every consumer
falls back to the excerpt.

**Bounded, and it says so.** Per-artifact cap MAX_ARTIFACT_BYTES; on hitting it
the artifact keeps the head and records the loss as a literal marker line inside
the file rather than silently stopping. Memory is bounded by the same cap, so a
runaway agent cannot grow the writer without limit. Retention is pruned only by
the opt-in ``hive gc`` (list_artifacts/total_bytes) — never automatically:
deleting the operator's only evidence must be a deliberate act.

**In flight.** append() buffers and flushes to a ``<run_key>.txt.part`` sidecar
every FLUSH_BYTES, so text produced before a kill is already on disk; finalise()
writes the real artifact once via paths.write_text_atomic (tmp sibling +
os.replace) and removes the sidecar. read() prefers the finalised artifact and
falls back to the sidecar, so an interrupted run is still retrievable. A
concurrent reader therefore never sees a torn artifact — the invariant that
matters most when 16 agents finish at once.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

from . import paths

MAX_ARTIFACT_BYTES = 1024 * 1024     # 1 MiB per run; head kept, tail announced
EXCERPT_CHARS = 2000                 # in-event excerpt: the pre-MN-0 cap, unchanged
FLUSH_BYTES = 64 * 1024              # in-flight sidecar flush interval
TRUNCATION_MARKER = "--- truncated: {dropped} bytes dropped ---"

# A run_key becomes a FILE NAME. Ids are secrets.token_hex / uuid4 hex today, but
# this is the boundary, so it enforces the charset instead of trusting callers:
# separators, dots, and traversal cannot survive.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _safe(part) -> str:
    return _UNSAFE.sub("-", str(part))


def runs_dir(project: str | None = None) -> Path:
    """Where artifacts live. paths.py owns the store layout (same shape as
    events_dir); this module owns everything inside the directory."""
    return paths.runs_dir(project)


# The `ws-` family, defined once here because this module has spelled it since
# before anything else needed it, and because `_safe` above is exactly the guard
# a value that becomes a FILE NAME wants — `events.append_event` writes
# `<session_id>.jsonl`, so the session id is one of those too.
SESSION_PREFIX = "ws-"


def workspace_key(agent_id, n) -> str:
    """One workspace ``send()`` — an Agent may be sent many prompts over its
    life and each is its own run."""
    return f"{SESSION_PREFIX}{_safe(agent_id)}-{_safe(n)}"


def workspace_session(agent_id) -> str:
    """The `MINDHIVE_SESSION` a board agent's child writes under.

    Per AGENT, not per send: an Agent holds one `proc` at a time so its runs
    cannot interleave in the segment, and one stream per agent is what the
    Sessions view reads. `workspace.send` had no overlay at all before this, so
    a child inherited the OPERATOR's session — it appended into the operator's
    ledger segment (the concurrent-writer tear `events._append_lock` exists
    for) and every row it wrote was attributed to a human. Recognised as an
    agent by `advice.ORCHESTRATION_PREFIXES`, pinned by
    `test_the_workspace_session_prefix_is_covered`."""
    return f"{SESSION_PREFIX}{_safe(agent_id)}"


def loop_key(run_id, iteration) -> str:
    return f"loop-{_safe(run_id)}-{_safe(iteration)}"


def artifact_path(run_key, project: str | None = None) -> Path:
    return runs_dir(project) / f"{_safe(run_key)}.txt"


class RunLog:
    """Buffered writer for one run's output.

    Thread-safe: the workspace reader thread appends while the dashboard thread
    may read, and 16 of these can finalise at once.
    """

    def __init__(self, run_key, project: str | None = None, max_bytes=None):
        self.run_key = str(run_key)
        self.path = artifact_path(run_key, project)
        self.part_path = self.path.with_name(self.path.name + ".part")
        self.dropped_bytes = 0            # refused by the cap, reported in-file
        self._max_bytes = max_bytes       # None -> the module cap, read live
        self._chunks: list = []           # the accepted head, in order
        self._flushed = 0                 # chunks already in the sidecar
        self._pending = 0                 # unflushed bytes
        self._bytes = 0                   # accepted bytes so far
        self._final = False
        self._lock = threading.Lock()

    # --- writing ----------------------------------------------------------
    def _cap(self) -> int:
        return MAX_ARTIFACT_BYTES if self._max_bytes is None else self._max_bytes

    def append(self, text) -> None:
        """Accept more output. Never raises: a failed sidecar flush keeps the
        text buffered for finalise, and text past the cap is counted, not lost
        silently."""
        if not text:
            return
        with self._lock:
            if self._final:
                return
            data = str(text)
            size = len(data.encode("utf-8"))
            room = self._cap() - self._bytes
            if room <= 0:
                self.dropped_bytes += size
                return
            if size > room:
                # cut on a character boundary that fits the byte budget
                kept = data.encode("utf-8")[:room].decode("utf-8", "ignore")
                self.dropped_bytes += size - len(kept.encode("utf-8"))
                data, size = kept, len(kept.encode("utf-8"))
                if not data:
                    return
            self._chunks.append(data)
            self._bytes += size
            self._pending += size
            if self._pending >= FLUSH_BYTES:
                self._flush_locked()

    def _write_part(self, text: str) -> None:
        """Append to the in-flight sidecar. Its own method so a test can make
        the disk fail here without touching the finalise path."""
        self.part_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.part_path, "a", encoding="utf-8") as f:
            f.write(text)

    def _flush_locked(self) -> None:
        pending = "".join(self._chunks[self._flushed:])
        if not pending:
            return
        try:
            self._write_part(pending)
        except OSError:
            return                        # keep it buffered; finalise retries
        self._flushed = len(self._chunks)
        self._pending = 0

    def text(self) -> str:
        """Everything accepted so far (the head), without the marker."""
        with self._lock:
            return "".join(self._chunks)

    def excerpt(self, limit=None) -> str:
        """The bounded copy that rides the ledger event."""
        return self.text().strip()[:EXCERPT_CHARS if limit is None else limit]

    def finalise(self) -> str | None:
        """Write the artifact atomically and drop the sidecar. Returns the path
        as a str, or None when there was nothing to keep or the disk refused —
        the caller degrades to the excerpt, it does not fail the run."""
        with self._lock:
            if self._final:
                return str(self.path) if self.path.exists() else None
            self._final = True
            body = "".join(self._chunks)
            if self.dropped_bytes:
                # a bounded view must announce its bound: record the loss inside
                # the file instead of stopping mid-sentence
                body += "\n" + TRUNCATION_MARKER.format(
                    dropped=self.dropped_bytes) + "\n"
            if not body.strip():
                self._discard_part()
                return None
            try:
                paths.write_text_atomic(self.path, body)
            except OSError:
                return None               # evidence lost; the excerpt survives
            self._discard_part()
            return str(self.path)

    def _discard_part(self) -> None:
        try:
            self.part_path.unlink()
        except OSError:
            pass


def write_once(run_key, text, project: str | None = None) -> str | None:
    """One-shot artifact for output already held in full (a loop iteration's
    reply). Returns the path, or None when there was nothing to keep."""
    rl = RunLog(run_key, project=project)
    rl.append(text)
    return rl.finalise()


def read(run_key, project: str | None = None) -> str | None:
    """The run's output: the finalised artifact, else whatever an interrupted
    run flushed to its sidecar, else None. Total — never raises."""
    p = artifact_path(run_key, project)
    for candidate in (p, p.with_name(p.name + ".part")):
        try:
            return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return None


def list_artifacts(project: str | None = None) -> list:
    """Finalised artifacts with sizes, oldest key first — the enumeration
    ``hive gc`` reports from. In-flight sidecars are excluded: a run that has
    not ended yet is not garbage."""
    d = runs_dir(project)
    out = []
    try:
        entries = sorted(d.glob("*.txt"))
    except OSError:
        return out
    for f in entries:
        try:
            size = f.stat().st_size
        except OSError:
            continue
        out.append({"run_key": f.stem, "path": str(f), "bytes": size})
    return out


def total_bytes(project: str | None = None) -> int:
    return sum(r["bytes"] for r in list_artifacts(project))
