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
import threading
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


# --------------------------------------------------------------------------
# `threading.Thread` is looked up by NAME, at call time, by every server fixture
# --------------------------------------------------------------------------
#
# `tests/serving.py` starts each test server's `serve_forever` loop through the
# name `threading.Thread`, so a test that replaces that name and does not put
# it back hands every later fixture whatever the stand-in does instead. A
# stand-in whose `start` raises is loud; one whose `start` does nothing hands
# the fixture a server whose loop never ran, and the stdlib's `shutdown()` on
# that server waits forever. `tests/test_jobs.py` replaces it on purpose and
# restores it in-body; this keeps that the only shape.
#
# A hook wrapper rather than an autouse fixture, because ORDER is the whole
# point: `isolate_store` above requests `monkeypatch`, which puts that
# monkeypatch ahead of any later autouse fixture in setup and therefore
# BEHIND it in teardown -- a fixture-based check ran before `monkeypatch.undo`
# and reported every `monkeypatch.setattr(threading, ...)` as a leak (it did,
# on a probe, before this was rewritten). After `pytest_runtest_teardown`
# yields, every fixture of the test is gone, undo included; what is still
# replaced then is replaced for the next test.

_REAL_THREADING = {name: getattr(threading, name)
                   for name in ("Thread", "Timer", "Event", "Lock")}


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Error the test that left a `threading` class replaced. Four identity
    checks per test, once its own fixtures have been torn down."""
    result = yield
    stale = sorted(name for name, real in _REAL_THREADING.items()
                   if getattr(threading, name) is not real)
    if stale:
        raise AssertionError(
            "%s left threading.%s replaced (%s). Every server fixture that "
            "runs after it starts its serve_forever loop through that name; "
            "restore the real class before the test returns, or patch it with "
            "`monkeypatch.setattr` so pytest restores it."
            % (item.nodeid, ", threading.".join(stale),
               ", ".join(repr(getattr(threading, n)) for n in stale)))
    return result


# --------------------------------------------------------------------------
# The CHECKOUT is not a scratch directory either
# --------------------------------------------------------------------------
#
# `isolate_store` keeps the suite out of the developer's ~/.mindhive. This
# keeps it out of the working copy, which is the other place a test scribbles
# by accident and nothing notices: pytest runs with the repo root as the
# working directory, so any code under test handed a RELATIVE path, and any
# subprocess spawned without `cwd=`, writes here.
#
# The failure it is built for is a quiet one. Six stray files
# (`b.bin`, `d/in.txt`, `f.txt`, `n.txt`, `s.txt`, `z.txt`) sat untracked in
# this worktree for most of a day, and the cost was not the disk: every
# `git status` read as dirty, so `git add -A` became unsafe and the debris had
# to be excluded by hand on every commit. Nothing failed. Nothing reported it.
#
# Cost is two directory listings PER RUN -- not per test -- and it names the
# entries, so the finding is actionable rather than "something changed".
# Session scope, not function scope, on purpose: a per-test check would be the
# precise one (it would name the test), but it would also pay 9500 listings to
# find a thing that has happened once, and a guard expensive enough to be
# turned off guards nothing. What this reports is enough to bisect by hand.

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Churn the tooling writes into the root by design. `.pytest_cache` is the
#: one that matters -- a default run creates it, `-p no:cacheprovider` does
#: not, and a guard that disagreed with itself between those two spellings
#: would be noise.
REPO_ROOT_IGNORED = frozenset({
    "__pycache__", ".pytest_cache", ".hypothesis", ".mypy_cache",
    ".ruff_cache", ".tox", ".coverage",
})


def repo_root_entries(root=None):
    """The checkout's top-level entry names, minus the tooling's own churn.

    Names, not paths, and not recursive: a stray file lands AT the root or in
    a directory it created there, and both show up as one new top-level name.
    A missing root answers empty rather than raising -- the guard must never
    be the thing that fails a run."""
    root = REPO_ROOT if root is None else Path(root)
    try:
        names = {entry.name for entry in root.iterdir()}
    except OSError:
        return set()
    return {n for n in names
            if n not in REPO_ROOT_IGNORED and not n.startswith(".coverage.")}


@pytest.fixture(scope="session", autouse=True)
def repo_root_stays_clean():
    """Fail the run that left a new entry in the checkout's root.

    Snapshot-and-compare rather than an allow-list of what MAY be there: a
    developer's `.venv`, `notes.md` or editor directory is their business,
    and a guard that fails on those is one they learn to delete. What is not
    their business is the suite adding something while it runs."""
    before = repo_root_entries()
    yield
    new = sorted(repo_root_entries() - before)
    assert not new, (
        "the suite left %d new entr(y/ies) in the repository root: %s. "
        "Something under test was handed a RELATIVE path, or a subprocess was "
        "spawned without `cwd=`; pytest's working directory is the checkout, "
        "so that is where it landed. Write under `tmp_path`, or "
        "`monkeypatch.chdir(tmp_path)` first. (If the run was interleaved with "
        "a hand-run script, that script is the likelier author -- check the "
        "mtimes before bisecting the suite.)"
        % (len(new), ", ".join(new)))
