# SPDX-License-Identifier: Apache-2.0
"""MindNetwork task-graph handlers that move a task along the phase spine:
`mindnet declare|enter|fall-back|validate|restat`, plus the shared
`_mn_*` readers, the projection refresher and the exit-code constants
(`MN_REFUSED`, `MN_NOOP`) every `mindnet` verb answers with.
"""
import os
import sys
from contextlib import contextmanager
from .. import events

__all__ = ["MINDNET_FACULTY", "MN_REFUSED", "MN_NOOP", "_mn_error", "_mn_read",
    "_mn_refresh", "_mn_projection", "_mn_work", "_mn_stored",
    "_mn_phase_event", "_mn_no_record", "cmd_mindnet_declare",
    "cmd_mindnet_enter", "cmd_mindnet_fall_back", "cmd_mindnet_validate",
    "cmd_mindnet_restat"]


# --- mindnet: the operator surface for MN-1's phase spine --------------------
# `tasks.py` owns the graph axis, `phases.py` the evidence contract,
# `controller.py` the phase axis, `executor.py` the validation runs and
# `sidecar.py` the projection. Nothing below re-implements any of that; these
# handlers are the argv-to-contract adapter, and three rules shape all of them.
#
# 1. A DECLARE WRITES BOTH AXES. A `mindnet_task` declare does not create a
#    `PhaseRecord` — only a `mindnet_phase` declare does (`controller.fold`,
#    create-only). A command that wrote the graph half alone would print a task
#    and then silently orphan every phase event that task ever received, which
#    is exactly the failure `test_a_task_declared_only_on_the_graph_axis_orphans_
#    its_phase_events` pins in the controller suite.
#
# 2. THE SIDECAR IS WRITTEN LAST. `sidecar._staleness` compares segment size and
#    mtime, so ANY event appended after the write makes the sidecar read stale —
#    and `telemetry.busy` appends two `mindnet_activity` rows around every
#    mutation. A stale sidecar sends U9's injector to its phase-agnostic block,
#    which silently disables the phase-aware injection MN-1 exists to deliver.
#    `_mn_work` is the ONE place that ordering lives, so it cannot be got wrong
#    per-command.
#
# 3. `verified_by` IS DERIVED HERE, NEVER TAKEN FROM THE OPERATOR (KTD2). There
#    is deliberately no `--verified-by` flag. A stat is a Python check, a real
#    subprocess exit code is a Python check — and nothing else is. A phase this
#    surface cannot honestly check is REFUSED with the flag that records the
#    weaker claim, rather than stamped `python` for the operator's convenience.

# The faculty a mindnet command wakes (U10's cat feed). One name, drawn from
# `telemetry.FACULTIES` — invariant 10 forbids a second spelling, and
# `test_a_mutating_command_emits_faculty_telemetry` asserts membership rather
# than trusting the string.
MINDNET_FACULTY = "task-graph"

# Exit codes. 2 keeps the file's existing meaning — REFUSED, nothing recorded —
# and 1 is the case this surface adds: the command ran and recorded something,
# but the outcome is not the one asked for (a failing validation run, a re-stat
# that found the evidence gone, an agent-reported satisfy the fold will not
# accept). Printing a success line over any of those is the specific failure
# mode this machine exists to prevent.
MN_REFUSED = 2

MN_NOOP = 1

def _mn_error(reason) -> int:
    print(f"error: {reason}", file=sys.stderr)
    return MN_REFUSED

def _mn_read(args):
    """``(project, session, evs, tasks, PhaseState)`` — ONE ledger read per
    command, threaded into every module that would otherwise read it again."""
    from ..mindnet import controller as c_mod
    from ..mindnet import tasks as t_mod
    project = getattr(args, "project", None)
    sid = getattr(args, "session", None) or os.environ.get("MINDHIVE_SESSION")
    evs = events.read_all(project)
    tasks = t_mod.fold(evs)
    return project, sid, evs, tasks, c_mod.fold(evs, tasks)

def _mn_refresh(project):
    """Rebuild the sidecar from the ledger. The ledger is the source of truth,
    so a projection that could not be refreshed is a NOTE, never a failed
    command — the write itself already landed."""
    from ..mindnet import sidecar as sc_mod
    out = sc_mod.rebuild(project)
    if not out.written:
        print(f"note: sidecar not refreshed: {out.reason}", file=sys.stderr)
    return out

def _mn_projection(project):
    """The projection as the sidecar now reads it — i.e. what the block that
    just ran actually wrote. Cheaper than a second `read_all`, and it reports
    the fold's own answer rather than the handler's guess at it."""
    from ..mindnet import sidecar as sc_mod
    return sc_mod.read(project).state

@contextmanager
def _mn_work(project, sid, task_id=None, phase=None):
    """Wake the faculty for the duration of a mutation, then write the sidecar —
    IN THAT ORDER, ALWAYS, including on the way out of an exception.

    Rule 2 above, in one place. `telemetry.busy` emits its closing `idle` from a
    `finally`, so the refresh here is genuinely the last write of the command;
    doing it inside the block would stamp the sidecar before the `idle` row
    landed and leave it reading stale. The refresh runs even when the body
    raised, because the telemetry rows landed either way."""
    from ..mindnet import telemetry as tel_mod
    try:
        with tel_mod.busy(MINDNET_FACULTY, task_id=task_id, phase=phase,
                          project=project, session_id=sid):
            yield
    finally:
        _mn_refresh(project)

def _mn_stored(payload):
    """``None`` when *payload* satisfies the WRITER contract, else the reason.

    `phases.validate_stored` immediately before the write is the roadmap/sop
    convention, and calling it BEFORE the telemetry block matters: a contract
    violation then costs no events at all rather than a pair of orphaned
    activity rows."""
    from ..mindnet import phases as ph_mod
    try:
        ph_mod.validate_stored(payload)
    except ValueError as e:
        return str(e)
    return None

def _mn_phase_event(payload, sid, project):
    return events.append_event("mindnet_phase", payload, session_id=sid,
                               tags=["mindnet", "phase"], project=project)

def _mn_no_record(task_id) -> int:
    return _mn_error(f"no phase record for '{task_id}' — declare it on both "
                     f"axes first with: hive mindnet declare {task_id} "
                     f'--title "…"')

def cmd_mindnet_declare(args) -> int | None:
    """Declare a task on BOTH axes (rule 1). Idempotent by construction: the
    graph fold upserts a re-declare and the phase fold ignores it, so re-running
    this to sharpen a title never resets accumulated progress."""
    from ..mindnet import tasks as t_mod
    project = getattr(args, "project", None)
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    task_payload = {"action": "declare", "task_id": args.task_id,
                    "title": args.title}
    for key, value in (("parent_id", args.parent),
                       ("description", args.description),
                       ("validation_script", args.validation_script),
                       ("faculty", args.faculty)):
        if value:
            task_payload[key] = value
    if args.language:
        task_payload["language_context"] = list(args.language)
    if args.triviality_bypass:
        # FIRST DECLARE ONLY (KTD10) — `tasks.fold` records a later change as a
        # violation and drops it. Not re-checked here: one home for the rule.
        task_payload["triviality_bypass"] = True
    # The phase-axis declare names NO phase: the entry point is derived by
    # `tasks.fold` (the gate, or `execution` for a bypassed task), and a
    # caller-named entry phase is the teleport MN-2 stopped accepting.
    phase_payload = {"action": "declare", "task_id": args.task_id,
                     "verified_by": "python"}
    try:
        t_mod.validate_payload(task_payload)
    except ValueError as e:
        return _mn_error(e)
    why = _mn_stored(phase_payload)
    if why:
        return _mn_error(why)

    # Stamped on the way IN: `tasks.feed_phase` owns which phase that is.
    at = t_mod.feed_phase(_mn_projection(project), args.task_id,
                          args.triviality_bypass)
    with _mn_work(project, sid, args.task_id, at):
        events.append_event("mindnet_task", task_payload, session_id=sid,
                            tags=["mindnet", "task"], project=project)
        ev = _mn_phase_event(phase_payload, sid, project)
    rec = _mn_projection(project).records.get(args.task_id)
    print(f"mindnet declare: {args.task_id} at "
          f"'{rec.phase if rec is not None else at}' @ {ev.ts}")
    return None

def cmd_mindnet_enter(args) -> int | None:
    """Move a task forward (or back) one phase, gated by `can_advance`."""
    from ..mindnet import controller as c_mod
    project, sid, _evs, tasks, state = _mn_read(args)
    tid, to_phase = args.task_id, args.phase
    rec = state.records.get(tid)
    if rec is None:
        return _mn_no_record(tid)
    ok, why = c_mod.can_advance(state.records, tasks, tid, to_phase)
    if not ok:
        # roadmap.record's idiom: name the command that unsticks it. The two
        # moves that ever can are earning the exit criterion and recording that
        # it was not earned.
        return _mn_error(
            f'{why}. Earn it with: hive mindnet satisfy {tid} …  ·  or record '
            f'the failure with: hive mindnet fall-back {tid} <phase> '
            f'--detail "…"')
    payload = {"action": "enter", "task_id": tid, "phase": to_phase,
               "epoch": rec.epoch, "verified_by": "python"}
    why = _mn_stored(payload)
    if why:
        return _mn_error(why)
    with _mn_work(project, sid, tid, to_phase):
        ev = _mn_phase_event(payload, sid, project)
    print(f"mindnet enter: {tid} '{rec.phase}' -> '{to_phase}' @ {ev.ts}")
    return None

def cmd_mindnet_fall_back(args) -> int | None:
    """Move a task BACKWARD and pay the epoch cost (KTD4): every satisfaction at
    or past the target is invalidated, so walking forward again needs new
    evidence at the new generation."""
    from ..mindnet.tasks import phase_index
    project, sid, _evs, _tasks, state = _mn_read(args)
    tid, to_phase = args.task_id, args.phase
    rec = state.records.get(tid)
    if rec is None:
        return _mn_no_record(tid)
    to_i, cur_i = phase_index(to_phase), phase_index(rec.phase)
    if to_i is None:
        from ..mindnet.tasks import PHASES
        return _mn_error(f"'{to_phase}' is not a phase — one of: "
                         + ", ".join(PHASES))
    if cur_i is not None and to_i > cur_i:
        return _mn_error(
            f"'{to_phase}' is ahead of '{rec.phase}' — falling back means "
            f"going back. Move forward with: hive mindnet enter {tid} "
            f"{to_phase}")
    payload = {"action": "fall_back", "task_id": tid, "phase": to_phase,
               "epoch": rec.epoch, "detail": args.detail,
               "verified_by": "python"}
    why = _mn_stored(payload)
    if why:
        return _mn_error(why)
    with _mn_work(project, sid, tid, to_phase):
        ev = _mn_phase_event(payload, sid, project)
    new = _mn_projection(project).records.get(tid)
    print(f"mindnet fall-back: {tid} '{rec.phase}' -> '{to_phase}' "
          f"(epoch {rec.epoch + 1 if new is None else new.epoch}) @ {ev.ts}")
    return None

def cmd_mindnet_validate(args) -> int | None:
    """Run the task's ``validation_script`` behind U6's fail-closed gate and
    record what came back as python-verified command evidence."""
    from ..mindnet import executor as x_mod
    project, sid, evs, tasks, state = _mn_read(args)
    tid = args.task_id
    rec = state.records.get(tid)
    if rec is None:
        return _mn_no_record(tid)
    with _mn_work(project, sid, tid, rec.phase):
        out = x_mod.validate(
            tid, script=args.script, evs=evs, tasks=tasks, project=project,
            session_id=sid,
            timeout_s=args.timeout or x_mod.DEFAULT_TIMEOUT_S)
    run = out.run
    if out.event is None:
        # A refusal appends nothing at all — the run never happened, so there
        # is no evidence and no honest satisfy to write.
        return _mn_error(out.reason or run.reason)
    print(f"mindnet validate: {tid} '{rec.phase}' {run.result} "
          f"(exit {run.exit_code} in {run.duration_s}s) @ {out.event.ts}")
    if run.artifact:
        print(f"  artifact: {run.artifact}")
    if run.result != "ok":
        print(f"  {run.reason}")
        return MN_NOOP
    return None

def cmd_mindnet_restat(args) -> int | None:
    """Re-check the file evidence behind the task's current phase (KTD8).

    A failure never moves the task — it marks the phase stale, which blocks the
    way out of it. A re-stat from another checkout answers `unknown`, never
    `fail` (KTD7)."""
    from ..mindnet import controller as c_mod
    project, sid, evs, tasks, state = _mn_read(args)
    tid = args.task_id
    rec = state.records.get(tid)
    if rec is None:
        return _mn_no_record(tid)
    with _mn_work(project, sid, tid, rec.phase):
        out = c_mod.restat(tid, evs=evs, tasks=tasks, project=project,
                           session_id=sid)
    print(f"mindnet restat: {tid} {out.status} — {out.reason}")
    return MN_NOOP if out.status == "stale" else None
