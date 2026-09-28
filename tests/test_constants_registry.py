# tests/test_constants_registry.py — blueprint P8 (MEB section 10.1): the calibrated constants, one table.
"""The MEB's appendix 10.1 names thirteen calibrated constants, the module that
owns each, its type and its value. They were implemented phase by phase (P1c,
P1j, P3, P4, P0b) and each is pinned somewhere near its use; this file is the
appendix itself, read back against the code in one place, so a change to any of
them is a change to the table a reader of the MEB will check first.

It is a characterisation test: every value already exists when it is written.
What keeps it from being vacuous is `_mismatch`, the one checker every row goes
through, and the canary that proves it reports a wrong value, a wrong type, a
missing name and a re-export posing as a definition.

Two places where the tree and the MEB's wording part, both deliberate and both
asserted rather than papered over:

* `GENESIS_PREV_HASH` -- the MEB puts it in `events.py`. Blueprint P0b carved
  the canonical-record helpers into `core/models.py` (the stdlib-only core), and
  `events` re-exports the SAME object, so `events.GENESIS_PREV_HASH` is still
  the name a caller reaches for.
* `_LOCK_RETRY_BASE_MS` / `_LOCK_RETRY_MAX_MS` -- named in milliseconds, valued
  in seconds (0.015 = 15 ms), exactly as the MEB's own table gives them. The
  values are what `task_lock`'s backoff sleeps, so the names stay the MEB's.
"""
import ast
import importlib
import inspect
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "mindhive"

# (symbol, owner module per the MEB, type, calibrated value) — MEB 10.1 row order.
MEB_10_1 = [
    ("_LOCK_TIMEOUT_SECS", "mindhive.tasktool", float, 15.0),
    ("_STALE_LOCK_AGE_SECS", "mindhive.tasktool", float, 60.0),
    ("_TASK_LOCK_DENIED_SPINS", "mindhive.tasktool", int, 20),
    ("_LOCK_RETRY_BASE_MS", "mindhive.tasktool", float, 0.015),
    ("_LOCK_RETRY_MAX_MS", "mindhive.tasktool", float, 0.200),
    ("MAX_LOADER_BYTES", "mindhive.adaptivetail", int, 24500),
    ("MAX_LOADER_LINES", "mindhive.adaptivetail", int, 195),
    ("DREAMING_THRESHOLD", "mindhive.core.dreaming", float, 0.88),
    ("MIN_TAIL_BYTES", "mindhive.adaptivetail", int, 1500),
    ("DEFAULT_WEIGHT_MODEL", "mindhive.orchestrator.metrics", str, "log_linear"),
    ("WEIGHT_MIN", "mindhive.orchestrator.payoff", float, 1.0),
    ("WEIGHT_MAX", "mindhive.orchestrator.payoff", float, 50.0),
    ("GENESIS_PREV_HASH", "mindhive.events", str, "0" * 64),
]

# Where a constant is DEFINED when that is not the MEB's owner module (the
# owner then re-exports the very same object).
DEFINED_IN = {"GENESIS_PREV_HASH": "mindhive.core.models"}


def _defines(module, name) -> bool:
    """True when *module*'s own source binds *name* at top level by assignment."""
    tree = ast.parse(inspect.getsource(module))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else \
            [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return True
    return False


def _mismatch(module, name, typ, value, *, must_define=True):
    """None when *module* holds *name* as a *typ* equal to *value* (and, when
    *must_define*, binds it itself); otherwise a sentence saying what differs."""
    if not hasattr(module, name):
        return f"{module.__name__} has no {name}"
    got = getattr(module, name)
    if type(got) is not typ:
        return f"{module.__name__}.{name} is {type(got).__name__}, the MEB says {typ.__name__}"
    if got != value:
        return f"{module.__name__}.{name} = {got!r}, the MEB says {value!r}"
    if must_define and not _defines(module, name):
        return f"{module.__name__}.{name} is imported there, not defined"
    return None


def test_the_table_is_the_mebs_thirteen_rows():
    assert len(MEB_10_1) == 13
    assert len({row[0] for row in MEB_10_1}) == 13


@pytest.mark.parametrize("name, owner, typ, value", MEB_10_1, ids=[r[0] for r in MEB_10_1])
def test_each_constant_has_the_mebs_owner_type_and_value(name, owner, typ, value):
    home = DEFINED_IN.get(name, owner)
    assert _mismatch(importlib.import_module(home), name, typ, value) is None
    if home != owner:
        reexported = importlib.import_module(owner)
        assert _mismatch(reexported, name, typ, value, must_define=False) is None
        assert getattr(reexported, name) is getattr(importlib.import_module(home), name)


def test_the_constants_are_the_ones_the_code_runs_on():
    from mindhive import adaptivetail, tasktool
    from mindhive.core import dreaming
    from mindhive.orchestrator import metrics, payoff
    timeout = inspect.signature(tasktool.task_lock).parameters["timeout_s"].default
    assert timeout == tasktool._LOCK_TIMEOUT_SECS
    assert 0 < tasktool._LOCK_RETRY_BASE_MS < tasktool._LOCK_RETRY_MAX_MS
    assert adaptivetail.DREAMING_THRESHOLD is dreaming.DREAMING_THRESHOLD
    assert 0 < adaptivetail.MIN_TAIL_BYTES < adaptivetail.MAX_LOADER_BYTES < 25000
    assert adaptivetail.MAX_LOADER_LINES < 200
    assert metrics.DEFAULT_WEIGHT_MODEL in metrics.WEIGHT_MODELS
    assert payoff.WEIGHT_MIN < payoff.WEIGHT_MAX


def test_the_genesis_hash_is_sixty_four_zero_hex_digits():
    from mindhive import events
    assert events.GENESIS_PREV_HASH == "0" * 64
    assert len(events.GENESIS_PREV_HASH) == 64 and set(events.GENESIS_PREV_HASH) == {"0"}


def test_canary_the_checker_reports_every_kind_of_drift(tmp_path, monkeypatch):
    source = tmp_path / "fake_consts.py"
    source.write_text("from os import sep as IMPORTED\nGOOD = 15.0\nINT_AS_FLOAT = 20.0\n",
                      encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    fake = importlib.import_module("fake_consts")
    assert _mismatch(fake, "GOOD", float, 15.0) is None
    assert "the MEB says 16.0" in _mismatch(fake, "GOOD", float, 16.0)
    assert "is float, the MEB says int" in _mismatch(fake, "INT_AS_FLOAT", int, 20)
    assert "has no ABSENT" in _mismatch(fake, "ABSENT", int, 1)
    import os
    assert "imported there, not defined" in _mismatch(fake, "IMPORTED", str, os.sep)
    assert _mismatch(fake, "IMPORTED", str, os.sep, must_define=False) is None


def test_canary_a_bool_is_not_accepted_as_an_int():
    fake = types.ModuleType("fake_bool")
    fake.SPINS = True
    assert "is bool, the MEB says int" in _mismatch(fake, "SPINS", int, 1, must_define=False)
