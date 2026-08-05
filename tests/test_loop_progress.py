# tests/test_loop_progress.py
from mindhive.loop import progress


def test_mtime_tracker_detects_change(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one", encoding="utf-8")
    tracker = progress.MtimeTracker(tmp_path)
    tracker.snapshot()                 # baseline
    assert tracker.changed() is False  # nothing changed yet
    # write content of a DIFFERENT size: the cheap (mtime_ns, size) detector
    # must not depend on sub-tick mtime resolution to notice an edit.
    f.write_text("rewritten", encoding="utf-8")
    assert tracker.changed() is True   # size changed


def test_mtime_tracker_detects_new_file(tmp_path):
    tracker = progress.MtimeTracker(tmp_path)
    tracker.snapshot()
    (tmp_path / "new.txt").write_text("x", encoding="utf-8")
    assert tracker.changed() is True


def test_changed_paths_names_what_moved(tmp_path):
    from mindhive.loop.progress import MtimeTracker
    (tmp_path / "a.py").write_text("one", encoding="utf-8")
    (tmp_path / "b.py").write_text("two", encoding="utf-8")
    tracker = MtimeTracker(tmp_path)
    tracker.snapshot()
    (tmp_path / "a.py").write_text("one changed", encoding="utf-8")
    moved = tracker.changed_paths()
    assert [p for p in moved if p.endswith("a.py")]
    assert not [p for p in moved if p.endswith("b.py")]


def test_changed_paths_reports_a_deletion(tmp_path):
    from mindhive.loop.progress import MtimeTracker
    (tmp_path / "gone.py").write_text("x", encoding="utf-8")
    tracker = MtimeTracker(tmp_path)
    tracker.snapshot()
    (tmp_path / "gone.py").unlink()
    assert [p for p in tracker.changed_paths() if p.endswith("gone.py")]


def test_changed_paths_does_not_consume_the_snapshot(tmp_path):
    # The engine owns when the snapshot advances. A reader that silently consumed
    # it would make the next `changed()` report no progress on a run that made
    # some.
    from mindhive.loop.progress import MtimeTracker
    (tmp_path / "a.py").write_text("one", encoding="utf-8")
    tracker = MtimeTracker(tmp_path)
    tracker.snapshot()
    # A size change, not just content: the tracker's key is (mtime_ns, size), and
    # a same-size rewrite is invisible to it when the OS mtime tick is coarse.
    # That limitation predates `changed_paths` and applies equally to `changed()`.
    (tmp_path / "a.py").write_text("one plus more", encoding="utf-8")
    assert tracker.changed_paths()
    assert tracker.changed() is True


def test_changed_keeps_its_boolean_contract(tmp_path):
    from mindhive.loop.progress import MtimeTracker
    tracker = MtimeTracker(tmp_path)
    tracker.snapshot()
    assert tracker.changed() is False
