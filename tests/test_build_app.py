# tests/test_build_app.py — blueprint P5 (d2): the frozen build carries the package's data.
"""`build/build_app.py` has to ask PyInstaller for the files the package READS.

PyInstaller follows imports: it bundles every module the entry point reaches and
nothing the code merely opens. `src/mindhive` holds two such files:

* `static/dashboard.js`, which `dashboard_page` reads through
  `importlib.resources` while it is being imported -- without it the frozen
  dashboard cannot import its own page module;
* `pgmigrations/*.sql`, which `pgmirror` applies -- without them the mirror
  finds zero migrations and reports success (`tests/test_pg_packaging.py` tells
  that story for wheels; `package-data` never reaches PyInstaller).

`--collect-data mindhive` asks for every data file under the package, both of
those included. These tests read the argv the script BUILDS: it is imported by
path and its `subprocess` seam replaced, so PyInstaller (the optional `[build]`
extra) never runs.
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "build" / "build_app.py"


def _collected(argv) -> list:
    """The packages *argv* hands to `--collect-data`, in either spelling
    PyInstaller accepts: `--collect-data X` and `--collect-data=X`."""
    out = []
    for i, arg in enumerate(argv):
        if arg == "--collect-data" and i + 1 < len(argv):
            out.append(argv[i + 1])
        elif arg.startswith("--collect-data="):
            out.append(arg.split("=", 1)[1])
    return out


def _build(monkeypatch, returncode=0):
    """Run the script's `main()` with PyInstaller swapped out. Returns
    (main's result, [(argv, cwd)] for every command it would have run)."""
    spec = importlib.util.spec_from_file_location("build_app_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def call(args, cwd=None):
        calls.append((list(args), cwd))
        return returncode

    monkeypatch.setattr(module, "subprocess", SimpleNamespace(call=call))
    return module.main(), calls


def test_the_build_asks_pyinstaller_for_every_data_file_in_the_package(monkeypatch):
    _code, calls = _build(monkeypatch)
    (argv, _cwd), = calls
    assert "mindhive" in _collected(argv), argv


def test_the_flag_reader_would_catch_a_build_that_collects_nothing():
    """CANARY for the test above: the argv the script built before this change,
    which shipped no data file, reads as collecting nothing -- and both
    spellings of the flag are read, so the test cannot pass on a form it
    failed to parse."""
    before = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
              "--onefile", "--name", "MindHive", "--paths", "src", "app_entry.py"]
    assert _collected(before) == []
    assert _collected(before[:-1] + ["--collect-data", "mindhive", "app_entry.py"]) \
        == ["mindhive"]
    assert _collected(before[:-1] + ["--collect-data=mindhive.pgmigrations",
                                     "app_entry.py"]) == ["mindhive.pgmigrations"]


def test_the_build_is_still_one_onefile_pyinstaller_run_of_the_entry_point(monkeypatch):
    """REGRESSION PIN: the flag joined the build; it did not become a second
    one, and the script still hands back PyInstaller's exit code."""
    code, calls = _build(monkeypatch, returncode=3)
    assert code == 3
    (argv, cwd), = calls
    assert argv[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "--onefile" in argv
    assert argv[argv.index("--name") + 1] == "MindHive"
    assert argv[argv.index("--paths") + 1] == str(REPO / "src")
    assert argv[-1] == str(REPO / "build" / "app_entry.py")
    assert cwd == str(REPO)
