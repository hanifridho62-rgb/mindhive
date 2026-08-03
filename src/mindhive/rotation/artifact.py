# src/mindhive/rotation/artifact.py
"""The ROTATION_PENDING file — the only channel between hook and supervisor.

A Stop hook has exactly two outcomes available to it: allow the stop, or block
it (which *continues* the conversation — the opposite of rotating). Neither one
can end a session. So the hook cannot tell the supervisor anything through the
protocol; it has to leave something behind that outlives the child process.

That is this file. The hook writes it when occupancy crosses the threshold; the
supervisor reads it after the child exits, whatever killed the child. A file
survives a clean exit, a crash, and a kill alike, which no in-band signal does.

`read` is total for the same reason `occupancy` is: the artifact gets written by
a process that may be torn down mid-write, so a truncated one is a normal event,
not an exceptional one. A corrupt artifact reads as "no rotation pending" — the
conservative direction, since the cost is one missed rotation rather than a
crashed supervisor or an unbounded relaunch loop.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from .. import paths

NAME = "ROTATION_PENDING"


def path(project: str | None = None) -> Path:
    """Always resolved through `paths.project_dir`, never hand-built from a
    home directory. That indirection is what keeps the test suite's isolated
    store honest — `paths` reads MINDHIVE_HOME at call time, so a test can
    redirect it and a hand-built '~/.mindhive' path cannot be redirected."""
    return paths.project_dir(project) / NAME


def write(*, session_id, occupancy: int, threshold: int, model,
          project: str | None = None, now: str | None = None) -> Path:
    """Record that this session crossed the smart-zone boundary.

    The numbers ride along rather than just a marker: the supervisor reports
    them when the spin guard trips, and a guard that says "rotated 3 times in
    40 seconds at 203820/500000" is debuggable where a bare count is not."""
    target = path(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "occupancy": occupancy,
        "threshold": threshold,
        "model": model,
        "ts": now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def read(project: str | None = None) -> dict | None:
    """The pending rotation, or None. Never raises — see the module docstring."""
    target = path(project)
    try:
        if not target.is_file():
            return None
        obj = json.loads(target.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def pending(project: str | None = None) -> bool:
    return read(project) is not None


def clear(project: str | None = None) -> None:
    """Remove the artifact. A missing file is the normal case, not an error —
    the supervisor clears before every spawn so a stale artifact from a previous
    run can never be mistaken for this run's rotation."""
    try:
        path(project).unlink(missing_ok=True)
    except OSError:
        pass
