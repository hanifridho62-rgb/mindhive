# SPDX-License-Identifier: Apache-2.0
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

`_child_env` sets a second marker (`hook.SUPERVISED_ENV`) for the same reason
inverted: a session may have rotation switched on with no supervisor waiting on
it, and the hook's advice used to promise a successor to that session anyway.
This module is the only place the marker is set, because this module is the only
place where "someone is waiting on you" is a fact rather than an assumption.
"""
import os
import subprocess
from dataclasses import dataclass
from time import monotonic

from . import artifact, hook

# Rotations allowed inside SPIN_WINDOW_S before the loop is judged to be
# spiralling. A healthy long session rotates on the order of hours; three inside
# three minutes means the threshold is mis-set or the transcript is stuck, and
# relaunching forever would spend the account on nothing.
MAX_ROTATIONS = 3
SPIN_WINDOW_S = 180.0

# Absolute backstop, independent of the window above. A spiral that rotates just
# SLOWER than the window — every 200s against a 180s window — never puts two
# rotations in the same window and so never trips the time-based guard, which
# would leave the loop relaunching forever. That is the exact failure the guard
# exists to prevent, so it needs a bound that does not depend on rate. Generous:
# an all-day session rotating properly lands in the tens.
MAX_TOTAL_ROTATIONS = 40


@dataclass(frozen=True)
class SessionResult:
    exit_code: int
    spawns: int
    rotations: int
    reason: str          # user_quit | spin_guard | spawn_failed
    detail: str = ""


def prune_window(recent, stamp: float, window_s: float) -> list[float]:
    """Rotation timestamps still inside the sliding window, plus *stamp*.

    Extracted so the loop's one piece of accumulating state is directly
    testable. This is what keeps the supervisor's memory flat across a long
    session: everything older than the window is dropped on every rotation, so
    the list is bounded by the rotation RATE, never by how long the loop has
    been running. The loop then trips at `max_rotations` entries, which bounds
    it a second time — a list that cannot exceed a handful of floats."""
    return [t for t in recent if stamp - t <= window_s] + [stamp]


def spin_verdict(rotations: int, recent, *, max_rotations: int,
                 max_total_rotations: int, window_s: float,
                 pending: dict | None = None) -> str | None:
    """Why this loop looks like a spiral, or None if it looks healthy.

    Both bounds live here because they answer ONE question — "should this stop
    relaunching?" — and splitting them across two branches in the loop body made
    the loop read as if two unrelated things could go wrong. They are two
    symptoms of the same failure, and they need each other: the sliding window
    catches a fast spiral, and the total catches a slow one that never puts two
    rotations in the same window and so slips past the window entirely.

    Returns the detail string rather than a bool so the caller reports numbers
    instead of the fact that a limit exists."""
    if rotations >= max_total_rotations:
        return (f"{rotations} rotations total (limit {max_total_rotations}); "
                f"too slow to trip the {window_s:g}s window but not a healthy "
                f"session either")
    if len(recent) >= max_rotations:
        at = ""
        if pending:
            at = (f"; last at {pending.get('occupancy')}/"
                  f"{pending.get('threshold')} tokens")
        return (f"{len(recent)} rotations within {window_s:g}s "
                f"(limit {max_rotations}){at}")
    return None


def _child_env(env=None):
    """The child's environment: ours, plus rotation on and headless off.

    Deliberately NOT `claude_cli._child_env` — that FORCES MINDHIVE_HEADLESS=1,
    and every hooks/*.ps1 (including stop.ps1) treats that as "exit immediately".
    A headless child is the one thing this must not produce: the Stop hook is
    what makes the session rotate, so it has to be allowed to fire.

    The flag is also actively removed rather than merely not set. It is
    inherited, so running `hive session run` from inside a headless session — a
    reflection run, an autoresume delivery, an outer loop — would otherwise pass
    it down and silently disable rotation for every child. Nothing would error;
    sessions would just never rotate, which is the failure you discover weeks
    later."""
    merged = dict(os.environ if env is None else env)
    merged.pop("MINDHIVE_HEADLESS", None)
    merged["MINDHIVE_ROTATION"] = "1"
    # The second marker is what lets the hook's advice tell the truth. This
    # function is the ONLY place it is ever set: it means "a supervisor is
    # waiting on you", and this is the only code from which that is a fact. The
    # line above cannot carry it, because a user who switches rotation on by
    # hand sets that variable too and no one is waiting on them.
    merged[hook.SUPERVISED_ENV] = "1"
    # Not a rotation switch: the successor session runs Python tools in the
    # operator's tree and should not litter it with `__pycache__` (MEB risk 7).
    # A default only -- whatever the operator already set is left alone.
    from .. import paths
    return paths.no_bytecode_env(merged)


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


def active_ask(project: str | None = None) -> str:
    """The original ask of the active roadmap, if there is one.

    A successor reseeded from the briefing alone knows what HAPPENED but not
    what it was FOR — and a session that has lost the goal drifts toward
    whatever the last few events were about. The roadmap already stores the ask
    verbatim, so the handoff carries the destination alongside the progress.

    Best-effort and total: no roadmap, a corrupt fold, an unreadable store — any
    of them yield an empty string rather than costing the successor its whole
    context."""
    try:
        from .. import events, roadmap
        plans = roadmap.fold(events.read_all(project))
    except Exception:                                       # noqa: BLE001
        return ""
    for record in plans.values():
        if not isinstance(record, dict) or record.get("status") != "active":
            continue
        prompt = record.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            steps = record.get("steps") or []
            done = sum(1 for s in steps
                       if isinstance(s, dict) and s.get("status") == "done")
            progress = f" ({done}/{len(steps)} steps done)" if steps else ""
            return f"{prompt.strip()}{progress}"
    return ""


def default_reseed(pending: dict, *, project: str | None = None) -> str:
    """Compact the ledger and build the successor's opening context.

    Consolidation runs first so the briefing the successor reads is the merged
    one, not the sprawl the dead session left behind."""
    from .. import projector
    from ..briefing import briefing as build_briefing
    from ..consolidate import consolidate

    # Consolidate, then rebuild projections, then read the briefing — in that
    # order. Each step feeds the next: merging near-duplicates first means the
    # projections fold a compacted ledger, and rebuilding before reading means
    # the successor gets the compacted view rather than the dead session's
    # sprawl. Both are best-effort; a stale briefing beats no successor at all.
    for step in (lambda: consolidate(project=project), projector.project):
        try:
            step()
        except Exception:                                   # noqa: BLE001
            pass

    header = ("This session is the successor to one that reached its context "
              f"limit ({pending.get('occupancy')} of {pending.get('threshold')} "
              "tokens). Continue that work; do not restart it.\n\n")
    ask = active_ask(project)
    if ask:
        header += f"THE ASK (unchanged by the rotation):\n{ask}\n\n"
    return header + build_briefing().read_text(encoding="utf-8")


def run(*, project: str | None = None, spawn=None, now=None, reseed=None,
        max_rotations: int = MAX_ROTATIONS,
        window_s: float = SPIN_WINDOW_S,
        max_total_rotations: int = MAX_TOTAL_ROTATIONS) -> SessionResult:
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
        except Exception as e:                              # noqa: BLE001
            # OSError covers the documented spawn failures (binary missing,
            # exec denied). The broad catch is for everything else: this loop IS
            # the user's session wrapper, so an unanticipated exception must
            # become a reported stop, not a traceback that takes their whole
            # working session down with it.
            return SessionResult(1, spawns, rotations, "spawn_failed",
                                 f"{type(e).__name__}: {e}")

        pending = artifact.read(project)
        if pending is None:
            return SessionResult(0, spawns, rotations, "user_quit")
        artifact.clear(project)
        rotations += 1

        recent = prune_window(recent, now(), window_s)
        tripped = spin_verdict(rotations, recent, max_rotations=max_rotations,
                               max_total_rotations=max_total_rotations,
                               window_s=window_s, pending=pending)
        if tripped:
            return SessionResult(1, spawns, rotations, "spin_guard", tripped)

        try:
            handoff = reseed(pending)
        except Exception:                                   # noqa: BLE001
            handoff = None      # unseeded successor beats no successor
