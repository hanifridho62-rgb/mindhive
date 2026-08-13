# src/mindhive/verify.py
"""Mechanical test-run records: the evidence a "the suite is green" claim can
be checked against.

`directives.OPERATING_SPEC` §0 forbids reporting a result you did not observe.
Enforcing that needs something the system observed, and before this module
nothing in the store recorded one: a grep of `src/mindhive/` found no event
type and no parser holding a pytest count anywhere. So "3518 tests pass" was
unfalsifiable — which is exactly how a fabricated baseline survived several
sessions unchallenged.

`run_and_record` runs a command as a real subprocess, reads its real exit code,
and appends one `verification` event stamped `verified_by: "python"`. That
stamp means here what it means in `mindnet/executor.py`, which sets its own
`VERIFIED_BY = "python"` only after a subprocess ran and an exit code was read.
There is one notion of mechanical verification in this codebase and this module
does not invent a second, weaker one.

**The stamp attaches to the exit code, not to the counts.** The exit code was
observed. The counts are a best-effort parse of human-readable output, and when
the parse fails the counts are recorded ABSENT rather than zero — a broken
parse must not be able to read as "a suite where nothing passed".

**Two validators, deliberately, mirroring `mindnet/phases.py`.**
`validate_payload` guards the arbitrary-payload append surfaces (`hive append`,
`POST /api/append`) and REFUSES any payload carrying `verified_by`, so an agent
cannot hand-write a mechanical verification nothing performed. `validate_stored`
is the writer's shape and REQUIRES the stamp. Registering the writer's shape on
the append surface would invert the honesty rule, which is the mistake
`phases.py`'s comment already warns about.

**Output goes to `runlog`, not the ledger.** `runlog.py` is explicit that
multi-kilobyte transcripts on the append-only chain violate memo hygiene and
blow the context budgets `context.py` polices. The event keeps a bounded
excerpt plus the artifact path.

**Never called from a hook.** `premise.py` reads the LAST recorded run; nothing
on the `UserPromptSubmit` path may produce one. A gate that could spawn a
multi-minute subprocess on a keystroke is a denial of service on the user's own
session.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from . import events as ev_mod
from . import runlog

VERIFIED_BY = "python"

# Keywords pytest reports counts against. `errors?` covers both spellings.
_COUNT_RE = re.compile(r"\b(\d{1,7})\s+(passed|failed|errors?|skipped|xfailed|"
                       r"xpassed|deselected)\b")

# Only the tail of the output is parsed. pytest's own summary lives there, and
# scanning the whole transcript would let a failure message that happens to
# quote "3 passed" become the recorded result.
_SUMMARY_TAIL_LINES = 8


def default_argv(root: Path | None = None) -> list:
    """The repo's own suite. `sys.executable` rather than a bare `python`:
    `python` is not on PATH in this environment's Git Bash, and the venv
    interpreter is what has the dependencies."""
    base = Path(root) if root else Path(__file__).resolve().parents[2]
    return [sys.executable, "-m", "pytest", "-q", str(base / "tests")]


def parse_summary(text) -> dict:
    """Counts from pytest's summary line, or ``{}`` when nothing parseable is
    there. Total — never raises, never guesses. An absent key means "not
    observed", which is a different fact from zero and must stay different."""
    if not isinstance(text, str) or not text.strip():
        return {}
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    counts: dict[str, int] = {}
    for line in lines[-_SUMMARY_TAIL_LINES:]:
        for m in _COUNT_RE.finditer(line):
            key = m.group(2)
            if key == "error":
                key = "errors"
            try:
                counts[key] = int(m.group(1))
            except ValueError:                      # pragma: no cover
                continue
    return counts


def _base_shape(payload) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("verification payload must be an object")
    argv = payload.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ValueError("verification requires a non-empty argv list — the "
                         "command that was actually run")
    code = payload.get("exit_code")
    if not isinstance(code, int) or isinstance(code, bool):
        raise ValueError("verification requires an integer exit_code — the "
                         "real exit status of that command")
    return payload


def validate_payload(payload) -> None:
    """Contract for the ARBITRARY-payload append surfaces.

    Refuses any payload carrying `verified_by`. A `verification` event asserts
    that a machine ran something and read the result; a hand-written append has
    performed no such thing, and letting one carry the stamp would make the
    record worthless for exactly the claim it exists to settle. This mirrors
    `phases.validate_payload`, which refuses `verified_by` for the same
    reason."""
    _base_shape(payload)
    if "verified_by" in payload:
        raise ValueError(
            "verification cannot be appended with a `verified_by` stamp — the "
            "stamp is applied only by a real subprocess run. Run the suite "
            "(`hive selftest`) instead of asserting its result")


def validate_stored(payload) -> None:
    """The WRITER's shape: the stamp is required here."""
    _base_shape(payload)
    if payload.get("verified_by") != VERIFIED_BY:
        raise ValueError(
            f"a stored verification must be stamped verified_by="
            f"{VERIFIED_BY!r} — nothing else may record a mechanical run")


def _stream(argv, echo: bool, emit=None):
    """Run *argv* and return ``(exit_code, output, error)``.

    Line-streamed rather than captured wholesale so a four-minute suite still
    prints as it goes; the buffer is what becomes the artifact. argv list only
    — `shell=True` appears nowhere in this module and a test pins that."""
    chunks: list[str] = []
    kept = 0
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError) as e:
        # A missing program is a real, recordable outcome — not a crash for the
        # caller to handle. 127 is the shell convention for "not found".
        return 127, "", f"{type(e).__name__}: {e}"
    with proc:
        if proc.stdout is not None:
            for line in proc.stdout:
                # Bounded in MEMORY, not just at write time. `runlog` caps the
                # artifact when it is written, but the buffer that feeds it was
                # unbounded — a runaway suite printing gigabytes would grow this
                # process until it died, losing the exit code that is the whole
                # point. Past the cap we keep echoing and keep DRAINING the pipe
                # (stopping the read would block the child on a full pipe and
                # hang the run), we just stop retaining.
                if kept < runlog.MAX_ARTIFACT_BYTES:
                    chunks.append(line)
                    kept += len(line)
                if emit is not None:
                    # A second reader, line by line for the same reason `echo`
                    # flushes: the dashboard's job pane is polled while the run is
                    # still going. Its failure must not take the run down — the
                    # exit code is the point, and a broken sink is not a reason to
                    # lose it.
                    try:
                        emit(line)
                    except Exception:           # noqa: BLE001 — see comment
                        emit = None
                if echo:
                    sys.stdout.write(line)
                    # Explicit, because "streams as it goes" is otherwise only
                    # true on a terminal. Redirected to a file or a CI log,
                    # Python block-buffers at 8KB, so a four-minute suite shows
                    # nothing at all until it is already over — measured: the
                    # capture file sat at 0 bytes for the whole run. The flush
                    # costs a syscall per line on a command that is about to
                    # spend minutes in a subprocess.
                    sys.stdout.flush()
        code = proc.wait()
    return code, "".join(chunks), ""


def run_and_record(argv=None, project=None, root=None, echo: bool = True,
                   emit=None) -> dict:
    """Run a test command, record the result, return
    ``{exit_code, counts, error, artifact, excerpt}``.

    The returned dict is a convenience for the caller; the EVENT is the
    artifact. Never raises for a failing or missing command — a failure is a
    result to record, not an exception to hide."""
    cmd = list(argv) if argv else default_argv(root)
    code, output, error = _stream(cmd, echo, emit)
    counts = parse_summary(output)
    excerpt = output.strip()[:runlog.EXCERPT_CHARS]

    artifact = None
    try:
        key = f"verify-{ev_mod.now_iso()}"
        artifact = runlog.write_once(key, output, project=project)
    except Exception:                                   # noqa: BLE001
        artifact = None                 # evidence lost; the excerpt survives

    summary = (f"{cmd[0]}: exit {code}"
               + (f" — {counts.get('passed', 0)} passed, "
                  f"{counts.get('failed', 0)} failed" if counts
                  else " — counts not parseable"))
    payload = {
        "text": summary,
        "argv": [str(c) for c in cmd],
        "exit_code": int(code),
        "counts": counts,
        "excerpt": excerpt,
        "verified_by": VERIFIED_BY,
    }
    if artifact:
        payload["artifact"] = artifact
    if error:
        payload["error"] = error
    validate_stored(payload)
    try:
        ev_mod.append_event("verification", payload, tags=["verify", "auto"],
                            project=project)
    except Exception:                                   # noqa: BLE001
        pass          # a down ledger must not turn a real run into a crash
    return {"exit_code": int(code), "counts": counts, "error": error,
            "artifact": artifact, "excerpt": excerpt}


def last_run(evs) -> dict | None:
    """The most recent recorded run's payload, or None when the store has
    none. None is the honest answer for an empty store — `premise` renders it
    as "no recorded run to check against", never as a contradiction."""
    latest = None
    for e in (evs or []):
        if getattr(e, "type", None) != "verification":
            continue
        if not isinstance(getattr(e, "payload", None), dict):
            continue
        latest = e.payload
    return latest
