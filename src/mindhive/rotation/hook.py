# SPDX-License-Identifier: Apache-2.0
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

**It must not overstate what happens next.** Whether a successor session exists
is not a property of the boundary being crossed — it is a property of who
launched this session, and only the supervisor knows that. `MINDHIVE_ROTATION`
cannot answer it, because the supervisor sets that variable for its children and
a user switching the feature on by hand sets the same variable; a hook reading
only that has to guess. So the advice branches on a second marker that only
`supervisor._child_env` sets: supervised, it says a successor is queued, which is
then true; unsupervised, it says plainly that nothing is queued, names where the
crossing was recorded, and names the command that would queue one.
"""
import json
import os

from . import artifact, occupancy, window
from .. import outcomes
from ..loop.config import LoopConfig

_TRUTHY = {"1", "true", "on", "yes"}

# Set ONLY by `supervisor._child_env`. It answers the one question
# `MINDHIVE_ROTATION` cannot: is anything waiting to READ the artifact this hook
# writes? A separate variable rather than a value of the existing one, because
# the existing one is also how a human turns the feature on, and a marker that
# the subject of the message can set for itself is not evidence of anything.
SUPERVISED_ENV = "MINDHIVE_ROTATION_SUPERVISED"

_ADVICE_SUPERVISED = (
    "Context has passed the smart-zone boundary ({occupancy:,} of {threshold:,} "
    "tokens). `hive session run` is waiting on this session and starts a "
    "successor, seeded from the ledger, as soon as it exits. Wrap up the current "
    "thought, write anything the next session needs into the ledger, and stop — "
    "do not start new work in this session."
)

# The unsupervised half of the same crossing. Everything the supervised message
# claims about what happens next is false here, so none of it is said: what DID
# happen is one file being written, and the honest remainder is a command the
# user can run. Naming the artifact path matters — it is the difference between
# "something was recorded" and a claim the reader can go and check.
_ADVICE_UNSUPERVISED = (
    "Context has passed the smart-zone boundary ({occupancy:,} of {threshold:,} "
    "tokens). Nothing is queued: this session was not launched by "
    "`hive session run`, so no successor starts when it ends — the crossing was "
    "recorded at {artifact}, and that is the whole of it. Wrap up the current "
    "thought and write what a successor would need into the ledger "
    "(`hive append decision \"<one line>\"`, then `hive sync`), then start the "
    "next session yourself; `hive session run` is the one that supervises a "
    "session and rotates it for you."
)


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("MINDHIVE_ROTATION", "")).strip().lower() in _TRUTHY


def supervised(env=None) -> bool:
    """True when `hive session run` spawned this session and is waiting on it.

    Deliberately a different question from `enabled`, and never a fallback for
    it: rotation being switched on means this hook may speak, not that anyone
    will act on what it writes. The marker alone enables nothing — an
    unsupervised session with rotation off stays silent whatever it says."""
    env = os.environ if env is None else env
    return str(env.get(SUPERVISED_ENV, "")).strip().lower() in _TRUTHY


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

        recorded = artifact.write(session_id=payload.get("session_id"),
                                  occupancy=occ.tokens, threshold=threshold,
                                  model=occ.model, project=project)
        # One `format` call for both templates: the supervised one has no
        # `{artifact}` field and `str.format` ignores the spare argument, so the
        # branch stays a choice of words rather than a second code path.
        advice = _ADVICE_SUPERVISED if supervised(env) else _ADVICE_UNSUPERVISED
        return {"hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": advice.format(occupancy=occ.tokens,
                                               threshold=threshold,
                                               artifact=recorded),
        }}
    except Exception:                                       # noqa: BLE001
        return None


# --- the outcomes-registry probe ----------------------------------------------
# Two module-level lines in the file that runs at the end of every turn in every
# session on this machine, so both are deliberately inert: `outcomes` imports
# nothing but `collections`, and `register` is called with literals and a
# function defined above it. Neither touches the environment, the transcript, or
# the artifact. `test_the_probe_registers_on_import` is what keeps that true.

def _probe(project=None) -> outcomes.Report:
    """Report on the advisory's own feedback loop: when this hook speaks, is what
    it says true of the session it is speaking to?

    Silence is the honest zero here and is reported as one. The hook says nothing
    unless `MINDHIVE_ROTATION` is set, and that is opt-in by design (CLAUDE.md) —
    "off" and "broken" are different facts, and a probe that reported them alike
    would be the thing this registry exists to prevent.

    The one genuinely degraded state it can see is the two advice texts having
    become the same text, which is exactly the defect the supervised marker was
    added to fix: one message for both modes means one of the two sessions is
    being told something false. Cheap and read-only — it reads two env vars and
    compares two constants, and never calls `hook_decision`.
    """
    if _ADVICE_SUPERVISED == _ADVICE_UNSUPERVISED:
        return outcomes.Report(
            outcomes.DEGRADED,
            "supervised and unsupervised advice are the same text — one of the "
            "two modes is being told something untrue about a successor")
    if not enabled():
        return outcomes.Report(
            outcomes.NOTHING_TO_DO,
            "rotation is off, so the hook returns None and says nothing — "
            "registration and activation are separate on purpose; "
            "`hive session run` is what switches it on")
    if supervised():
        return outcomes.Report(
            outcomes.DID_WORK,
            "rotation on under a supervisor — a crossing names the successor "
            "`hive session run` will start, which is true here")
    return outcomes.Report(
        outcomes.DID_WORK,
        "rotation on with no supervisor — a crossing says plainly that nothing "
        "is queued, names the artifact it wrote, and names the command that "
        "would queue one")


outcomes.register("rotation-advice",
                  "the end-of-turn advisory claims a queued successor only when "
                  "one is really queued",
                  _probe)
