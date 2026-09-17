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

from mindhive import events
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
    assert core_models.__all__ == ["GENESIS_PREV_HASH", "compute_chain_hash", "EventRecord"]


def test_core_models_is_a_stdlib_leaf():
    loaded = _child(
        "import sys, mindhive.core.models; "
        "mods = sorted(m for m in sys.modules if m.startswith('mindhive')); "
        "heavy = [m for m in ('chromadb', 'psycopg', 'numpy', 'http.server') if m in sys.modules]; "
        "print(mods, heavy)")
    assert loaded == "['mindhive', 'mindhive.core', 'mindhive.core.models'] []"


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


def test_core_models_does_not_import_events():
    tree = ast.parse((REPO / "src/mindhive/core/models.py").read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            imported.add(n.module or "")
    assert imported == {"hashlib", "dataclasses"}


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
