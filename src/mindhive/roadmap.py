# src/mindhive/roadmap.py
"""Event-sourced roadmap registry: the ask, the plan, and the progress.

A roadmap captures one piece of work as the user framed it: the original
prompt (**what was asked**), the ordered plan steps built from it (**what the
plan is**), and per-step statuses (**how far along it is**). The registry is
event-sourced on the append-only ledger — every ``set`` / ``step`` /
``interrupt`` / ``resume`` / ``complete`` is one semantic ``roadmap`` event,
and the current registry is a deterministic, total fold over those events in
timestamp order (the ``sop.py`` idiom).

Two projections consume it:

- The dashboard's Oversee section renders each plan as an interactive roadmap
  (prompt, progress bar, clickable step nodes) via ``/api/oversee``.
- ``notice()`` renders every *interrupted* plan as a STATE.md block — the
  out-of-usage marker. When a run is cut off mid-plan (Claude usage limits:
  the loop engine's ``api_cap`` / ``budget_cap``, or a manual
  ``hive roadmap interrupt``), the marker preserves the creation context so
  the next session auto-loads the big picture and resumes from the next step
  instead of starting over.

Like the change-log and SOPs, roadmap events are registry/audit content:
consolidation never absorbs them and display-time dedupe never collapses them.
"""
from . import events as ev_mod
from .sop import ID_RE

ACTIONS = ("set", "step", "interrupt", "resume", "complete")
STATUSES = ("pending", "active", "done", "skipped")

# Size caps bound every event (and therefore the STATE.md notice): the marker
# must preserve context, not become a context-budget regression (invariant 4).
MAX_PROMPT_CHARS = 2000
MAX_TITLE_CHARS = 120
MAX_STEPS = 50
MAX_STEP_CHARS = 300
MAX_NOTE_CHARS = 500
MAX_CAUSE_CHARS = 120

# Notice rendering caps: done steps collapse to a tail, pending to a head, so
# one interrupted plan costs a bounded slice of the auto-loaded MEMORY.md.
_NOTICE_DONE_TAIL = 3
_NOTICE_PENDING_HEAD = 3


def _norm_steps(raw):
    """Normalize a ``set`` payload's steps into ``[{text, status, note}]``.
    Plain strings default to pending; when no step carries an explicit
    status, the first becomes active so the roadmap always shows where work
    starts. Assumes validate_payload already accepted *raw*."""
    steps, explicit = [], False
    for s in raw:
        if isinstance(s, dict):
            status = s.get("status") or "pending"
            explicit = explicit or bool(s.get("status"))
            steps.append({"text": str(s.get("text", "")).strip(),
                          "status": status,
                          "note": str(s.get("note") or "").strip()})
        else:
            steps.append({"text": str(s).strip(), "status": "pending",
                          "note": ""})
    if steps and not explicit:
        steps[0]["status"] = "active"
    return steps


def validate_payload(payload) -> None:
    """Enforce the roadmap event contract at every append surface (CLI
    ``hive roadmap``, raw ``hive append roadmap``, engine snapshot). The
    event store stays type-agnostic — the contract lives here, mirroring
    changelog.py / sop.py."""
    if not isinstance(payload, dict):
        raise ValueError("roadmap payload must be an object")
    action = payload.get("action")
    if action not in ACTIONS:
        raise ValueError(
            "roadmap requires an action — one of: " + ", ".join(ACTIONS))
    plan_id = payload.get("plan_id") or ""
    if not ID_RE.match(plan_id):
        raise ValueError(
            "roadmap requires a kebab-case plan_id (lowercase letters, "
            "digits, hyphens; max 64 chars), e.g. a run id or 'auth-refactor'")
    title = payload.get("title")
    if title is not None and len(str(title).strip()) > MAX_TITLE_CHARS:
        raise ValueError(f"roadmap title exceeds {MAX_TITLE_CHARS} chars")
    note = payload.get("note")
    if note is not None and len(str(note)) > MAX_NOTE_CHARS:
        raise ValueError(f"roadmap note exceeds {MAX_NOTE_CHARS} chars")
    if action == "set":
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(
                "roadmap set requires a prompt — the original ask this plan "
                "was built from")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(
                f"roadmap prompt exceeds {MAX_PROMPT_CHARS} chars — keep the "
                "headline of the ask; long context belongs in the ledger's "
                "own prompt capture")
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("roadmap set requires a non-empty steps list")
        if len(steps) > MAX_STEPS:
            raise ValueError(f"roadmap set exceeds {MAX_STEPS} steps — plan "
                             "at a coarser grain")
        for s in steps:
            if isinstance(s, dict):
                text = str(s.get("text") or "").strip()
                status = s.get("status")
                if status is not None and status not in STATUSES:
                    raise ValueError(
                        "roadmap step status must be one of: "
                        + ", ".join(STATUSES))
            else:
                text = str(s).strip()
            if not text:
                raise ValueError("roadmap steps must be non-empty text")
            if len(text) > MAX_STEP_CHARS:
                raise ValueError(
                    f"roadmap step exceeds {MAX_STEP_CHARS} chars")
    elif action == "step":
        index = payload.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError("roadmap step requires a non-negative integer "
                             "index (0-based)")
        if payload.get("status") not in STATUSES:
            raise ValueError(
                "roadmap step requires a status — one of: "
                + ", ".join(STATUSES))
    elif action == "interrupt":
        cause = str(payload.get("cause") or "").strip()
        if not cause:
            raise ValueError(
                "roadmap interrupt requires a cause, e.g. 'usage_limit'")
        if len(cause) > MAX_CAUSE_CHARS:
            raise ValueError(
                f"roadmap cause exceeds {MAX_CAUSE_CHARS} chars")
    # resume / complete: plan_id is enough.


def _progress(steps) -> dict:
    return {"done": sum(1 for s in steps if s["status"] == "done"),
            "total": len(steps)}


def _enforce_single_active(steps, changed_index):
    """One current step at a time. When *changed_index* was set active, demote
    every other active step to pending; when nothing is active anymore (the
    active step just finished), promote the first pending step so the roadmap
    keeps pointing at where work is."""
    if steps and 0 <= changed_index < len(steps) \
            and steps[changed_index]["status"] == "active":
        for i, s in enumerate(steps):
            if i != changed_index and s["status"] == "active":
                s["status"] = "pending"
        return
    if any(s["status"] == "active" for s in steps):
        return
    for s in steps:
        if s["status"] == "pending":
            s["status"] = "active"
            return


def fold(evs) -> dict:
    """Deterministic fold of roadmap events into plan_id -> record. Total —
    it never raises on historical data: a step on an unknown plan or an
    out-of-range index is skipped, unknown actions are skipped, and lifecycle
    actions on unknown plans are no-ops. ``set`` upserts (a re-plan replaces
    the steps wholesale); re-``set`` after complete revives the plan."""
    plans: dict = {}
    for ev in evs:
        if ev.type != "roadmap" or not isinstance(ev.payload, dict):
            continue
        p = ev.payload
        action, plan_id = p.get("action"), p.get("plan_id")
        if action not in ACTIONS or not plan_id:
            continue
        rec = plans.get(plan_id)
        if action == "set":
            steps_raw = p.get("steps")
            if not isinstance(steps_raw, list) or not steps_raw:
                continue
            steps = _norm_steps(steps_raw)
            if not all(s["text"] and s["status"] in STATUSES for s in steps):
                continue
            prompt = str(p.get("prompt") or "").strip()
            if not prompt:
                continue
            plans[plan_id] = {
                "plan_id": plan_id,
                "prompt": prompt,
                "title": str(p.get("title") or "").strip(),
                "steps": steps,
                "status": "active",
                "cause": "",
                "interrupted_at": "",
                "created": rec["created"] if rec else ev.ts,
                "updated": ev.ts,
                "version": (rec["version"] + 1) if rec else 1,
                "session_id": ev.session_id,
                "tags": list(ev.tags or []),
                "progress": _progress(steps),
            }
            continue
        if rec is None:
            continue  # lifecycle action on an unknown plan: nothing to do
        rec = dict(rec, steps=[dict(s) for s in rec["steps"]])
        if action == "step":
            index, status = p.get("index"), p.get("status")
            if not isinstance(index, int) or isinstance(index, bool) \
                    or not (0 <= index < len(rec["steps"])) \
                    or status not in STATUSES:
                continue
            rec["steps"][index]["status"] = status
            note = str(p.get("note") or "").strip()
            if note:
                rec["steps"][index]["note"] = note
            _enforce_single_active(rec["steps"], index)
        elif action == "interrupt":
            cause = str(p.get("cause") or "").strip()
            if not cause:
                continue
            rec["status"] = "interrupted"
            rec["cause"] = cause
            rec["interrupted_at"] = ev.ts
            note = str(p.get("note") or "").strip()
            if note:
                rec["interrupt_note"] = note
        elif action == "resume":
            rec["status"] = "active"
            rec["cause"] = ""
            rec["interrupted_at"] = ""
            rec.pop("interrupt_note", None)
        elif action == "complete":
            rec["status"] = "completed"
        rec["updated"] = ev.ts
        rec["version"] += 1
        rec["session_id"] = ev.session_id
        rec["tags"] = list(ev.tags or [])
        rec["progress"] = _progress(rec["steps"])
        plans[plan_id] = rec
    return plans


def plans(evs, include_completed=False) -> list:
    """Plan records in stable delivery order: (created, plan_id). Completed
    plans are hidden by default — they earned their exit from every view."""
    out = [r for r in fold(evs).values()
           if include_completed or r["status"] != "completed"]
    return sorted(out, key=lambda r: (r["created"], r["plan_id"]))


def get(evs, plan_id):
    """The record for *plan_id* regardless of status, or None."""
    return fold(evs).get(plan_id)


def interrupted(evs) -> list:
    """Interrupted plans in delivery order — the out-of-usage markers."""
    return [r for r in plans(evs) if r["status"] == "interrupted"]


def next_step(rec):
    """The step the work should pick back up at: the active step, else the
    first pending one, else None (everything done/skipped)."""
    for s in rec["steps"]:
        if s["status"] == "active":
            return s
    for s in rec["steps"]:
        if s["status"] == "pending":
            return s
    return None


def steps_from_plan_items(items) -> list:
    """Build explicit-status steps from a loop fix_plan split
    (``task.plan_items``): checked work is done, the first unchecked-required
    item is where work stopped (active), the rest stay pending, and
    Future/Optional items are skipped — the engine's interruption snapshot.
    Texts are truncated to the contract cap so an oversized checklist item can
    never invalidate (and thereby silently drop) the whole marker."""
    steps = []
    for t in items.get("done") or []:
        steps.append({"text": t[:MAX_STEP_CHARS], "status": "done"})
    for i, t in enumerate(items.get("planned") or []):
        steps.append({"text": t[:MAX_STEP_CHARS],
                      "status": "active" if i == 0 else "pending"})
    for t in items.get("not_yet") or []:
        steps.append({"text": t[:MAX_STEP_CHARS], "status": "skipped"})
    return steps[:MAX_STEPS]


def record(action, plan_id, prompt=None, title=None, steps=None, index=None,
           status=None, note=None, cause=None, tags=None, session_id=None,
           project=None, evs=None):
    """Append one roadmap event after registry-aware guards: lifecycle
    actions on a missing plan (or in the wrong state) are almost always typos
    — fail with the corrective command instead of appending a dead event.
    (The raw ``hive append roadmap`` path skips these guards on purpose:
    validate_payload still holds the shape contract, and fold() stays total.)"""
    payload = {"action": action, "plan_id": plan_id}
    if prompt is not None:
        payload["prompt"] = prompt.strip()
    if title is not None:
        payload["title"] = title.strip()
    if steps is not None:
        payload["steps"] = steps
    if index is not None:
        payload["index"] = index
    if status is not None:
        payload["status"] = status
    if note is not None:
        payload["note"] = note.strip()
    if cause is not None:
        payload["cause"] = cause.strip()
    validate_payload(payload)
    if evs is None:
        evs = ev_mod.read_all(project)
    current = fold(evs)
    rec = current.get(plan_id)
    if action != "set":
        if rec is None:
            known = ", ".join(sorted(current)) or "(none)"
            raise ValueError(
                f"no roadmap '{plan_id}' — declare it first with: "
                f"hive roadmap set {plan_id} --prompt … --step … "
                f"(known ids: {known})")
        if rec["status"] == "completed" and action != "complete":
            raise ValueError(
                f"roadmap '{plan_id}' is completed — revive it with a new: "
                f"hive roadmap set {plan_id} …")
        if action == "step" and not (
                isinstance(index, int) and 0 <= index < len(rec["steps"])):
            raise ValueError(
                f"step index out of range — roadmap '{plan_id}' has "
                f"{len(rec['steps'])} steps (0-based)")
        if action == "interrupt" and rec["status"] == "interrupted":
            raise ValueError(
                f"roadmap '{plan_id}' is already interrupted "
                f"(cause: {rec['cause']}) — resume it with: "
                f"hive roadmap resume {plan_id}")
        if action == "resume" and rec["status"] != "interrupted":
            raise ValueError(
                f"roadmap '{plan_id}' is not interrupted — nothing to resume")
        if action == "complete" and rec["status"] == "completed":
            raise ValueError(f"roadmap '{plan_id}' is already completed")
    return ev_mod.append_event(
        "roadmap", payload, session_id=session_id,
        tags=["roadmap"] + [t for t in (tags or []) if t != "roadmap"],
        project=project)


def _step_line(s, marker) -> str:
    note = f" — {s['note']}" if s.get("note") else ""
    return f"- {marker} {s['text']}{note}"


def notice(evs) -> str:
    """The "⚠ Interrupted plan" STATE.md block — the out-of-usage marker.
    Empty string when no plan is interrupted. Deterministic; each plan's step
    list is capped (done tail + pending head) so the marker preserves the big
    picture without regressing the auto-loaded context budget."""
    marks = interrupted(evs)
    if not marks:
        return ""
    out = ["## ⚠ Interrupted plan — creation context preserved", "",
           "Work below was interrupted before it finished (e.g. Claude usage "
           "ran out). The original ask and the plan's progress are the "
           "creation context: pick the work up at the next step instead of "
           "starting over. Resume: `hive roadmap resume <id>` · finished: "
           "`hive roadmap complete <id>` · full detail: "
           "`hive roadmap show <id>`.", ""]
    for r in marks:
        head = r["title"] or r["plan_id"]
        prog = r["progress"]
        out.append(f"### {head}  `[{r['plan_id']}]`")
        out.append(f"- **Interrupted:** {r['interrupted_at']} "
                   f"(cause: {r['cause']})")
        out.append(f"- **Asked:** {r['prompt']}")
        out.append(f"- **Progress:** {prog['done']}/{prog['total']} steps done")
        done = [s for s in r["steps"] if s["status"] == "done"]
        hidden = len(done) - _NOTICE_DONE_TAIL
        if hidden > 0:
            out.append(f"- [x] … {hidden} earlier step(s) done")
        for s in done[-_NOTICE_DONE_TAIL:]:
            out.append(_step_line(s, "[x]"))
        nxt = next_step(r)
        if nxt is not None:
            out.append(_step_line(nxt, "[→] NEXT:"))
        pending = [s for s in r["steps"]
                   if s["status"] == "pending" and s is not nxt]
        for s in pending[:_NOTICE_PENDING_HEAD]:
            out.append(_step_line(s, "[ ]"))
        if len(pending) > _NOTICE_PENDING_HEAD:
            out.append(f"- [ ] … {len(pending) - _NOTICE_PENDING_HEAD} "
                       "more step(s)")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def ledger_body(payload) -> str:
    """Self-describing markdown body for this event's LEDGER entry, so
    keyword recall finds plans by id, prompt, step text, or cause."""
    action = payload.get("action", "")
    head = f"[{action}] roadmap {payload.get('plan_id', '')}"
    title = str(payload.get("title") or "").strip()
    if title:
        head += f" — {title}"
    parts = [head]
    prompt = str(payload.get("prompt") or "").strip()
    if prompt:
        parts.append(f"Asked: {prompt}")
    steps = payload.get("steps")
    if isinstance(steps, list) and steps:
        lines = []
        for s in steps:
            if isinstance(s, dict):
                lines.append(f"- [{s.get('status', 'pending')}] "
                             f"{str(s.get('text') or '').strip()}")
            else:
                lines.append(f"- {str(s).strip()}")
        parts.append("\n".join(lines))
    if action == "step":
        parts.append(f"step {payload.get('index')} -> "
                     f"{payload.get('status', '')}")
    cause = str(payload.get("cause") or "").strip()
    if cause:
        parts.append(f"cause: {cause}")
    note = str(payload.get("note") or "").strip()
    if note:
        parts.append(note)
    return "\n\n".join(parts)
