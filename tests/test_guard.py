# tests/test_guard.py
import pytest
from mindhive import guard

def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")

def test_no_lock_is_safe(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    guard.ensure_safe()  # no raise

def test_lock_blocks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    (tmp_path / "LIVE_LOCK").write_text("v17 trading", encoding="utf-8")
    with pytest.raises(RuntimeError):
        guard.ensure_safe()

def test_force_overrides_lock(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    (tmp_path / "LIVE_LOCK").write_text("v17 trading", encoding="utf-8")
    guard.ensure_safe(force=True)  # no raise
