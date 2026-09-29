# SPDX-License-Identifier: Apache-2.0
"""The `mindnet satisfy` handler and its evidence tiers: file, event,
python-script and agent-reported satisfaction of a task's current
phase gate. Split from `mindnet_tasks` so each stays under the
blueprint's 500-line module target.
"""
import sys
from .. import paths
from .mindnet_tasks import (MN_NOOP,
                            _mn_error,
                            _mn_no_record,
                            _mn_phase_event,
                            _mn_read,
                            _mn_stored,
                            _mn_work)

__all__ = ["_mn_file_evidence", "_mn_event_evidence", "_mn_satisfy_payload",
    "_mn_python_evidence", "_mn_agent_satisfy", "cmd_mindnet_satisfy"]


def _mn_file_evidence(args, rec):
    """File evidence for a document phase: a stat, which IS a Python check — so
    `python` is the honest label for it and nothing weaker is accepted.

    KTD7 rides along: the resolving worktree root and branch go onto the
    evidence, so a later re-stat from a sibling checkout answers `unknown`
    rather than `fail`. `detect_project` collapses every worktree onto one
    namespace (SOP 10), so a branch-specific plan file is legitimately absent
    from another checkout and calling that a failure would be a lie."""
    from ..mindnet import controller as c_mod
    from ..mindnet import phases as ph_mod
    ref = str(args.ref or "").strip()
    if not ref:
        wanted = ", ".join(ph_mod.descriptor(rec.phase)["required_dir"])
        return None, (f"'{rec.phase}' takes file evidence — name the document "
                      f"with: hive mindnet satisfy {args.task_id} --ref "
                      f"<path under {wanted}>")
    root = paths.worktree_root()
    target, why = ph_mod.resolve_ref(rec.phase, ref, root)
    if target is None:
        return None, why
    try:
        present = target.is_file()
    except OSError as e:
        return None, f"cannot stat '{ref}': {type(e).__name__}: {e}"
    if not present:
        return None, (f"'{ref}' does not exist under {root} — a satisfaction "
                      f"for evidence that is not there is precisely the claim "
                      f"this machine exists to refuse")
    detail = args.detail or f"stat: '{ref}' exists under {root}"
    evidence = {"kind": "file", "ref": ref, "result": "ok", "detail": detail,
                "root": str(root)}
    # Reuses `controller._head_branch` rather than respelling `git rev-parse`
    # here: `controller.restat` compares against exactly this value, and two
    # spellings of the same git question are two things that can drift apart
    # without either one looking wrong.
    branch = c_mod._head_branch(root)
    if branch:
        evidence["branch"] = branch
    return _mn_satisfy_payload(args.task_id, rec, detail, evidence), None

def _mn_event_evidence(args, evs, rec):
    """`execution`'s exit criterion (KTD16): a ``code_change`` event that names
    this task.

    The join key is optional and additive, so historical rows simply do not
    match. `hive log --task` is U8's producer; until it lands the corrective
    hint names the raw append that writes the same row today, because a hint
    pointing at a flag that does not exist is worse than no hint."""
    from ..mindnet import phases as ph_mod
    tid = args.task_id
    hit = None
    for ev in evs or []:
        if getattr(ev, "type", None) != "code_change":
            continue
        p = getattr(ev, "payload", None)
        if isinstance(p, dict) and p.get("task_id") == tid:
            hit = ev            # the LAST match: the newest change-set for it
    if hit is None:
        return None, (
            f"no code_change event on the ledger carries task_id '{tid}', so "
            f"'{rec.phase}' has nothing to verify. Record the change-set "
            f"against this task with: hive append code_change "
            f'\'{{"category": "update", "text": "…", "files": ["…"], '
            f'"task_id": "{tid}"}}\' --json')
    detail = (args.detail
              or f"code_change @ {hit.ts}: {hit.payload.get('text') or ''}")
    detail = str(detail)[:ph_mod.MAX_DETAIL_CHARS]
    evidence = {"kind": "event", "ref": "", "result": "ok", "detail": detail}
    return _mn_satisfy_payload(tid, rec, detail, evidence), None

def _mn_satisfy_payload(task_id, rec, detail, evidence) -> dict:
    return {"action": "satisfy", "task_id": task_id, "phase": rec.phase,
            "epoch": rec.epoch, "detail": detail,
            # Derived by THIS code path, never accepted from the operator
            # (KTD2). Both callers above ran a real check to get here.
            "verified_by": "python", "evidence": evidence}

def _mn_python_evidence(args, evs, rec):
    """``(payload, None)`` when this surface can honestly stamp `python` for
    *rec*'s current phase, else ``(None, reason)``.

    Two refusals before any evidence is looked at, and both are KTD2 rather than
    ergonomics:

    - a phase whose descriptor names a verifier WEAKER than `python` has no
      machine check to run (KTD17: no review event type exists and `hive review
      clear` appends nothing), so a `python` claim for it would be a lie;
    - a command-kind phase's only honest producer is a real run, which is
      `hive mindnet validate` — a hand-written satisfy there attests to a
      subprocess that never happened."""
    from ..mindnet import phases as ph_mod
    desc = ph_mod.descriptor(rec.phase) or {}
    need, kind = desc.get("verified_by"), desc.get("kind")
    if need != "python":
        return None, (
            f"'{rec.phase}' is {need}-reported by design — there is no machine "
            f"check that can honestly clear it, so this surface will not stamp "
            f"one. Record it with: hive mindnet satisfy {args.task_id} "
            f'--agent-reported --detail "…"')
    if kind == "command":
        return None, (
            f"'{rec.phase}' takes command evidence, and the only honest "
            f"producer of that is a real run: hive mindnet validate "
            f"{args.task_id}  ·  if the gate refused the script, record the "
            f"refusal with: hive mindnet satisfy {args.task_id} "
            f'--agent-reported --detail "<the refusal reason>"')
    if kind == "file":
        return _mn_file_evidence(args, rec)
    if kind == "event":
        return _mn_event_evidence(args, evs, rec)
    return None, (f"'{rec.phase}' has no evidence descriptor this surface "
                  f"knows how to check")

def _mn_agent_satisfy(args, project, sid, evs, tasks, rec) -> int | None:
    """The KTD15 escape hatch: an AGENT-reported satisfy carrying its reason
    verbatim, so a fail-closed gate's false positive cannot deadlock a task
    while the ledger still shows Python did not verify it.

    `executor.agent_satisfy` returns ``clears`` and this MUST read it. Against
    the committed descriptors it is False for `audit-bug` and, since MN-8b, for
    `pr-green` — both name `python` as their verifier, so
    `controller._apply_satisfy` refuses the weaker claim, the event folds as a
    violation, and the phase stays unsatisfied. Printing a success line over
    that no-op is the exact dishonesty the two-validator contract exists to
    stop.

    The two are not the same situation and the warning is worth reading in
    both. At `audit-bug` a False here is a GAP — the python verifier refused
    and the hatch cannot close it. At `pr-green` it is the DESIGN: a task must
    not be closeable by an agent asserting its own PR is green. Either way the
    remedy is the same, a mechanical run through `hive mindnet validate`."""
    from ..mindnet import executor as x_mod
    tid = args.task_id
    if not str(args.detail or "").strip():
        return _mn_error(
            f"--agent-reported must carry the reason verbatim: hive mindnet "
            f'satisfy {tid} --agent-reported --detail "…" — an unexplained '
            f"agent claim is the self-attestation this contract exists to stop")
    out = err = None
    with _mn_work(project, sid, tid, rec.phase):
        try:
            out = x_mod.agent_satisfy(tid, args.detail, evs=evs, tasks=tasks,
                                      project=project, session_id=sid)
        except ValueError as e:
            err = e
    if err is not None:
        return _mn_error(err)
    print(f"mindnet satisfy: {tid} '{rec.phase}'@{rec.epoch} recorded "
          f"(verified by agent) @ {out.event.ts}")
    if not out.clears:
        # Flushed first so the warning cannot surface AHEAD of the line it
        # qualifies: stdout is block-buffered when piped and stderr is not, so
        # a log would otherwise read "did NOT clear" before "recorded".
        sys.stdout.flush()
        print(f"warning: this did NOT clear '{rec.phase}' — {out.note}",
              file=sys.stderr)
        return MN_NOOP
    return None

def cmd_mindnet_satisfy(args) -> int | None:
    """Record that the task's CURRENT phase cleared its exit criterion.

    The phase is never named on the command line: KTD5 binds a satisfaction to
    the phase the task is actually in, so accepting one here would only produce
    events the fold records as violations."""
    project, sid, evs, tasks, state = _mn_read(args)
    tid = args.task_id
    rec = state.records.get(tid)
    if rec is None:
        return _mn_no_record(tid)
    if args.agent_reported:
        return _mn_agent_satisfy(args, project, sid, evs, tasks, rec)
    payload, why = _mn_python_evidence(args, evs, rec)
    if payload is None:
        return _mn_error(why)
    why = _mn_stored(payload)
    if why:
        return _mn_error(why)
    with _mn_work(project, sid, tid, rec.phase):
        ev = _mn_phase_event(payload, sid, project)
    print(f"mindnet satisfy: {tid} '{rec.phase}'@{rec.epoch} ok "
          f"(verified by python) @ {ev.ts}")
    return None
