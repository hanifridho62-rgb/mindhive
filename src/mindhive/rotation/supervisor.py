# src/mindhive/rotation/supervisor.py
"""`hive session run` — the outer loop around an interactive Claude Code session.

Spawn the CLI, wait, and look for the artifact the Stop hook may have left. No
artifact means the user quit; an artifact means the session filled up, so
consolidate what it learned and launch a successor seeded from the ledger.

Two design points that are easy to get wrong.

**Stdio is inherited, not piped.** `Popen(argv)` with no stdin/stdout/stderr
arguments hands the child the parent's console handles directly, so the TUI owns
a real console — full-screen redraw, key handling and ANSI all work because
nothing sits in the middle. Piping is what breaks TUIs.
The cost is that we cannot read the child's output — which is fine, because the
artifact was designed to carry the signal instead. There is no `pty` on Windows
and none is wanted: the fix for a broken TUI is not a cleverer pipe, it is no
pipe at all.

**The child is opted INTO rotation; nothing else is.** The Stop hook is
registered globally but stays inert unless `MINDHIVE_ROTATION` is set, and the
supervisor sets it only in the environment of the children it spawns. So a bare
`claude` session is untouched by all of this, and rotation is a property of
running under the supervisor rather than of having installed the hook. That is
what makes registering it safe.
"""
import os
import subprocess
from dataclasses import dataclass
from time import monotonic

from . import artifact

# Rotations allowed inside SPIN_WINDOW_S before the loop is judged to be
# spiralling. A healthy long session rotates on the order of hours; three inside
# three minutes means the threshold is mis-set or the transcript is stuck, and
# relaunching forever would spend the account on nothing.
MAX_ROTATIONS = 3
SPIN_WINDOW_S = 180.0


@dataclass(frozen=True)
class SessionResult:
    exit_code: int
    spawns: int
    rotations: int
    reason: str          # user_quit | spin_guard | spawn_failed
    detail: str = ""


def _child_env(env=None):
    """The child's environment: ours, plus rotation switched on.

    Deliberately NOT `claude_cli._child_env` — that forces MINDHIVE_HEADLESS=1,
    which every hooks/*.ps1 treats as "exit immediately". A headless child is
    exactly what we do not want here: the Stop hook is the thing that makes this
    session rotate, so it has to be allowed to fire."""
    merged = dict(os.environ if env is None else env)
    merged["MINDHIVE_ROTATION"] = "1"
    return merged


def default_spawn(handoff: str | None, *, cmd: str = "claude", extra=()) -> int:
    """Launch the interactive CLI and block until it exits.

    stdin/stdout/stderr are left alone on purpose — see the module docstring.
    The handoff rides in `--append-system-prompt`, which is bounded by
    construction (it is built from capped briefing sections); the loop driver
    documents why an unbounded string must never go into argv on Windows."""
    from ..claude_cli import resolve_exe

    argv = [resolve_exe(cmd)]
    if handoff:
        argv += ["--append-system-prompt", handoff]
    argv += list(extra)
    proc = subprocess.Popen(argv, env=_child_env())
    return proc.wait()


def default_reseed(pending: dict, *, project: str | None = None) -> str:
    """Compact the ledger and build the successor's opening context.

    Consolidation runs first so the briefing the successor reads is the merged
    one, not the sprawl the dead session left behind."""
    from ..briefing import briefing as build_briefing
    from ..consolidate import consolidate

    try:
        consolidate(project=project)
    except Exception:                                       # noqa: BLE001
        pass            # best-effort: a stale briefing beats no successor
    text = build_briefing().read_text(encoding="utf-8")
    return (
        "This session is the successor to one that reached its context limit "
        f"({pending.get('occupancy')} of {pending.get('threshold')} tokens). "
        "Continue that work from the state below; do not restart it.\n\n" + text
    )


def run(*, project: str | None = None, spawn=None, now=None, reseed=None,
        max_rotations: int = MAX_ROTATIONS,
        window_s: float = SPIN_WINDOW_S) -> SessionResult:
    """Spawn, wait, rotate, repeat — until the user quits or the guard trips.

    `spawn`, `now` and `reseed` are injected so the loop's decisions can be
    tested without launching a CLI or sleeping through a spin window."""
    spawn = spawn or default_spawn
    now = now or monotonic
    reseed = reseed or (lambda pending: default_reseed(pending, project=project))

    handoff: str | None = None
    spawns = 0
    rotations = 0
    recent: list[float] = []

    while True:
        # Clear BEFORE spawning, never after: a crash last run can leave an
        # artifact behind, and reading it as this run's rotation would make the
        # loop double-spawn on every start.
        artifact.clear(project)
        spawns += 1
        try:
            spawn(handoff)
        except KeyboardInterrupt:
            # Inherited stdio puts the child in our console group, so Ctrl+C
            # reaches both. Relaunching here would make the loop unquittable.
            return SessionResult(0, spawns, rotations, "user_quit", "interrupted")
        except OSError as e:
            return SessionResult(1, spawns, rotations, "spawn_failed", str(e))

        pending = artifact.read(project)
        if pending is None:
            return SessionResult(0, spawns, rotations, "user_quit")
        artifact.clear(project)
        rotations += 1

        stamp = now()
        recent = [t for t in recent if stamp - t <= window_s]
        recent.append(stamp)
        if len(recent) >= max_rotations:
            return SessionResult(
                1, spawns, rotations, "spin_guard",
                f"{len(recent)} rotations within {window_s:g}s "
                f"(limit {max_rotations}); last at "
                f"{pending.get('occupancy')}/{pending.get('threshold')} tokens",
            )

        try:
            handoff = reseed(pending)
        except Exception:                                   # noqa: BLE001
            handoff = None      # unseeded successor beats no successor
