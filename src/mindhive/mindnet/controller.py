# src/mindhive/mindnet/controller.py
"""MN-1 — the phase state machine: where a task is, what cleared each exit
criterion, and which move the machine will accept next.

`tasks.py` owns the graph axis, `phases.py` owns the evidence contract, and this
module owns the phase axis — as its ONLY writer (KTD1). Three parts:

- `fold` — a deterministic, total replay of ``mindnet_phase`` into
  ``task_id -> PhaseRecord`` plus an ORPHANS bucket;
- `can_advance` — a PURE predicate over that fold plus the task graph;
- `restat` — the one impure function here, and the only one that appends.

## The fold refuses; it does not merely report

`hive append mindnet_phase` accepts any shape-valid payload, so a projection
that trusted every ``enter`` would let two raw appends park a task at the
terminal gate — the exact defect KTD1 just closed on the graph axis. The fold
therefore applies the transition rule itself and records every refusal in the
record's ``violations``, mirroring `tasks.fold`. The event stays on the ledger
(appends are immutable), but the projection never enters the bad state.

**What the fold gates, and what only `can_advance` gates.** The fold applies the
PHASE-AXIS rule — the part decidable from the phase stream itself plus two facts
that cannot change after a task is declared (its entry phase and its
``triviality_bypass``, which KTD10 makes first-declare-only). The graph-shaped
gates — status, subtree soundness, descendant coupling — live in `can_advance`
alone, because status and shape are END-STATE facts of a different event stream:
applying them per replay step would judge a two-week-old transition against
today's graph and retroactively refuse a move that was legal when it happened.
`can_advance` is pre-flight, where those facts are current by construction. One
rule, one home: both callers route the phase-axis half through
`_phase_axis_refusal`, so a gate cannot be answered by only one of them.

## Three rules carry the whole design

1. **A satisfaction is bound to the phase AND the epoch** (KTD5). Accepted only
   when its ``phase`` is the task's current phase at that point in the replay and
   its ``epoch`` is the fold's current epoch. Without the binding, eight
   ``satisfy`` appends plus seven legal forward moves walk a task from the gate
   to `pr-green` with no work done — each move only ever checks the phase being
   LEFT, and all eight were already sitting there waiting.

2. **Backward movement invalidates forward evidence** (KTD4). It increments the
   epoch and kills every satisfaction at or past the target index, so walking
   forward again needs NEW evidence at the NEW epoch. Without it the failure
   handler is a no-op loop: the old satisfactions survive, two forward moves
   return the task to the phase that just failed, and nothing was fixed.

   *Deviation, deliberate:* the plan defines the epoch as the ``fall_back``
   count, but TWO spellings move a task backward — ``fall_back`` and a backward
   ``enter``, which `can_advance` must permit ungated or every failing task
   deadlocks. An epoch that counted only one of them would let the other spell
   its way around the invalidation. One rule per axis whatever the spelling
   (`tasks.py`'s own doctrine): the epoch counts backward MOVES. Driven by the
   CLI the two numbers are identical, because `fall_back` is how a backward move
   is spelled there — and `fall_back` additionally has to say what failed.

3. **Contradictions resolve by AUTHORITY, not replay order** (KTD3):
   ``python > agent > human``, and a `fail` at the deciding authority is never
   overridden by an `ok`. Replay order is something a writer controls in this
   codebase. `read_all` sorts on `events._order_key`, ``(ts, seq, segment,
   line)``, and `append_event` takes a caller-supplied ``ts`` with no
   monotonicity check at all, backdating being already in production use
   (`harvest.harvest` and `harvest.backfill_skills` append at the transcript's
   own timestamps), so a backdated row replays ahead of every second it skips.
   `events.now_iso` is second-resolution, so ordinary rows tie on ``ts``
   constantly; the append stamp ``seq`` settles that tie in APPEND order, so
   whoever writes last in a second replays last — while the high-water mark in
   `append.lock` rules every stamp. A stamp it does not rule can replay ahead of
   a row written before it (`events._next_seq`): an append on either of
   `events._append_lock`'s fail-open paths stamps from the bare clock, blind to
   the mark (below it whenever the mark has run ahead of the clock) and outside
   the lock, so another append can stamp and write between that stamp and its
   write; and while the clock reads no later than stamps already issued (it
   stepped back, or appends inside one tick of a coarse clock ran the mark
   ahead of it), an append that finds the mark lost (`append.lock` deleted,
   garbled or unreadable) seeds from the clock, and one that reads a mark a
   failed write left stale can repeat a stamp or land below one. Where no stamp
   settles it,
   `events._segment_events` and `events._order_key` do: an unstamped row below
   a stamped one in its own segment is ordered by the highest stamp above it; a
   row with no stamp on or above it sorts FIRST in its second whatever its
   session id; and two such rows, or two sharing a stamp, break on the segment
   name, then line — alphabetically by session id, as every same-second tie did
   before ``seq`` existed. A verdict that depended on order would be forgeable
   by timing an append, backdating one, or (for those last rows) spelling a
   session id with an earlier letter. This one is order-INDEPENDENT by
   construction.

## Purity

`fold`, `can_advance`, and every predicate here are deterministic and total: no
clock, no RNG, no I/O, and every traversal iterative (`tasks.py` carries a
5000-node chain test because recursion blows CPython's ~1000-frame stack).
Malformed rows, unknown actions, unknown phases and missing fields are skipped
or recorded, never raised — the ledger holds history written by every past
version of the code. `restat` is the sole exception and owns the sole append.

Every field on a record is a str/int/bool/tuple, so U5's sidecar can serialize
the fold without a custom encoder.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .. import events as ev_mod
from .. import paths
from . import phases as ph_mod
from . import tasks as t_mod
from .tasks import (CLOSED_STATUSES, MAX_NODES, MAX_VIOLATIONS, PHASES,
                    STATUS_IN_PROGRESS, STATUS_PENDING, STATUS_READY,
                    TRIVIAL_ENTRY_PHASE, ancestors, descendants, phase_index)

# Statuses that may move FORWARD. `pending` is included deliberately (KTD12): it
# is the dataclass default for every declared task (`tasks.py:161`), so omitting
# it would make the machine unusable — nothing could ever take its first step.
# `blocked`, `completed` and `abandoned` may only move backward, which is never
# gated, so a settled task can still record that it fell over.
WORKABLE_STATUSES = (STATUS_PENDING, STATUS_READY, STATUS_IN_PROGRESS)

# The phase a parent may not pass while a descendant is still short of the work
# boundary (KTD13). Named once because `can_advance` gates on it and the
# operator-facing reason quotes it.
DESCENDANT_GATE_PHASE = "audit-bug"

# The work boundary itself. `TRIVIAL_ENTRY_PHASE` is already the one home for
# this string — it is where a bypassed task is born, i.e. exactly the line
# between "still deciding what to build" and "building it".
WORK_PHASE = TRIVIAL_ENTRY_PHASE

RESTAT_STATUSES = ("ok", "stale", "unknown")


# --- the shapes ----------------------------------------------------------------

@dataclass(frozen=True)
class Satisfaction:
    """The RESOLVED verdict for one ``(phase, epoch)`` — never a single event.

    Several `satisfy` records can name the same pair; `_resolve` reduces them by
    authority to this one answer, so no consumer has to re-derive the ordering
    rule (and get it subtly different).

    ``valid`` goes False when a later backward move invalidated the pair; the
    record keeps it anyway, because `show` explaining WHY a phase went unsatisfied
    is the difference between a machine and a mystery. ``stale`` marks the sharper
    case: evidence that WAS accepted and a later check at the same authority
    contradicted — a re-stat finding a vanished plan file, say.

    ``root`` / ``branch`` are the checkout that produced file evidence (KTD7).
    `detect_project` collapses every worktree onto one namespace, so without them
    a re-stat from a sibling checkout would report `fail` for a file that is
    simply on another branch.

    ``failed_refs`` is every ref a fail was recorded against IN THIS FOLD. It is
    diagnostic, NOT `restat`'s idempotence guard — it used to be, and that made
    the guard only as good as the fold's acceptance of `restat`'s own write. See
    `_restat_fail_recorded`, which asks the raw ledger instead."""

    phase: str
    epoch: int
    result: str                 # "ok" | "fail" — the resolved verdict
    verified_by: str            # the authority that decided it
    kind: str = ""              # phases.EVIDENCE_KINDS
    ref: str = ""
    detail: str = ""
    valid: bool = True
    stale: bool = False
    failed_refs: tuple = ()     # every ref a fail was recorded against
    root: str = ""              # worktree root that resolved file evidence
    branch: str = ""            # branch it resolved on


@dataclass(frozen=True)
class PhaseRecord:
    """One task's phase state. Frozen for `tasks.Task`'s reason: the fold builds
    records rather than mutating them, so two folds of the same events can never
    alias each other's state.

    ``satisfactions`` is a TUPLE sorted by ``(phase index, epoch)`` rather than a
    dict — a deterministic order the sidecar and `show` can render straight
    through, and one fewer mutable container inside a frozen record.

    ``violations`` mirrors `tasks.Task.violations`: the refusals this fold made
    against this record, oldest first, capped. A refused event stays on the
    ledger forever, so without somewhere to record the refusal the projection
    would silently disagree with the ledger and nobody could see why."""

    task_id: str
    phase: str
    epoch: int = 0
    satisfactions: tuple = ()
    violations: tuple = ()

    def satisfaction(self, phase, epoch):
        """The resolved verdict for exactly ``(phase, epoch)``, or None."""
        return _find(self.satisfactions, phase, epoch)

    def history(self, phase):
        """Every satisfaction ever recorded for *phase*, valid or not. The
        trivial-entry exception reads this: a task that produced a plan is not a
        trivial task, whatever a later bypass claims."""
        return tuple(s for s in self.satisfactions if s.phase == phase)


@dataclass(frozen=True)
class Orphan:
    """A ``mindnet_phase`` event with nowhere to land (KTD14).

    A typo'd ``task_id`` produces events that pass `validate_payload`, ride the
    hash chain, and then vanish from every projection with nothing reporting the
    loss — `tasks.fold`'s own H19. Carrying ``ts`` and ``session_id`` is the
    point: they are how an operator finds the typo."""

    task_id: str
    action: str
    phase: str
    ts: str
    session_id: str
    reason: str


class PhaseState(NamedTuple):
    """`fold`'s two halves.

    `tasks.fold` returns a bare dict and this does not, for one reason: an orphan
    has no record to hang off, and a sentinel key inside the records dict is
    itself a legal ``task_id`` (``ID_RE`` would happily match ``orphans``), so it
    could collide with a real task. A NamedTuple keeps
    ``records, orphans = fold(evs)`` unpacking while naming both halves, so U5
    and U7 cannot transpose them."""

    records: dict
    orphans: dict


class Restat(NamedTuple):
    """`restat`'s answer: one of `RESTAT_STATUSES`, why, and the event it
    appended (None when it appended nothing — the common case)."""

    status: str
    reason: str
    event: object = None


# --- small totals ----------------------------------------------------------------

def _note(existing, reason) -> tuple:
    """*existing* violations plus *reason*, capped. Mirrors `tasks._note`: the
    OLDEST are kept, because the first refusal explains how the record got into
    this shape and a loop only ever repeats itself."""
    if len(existing) >= MAX_VIOLATIONS:
        return existing
    return existing + (reason,)


def _find(sats, phase, epoch):
    for s in sats:
        if s.phase == phase and s.epoch == epoch:
            return s
    return None


def _text(value, cap=ph_mod.MAX_DETAIL_CHARS) -> str:
    """Any payload value as a bounded string. Size caps bound every field so one
    oversized value can never invalidate — and thereby silently drop — a whole
    record (roadmap.py / tasks.py rationale)."""
    return "" if value is None else str(value)[:cap]


def _authority(verified_by):
    """Rank of *verified_by*, lower being stronger, or None when it is not a
    declared verifier. Total: an unknown value is a refusal, not an exception."""
    try:
        return ph_mod.VERIFIERS.index(verified_by)
    except ValueError:
        return None


# --- resolving contradictions ------------------------------------------------------

@dataclass(frozen=True)
class _Cand:
    """One ACCEPTED satisfy record, before contradictions are resolved."""
    verified_by: str
    result: str
    kind: str
    ref: str
    detail: str
    root: str
    branch: str


def _resolve(cands, phase, epoch, valid) -> Satisfaction:
    """Reduce every accepted record for one ``(phase, epoch)`` to one verdict.

    Two rules, in this order (KTD3):

    1. **The strongest authority present decides.** A weaker record does not get
       a vote — that is what "a python `fail` is never overridden by an agent
       `ok`" means operationally.
    2. **Within it, a `fail` wins.** Among fails the LAST is kept (the newest
       observation of a failure is the informative one); with no fail, the FIRST
       `ok` is kept (the evidence that originally cleared the gate, not a later
       restatement of it).

    Fail-dominance is what makes the verdict order-independent, and it is the
    fail-safe direction: clearing a recorded failure takes a backward move, which
    bumps the epoch and puts the retry on the ledger. Letting a later `ok` at the
    same epoch bury a `fail` would hand back the no-op loop the epoch exists to
    close."""
    best = min(_authority(c.verified_by) for c in cands)
    top = [c for c in cands if _authority(c.verified_by) == best]
    fails = [c for c in top if c.result == "fail"]
    if fails:
        win, result = fails[-1], "fail"
    else:
        win, result = next(c for c in top if c.result == "ok"), "ok"
    return Satisfaction(
        phase=phase, epoch=epoch, result=result, verified_by=win.verified_by,
        kind=win.kind, ref=win.ref, detail=win.detail, valid=valid,
        # Accepted, then contradicted at the SAME authority. An agent `ok` that a
        # python `fail` outranked is not stale evidence — it is a claim that was
        # never load-bearing in the first place.
        stale=bool(fails) and any(c.result == "ok" for c in top),
        # Every ref a fail names, at ANY authority: `restat`'s idempotence test
        # asks "is this exact failure already on the ledger", and re-appending it
        # because a stronger record exists elsewhere would spam the chain.
        failed_refs=tuple(sorted({c.ref for c in cands
                                  if c.result == "fail" and c.ref})),
        root=win.root, branch=win.branch)


# --- the replay's working state -----------------------------------------------------

@dataclass
class _Live:
    """Mutable per-task state during the replay. Deliberately not `PhaseRecord`:
    the fold mutates as it walks, and the exported record is frozen."""

    task_id: str
    phase: str
    epoch: int = 0
    groups: dict = field(default_factory=dict)   # (phase, epoch) -> [_Cand]
    dead: dict = field(default_factory=dict)     # phase -> first epoch still valid
    violations: tuple = ()

    def resolved(self) -> tuple:
        """Every group resolved, sorted by ``(phase index, epoch)`` — the same
        view `can_advance` gets, so the fold's own transition decisions are made
        against exactly the state a caller would have seen at that moment."""
        out = [_resolve(cands, phase, epoch,
                        valid=epoch >= self.dead.get(phase, 0))
               for (phase, epoch), cands in self.groups.items() if cands]
        out.sort(key=lambda s: (phase_index(s.phase) or 0, s.epoch))
        return tuple(out)

    def note(self, reason) -> None:
        self.violations = _note(self.violations, reason)


# --- the phase-axis rule (ONE home, two callers) -------------------------------------

def _phase_axis_refusal(task_id, phase, epoch, sats, trivial, to_phase):
    """None when the phase axis alone permits ``phase -> to_phase``, else the
    reason. THE rule both the fold and `can_advance` answer to.

    - **Backward, any distance: always legal, never evidence-gated.** This is how
      an audit failure is handled, and gating it would deadlock every failing
      task at exactly the moment it needs to move.
    - **Forward exactly +1**, and only with the phase being left satisfied at the
      CURRENT epoch. A satisfaction from a previous epoch was invalidated by the
      backward move that created this one.
    - **Forward by more than one: refused**, with one written exception —
      ADR-0005's bypass waives the METHOD for a one-line fix, so a bypassed task
      may step from the gate straight to `TRIVIAL_ENTRY_PHASE`. It may not if any
      phase it would leap has EVER carried a satisfaction: a task that produced a
      plan is not a trivial task, whatever a later bypass claims.
    - **Stale evidence blocks the way out.** A re-stat that found the evidence
      gone marks the phase stale rather than moving the task (KTD8); leaving a
      phase at or past that mark would walk right over the finding."""
    to_i, cur_i = phase_index(to_phase), phase_index(phase)
    if to_i is None:
        return (f"'{to_phase}' is not a phase — one of: " + ", ".join(PHASES))
    if cur_i is None:
        # Written by some past version of the code. Total, and fail-safe: a
        # position the machine cannot locate is not a position to move from.
        return (f"'{task_id}' sits at unrecognised phase '{phase}' — rebuild "
                f"the projection before moving it")
    if to_i == cur_i:
        return f"'{task_id}' is already at '{to_phase}' — that is not a move"
    if to_i < cur_i:
        return None

    # Tested BEFORE the satisfaction gate on purpose: a stale phase is also an
    # unsatisfied one, and "your evidence vanished" is the answer the operator
    # can act on, where "unsatisfied" sends them looking for work they did.
    stale = [s.phase for s in sats
             if s.stale and s.valid and (phase_index(s.phase) or 0) >= cur_i]
    if stale:
        return (f"'{task_id}' cannot leave '{phase}': "
                + ", ".join(f"'{p}'" for p in sorted(set(stale)))
                + " is stale — a re-stat found its evidence gone. Fall back to "
                  "it and satisfy it again")

    if to_i > cur_i + 1:
        leapt = [PHASES[i] for i in range(cur_i + 1, to_i)]
        if not (trivial and cur_i == 0 and to_phase == TRIVIAL_ENTRY_PHASE):
            return (f"'{task_id}' cannot skip from '{phase}' to '{to_phase}' — "
                    f"forward transitions move one phase at a time, past "
                    + ", ".join(f"'{p}'" for p in leapt))
        done = [p for p in leapt if _has_history(sats, p)]
        if done:
            return (f"'{task_id}' cannot take the trivial entry to "
                    f"'{to_phase}': " + ", ".join(f"'{p}'" for p in done)
                    + " already carries a satisfaction, so this task is not "
                      "the one-line fix the bypass waives the method for")
    else:
        sat = _find(sats, phase, epoch)
        if sat is None or not sat.valid or sat.result != "ok":
            return (f"'{task_id}' has not satisfied '{phase}' at epoch {epoch} "
                    f"— clearing that phase's exit criterion is what earns the "
                    f"move to '{to_phase}'")

    stale = [s.phase for s in sats
             if s.stale and s.valid and (phase_index(s.phase) or 0) >= cur_i]
    if stale:
        return (f"'{task_id}' cannot leave '{phase}': "
                + ", ".join(f"'{p}'" for p in sorted(set(stale)))
                + " is stale — a re-stat found its evidence gone. Fall back to "
                  "it and satisfy it again")
    return None


def _has_history(sats, phase) -> bool:
    return any(s.phase == phase for s in sats)


# --- the fold ------------------------------------------------------------------------

def fold(evs, tasks=None) -> PhaseState:
    """Deterministic, total fold of ``mindnet_phase`` into
    ``(records, orphans)``.

    *tasks* is the graph from `tasks.fold`, injected so U5 can fold both streams
    once; omitted, it is folded from the same events. There is no cycle — KTD1
    left `tasks.py` with no writer on the phase axis, so the graph fold never
    reads this one.

    **The task graph is the register of tasks.** A phase event for a ``task_id``
    the graph never declared has nowhere to land: its entry phase is underivable,
    and inventing one would make a typo indistinguishable from a task. It goes to
    `orphans` — visibly — rather than being dropped the way H19 dropped its
    lifecycle events."""
    if tasks is None:
        tasks = t_mod.fold(evs)
    live: dict = {}
    orphans: dict = {}
    for ev in evs or []:
        if getattr(ev, "type", None) != "mindnet_phase":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        action, tid = p.get("action"), p.get("task_id")
        if action not in ph_mod.ACTIONS or not isinstance(tid, str) or not tid:
            continue
        st = live.get(tid)

        if action == "declare":
            if st is not None:
                # Create-only. A re-declare must never reset the phase axis: that
                # would be a silent teleport back to the gate, discarding every
                # satisfaction with nothing on the record saying so.
                continue
            if tid not in tasks:
                _orphan(orphans, tid, action, p, ev,
                        "no such task in the graph — declare it first with "
                        "`hive mindnet declare`, or fix the task_id")
                continue
            entry = tasks[tid].phase
            live[tid] = _Live(task_id=tid,
                              phase=entry if phase_index(entry) is not None
                              else PHASES[0])
            continue

        if st is None:
            _orphan(orphans, tid, action, p, ev,
                    "no phase record — this task was never declared on the "
                    "phase axis")
            continue

        if action == "enter":
            _apply_enter(st, p, tasks)
        elif action == "satisfy":
            _apply_satisfy(st, p)
        elif action == "fall_back":
            _apply_fall_back(st, p)

    records = {tid: PhaseRecord(task_id=tid, phase=st.phase, epoch=st.epoch,
                                satisfactions=st.resolved(),
                                violations=st.violations)
               for tid, st in live.items()}
    return PhaseState(records, {tid: tuple(rows)
                                for tid, rows in orphans.items()})


def _orphan(orphans, tid, action, payload, ev, reason) -> None:
    rows = orphans.get(tid)
    if rows is None:
        if len(orphans) >= MAX_NODES:
            return              # a runaway typo loop is bounded like the graph
        rows = orphans[tid] = []
    if len(rows) >= MAX_VIOLATIONS:
        return
    rows.append(Orphan(task_id=tid, action=str(action),
                       phase=_text(payload.get("phase"), 64),
                       ts=_text(getattr(ev, "ts", ""), 40),
                       session_id=_text(getattr(ev, "session_id", ""), 120),
                       reason=reason))


def _apply_enter(st, p, tasks) -> None:
    to_phase = p.get("phase")
    trivial = bool(getattr(tasks.get(st.task_id), "triviality_bypass", False))
    why = _phase_axis_refusal(st.task_id, st.phase, st.epoch, st.resolved(),
                              trivial, to_phase)
    if why is not None:
        st.note(f"refused enter: {why}")
        return
    _move(st, to_phase)


def _apply_fall_back(st, p) -> None:
    to_phase = p.get("phase")
    to_i, cur_i = phase_index(to_phase), phase_index(st.phase)
    if not _text(p.get("detail")).strip():
        # The contract already refuses this at both append surfaces; the fold
        # must not apply one that slipped past an older version of it. A
        # fall_back is the one transition that exists to record a failure, and
        # one that records nothing about it is a lost incident.
        st.note(f"refused fall_back to '{to_phase}': it must say what failed")
        return
    if to_i is None:
        st.note(f"refused fall_back: '{to_phase}' is not a phase")
        return
    if cur_i is not None and to_i > cur_i:
        st.note(f"refused fall_back: '{to_phase}' is ahead of '{st.phase}' — "
                f"falling back means going back")
        return
    _move(st, to_phase, force_bump=True)


def _move(st, to_phase, force_bump=False) -> None:
    """Apply an accepted phase move, and pay the epoch cost of going backward.

    *force_bump* is what makes a `fall_back` to the CURRENT phase meaningful:
    same position, new generation, evidence invalidated — "redo this phase"."""
    to_i, cur_i = phase_index(to_phase), phase_index(st.phase)
    if force_bump or (cur_i is not None and to_i is not None and to_i < cur_i):
        st.epoch += 1
        for ph in PHASES[to_i:]:
            # Watermark, not a delete: `show` explaining that evidence WAS there
            # and what killed it is worth more than a smaller record.
            st.dead[ph] = max(st.dead.get(ph, 0), st.epoch)
    st.phase = to_phase


def _apply_satisfy(st, p) -> None:
    """Record a satisfaction, or the reason it is not one.

    Six gates. The first two are KTD5 and carry the whole design; the rest are
    the honesty rule (KTD2) applied at read time, because the ledger holds rows
    written by every past version of this code and by anything that reached
    `hive append`."""
    phase = p.get("phase")
    epoch = p.get("epoch")
    if phase != st.phase:
        st.note(f"refused satisfy for '{phase}': '{st.task_id}' is at "
                f"'{st.phase}' — a satisfaction not bound to the phase the task "
                f"is actually in is re-usable the moment it gets there")
        return
    if isinstance(epoch, bool) or not isinstance(epoch, int) \
            or epoch != st.epoch:
        st.note(f"refused satisfy for '{phase}': epoch {epoch!r} is not the "
                f"task's current epoch {st.epoch} — evidence from before a "
                f"fall_back does not survive it")
        return
    who = p.get("verified_by")
    rank = _authority(who)
    if rank is None:
        st.note(f"refused satisfy for '{phase}': it records no verified_by, so "
                f"there is no way to weigh it against a contradicting record")
        return
    desc = ph_mod.descriptor(phase) or {}
    need = _authority(desc.get("verified_by"))
    if need is not None and rank > need:
        st.note(f"refused satisfy for '{phase}': {who}-reported, but this phase "
                f"is verified by {desc.get('verified_by')} — a weaker claim "
                f"cannot clear a criterion something stronger can check")
        return
    ev = p.get("evidence")
    if not isinstance(ev, dict):
        st.note(f"refused satisfy for '{phase}': evidence must be an object "
                f"{{kind, ref, result, detail}}")
        return
    result = ev.get("result")
    if result not in ph_mod.RESULTS:
        # Tightened past U3, which left `result` optional: a satisfaction that
        # does not say whether it SUCCEEDED is not evidence of anything, and
        # treating a silent one as a pass is how a failed check clears a gate.
        st.note(f"refused satisfy for '{phase}': evidence records no result — "
                f"one of: " + ", ".join(ph_mod.RESULTS))
        return
    kind = ev.get("kind")
    if kind != desc.get("kind"):
        # The descriptor names what CAN be checked for this phase. Command
        # evidence for a document phase is not weaker evidence, it is different
        # evidence — and nothing would ever re-stat it.
        st.note(f"refused satisfy for '{phase}': {kind!r} evidence, but this "
                f"phase takes {desc.get('kind')!r} evidence")
        return
    ref = _text(ev.get("ref"), ph_mod.MAX_REF_CHARS).strip()
    if kind == "file" and not ref:
        st.note(f"refused satisfy for '{phase}': file evidence must name the "
                f"ref it is evidence of")
        return
    st.groups.setdefault((phase, epoch), []).append(_Cand(
        verified_by=who, result=result, kind=_text(kind, 40), ref=ref,
        detail=_text(ev.get("detail")) or _text(p.get("detail")),
        root=_text(ev.get("root"), ph_mod.MAX_REF_CHARS),
        branch=_text(ev.get("branch"), 200)))


# --- reading the fold -----------------------------------------------------------------

def satisfied(records, task_id, phase) -> bool:
    """True when *phase* is CURRENTLY cleared for *task_id*.

    Respects both epoch and authority: a satisfaction a later backward move
    invalidated is not a satisfaction, and neither is an `ok` that a stronger
    `fail` outranks."""
    rec = (records or {}).get(task_id)
    if rec is None:
        return False
    return any(s.phase == phase and s.valid and s.result == "ok"
               for s in rec.satisfactions)


def satisfaction_predicate(records):
    """The ``(task_id, phase) -> bool`` predicate `tasks.can_complete` takes
    INJECTED. Injected rather than imported so the dependency stays
    one-directional: this module reads `tasks.py`, never the reverse."""
    return lambda task_id, phase: satisfied(records, task_id, phase)


def phase_of(records, tasks, task_id):
    """*task_id*'s current phase, or None when nothing knows it.

    The phase record wins; a task declared on the graph but not yet on the phase
    axis falls back to its DECLARED entry phase (`tasks.Task.phase`), which after
    KTD1 has no writer and therefore cannot be stale."""
    rec = (records or {}).get(task_id)
    if rec is not None:
        return rec.phase
    task = (tasks or {}).get(task_id)
    return None if task is None else task.phase


# --- structural gates -------------------------------------------------------------------

def unsound_tasks(tasks) -> dict:
    """``task_id -> (reason, ...)`` for every task implicated in a `check_graph`
    violation. ``{}`` for a sound graph.

    `check_graph` returns prose, which is right for a report and useless for a
    gate: `can_advance` has to know WHICH subtree is unsound, and parsing task
    ids back out of English is how a gate rots. So `check_graph` is the
    authority on WHETHER the graph is sound — its empty list is the fast path —
    and the attribution below is recomputed from the same structured primitives
    it uses. This is that function's first consumer anywhere in ``src/``."""
    tasks = tasks or {}
    if not t_mod.check_graph(tasks):
        return {}
    out: dict = {}

    def add(tid, reason):
        out[tid] = _note(out.get(tid, ()), reason)

    if len(tasks) > MAX_NODES:
        # A graph over the wall is unsound everywhere at once.
        for tid in sorted(tasks):
            add(tid, f"the graph holds {len(tasks)} nodes, past MAX_NODES "
                     f"{MAX_NODES}")
    for tid in sorted(tasks):
        task = tasks[tid]
        d = t_mod.depth_of(tasks, tid)
        if d == -1:
            add(tid, f"'{tid}' sits in a hierarchy cycle")
        elif d > t_mod.MAX_DEPTH:
            add(tid, f"'{tid}' sits at depth {d}, past MAX_DEPTH "
                     f"{t_mod.MAX_DEPTH}")
        if len(task.dependencies) > t_mod.MAX_DEPS_PER_TASK:
            add(tid, f"'{tid}' waits on {len(task.dependencies)} tasks, past "
                     f"MAX_DEPS_PER_TASK {t_mod.MAX_DEPS_PER_TASK}")
        for dep in task.dependencies:
            if dep not in tasks:
                add(tid, f"'{tid}' depends on unknown task '{dep}'")
    cyc = t_mod.dependency_cycle(tasks)
    if cyc:
        for tid in cyc:
            add(tid, "dependency cycle: " + " -> ".join(cyc))
    dead = t_mod.cross_graph_conflict(tasks)
    if dead:
        for tid in dead:
            add(tid, f"deadlock: '{dead[0]}' depends on its own relative "
                     f"'{dead[1]}'")
    return out


def coupling_violations(tasks, records) -> dict:
    """``task_id -> (reason, ...)`` for every task running ahead of a parent that
    has not finished deciding what to build. The reparent phase check U2
    deferred.

    U2 left it out because KTD1 leaves `Task.phase` with no writer in
    `tasks.py`, so a comparison there would read declared defaults and pass
    vacuously. It cannot be pushed back into `tasks.fold` either: the phase view
    is folded FROM a fold that needs the task graph, so a fold-time refusal would
    close a circular dependency. Report-and-block has real teeth without the
    cycle — the inversion can be created, and then nothing in that subtree can
    advance until it is resolved.

    **The rule is narrower than "the parent is behind the child", deliberately.**
    KTD13 REQUIRES descendants to reach `pr-green` before their parent may, so a
    bare index comparison would flag the flow the spec mandates and freeze every
    correctly-built tree. What is actually unsound is a descendant at or past the
    work boundary under a parent that has not reached it: the parent never
    planned this work, yet KTD13's gates would read the child's finished state as
    evidence the parent may proceed. Reparenting a finished subtree under a fresh
    parent is exactly how that is manufactured.

    Only the CHILD is implicated. Blocking the parent too would deadlock the one
    move that fixes the inversion — the parent catching up."""
    tasks, out = tasks or {}, {}
    for tid in sorted(tasks):
        parent = tasks[tid].parent_id
        if parent is None or parent not in tasks:
            continue
        child_ph = phase_of(records, tasks, tid)
        parent_ph = phase_of(records, tasks, parent)
        if not t_mod._at_or_past(child_ph, WORK_PHASE):
            continue
        if t_mod._at_or_past(parent_ph, WORK_PHASE):
            continue
        out[tid] = (f"'{tid}' is at '{child_ph}' under parent '{parent}' at "
                    f"'{parent_ph}': work cannot be built under a parent that "
                    f"has not reached '{WORK_PHASE}' — its plan never described "
                    f"this task",)
    return out


def _subtree_unsound(tasks, task_id):
    """Reason when a `check_graph` violation touches *task_id*'s subtree, else
    None. Scope is the task and everything beneath it: a bad edge two levels down
    makes the parent's forward claim untrue too."""
    bad = unsound_tasks(tasks)
    if not bad:
        return None
    for tid in sorted({task_id} | descendants(tasks, task_id)):
        if tid in bad:
            return (f"'{task_id}' cannot advance: its subtree is structurally "
                    f"unsound — {bad[tid][0]}")
    return None


def _coupling_block(tasks, records, task_id):
    """Reason when *task_id* sits inside an inverted subtree, else None. The
    implicated task and everything under it are frozen; its ancestors are not."""
    bad = coupling_violations(tasks, records)
    if not bad:
        return None
    for tid in (task_id,) + ancestors(tasks, task_id):
        if tid in bad:
            where = ("" if tid == task_id
                     else f" — it is beneath '{tid}', which is")
            return f"'{task_id}' cannot advance{where}: {bad[tid][0]}"
    return None


def _descendant_block(tasks, records, task_id, to_phase):
    """KTD13, both halves. Otherwise a parent reports CI green precisely because
    its child's code was never written.

    Closed descendants are exempt from BOTH halves. `CLOSED_STATUSES` exists for
    this: abandoned work is settled, not outstanding, and without the exemption
    one abandoned subtask blocks its parent forever."""
    open_kids = [d for d in sorted(descendants(tasks, task_id))
                 if tasks[d].status not in CLOSED_STATUSES]
    if not open_kids:
        return None
    if t_mod._at_or_past(to_phase, DESCENDANT_GATE_PHASE):
        behind = [d for d in open_kids
                  if not t_mod._at_or_past(phase_of(records, tasks, d),
                                           WORK_PHASE)]
        if behind:
            return (f"'{task_id}' cannot enter '{to_phase}' while "
                    + ", ".join(f"'{d}'" for d in behind[:5])
                    + f" {'is' if len(behind) == 1 else 'are'} short of "
                      f"'{WORK_PHASE}' — auditing a parent whose children have "
                      f"not been built audits nothing")
    if to_phase == PHASES[-1]:
        unfinished = [d for d in open_kids
                      if phase_of(records, tasks, d) != PHASES[-1]]
        if unfinished:
            return (f"'{task_id}' cannot enter '{PHASES[-1]}' while "
                    + ", ".join(f"'{d}'" for d in unfinished[:5])
                    + f" {'is' if len(unfinished) == 1 else 'are'} neither "
                      f"'{PHASES[-1]}' nor closed")
    return None


# --- the transition predicate (PURE) -------------------------------------------------

def can_advance(records, tasks, task_id, to_phase):
    """``(ok, reason)`` for moving *task_id* to *to_phase*. Pure: no appends, no
    I/O, no clock — `restat` owns the side effect (KTD8), so this can be called
    from a hook path, a dry run, or a test without writing anything.

    Backward moves short-circuit before every gate below. That is not laxity: a
    task fails its audit precisely when it is blocked, unsound, or unsatisfied,
    and a backward move is the documented handling for all three. Gate it and
    every failing task deadlocks."""
    rec = (records or {}).get(task_id)
    if rec is None:
        return False, (f"no phase record for '{task_id}' — declare it first: "
                       f"hive mindnet declare {task_id}")
    tasks = tasks or {}
    task = tasks.get(task_id)
    if task is None:
        # Every forward gate below reads the graph. A record whose task is gone
        # cannot be judged, and a gate that cannot judge refuses.
        return False, (f"'{task_id}' has a phase record but no task in the "
                       f"graph — rebuild the projection")
    to_i, cur_i = phase_index(to_phase), phase_index(rec.phase)
    if to_i is None:
        return False, (f"'{to_phase}' is not a phase — one of: "
                       + ", ".join(PHASES))
    if cur_i is not None and to_i < cur_i:
        return True, ""

    if task.status not in WORKABLE_STATUSES:
        return False, (f"'{task_id}' is {task.status} — only "
                       + ", ".join(WORKABLE_STATUSES)
                       + " may move forward; a blocked or settled task may only "
                         "fall back")
    for gate in (
        lambda: _phase_axis_refusal(task_id, rec.phase, rec.epoch,
                                    rec.satisfactions, task.triviality_bypass,
                                    to_phase),
        lambda: _subtree_unsound(tasks, task_id),
        lambda: _coupling_block(tasks, records, task_id),
        lambda: _descendant_block(tasks, records, task_id, to_phase),
    ):
        why = gate()
        if why is not None:
            return False, why
    return True, ""


# --- the one impure function ------------------------------------------------------------

def _head_branch(root):
    """Branch checked out at *root*, or None. Total, and the `paths` idiom:
    `NO_WINDOW` so a dashboard child does not flash a console window."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=2,
            creationflags=paths.NO_WINDOW,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _same_path(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, ValueError):
        return False


def _restat_fail_recorded(evs, task_id, phase, epoch, ref) -> bool:
    """True when the ledger ALREADY carries this re-stat finding for exactly
    ``(task_id, phase, epoch, ref)``.

    EXISTENCE over the raw rows, deliberately — not `Satisfaction.failed_refs`.
    Asking the FOLD whether the failure `restat` just wrote is on record made the
    guard only as good as the fold's acceptance of that write, and it failed
    OPEN. A re-stat whose row replays BEFORE the `enter` that put the task in
    that phase orphans itself and leaves ``failed_refs`` empty — after which
    EVERY call appended again. Before the append stamp existed that took nothing
    more than a same-second tie, which `read_all` broke by segment name. It is
    still reachable. `read_all` sorts on ``ts`` first (`events._order_key`), so
    a caller-supplied ``ts``, or a wall clock stepped back past a second
    boundary, puts the re-stat ahead of the transition. Inside one second the
    stamp keeps the re-stat after the `enter` it re-checks while the high-water
    mark in `append.lock` rules both stamps. A re-stat stamps only after it has
    read that `enter`, so a stamp the mark does not rule lands below the
    enter's, or repeats it, only from a clock that reads no later than stamps
    already issued (it stepped back, it has not yet ticked past the enter's
    stamp on a coarse clock, or a garbled mark ran them ahead): on either of
    `events._append_lock`'s fail-open paths (the file cannot be opened, or the
    wait times out) the re-stat stamps from the bare clock without reading or
    writing the mark; with the mark lost (`append.lock` deleted, garbled or
    unreadable) it seeds from the clock; and reading a mark that a failed write
    left stale, it can land below the enter's stamp or repeat it, a repeat
    breaking on segment name. A re-stat written by a process still running
    pre-`seq` code carries
    no stamp, and with no stamped row above it in its segment sorts FIRST in
    its second (`events.LEGACY_SEQ`).
    `hive mindnet show` calls `restat`, so that grew the hash chain once per
    look. An existence test is order-INDEPENDENT by construction and survives
    across processes, because it reads the ledger rather than a fold or a
    previous call's return value.

    Matched on the SHAPE `restat` writes — a python-verified file `fail` — not on
    "any fail here". ``failed_refs`` is built from ACCEPTED records only, so a
    fail the fold rejects (an agent-reported one for a python phase, say) is
    invisible to it; that fail gates nothing, and letting it suppress this append
    would trade the runaway for a silent hole."""
    want = str(ref)
    for ev in evs or []:
        if getattr(ev, "type", None) != "mindnet_phase":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        if p.get("action") != "satisfy" or p.get("task_id") != task_id:
            continue
        if p.get("phase") != phase or p.get("verified_by") != "python":
            continue
        # `bool` is an `int`, so a payload carrying True would match epoch 1 —
        # the guard phases.py spells the same way.
        raw_epoch = p.get("epoch")
        if isinstance(raw_epoch, bool) or raw_epoch != epoch:
            continue
        e = p.get("evidence")
        if not isinstance(e, dict):
            continue
        if e.get("result") != "fail" or e.get("kind") != "file":
            continue
        if _text(e.get("ref"), ph_mod.MAX_REF_CHARS).strip() == want:
            return True
    return False


def restat(task_id, root=None, branch=None, evs=None, tasks=None, project=None,
           session_id=None, ts=None) -> Restat:
    """Re-check the file evidence behind *task_id*'s CURRENT phase.

    The only impure function here and the only one that appends. Three rules
    make it safe to run from `show`:

    1. **A failure never moves the task** (KTD8). It records a `fail` at the
       current ``(phase, epoch)``, which the fold reads as stale evidence and
       `can_advance` reads as "you may not leave this phase". A task at
       `pr-green` must not silently regress seven steps with nothing on the
       ledger admitting it.
    2. **It appends at most once per finding.** With a `fail` already on the
       LEDGER for the same ``(phase, epoch)`` and the same ref, that record is
       reported and nothing is written — otherwise every `show` would grow the
       chain by one event. The test is `_restat_fail_recorded`, an existence
       check over the raw rows: keying it off the folded record instead made the
       guard fail open whenever the fold did not accept `restat`'s own write.
    3. **From another checkout it answers `unknown`, never `fail`** (KTD7).
       `detect_project` collapses every worktree onto one namespace (SOP 10), so
       a branch-specific plan file is legitimately absent from a sibling
       checkout. `unknown` neither satisfies nor invalidates.

    Only the current phase is re-stat-able, because KTD5 binds a `satisfy` to the
    phase the task is in: a `fail` recorded against any other phase would fold as
    a violation rather than as the finding it is."""
    if evs is None:
        evs = ev_mod.read_all(project)
    records, _orphans = fold(evs, tasks)
    rec = records.get(task_id)
    if rec is None:
        return Restat("unknown", f"no phase record for '{task_id}'")
    sat = rec.satisfaction(rec.phase, rec.epoch)
    if sat is None:
        return Restat("unknown", f"'{task_id}' has no evidence recorded for "
                                 f"'{rec.phase}' at epoch {rec.epoch}")
    if sat.kind != "file" or not sat.ref:
        return Restat("unknown", f"'{rec.phase}' takes {sat.kind or 'no'} "
                                 f"evidence, which cannot be re-stat'd")
    if root is None:
        root = paths.worktree_root()
    if root is None:
        return Restat("unknown", "no git worktree here — file evidence is "
                                 "resolved relative to the worktree root")
    if sat.root and not _same_path(sat.root, root):
        return Restat("unknown",
                      f"'{sat.ref}' was resolved under {sat.root}, this "
                      f"checkout is {root} — a file on another worktree's "
                      f"branch is absent here for legitimate reasons")
    if sat.branch:
        here = _head_branch(root) if branch is None else branch
        if here and here != sat.branch:
            return Restat("unknown",
                          f"'{sat.ref}' was resolved on branch "
                          f"'{sat.branch}', this checkout is on '{here}'")
    target, why = ph_mod.resolve_ref(rec.phase, sat.ref, root)
    if target is None:
        return Restat("unknown", why)
    try:
        present = target.is_file()
    except OSError as e:
        return Restat("unknown", f"cannot stat '{sat.ref}': {type(e).__name__}")
    if present:
        return Restat("ok", f"'{sat.ref}' is still there")

    gone = (f"re-stat: '{sat.ref}' is gone from {root} — the evidence that "
            f"cleared '{rec.phase}' no longer exists")
    if _restat_fail_recorded(evs, task_id, rec.phase, rec.epoch, sat.ref):
        note = f"already recorded at epoch {rec.epoch}; nothing appended"
        if sat.ref not in sat.failed_refs:
            # On the ledger, but the PROJECTION does not carry it — the finding
            # replayed ahead of the transition it answers: a backdated or
            # clock-stepped `ts`; a stamp the high-water mark did not rule (a
            # fail-open append, a lost mark, a mark a failed write left stale)
            # that landed below the transition's or repeated it, a repeat
            # breaking on segment name; or no stamp at all, from a writer still
            # running pre-`seq` code, which sorts first in its second unless a
            # stamped row above it in its segment carries it (see
            # `_restat_fail_recorded`). Reporting a bare `stale` here would
            # claim a gate that `can_advance` is not actually applying, so the
            # divergence is named instead of papered over. Appending again would
            # not fix it and is what this guard exists to stop.
            note += ("; the fold did not apply it — that event sorts before the "
                     "transition it answers, so this phase still reads "
                     "satisfied. Fall back to it to force a new epoch")
        return Restat("stale", f"{gone} ({note})")
    payload = {
        "action": "satisfy", "task_id": task_id, "phase": rec.phase,
        "epoch": rec.epoch, "detail": gone,
        # Derived, never accepted (KTD2): a stat IS a Python check, and this is
        # the code path that ran it.
        "verified_by": "python",
        "evidence": {"kind": "file", "ref": sat.ref, "result": "fail",
                     "detail": gone, "root": str(root), "branch": sat.branch},
    }
    ph_mod.validate_stored(payload)
    ev = ev_mod.append_event("mindnet_phase", payload, session_id=session_id,
                             tags=["mindnet", "phase"], ts=ts, project=project)
    return Restat("stale", gone, ev)
