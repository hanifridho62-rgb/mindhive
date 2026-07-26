# tests/test_mindnet_sidecar.py
"""MN-1 U5 — the phase sidecar: freshness, atomicity, and the rebuild refusal.

Written SPEC-FIRST against ``docs/plans/2026-07-25-001-feat-mn1-phase-spine-
plan.md`` KTD9 and the committed record shapes in `controller.py`. No line of
`sidecar.py` existed when these were written.

Four properties carry this file, and each one is a defect the sidecar would
otherwise ship with:

1. **The stamp must be COMPUTABLE without the ledger** (KTD9). `events.read_all`
   measures ~264 ms on the real store; U9's injector runs on every user prompt,
   so a freshness test that reads the ledger costs exactly what the sidecar
   exists to save. ``events_seen`` and ``last_ts`` alone cannot answer "am I
   stale?" — only the per-segment ``[size, mtime_ns]`` map can, from `os.stat`.
   `test_the_read_path_never_touches_the_ledger` is the durable form of that
   claim; the wall-clock test below is only a secondary signal.

2. **The write must be atomic AND per-writer** — `paths.write_text_atomic`
   derives its tmp name from the target alone (`paths.py:28`), so two writers
   share one tmp and `os.replace` publishes a hybrid. `promptgate._write_manifest`
   (`promptgate.py:319`) pid-scopes for exactly this reason.

3. **Reads degrade, never raise.** The sidecar is read on the hook path; a raise
   there breaks every prompt.

4. **`rebuild` must refuse to blank real state from an empty ledger.**
   `hygiene.prune` deletes the whole namespace including ``events/``, and
   `projector.project` re-creates it and writes from zero events with no guard
   (H6). Without the refusal, a post-prune rebuild silently erases the phase
   spine.

Round-trip equality is asserted on the WHOLE `PhaseState`, not field by field:
every controller record is frozen with dataclass ``__eq__``, so a tuple that
came back from JSON as a list fails the comparison loudly instead of drifting
into a later consumer.
"""
import json
import os
import shutil
import threading
import time

import pytest

from mindhive import events
from mindhive import paths
from mindhive.mindnet import controller as c_mod
from mindhive.mindnet import phases as ph_mod
from mindhive.mindnet import sidecar as s_mod

TASK = "mn1-u5"


# --- ledger helpers (mirroring tests/test_mindnet_controller.py) -----------------

def _phase_ev(payload, ts=None, session="s1"):
    return events.append_event("mindnet_phase", payload, session_id=session,
                               ts=ts, tags=["mindnet", "phase"])


def _declare(task_id=TASK, session="s1", **kw):
    """Declare on BOTH axes — the graph declare registers the task, the phase
    declare materializes the `PhaseRecord`. A task holding only the first
    orphans every phase event aimed at it."""
    events.append_event(
        "mindnet_task",
        {"action": "declare", "task_id": task_id,
         "title": f"task {task_id}", **kw},
        session_id=session, tags=["mindnet", "task"])
    return _phase_ev({"action": "declare", "task_id": task_id,
                      "verified_by": "python"}, session=session)


def _satisfy(task_id=TASK, phase="brainstorm", epoch=0, result="ok",
             session="s1"):
    desc = ph_mod.PHASE_EVIDENCE[phase]
    kind = desc["kind"]
    ref = (desc["required_dir"][0] + f"{phase}-evidence.md" if kind == "file"
           else f"runs/mn-{task_id}-1/out.txt")
    return _phase_ev({"action": "satisfy", "task_id": task_id, "phase": phase,
                      "epoch": epoch, "verified_by": desc["verified_by"],
                      "evidence": {"kind": kind, "ref": ref, "result": result}},
                     session=session)


def _enter(task_id=TASK, phase="plan", session="s1"):
    return _phase_ev({"action": "enter", "task_id": task_id, "phase": phase,
                      "verified_by": "python"}, session=session)


def _populate():
    """A ledger exercising every shape the sidecar has to carry: a record with
    satisfactions (one of them a `fail`, so ``failed_refs`` — a tuple nested
    inside a tuple of dataclasses — is non-empty), a recorded violation, and an
    orphan.

    Every one of those is asserted non-empty by `_fold_or_fail` before any
    round-trip claim is made, so a fixture that stops producing them turns this
    file's central test vacuous LOUDLY rather than silently."""
    _declare(TASK)
    _satisfy(TASK, "brainstorm", 0, result="ok")
    _enter(TASK, "plan")
    _satisfy(TASK, "plan", 0, result="fail")     # -> Satisfaction.failed_refs
    _satisfy(TASK, "prd", 0)                     # out of phase -> a violation
    # An `enter` for a task never declared on either axis: KTD14's orphan.
    _phase_ev({"action": "enter", "task_id": "typo-task", "phase": "plan",
               "verified_by": "python"})


def _fold_or_fail():
    """The in-memory fold, with the fixture's own coverage pinned."""
    state = c_mod.fold(events.read_all())
    rec = state.records.get(TASK)
    assert rec is not None, "fixture did not produce a phase record"
    assert rec.satisfactions, "fixture produced no satisfactions"
    assert any(s.failed_refs for s in rec.satisfactions), \
        "fixture produced no failed_refs — the nested-tuple case is untested"
    assert rec.violations, "fixture produced no violations"
    assert state.orphans, "fixture produced no orphans"
    return state


def _segment(name="s1.jsonl"):
    return paths.events_dir() / name


# --- the round trip ---------------------------------------------------------------

def test_the_round_trip_reproduces_the_in_memory_fold():
    _populate()
    state = _fold_or_fail()

    assert s_mod.rebuild().written
    got = s_mod.read()

    assert got.fresh, got.reason
    assert got.state == state
    assert got.state.records == state.records
    assert got.state.orphans == state.orphans


def test_the_round_trip_restores_tuples_not_lists():
    """JSON has one sequence type. A `satisfactions` that comes back as a list
    compares unequal to the fold and — worse — a `failed_refs` list would be
    mutable inside a frozen record. Asserted on the types directly, because a
    consumer that only ever iterates would not notice for months."""
    _populate()
    _fold_or_fail()
    s_mod.rebuild()

    rec = s_mod.read().state.records[TASK]
    assert isinstance(rec.satisfactions, tuple)
    assert isinstance(rec.violations, tuple)
    for sat in rec.satisfactions:
        assert isinstance(sat, c_mod.Satisfaction)
        assert isinstance(sat.failed_refs, tuple)
    for rows in s_mod.read().state.orphans.values():
        assert isinstance(rows, tuple)
        assert all(isinstance(o, c_mod.Orphan) for o in rows)


# --- degrade, never raise ----------------------------------------------------------

def test_a_missing_sidecar_reads_empty_without_raising():
    got = s_mod.read()
    assert got.state == s_mod.empty_state()
    assert not got.fresh
    assert got.reason


@pytest.mark.parametrize("body,label", [
    ("", "zero-byte"),
    ("   \n", "whitespace only"),
    ("{not json at all", "corrupt json"),
    ('{"version": 1, "records": {"a": {"task_id": "a", "phase": "pl',
     "truncated mid-object"),
    ("[]", "valid json, wrong shape"),
    ('{"version": 1, "records": "not-a-mapping"}', "records not a mapping"),
])
def test_an_unreadable_sidecar_reads_empty_without_raising(body, label):
    """The hook path calls this on every prompt. A raise here breaks the
    prompt, so every one of these degrades to an empty, not-fresh read."""
    p = s_mod.sidecar_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")

    got = s_mod.read()
    assert got.state == s_mod.empty_state(), label
    assert not got.fresh, label
    assert got.reason, label


def test_a_record_the_decoder_cannot_rebuild_is_skipped_not_raised():
    """Totality at the row level, mirroring `events._row_to_event`: one bad row
    must not cost the whole projection."""
    p = s_mod.sidecar_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": 1,
        "stamp": {"events_seen": 0, "last_ts": "", "segments": {}},
        "records": {
            "good": {"task_id": "good", "phase": "plan", "epoch": 0,
                     "satisfactions": [], "violations": []},
            "bad": {"phase": "plan"},              # no task_id: unbuildable
            "worse": "not even an object",
        },
        "orphans": {},
    }), encoding="utf-8")

    got = s_mod.read()
    assert set(got.state.records) == {"good"}


# --- the freshness stamp (KTD9) ----------------------------------------------------

def test_the_stamp_carries_the_three_fields_KTD9():
    _populate()
    assert s_mod.rebuild().written

    stamp = s_mod.read().stamp
    assert set(stamp) == {"events_seen", "last_ts", "segments"}
    assert stamp["events_seen"] == len(events.read_all())
    assert stamp["last_ts"] == max(e.ts for e in events.read_all())
    # The per-segment map is the load-bearing half: it is the ONLY part of the
    # stamp answerable from `os.stat` alone.
    assert stamp["segments"] == {"s1.jsonl": list(
        s_mod.segment_stats()["s1.jsonl"])}


def test_a_matching_stamp_reads_fresh():
    _populate()
    assert s_mod.rebuild().written

    got = s_mod.read()
    assert got.fresh
    assert got.reason == ""


def test_a_diverging_segment_size_reads_stale():
    """Size only. The mtime is restored to what it was, so this test cannot
    pass for the wrong reason (an mtime that moved)."""
    _populate()
    assert s_mod.rebuild().written
    seg = _segment()
    before = seg.stat()

    with open(seg, "a", encoding="utf-8") as f:
        f.write('{"ts":"2026-07-26T00:00:00Z","session_id":"s1",'
                '"type":"user_prompt","payload":{}}\n')
    os.utime(seg, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = seg.stat()
    assert after.st_size != before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns, "mtime moved; test invalid"

    got = s_mod.read()
    assert not got.fresh
    assert "s1.jsonl" in got.reason
    # Stale is reported, not blanked: `rebuild`'s empty-ledger refusal and
    # `hive mindnet show` both need to see what the sidecar is holding.
    assert got.state.records


def test_a_diverging_mtime_reads_stale():
    """mtime only — the size is untouched. This is the case a size-only check
    would miss: an in-place edit of a segment, or a segment restored from a
    backup, leaves the byte count identical."""
    _populate()
    assert s_mod.rebuild().written
    seg = _segment()
    before = seg.stat()

    os.utime(seg, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
    after = seg.stat()
    assert after.st_size == before.st_size, "size moved; test invalid"
    assert after.st_mtime_ns != before.st_mtime_ns

    got = s_mod.read()
    assert not got.fresh
    assert "s1.jsonl" in got.reason


def test_a_new_segment_reads_stale():
    """A second session writes its own segment file. Neither an unchanged
    segment set nor `events_seen` (unreadable without the ledger) would catch
    it — the segment MAP does."""
    _populate()
    assert s_mod.rebuild().written
    assert s_mod.read().fresh

    _declare("second-task", session="s2")

    got = s_mod.read()
    assert not got.fresh
    assert "s2.jsonl" in got.reason


def test_a_vanished_segment_reads_stale():
    """The post-prune shape: `hygiene.prune` removed ``events/`` entirely."""
    _populate()
    assert s_mod.rebuild().written
    shutil.rmtree(paths.events_dir())

    got = s_mod.read()
    assert not got.fresh
    assert "s1.jsonl" in got.reason


def test_a_sidecar_with_no_segment_stamp_reads_stale():
    """A stamp with no ``segments`` map cannot prove anything. It must not be
    conflated with an EMPTY map, which legitimately means "the ledger has no
    segments" and does compare equal to an empty events dir."""
    p = s_mod.sidecar_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": 1,
        "stamp": {"events_seen": 3, "last_ts": "2026-07-26T00:00:00Z"},
        "records": {}, "orphans": {},
    }), encoding="utf-8")

    assert not s_mod.read().fresh


def test_an_empty_ledger_and_an_empty_stamp_read_fresh():
    """The complement of the test above: no segments on disk and no segments in
    the stamp is a genuinely current empty projection, not a stale one. Without
    this, a brand-new namespace would be permanently 'stale' and U9 would never
    inject a phase block on a fresh project."""
    assert s_mod.rebuild().written
    got = s_mod.read()
    assert got.fresh, got.reason
    assert got.state == s_mod.empty_state()


# --- rebuild -----------------------------------------------------------------------

def test_rebuild_is_idempotent_byte_for_byte():
    _populate()
    assert s_mod.rebuild().written
    first = s_mod.sidecar_path().read_bytes()

    assert s_mod.rebuild().written
    assert s_mod.sidecar_path().read_bytes() == first


def test_rebuild_reports_what_it_wrote():
    _populate()
    out = s_mod.rebuild()
    assert out.written
    assert out.reason == ""
    assert out.events_seen == len(events.read_all())
    assert out.records == 1


def test_rebuild_refuses_to_blank_a_populated_sidecar_from_an_empty_ledger():
    """H6, the post-prune footgun. `hygiene.prune` deletes the namespace
    including ``events/``; `projector.project` re-creates the directory and
    writes from zero events with no guard at all. A rebuild that followed suit
    would erase the phase spine and report success."""
    _populate()
    assert s_mod.rebuild().written
    before = s_mod.sidecar_path().read_bytes()

    shutil.rmtree(paths.events_dir())
    out = s_mod.rebuild()

    assert not out.written
    assert out.reason
    assert s_mod.sidecar_path().read_bytes() == before, \
        "the refusal did not protect the file it refused to overwrite"


def test_rebuild_refuses_for_an_empty_events_dir_too():
    """Same defect, the other spelling: `projector.project` re-creates
    ``events/`` before anything writes into it, so "the directory is gone" and
    "the directory is empty" must refuse identically."""
    _populate()
    assert s_mod.rebuild().written
    before = s_mod.sidecar_path().read_bytes()

    for seg in paths.events_dir().glob("*.jsonl"):
        seg.unlink()
    assert paths.events_dir().exists()

    assert not s_mod.rebuild().written
    assert s_mod.sidecar_path().read_bytes() == before


def test_rebuild_from_an_empty_ledger_over_an_empty_sidecar_is_allowed():
    """The refusal defends STATE, not the file. With nothing to lose there is
    nothing to refuse — otherwise a fresh namespace could never mint its first
    sidecar."""
    assert s_mod.rebuild().written
    assert s_mod.rebuild().written          # again, over the empty one it wrote
    assert s_mod.read().state == s_mod.empty_state()


def test_rebuild_accepts_injected_events_without_reading_the_ledger(monkeypatch):
    """U7's CLI and U9 both already hold a fold; re-reading 20k events to write
    the sidecar would pay the cost twice."""
    _populate()
    evs = events.read_all()
    monkeypatch.setattr(events, "read_all", _explode)

    assert s_mod.rebuild(evs=evs).written
    assert s_mod.read().state.records[TASK].satisfactions


# --- the durable latency assertion --------------------------------------------------

def _explode(*a, **kw):
    raise AssertionError("the read path must never call events.read_all — "
                         "it measures ~264 ms on the real store and U9 runs "
                         "on EVERY user prompt")


def test_the_read_path_never_touches_the_ledger(monkeypatch):
    """The assertion that still means something in three years. A wall-clock
    bound drifts as the store grows and as CI hardware changes; "does not call
    `read_all`" is the actual property KTD9 is buying."""
    _populate()
    assert s_mod.rebuild().written

    monkeypatch.setattr(events, "read_all", _explode)

    got = s_mod.read()
    assert got.fresh
    assert got.state.records[TASK].phase == "plan"
    # The freshness test itself is the part most at risk of being "simplified"
    # into a ledger read, so it is exercised in both answers.
    os.utime(_segment(), ns=(0, 1))
    assert not s_mod.read().fresh


def test_reading_is_cheap_enough_to_run_on_every_prompt():
    """SECONDARY SIGNAL ONLY — deliberately loose. 200 reads inside 10 s is a
    50 ms/read budget against a target under 1 ms; CI runners are shared, cold,
    and occasionally paused by a snapshot. A tight timing bound becomes a flaky
    test that everyone learns to re-run rather than read, which is worse than
    no bound at all. The property that must hold precisely is asserted in
    `test_the_read_path_never_touches_the_ledger`."""
    _populate()
    assert s_mod.rebuild().written

    start = time.perf_counter()
    for _ in range(200):
        s_mod.read()
    assert time.perf_counter() - start < 10.0


# --- atomicity ------------------------------------------------------------------------

def _sized_state(n):
    """A state whose serialized length scales with *n*, so a hybrid of two
    writers' bytes is detectable rather than coincidentally valid."""
    return c_mod.PhaseState(
        {f"t-{i:04d}": c_mod.PhaseRecord(task_id=f"t-{i:04d}", phase="plan",
                                         epoch=i,
                                         violations=("filler " * 8,))
         for i in range(n)},
        {})


def _stamp():
    return {"events_seen": 0, "last_ts": "", "segments": s_mod.segment_stats()}


def _retry(fn, attempts=200):
    """Call *fn*, retrying ONLY a Windows sharing violation.

    `os.replace` is `MoveFileExW`, which has to open the destination for delete;
    CPython opens files without ``FILE_SHARE_DELETE``, so a concurrent reader —
    or another writer already mid-replace on the same destination — makes it
    raise `PermissionError`. That is a platform property of replacing a file
    in-place, NOT a symptom of tearing: distinct tmp names do not and cannot
    prevent it, because the contention is over the destination.

    In production `rebuild` catches this and reports it as a reason (the ledger
    is untouched and the sidecar is left whole either way), so it is a degraded
    refresh, never corruption. Retried here so these tests stay about atomicity
    instead of about Windows. Torn CONTENT is never retried — it fails."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.001)


def test_no_temp_file_survives_a_successful_write():
    s_mod.write(_sized_state(3), _stamp())
    assert list(s_mod.sidecar_path().parent.glob("*.tmp")) == []


def test_the_temp_name_is_distinct_per_writer(monkeypatch):
    """`paths.write_text_atomic` derives its tmp from the target alone
    (`paths.py:28`), so every writer shares one. pid-scoping (the
    `promptgate._write_manifest` idiom, `promptgate.py:319`) is not enough on
    its own here: the dashboard serves requests on THREADS, so two writers in
    one process would still collide. The name must be distinct on both axes."""
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    state, stamp = _sized_state(2), _stamp()

    def one_write():
        # Retried for the destination-contention reason in `_retry` — this test
        # is about the SOURCE name being unique, and an unhandled thread
        # exception here would pass while leaking a warning.
        _retry(lambda: s_mod.write(state, stamp))

    threads = [threading.Thread(target=one_write) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # One UNIQUE name per thread. Not `len(seen) == 4`: a retried replace spies
    # the same source again, so the count floats while the SET does not.
    assert len(seen) >= 4
    assert len(set(seen)) == 4, f"writers shared a tmp name: {sorted(set(seen))}"
    assert all(str(os.getpid()) in name for name in seen)


def test_concurrent_writers_never_publish_a_torn_file():
    """Real threads, distinct payload sizes, and a reader watching the whole
    time. With a shared tmp name one writer truncates another's half-written
    file and `os.replace` publishes the hybrid — which is not valid JSON.

    Both sides go through `_retry`, which absorbs the Windows destination
    contention described there and nothing else. Torn content is never retried
    — it fails the run."""
    sizes = (1, 30, 90, 250)
    rounds = 12
    torn, errors = [], []
    stop = threading.Event()

    def writer(n):
        state, stamp = _sized_state(n), _stamp()
        try:
            for _ in range(rounds):
                _retry(lambda: s_mod.write(state, stamp))
        except Exception as e:                      # reported, never swallowed
            errors.append(f"writer {n}: {type(e).__name__}: {e}")

    def reader():
        while not stop.is_set():
            try:
                raw = _retry(s_mod.sidecar_path().read_bytes)
            except (FileNotFoundError, PermissionError):
                continue
            if not raw:
                continue
            try:
                doc = json.loads(raw)
            except ValueError:
                torn.append(raw[:200])
                return
            if len(doc.get("records", {})) not in sizes:
                torn.append(raw[:200])
                return

    threads = [threading.Thread(target=writer, args=(n,)) for n in sizes]
    watcher = threading.Thread(target=reader)
    watcher.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    watcher.join()

    assert errors == []
    assert torn == [], f"a torn file was published: {torn[:1]}"
    assert len(json.loads(s_mod.sidecar_path().read_text(encoding="utf-8"))
               ["records"]) in sizes
    assert list(s_mod.sidecar_path().parent.glob("*.tmp")) == [], \
        "a temp file survived the storm"


# --- the surface itself ----------------------------------------------------------------

def test_the_sidecar_lives_where_the_evidence_contract_says_it_does():
    """`phases.py` names ``mindnet/state.json`` in prose as the machine's own
    sidecar — and gave `brainstorm` a required directory specifically so this
    file could not be submitted as evidence for it. If the path moves, that
    comment silently stops describing anything."""
    p = s_mod.sidecar_path()
    assert p == paths.project_dir() / "mindnet" / "state.json"
    assert p.parent.parent == paths.project_dir()
