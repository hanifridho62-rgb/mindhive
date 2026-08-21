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
  else's progress. **And the result to read is the STRUCTURED one, not the prose
  line.** The first cut of this module matched `Task #N created`, counted 238 of
  239 -- and counted them in the transcript's RENDERED text, which is what a
  human reads back, not the `tool_response` the hook is handed. On the first real
  traffic this feature ever received, five creates and one update in production,
  every create fell through to the subject fallback, the update looked up an id
  that was never a key, and the plan sat at 0/5. Measured on the transcript that
  caught it: `"toolUseResult":"Task #` appears 0 times, while every task result
  carries `taskId`. Both shapes are read now, structured first, because both are
  real and neither is contractual. The batch (`{"tasks": [...]}`) form carries no
  id at all and lands under its own subject, where a later TaskUpdate cannot
  reach it -- and `_update` now says so out loud rather than freezing quietly.
* **`deleted` is a real status** (5 of 346 updates) and is not a step state. A
  deleted task leaves the list rather than becoming a step, which is how an
  abandoned batch stops being reported as pending work.

**Bounds and concurrency, stated plainly.** The sidecar is capped at
`MAX_SESSIONS` sessions (least-recently-touched evicted) and `MAX_TASKS` per
session (lowest ids evicted, because a live plan is about the newest work), so
it cannot grow without limit. Every PostToolUse hook is its own process, so the
read-modify-write is a CROSS-PROCESS critical section and it is held under an
O_EXCL lock (`_exclusive`). The first cut of this paragraph called an
unserialised race "one missed resync, corrected by the next call", and that was
wrong in the way that matters: measured with two processes and 25 creates each,
one session's entire list was absent from the file, and a lost create can never
be reached again because `_update` refuses ids it has not seen. The lock fails
open -- an unserialised write is a bad day, a hook that blocks or raises is
every session on this machine. Reads never raise either: a corrupt or
half-written sidecar reads as empty rather than breaking a session.

The sidecar is per NAMESPACE while task ids are per session, so one session
working across two projects accumulates part of its list in each. That is the
same split the ledger already makes -- events land in the namespace detected at
the call -- and keeping the two consistent matters more than keeping one list
whole, since the plan those steps land on is per-namespace too.
"""
import contextlib
import json
import os
import time

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

# Serialising the sidecar's read-modify-write. Every PostToolUse hook is its
# own process, so `load` ... `save` is a cross-process critical section: two
# writers folding onto the same stale snapshot meant the last one to land
# BECAME the file, dropping every task -- and every session -- the loser had
# not seen. Measured before the fix, two processes and 25 creates each: one
# session's entire list absent from the file.
#
# An O_EXCL lock file is the one primitive that behaves the same on Windows
# and POSIX. The waits are short and the whole thing FAILS OPEN, because this
# runs inside a hook: an unserialised write is a bad day, a hook that blocks
# or raises is every session on the machine.
_LOCK_WAIT_S = 2.0      # total patience before writing unlocked anyway
_LOCK_SPIN_S = 0.01     # between attempts
_LOCK_STALE_S = 30.0    # older than this and the holder is dead, not slow


@contextlib.contextmanager
def _exclusive(project=None):
    """Hold the sidecar lock if it can be had, and never fail because it
    could not. Yields exactly once either way."""
    lock = sidecar_path(project).with_name(SIDECAR + ".lock")
    fd = None
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                # A lock older than any hook could legitimately hold belongs to
                # a process that died mid-write. Breaking it beats honouring it
                # forever, which would leave the sidecar permanently unwritable.
                if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(_LOCK_SPIN_S)
        except PermissionError:
            # CONTENTION, not an unwritable directory, and it landed in the arm
            # below for the whole life of this lock. On Windows a name whose
            # holder has closed its fd and called `unlink` is DELETE-PENDING
            # until the entry actually goes, and a create against a
            # delete-pending name answers ACCESS_DENIED rather than EEXIST. So
            # the one error that clears in microseconds took the arm that fails
            # open INSTANTLY, without waiting at all -- the loser walked into
            # the critical section beside the next holder and the last writer
            # published a snapshot read before the other's write.
            #
            # Caught in the act rather than reasoned about: under 12-way CPU
            # load, with `os.open` audited and both sections timestamped, every
            # lost task came with exactly one overlapping critical section and a
            # PermissionError on the .lock path in the process that lost it.
            # 1:1, and no loss without an overlap. The symptom was one create of
            # fifty vanishing, ~8-15% of runs, which `_LOCK_WAIT_S` could not
            # touch because this arm never waited.
            #
            # It waits now, and the deadline still bounds it: the fail-open
            # below survives for the failure that CANNOT clear -- an unwritable
            # directory, which is what its comment was always describing.
            if time.monotonic() >= deadline:
                break
            time.sleep(_LOCK_SPIN_S)
        except OSError:
            break       # unwritable directory: fail open, never raise
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lock.unlink()
            except OSError:
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
    """The task id the harness reported, or "" when the result has none.

    TWO SHAPES, and the one this shipped reading was the one a human sees. A
    transcript renders a create as `Task #1 created successfully: ...`; the field
    the HOOK is handed is the structured object,
    `{"success": true, "taskId": "1", "updatedFields": [...], ...}`. Measured on
    the 18 MB transcript that caught it: `"toolUseResult":"Task #` appears 0
    times. Matching only the prose meant every create landed under
    `_SUBJECT_KEY`, every later update looked up an id that was never a key, and
    the plan froze at 0/N -- the exact symptom this whole feature exists to
    remove.

    So the structured id wins, and the prose stays as the fallback: it is real in
    the rendered form, and neither shape is contractual. A disagreement between
    them is not a tie -- the structured field is the harness's own answer and the
    prose is a rendering of it.
    """
    if isinstance(response, dict):
        for key in ("taskId", "task_id", "id"):
            value = response.get(key)
            # `isinstance(True, int)` is True, and a boolean is not an id.
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                found = str(value).strip()
                if found:
                    return found
    import re

    from . import hookcapture
    # One reader for "text out of a tool_response", shared with the Skill path:
    # two readers that disagree about dicts and content blocks is a bug waiting
    # for a harness change, and this module has already been bitten by one.
    m = re.search(r"[Tt]ask #(\d+)", hookcapture._response_text(response))
    return m.group(1) if m else ""


def _text(value) -> str:
    return str(value or "").strip()[:MAX_TEXT]


def _create(tasks, inp, response) -> bool:
    """Apply one TaskCreate. True when the list changed."""
    from . import hookcapture
    if not hookcapture._response_ok(response):
        # An error envelope is not a creation receipt. `_response_text` reads
        # a dict's `error` key by design, so a failure whose message named
        # some OTHER task number handed that number to `_result_id`, and the
        # unconditional assignment below wrote this record straight over that
        # task -- destroying a completed step's text, reverting its status,
        # and writing the regression into the ledger through the roadmap
        # `set`. Nothing was created, so nothing is folded. `_response_ok`
        # lives in that module for exactly this call, and the sibling Skill
        # path already makes it.
        return False
    subject = _text(inp.get("subject"))
    if not subject:
        # The batch form: `{"tasks": [{subject: ...}, ...]}`, one observed
        # result of 239 and the only one with no id to read. Folding each entry
        # keeps the work visible; the alternative is dropping a whole plan.
        batch = inp.get("tasks")
        if not isinstance(batch, list):
            return False
        # A LIST, then `any()`. `any()` over a GENERATOR stops consuming at
        # the first truthy element and `_create` is truthy for every entry
        # with a subject, so the generator form folded entry 1 and abandoned
        # 2..N -- and a batch result carries no id, so no later TaskUpdate
        # could ever bring them back. The docstring above promised the
        # opposite of what the code did.
        folded = [_create(tasks, item, None) for item in batch
                  if isinstance(item, dict)]
        return any(folded)
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
        #
        # But if EVERY stored key is subject-based, no create ever read an id, so
        # no update can ever land and the plan freezes at 0/N -- which is how
        # this feature spent its whole life dead while the hook around it
        # appended 23,973 events without logging one error. That is a shape
        # drift, not a missing task, and it gets a line in hooks_errors.log. The
        # `all()` is what keeps it quiet in the ordinary case: one id-keyed entry
        # anywhere proves ids work, and then an unseen id is just an evicted or
        # pre-hook task.
        if tasks and all(str(k).startswith(_SUBJECT_KEY) for k in tasks):
            from . import hookcapture
            hookcapture.log_hook_error(
                "tasktool_id_drift",
                RuntimeError(f"update named task {key!r} but all {len(tasks)} "
                             "stored keys are subject-based: no create could "
                             "read an id from its result, so this plan can "
                             "never show progress"))
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
    # READ AND WRITE INSIDE ONE LOCK. Loading outside it is the defect: the
    # snapshot goes stale the moment another hook process saves, and this
    # call would then publish that stale whole-file state as the file.
    with _exclusive(project):
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
            # The newest work is what a live plan is about, so the lowest ids
            # go.
            for key in sorted(tasks, key=_order)[:len(tasks) - MAX_TASKS]:
                del tasks[key]
        state[sid] = {"tasks": tasks, "seen": ev_mod.now_iso(),
                      "n": _next_seq(state)}
        save(state, project)
        return todos(tasks)
