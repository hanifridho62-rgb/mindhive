"""Where replenished work lands.

`replenish.py` decides WHAT is outstanding and refuses to know where it goes; a
sink knows one surface and nothing about lenses or gating. They live in separate
modules so the split is structural rather than a convention someone can quietly
break — the core's own test asserts it does not import a sink.

The roadmap sink is the interactive session's. It appends steps to the plan whose
`plan_id` is the session id, which is the existing convention (`sync_from_todos`
writes the session's TodoWrite state there). Three things fall out of that reuse
rather than being built:

* the steps ride the session's own Oversee card while it is alive;
* `roadmap.autoclose_session`, already wired into the SessionEnd hook, marks the
  plan interrupted when steps remain unfinished;
* `roadmap.notice` then renders it into STATE.md, which `sync` folds into the
  auto-loaded MEMORY.md the next session reads before anything else.

That last step is the forcing mechanism on this surface. Nothing asks an agent to
keep going; the work is simply in front of the next session, marked `new`.

Phase 3's loop sink lands checklist items instead, so that
`loop.task.required_remaining_count` rises and `loop.safeguards.should_exit`
refuses to exit. Same core, different sink.
"""
from . import events as ev_mod
from . import roadmap as rm_mod
from .replenish import Placement
from .sop import ID_RE


def roadmap_sink(session_id, project=None, evs=None, ts=None):
    """A sink that appends kept candidates to *session_id*'s roadmap plan.

    Returns ``(placements, dropped)``. `dropped` is what did not fit under
    `roadmap.MAX_STEPS`; those candidates get no audit row, so their fingerprints
    stay unspent and the next round may propose them again."""
    sid = str(session_id or "")
    if not ID_RE.match(sid):
        # Not silently degraded to "nothing landed": a session id that cannot be
        # a plan_id means every future round for this session lands nowhere, and
        # that is a wiring bug the caller has to see.
        raise ValueError(
            f"cannot land work for session {session_id!r}: a plan_id must be "
            "kebab-case (lowercase letters, digits, hyphens; max 64 chars)")

    def sink(kept):
        rows = ev_mod.read_all(project) if evs is None else evs
        rec = rm_mod.fold(rows).get(sid)
        # dict() per step: the fold hands back its own records, and the payload
        # we are about to build must not alias them.
        existing = [dict(s) for s in (rec["steps"] if rec else [])]
        room = max(0, rm_mod.MAX_STEPS - len(existing))
        take = list(kept)[:room]
        dropped = len(kept) - len(take)
        if not take:
            return [], dropped

        steps = existing + [
            {"text": c.title[:rm_mod.MAX_STEP_CHARS], "status": "pending",
             "new": True}
            for c in take]
        # An existing plan keeps its own ask. A plan created here inherits the
        # session's first prompt, the same source `sync_from_todos` uses, so the
        # card reads as this session's work rather than as the injector's.
        prompt = rec["prompt"] if rec else rm_mod._session_prompt(rows, sid)
        payload = {"action": "set", "plan_id": sid, "prompt": prompt,
                   "title": prompt[:rm_mod.MAX_TITLE_CHARS], "steps": steps}
        rm_mod.validate_payload(payload)
        # AUTO_TAG is load-bearing, not decoration: `autoclose_session` leaves
        # human-declared plans alone, so without it the plan would never be
        # marked interrupted and would never reach STATE.md.
        ev_mod.append_event("roadmap", payload, session_id=sid,
                            tags=["roadmap", rm_mod.AUTO_TAG, "replenish"],
                            ts=ts, project=project)
        placements = [Placement(c.fingerprint, sid, len(existing) + i)
                      for i, c in enumerate(take)]
        return placements, dropped

    return sink
