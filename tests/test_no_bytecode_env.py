# tests/test_no_bytecode_env.py
"""``PYTHONDONTWRITEBYTECODE=1`` in every environment this package builds for a
child (MEB operational risk 7).

A Python child that writes `__pycache__` into a lane, or into the checkout a
suite run is judging, leaves litter the merge-back reads as work. One leaf
(`paths.no_bytecode_env`) sets the default and every spawner asks it, so the
tests here are one per spawner plus a canary that runs a real interpreter.
The operator's own value always wins: this is a default, never a switch."""
import os
import subprocess
import sys

import pytest

from mindhive import claude_cli, paths, selftest, verify
from mindhive.mindnet import executor
from mindhive.orchestrator import driver, resilience
from mindhive.rotation import supervisor

NAME = "PYTHONDONTWRITEBYTECODE"


@pytest.fixture(autouse=True)
def unset(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)


def test_the_leaf_sets_a_default_and_never_overrides():
    assert paths.NO_BYTECODE_ENV == NAME == driver.NO_BYTECODE_ENV
    assert driver.no_bytecode_env is paths.no_bytecode_env
    assert paths.no_bytecode_env()[NAME] == "1"
    assert paths.no_bytecode_env({"A": "b"}) == {"A": "b", NAME: "1"}
    for chosen in ("0", "", "x"):
        assert paths.no_bytecode_env({NAME: chosen})[NAME] == chosen
    given = {"A": "b"}
    assert paths.no_bytecode_env(given) is not given and given == {"A": "b"}


def test_the_name_is_matched_the_way_the_platform_matches_it():
    """Windows folds environment names, so a second spelling would be a duplicate
    key in the block handed to CreateProcess. POSIX names are case-sensitive."""
    merged = paths.no_bytecode_env({"PythonDontWriteBytecode": "0"})
    if os.name == "nt":
        assert merged == {"PythonDontWriteBytecode": "0"}
    else:
        assert merged == {"PythonDontWriteBytecode": "0", NAME: "1"}


def test_the_inherited_environment_wins_when_the_operator_set_it(monkeypatch):
    monkeypatch.setenv(NAME, "0")
    assert paths.no_bytecode_env()[NAME] == "0"
    assert claude_cli._child_env(None)[NAME] == "0"
    assert supervisor._child_env()[NAME] == "0"


# --- one per spawner ---------------------------------------------------------------------------

def test_selftest_builds_it_into_the_checkouts_child(tmp_path):
    (tmp_path / "src").mkdir()
    env = selftest.child_env(tmp_path)
    assert env[NAME] == "1" and env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path / "src")
    assert selftest.child_env(tmp_path / "wheel") is None      # `_stream` covers this one


def _popen_env(monkeypatch, module):
    seen = {}
    real = subprocess.Popen

    def spy(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return real(argv, **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", spy)
    return seen


def test_verify_stream_covers_an_inherited_environment_too(monkeypatch):
    seen = _popen_env(monkeypatch, verify)
    code, out, _ = verify._stream(
        [sys.executable, "-c", "import sys; print(sys.dont_write_bytecode)"], echo=False)
    assert (code, out.strip()) == (0, "True")
    assert seen["env"][NAME] == "1" and seen["env"].get("PATH") == os.environ.get("PATH")
    verify._stream([sys.executable, "-c", "pass"], echo=False, env={**os.environ, NAME: "0"})
    assert seen["env"][NAME] == "0"


def test_the_sandboxed_runner_covers_fenced_and_unfenced_children(monkeypatch):
    seen = _popen_env(monkeypatch, resilience)
    got = resilience.run_sandboxed(
        [sys.executable, "-c", "import sys; print(sys.dont_write_bytecode)"], timeout_s=60)
    assert got.result == "ok" and got.stdout.strip() == "True"
    assert seen["env"][NAME] == "1"


def test_the_mindnet_executor_sets_it_without_changing_what_child_env_means(tmp_path):
    assert executor._child_env(None) is None                   # "inherit" is still None
    probe = tmp_path / "probe.py"
    probe.write_text("import sys\n"
                     "open(sys.argv[1], 'w').write(str(sys.dont_write_bytecode))\n",
                     encoding="utf-8")
    marker = tmp_path / "marker.txt"
    run = executor.execute("mn-t-nobytecode", f"python {probe} {marker}", root=tmp_path)
    assert run.result == "ok", run.reason
    assert marker.read_text(encoding="utf-8") == "True"
    assert not (tmp_path / "__pycache__").exists()


def test_the_headless_agent_and_the_rotation_successor_carry_it():
    headless = claude_cli._child_env({"X": "1"})
    assert headless[NAME] == "1" and headless["MINDHIVE_HEADLESS"] == "1"
    successor = supervisor._child_env({})
    assert successor[NAME] == "1" and successor["MINDHIVE_ROTATION"] == "1"
    # A default is not a switch: the rotation markers are exactly what they were.
    assert "MINDHIVE_HEADLESS" not in successor


def test_a_real_child_of_the_driver_writes_no_bytecode(tmp_path):
    (tmp_path / "mod_under_test.py").write_text("VALUE = 1\n", encoding="utf-8")
    done = driver.run_agent_process_isolated(
        [sys.executable, "-c", "import sys, mod_under_test; print(sys.dont_write_bytecode)"],
        cwd=tmp_path)
    assert done.stdout.strip() == "True", done.stderr
    assert not (tmp_path / "__pycache__").exists()
