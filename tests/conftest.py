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
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")
    # Auto-resume scheduling must never register a REAL Windows scheduled
    # task from inside the suite (autoresume._timer_enabled reads this).
    monkeypatch.setenv("MINDHIVE_AUTORESUME_TIMER", "off")
    # The MindSwarm soft-deprecation toggle is READ FROM THE AMBIENT
    # ENVIRONMENT by dashboard.mindswarm_enabled(), and README.md tells
    # operators to export MINDHIVE_MINDSWARM=0. A developer who did that
    # dropped the served shell to 15 nav buttons, which reds the browser
    # tier's `assert laid["buttons"] >= 16` with a message about a lost
    # rail -- a false regression caused by a documented, supported
    # setting. Pinned ON here; the tests that need it off set it in-body.
    monkeypatch.setenv("MINDHIVE_MINDSWARM", "1")
    # The SOP authorship gate (`mindhive.advice`, blueprint P1d) reads three
    # AMBIENT variables, and two supported setups make them hostile to a green
    # suite: a host that configured `MINDHIVE_ORCHESTRATION_SECRET` refuses
    # every `hive sop add` a test makes unless the token matches, and a suite run
    # by an orchestration agent inherits `MINDHIVE_SESSION=lm-...`, which the
    # fallback rule refuses by design. Cleared here for the MINDSWARM reason
    # above -- a documented setting must not read as a regression; the tests
    # that need one set it in-body.
    for ambient in ("MINDHIVE_ORCHESTRATION_SECRET", "MINDHIVE_PROCESS_TOKEN",
                    "MINDHIVE_SESSION"):
        monkeypatch.delenv(ambient, raising=False)
    yield
    # The isolation is only as good as the last line of the test. A blanket
    # `monkeypatch.undo()` inside a test body -- reached for to restore one
    # patched attribute -- also undoes THIS fixture, because pytest hands the
    # whole module one function-scoped monkeypatch. Everything the test writes
    # after that lands in the developer's real ~/.mindhive, silently: no error,
    # no failed assertion, just rows quietly appearing in the live store.
    #
    # It had been happening since 2026-08-13. `~/.mindhive/test/events/manual.jsonl`
    # held 82 job_started/job_finished pairs, one per full suite run, all from a
    # single test that called undo() to put `threading.Thread` back. Nothing
    # reported it because nothing was watching for it.
    #
    # This check is what watches. It costs one string comparison per test and it
    # names the test that escaped, at the moment it escapes.
    # The bar is "not the developer's real store", NOT "exactly tmp_path". A
    # first cut demanded equality and reported 138 failures on a green suite:
    # setting MINDHIVE_HOME to a tmp SUBDIR is a documented, supported variant
    # (tests/test_vectors.py:10 uses `tmp_path / "store"`), and a check that
    # forbids the supported case is a broken check, not a strict one.
    home = os.environ.get("MINDHIVE_HOME")
    real = Path.home() / ".mindhive"
    escaped = not home or Path(home).resolve() == real.resolve()
    assert not escaped, (
        "this test left MINDHIVE_HOME pointing at the REAL store (%s) — "
        "everything it wrote after that moment landed in the developer's live "
        "ledger, silently. A blanket monkeypatch.undo() is the usual cause, "
        "because pytest hands this fixture and the test body the same "
        "function-scoped monkeypatch; restore the one attribute you patched "
        "instead, or scope it with `with monkeypatch.context() as m:`."
        % (home or "unset -> ~/.mindhive"))
