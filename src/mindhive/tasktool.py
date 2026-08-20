# src/mindhive/tasktool.py
"""The harness's per-item task tools, folded back into a whole-list snapshot.

`roadmap.sync_from_todos` needs the WHOLE todo list, because a `set` replaces a
plan's steps wholesale. The tool that used to supply one -- `TodoWrite`, which
posts its entire list on every call -- is not the tool this harness sends. It
sends `TaskCreate` and `TaskUpdate`, one item at a time, and neither the call
nor its result carries the other items. So the list has to be accumulated
across calls, and this module is that accumulator: a plain per-namespace JSON
sidecar beside `harvest_state.json` and `context_budget.json`, keyed by session.

**What this fixes.** Measured across all 592 transcripts on this machine before
a line was written: `TodoWrite` 0 calls, `TaskCreate` 239, `TaskUpdate` 346 --
and 0 auto-tagged roadmap events in 61 namespaces, while the PostToolUse hook
the sync hangs off had appended 23,973 events with no error ever logged. The
whole session-roadmap feature (`auto_plan_id`, `plan_for_session`, the Oversee
card's plan, the roadmap progress on the session strip) has therefore never
received a single real input. It was not broken; nothing was ever handed to it.
That is why the dashboard's awaiting-resume pile sat empty and its progress
never moved.

Two facts from that measurement shape the code, and a plausible guess gets both
wrong:

* **The id is READ off the result, never counted.** Tasks created in one
  parallel batch answer out of order -- one real session issued five creates and
  was handed ids 5, 4, 1, 3, 2, in that order. A creation-order counter would
  assign every later status to the wrong task, so the plan would show someone
  else's progress. 238 of 239 real results carry `Task #N created`; the one that
  does not is the batch (`{"tasks": [...]}`) form, which lands under its own
  subject instead and simply cannot be updated by id afterwards.
* **`deleted` is a real status** (5 of 346 updates) and is not a step state. A
  deleted task leaves the list rather than becoming a step, which is how an
  abandoned batch stops being reported as pending work.

**Bounds and concurrency, stated plainly.** The sidecar is capped at
`MAX_SESSIONS` sessions (least-recently-touched evicted) and `MAX_TASKS` per
session (lowest ids evicted, because a live plan is about the newest work), so
it cannot grow without limit. Every PostToolUse hook is its own process, so two
tool calls finishing at once can race and the later write wins -- the loss is
one missed resync, corrected by the next call, and the hook path is fail-open
around all of it. Reads never raise: a corrupt or half-written sidecar reads as
empty rather than breaking a session.

The sidecar is per NAMESPACE while task ids are per session, so one session
working across two projects accumulates part of its list in each. That is the
same split the ledger already makes -- events land in the namespace detected at
the call -- and keeping the two consistent matters more than keeping one list
whole, since the plan those steps land on is per-namespace too.
"""
import json

from . import paths

# The two tools whose calls this module folds. Named here so the hook and the
# tests share one list rather than two spellings that can drift apart.
TASK_TOOLS = ("TaskCreate", "TaskUpdate")

SIDECAR = "task_tools.json"

# Statuses the roadmap writer understands (`roadmap_auto._TODO_STATUS`), plus
# the one it does not: `deleted` removes the task instead of describing it.
STATUSES = ("pending", "in_progress", "completed")
DELETED = "deleted"

MAX_TASKS = 50          # == roadmap.MAX_STEPS: more would be trimmed anyway
MAX_SESSIONS = 12
MAX_TEXT = 300          # == roadmap.MAX_STEP_CHARS, so this cap binds nothing

_SUBJECT_KEY = "s:"     # for a create whose result carried no id


def sidecar_path(project=None):
    """Where the accumulated per-session task state lives."""
    return paths.project_dir(project) / SIDECAR


def load(project=None) -> dict:
    """The whole sidecar, or ``{}``. Never raises: a missing, unreadable or
    malformed file is indistinguishable from no state yet, and the hook path
    must not fail a session over either."""
    try:
        raw = json.loads(sidecar_path(project).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def save(state, project=None) -> None:
    """Write the sidecar, evicting past the bounds first. Best-effort."""
    try:
        p = sidecar_path(project)
        p.parent.mkdir(parents=True, exist_ok=True)
        paths.write_text_atomic(p, json.dumps(_evicted(state), indent=2))
    except Exception:
        pass


def _seq(entry) -> int:
    """An entry's write order, 0 when it predates the counter."""
    try:
        return int((entry or {}).get("n") or 0)
    except (TypeError, ValueError):
        return 0


def _next_seq(state) -> int:
    """The next write-order number.

    Recency is counted, not timed. `seen` is a ledger stamp with one-SECOND
    resolution, and two tool calls inside one second is the ordinary case -- so
    ranking by it made eviction a coin toss between a session still being
    worked and one that went quiet an hour ago. The counter is exact whatever
    the clock does. `seen` stays because a human reading the sidecar wants it.
    """
    return 1 + max((_seq(v) for v in state.values()), default=0)


def _evicted(state) -> dict:
    """`state` reduced to the `MAX_SESSIONS` most recently written sessions."""
    if len(state) <= MAX_SESSIONS:
        return state
    ranked = sorted(state.items(),
                    key=lambda kv: (_seq(kv[1]), str(kv[0])), reverse=True)
    return dict(ranked[:MAX_SESSIONS])


def _order(key):
    """Sort key putting numbered tasks in numeric order, id-less ones last.

    Numeric order, not lexical: `"10" < "9"` as strings, and a plan whose tenth
    step sorts before its ninth is a plan in the wrong order."""
    return (0, int(key), "") if key.isdigit() else (1, 0, key)


def _result_id(response) -> str:
    """The task id the harness reported, or "" when the result has none."""
    import re

    from . import hookcapture
    # One reader for "text out of a tool_response", shared with the Skill path.
    # Every observed task result is a plain string, but the hook payload's
    # `tool_response` is not contractually one, and two readers that disagree
    # about dicts and content blocks is a bug waiting for a harness change.
    m = re.search(r"[Tt]ask #(\d+)", hookcapture._response_text(response))
    return m.group(1) if m else ""


def _text(value) -> str:
    return str(value or "").strip()[:MAX_TEXT]


def _create(tasks, inp, response) -> bool:
    """Apply one TaskCreate. True when the list changed."""
    subject = _text(inp.get("subject"))
    if not subject:
        # The batch form: `{"tasks": [{subject: ...}, ...]}`, one observed
        # result of 239 and the only one with no id to read. Folding each entry
        # keeps the work visible; the alternative is dropping a whole plan.
        batch = inp.get("tasks")
        if not isinstance(batch, list):
            return False
        return any(_create(tasks, t, None) for t in batch
                   if isinstance(t, dict))
    status = inp.get("status")
    rec = {"text": subject,
           "status": status if status in STATUSES else "pending"}
    # Keyed by subject when the id is unknown, so the SAME create arriving
    # twice updates one entry instead of duplicating a step -- it just can
    # never be reached by a later TaskUpdate, which carries only an id.
    tasks[_result_id(response) or (_SUBJECT_KEY + subject)] = rec
    return True


def _update(tasks, inp) -> bool:
    """Apply one TaskUpdate. False when it names a task we have never seen."""
    key = _text(inp.get("taskId"))
    rec = tasks.get(key)
    if rec is None:
        # A bare id cannot be materialised into a step: the sidecar can
        # legitimately lack the task (created before the hook was wired, or
        # evicted), and inventing one would put a blank line on the plan.
        return False
    if inp.get("status") == DELETED:
        del tasks[key]
        return True
    subject = _text(inp.get("subject"))
    if subject:
        rec["text"] = subject
    if inp.get("status") in STATUSES:
        rec["status"] = inp["status"]
    # An update that changes nothing this module models (`addBlockedBy` rides
    # 14 of the observed ones) still counts: the caller wants the current list,
    # and returning nothing there would skip a legitimate resync.
    return True


def todos(tasks) -> list:
    """The accumulated tasks as a TodoWrite-shaped list, in the harness's own
    id order -- exactly what `roadmap.sync_from_todos` expects."""
    out = []
    for key in sorted(tasks, key=_order):
        rec = tasks[key]
        if isinstance(rec, dict) and rec.get("text"):
            out.append({"content": rec["text"],
                        "status": rec.get("status") or "pending"})
    return out[:MAX_TASKS]


def record(session_id, tool, inp, response, project=None):
    """Fold one task-tool call into the session's list and return the whole
    list, or None when the call says nothing this module can use.

    None and ``[]`` are different answers on purpose: None is "nothing to say
    about this call", ``[]`` is "this session's list is empty now" -- which is
    what deleting the last task means.
    """
    from . import events as ev_mod
    sid = str(session_id or "")
    if not sid or tool not in TASK_TOOLS or not isinstance(inp, dict):
        return None
    state = load(project)
    entry = state.get(sid) or {}
    tasks = entry.get("tasks")
    if not isinstance(tasks, dict):
        tasks = {}
    changed = (_create(tasks, inp, response) if tool == "TaskCreate"
               else _update(tasks, inp))
    if not changed:
        return None
    if len(tasks) > MAX_TASKS:
        # The newest work is what a live plan is about, so the lowest ids go.
        for key in sorted(tasks, key=_order)[:len(tasks) - MAX_TASKS]:
            del tasks[key]
    state[sid] = {"tasks": tasks, "seen": ev_mod.now_iso(),
                  "n": _next_seq(state)}
    save(state, project)
    return todos(tasks)
