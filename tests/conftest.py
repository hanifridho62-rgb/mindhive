# tests/conftest.py
"""Shared pytest fixtures for the MindHive suite.

Store isolation (MINDHIVE_HOME / MINDHIVE_PROJECT) was previously re-implemented
as a local _setup / _isolate / _store helper in most test files. `isolate_store`
centralizes the standard case as an autouse fixture: every test runs against a
throwaway per-test store, so the live ~/.mindhive ledger is never touched.
paths.* read these env vars at call time, so setting them here is sufficient.

Tests that need a variant (store under a tmp subdir, an extra chdir, a
pre-created .hive-loop dir) still set their own env in-body. monkeypatch applies
those AFTER this fixture, so the in-body values win — those files keep their
local helper on purpose.
"""
import pytest


@pytest.fixture(autouse=True)
def isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")
    # Auto-resume scheduling must never register a REAL Windows scheduled
    # task from inside the suite (autoresume._timer_enabled reads this).
    monkeypatch.setenv("MINDHIVE_AUTORESUME_TIMER", "off")
