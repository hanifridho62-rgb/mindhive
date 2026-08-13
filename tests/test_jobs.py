"""The dashboard's job runtime: 202-accepted work, capped output, and a ledger
that can tell a finished run from one that died with its process (T13/T14/T16).
"""
import os
import threading
import time
import types

import pytest

from mindhive import events, jobs, outcomes


@pytest.fixture(autouse=True)
def clean_jobs():
    """Restore every process-wide registry this module keeps. A leaked `_RUNNING`
    entry would make the NEXT test's first click get refused, which reads as a
    concurrency bug in whichever test happened to run second."""
    saved_kinds = dict(jobs._KINDS)
    saved_jobs = dict(jobs._JOBS)
    saved_running = dict(jobs._RUNNING)
    yield
    jobs._KINDS.clear(); jobs._KINDS.update(saved_kinds)
    jobs._JOBS.clear(); jobs._JOBS.update(saved_jobs)
    jobs._RUNNING.clear(); jobs._RUNNING.update(saved_running)


def _finished(job, timeout=5.0):
    """Wait for a job's own thread to close it out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state == jobs.FINISHED:
            return True
        time.sleep(0.01)
    return False


def _register(name, run, lock=None):
    return jobs.register_kind(name, f"Running {name}", run, lock)


# --- the output ring (T16) ---------------------------------------------------

def test_a_reader_gets_only_what_is_new_since_its_offset():
    """The defect: the whole buffer was re-sent on every 2.5s poll."""
    ring = jobs.OutputRing()
    ring.write("first\n")
    text, offset, dropped = ring.read(0)
    assert (text, dropped) == ("first\n", 0)
    ring.write("second\n")
    text, offset_2, dropped = ring.read(offset)
    assert (text, dropped) == ("second\n", 0)
    assert offset_2 > offset
    # Nothing new: an empty read, not a re-send.
    assert ring.read(offset_2)[0] == ""


def test_output_past_the_cap_is_dropped_from_the_front_and_reported():
    """Truncation the reader cannot see is a log it will misread."""
    ring = jobs.OutputRing(capacity=64)
    ring.write("a" * 50)
    ring.write("b" * 50)
    text, offset, dropped = ring.read(0)
    assert dropped > 0, "a reader asking from 0 must be told bytes are gone"
    assert "b" * 50 in text
    assert len(text) <= 64
    assert offset == 100, "offsets count everything ever written, not what is held"


def test_an_offset_stays_meaningful_after_the_front_is_dropped():
    """Absolute offsets, so a slow reader resumes at the right place instead of
    re-reading from byte zero."""
    ring = jobs.OutputRing(capacity=64)
    ring.write("a" * 40)
    _, offset, _ = ring.read(0)
    assert offset == 40
    ring.write("b" * 40)                    # pushes the first chunk out
    text, offset_2, dropped = ring.read(offset)
    assert text == "b" * 40
    assert dropped == 0, "the reader had already consumed the dropped bytes"
    assert offset_2 == 80


def test_a_nonsense_offset_reads_from_the_start_instead_of_raising():
    """The offset arrives from a query string; a bad one is a 200, not a 500."""
    ring = jobs.OutputRing()
    ring.write("hello")
    for junk in ("banana", None, -5, ""):
        assert ring.read(junk)[0] == "hello"
    # A number that merely is not an int is not junk — it is truncated, the same
    # as `int()` would, rather than being treated as "start from zero".
    assert ring.read(3.7)[0] == "lo"


# --- starting and refusing ---------------------------------------------------

def test_a_second_click_while_one_runs_is_refused():
    """The guarantee `_one_at_a_time` made for the synchronous endpoints, kept
    for the asynchronous ones."""
    gate = threading.Event()

    def blocking(project, request, emit):
        gate.wait(5)
        return outcomes.Report(outcomes.DID_WORK, "done")

    _register("blocker", blocking)
    first = jobs.start("blocker", "test")
    assert first is not None
    assert jobs.start("blocker", "test") is None, "second click must be refused"
    gate.set()
    assert _finished(first)
    # Once it is finished the kind is free again.
    second = jobs.start("blocker", "test")
    assert second is not None
    assert _finished(second)


def test_a_lock_held_through_another_door_refuses_the_click():
    """The kind's lock is not redundant with one-job-per-kind: the same work is
    reachable from the CLI and from a sibling endpoint, and dropping the lock
    would have quietly removed that guarantee."""
    lock = threading.Lock()
    _register("locked", lambda p, r, emit: outcomes.Report(
        outcomes.DID_WORK, "done"), lock)
    lock.acquire()                            # somebody else is already running
    try:
        assert jobs.start("locked", "test") is None
    finally:
        lock.release()
    assert jobs.start("locked", "test") is not None


def test_a_finished_job_releases_the_lock_for_the_next_click():
    lock = threading.Lock()
    _register("relock", lambda p, r, emit: outcomes.Report(
        outcomes.DID_WORK, "done"), lock)
    job = jobs.start("relock", "test")
    assert _finished(job)
    assert lock.acquire(blocking=False), "the job kept its lock after finishing"
    lock.release()


def test_a_run_that_raises_releases_the_lock_too():
    """A wedged endpoint for the life of the process is worse than a failed run."""
    lock = threading.Lock()

    def exploding(project, request, emit):
        raise RuntimeError("pytest is not installed")

    _register("boom", exploding, lock)
    job = jobs.start("boom", "test")
    assert _finished(job)
    assert job.report.outcome == outcomes.FAILED
    assert "pytest is not installed" in job.report.summary
    assert lock.acquire(blocking=False)
    lock.release()


def test_a_run_returning_the_wrong_type_is_a_failure_not_a_crash():
    _register("sloppy", lambda p, r, emit: {"ok": True})
    job = jobs.start("sloppy", "test")
    assert _finished(job)
    assert job.report.outcome == outcomes.FAILED
    assert "not a Report" in job.report.summary


def test_a_run_cannot_report_its_own_abandonment():
    """`abandoned` is in the vocabulary, but only the boot seal witnesses it.

    Coerced in `_run` rather than left to `validate_stored`, whose rejection
    `_record` swallows: that path drops the finish row silently, and then the next
    boot seals as abandoned a job that had already reported an outcome.
    """
    _register("liar", lambda p, r, emit: outcomes.Report(
        outcomes.ABANDONED, "I died mid-run"))
    job = jobs.start("liar", "test")
    assert _finished(job)
    assert job.report.outcome == outcomes.FAILED
    assert "abandoned" in job.report.summary
    finished = [ev for ev in _job_rows() if ev.type == "job_finished"]
    assert len(finished) == 1, "the finish row was dropped — a boot would seal it"
    assert jobs.orphans("test") == []


def test_a_job_whose_thread_will_not_start_does_not_wedge_its_kind(monkeypatch):
    """No `finally` covers the gap between taking the lock and the thread that is
    supposed to give it back — so the wedge `_one_at_a_time` was written to
    prevent could reappear one layer up."""
    lock = threading.Lock()
    _register("nothread", lambda p, r, emit: outcomes.Report(
        outcomes.DID_WORK, "done"), lock)

    class RefusesToStart:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading, "Thread", RefusesToStart)
    job = jobs.start("nothread", "test")
    assert job.state == jobs.FINISHED
    assert job.report.outcome == outcomes.FAILED
    assert "thread" in job.report.summary
    assert jobs.running_id("nothread") is None, "the kind stayed marked running"
    monkeypatch.undo()
    assert lock.acquire(blocking=False), "the lock was never given back"
    lock.release()
    second = jobs.start("nothread", "test")
    assert second is not None
    assert _finished(second)


def test_an_unregistered_kind_is_a_caller_bug():
    with pytest.raises(KeyError):
        jobs.start("never-registered", "test")


def test_a_kind_needs_a_label_and_a_callable():
    with pytest.raises(ValueError, match="needs a name"):
        jobs.register_kind("", "Label", lambda p, r, e: None)
    with pytest.raises(ValueError, match="needs a label"):
        jobs.register_kind("k", "", lambda p, r, e: None)
    with pytest.raises(ValueError, match="needs a callable"):
        jobs.register_kind("k", "Label", "nope")
    with pytest.raises(ValueError, match="exceeds"):
        jobs.register_kind("k" * 200, "Label", lambda p, r, e: None)


def test_two_modules_cannot_claim_the_same_kind():
    run = lambda p, r, emit: outcomes.Report(outcomes.DID_WORK, "a")
    _register("owned", run)
    with pytest.raises(ValueError, match="already registered"):
        _register("owned", lambda p, r, emit: outcomes.Report(
            outcomes.DID_WORK, "b"))
    _register("owned", run)              # same callable: fine


# --- what the page polls -----------------------------------------------------

def test_the_snapshot_carries_the_outcome_and_the_surface_own_result():
    _register("shaped", lambda p, r, emit: outcomes.Report(
        outcomes.NOTHING_TO_DO, "no transcripts to harvest", {"harvested": 0}))
    job = jobs.start("shaped", "test")
    assert _finished(job)
    snap = job.snapshot(0)
    assert snap["state"] == jobs.FINISHED
    assert snap["outcome"] == outcomes.NOTHING_TO_DO
    assert snap["summary"] == "no transcripts to harvest"
    assert snap["worked"] is True
    assert snap["result"] == {"harvested": 0}


def test_a_running_snapshot_has_no_outcome_to_report_yet():
    gate = threading.Event()
    _register("pending", lambda p, r, emit: (
        gate.wait(5), outcomes.Report(outcomes.DID_WORK, "done"))[1])
    job = jobs.start("pending", "test")
    snap = job.snapshot(0)
    assert snap["state"] == jobs.RUNNING
    assert "outcome" not in snap, "a running job must not claim an outcome"
    gate.set()
    assert _finished(job)


def test_the_request_body_reaches_the_run():
    seen = {}
    _register("echo", lambda p, r, emit: (
        seen.update(r), outcomes.Report(outcomes.DID_WORK, "ok"))[1])
    job = jobs.start("echo", "test", {"limit": 3})
    assert _finished(job)
    assert seen == {"limit": 3}


def test_a_running_job_is_never_evicted_by_retention():
    """A job you cannot poll is a job whose outcome you can never learn."""
    gate = threading.Event()
    _register("long", lambda p, r, emit: (
        gate.wait(5), outcomes.Report(outcomes.DID_WORK, "done"))[1])
    _register("quick", lambda p, r, emit: outcomes.Report(
        outcomes.DID_WORK, "done"))
    long_job = jobs.start("long", "test")
    for _ in range(jobs.FINISHED_RETAINED + 5):
        quick = jobs.start("quick", "test")
        assert _finished(quick)
    assert jobs.get(long_job.id) is long_job, "the running job was evicted"
    gate.set()
    assert _finished(long_job)


# --- the ledger rows (T14) ---------------------------------------------------

def _job_rows(project="test"):
    return [ev for ev in events.read_all(project)
            if str(getattr(ev, "type", "")).startswith("job_")]


def test_a_run_writes_a_start_row_and_a_finish_row():
    _register("recorded", lambda p, r, emit: outcomes.Report(
        outcomes.DID_WORK, "ingested 3"))
    job = jobs.start("recorded", "test")
    assert _finished(job)
    rows = {ev.type: ev.payload for ev in _job_rows()}
    assert set(rows) == {"job_started", "job_finished"}
    assert rows["job_started"]["job_id"] == job.id
    assert rows["job_started"]["pid"] == os.getpid()
    assert rows["job_finished"]["outcome"] == outcomes.DID_WORK
    assert rows["job_finished"]["summary"] == "ingested 3"


def test_the_finish_row_is_written_before_the_job_reads_as_finished(monkeypatch):
    """A job that says FINISHED while its close row is still unwritten can be
    sealed as abandoned by a boot that lands in that window — a run reported as
    finished and later abandoned is the one contradiction these rows rule out.

    Asserts the ordering at the moment of the append rather than after the fact,
    because after the fact both orders look identical.
    """
    real_append = events.append_event
    seen_state = {}

    def watching(event_type, payload, *a, **k):
        if event_type == "job_finished":
            seen_state["state"] = jobs.get(payload["job_id"]).state
        return real_append(event_type, payload, *a, **k)

    monkeypatch.setattr(events, "append_event", watching)
    _register("ordered", lambda p, r, emit: outcomes.Report(
        outcomes.DID_WORK, "done"))
    job = jobs.start("ordered", "test")
    assert _finished(job)
    assert seen_state["state"] == jobs.RUNNING, (
        "the finish row was appended after the job already read as finished")


def test_a_failed_run_is_recorded_as_failed_not_left_open():
    _register("recorded_fail", lambda p, r, emit: outcomes.Report(
        outcomes.FAILED, "exit code 1"))
    job = jobs.start("recorded_fail", "test")
    assert _finished(job)
    finished = [ev for ev in _job_rows() if ev.type == "job_finished"]
    assert len(finished) == 1
    assert finished[0].payload["outcome"] == outcomes.FAILED


def test_a_ledger_that_cannot_be_written_does_not_kill_the_job(monkeypatch):
    """The ledger is the audit trail of the work, not a precondition for it."""
    monkeypatch.setattr(events, "append_event",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    _register("unrecorded", lambda p, r, emit: outcomes.Report(
        outcomes.DID_WORK, "done anyway"))
    job = jobs.start("unrecorded", "test")
    assert _finished(job)
    assert job.report.outcome == outcomes.DID_WORK


# --- the boot seal (T14) -----------------------------------------------------

def _started_row(job_id, pid, kind="stale"):
    events.append_event("job_started",
                        {"job_id": job_id, "kind": kind, "pid": pid},
                        project="test", tags=["job"])


def test_a_start_with_no_finish_from_a_dead_process_is_an_orphan():
    _started_row("orphaned", pid=os.getpid() + 99999)
    assert [j for j, _ in jobs.orphans("test")] == ["orphaned"]


def test_this_process_own_running_job_is_never_an_orphan():
    """A seal that abandoned its own in-flight jobs would kill the work it is
    booting to serve."""
    gate = threading.Event()
    _register("mine", lambda p, r, emit: (
        gate.wait(5), outcomes.Report(outcomes.DID_WORK, "done"))[1])
    job = jobs.start("mine", "test")
    assert jobs.orphans("test") == []
    assert jobs.seal_orphans("test") == 0
    gate.set()
    assert _finished(job)


def test_a_finished_job_is_not_an_orphan():
    _started_row("closed", pid=os.getpid() + 99999)
    events.append_event("job_finished",
                        {"job_id": "closed", "kind": "stale",
                         "outcome": outcomes.DID_WORK, "summary": "done"},
                        project="test", tags=["job"])
    assert jobs.orphans("test") == []


def test_the_seal_writes_an_abandoned_row_and_is_idempotent():
    """Booting twice must not write the same abandonment twice — the second boot
    would otherwise keep re-sealing every job in the ledger's history."""
    _started_row("orphaned", pid=os.getpid() + 99999)
    assert jobs.seal_orphans("test") == 1
    abandoned = [ev for ev in _job_rows() if ev.type == "job_abandoned"]
    assert len(abandoned) == 1
    assert abandoned[0].payload["job_id"] == "orphaned"
    assert abandoned[0].payload["reason"]
    assert jobs.seal_orphans("test") == 0


def test_the_seal_reads_junk_history_without_raising():
    """Reads stay total: the ledger holds rows written by every past version."""
    junk = [
        types.SimpleNamespace(type="job_started", payload=None),
        types.SimpleNamespace(type="job_started", payload={}),
        types.SimpleNamespace(type="job_started", payload={"job_id": ""}),
        types.SimpleNamespace(type="job_started", payload={"job_id": 7}),
        types.SimpleNamespace(type="job_finished", payload="not a dict"),
        types.SimpleNamespace(type="unrelated", payload={"job_id": "x"}),
        types.SimpleNamespace(type="job_started",
                              payload={"job_id": "real", "kind": "k", "pid": -1}),
    ]
    assert [j for j, _ in jobs.orphans("test", evs=junk)] == ["real"]


def test_the_seal_stays_inside_the_selected_namespace():
    """Sealing every namespace would extend the single-writer claim to stores
    this process has no business speaking for."""
    _started_row("orphaned", pid=os.getpid() + 99999)
    events.append_event("job_started",
                        {"job_id": "elsewhere", "kind": "stale",
                         "pid": os.getpid() + 99999},
                        project="other", tags=["job"])
    assert jobs.seal_orphans("test") == 1
    assert [j for j, _ in jobs.orphans("other")] == ["elsewhere"]


# --- the payload contract (T14) ----------------------------------------------

def test_the_append_surface_contract_requires_what_the_fold_pairs_on():
    for bad, match in (
        ("not a dict", "must be an object"),
        ({"kind": "k"}, "non-empty job_id"),
        ({"job_id": "j"}, "non-empty kind"),
        ({"job_id": "j", "kind": "k" * 200}, "exceeds"),
        ({"job_id": "j", "kind": "k", "outcome": "fine"}, "must be one of"),
        ({"job_id": "j", "kind": "k", "summary": 7}, "must be text"),
    ):
        with pytest.raises(ValueError, match=match):
            jobs.validate_payload(bad)
    jobs.validate_payload({"job_id": "j", "kind": "k"})


def test_a_finished_row_cannot_omit_the_outcome_that_closes_it():
    """This is the row whose absence made a finished job read as running."""
    with pytest.raises(ValueError, match="requires an outcome"):
        jobs.validate_stored("job_finished", {"job_id": "j", "kind": "k"})
    with pytest.raises(ValueError, match="requires a summary"):
        jobs.validate_stored("job_finished", {
            "job_id": "j", "kind": "k", "outcome": outcomes.DID_WORK})


def test_a_run_cannot_record_itself_as_abandoned():
    """Only the boot seal may say this, so the writer refuses the outcome even
    though the shared vocabulary contains it."""
    with pytest.raises(ValueError, match="requires an outcome"):
        jobs.validate_stored("job_finished", {
            "job_id": "j", "kind": "k", "outcome": outcomes.ABANDONED,
            "summary": "died"})


def test_the_writer_refuses_rows_that_are_not_job_events():
    with pytest.raises(ValueError, match="not a job event"):
        jobs.validate_stored("code_change", {"job_id": "j", "kind": "k"})
    with pytest.raises(ValueError, match="requires the pid"):
        jobs.validate_stored("job_started", {"job_id": "j", "kind": "k"})
    with pytest.raises(ValueError, match="requires a reason"):
        jobs.validate_stored("job_abandoned", {"job_id": "j", "kind": "k"})


def test_the_three_types_are_registered_where_the_append_surfaces_look():
    """Registration is what makes `hive append` and POST /api/append enforce the
    contract; the type list alone would accept any shape."""
    for etype in ("job_started", "job_finished", "job_abandoned"):
        assert etype in events.DETERMINISTIC_TYPES
        assert etype in events._PAYLOAD_CONTRACTS
        with pytest.raises(ValueError):
            events.validate_typed_payload(etype, {"job_id": "j"})


def test_job_rows_stay_out_of_the_semantic_layer():
    """A LEDGER markdown file, a Memory-view row and a recall-corpus entry per
    button press is how a click would drown the operator's actual memory."""
    for etype in ("job_started", "job_finished", "job_abandoned"):
        assert etype not in events.SEMANTIC_TYPES
        assert etype not in events.MEMORY_VIEW_TYPES
