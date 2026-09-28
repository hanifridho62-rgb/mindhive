# SPDX-License-Identifier: Apache-2.0
# src/mindhive/roadmap_auto.py
"""The session auto-sync layer: TodoWrite in, roadmap events out.

`roadmap.py` owns the event contract, the deterministic fold and the read
projections. This module owns the WRITERS that keep an interactive session's
plan in step with its own TodoWrite state, and the one question all of them
have to answer first: which plan does this session's work belong to?

That question used to be answered four separate times as `plan_id ==
session_id`, and the pile of orphaned plans under the dashboard's
awaiting-resume list was the result. `auto_plan_id` is it asked once.

Every public name here is re-exported from `roadmap`, which is how callers
reach them (`roadmap.sync_from_todos`, `rm_mod.auto_plan_id`) and how
`tests/test_hookcapture.py` patches one. Splitting a module in this repo has
already moved a patch address silently once; `tests/test_roadmap_auto.py`
asserts the re-exports are the SAME objects so it cannot happen again.

`roadmap` is imported INSIDE the functions that use it, not at module scope.
The dependency runs both ways -- this module needs that one's contract, fold
and caps; that one needs these writers to re-export -- and a module-scope
import here made the pair work only when `roadmap` happened to be touched
first: `import mindhive.roadmap_auto` on its own died with `cannot import
name 'AUTO_TAG' from partially initialized module`. Nothing enforced that
order, and the one test that imports this module was one word-order away
from red. Resolving at call time removes the ordering question instead of
documenting it, and matches how the rest of the package breaks cycles.
"""
from . import events as ev_mod
from .sop import ID_RE

# --- session auto-sync (TodoWrite -> roadmap) --------------------------------
# The deterministic writer behind the Oversee panel for interactive sessions:
# the session's own TodoWrite telemetry IS its plan + live progress, piped in
# by the PostToolUse hook (hookcapture.record_post_tool_use).
#
# A session's FIRST plan is keyed by its session id, and for one session that is
# the whole story. Across sessions it was the defect: every writer here asked
# `fold(evs).get(session_id)`, so a plan could only ever belong to the session
# that created it. The session ended, `autoclose_session` interrupted the plan,
# the next session had a new id and minted a NEW plan -- and the old one, still
# holding the real progress, could no longer be matched to any live card. It sat
# under the dashboard's awaiting-resume pile forever while the injector's `(new)`
# steps landed on a plan nobody was working. `auto_plan_id` is that question
# asked once, so all four readers agree on the answer.

AUTO_TAG = "auto"

# Which plan this session actually WORKED, recorded rather than inferred.
#
# `_authored_plan_id` used to answer that from the session's LAST roadmap
# event, whatever plan it named -- so one unrelated write redirected the whole
# session close: measured, `run-99` went to `interrupted` and the session's own
# plan stayed `active` with nobody working it. A write naming another plan is
# ordinary (loop/engine's usage-cap snapshot stamps the live session and names
# its run id; so does any hand-typed `hive roadmap set --plan other`), so the
# inference was wrong rather than unlucky.
#
# A third tag is the pattern this ledger already uses for exactly this -- 
# `replenish_sinks` marks its injections `replenish` the same way. It is a
# property of the WRITE and not of the plan, which is why `roadmap._tags_for`
# refuses to inherit it: inherited, a manual `step` on a plan some other
# session had worked would claim authorship it never had.
WORK_TAG = "session-work"

# How long a paused plan stays adoptable. The bound is what keeps adoption from
# being permanent: without it, a plan interrupted in March is still the work in
# August, and an unrelated task silently inherits its ask. Three days covers a
# usage-cap rotation and a weekend; past that, new work gets a new plan.
RESUME_WINDOW_HOURS = 72


def _hours_before(iso, hours) -> str:
    """*iso* minus *hours*, in ledger stamp format, for lexical comparison.

    An unreadable clock returns a stamp no real event can predate, which
    disables ADOPTION rather than disabling the bound. Fail-closed on purpose:
    an unbounded window resurrects an ancient plan onto today's work, whereas
    falling back to a freshly minted plan is merely the old behaviour.
    """
    from datetime import datetime, timedelta, timezone
    try:
        base = datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return "9999-12-31T23:59:59Z"
    base = base.replace(tzinfo=timezone.utc) - timedelta(hours=hours)
    return base.strftime("%Y-%m-%dT%H:%M:%SZ")


def auto_plan_id(session_id, registry, *, now=None) -> str:
    """The plan id this session's auto-managed work belongs to.

    Takes the FOLDED registry rather than the events, because all three writers
    already fold -- handing this function `evs` would fold twice, and a second
    fold over a generator would see nothing at all.

    The answer is the session id in two cases out of three, and the exception is
    the point: when this session has no plan of its own and a previous one left
    an auto-managed plan INTERRUPTED, that plan is what this session continues,
    so its id is returned and the work lands there. Three properties make doing
    that automatically safe:

    * **An active plan is never adopted.** It has a live owner, and two sessions
      writing one record would overwrite each other's steps.
    * **A retired plan is never ADOPTED.** `complete` and `archive` are how a
      user says not-this-one, so they have to be the escape hatch out of
      adoption. Note the scope: this governs the two rules below, not the
      first one -- a session's OWN retired plan is still returned, and its
      next TodoWrite sets it active again. See the comment on that rule.
    * **A human-declared plan is never adopted.** The same guard the writers
      already enforce, one layer earlier.

    The newest candidate wins, so a chain of paused plans converges on the most
    recent rather than on whichever the fold happened to yield first. The known
    cost, stated plainly: an unrelated task begun within the window continues the
    previous plan's record -- `RESUME_WINDOW_HOURS` bounds that, and completing
    or archiving the plan ends it immediately.
    """
    from . import roadmap as rm_mod
    sid = str(session_id or "")
    if sid in registry:
        # This session declared its own plan, so it keeps it -- INCLUDING when
        # it has already retired it, which means one more TodoWrite revives its
        # own completed or archived plan. Measured, not assumed, and left
        # alone deliberately: `plan_id` was unconditionally `sid` with no
        # retired guard anywhere long before this resolver existed, so this is
        # the writer's oldest behaviour and not a regression -- and for
        # `complete` it is defensible, because "finished, and now there is
        # more" is an ordinary session. Refusing it needs a SECOND plan id for
        # post-retirement work in the same session, which is a schema
        # decision. `tests/test_roadmap.py` pins what it actually does.
        return sid
    # A plan this session has ALREADY continued. Load-bearing rather than an
    # optimisation: adopting a plan makes it active, so the interrupted-plan
    # rule below can never find it a second time -- and the reader that needs a
    # second look is `autoclose_session`, which would otherwise resolve to a
    # plan_id that does not exist and leave the real plan active with nobody
    # working it. Retired plans are excluded because `session_id` is stamped by
    # the LAST event, and completing a plan is an event: without that check the
    # session that closed a plan would re-open it on its next TodoWrite.
    mine = ("", "")
    for rec in registry.values():
        auto = AUTO_TAG in (rec.get("tags") or [])
        if rec.get("session_id") != sid or not auto:
            continue
        if rec["status"] in rm_mod.RETIRED_STATUSES:
            continue
        if (str(rec.get("updated") or ""), rec["plan_id"]) > mine:
            mine = (str(rec.get("updated") or ""), rec["plan_id"])
    if mine[1]:
        return mine[1]
    stamp = now or ev_mod.now_iso()
    cutoff = _hours_before(stamp, RESUME_WINDOW_HOURS)
    best = ("", "")
    for rec in registry.values():
        auto = AUTO_TAG in (rec.get("tags") or [])
        if rec["status"] != "interrupted" or not auto:
            continue
        # `updated` is the fallback because not every record carries the other:
        # `interrupted_at` is set by the interrupt itself, and a record folded
        # from a shape that never supplied one must not sort as oldest by
        # accident and lose to a plan that was actually paused earlier.
        paused = str(rec.get("interrupted_at") or rec.get("updated") or "")
        if paused < cutoff:
            continue
        # A pause stamped in the FUTURE is a bad clock or a hand-written
        # row, not a recent pause -- and because candidates are RANKED by
        # this same string, an unbounded one wins every round forever and
        # every genuinely recent plan loses to it. The window has a far
        # edge for the same reason it has a near one.
        if paused > stamp:
            continue
        if (paused, rec["plan_id"]) > best:
            best = (paused, rec["plan_id"])
    return best[1] or sid
_TODO_STATUS = {"completed": "done", "in_progress": "active",
                "pending": "pending"}


def todos_to_steps(todos) -> list:
    """Explicit-status steps from a TodoWrite ``todos`` list. Malformed items
    degrade to nothing rather than raising — the hook path must never fail a
    session over a payload quirk — and texts are truncated to the contract cap
    so one oversized todo can't invalidate the whole sync."""
    from . import roadmap as rm_mod
    steps = []
    if not isinstance(todos, list):
        return steps
    for t in todos:
        if not isinstance(t, dict):
            continue
        text = str(t.get("content") or "").strip()
        status = _TODO_STATUS.get(t.get("status"))
        if text and status:
            steps.append({"text": text[:rm_mod.MAX_STEP_CHARS], "status": status})
    return steps[:rm_mod.MAX_STEPS]


def _session_prompt(evs, session_id) -> str:
    """The session's original ask: headline of its first user_prompt event.
    Capture may lag the first TodoWrite, so a stable placeholder stands in —
    sync_from_todos treats the later prompt upgrade as a material change."""
    from . import roadmap as rm_mod
    from .briefing import headline
    for e in evs:
        if e.session_id == session_id and e.type == "user_prompt":
            text = e.payload.get("text", "") if isinstance(e.payload, dict) \
                else str(e.payload)
            t = headline(str(text))
            if t:
                return t[:rm_mod.MAX_PROMPT_CHARS]
    return f"session {session_id}"      # ID_RE caps sid at 64 chars — fits


def _step_shape(items) -> list:
    """The part of a step list a no-op comparison is allowed to see.

    Notes are excluded on purpose: a step note is commentary, and re-writing
    the whole plan because one changed would bloat the ledger."""
    return [(s["text"], s["status"], bool(s.get("new"))) for s in items]


def sync_from_todos(session_id, todos, project=None, evs=None, ts=None):
    """Auto-declare/update the session's roadmap from its TodoWrite state.
    Appends at most one ``set`` per call (the fold's upsert replaces steps
    wholesale and revives interrupted plans), and only on a material change —
    identical steps and prompt are a no-op so routine re-writes don't bloat
    the ledger. Plans not tagged ``auto`` are human-declared and never
    touched. Returns the appended event, or None when nothing applies.

    Steps marked ``new`` are CARRIED FORWARD. A ``set`` replaces the step list
    wholesale, and this writer builds that list from TodoWrite alone — so
    without this, one more TodoWrite after the replenishment injector had
    written (a resumed session is enough) would silently delete the injected
    work. TodoWrite stays authoritative for its own steps; the injected ones
    ride along behind them.

    **Injected steps win the ``MAX_STEPS`` tie-break, and todos are trimmed to
    make room.** The first cut appended them and re-capped, which meant a
    session at the 50-todo cap dropped every injected step — permanently, since
    the fingerprint was already spent and every later round refuses the
    condition as a duplicate. The two losses are not symmetric: a trimmed todo
    reappears on the next TodoWrite, an evidence-backed injected step never
    comes back."""
    from . import roadmap as rm_mod
    sid = str(session_id or "")
    if not ID_RE.match(sid):
        return None
    steps = todos_to_steps(todos)
    if not steps:
        # `[]` is not None. An empty LIST is the harness saying every task is
        # gone, and collapsing it into "nothing to say" left the deleted step
        # on the plan forever. Anything else that folds to no steps (None, a
        # malformed payload) still says nothing.
        if isinstance(todos, list) and not todos:
            return _clear_plan(sid, project=project, evs=evs, ts=ts)
        return None
    if evs is None:
        evs = ev_mod.read_all(project)
    registry = rm_mod.fold(evs)
    plan_id = auto_plan_id(sid, registry, now=ts)
    continued = plan_id != sid
    rec = registry.get(plan_id)
    if rec is not None and AUTO_TAG not in (rec.get("tags") or []):
        return None
    if rec is not None:
        live = {s["text"] for s in steps}      # this session's own todos
        carried = {s["text"]: dict(s) for s in rec["steps"] if s.get("new")}
        # A todo whose text matches an injected step INHERITS the flag rather
        # than replacing it. Copying the step into TodoWrite is the natural
        # response to reading "(new) …" in STATE.md, and dropping the carried
        # copy there would quietly erase provenance the flag promises to keep.
        merged = [dict(s, new=True) if carried.pop(s["text"], None) else s
                  for s in steps]
        # A CONTINUED plan's finished steps ARE its progress, and `set`
        # replaces the step list wholesale -- so rebuilding that list from
        # this session's TodoWrite alone would carry the plan forward and
        # zero its progress in the same event, which is the opposite of
        # continuing it. Inside the session that OWNS the plan nothing is
        # carried: TodoWrite stays authoritative there, so deleting a todo
        # has to keep deleting its step.
        if continued:
            mine = {s["text"]: s for s in merged}
            # The adopted plan's own steps are BOTH halves of what the
            # operator asked to see: the finished ones are its progress,
            # the unfinished ones are what is still left. `set` replaces
            # the step list wholesale, so rebuilding it from this session's
            # TodoWrite alone deleted the unfinished half -- the plan
            # visibly shrank and `progress` rose without any work being
            # done, which is the symptom this whole change set exists to
            # remove, re-entering through its own fix.
            #
            # The plan's ORDER is kept, because that order is the plan. A
            # step this session re-listed takes THIS session's status while
            # it is still open -- otherwise resuming would freeze a step at
            # `pending` the moment the agent started it -- but a finished
            # step stays finished, because re-listing old work as a fresh
            # todo must never un-do recorded progress.
            carried_plan = []
            for s in rec["steps"]:
                if s.get("new"):
                    continue          # injected steps ride `carried` below
                own = mine.get(s["text"])
                keep = own if (own is not None
                               and s["status"] not in ("done", "skipped")) \
                    else s
                carried_plan.append(dict(keep))
            seen = {s["text"] for s in carried_plan}
            merged = (carried_plan
                      + [s for s in merged if s["text"] not in seen])
        # Room is reserved for the LIVE session's steps BEFORE the list is
        # trimmed. Cutting the tail instead deleted every current todo the
        # moment a carried plan reached MAX_STEPS -- and `autoclose_session`
        # then read a plan with no unfinished steps left and marked it
        # `complete`, retiring it from every view. One cap collision, the
        # session's whole visible workload gone. Oldest carried steps go
        # first: a finished step already recorded its progress in the
        # plan's history, a live todo has nowhere else to be.
        # A FLOOR, because reserving the budget for carried steps FIRST was
        # the same failure in the other direction, and it measured worse:
        # 50 injected steps left `room` 0 and dropped the session's ENTIRE
        # visible workload -- every current todo, on every later call, for
        # as long as the plan stayed full -- and 48 left two of twenty.
        # Injected steps still win the budget, which is the asymmetry
        # argued for above; they no longer win all of it, because a plan
        # that cannot show the work being done NOW has failed the only job
        # it has: it is what the Oversee card and STATE.md render.
        floor = min(len(merged), rm_mod.MAX_STEPS // 2)
        room = max(rm_mod.MAX_STEPS - len(carried), floor)
        if len(merged) > room:
            drop = len(merged) - room
            kept = []
            for s in merged:
                if drop and s["text"] not in live:
                    drop -= 1
                    continue
                kept.append(s)
            merged = kept
        live_kept = merged[:room]
        steps = (live_kept + list(carried.values()))[:rm_mod.MAX_STEPS]
        # NOT a silent cap. The floor can only be honoured by dropping
        # carried steps, and an injected step that leaves a plan does not
        # come back -- its fingerprint is already spent, so no later round
        # re-proposes it. Whoever reads the plan afterwards is entitled to
        # know the plan is not the whole story.
        lost = len(live_kept) + len(carried) - len(steps)
        if lost:
            try:
                from . import hookcapture
                hookcapture.log_hook_error(
                    "roadmap_steps_capped",
                    RuntimeError(
                        f"plan {plan_id}: {lost} injected step(s) dropped "
                        f"to keep {len(live_kept)} live step(s) visible at "
                        f"the {rm_mod.MAX_STEPS}-step cap; an injected "
                        "step is not re-proposed once its fingerprint is "
                        "spent"))
            except Exception:
                pass          # a notice must never fail a sync
    asked = _session_prompt(evs, sid)
    # The ask is PROVENANCE: a continued plan keeps the prompt it was
    # declared with, because that is the one field a resuming session must
    # not overwrite -- the roadmap panel renders it as `Asked:`. The title is
    # the opposite and takes this session's headline, so the card names what
    # is being worked now instead of reading as last session's job.
    prompt = asked
    if continued and rec is not None:
        prompt = rec["prompt"]
    title = (asked if continued else prompt)[:rm_mod.MAX_TITLE_CHARS]
    # The title joins the no-op guard because it is now derived from a
    # different source than the prompt: without it, a resumed session whose
    # steps and ask both match would never retitle the card.
    unchanged = (rec is not None
                 and rec["prompt"] == prompt
                 and rec.get("title") == title
                 and _step_shape(rec["steps"]) == _step_shape(steps))
    if unchanged:
        return None
    payload = {"action": "set", "plan_id": plan_id, "prompt": prompt,
               "title": title, "steps": steps}
    rm_mod.validate_payload(payload)
    return ev_mod.append_event("roadmap", payload, session_id=sid,
                               tags=["roadmap", AUTO_TAG, WORK_TAG],
                               ts=ts, project=project)


def _clear_plan(session_id, project=None, evs=None, ts=None):
    """Every task the harness had is gone. Say so, honestly.

    `record` answers `[]` for "this session's list is empty now" and None for
    "nothing to say about this call", and that distinction used to die one line
    into this module: `if not steps: return None` collapsed them. So deleting
    tasks one at a time worked until the LAST one, and then the final step sat
    pending on the plan forever -- the Oversee card and the awaiting-resume list
    reporting work the harness had deleted, with no later call able to clear it
    because every later list is empty too.

    `roadmap set requires a non-empty steps list`, so an empty plan cannot be
    written and should not be: a plan with no steps is not a plan. Two honest
    exits instead, and which one applies is not a judgement call:

    * INJECTED steps survive. A step marked `new` came from the replenishment
      injector with evidence behind it, and this module already holds that a
      trimmed todo reappears on the next call while an injected step never comes
      back. So the harness's steps clear and the injected ones stay, as a `set`.
    * Otherwise `archive`, with a reason -- the documented state between
      finished and still open. Nothing was completed, so `complete` would lie;
      nothing is left, so `active` lies too.

    Human-declared plans are untouched, like everywhere else here."""
    from . import roadmap as rm_mod
    if evs is None:
        evs = ev_mod.read_all(project)
    registry = rm_mod.fold(list(evs))
    plan_id = auto_plan_id(session_id, registry, now=ts)
    rec = registry.get(plan_id)
    if rec is None or AUTO_TAG not in (rec.get("tags") or []):
        return None
    if rec.get("status") in rm_mod.RETIRED_STATUSES:
        return None
    if not rec.get("steps"):
        return None                      # already empty: nothing to clear
    carried = [dict(s) for s in rec["steps"] if s.get("new")]
    if carried:
        payload = {"action": "set", "plan_id": plan_id,
                   "prompt": rec["prompt"],
                   "title": rec.get("title") or rec["prompt"],
                   "steps": carried[:rm_mod.MAX_STEPS]}
    else:
        payload = {"action": "archive", "plan_id": plan_id,
                   "reason": "every task the harness had was deleted"}
    rm_mod.validate_payload(payload)
    return ev_mod.append_event("roadmap", payload, session_id=session_id,
                               tags=["roadmap", AUTO_TAG, WORK_TAG],
                               ts=ts, project=project)

def _authored_plan_id(evs, session_id) -> str:
    """The plan THIS session actually wrote to, newest write wins.

    `autoclose_session` used to resolve through `auto_plan_id`, which adopts
    an interrupted plan inside the resume window. That is right for a writer
    STARTING work and wrong for one ending it: adoption at session end cannot
    mean the session worked the plan, and the all-steps-done branch would
    then `complete` a stranger's plan on the strength of it -- retiring, from
    every view, a plan its real owner had only paused.

    Authorship is the honest question at closing time, and it also answers the
    case a session-id stamp cannot: when another session later overwrote the
    record, the fold's `session_id` is the other one's, but this session's own
    `set` is still in the log, so the plan it spent the session on still gets
    interrupted rather than left active with nobody working it.

    Total, like every other read here: a row of the wrong shape is skipped,
    and a session that wrote no roadmap event at all yields "".
    """
    out = ""
    worked = ""
    for ev in evs:
        if getattr(ev, "type", None) != "roadmap":
            continue
        if getattr(ev, "session_id", None) != session_id:
            continue
        p = getattr(ev, "payload", None)
        if isinstance(p, dict) and p.get("plan_id"):
            out = str(p["plan_id"])
            if WORK_TAG in (getattr(ev, "tags", None) or []):
                worked = out
    # A write that CLAIMED the work wins over one that merely happened
    # last, and the fallback is the old rule verbatim -- every roadmap
    # event written before `WORK_TAG` existed carries none, and a resolver
    # that required one would stop closing every plan already in the
    # ledger.
    return worked or out


def autoclose_session(session_id, project=None, evs=None, ts=None):
    """Session-end lifecycle for auto-synced plans (run by the SessionEnd
    hook): every step done/skipped -> ``complete`` (earned exit from every
    view); unfinished steps -> ``interrupt`` with cause ``session_end`` so
    the STATE.md marker carries the plan's creation context to the next
    session. Human-declared plans are left alone. Returns the appended
    event, or None when nothing applies."""
    from . import roadmap as rm_mod
    sid = str(session_id or "")
    if not ID_RE.match(sid):
        return None
    if evs is None:
        evs = ev_mod.read_all(project)
    # Resolved, not assumed: a session that CONTINUED another session's plan
    # has no plan of its own, and asking for one by session id returned None
    # here -- so the plan it spent the whole session on was never interrupted
    # and stayed active with nobody working it, missing from the very
    # awaiting-resume list it belongs in.
    # `evs` may be a generator and this needs two passes over it.
    rows = list(evs)
    registry = rm_mod.fold(rows)
    plan_id = _authored_plan_id(rows, sid)
    rec = registry.get(plan_id) if plan_id else None
    if rec is None:
        return None
    # Retired means retired, both ways out. Testing only for `completed`
    # left an ARCHIVED plan eligible for a second closing event.
    if rec["status"] in rm_mod.RETIRED_STATUSES:
        return None
    if AUTO_TAG not in (rec.get("tags") or []):
        return None
    if any(s["status"] in ("pending", "active") for s in rec["steps"]):
        if rec["status"] == "interrupted":
            return None
        payload = {"action": "interrupt", "plan_id": plan_id,
                   "cause": "session_end"}
    else:
        payload = {"action": "complete", "plan_id": plan_id}
    rm_mod.validate_payload(payload)
    return ev_mod.append_event("roadmap", payload, session_id=sid,
                               tags=["roadmap", AUTO_TAG], ts=ts,
                               project=project)
