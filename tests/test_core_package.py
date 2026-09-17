# tests/test_core_package.py
"""Blueprint P0b carve-out B (decision K6): `EventRecord`, `GENESIS_PREV_HASH`
and `compute_chain_hash` are DEFINED in `mindhive.core.models` and re-exported
by `events` by identity. The `core` package is a stdlib leaf whose `__init__`
imports nothing, because `core.models` sits on every hook's append path.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import events, models as bab3
from mindhive.core import models as core_models

REPO = Path(__file__).resolve().parents[1]


def _child(code: str) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run([sys.executable, "-W", "error", "-c", code],
                         capture_output=True, text=True, env=env, cwd=str(REPO))
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_the_three_primitives_are_defined_in_core_models_and_reexported():
    assert events.EventRecord is core_models.EventRecord
    assert events.EventRecord.__module__ == "mindhive.core.models"
    assert events.compute_chain_hash is core_models.compute_chain_hash
    assert events.compute_chain_hash.__module__ == "mindhive.core.models"
    assert events.GENESIS_PREV_HASH == core_models.GENESIS_PREV_HASH == "0" * 64
    assert core_models.__all__[:6] == ["GENESIS_PREV_HASH", "compute_chain_hash", "EventRecord",
                                       "CircuitBreakerState", "PoissonMetrics", "BreakerVerdict"]
    assert core_models.__all__[6:] == list(bab3.__all__)      # P1j: BAB 3 re-exported


def test_core_models_is_a_stdlib_leaf():
    loaded = _child(
        "import sys, mindhive.core.models; "
        "mods = sorted(m for m in sys.modules if m.startswith('mindhive')); "
        "heavy = [m for m in ('chromadb', 'psycopg', 'numpy', 'http.server') if m in sys.modules]; "
        "print(mods, heavy)")
    assert loaded == "['mindhive', 'mindhive.core', 'mindhive.core.models', 'mindhive.models'] []"


def test_the_core_package_init_imports_nothing():
    loaded = _child("import sys, mindhive.core; "
                    "print(sorted(m for m in sys.modules if m.startswith('mindhive')))")
    assert loaded == "['mindhive', 'mindhive.core']"
    tree = ast.parse((REPO / "src/mindhive/core/__init__.py").read_text(encoding="utf-8"))
    assert not [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]


def test_events_imports_only_core_models_from_the_core_package():
    """`events` must never pull `core.locks` or `core.dreaming` onto the hook
    path; the one allowed edge is the re-export of the three primitives."""
    tree = ast.parse((REPO / "src/mindhive/events.py").read_text(encoding="utf-8"))
    core_edges = [(n.module, sorted(a.name for a in n.names))
                  for n in ast.walk(tree)
                  if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("core")]
    assert core_edges == [("core.models", ["EventRecord", "GENESIS_PREV_HASH", "compute_chain_hash"])]
    src = (REPO / "src/mindhive/events.py").read_text(encoding="utf-8")
    assert "class EventRecord" not in src
    assert "def compute_chain_hash" not in src
    assert 'GENESIS_PREV_HASH = "0" * 64' not in src


def test_core_models_imports_the_stdlib_and_the_bab3_leaf_only():
    """`hashlib`, `math`, `dataclasses` and `..models` (a stdlib leaf itself):
    never `events`, never a filesystem."""
    tree = ast.parse((REPO / "src/mindhive/core/models.py").read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            imported.add("." * n.level + (n.module or ""))
    assert imported == {"hashlib", "math", "dataclasses", "..models", ".."}


def test_the_chain_formula_did_not_move_by_a_byte():
    """Carved out verbatim: the digest over the genesis hash and an empty
    object is the number `chain.py` has always produced for that row."""
    import hashlib
    expected = hashlib.sha256(("0" * 64 + "{}").encode("utf-8")).hexdigest()
    assert core_models.compute_chain_hash(core_models.GENESIS_PREV_HASH, "{}") == expected
    rec = core_models.EventRecord(event_id="s:1", session_id="s", seg_line=1,
                                  timestamp="2026-01-01T00:00:00Z", event_type="decision",
                                  payload={}, tags=[], prev_hash="0" * 64, chain_hash=expected)
    assert rec.seg_line == 1 and rec.event_id == "s:1"


# --- P1j: the MEB domain shapes and the package as a whole ---------------------------

def test_every_bab3_name_is_reexported_by_identity():
    assert len(bab3.__all__) >= 50
    for name in bab3.__all__:
        assert getattr(core_models, name) is getattr(bab3, name), name


def test_circuit_breaker_state_is_frozen_validated_and_round_trips_as_json():
    import json
    s = core_models.CircuitBreakerState()
    assert (s.state, s.failure_count, s.last_trip_ts, s.trips_in_window, s.quarantine_reason) == \
        ("CLOSED", 0, None, (), None)
    with pytest.raises(Exception):
        s.failure_count = 1                                    # frozen
    q = core_models.CircuitBreakerState(state="QUARANTINED", failure_count=3,
                                        last_trip_ts="2026-09-17T03:00:00Z",
                                        trips_in_window=["2026-09-17T02:00:00Z", "2026-09-17T03:00:00Z"],
                                        quarantine_reason="trip berulang")
    assert q.trips_in_window == ("2026-09-17T02:00:00Z", "2026-09-17T03:00:00Z")   # tuple, hashable
    assert hash(q) == hash(core_models.CircuitBreakerState.from_dict(q.as_dict()))
    assert core_models.CircuitBreakerState.from_dict(json.loads(json.dumps(q.as_dict()))) == q
    assert q.as_dict()["trips_in_window"] == list(q.trips_in_window)
    for bad in ({"state": "BROKEN"}, {"failure_count": -1}, {"failure_count": True},
                {"last_trip_ts": 5}, {"trips_in_window": "2026"}, {"trips_in_window": [1]},
                {"quarantine_reason": "x"},                    # reason without QUARANTINED
                {"state": "QUARANTINED"}):                     # QUARANTINED without reason
        with pytest.raises(ValueError):
            core_models.CircuitBreakerState(**bad)
    with pytest.raises(ValueError, match="kunci tidak dikenal"):
        core_models.CircuitBreakerState.from_dict({"state": "CLOSED", "extra": 1})
    with pytest.raises(ValueError):
        core_models.CircuitBreakerState.from_dict(["not", "a", "dict"])


def test_poisson_metrics_and_breaker_verdict_hold_their_invariants():
    m = core_models.PoissonMetrics(lambda_rate=0.05, mtbf=20.0, reliability_100=0.0067)
    assert (m.lambda_rate, m.mtbf, m.reliability_100) == (0.05, 20.0, 0.0067)
    assert core_models.PoissonMetrics(0.0, float("inf"), 1.0).mtbf == float("inf")
    for bad in ((-0.1, 10.0, 0.5), (float("nan"), 10.0, 0.5), (0.1, 0.0, 0.5), (0.1, 10.0, 1.5),
                (0.1, 10.0, -0.1), (True, 10.0, 0.5)):
        with pytest.raises(ValueError):
            core_models.PoissonMetrics(*bad)
    v = core_models.BreakerVerdict(allowed=False, state="OPEN", reason="cooldown 300 s")
    assert v == core_models.BreakerVerdict(False, "OPEN", "cooldown 300 s")
    for bad in ((1, "OPEN", "x"), (True, "BROKEN", "x"), (True, "OPEN", ""), (True, "OPEN", None)):
        with pytest.raises(ValueError):
            core_models.BreakerVerdict(*bad)


def test_the_whole_core_package_imports_cold_without_the_ledger_or_a_heavy_import():
    loaded = _child(
        "import sys, mindhive.core.models, mindhive.core.locks, mindhive.core.dreaming, "
        "mindhive.core.recommend; "
        "mods = sorted(m for m in sys.modules if m.startswith('mindhive')); "
        "heavy = [m for m in ('chromadb', 'psycopg', 'numpy', 'http.server') if m in sys.modules]; "
        "print(mods, heavy)")
    assert loaded == ("['mindhive', 'mindhive.core', 'mindhive.core.dreaming', "
                      "'mindhive.core.locks', 'mindhive.core.models', 'mindhive.core.recommend', "
                      "'mindhive.models'] []")
