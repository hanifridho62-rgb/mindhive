# tests/test_guard.py
import pytest
from mindhive import guard


def test_no_lock_is_safe():
    guard.ensure_safe()  # no raise

def test_lock_blocks(tmp_path):
    (tmp_path / "LIVE_LOCK").write_text("v17 trading", encoding="utf-8")
    with pytest.raises(RuntimeError):
        guard.ensure_safe()

def test_force_overrides_lock(tmp_path):
    (tmp_path / "LIVE_LOCK").write_text("v17 trading", encoding="utf-8")
    guard.ensure_safe(force=True)  # no raise
