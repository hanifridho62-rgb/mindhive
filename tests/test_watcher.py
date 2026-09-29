# tests/test_watcher.py
"""The background embedding watcher (blueprint P1h; PDF BAB 4.4-4.6, 8.3;
MEB VEC-02).

The watcher is the consumer half of `embedqueue`: `append_event` queues
`(session_id, segment, seg_line)` into `state/pending_embed.json` and returns,
and this is what later turns those coordinates back into rows and embeds them.
Four things are load-bearing and each has its own section below:

* **The three-status marker lifecycle.** pending -> `processing_embed_<ts>.json`
  -> unlinked. The rename is the claim: one atomic operation, so two watchers
  cannot both take the same queue, and a crash mid-embed leaves the claim on
  disk where the next boot can find it.
* **NTFS rename resilience.** `os.replace` on Windows loses to the search
  indexer and to antimalware, with `PermissionError`, on a file that is
  perfectly intact.
* **Reading the row back.** The marker carries the SEGMENT, not just the id,
  because a quarantined id (`../x`, `CON`) lives in `unsafe-<hash>.jsonl` and
  a reader that rebuilt `f"{session_id}.jsonl"` would walk the exact path the
  quarantine closed.
* **Staying out of the way.** A daemon thread that the dashboard owns, a
  bounded `stop()`, and a marker emit fast enough that the append path does
  not notice it.
"""
import io
import json
import os
import statistics
import threading
import time
from pathlib import Path

import pytest

from mindhive import chain, embedqueue, events, paths, vectors, watcher
from mindhive.core.models import (EventRecord, GENESIS_PREV_HASH,
                                  compute_chain_hash)


# --- helpers -----------------------------------------------------------------

def _append(text="chose postgres", session_id="alpha", type="decision"):
    return events.append_event(type, {"text": text}, session_id=session_id)


def _marker() -> Path:
    return paths.state_dir() / embedqueue.MARKER_NAME


def _queue() -> dict:
    return json.loads(_marker().read_text(encoding="utf-8"))


def _processing() -> list:
    return sorted(paths.state_dir().glob(watcher.PROCESSING_GLOB))


class _Recorder:
    """Stands in for the VectorStoreManager the watcher drives."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def embed_batch(self, slug, records, progress_callback=None, **kw):
        self.calls.append((slug, list(records)))
        if self.fail:
            raise RuntimeError("chroma is unhappy")
        total = len(records)
        for i in range(total):
            if progress_callback:
                progress_callback(i + 1, total)
        return total


def _stub_manager(monkeypatch, rec=None):
    rec = rec if rec is not None else _Recorder()
    monkeypatch.setattr(vectors.VectorStoreManager, "get_instance",
                        classmethod(lambda cls, project=None: rec))
    monkeypatch.setattr(vectors, "_require_deps", lambda: None)
    return rec


# --- atomic rename (PDF 4.4.2) -----------------------------------------------

def test_the_backoff_is_the_ladder_the_blueprint_fixed():
    assert watcher.RENAME_BACKOFF_S == (0.02, 0.04, 0.08, 0.16, 0.32)
    assert watcher.RENAME_ATTEMPTS == 5


def test_a_rename_that_works_first_time_never_sleeps(tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("x", encoding="utf-8")
    slept = []
    assert watcher.atomic_rename_resilient(src, dst, sleep=slept.append) is True
    assert slept == []
    assert dst.read_text(encoding="utf-8") == "x"


def test_a_denied_rename_retries_down_the_whole_ladder(tmp_path, monkeypatch):
    """Five attempts means four waits: the last failure has nothing left to
    wait for, and sleeping after it is 0.32 s of latency nobody uses."""
    slept, tries = [], []

    def _angry(src, dst):
        tries.append((src, dst))
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(watcher.os, "replace", _angry)
    assert watcher.atomic_rename_resilient(tmp_path / "a", tmp_path / "b",
                                           sleep=slept.append) is False
    assert len(tries) == 5
    assert slept == [0.02, 0.04, 0.08, 0.16]


def test_a_rename_that_clears_on_the_third_try_succeeds(tmp_path, monkeypatch):
    slept = []
    calls = {"n": 0}
    real = watcher.os.replace

    def _flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    (tmp_path / "a").write_text("x", encoding="utf-8")
    monkeypatch.setattr(watcher.os, "replace", _flaky)
    assert watcher.atomic_rename_resilient(tmp_path / "a", tmp_path / "b",
                                           sleep=slept.append) is True
    assert slept == [0.02, 0.04]


def test_a_strict_rename_raises_the_error_it_could_not_beat(tmp_path,
                                                            monkeypatch):
    """The daemon wants a bool -- a transient NTFS lock must not kill the
    loop -- but a one-shot `hive watcher run --once` wants the reason."""
    monkeypatch.setattr(watcher.os, "replace",
                        lambda s, d: (_ for _ in ()).throw(
                            PermissionError(5, "Access is denied")))
    with pytest.raises(PermissionError):
        watcher.atomic_rename_resilient(tmp_path / "a", tmp_path / "b",
                                        sleep=lambda _s: None, strict=True)


def test_a_missing_source_is_not_retried_five_times(tmp_path, monkeypatch):
    """FileNotFoundError is not contention: another watcher already claimed
    the queue, and waiting 0.6 s to be told so again is pure latency."""
    slept = []
    assert watcher.atomic_rename_resilient(tmp_path / "gone", tmp_path / "b",
                                           sleep=slept.append) is False
    assert slept == []


# --- reading the physical row back (RP I.1) ----------------------------------

def test_a_record_is_rebuilt_from_its_physical_line():
    _append("first")
    _append("second")
    rec = watcher.read_physical_record("alpha", 2)
    assert isinstance(rec, EventRecord)
    assert rec.event_id == "alpha:2"
    assert rec.seg_line == 2
    assert rec.event_type == "decision"
    assert rec.payload["text"] == "second"


def test_the_chain_hash_is_computed_over_the_row_it_names():
    _append("first")
    _append("second")
    first = watcher.read_physical_record("alpha", 1)
    second = watcher.read_physical_record("alpha", 2)
    assert first.prev_hash == GENESIS_PREV_HASH
    assert second.prev_hash == first.chain_hash
    row = events.canonical_json(events.to_dict(
        events.read_all()[1]))
    assert second.chain_hash == compute_chain_hash(second.prev_hash, row)


def test_the_segment_fold_agrees_with_the_chain_over_one_segment():
    """Carrying one fold across segments in name order IS `chain._fold`, so
    over a single segment the two must already agree -- otherwise the
    back-pointer names a hash no verifier will ever compute."""
    for i in range(4):
        _append(f"row {i}")
    last = watcher.read_physical_record("alpha", 4)
    assert last.chain_hash == chain.compute_chain(events.read_all())["head"]


def test_a_line_past_the_end_is_none_rather_than_a_crash():
    _append("only")
    assert watcher.read_physical_record("alpha", 9) is None


def test_a_missing_segment_is_none():
    assert watcher.read_physical_record("nobody", 1) is None


@pytest.mark.parametrize("session_id", [
    "../x",
    # A device, and so quarantined, on Windows alone: POSIX keeps `CON.jsonl`
    # as the ordinary name it is there (`paths.is_reserved_component`).
    pytest.param("CON", marks=pytest.mark.skipif(
        os.name != "nt", reason="CON is quarantined only where it is a device")),
    "a\nb"])
def test_a_quarantined_id_is_read_from_the_segment_it_was_written_to(
        session_id, monkeypatch):
    """`append_event` quarantines these to `unsafe-<sha256[:16]>.jsonl`. A
    reader that rebuilt `f"{session_id}.jsonl"` would walk the exact path
    the quarantine closed, and raise -- or worse, on `CON`, open a device."""
    events.append_event("decision", {"text": "quarantined"},
                        session_id=session_id)
    opened = []
    real = events._segment_parts
    monkeypatch.setattr(events, "_segment_parts",
                        lambda seg: (opened.append(Path(seg).name) or real(seg)))
    rec = watcher.read_physical_record(session_id, 1)
    assert rec is not None and rec.payload["text"] == "quarantined"
    assert opened and all(n.startswith("unsafe-") for n in opened), opened
    assert not any(n == f"{session_id}.jsonl" for n in opened)


def test_the_marker_segment_is_preferred_over_a_recomputed_name(monkeypatch):
    """The queue entry carries the basename `append_event` actually wrote to.
    Trusting it is what keeps the watcher correct if the quarantine rule ever
    changes shape."""
    _append("row")
    seg = events.segment_name("alpha")
    opened = []
    real = events._segment_parts
    monkeypatch.setattr(events, "_segment_parts",
                        lambda s: (opened.append(Path(s).name) or real(s)))
    assert watcher.read_physical_record("ignored", 1, segment=seg) is not None
    assert opened == [seg]


# --- the marker lifecycle (PDF 4.4.1) ----------------------------------------

def test_claiming_renames_the_pending_queue_to_a_processing_marker():
    _append("one")
    assert _marker().exists()
    w = watcher.BackgroundEmbeddingWatcher()
    claimed = w.claim()
    assert claimed is not None
    assert claimed.name.startswith(watcher.PROCESSING_PREFIX)
    assert claimed.name.endswith(".json")
    assert not _marker().exists(), "the pending queue survived the claim"
    assert _processing() == [claimed]


def test_claiming_an_empty_queue_is_none():
    assert watcher.BackgroundEmbeddingWatcher().claim() is None
    assert _processing() == []


def test_a_claim_carries_the_entries_it_took():
    _append("one")
    _append("two")
    claimed = watcher.BackgroundEmbeddingWatcher().claim()
    doc = json.loads(claimed.read_text(encoding="utf-8"))
    assert [e["seg_line"] for e in doc["entries"]] == [1, 2]
    assert all(e["segment"] == "alpha.jsonl" for e in doc["entries"])


def test_two_claims_cannot_take_the_same_queue():
    """The rename IS the mutual exclusion: the loser finds nothing to rename
    rather than embedding the same rows twice."""
    _append("one")
    a = watcher.BackgroundEmbeddingWatcher().claim()
    b = watcher.BackgroundEmbeddingWatcher().claim()
    assert a is not None and b is None


def test_processing_a_marker_embeds_and_then_unlinks_it(monkeypatch):
    rec = _stub_manager(monkeypatch)
    _append("one")
    _append("two")
    w = watcher.BackgroundEmbeddingWatcher()
    claimed = w.claim()
    out = w.process_marker(claimed)
    assert out["count"] == 2
    assert not claimed.exists(), "a finished claim was left on disk"
    assert len(rec.calls) == 1
    _slug, records = rec.calls[0]
    assert [r.seg_line for r in records] == [1, 2]
    assert all(isinstance(r, EventRecord) for r in records)


def test_a_failed_embed_leaves_the_claim_for_the_next_boot(monkeypatch):
    """Unlinking on failure would drop the rows silently: the marker IS the
    only record that they were never indexed."""
    _stub_manager(monkeypatch, _Recorder(fail=True))
    _append("one")
    w = watcher.BackgroundEmbeddingWatcher()
    claimed = w.claim()
    out = w.process_marker(claimed)
    assert out["count"] == 0
    assert out["error"]
    assert claimed.exists()


def test_an_entry_whose_row_vanished_is_skipped_not_fatal(monkeypatch):
    rec = _stub_manager(monkeypatch)
    _append("one")
    w = watcher.BackgroundEmbeddingWatcher()
    claimed = w.claim()
    doc = json.loads(claimed.read_text(encoding="utf-8"))
    doc["entries"].append({"session_id": "ghost", "seg_line": 4,
                           "segment": "ghost.jsonl", "queued_at": ""})
    claimed.write_text(json.dumps(doc), encoding="utf-8")
    assert w.process_marker(claimed)["count"] == 1
    assert not claimed.exists()


def test_a_marker_with_nothing_readable_is_still_cleared(monkeypatch):
    _stub_manager(monkeypatch)
    w = watcher.BackgroundEmbeddingWatcher()
    stub = paths.state_dir() / f"{watcher.PROCESSING_PREFIX}9.json"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("{ not json", encoding="utf-8")
    assert w.process_marker(stub)["count"] == 0
    assert not stub.exists(), "an unparseable claim would be retried forever"


# --- progress (PDF 4.6) ------------------------------------------------------

def test_the_progress_line_is_the_one_the_dashboard_parses(monkeypatch):
    out = io.StringIO()
    rec = _Recorder()
    _stub_manager(monkeypatch, rec)
    for i in range(10):
        _append(f"row {i}")
    w = watcher.BackgroundEmbeddingWatcher(out=out)
    w.process_marker(w.claim())
    text = out.getvalue()
    assert "\r[MINDHIVE VEC-SYNC] Menanam: 3/10 (30.0%)" in text
    assert "\r[MINDHIVE VEC-SYNC] Menanam: 10/10 (100.0%)" in text
    assert text.endswith("\n"), "the carriage-return run was never closed"


def test_progress_carries_the_prefix_the_job_ring_keys_on():
    assert watcher.PROGRESS_PREFIX == "[MINDHIVE VEC-SYNC]"


def test_a_watcher_with_no_output_sink_stays_silent(monkeypatch, capsys):
    _stub_manager(monkeypatch)
    _append("one")
    w = watcher.BackgroundEmbeddingWatcher(out=None)
    w.process_marker(w.claim())
    assert capsys.readouterr().out == ""


# --- the ledger record of a sync ---------------------------------------------

def test_a_sync_is_recorded_at_both_ends(monkeypatch):
    _stub_manager(monkeypatch)
    _append("one")
    w = watcher.BackgroundEmbeddingWatcher()
    claimed = w.claim()
    w.process_marker(claimed)
    kinds = [e.type for e in events.read_all()]
    assert kinds.count(watcher.SYNC_STARTED) == 1
    assert kinds.count(watcher.SYNC_COMPLETED) == 1
    done = [e for e in events.read_all() if e.type == watcher.SYNC_COMPLETED][0]
    assert set(done.payload) == {"marker", "count", "duration_s"}
    assert done.payload["marker"] == claimed.name
    assert done.payload["count"] == 1
    assert isinstance(done.payload["duration_s"], float)


def test_a_sync_event_never_queues_another_embed(monkeypatch):
    """`vector_sync_*` must stay out of `SEMANTIC_TYPES`: a watcher whose own
    completion row queued a new embed would never stop."""
    assert watcher.SYNC_STARTED not in events.SEMANTIC_TYPES
    assert watcher.SYNC_COMPLETED not in events.SEMANTIC_TYPES
    _stub_manager(monkeypatch)
    _append("one")
    w = watcher.BackgroundEmbeddingWatcher()
    w.process_marker(w.claim())
    assert not _marker().exists(), "the sync rows re-queued themselves"


def test_the_dashboard_draws_its_bar_from_the_number_not_the_tail():
    """The pane cannot re-parse the progress frame: the ring holds the live
    one out of the byte stream the tail is built from. So `jobText` has to
    read `progress_pct`, and only while the job is still running."""
    from mindhive import dashboard_js_actions
    js = dashboard_js_actions.JS
    assert "job.progress_pct" in js
    assert "job.state !== 'finished'" in js
    assert "'] ' + pct + '%" in js


def test_the_bar_is_twenty_cells_wide_at_every_percentage():
    """Pure arithmetic over the expression the fragment carries -- a bar that
    goes negative at 100% would throw `RangeError` inside the pane."""
    for pct in (0, 0.1, 30.0, 49.9, 50, 99.9, 100):
        filled = round(pct / 5)
        assert 0 <= filled <= 20, pct
        assert 20 - filled >= 0, pct


def test_the_bar_clamps_the_number_before_it_draws_a_cell():
    """The arithmetic above holds only INSIDE 0..100. `repeat` throws
    `RangeError` on a negative count, so a frame past 100% (`20 - filled`
    negative) or below 0% (`filled` negative) took the whole poll render down
    with it. The one producer keeps `done <= total` today; the pane does not
    get to depend on that. `tests/test_dashboard_jobbar_node.py` runs the
    fragment's own bytes against both; this pins the clamp in the default
    tier, where node does not run."""
    from mindhive import dashboard_js_actions
    js = dashboard_js_actions.JS
    assert "Math.min(100, Math.max(0, " in js
    assert "Number.isFinite(job.progress_pct)" in js


# --- startup recovery (PDF 4.5) ----------------------------------------------

def test_a_stale_claim_is_returned_to_the_pending_queue():
    _append("one")
    claimed = watcher.BackgroundEmbeddingWatcher().claim()
    import os as _os
    old = time.time() - (watcher.STALE_MARKER_S + 60)
    _os.utime(claimed, (old, old))
    assert watcher.recover_stale_markers() == 1
    assert not claimed.exists()
    assert [e["seg_line"] for e in _queue()["entries"]] == [1]


def test_a_fresh_claim_belongs_to_a_live_watcher_and_is_left_alone():
    _append("one")
    claimed = watcher.BackgroundEmbeddingWatcher().claim()
    assert watcher.recover_stale_markers() == 0
    assert claimed.exists()
    assert not _marker().exists()


def test_the_stale_window_is_the_one_the_blueprint_fixed():
    assert watcher.STALE_MARKER_S == 600.0


def test_recovery_merges_rather_than_overwrites_a_live_queue():
    """Entries queued while the dead watcher held its claim must survive the
    recovery that puts the claimed ones back."""
    _append("one")
    claimed = watcher.BackgroundEmbeddingWatcher().claim()
    _append("two")
    import os as _os
    old = time.time() - (watcher.STALE_MARKER_S + 60)
    _os.utime(claimed, (old, old))
    watcher.recover_stale_markers()
    assert sorted(e["seg_line"] for e in _queue()["entries"]) == [1, 2]


def test_recovery_drops_entries_already_indexed(monkeypatch):
    """PDF 4.5 step 3: the surviving vectors are reconciled against the
    index, so a crash two rows from the end re-embeds two rows, not two
    hundred."""
    _append("one")
    _append("two")
    claimed = watcher.BackgroundEmbeddingWatcher().claim()
    import os as _os
    old = time.time() - (watcher.STALE_MARKER_S + 60)
    _os.utime(claimed, (old, old))

    class _Indexed:
        def already_indexed(self, slug, ids):
            return {i for i in ids if i == "alpha:1"}

    # Recovery only reconciles where an index EXISTS: opening a client to be
    # told a namespace has never embedded would import chromadb and mint a
    # store on every boot of a machine that does not use the semantic layer.
    (paths.project_dir() / "chroma").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vectors.VectorStoreManager, "get_instance",
                        classmethod(lambda cls, project=None: _Indexed()))
    assert watcher.recover_stale_markers() == 1
    assert [e["seg_line"] for e in _queue()["entries"]] == [2]


def test_recovery_with_nothing_to_recover_is_zero():
    assert watcher.recover_stale_markers() == 0


def test_recovery_of_an_unreadable_claim_still_clears_it():
    stub = paths.state_dir() / f"{watcher.PROCESSING_PREFIX}1.json"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("{ not json", encoding="utf-8")
    import os as _os
    old = time.time() - (watcher.STALE_MARKER_S + 60)
    _os.utime(stub, (old, old))
    assert watcher.recover_stale_markers() == 1
    assert not stub.exists()


# --- the daemon thread (RP II.4) ---------------------------------------------

def test_the_thread_is_a_named_daemon(monkeypatch):
    _stub_manager(monkeypatch)
    w = watcher.BackgroundEmbeddingWatcher(poll_interval_s=0.01)
    w.start()
    try:
        assert w.thread is not None
        assert w.thread.daemon is True
        assert w.thread.name == watcher.THREAD_NAME
    finally:
        w.stop()


def test_stop_returns_inside_its_budget(monkeypatch):
    _stub_manager(monkeypatch)
    w = watcher.BackgroundEmbeddingWatcher(poll_interval_s=0.01)
    w.start()
    t0 = time.monotonic()
    w.stop()
    assert time.monotonic() - t0 < watcher.STOP_JOIN_S
    assert w.thread is None or not w.thread.is_alive()


def test_starting_twice_does_not_make_a_second_thread(monkeypatch):
    _stub_manager(monkeypatch)
    w = watcher.BackgroundEmbeddingWatcher(poll_interval_s=0.01)
    w.start()
    first = w.thread
    try:
        w.start()
        assert w.thread is first
    finally:
        w.stop()


def test_stopping_a_watcher_that_never_started_is_harmless():
    watcher.BackgroundEmbeddingWatcher().stop()


def test_the_loop_drains_a_queue_it_finds(monkeypatch):
    rec = _stub_manager(monkeypatch)
    _append("one")
    w = watcher.BackgroundEmbeddingWatcher(poll_interval_s=0.01)
    w.start()
    try:
        for _ in range(200):
            if rec.calls:
                break
            time.sleep(0.02)
    finally:
        w.stop()
    assert rec.calls, "the loop never drained the pending queue"
    assert not _marker().exists()


def test_a_raising_cycle_never_kills_the_loop(monkeypatch):
    """A daemon that dies on one bad marker stops embedding for the life of
    the dashboard, silently."""
    calls = []

    def _boom(self):
        calls.append(1)
        raise RuntimeError("no")

    monkeypatch.setattr(watcher.BackgroundEmbeddingWatcher, "run_once", _boom)
    w = watcher.BackgroundEmbeddingWatcher(poll_interval_s=0.01)
    w.start()
    try:
        for _ in range(200):
            if len(calls) >= 3:
                break
            time.sleep(0.02)
    finally:
        w.stop()
    assert len(calls) >= 3


def test_the_poll_interval_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv(watcher.POLL_ENV, "0.25")
    assert watcher.BackgroundEmbeddingWatcher().poll_interval_s == 0.25


def test_a_nonsense_poll_interval_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(watcher.POLL_ENV, "not-a-number")
    assert (watcher.BackgroundEmbeddingWatcher().poll_interval_s
            == watcher.POLL_INTERVAL_S)


def test_an_explicit_interval_beats_the_environment(monkeypatch):
    monkeypatch.setenv(watcher.POLL_ENV, "0.25")
    assert watcher.BackgroundEmbeddingWatcher(poll_interval_s=5.0
                                              ).poll_interval_s == 5.0


# --- status ------------------------------------------------------------------

def test_status_counts_what_is_waiting_and_what_is_claimed():
    assert watcher.status() == {"pending": 0, "processing": 0, "markers": []}
    _append("one")
    _append("two")
    assert watcher.status()["pending"] == 2
    claimed = watcher.BackgroundEmbeddingWatcher().claim()
    st = watcher.status()
    assert st == {"pending": 0, "processing": 1, "markers": [claimed.name]}


# --- the CLI -----------------------------------------------------------------

def _args(**kw):
    import argparse
    kw.setdefault("project", None)
    return argparse.Namespace(**kw)


def test_hive_watcher_status_prints_the_counts(monkeypatch, capsys):
    from mindhive.commands import verification as verif
    _append("one")
    assert verif.cmd_watcher_status(_args(json=False)) is None
    assert "pending 1" in capsys.readouterr().out


def test_hive_watcher_status_json_is_machine_readable(capsys):
    from mindhive.commands import verification as verif
    _append("one")
    verif.cmd_watcher_status(_args(json=True))
    assert json.loads(capsys.readouterr().out) == {
        "pending": 1, "processing": 0, "markers": []}


def test_hive_watcher_run_once_drains_and_reports(monkeypatch, capsys):
    from mindhive.commands import verification as verif
    _stub_manager(monkeypatch)
    _append("one")
    assert verif.cmd_watcher_run(_args(once=True, json=True)) is None
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert not _marker().exists()


def test_hive_watcher_run_once_on_an_empty_queue_says_so(monkeypatch, capsys):
    from mindhive.commands import verification as verif
    _stub_manager(monkeypatch)
    assert verif.cmd_watcher_run(_args(once=True, json=True)) is None
    assert json.loads(capsys.readouterr().out)["count"] == 0


def test_hive_watcher_recover_returns_stale_claims(monkeypatch, capsys):
    from mindhive.commands import verification as verif
    _append("one")
    claimed = watcher.BackgroundEmbeddingWatcher().claim()
    import os as _os
    old = time.time() - (watcher.STALE_MARKER_S + 60)
    _os.utime(claimed, (old, old))
    assert verif.cmd_watcher_recover(_args(stale=None, json=False)) is None
    assert "1" in capsys.readouterr().out
    assert _marker().exists()


def test_hive_watcher_recover_honours_an_explicit_window(capsys):
    from mindhive.commands import verification as verif
    _append("one")
    claimed = watcher.BackgroundEmbeddingWatcher().claim()
    verif.cmd_watcher_recover(_args(stale=0.0, json=False))
    capsys.readouterr()
    assert not claimed.exists(), "an explicit --stale 0 recovered nothing"


def test_the_watcher_parser_offers_all_three_verbs():
    from mindhive.commands.parsers import _build_parser
    p = _build_parser()
    assert p.parse_args(["watcher", "run", "--once"]).once is True
    assert p.parse_args(["watcher", "status"]).func.__name__ == (
        "cmd_watcher_status")
    assert p.parse_args(["watcher", "recover"]).func.__name__ == (
        "cmd_watcher_recover")


def test_the_watcher_parser_validates_its_project(capsys):
    from mindhive.commands.parsers import _build_parser
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["watcher", "status", "--project", "../x"])


# --- the append path stays cheap (K8) ----------------------------------------

def test_the_marker_emit_is_cheap_enough_to_sit_inside_the_append_lock():
    """MEB 1.4 replaced the 24/7 daemon with an in-process projection, and the
    whole argument rests on this number: `emit_pending_embed_marker` runs
    inside `events._append_lock`, on every semantic append, so a slow emit is
    a slow ledger for every writer. The median of 20 is reported either way."""
    seg = paths.events_dir() / "alpha.jsonl"
    samples = []
    for i in range(20):
        t0 = time.perf_counter()
        embedqueue.emit_pending_embed_marker(None, "alpha", seg.name, i + 1)
        samples.append((time.perf_counter() - t0) * 1000.0)
    median = statistics.median(samples)
    print(f"\ntime_to_emit_marker: median {median:.2f} ms over 20 "
          f"(max {max(samples):.2f} ms)")
    assert median < 15.0, f"median {median:.2f} ms over 20 appends"
