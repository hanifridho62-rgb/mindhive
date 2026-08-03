# src/mindhive/rotation/hook.py
"""The Stop-hook decision: measure the session, and signal when it is full.

Registered globally, this runs at the end of EVERY agent turn in every Claude
Code session on the machine. Three consequences shape the whole file.

**It cannot raise.** A traceback here sits between the model finishing and the
user seeing the answer, in every session at once. `hook_decision` catches
everything and returns None — silence — because the failure mode of a measuring
hook should be "we did not measure", never "your work stopped".

**It must never block.** The Stop contract's `{"decision": "block"}` means *do
not stop, keep working*; emitting it on a full context would pin the session
open rather than rotating it. This hook emits at most `additionalContext`, which
is advice. The actual rotation signal is the artifact file — the only thing that
survives the child process exiting.

**It is off until you turn it on.** Registering the hook and activating it are
separate steps (`MINDHIVE_ROTATION`), so a bad build is inert rather than
machine-wide. Registration is reversible by editing one file; a wedged Claude
Code on every project is not reversible from inside Claude Code.
"""
import json
import os

from . import artifact, occupancy, window
from ..loop.config import LoopConfig

_TRUTHY = {"1", "true", "on", "yes"}

_ADVICE = (
    "Context has passed the smart-zone boundary ({occupancy:,} of {threshold:,} "
    "tokens). A successor session is queued. Wrap up the current thought, write "
    "anything the next session needs into the ledger, and stop — do not start "
    "new work in this session."
)


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("MINDHIVE_ROTATION", "")).strip().lower() in _TRUTHY


def _ratio(env) -> float:
    raw = env.get("MINDHIVE_ROTATION_RATIO")
    try:
        return float(raw) if raw is not None else LoopConfig().smart_zone_ratio
    except (TypeError, ValueError):
        return LoopConfig().smart_zone_ratio


def _cap(env) -> int:
    raw = env.get("MINDHIVE_ROTATION_CAP")
    try:
        return int(raw) if raw is not None else window.DEFAULT_CAP
    except (TypeError, ValueError):
        return window.DEFAULT_CAP


def hook_decision(raw: str, *, project: str | None = None, env=None) -> dict | None:
    """Decide what — if anything — to say at the end of this turn.

    Returns None for silence (the overwhelmingly common case), or the
    `hookSpecificOutput` envelope when the session has crossed the boundary and
    the artifact has been written.

    The blanket except is deliberate and is the point of the function: see the
    module docstring. A narrower catch would let some unanticipated failure
    through into a surface where any exception is a user-visible outage.
    """
    env = os.environ if env is None else env
    try:
        if not enabled(env):
            return None

        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            return None

        # Another hook is already holding this stop open. Adding to it compounds
        # whatever it is doing, and the field exists precisely to stop that.
        if payload.get("stop_hook_active"):
            return None

        # `agent_id` appears only inside a subagent. A subagent has its own
        # context window; rotating the parent because a child filled up would
        # discard a session that is nowhere near full. Note this is the narrow
        # test on purpose: `agent_type` is also set on a main session launched
        # with --agent, so gating on it would silence normal sessions.
        if payload.get("agent_id"):
            return None

        occ = occupancy.read_occupancy(payload.get("transcript_path"))
        if not occ.known:
            return None            # cannot measure -> do not guess

        threshold = window.threshold(occ.model, _ratio(env), _cap(env))
        if occ.tokens < threshold:
            return None

        artifact.write(session_id=payload.get("session_id"),
                       occupancy=occ.tokens, threshold=threshold,
                       model=occ.model, project=project)
        return {"hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": _ADVICE.format(occupancy=occ.tokens,
                                                threshold=threshold),
        }}
    except Exception:                                       # noqa: BLE001
        return None
