# src/mindhive/mindnet/autodeclare.py
"""MN-18 — decide whether a prompt should bring a MindNetwork task into existence.

The founding requirement asks for a system that is "otomatis terpanggil" across the eight
phases. Until now nothing created the first task: `hive mindnet declare` is a hand-run
command and `decompose` needs a parent. So the live store held zero tasks, `inject.block`
returned "" on its first line, and the phase spine, the SOP section and the Improvements
section — all three implemented and tested — were never reached by any prompt.

**Pure by contract.** Everything this module needs is passed in. It runs on the hook path,
where reading the ledger costs ~1.09 s against a ~0.4 s budget, and where a raise breaks
the user's prompt rather than degrading a feature. So: no disk, no clock, no randomness,
and every uncertain branch refuses.

**Refusal is the default.** The ledger is append-only — a task written by mistake cannot be
removed, only superseded. Three independent bounds keep that from becoming a mess:

1. only an ARMED project declares at all (the caller consults `optin`);
2. only when no record is already active — one auto-declared root at a time, so writes are
   one-per-work-session rather than one-per-prompt;
3. the id is derived deterministically from the prompt, so a repeated prompt re-declares
   the same id and the graph fold upserts it instead of adding a second task.

**No phase is ever named.** `cmd_mindnet_declare` is explicit about why: the entry point is
derived by `tasks.fold`, and "a caller-named entry phase is the teleport MN-2 stopped
accepting". `playbook.infer` therefore decides only WHETHER a prompt is recognizable code
work worth declaring for — never where the task starts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import tasks as t_mod

# The title is a headline, not the prompt. A long title makes `hive mindnet list`
# unreadable and buys nothing the description would not carry better.
MAX_TITLE_CHARS = 90

# Stamped on every auto-declared task so `hive mindnet list` distinguishes one the machine
# created from one the operator wrote. The store is append-only, so provenance has to be
# written at declare time — there is no later pass that could add it.
AUTO_DESCRIPTION = "auto-declared by MindNetwork from the prompt that started this work"

# Where a headline ends. First sentence, first line, or the cap — whichever comes first.
_HEADLINE_END = re.compile(r"[.!?\n]")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Decision:
    """Whether to declare, and with what.

    Frozen and phase-free: `test_the_decision_never_carries_a_phase` asserts the absence
    of a phase field structurally, so an edit that adds one fails loudly instead of
    quietly teleporting a task past its gates."""

    declare: bool
    task_id: str = ""
    title: str = ""
    reason: str = ""


def headline(text) -> str:
    """*text*'s first sentence or line, collapsed and capped.

    The title a human will read in `hive mindnet list`, so it is trimmed at a sentence
    boundary rather than mid-clause — the same instinct as MN-16's gist truncation, for the
    same reason."""
    body = _WS.sub(" ", str(text or "")).strip()
    if not body:
        return ""
    cut = _HEADLINE_END.search(body)
    if cut and cut.start() > 0:
        body = body[:cut.start()]
    body = body.strip()
    if len(body) <= MAX_TITLE_CHARS:
        return body
    clipped = body[:MAX_TITLE_CHARS]
    space = clipped.rfind(" ")
    return (clipped[:space] if space > 0 else clipped).strip()


def derive_id(text, taken=()) -> str:
    """A `tasks.ID_RE`-legal id derived from *text*, unique against *taken*.

    Delegates to `decompose.slugify`, which already owns this problem: it enforces the
    `^[a-z0-9][a-z0-9-]{0,63}$` contract, suffixes only on a real collision, and gives up
    cleanly rather than looping. Reusing it means one id derivation in the tree, not two
    that drift — and it is already tested against the shapes that break naive slugs.

    Deterministic in *text* and *taken*: the same prompt yields the same id, which is what
    makes a repeated prompt a re-declare rather than a second task."""
    from . import decompose as dc_mod
    return dc_mod.slugify(headline(text) or "task", taken=tuple(sorted(taken or ())))


_MISSING = object()


def _active_records(sidecar):
    """The sidecar's phase records as a dict, or None when the shape is unreadable.

    None and `{}` mean different things here and only the second is safe to declare
    against. Getting that backwards is a live hazard, not a hypothetical: a first version
    read a missing `state` as "records are empty" and happily declared against `None`
    passed in place of a sidecar. A real `sidecar.read` never raises and always returns a
    Sidecar — so the ABSENCE of `state` means the caller handed over something else, and
    an unreadable projection must not be mistaken for a clean slate."""
    state = getattr(sidecar, "state", _MISSING)
    if state is _MISSING or state is None:
        return None
    records = getattr(state, "records", _MISSING)
    if records is _MISSING:
        return None
    if records is None:
        return {}                      # a real projection with nothing folded yet
    return records if isinstance(records, dict) else None


def declare(decision, session_id=None, project=None, description=None):
    """Append *decision* on BOTH axes. Returns the two events, or None.

    Both or neither. A task on the graph axis with no phase record is the half-declared
    state `_mn_no_record` exists to complain about, and on an append-only store you cannot
    take the first event back — so the graph event goes first and a failure on the phase
    event is reported, not swallowed into a half-task.

    **The phase payload names no phase.** `tasks.fold` derives the entry point; naming one
    here is the teleport MN-2 removed.

    Never raises: the caller is a hook whose product is the printed context, and a
    bookkeeping failure must not touch it."""
    if not getattr(decision, "declare", False):
        return None
    try:
        from .. import events as ev_mod
        task_ev = ev_mod.append_event(
            "mindnet_task",
            {"action": "declare", "task_id": decision.task_id,
             "title": decision.title,
             # Provenance, so `hive mindnet list` distinguishes a machine-declared task
             # from one the operator wrote by hand. Permanent, like everything here.
             #
             # *description* overrides it, and the override is the whole reason
             # the parameter exists: this default is boilerplate about HOW the
             # task was declared, and a sub-agent briefed from it is told nothing
             # about WHAT it was declared for. A caller that holds the operator's
             # real words — the mission pipeline does — should pass them, and
             # every caller that does not keeps the provenance line unchanged.
             "description": str(description or "").strip() or AUTO_DESCRIPTION},
            session_id=session_id, tags=["mindnet", "task", "auto"], project=project)
        phase_ev = ev_mod.append_event(
            "mindnet_phase",
            {"action": "declare", "task_id": decision.task_id,
             "verified_by": "python"},
            session_id=session_id, tags=["mindnet", "phase", "auto"], project=project)
        return task_ev, phase_ev
    except Exception:                                       # noqa: BLE001
        return None


def refresh_projection(project=None) -> bool:
    """Rebuild the sidecar when it is stale. Returns whether a rebuild was written.

    **Not on the prompt path**, and that is the entire point of the function existing
    separately. `sidecar.rebuild` measures 0.32 s against the hook's ~0.4 s budget, so per
    prompt it is unaffordable — but without it the projection never returns to fresh and
    per-task phase direction never appears.

    `hive state` is the host: the session-start hook already runs it, it is the only command
    in that hook belonging to this repo, and 0.32 s is invisible at session start while it is
    decisive on the prompt path. There is no `session_start` stage in this CLI to hang it on
    — checked — so this is the available session-start signal, not a first choice discarded.

    Stale-gated: a session opening against a fresh projection pays nothing. Never raises —
    it runs inside a display command, and a projection that cannot be rebuilt must not take
    that command down."""
    try:
        from . import sidecar as s_mod
        if s_mod.read(project).fresh:
            return False
        return bool(getattr(s_mod.rebuild(project), "written", False))
    except Exception:                                       # noqa: BLE001
        return False


def maybe_declare(prompt, sidecar, session_id=None, project=None, env=None):
    """The whole gate: armed, then decided, then appended. Returns the events or None.

    One entry point so the hook has one call and the ORDER of the gates is stated in one
    place. The opt-in comes first deliberately — an unarmed project must not even pay the
    decision, and more importantly must produce no trace of having considered it."""
    try:
        from . import optin as optin_mod
        if not optin_mod.armed(project, env=env):
            return None
    except Exception:                                       # noqa: BLE001
        return None                                         # unreadable opt-in => dormant
    return declare(decide(prompt, sidecar), session_id=session_id, project=project)


def decide(prompt, sidecar) -> Decision:
    """Whether *prompt* should declare a task, given an already-read *sidecar*.

    Total: any fault, unreadable shape, or uncertainty answers `declare=False` with a
    reason. The reason is not decoration — it is what makes a silent hook explicable
    when an operator asks why no task appeared."""
    try:
        from .. import promptgate as pg_mod
        from .. import playbook as pb_mod

        text = prompt if isinstance(prompt, str) else ""
        if not text.strip():
            return Decision(False, reason="the prompt is empty")
        if pg_mod.is_machine_prompt(text):
            return Decision(False, reason="the prompt is machine-injected, not typed")
        if not pg_mod.is_code_work(text):
            return Decision(False, reason="the prompt is not code work")
        if pb_mod.infer(text) is None:
            # Deliberately strict: without a recognizable technique there is no basis for
            # a task, and one parked at a gate nobody chose is worse than none.
            return Decision(False, reason="no technique could be inferred for the prompt")

        records = _active_records(sidecar)
        if records is None:
            return Decision(False, reason="the sidecar's records are unreadable")
        if records:
            # Reuse, not a second root. The spine already has something to speak about.
            return Decision(False, reason="a task is already active — reuse it")

        title = headline(text)
        if not title:
            return Decision(False, reason="no usable title could be derived")
        task_id = derive_id(text, taken=set(records))
        if not task_id:
            return Decision(False, reason="no legal task id could be derived")
        return Decision(True, task_id=task_id, title=title)
    except Exception as exc:                                # noqa: BLE001
        return Decision(False, reason=f"refused: {type(exc).__name__}")
