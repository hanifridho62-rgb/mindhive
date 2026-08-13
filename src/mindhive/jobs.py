"""The dashboard's long operations, run off the request thread.

Six of the panel's buttons started work that takes longer than a person will
watch a spinner: a full pytest run, an embedding pass over every event, a
`claude` call per incident, a harvest of every transcript directory. Each of them
ran INSIDE the HTTP handler, which had three consequences that all read as "the
dashboard is broken":

1. The browser held one fetch open for the whole run. Past the fetch timeout the
   page gave up on work that was still running and would still finish — so the
   panel reported failure for a success.
2. A worker thread was pinned for minutes. `ThreadingHTTPServer` has no bound on
   threads, but the GIL means those minutes were also spent NOT answering the
   two 2.5s pollers, which is what made every other click feel slow.
3. Nothing was written down. A run that ended when the process did left no trace
   at all, so the only honest thing anybody could say afterwards was "it was
   running when I last looked".

So a long operation now returns **202 Accepted with a job id** and the page polls
for its state. Polling, deliberately, and not SSE: the panel already has a
2.5s poll loop and its own discard-stale-paint idiom, so a second transport with
its own reconnect semantics would be a new failure mode to earn the same result.

## What is written to the ledger (T14)

Three deterministic types — `job_started`, `job_finished`, `job_abandoned` — so
a run that vanished with its process is distinguishable from one that finished.
Before them, an orphaned job read as still-running forever.

`job_abandoned` is written by the **boot seal** (`seal_orphans`), never by the
job itself: nothing can record its own death. On start-up the seal folds the
selected namespace's job events, finds every `job_started` with no matching
finish, and closes it.

**The single-writer assumption, written down:** the seal treats "started, never
finished, and not by this process" as abandoned. That is correct for the case it
exists for — the dashboard was restarted while a job was running — and it is
WRONG if two dashboard processes share one namespace, because the second one's
boot would abandon the first one's in-flight jobs. This code does not defend
against that; it only refuses to abandon jobs carrying its OWN pid, so a seal
can never kill a job it is currently running. One dashboard per namespace is the
assumption. `stable_url`/`_instance_running` already make a second instance on
the same port hard to start by accident, which is why this is an accepted
assumption rather than a defect — but it IS an assumption, and a store on a
shared drive breaks it.

Scoped to the selected namespace only, for the same reason: sealing every
namespace on the machine would extend the single-writer claim to stores this
process has no business speaking for.
"""

from __future__ import annotations

import collections
import os
import threading
import time
import uuid

from . import events, outcomes


# Per-job output cap. Selftest is the honest worst case — a full pytest run over
# this repo prints far more than a person will read — and the whole buffer used
# to sit in memory and be re-sent, in full, on every 2.5s poll. Capped and read
# by offset, a job costs a bounded amount of memory and each poll carries only
# what is new.
OUTPUT_CAPACITY_BYTES = 64 * 1024

# Finished jobs kept for the page to collect after the fact. A job the browser
# stopped polling (tab closed mid-run) must still be readable when it comes
# back, and without a bound that is a leak for the life of the process.
FINISHED_RETAINED = 12

MAX_KIND_CHARS = 48
MAX_SUMMARY_CHARS = outcomes.MAX_SUMMARY_CHARS

RUNNING = "running"
FINISHED = "finished"


class OutputRing:
    """A job's output, capped, and readable from an absolute offset.

    Offsets are counted over everything ever written, not over what is retained,
    so a client's offset stays meaningful after the front of the buffer has been
    dropped. `read` reports the drop rather than silently resuming further along:
    a gap the reader cannot see is a log it will misread.
    """

    def __init__(self, capacity=OUTPUT_CAPACITY_BYTES):
        self._capacity = int(capacity)
        self._chunks: "collections.deque[bytes]" = collections.deque()
        self._retained = 0        # bytes currently held
        self._dropped = 0         # bytes discarded off the front, ever
        self._lock = threading.Lock()

    @property
    def total_written(self) -> int:
        with self._lock:
            return self._dropped + self._retained

    def write(self, text) -> None:
        chunk = str(text).encode("utf-8", "replace")
        if not chunk:
            return
        with self._lock:
            self._chunks.append(chunk)
            self._retained += len(chunk)
            while self._retained > self._capacity and self._chunks:
                oldest = self._chunks.popleft()
                self._retained -= len(oldest)
                self._dropped += len(oldest)

    def read(self, offset=0):
        """Return ``(text, next_offset, dropped_before)`` for everything written
        at or after *offset*.

        `dropped_before` is how many bytes the reader asked for and can never
        get. It is reported instead of being smoothed over, and the page says so
        in the output pane.
        """
        try:
            want = max(0, int(offset))
        except (TypeError, ValueError):
            want = 0
        with self._lock:
            first = self._dropped
            held = b"".join(self._chunks)
            end = first + len(held)
            if want < first:
                dropped_before = first - want
                want = first
            else:
                dropped_before = 0
            if want >= end:
                return "", end, dropped_before
            return (held[want - first:].decode("utf-8", "replace"),
                    end, dropped_before)


class Job:
    """One run of one registered kind. Owns its own state; the registry owns the
    set of them."""

    def __init__(self, job_id, kind, label, project):
        self.id = job_id
        self.kind = kind
        self.label = label
        self.project = project
        self.state = RUNNING
        self.report = None            # outcomes.Report once finished
        self.started_at = time.time()
        self.finished_at = None
        self.output = OutputRing()

    @property
    def seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(end - self.started_at, 3)

    def snapshot(self, offset=0) -> dict:
        """The polling body. Small by construction: the output is the only part
        that grows, and it is the part read by offset.

        State and report are read together under the registry lock because
        `finish` sets them together. Reading them separately let a poll that
        landed between the two report a RUNNING job that already had a final
        outcome — a self-contradicting body, and the page decides when to stop
        polling from exactly that pair.
        """
        text, next_offset, dropped = self.output.read(offset)
        with _LOCK:
            state, report, seconds = self.state, self.report, self.seconds
        body = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": state,
            "seconds": seconds,
            "output": text,
            "offset": next_offset,
            "dropped": dropped,
        }
        if report is not None:
            body["outcome"] = report.outcome
            body["summary"] = report.summary
            body["worked"] = report.worked
            body["result"] = report.result
        return body


class Kind(collections.namedtuple("Kind", "name label run lock")):
    """A long operation that may be started as a job.

    `lock` is not redundant with "one job per kind at a time". A kind's work is
    often reachable through doors this module cannot see — `/api/plugins/scan`
    and `/api/plugins/autoscan` run the same machine-wide scan, and the CLI
    reaches most of this work directly — so the pre-existing per-operation lock
    is taken for the life of the job. Dropping it because the job registry
    already refuses a second click would have quietly removed the guarantee that
    two DIFFERENT doors cannot start the same work twice.
    """

    __slots__ = ()


_KINDS: "collections.OrderedDict[str, Kind]" = collections.OrderedDict()
_JOBS: "collections.OrderedDict[str, Job]" = collections.OrderedDict()
_RUNNING: dict = {}          # kind name -> job id
_LOCK = threading.Lock()     # guards _JOBS and _RUNNING


def register_kind(name, label, run, lock=None) -> Kind:
    """Make an operation startable as a job. Opt-in: an operation nobody
    registered still runs synchronously, which is the right default for the ones
    that finish in milliseconds."""
    key = str(name or "").strip()
    if not key:
        raise ValueError("a job kind needs a name")
    if len(key) > MAX_KIND_CHARS:
        raise ValueError(f"job kind exceeds {MAX_KIND_CHARS} chars")
    if not str(label or "").strip():
        raise ValueError(f"job kind {key!r} needs a label for the panel")
    if not callable(run):
        raise ValueError(f"job kind {key!r} needs a callable run")
    existing = _KINDS.get(key)
    if existing is not None and existing.run is not run:
        raise ValueError(f"job kind {key!r} is already registered by something else")
    kind = Kind(key, str(label).strip(), run, lock)
    _KINDS[key] = kind
    return kind


def kinds() -> tuple:
    return tuple(_KINDS.values())


def get_kind(name) -> "Kind | None":
    return _KINDS.get(str(name or "").strip())


def get(job_id) -> "Job | None":
    with _LOCK:
        return _JOBS.get(str(job_id or ""))


def running_id(kind_name) -> "str | None":
    with _LOCK:
        return _RUNNING.get(str(kind_name or "").strip())


def _remember(job) -> None:
    """Record a job, evicting the oldest FINISHED ones past the retention bound.
    Running jobs are never evicted — a job you cannot poll is a job you cannot
    learn the outcome of."""
    _JOBS[job.id] = job
    finished = [j for j in _JOBS.values() if j.state == FINISHED]
    for stale in finished[:max(0, len(finished) - FINISHED_RETAINED)]:
        _JOBS.pop(stale.id, None)


def start(kind_name, project, request=None) -> "Job | None":
    """Start *kind_name* for *project* and return its Job, or None when it is
    already running.

    None rather than an exception: "something is already running" is an expected
    domain answer the page renders, exactly as `dashboard._one_at_a_time` returns
    ``ok:False`` with a 200 rather than raising.
    """
    kind = get_kind(kind_name)
    if kind is None:
        raise KeyError(f"no job kind registered as {kind_name!r}")
    with _LOCK:
        if _RUNNING.get(kind.name):
            return None
        if kind.lock is not None and not kind.lock.acquire(blocking=False):
            return None
        job = Job(uuid.uuid4().hex[:16], kind.name, kind.label, project)
        _RUNNING[kind.name] = job.id
        _remember(job)
    _record(job, "job_started", {"pid": os.getpid()})
    try:
        threading.Thread(target=_run, args=(kind, job, request or {}),
                         name=f"job-{kind.name}", daemon=True).start()
    except Exception as e:                      # noqa: BLE001 — see below
        # Without this the kind stays marked running and its lock stays held for
        # the life of the process: the wedge `_one_at_a_time`'s `finally` exists
        # to prevent, reintroduced one layer up where no `finally` covers it.
        # The job is returned already-failed rather than raising, because it has a
        # start row in the ledger and something has to close it.
        _release(kind)
        finish(job, outcomes.Report(
            outcomes.FAILED, f"could not start a thread for the job: {e}"))
    return job


def _release(kind) -> None:
    """Give a kind's lock back, tolerating a lock that was never taken. One place
    rather than two, because a lock released on only one of the paths out is the
    exact defect this module's callers already learned the hard way."""
    if kind.lock is not None:
        try:
            kind.lock.release()
        except RuntimeError:
            pass


def _run(kind, job, request) -> None:
    """The job body, on its own thread. Never raises: the thread has nobody to
    raise to, and a job that died without recording anything is precisely the
    state the ledger events exist to make impossible."""
    try:
        report = kind.run(job.project, request, job.output.write)
        if not isinstance(report, outcomes.Report):
            report = outcomes.Report(
                outcomes.FAILED,
                f"{kind.name} returned {type(report).__name__}, not a Report")
        elif report.outcome not in outcomes.SELF_REPORTABLE:
            # Coerced HERE rather than left to be refused by `validate_stored`,
            # whose rejection `_record` swallows: that path would drop the finish
            # row silently and the next boot would seal the job as abandoned — a
            # run that reported an outcome and then appears never to have ended.
            report = outcomes.Report(
                outcomes.FAILED,
                f"{kind.name} reported {report.outcome!r}, which nothing may "
                f"claim about its own run: {report.summary}")
    except Exception as e:                      # noqa: BLE001 — see docstring
        report = outcomes.Report(
            outcomes.FAILED, f"{type(e).__name__}: {e}")
    finally:
        _release(kind)
    finish(job, report)


def finish(job, report) -> None:
    """Close a job out: ledger row, then state, then its kind freed for the next
    click. Separate from `_run` so the boot seal can close a job it did not run.

    **The row is written BEFORE the job reads as finished, and that order is the
    point.** Flipping the state first left a window where the job publicly said
    FINISHED while no finish row existed yet — and a process that died inside that
    window left a start with no close, which the boot seal is required to read as
    abandoned. A run reported as finished and later sealed as abandoned is the
    one contradiction these events exist to rule out. This way anything that
    observes FINISHED can rely on the row already being there.
    """
    job.finished_at = time.time()       # only this job's own thread writes it
    _record(job, "job_finished", {
        "outcome": report.outcome,
        "summary": report.summary,
        "seconds": job.seconds,
    })
    with _LOCK:
        job.report = report
        job.state = FINISHED
        if _RUNNING.get(job.kind) == job.id:
            _RUNNING.pop(job.kind, None)


def _record(job, event_type, extra) -> None:
    """Append one job row. Ledger failures are swallowed on purpose: the ledger
    is the audit trail of the work, not a precondition for it, and a full disk
    should not turn a running pytest into a crashed thread."""
    payload = {"job_id": job.id, "kind": job.kind}
    payload.update(extra)
    try:
        validate_stored(event_type, payload)
        events.append_event(event_type, payload, project=job.project,
                            tags=["job", f"kind:{job.kind}"])
    except Exception:                           # noqa: BLE001 — see docstring
        pass


# --- the payload contract ----------------------------------------------------
# Two validators, the same split `verify.py` and `mindnet/phases.py` use, and for
# the same reason. `validate_payload` guards the arbitrary-append surfaces (`hive
# append`, POST /api/append), where it can only see the payload — one shape for
# all three types, enforcing what the fold actually depends on: a job_id and a
# kind to pair rows by, and a recognised outcome if one is claimed.
# `validate_stored` is the writer's half and DOES know the type, so it can
# require what each row must carry — a `job_finished` without an outcome is the
# row that would make a finished job read as running forever.

def validate_payload(payload) -> None:
    """Enforce the shared `job_*` contract at an arbitrary-append surface."""
    if not isinstance(payload, dict):
        raise ValueError("job payload must be an object")
    for key in ("job_id", "kind"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"job payload requires a non-empty {key}")
    if len(payload["kind"]) > MAX_KIND_CHARS:
        raise ValueError(f"job kind exceeds {MAX_KIND_CHARS} chars")
    outcome = payload.get("outcome")
    if outcome is not None and outcome not in outcomes.OUTCOMES:
        raise ValueError("job outcome must be one of: "
                         + ", ".join(outcomes.OUTCOMES))
    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("job summary must be text when present")


def validate_stored(event_type, payload) -> None:
    """The writer's stricter, type-aware half."""
    validate_payload(payload)
    if event_type == "job_finished":
        if payload.get("outcome") not in outcomes.SELF_REPORTABLE:
            raise ValueError(
                "job_finished requires an outcome a run can report about "
                "itself — one of: " + ", ".join(outcomes.SELF_REPORTABLE))
        if not str(payload.get("summary") or "").strip():
            raise ValueError("job_finished requires a summary saying why")
    elif event_type == "job_abandoned":
        if not str(payload.get("reason") or "").strip():
            raise ValueError("job_abandoned requires a reason")
    elif event_type == "job_started":
        if not isinstance(payload.get("pid"), int):
            raise ValueError("job_started requires the pid that ran it")
    else:
        raise ValueError(f"{event_type} is not a job event")


# --- the boot seal -----------------------------------------------------------

def orphans(project, evs=None) -> list:
    """Job ids in *project* that started, never finished, and were not started by
    this process. Total on junk history: a row missing the fields it needs is
    skipped, never raised on — the ledger holds rows written by every past
    version of this code."""
    started, closed = {}, set()
    mine = os.getpid()
    for ev in (evs if evs is not None else events.read_all(project)):
        etype = getattr(ev, "type", None)
        if etype not in ("job_started", "job_finished", "job_abandoned"):
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        if etype == "job_started":
            if payload.get("pid") != mine:
                started[job_id] = payload.get("kind") or "unknown"
        else:
            closed.add(job_id)
    return [(job_id, kind) for job_id, kind in started.items()
            if job_id not in closed]


def seal_orphans(project) -> int:
    """Close out every orphaned job in *project* with a `job_abandoned` row and
    return how many were sealed. Called once at dashboard boot.

    Read the module docstring for the single-writer assumption this rests on.
    """
    sealed = 0
    for job_id, kind in orphans(project):
        payload = {"job_id": job_id, "kind": kind,
                   "reason": "no finish row — the process that ran it is gone"}
        try:
            validate_stored("job_abandoned", payload)
            events.append_event("job_abandoned", payload, project=project,
                                tags=["job", f"kind:{kind}"])
        except Exception:                       # noqa: BLE001 — as `_record`
            continue
        sealed += 1
    return sealed


# --- the outcomes-registry probe (T15) ---------------------------------------

def _probe(project=None) -> outcomes.Report:
    """Report on the job surface's own feedback loop: can a click still be told
    what happened to it?

    Read-only and cheap — it inspects the in-process registry, and never starts a
    job or reads the ledger. What it can actually catch is the state that made
    this module necessary: a kind registered with no way to report an outcome, or
    a job stuck RUNNING with its kind slot held so every later click is refused.
    """
    if not _KINDS:
        return outcomes.Report(
            outcomes.DEGRADED,
            "no long operation is registered as a job — every click runs on the "
            "request thread")
    with _LOCK:
        stuck = [job for job in _JOBS.values()
                 if job.state == RUNNING and _RUNNING.get(job.kind) == job.id]
    if stuck:
        return outcomes.Report(
            outcomes.DID_WORK,
            f"{len(_KINDS)} kinds registered; {len(stuck)} running "
            f"({', '.join(sorted(j.kind for j in stuck))})")
    return outcomes.Report(
        outcomes.NOTHING_TO_DO,
        f"{len(_KINDS)} kinds registered, none running")


outcomes.register("dashboard-jobs",
                  "a long click reports what it did, not just that it started",
                  _probe)
