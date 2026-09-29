# SPDX-License-Identifier: Apache-2.0
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

**The public task lock (blueprint P3, MEB CONC-02 / §4.2, decision K4).**
`task_lock(project_dir, timeout_s=15.0)` is the opposite contract to
`_exclusive`: fail-CLOSED, and a `LockTimeoutError` -- this module's subclass
of `core.locks.LockTimeoutError` -- when the lock cannot be had. It is for a
caller that would rather hear "no" than run unlocked, a sub-agent manipulating
the board from outside a hook, and it is the MEB's code verbatim, constants
included; the comment above it names the two deliberate deviations and the
properties a caller has to know: its patience under contention is twenty
attempts (0.3-4 s of jittered sleep), the 15 s deadline mattering only when
attempts stall, and it serialises `task_lock` callers against each other, not
against the hook path -- a board write under it goes through `record()`, which
takes `_exclusive` for itself. `_exclusive` and its budget do not move.

The sidecar is per NAMESPACE while task ids are per session, so one session
working across two projects accumulates part of its list in each. That is the
same split the ledger already makes -- events land in the namespace detected at
the call -- and keeping the two consistent matters more than keeping one list
whole, since the plan those steps land on is per-namespace too.
"""
import contextlib
import json
import os
import random
import time
from contextlib import contextmanager
from pathlib import Path

from . import paths
from .core.locks import LockTimeoutError as _CoreLockTimeoutError

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
# ACCESS_DENIED gets its OWN, much smaller budget, because it is the one error
# code that arrives for both a transient and a permanent cause: a delete-pending
# name (clears in microseconds) and a directory that cannot be written (never
# clears). Nothing in the error distinguishes them -- `errno` is 13 either way,
# and stat'ing the path to see whether an entry exists fails with ACCESS_DENIED
# for a delete-pending file too, so there is no cheap property to branch on.
#
# A COUNT rather than a second deadline, and a small one: waiting 50 ms covers
# the transient case with four orders of magnitude to spare, while anything
# slower than that is indistinguishable from permanent -- and for permanent,
# failing open is this module's documented contract. Uncapped it cost a measured
# 2.004 s over 190 attempts on a fault that could never clear, inside a hook that
# runs on every task-tool call.
_LOCK_DENIED_SPINS = 5


@contextlib.contextmanager
def _exclusive(project=None):
    """Hold the sidecar lock if it can be had, and never fail because it
    could not. Yields exactly once either way."""
    lock = sidecar_path(project).with_name(SIDECAR + ".lock")
    fd = None
    deadline = time.monotonic() + _LOCK_WAIT_S
    denied = 0
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
            # It waits now, but only `_LOCK_DENIED_SPINS` times. The first
            # version of this arm waited out the whole `_LOCK_WAIT_S`, and that
            # was wrong for the reason the comment below states and this clause
            # then contradicted: an unwritable directory raises EACCES too, so
            # `except OSError` never saw it again and the permanent fault it
            # exists for started costing 2.004 s over 190 attempts -- inside a
            # hook, which this module's header rates worse than an unserialised
            # write. Capped, the transient case still gets 50 ms against a window
            # measured in microseconds, and the permanent one fails open in 50 ms
            # instead of two seconds.
            denied += 1
            if denied > _LOCK_DENIED_SPINS or time.monotonic() >= deadline:
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
    the HOOK is handed is a structured object. Measured on the 18 MB transcript
    that caught it: `"toolUseResult":"Task #` appears 0 times. Matching only the
    prose meant every create landed under `_SUBJECT_KEY`, every later update
    looked up an id that was never a key, and the plan froze at 0/N -- the exact
    symptom this whole feature exists to remove.

    So the structured id wins, and the prose stays as the fallback: it is real in
    the rendered form, and neither shape is contractual. A disagreement between
    them is not a tie -- the structured field is the harness's own answer and the
    prose is a rendering of it.

    AND THE STRUCTURED ID IS NESTED. The first fix read the right field off the
    wrong object, and left the symptom standing for a second round: `TaskUpdate`
    answers `{"success": true, "taskId": "1", ...}`, and reading a top-level
    `taskId` was generalised from it -- but the call that has to SUPPLY the key
    is the create, and a create answers `{"task": {"id": "1", "subject": ...}}`.
    Re-measured 2026-08-24 over all 541 transcripts on this machine, every
    paired TaskCreate result: 241 of 241 nested, 0 with a top-level id in any
    spelling. So the plan still froze at 0/N, now with a green test above it
    whose fixture asserted a shape nobody sends.

    The nested holder is therefore tried FIRST -- it is the only create shape
    ever observed -- and the flat one stays, because it is exactly what an
    update answers and dropping it would trade one blind spot for another.
    """
    holders = []
    if isinstance(response, dict):
        nested = response.get("task")
        if isinstance(nested, dict):
            holders.append(nested)
        holders.append(response)
    for holder in holders:
        for key in ("taskId", "task_id", "id"):
            value = holder.get(key)
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
    found = _result_id(response)
    if found:
        # LEARNING THE ID SUPERSEDES THE PLACEHOLDER. The subject key exists
        # only because no id could be read; once one can, keeping both rows
        # puts the same step on the plan twice -- once tickable, once frozen
        # at pending forever, because nothing can ever address the ghost.
        # Measured on this session's real traffic replayed over the sidecar
        # the old reader left: `6/12`, every step doubled. Undercounting is a
        # gap; double-counting is a wrong answer that reads as a right one.
        # Nothing is lost with the ghost: an update could never have touched
        # it, so its status can only be whatever its own create declared.
        tasks.pop(_SUBJECT_KEY + subject, None)
    tasks[found or (_SUBJECT_KEY + subject)] = rec
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


# --- CONC-02: the public task lock (blueprint P3, MEB §4.2, decision K4) ------
#
# `_exclusive` above is the HOOK path's lock: fail-open, two seconds of patience,
# never raises, because a hook that blocks is every session on the machine. This
# one is the opposite contract, for a caller that would rather hear "no" than run
# unlocked -- a sub-agent manipulating the board from outside a hook. `task_lock`
# sleeps a uniformly jittered `_LOCK_RETRY_BASE_MS`..`_LOCK_RETRY_MAX_MS` between
# attempts (SECONDS, whatever the MEB's suffix says: 15-200 ms), reclaims a
# holder older than `_STALE_LOCK_AGE_SECS`, counts an ACCESS_DENIED answer
# (Windows DELETE-PENDING) like EEXIST, and raises `LockTimeoutError` once either
# the `_TASK_LOCK_DENIED_SPINS` attempts or the `_LOCK_TIMEOUT_SECS` deadline are
# spent. Twenty sleeps of at most 200 ms are 4 s, so under contention it is the
# attempt cap that gives up (0.3-4 s of patience, about 2 s on average); the
# deadline fires only when single attempts stall, and the message quotes the
# deadline rather than the elapsed time because that is the MEB's string. The
# five constants and the loop are the MEB's code verbatim (§10.1 names
# `tasktool.py` as their owner) but for two guards, each marked where it stands.
# Two properties of the verbatim loop stay as written and are recorded here
# rather than fixed: the stale reclaim is unlink-then-open, so on POSIX a live
# holder older than 60 s can be reclaimed and two reclaimers can race (Windows
# refuses both -- an open handle blocks the delete), and an unlink the `finally`
# cannot complete leaves a fresh-mtime file nobody can take for 60 s; a critical
# section under this lock therefore stays far under a minute. The lock file is
# `project_dir / ".tasktool.lock"`, beside the board's own `task_tools.json.lock`
# -- a DIFFERENT file, so this lock excludes other `task_lock` callers and not
# the hook path's `record()`, and a board write made under it goes through
# `record()`, which takes `_exclusive` for itself. The caller hands
# `paths.project_dir(project)` in -- never `store_home()` -- with the directory
# already there: the primitive creates the FILE, not the folder, so a missing
# directory is ENOENT from `os.open`, raised as-is.
_LOCK_TIMEOUT_SECS = 15.0
_LOCK_RETRY_BASE_MS = 0.015
_LOCK_RETRY_MAX_MS = 0.200
_STALE_LOCK_AGE_SECS = 60.0
_TASK_LOCK_DENIED_SPINS = 20


class LockTimeoutError(_CoreLockTimeoutError):
    """`task_lock` gave up: the deadline passed or the spin budget ran out.

    A subclass of `core.locks.LockTimeoutError` (itself a `TimeoutError`), so
    one `except` clause covers a lock from either module."""


@contextmanager
def task_lock(project_dir: Path, timeout_s: float = _LOCK_TIMEOUT_SECS):
    lock_file = project_dir / ".tasktool.lock"
    start_time = time.monotonic()
    spins = 0
    while True:
        try:
            # Periksa stale lock (> 60 detik)
            if lock_file.exists():
                try:
                    mtime = lock_file.stat().st_mtime
                except FileNotFoundError:
                    # THE ONE DEVIATION from the MEB text. A holder can let go
                    # between `exists()` and `stat()`, and the stat then answers
                    # ENOENT -- which is not in the except tuple below, so the
                    # verbatim loop crashed its caller on the lock coming FREE.
                    # Caught in the act by `tests/test_tasktool_lock.py`. It is
                    # read as a fresh holder: the O_EXCL open just below is the
                    # real answer, and it takes the lock or counts a spin.
                    mtime = time.time()
                if (time.time() - mtime) > _STALE_LOCK_AGE_SECS:
                    try:
                        lock_file.unlink(missing_ok=True)
                    except OSError:
                        pass

            # Upaya perolehan kunci atomik
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            try:
                os.write(fd, f"{os.getpid()}:{time.time()}".encode("utf-8"))
            except BaseException:
                # THE SECOND DEVIATION. The open above created the file, and an
                # error from the write -- ENOSPC, EIO, a Ctrl+C on this CLI
                # path -- is not in the except tuple below and never reaches
                # the `finally` further down: the verbatim loop left the fd
                # open and `.tasktool.lock` behind with a fresh mtime, which on
                # Windows no contender can unlink for as long as this process
                # lives (an open handle blocks the delete). Undo the open, then
                # let the error out unchanged.
                try:
                    os.close(fd)
                    lock_file.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            break
        except (FileExistsError, PermissionError):
            spins += 1
            if spins > _TASK_LOCK_DENIED_SPINS or (time.monotonic() - start_time) > timeout_s:
                raise LockTimeoutError(f"Gagal memperoleh task_lock setelah {timeout_s}s (spins={spins}).")
            jitter = random.uniform(_LOCK_RETRY_BASE_MS, _LOCK_RETRY_MAX_MS)
            time.sleep(jitter)

    try:
        yield
    finally:
        try:
            os.close(fd)
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass
