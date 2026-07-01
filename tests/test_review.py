# tests/test_review.py
from mindhive import events, projector, paths, review


def _mkfile(tmp_path, relpath, lines):
    p = tmp_path / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(f"line{i}" for i in range(lines)), encoding="utf-8")
    return str(p)


def test_in_scope_includes_source_and_tests_only():
    assert review.in_scope("/repo/src/mindhive/graph.py")
    assert review.in_scope(r"C:\repo\src\mindhive\graph.py")
    assert review.in_scope("/repo/tests/test_graph.py")
    # excluded
    assert not review.in_scope("/repo/docs/adr/0001.md")
    assert not review.in_scope("/repo/src/mindhive/__pycache__/graph.cpython-314.pyc")
    assert not review.in_scope("/repo/src/mindhive.egg-info/SOURCES.txt")
    assert not review.in_scope("/repo/README.md")
    assert not review.in_scope("/repo/build/app_entry.py")  # not under src/mindhive or tests


def test_touched_files_dedups_and_scopes_by_session(tmp_path):
    a = _mkfile(tmp_path, "src/mindhive/a.py", 10)
    events.append_event("file_touched", {"text": a}, session_id="s1", ts="2026-06-20T10:00:00Z")
    events.append_event("file_touched", {"text": a}, session_id="s1", ts="2026-06-20T10:01:00Z")
    events.append_event("file_touched", {"text": "/repo/docs/x.md"}, session_id="s1",
                        ts="2026-06-20T10:02:00Z")
    events.append_event("file_touched", {"text": "/repo/src/mindhive/other.py"},
                        session_id="s2", ts="2026-06-20T10:03:00Z")
    evs = events.read_all()
    got = review.touched_files(evs, session_id="s1")
    assert got == [a]  # deduped, docs excluded, s2 excluded


def test_batch_files_packs_whole_files_under_limit(tmp_path):
    f1 = _mkfile(tmp_path, "src/mindhive/f1.py", 600)
    f2 = _mkfile(tmp_path, "src/mindhive/f2.py", 600)
    f3 = _mkfile(tmp_path, "src/mindhive/f3.py", 600)
    batches = review.batch_files([f1, f2, f3], limit=1500)
    assert batches == [[f1, f2], [f3]]  # 600+600 fits, third spills to a new batch


def test_batch_files_oversized_file_stands_alone(tmp_path):
    big = _mkfile(tmp_path, "src/mindhive/big.py", 3000)
    small = _mkfile(tmp_path, "src/mindhive/small.py", 100)
    batches = review.batch_files([big, small], limit=1500)
    assert batches == [[big], [small]]


def test_batch_files_drops_missing_files(tmp_path):
    real = _mkfile(tmp_path, "src/mindhive/real.py", 50)
    batches = review.batch_files([real, str(tmp_path / "src/mindhive/gone.py")])
    assert batches == [[real]]


def test_queue_writes_marker_and_load_roundtrip(tmp_path):
    a = _mkfile(tmp_path, "src/mindhive/a.py", 100)
    events.append_event("file_touched", {"text": a}, session_id="s1", ts="2026-06-20T10:00:00Z")
    marker = review.queue(events.read_all(), session_id="s1")
    assert marker is not None
    assert marker["files"] == [a]
    assert marker["batch_count"] == 1
    assert review.load_pending() == marker


def test_queue_empty_scope_returns_none_and_clears_stale(tmp_path):
    # pre-existing stale marker
    a = _mkfile(tmp_path, "src/mindhive/a.py", 100)
    events.append_event("file_touched", {"text": a}, session_id="s1", ts="2026-06-20T10:00:00Z")
    review.queue(events.read_all(), session_id="s1")
    assert review.pending_path().exists()
    # a session that touched nothing in scope must clear the stale marker
    assert review.queue([], session_id="s2") is None
    assert not review.pending_path().exists()


def test_clear_reports_presence(tmp_path):
    a = _mkfile(tmp_path, "src/mindhive/a.py", 100)
    events.append_event("file_touched", {"text": a}, session_id="s1", ts="2026-06-20T10:00:00Z")
    review.queue(events.read_all(), session_id="s1")
    assert review.clear() is True
    assert review.clear() is False


def test_notice_empty_when_no_marker():
    assert review.notice(None) == ""


def test_projector_renders_pending_notice_into_state(tmp_path):
    a = _mkfile(tmp_path, "src/mindhive/a.py", 100)
    events.append_event("file_touched", {"text": a}, session_id="s1", ts="2026-06-20T10:00:00Z")
    review.queue(events.read_all(), session_id="s1")
    projector.project()
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "Pending thermo-nuclear review" in state
    assert a in state
    # and once cleared, the notice is gone after a rebuild
    review.clear()
    projector.project()
    state2 = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "Pending thermo-nuclear review" not in state2
