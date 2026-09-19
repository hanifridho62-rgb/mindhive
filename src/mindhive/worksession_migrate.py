# SPDX-License-Identifier: Apache-2.0
"""The roadmap bridge, Opsi A hibrida (blueprint P1b; plan review III.1).

`roadmap.py` keeps owning the macro plan. This module COPIES each roadmap step
into a ``plan``-phase work-session step tagged ``[USER_SPEC: <step text>]``
(`migrate_roadmap`, pure) and appends the copies (`record_migrated`); it never
appends to, or retires anything in, the roadmap.

A copy's id is ``<plan_id>-<n>`` (1-based), and a ``plan``-phase
``[USER_SPEC: …]`` step with that id ANYWHERE in the namespace is what makes a
re-run idempotent (`plan_migration`). The check spans every session, not only
the target, because a plan's owner in `roadmap.fold` is the session of its
latest lifecycle event: it moves whenever a later session re-sets or advances
the plan, and a per-session check copied the whole plan again under each new
owner. For the same reason a plan already copied keeps its session -- new steps
join the session holding the earlier copies. A copy whose roadmap text has
changed since is reported ``stale``: it is not rewritten (the ledger is
append-only and a copy's status may have moved on), but it is never passed off
as simply present either.

Status mapping: ``done`` and ``skipped`` are both settled, so both copy as
``completed`` (a skipped step copied as ``pending`` kept its session active
forever); ``active`` stays ``active``; anything else is ``pending``. A plan the
roadmap has retired (completed or archived) copies every step ``completed``.

The roadmap keeps moving after a copy is made (`roadmap_auto` re-sets live
plans from the todo list), so a re-run also reports two kinds of divergence: a
held copy whose status no longer matches its roadmap step (``drifted``), and a
held copy whose roadmap step is gone (``orphaned`` -- a step removed from a plan
is settled the way a skipped one is). Each becomes a status MOVE -- an ordinary
``step_status`` append (`settle_moves`), so the ledger stays append-only -- but
only for a copy still at the status it was copied with: a status someone moved
by hand is reported, never overwritten.
"""
from __future__ import annotations

import re

from . import worksessions as ws

_ROADMAP_STATUS = {"done": "completed", "skipped": "completed", "active": "active"}


def migrate_roadmap(evs, *, now_fn=None) -> dict:
    """plan id -> the roadmap plan's steps as ``plan``-phase `WorkSessionStep`s
    tagged ``[USER_SPEC: <step text>]``. Pure: reads `roadmap.fold`, appends
    nothing, retires nothing."""
    from . import roadmap
    ts = ws._now(now_fn).strftime(ws.TS_FORMAT)
    out: dict = {}
    for plan_id, rec in roadmap.fold(evs).items():
        retired = rec.get("status") in roadmap.RETIRED_STATUSES
        steps = []
        for i, s in enumerate(rec.get("steps") or []):
            # A roadmap step of only tag-breaking characters ("[ | ]") is
            # valid there but would clean to an empty [WS] action.
            text = ws.clean_tag_text(s.get("text")) or ws.USER_SPEC_DEFAULT
            status = ("completed" if retired
                      else _ROADMAP_STATUS.get(str(s.get("status") or ""), "pending"))
            steps.append(ws.WorkSessionStep(
                step_id=f"{plan_id}-{i + 1}", phase_tag="plan",
                technical_action=ws.make_work_session_tag("plan", text, ts),
                initiative_tag=ws.make_user_spec_tag(text),
                usage_tag=ws.make_usage_tag(0, ws.DEFAULT_TOKEN_BUDGET),
                status=status, timestamp=ts))
        out[plan_id] = steps
    return out


def migrated_copies(evs) -> dict:
    """step id -> ``(session id, initiative tag, status, copied status)`` for
    every ``plan``-phase ``[USER_SPEC: …]`` step in the namespace: ``status`` is
    the folded one, ``copied status`` the one its ``step_append`` carried. The
    earliest-folded session holds an id seen in several."""
    evs = list(evs)
    copied: dict = {}
    for ev in evs:
        p = ws.step_append_payload(ev)
        if p is not None:
            copied.setdefault((ev.session_id, p["step_id"]),
                              p["status"] if p.get("status") in ws.STEP_STATUSES
                              else "pending")
    out: dict = {}
    for sid, w in ws.fold(evs).items():
        for s in w.steps:
            if s.phase_tag == "plan" and ws.parse_user_spec_tag(s.initiative_tag) is not None:
                out.setdefault(s.step_id, (sid, s.initiative_tag, s.status,
                                           copied.get((sid, s.step_id), s.status)))
    return out


def plan_migration(evs, *, plan_id=None, session=None, now_fn=None) -> list:
    """One report per roadmap plan (only *plan_id* when given): ``{plan_id,
    session_id, steps, present, stale, pending, drifted, orphaned, settle,
    hand_moved, copies, moves}``. The target is the session already holding this
    plan's copies, else *session*, else the plan's owner, else ``manual``;
    ``copies`` are the steps no session holds yet, ``stale`` the held copies
    whose roadmap text has changed, ``drifted`` those whose status no longer
    matches the roadmap's, ``orphaned`` those whose roadmap step is gone, and
    ``moves`` the ``(session id, step id, status)`` moves `settle_moves` appends
    -- one per drifted or orphaned copy that still diverges and is still at the
    status it was copied with (``settle`` counts them; ``hand_moved`` counts the
    diverging copies left alone because their status was moved by hand)."""
    from . import roadmap
    evs = list(evs)
    owners = {pid: rec.get("session_id") for pid, rec in roadmap.fold(evs).items()}
    have = migrated_copies(evs)
    out = []
    for pid, steps in migrate_roadmap(evs, now_fn=now_fn).items():
        if plan_id is not None and pid != plan_id:
            continue
        held = [have[s.step_id] for s in steps if s.step_id in have]
        todo = [s for s in steps if s.step_id not in have]
        stale = sum(1 for s in steps
                    if s.step_id in have and have[s.step_id][1] != s.initiative_tag)
        drifted = [(s.step_id, s.status) for s in steps
                   if s.step_id in have and have[s.step_id][2] != s.status]
        own = re.compile(re.escape(pid) + r"-(\d+)")
        orphaned = [(step_id, "completed") for step_id in sorted(have)
                    if (m := own.fullmatch(step_id)) and int(m.group(1)) > len(steps)]
        diverged = [(step_id, status) for step_id, status in drifted + orphaned
                    if have[step_id][2] != status]
        moves = [(have[step_id][0], step_id, status) for step_id, status in diverged
                 if have[step_id][2] == have[step_id][3]]
        out.append({"plan_id": pid,
                    "session_id": (held[0][0] if held else None) or session
                    or owners.get(pid) or "manual",
                    "steps": len(steps), "present": len(steps) - len(todo),
                    "stale": stale, "pending": len(todo), "drifted": len(drifted),
                    "orphaned": len(orphaned), "settle": len(moves),
                    "hand_moved": len(diverged) - len(moves),
                    "copies": todo, "moves": moves})
    return out


def record_migrated(session_id: str, steps, project: str | None = None) -> int:
    """Append *steps* (the ``copies`` of a `plan_migration` report) as
    ``work_session`` rows under *session_id*; returns how many were written."""
    n = 0
    for step in steps:
        ws.record_work_session_step(session_id, step, project=project)
        n += 1
    return n


def settle_moves(moves, project: str | None = None) -> int:
    """Append *moves* (the ``moves`` of a `plan_migration` report) as
    ``step_status`` rows; returns how many were written."""
    n = 0
    for session_id, step_id, status in moves:
        ws.record_step_status(session_id, step_id, status, project=project)
        n += 1
    return n


__all__ = ["migrate_roadmap", "migrated_copies", "plan_migration", "record_migrated",
           "settle_moves"]
