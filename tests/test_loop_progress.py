# tests/test_loop_progress.py
from mindhive.loop import progress


def test_mtime_tracker_detects_change(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one", encoding="utf-8")
    tracker = progress.MtimeTracker(tmp_path)
    tracker.snapshot()                 # baseline
    assert tracker.changed() is False  # nothing changed yet
    f.write_text("two", encoding="utf-8")
    assert tracker.changed() is True   # mtime/content changed


def test_mtime_tracker_detects_new_file(tmp_path):
    tracker = progress.MtimeTracker(tmp_path)
    tracker.snapshot()
    (tmp_path / "new.txt").write_text("x", encoding="utf-8")
    assert tracker.changed() is True
