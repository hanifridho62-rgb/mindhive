# tests/test_contracts_reexport.py
"""Blueprint P0b carve-out A: the registry payload contracts moved from
`events.py` to `contracts.py` VERBATIM, and `events` re-exports them by
identity. Three claims: same objects, a leaf module, and a registry whose
every row still resolves to an importable owner with a `validate_payload`.
"""
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import contracts, events

REPO = Path(__file__).resolve().parents[1]


def _child(code: str) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run([sys.executable, "-W", "error", "-c", code],
                         capture_output=True, text=True, env=env, cwd=str(REPO))
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_events_reexports_the_registry_by_identity():
    assert events._PAYLOAD_CONTRACTS is contracts._PAYLOAD_CONTRACTS
    assert events.validate_typed_payload is contracts.validate_typed_payload
    assert contracts.validate_typed_payload.__module__ == "mindhive.contracts"
    assert contracts.__all__ == ["validate_typed_payload"]


def test_contracts_is_a_leaf_of_the_package():
    """`import mindhive.contracts` loads the package `__init__` and itself,
    nothing else -- the owner modules are imported at the one append that
    needs them, never at import time."""
    loaded = _child("import sys, mindhive.contracts; "
                    "print(sorted(m for m in sys.modules if m.startswith('mindhive')))")
    assert loaded == "['mindhive', 'mindhive.contracts']"


def test_owners_are_resolved_relative_to_the_package_from_the_new_home():
    """`importlib.import_module(name, __package__)` must still see
    `__package__ == "mindhive"` from `contracts.py`, or every relative owner
    path in the table would fail on the first typed append."""
    assert contracts.__package__ == "mindhive"
    with pytest.raises(ValueError, match="hive append state_set"):
        contracts.validate_typed_payload("state_set", ["not", "an", "object"])
    with pytest.raises(ValueError, match="hive log"):
        contracts.validate_typed_payload("code_change", {})
    assert contracts.validate_typed_payload("free_text_memo", {"anything": 1}) is None


def test_every_registry_row_names_an_importable_owner_with_a_validator():
    assert len(contracts._PAYLOAD_CONTRACTS) >= 14
    for etype, (module_name, hint) in contracts._PAYLOAD_CONTRACTS.items():
        assert etype in events.VALID_TYPES, etype
        assert module_name.startswith("."), (etype, module_name)
        owner = importlib.import_module(module_name, "mindhive")
        assert callable(getattr(owner, "validate_payload")), (etype, module_name)
        assert isinstance(hint, str) and hint, etype


def test_the_registry_block_left_events_py_entirely():
    """One stub line remains -- the re-export -- and no table. A second copy
    of the dict in `events.py` would be the drift the carve-out exists to
    make impossible."""
    src = (REPO / "src/mindhive/events.py").read_text(encoding="utf-8")
    assert "_PAYLOAD_CONTRACTS = {" not in src
    assert "def validate_typed_payload" not in src
    assert "from .contracts import _PAYLOAD_CONTRACTS, validate_typed_payload" in src
    assert "import importlib" not in src
