# tests/test_orchestrator_judge.py
"""The judge a unit may not edit (blueprint P2, MEB K13) -- `orchestrator/judge.py`.

`tests/test_orchestrator_gate.py` drives the gate end to end through real
missions. This file pins the pieces it is built from, each against a real
directory and, where git is the baseline, a real repository:

* WHAT the judge is (`is_judge_path`) and which NEW files join it
  (`is_implicit_load`) -- a new `conftest.py` changes every test below it
  without touching one, a new test module does not;
* the one on-disk walk (`judge_files`) that both the snapshot and the
  read-only lock use, and which stays out of other checkouts;
* the pytest SECTION of a shared config file, so a dependency bump is not a
  test edit and an `addopts` change is;
* `tests_mutated` against a pinned commit, which a child that commits cannot
  move -- and ``None``, not ``()``, when git cannot say;
* the judge by import NAME as well as by path -- the interpreter's startup
  hooks in every spelling, plugin metadata on the import path, pytest 9's
  `pytest.toml`, and a new module that would shadow one the run imports --
  and `judge_changes`, the commit-to-commit question `SuiteGate.begin` asks
  before it trusts a later commit on a shared lane.
"""
import hashlib
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from mindhive.mindnet import worktree as mn_wt
from mindhive.orchestrator import gate as gate_mod
from mindhive.orchestrator import judge


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *[str(a) for a in args]],
                       capture_output=True, text=True, shell=False)
    assert r.returncode == 0, f"git {' '.join(map(str, args))}: {r.stderr}"
    return r


def _write(root, rel, text):
    target = Path(root) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return target


PYPROJECT = """
    [project]
    name = "pkg"
    version = "0.1.0"
    dependencies = []

    [tool.pytest.ini_options]
    markers = ["slow: a slow test"]
"""


@pytest.fixture
def judged(tmp_path):
    """A committed repository holding every KIND of judge file, beside work
    that is not the judge at all."""
    root = tmp_path / "repo"
    _write(root, "src/pkg/__init__.py", "")
    _write(root, "src/pkg/writer.py", "def write(x):\n    return 1\n")
    _write(root, "tests/test_writer.py", "def test_w():\n    assert True\n")
    _write(root, "tests/helpers.py", "EXPECTED = 1\n")
    _write(root, "tests/data/expected.json", '{"x": 1}\n')
    _write(root, "tests/conftest.py", "import pytest\n")
    _write(root, "conftest.py", "# the root plugin\n")
    _write(root, "pyproject.toml", PYPROJECT)
    _write(root, "setup.cfg", "[metadata]\nname = pkg\n\n[tool:pytest]\n"
                              "addopts = -q\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


# --- what the judge is ---------------------------------------------------------

@pytest.mark.parametrize("rel, judged_", [
    ("tests/test_a.py", True),
    ("tests/conftest.py", True),
    ("tests/helpers/util.py", True),
    ("tests/data/expected.json", True),
    ("tests\\sub\\test_b.py", True),
    ("Tests/test_case.py", True),
    ("conftest.py", True),
    ("CONFTEST.PY", True),
    ("src/pkg/conftest.py", True),
    ("pytest.ini", True),
    (".pytest.ini", True),
    ("src/sitecustomize.py", True),
    # judged by their pytest SECTION, in `tests_mutated`, not whole
    ("pyproject.toml", False),
    ("setup.cfg", False),
    ("tox.ini", False),
    # the work, not the judge
    ("src/pkg/writer.py", False),
    ("README.md", False),
    ("testsuite/test_x.py", False),
    ("src/pytest.ini", False),
    # caches, and directories pytest never enters
    ("tests/__pycache__/test_a.cpython-314.pyc", False),
    ("tests/test_a.pyc", False),
    (".venv/lib/site-packages/x/conftest.py", False),
    ("node_modules/p/conftest.py", False),
    ("build/lib/conftest.py", False),
    ("pkg.egg/conftest.py", False),
    ("", False),
    (None, False),
])
def test_is_judge_path(rel, judged_):
    assert judge.is_judge_path(rel) is judged_


@pytest.mark.parametrize("rel, implicit", [
    ("conftest.py", True),
    ("pytest.ini", True),
    (".pytest.ini", True),
    ("sitecustomize.py", True),
    ("tests/conftest.py", True),
    ("tests/sub/conftest.py", True),
    ("tests/__init__.py", True),
    ("tests/pytest.ini", True),
    ("tests/pyproject.toml", True),
    ("tests/tox.ini", True),
    ("tests/setup.cfg", True),
    ("src/pkg/conftest.py", True),
    ("src/usercustomize.py", True),
    # a new test, helper or fixture is the greenfield flow, not a judge change
    ("tests/test_new.py", False),
    ("tests/helpers.py", False),
    ("tests/data/new.json", False),
    # a new package in the WORK is work
    ("src/pkg/sub/__init__.py", False),
    ("src/pkg/new.py", False),
    # root shared config: judged by whether it brings a pytest section
    ("pyproject.toml", False),
    ("setup.cfg", False),
    (".venv/x/conftest.py", False),
    ("tests/__pycache__/conftest.cpython-314.pyc", False),
])
def test_is_implicit_load(rel, implicit):
    assert judge.is_implicit_load(rel) is implicit


def test_every_implicit_load_file_is_also_a_judge_file():
    """An addition the gate refuses must also be a file whose later EDIT it
    refuses -- otherwise a child could create it in one attempt and change it
    freely in the next."""
    for rel in ("conftest.py", "pytest.ini", ".pytest.ini", "sitecustomize.py",
                "pytest.toml", ".pytest.toml",
                "tests/conftest.py", "tests/__init__.py", "tests/pytest.ini",
                "tests/pyproject.toml", "tests/pytest.toml", "tests/.pytest.toml",
                "src/pkg/conftest.py", "src/usercustomize.py",
                "src/sitecustomize/__init__.py", "src/sitecustomize.pyc",
                "src/zz-0.0.dist-info/entry_points.txt"):
        assert judge.is_implicit_load(rel) and judge.is_judge_path(rel), rel


# --- the one walk ---------------------------------------------------------------

def test_judge_files_lists_exactly_the_judge_on_disk(tmp_path):
    for rel in ("tests/test_a.py", "tests/sub/test_b.py", "tests/conftest.py",
                "tests/data/x.json", "conftest.py", "pytest.ini",
                "src/pkg/conftest.py", "src/sitecustomize.py",
                # not the judge
                "src/pkg/mod.py", "pyproject.toml", "README.md",
                "tests/__pycache__/test_a.cpython-314.pyc",
                ".venv/lib/conftest.py", "node_modules/x/conftest.py",
                "build/conftest.py", ".git/conftest.py"):
        _write(tmp_path, rel, "x = 1\n")

    got = judge.judge_files(tmp_path)
    assert got == ("conftest.py", "pytest.ini", "src/pkg/conftest.py",
                   "src/sitecustomize.py", "tests/conftest.py",
                   "tests/data/x.json", "tests/sub/test_b.py",
                   "tests/test_a.py")
    assert all(judge.is_judge_path(rel) for rel in got)


def test_the_walk_never_enters_another_checkout(tmp_path):
    """The main checkout holds every lane under `.worktrees/` and every
    session's tree under `.claude/worktrees/`. A walk rooted there must not
    hash -- or, for the lock, chmod -- files that belong to one of them."""
    _write(tmp_path, "tests/test_mine.py", "x = 1\n")
    _write(tmp_path, ".worktrees/lane-0/tests/test_theirs.py", "x = 1\n")
    _write(tmp_path, ".claude/worktrees/other/conftest.py", "x = 1\n")
    assert judge.judge_files(tmp_path) == ("tests/test_mine.py",)


def test_judge_files_is_total_over_a_path_that_is_not_a_directory(tmp_path):
    assert judge.judge_files(tmp_path / "nowhere") == ()
    assert judge.judge_files("") == ()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert judge.judge_files(tmp_path / "file.txt") == ()


# --- the pytest section of a shared config file ----------------------------------

def test_two_spellings_of_one_pytest_section_compare_equal():
    a = '[tool.pytest.ini_options]\naddopts = "-q"\n'
    b = "[tool.pytest.ini_options]\naddopts = '-q'   # quiet\n\n[project]\nx=1\n"
    assert judge.pytest_section("pyproject.toml", a) == \
        judge.pytest_section("pyproject.toml", b)
    assert judge.pytest_section("pyproject.toml", a.encode("utf-8")) == \
        judge.pytest_section("pyproject.toml", a)


def test_an_absent_section_and_an_unparseable_file_are_different_answers():
    assert judge.pytest_section("pyproject.toml", "[project]\nname='x'\n") == "{}"
    assert judge.pytest_section("pyproject.toml", b"") == "{}"
    assert judge.pytest_section("pyproject.toml", "[[[ not toml") is None
    assert judge.pytest_section("setup.cfg", "no section header at all") is None


def test_ini_style_sections_are_read_without_interpolation():
    """`%` is common in pytest options (`-k "not 100%"`); a parser that
    interpolated would raise on it and turn a valid config into a refusal."""
    cfg = '[tool:pytest]\naddopts = -k "not 100%"\n'
    assert "100%" in judge.pytest_section("setup.cfg", cfg)
    tox = "[tox]\nenvlist = py3\n\n[pytest]\naddopts = -q\n"
    assert judge.pytest_section("tox.ini", tox) == '{"pytest": {"addopts": "-q"}}'


# --- the explicit snapshot --------------------------------------------------------

def test_the_snapshot_hashes_every_judge_file_it_holds(tmp_path):
    _write(tmp_path, "tests/test_a.py", "x = 1\n")
    _write(tmp_path, "tests/sub/test_b.py", "y = 2\n")
    _write(tmp_path, "tests/data.json", "{}\n")
    _write(tmp_path, "conftest.py", "z = 0\n")
    _write(tmp_path, "src/pkg/mod.py", "z = 3\n")
    _write(tmp_path, "pyproject.toml", PYPROJECT)

    snap = judge.snapshot_tests(tmp_path)
    assert sorted(snap) == ["conftest.py", "pyproject.toml", "tests/data.json",
                            "tests/sub/test_b.py", "tests/test_a.py"]
    # The bytes on disk, not the string that was written: `_write` is text
    # mode, so on Windows every `\n` is a `\r\n` by the time it lands.
    on_disk = (tmp_path / "tests" / "test_a.py").read_bytes()
    assert snap["tests/test_a.py"] == hashlib.sha256(on_disk).hexdigest()
    # The shared config file is hashed by its pytest section alone.
    section = judge.pytest_section("pyproject.toml",
                                   (tmp_path / "pyproject.toml").read_bytes())
    assert snap["pyproject.toml"] == hashlib.sha256(
        section.encode("utf-8")).hexdigest()


def test_a_snapshot_names_what_changed_vanished_or_was_planted(tmp_path):
    _write(tmp_path, "tests/test_a.py", "x = 1\n")
    _write(tmp_path, "tests/test_b.py", "y = 2\n")
    _write(tmp_path, "tests/test_c.py", "w = 4\n")
    _write(tmp_path, "setup.cfg", "[metadata]\nname = pkg\n")
    snap = judge.snapshot_tests(tmp_path)

    _write(tmp_path, "tests/test_a.py", "x = 99\n")
    (tmp_path / "tests" / "test_b.py").unlink()
    _write(tmp_path, "tests/test_new.py", "n = 5\n")          # greenfield: fine
    _write(tmp_path, "tests/conftest.py", "import pytest\n")  # planted: not
    _write(tmp_path, "pyproject.toml", PYPROJECT)             # brings a section
    (tmp_path / "setup.cfg").unlink()                         # had none: fine

    assert judge.tests_mutated(tmp_path, snap) == (
        "pyproject.toml", "tests/conftest.py", "tests/test_a.py",
        "tests/test_b.py")


def test_an_unreadable_tree_is_unknown_not_clean(tmp_path, monkeypatch):
    """`check` refuses on ``None``: "nothing I could see changed" is not
    "nothing changed". The snapshot of a missing tree is still just empty.

    The ceiling keeps git from walking up out of `tmp_path` into whatever
    repository the temp directory happens to sit inside."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert judge.snapshot_tests(tmp_path / "nope") == {}
    assert judge.tests_mutated(tmp_path / "nope") is None
    assert judge.tests_mutated(tmp_path / "nope", snapshot={}) is None
    assert judge.tests_mutated(tmp_path) is None, "not a repository"


def test_an_empty_path_is_not_the_working_directory(monkeypatch, tmp_path):
    """`Path("")` is `.`. Read that way, an empty lane path would hash -- and
    the lock would chmod -- whatever checkout the caller stands in."""
    _write(tmp_path, "tests/test_here.py", "x = 1\n")
    monkeypatch.chdir(tmp_path)
    assert judge.judge_files(".") == ("tests/test_here.py",), "canary"
    for empty in ("", None):
        assert judge.judge_files(empty) == ()
        assert judge.snapshot_tests(empty) == {}
        assert judge.tests_mutated(empty) is None
        assert judge.tests_mutated(empty, snapshot={"tests/x.py": "0"}) is None


# --- against git ------------------------------------------------------------------

def test_edits_to_every_kind_of_judge_file_are_reported(judged):
    _write(judged, "tests/conftest.py", "import pytest\n\nX = 1\n")
    _write(judged, "conftest.py", "# edited\n")
    _write(judged, "tests/helpers.py", "EXPECTED = 2\n")
    _write(judged, "tests/data/expected.json", '{"x": 2}\n')
    (judged / "tests" / "test_writer.py").unlink()
    _write(judged, "src/pkg/writer.py", "def write(x):\n    return 2\n")

    assert judge.tests_mutated(judged) == (
        "conftest.py", "tests/conftest.py", "tests/data/expected.json",
        "tests/helpers.py", "tests/test_writer.py")


def test_new_files_that_change_a_run_by_existing_are_reported(judged):
    for rel in ("tests/sub/conftest.py", "tests/__init__.py", "tests/pytest.ini",
                "src/sitecustomize.py", "pytest.ini"):
        _write(judged, rel, "x = 1\n")
    assert judge.tests_mutated(judged) == (
        "pytest.ini", "src/sitecustomize.py", "tests/__init__.py",
        "tests/pytest.ini", "tests/sub/conftest.py")


def test_new_tests_helpers_and_fixtures_are_the_greenfield_flow(judged):
    _write(judged, "tests/test_lonely.py", "def test_l():\n    assert True\n")
    _write(judged, "tests/data/lonely.json", "{}\n")
    _write(judged, "src/pkg/lonely.py", "x = 1\n")
    _write(judged, "src/pkg/sub/__init__.py", "")
    _git(judged, "add", "-A")                   # staged, as a refused commit leaves it
    assert judge.tests_mutated(judged) == ()


def test_pyproject_is_judged_by_its_pytest_section_only(judged):
    text = (judged / "pyproject.toml").read_text(encoding="utf-8")
    _write(judged, "pyproject.toml", text.replace('version = "0.1.0"',
                                                  'version = "0.2.0"'))
    assert judge.tests_mutated(judged) == (), "a version bump is not a test edit"

    _write(judged, "pyproject.toml", text + 'addopts = "--deselect '
                                            'tests/test_writer.py::test_w"\n')
    assert judge.tests_mutated(judged) == ("pyproject.toml",)


def test_a_pytest_section_is_judged_whether_edited_added_removed_or_broken(
        judged):
    _write(judged, "setup.cfg", "[metadata]\nname = renamed\n\n[tool:pytest]\n"
                                "addopts = -q\n")
    assert judge.tests_mutated(judged) == (), "another section moved"
    _write(judged, "setup.cfg", "[metadata]\nname = pkg\n")
    assert judge.tests_mutated(judged) == ("setup.cfg",), "the section went"
    _write(judged, "setup.cfg", "[metadata]\nname = pkg\n\n[tool:pytest]\n"
                                "addopts = -q\n")
    (judged / "pyproject.toml").unlink()
    assert judge.tests_mutated(judged) == ("pyproject.toml",), "deleted"
    _write(judged, "pyproject.toml", "[[[ this is not toml")
    assert judge.tests_mutated(judged) == ("pyproject.toml",), "unparseable"
    _write(judged, "pyproject.toml", PYPROJECT)
    _write(judged, "tox.ini", "[pytest]\naddopts = --deselect x\n")
    assert judge.tests_mutated(judged) == ("tox.ini",), "a NEW file with one"


def test_a_committed_edit_is_seen_against_the_pinned_commit(judged):
    """The case the pin exists for. The default baseline, `HEAD`, is the
    weaker mode for a caller with no pin, and a committing child moves it."""
    start = mn_wt.rev_of(judged, "HEAD")
    _write(judged, "tests/test_writer.py", "def test_w():\n    pass\n")
    _write(judged, "tests/conftest.py", "import pytest\n\nX = 2\n")
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "the child committed its edit")

    assert judge.tests_mutated(judged) == (), "HEAD moved with the edit"
    assert judge.tests_mutated(judged, base=start) == (
        "tests/conftest.py", "tests/test_writer.py")


def test_a_git_that_cannot_answer_mid_way_is_unknown(judged, monkeypatch):
    text = (judged / "pyproject.toml").read_text(encoding="utf-8")
    _write(judged, "pyproject.toml", text + 'addopts = "-x"\n')
    monkeypatch.setattr(mn_wt, "show_at", lambda *a, **k: None)
    assert judge.tests_mutated(judged) is None
    monkeypatch.setattr(mn_wt, "tracked_at", lambda *a, **k: None)
    assert judge.tests_mutated(judged) is None


def test_the_gate_re_exports_the_same_objects():
    """`gate` is where callers and the gate's own tests have always looked;
    the carve-out moved the code, not the names."""
    for name in judge.__all__:
        if name == "TESTS_DIR":
            continue
        assert getattr(gate_mod, name) is getattr(judge, name), name
    assert gate_mod._TESTS_DIR == judge.TESTS_DIR


# --- by import name, and pytest 9's own config files -----------------------

HOOKS_AND_METADATA = (
    "src/sitecustomize/__init__.py", "sitecustomize/__init__.py",
    "src/sitecustomize/__init__.pyw", "src/sitecustomize/__init__.pyc",
    "src/sitecustomize.pyw", "src/sitecustomize.pyc",
    "src/sitecustomize.cp314-win_amd64.pyd", "src/usercustomize/__init__.py",
    "src/zz-0.0.dist-info/entry_points.txt", "src/zz-0.0.dist-info/METADATA",
    "zz.egg-info/entry_points.txt",
)


@pytest.mark.parametrize("rel", HOOKS_AND_METADATA)
def test_startup_hooks_and_plugin_metadata_are_judged_in_every_spelling(rel):
    """The interpreter imports `sitecustomize` by NAME, in whatever form the
    import system finds first -- a package, a `.pyw`, a sourceless `.pyc`, an
    extension module -- and pytest loads every `pytest11` entry point a
    `*.dist-info` or `*.egg-info` on `sys.path` declares. The gate puts the
    lane's `src/` there. Matching one file name missed all of them."""
    assert judge.is_judge_path(rel), rel
    assert judge.is_implicit_load(rel), rel


@pytest.mark.parametrize("rel", [
    "src/sitecustomize/__pycache__/__init__.cpython-314.pyc",
    ".venv/lib/site-packages/sitecustomize/__init__.py",
    ".venv/lib/site-packages/x-1.0.dist-info/METADATA",
    "build/lib/sitecustomize.py",
    "src/pkg/customize.py",
    "src/pkg/mysitecustomize.py",
])
def test_lookalikes_and_directories_nothing_imports_from_are_not(rel):
    assert not judge.is_judge_path(rel), rel
    assert not judge.is_implicit_load(rel), rel


@pytest.mark.parametrize("rel, judged_, implicit", [
    ("pytest.toml", True, True),
    (".pytest.toml", True, True),
    ("PYTEST.TOML", True, True),
    ("tests/pytest.toml", True, True),
    ("tests/sub/.pytest.toml", True, True),
    ("src/pytest.toml", False, False),
])
def test_pytest_toml_is_a_config_file_like_pytest_ini(rel, judged_, implicit):
    """pytest 9 looks for `pytest.toml` and `.pytest.toml` BEFORE `pytest.ini`,
    and one is the whole configuration even when empty."""
    assert judge.is_judge_path(rel) is judged_
    assert judge.is_implicit_load(rel) is implicit


def test_every_config_file_pytest_reads_is_part_of_the_judge():
    """A canary on pytest itself, because the list above is static: parse the
    file names out of the installed pytest's own `locate_config`. A future
    pytest that reads one more name fails here, loudly, instead of handing a
    child an unwatched config file."""
    import inspect
    import re

    from _pytest.config import findpaths

    source = inspect.getsource(findpaths.locate_config)
    names = set(re.findall(r'"([.\w-]+\.(?:toml|ini|cfg))"', source))
    assert {"pytest.ini", "pyproject.toml", "setup.cfg"} <= names, (
        f"canary: the parse no longer finds pytest's names: {names}")
    for name in sorted(names):
        assert (name in judge.ROOT_JUDGE_FILES
                or name in judge.PYTEST_SECTIONS), name
        assert judge.is_implicit_load(f"{judge.TESTS_DIR}/{name}"), name


def test_judge_files_finds_hooks_metadata_and_toml_configs_on_disk(tmp_path):
    for rel in ("src/sitecustomize/__init__.py", "src/sitecustomize.pyc",
                "src/zz-0.0.dist-info/entry_points.txt", "pytest.toml",
                "src/sitecustomize/__pycache__/__init__.cpython-314.pyc",
                "src/pkg/mod.py"):
        _write(tmp_path, rel, "x = 1\n")
    assert judge.judge_files(tmp_path) == (
        "pytest.toml", "src/sitecustomize.pyc", "src/sitecustomize/__init__.py",
        "src/zz-0.0.dist-info/entry_points.txt")


@pytest.mark.parametrize("name, header", [("setup.cfg", "[tool:pytest]"),
                                          ("tox.ini", "[pytest]")])
@pytest.mark.parametrize("label, after", [
    ("a key in another case", "{header}\nFilterwarnings = error\n"),
    ("a key moved under DEFAULT",
     "[DEFAULT]\nfilterwarnings = error\n\n{header}\n"),
    ("a key behind a bare CR",
     "{header}\nfilterwarnings = error\n# c\raddopts = -p no:warnings\n"),
    ("a key behind a form feed",
     "{header}\nfilterwarnings = error\n# c\x0caddopts = -p no:warnings\n"),
])
def test_ini_sections_are_read_the_way_pytest_reads_them(name, header, label,
                                                         after):
    """pytest parses these with `iniconfig`: key case kept, `[DEFAULT]` an
    ordinary section, lines split wherever `str.splitlines` splits. Read with
    `configparser` -- lower-cased keys, DEFAULT merged in, a newline the only
    line break -- each edit below compared EQUAL to the original while pytest
    ran under a different configuration."""
    before = f"{header}\nfilterwarnings = error\n"
    assert (judge.pytest_section(name, before)
            != judge.pytest_section(name, after.format(header=header))), label


def test_an_empty_pytest_section_is_a_configuration_not_an_absent_one():
    """pytest takes a file holding an empty section as THE config -- a new
    root `tox.ini` with a bare `[pytest]` outranks `setup.cfg` -- so it must
    not read as "no section at all"."""
    for name, header in (("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]")):
        assert judge.pytest_section(name, "") == "{}"
        assert judge.pytest_section(name, header + "\n") != "{}", name
    assert (judge.pytest_section("pyproject.toml", "[tool.pytest.ini_options]\n")
            != "{}")


def test_line_endings_alone_are_not_a_change():
    text = "[tool:pytest]\naddopts = -q\n"
    assert (judge.pytest_section("setup.cfg", text.replace("\n", "\r\n"))
            == judge.pytest_section("setup.cfg", text))
    assert (judge.pytest_section("setup.cfg", text.encode("utf-8"))
            == judge.pytest_section("setup.cfg", text))


def test_bytes_pytest_could_not_decode_are_unreadable_not_replaced():
    """pytest opens these files as UTF-8 and stops on a byte it cannot decode.
    Replacing the byte would read a config pytest never ran under."""
    assert judge.pytest_section("setup.cfg",
                                b"[tool:pytest]\naddopts = -q \xff\n") is None
    assert judge.pytest_section(
        "pyproject.toml", b"[tool.pytest.ini_options]\naddopts = '\xff'\n") is None


def test_pytest_toml_is_judged_new_edited_or_deleted(judged):
    start = mn_wt.rev_of(judged, "HEAD")
    for rel in ("pytest.toml", ".pytest.toml", "tests/pytest.toml"):
        _write(judged, rel, '[pytest]\naddopts = ["-k", "not test_w"]\n')
    assert judge.tests_mutated(judged) == (
        ".pytest.toml", "pytest.toml", "tests/pytest.toml")
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "the configs, committed")
    assert judge.tests_mutated(judged, base=start) == (
        ".pytest.toml", "pytest.toml", "tests/pytest.toml")

    _write(judged, "pytest.toml", "[pytest]\n")
    (judged / ".pytest.toml").unlink()
    assert judge.tests_mutated(judged) == (".pytest.toml", "pytest.toml")


def test_startup_hooks_in_every_spelling_are_judged_new_or_edited(judged):
    for rel in ("src/sitecustomize/__init__.py", "src/usercustomize.pyw",
                "sitecustomize.pyc"):
        _write(judged, rel, "x = 1\n")
    assert judge.tests_mutated(judged) == (
        "sitecustomize.pyc", "src/sitecustomize/__init__.py",
        "src/usercustomize.pyw")
    _git(judged, "add", "-A")
    # a global excludes file commonly ignores `*.pyc`; this repo ships one
    _git(judged, "add", "-f", "--", "sitecustomize.pyc")
    _git(judged, "commit", "-qm", "hooks the repo really ships")
    assert judge.tests_mutated(judged) == ()
    _write(judged, "src/sitecustomize/__init__.py", "import pkg\n")
    assert judge.tests_mutated(judged) == ("src/sitecustomize/__init__.py",)


def test_new_plugin_metadata_on_the_import_path_is_judged(judged):
    _write(judged, "src/zz_helper-0.0.dist-info/METADATA",
           "Name: zz-helper\nVersion: 0.0\n")
    _write(judged, "src/zz_helper-0.0.dist-info/entry_points.txt",
           "[pytest11]\nzz = zz_helper\n")
    _write(judged, "src/zz_helper.py", "x = 1\n")
    assert judge.tests_mutated(judged) == (
        "src/zz_helper-0.0.dist-info/METADATA",
        "src/zz_helper-0.0.dist-info/entry_points.txt")


def test_a_new_module_that_shadows_what_the_run_imports_is_judged(judged):
    """`python -m pytest` in the lane puts the lane first on `sys.path` and
    its `src/` next -- ahead of the standard library and the installed pytest
    -- and pytest's default import mode puts a test file's own directory ahead
    of both. A NEW module there named like something the run imports replaces
    it. A new module with a name of its own is the greenfield flow, and so is
    a namespace directory, which never outranks a regular package."""
    for rel in ("src/pytest/__init__.py", "pytest.py", "src/json.py",
                "tests/pkg.py",
                # names of their own, and a namespace portion
                "src/newthing.py", "src/newpkg/__init__.py", "tests/helpers2.py",
                "src/pytest_x/__init__.py", "src/unittest/__main__.py"):
        _write(judged, rel, "x = 1\n")
    assert judge.tests_mutated(judged) == (
        "pytest.py", "src/json.py", "src/pytest/__init__.py", "tests/pkg.py")


def test_a_new_tox_ini_with_an_empty_section_is_a_new_configuration(judged):
    _write(judged, "tox.ini", "[pytest]\n")
    assert judge.tests_mutated(judged) == ("tox.ini",)


@pytest.mark.parametrize("label, text", [
    ("key case", "[metadata]\nname = pkg\n\n[tool:pytest]\nAddopts = -q\n"),
    ("DEFAULT", "[DEFAULT]\naddopts = -q\n\n[metadata]\nname = pkg\n\n"
                "[tool:pytest]\n"),
    ("bare CR", "[metadata]\nname = pkg\n\n[tool:pytest]\naddopts = -q\n"
                "# c\rpython_files = x.py\n"),
])
def test_setup_cfg_edits_pytest_reads_differently_are_reported(judged, label,
                                                              text):
    _write(judged, "setup.cfg", text)
    assert judge.tests_mutated(judged) == ("setup.cfg",), label


def test_a_committed_new_conftest_and_section_change_are_seen_from_the_pin(
        judged):
    """The pin exists because a child can commit; both halves of the check --
    what was HELD at the pin, and the section AS COMMITTED at the pin -- must
    read the pin and not `HEAD`."""
    start = mn_wt.rev_of(judged, "HEAD")
    _write(judged, "tests/sub/conftest.py", "import pytest\n")
    text = (judged / "pyproject.toml").read_text(encoding="utf-8")
    _write(judged, "pyproject.toml", text + 'addopts = "--deselect '
                                            'tests/test_writer.py::test_w"\n')
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "the child committed")
    assert judge.tests_mutated(judged) == (), "HEAD moved with both"
    assert judge.tests_mutated(judged, base=start) == (
        "pyproject.toml", "tests/sub/conftest.py")


def test_a_test_swapped_for_a_symlink_is_an_edit(judged, tmp_path):
    """git calls this a TYPE change, not a modification; `T` in the diff
    filter is the only thing that catches it."""
    outside = tmp_path / "elsewhere.py"
    outside.write_text("def test_w():\n    assert True\n", encoding="utf-8")
    target = judged / "tests" / "test_writer.py"
    target.unlink()
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform or user cannot create a symlink")
    status = _git(judged, "status", "--porcelain").stdout
    if " T tests/test_writer.py" not in status:
        pytest.skip(f"git here records no type change: {status!r}")
    assert judge.tests_mutated(judged) == ("tests/test_writer.py",)


@pytest.mark.parametrize("rel", ["tests/test_\x7fdel.py",
                                 "tests/test_nbsp.py\N{NO-BREAK SPACE}",
                                 "tests/test_ls\N{LINE SEPARATOR}x.py",
                                 "tests/test_nel\x85x.py"])
def test_names_git_would_quote_or_python_would_split_come_back_exact(judged,
                                                                     rel):
    """`core.quotepath=false` still C-quotes DEL; `str.splitlines` breaks at
    LINE SEPARATOR and NEL; `strip()` eats a trailing NO-BREAK SPACE. Each name
    below came back wrong -- or not at all -- before git was read with `-z`."""
    try:
        _write(judged, rel, "def test_x():\n    assert False\n")
    except OSError:
        pytest.skip(f"this filesystem refuses {rel!r}")
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "an odd name")
    _write(judged, rel, "def test_x():\n    assert True\n")
    assert judge.tests_mutated(judged) == (rel,)


def test_a_directory_inside_a_checkout_is_not_the_checkout(judged):
    """git asked in `src/` answers for the repository around it, so every
    path would be read against the wrong root -- and a lane whose `.git` link
    is gone answers for the MAIN checkout the same way. Unknown, not clean."""
    assert judge.tests_mutated(judged) == (), "canary"
    assert judge.tests_mutated(judged / "src") is None
    assert judge.judge_changes(judged / "src", "HEAD", "HEAD") is None


def test_judge_changes_compares_two_commits_and_nothing_else(judged):
    """What `SuiteGate.begin` asks before trusting a later commit on a lane:
    does the judge at *head* match the judge at *base*? A new test module is
    the greenfield flow; a doctored test, a new conftest or a moved pytest
    section is not -- and the working tree is no part of the question."""
    start = mn_wt.rev_of(judged, "HEAD")
    _write(judged, "tests/test_lonely.py", "def test_l():\n    assert True\n")
    _write(judged, "src/pkg/lonely.py", "x = 1\n")
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "greenfield")
    clean = mn_wt.rev_of(judged, "HEAD")
    _write(judged, "tests/test_writer.py", "uncommitted = True\n")
    assert judge.judge_changes(judged, start, clean) == ()

    _write(judged, "tests/sub/conftest.py", "import pytest\n")
    text = (judged / "setup.cfg").read_text(encoding="utf-8")
    _write(judged, "setup.cfg", text.replace("addopts = -q", "addopts = -x"))
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "doctored")
    doctored = mn_wt.rev_of(judged, "HEAD")
    expected = ("setup.cfg", "tests/sub/conftest.py", "tests/test_writer.py")
    assert judge.judge_changes(judged, clean, doctored) == expected
    assert judge.judge_changes(judged, start, doctored) == expected

    _git(judged, "checkout", start, "--", "tests/test_writer.py", "setup.cfg")
    _git(judged, "rm", "-q", "tests/sub/conftest.py")
    _git(judged, "commit", "-qm", "restored")
    restored = mn_wt.rev_of(judged, "HEAD")
    assert judge.judge_changes(judged, start, restored) == ()
    assert judge.judge_changes(judged, start, "no-such-rev") is None
    assert judge.judge_changes(judged, "--output=x", restored) is None


def test_a_snapshot_sees_what_the_git_reading_sees(tmp_path):
    _write(tmp_path, "src/pkg/__init__.py", "")
    _write(tmp_path, "tests/test_a.py", "x = 1\n")
    _write(tmp_path, "setup.cfg", "[metadata]\nname = pkg\n")
    snap = judge.snapshot_tests(tmp_path)

    _write(tmp_path, "src/sitecustomize/__init__.py", "x = 1\n")
    _write(tmp_path, "pytest.toml", "[pytest]\n")
    _write(tmp_path, "src/json.py", "x = 1\n")
    _write(tmp_path, "tests/pkg.py", "x = 1\n")
    _write(tmp_path, "tox.ini", "[pytest]\n")
    _write(tmp_path, "src/newthing.py", "x = 1\n")
    assert judge.tests_mutated(tmp_path, snap) == (
        "pytest.toml", "src/json.py", "src/sitecustomize/__init__.py",
        "tests/pkg.py", "tox.ini")


def test_an_untouched_config_pytest_cannot_parse_is_not_a_change(tmp_path):
    """Unreadable at the baseline and unreadable now is only "the same" when
    it is the same bytes."""
    _write(tmp_path, "setup.cfg", "no section header at all\n")
    snap = judge.snapshot_tests(tmp_path)
    assert judge.tests_mutated(tmp_path, snap) == ()
    _write(tmp_path, "setup.cfg", "still no header, but not the same\n")
    assert judge.tests_mutated(tmp_path, snap) == ("setup.cfg",)


# --- P2 round 2, review fixes: the walk, the pair, the unreadable section -------

def _link_dir(target, link):
    """A directory link at *link* naming *target*: a junction on Windows
    (`_winapi.CreateJunction` needs no privilege), a symlink elsewhere. Skips
    when the platform refuses either."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, ImportError, AttributeError, NotImplementedError) as exc:
        pytest.skip(f"cannot link a directory here: {exc}")


def test_the_walk_does_not_enter_a_directory_link(tmp_path):
    """`os.walk(followlinks=False)` stops at a symlink and walks straight into
    a Windows junction, which is how a lane's `tests/` could list -- and the
    lock could chmod -- files that belong to another tree."""
    lane = tmp_path / "lane"
    outside = tmp_path / "outside"
    _write(lane, "tests/test_a.py", "x = 1\n")
    _write(outside, "test_out.py", "x = 1\n")
    _write(outside, "conftest.py", "x = 1\n")
    _link_dir(outside, lane / "tests" / "linked")
    assert (lane / "tests" / "linked" / "test_out.py").is_file(), (
        "canary: the link resolves")

    assert judge.judge_files(lane) == ("tests/test_a.py",)
    assert "tests/linked/conftest.py" not in judge.snapshot_tests(lane)


def test_a_module_and_its_test_side_twin_committed_together_are_not_a_shadow(
        judged):
    """`src/twin.py` and `tests/twin.py` in ONE commit. At *head* the test-side
    copy stands earlier on the run's path than the src-side one, but nothing
    the run imported at *base* was replaced: the unit that committed the pair
    was judged against *base*, where neither existed, and passed. Read with
    the names *head* holds, `judge_changes` disagreed with that verdict, and
    the lane's baseline then never moved again -- every later unit on the
    lane answered for this pair."""
    start = mn_wt.rev_of(judged, "HEAD")
    _write(judged, "src/twin.py", "x = 1\n")
    _write(judged, "tests/twin.py", "x = 2\n")
    assert judge.tests_mutated(judged, base=start) == (), (
        "canary: the unit itself was not refused")
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "the pair")
    paired = mn_wt.rev_of(judged, "HEAD")
    assert judge.judge_changes(judged, start, paired) == ()

    # The shadow the base DID hold is still one: `src/pkg/` provides `pkg` at
    # the later entry, and a `tests/pkg.py` committed after replaces it.
    _write(judged, "tests/pkg.py", "x = 3\n")
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "the shadow")
    shadowed = mn_wt.rev_of(judged, "HEAD")
    assert judge.judge_changes(judged, paired, shadowed) == ("tests/pkg.py",)
    assert judge.judge_changes(judged, start, shadowed) == ("tests/pkg.py",)


def test_a_new_module_named_like_one_the_run_imports_from_outside_is_a_change(
        judged):
    """`judge_changes`' other half of the shadow rule: a root `pytest.py`
    replaces the installed pytest for every test in the lane."""
    before = mn_wt.rev_of(judged, "HEAD")
    _write(judged, "pytest.py", "x = 1\n")
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "a pytest of our own")
    after = mn_wt.rev_of(judged, "HEAD")
    assert judge.judge_changes(judged, before, after) == ("pytest.py",)


def test_a_new_test_side_module_that_outranks_one_under_src_is_a_shadow(judged):
    """The intra-lane rank rule alone, before the import system is asked: a
    `tests/helper.py` stands earlier on the run's path than a committed
    `src/helper.py`, so every test importing `helper` now gets the new one.
    A name of its own is welcome."""
    _write(judged, "src/helper.py", "x = 1\n")
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "a helper under src")
    assert judge.tests_mutated(judged) == (), "canary"
    _write(judged, "tests/helper.py", "x = 2\n")
    assert judge.tests_mutated(judged) == ("tests/helper.py",)
    _write(judged, "tests/newthing.py", "x = 3\n")
    assert judge.tests_mutated(judged) == ("tests/helper.py",)


def test_without_iniconfig_an_ini_section_is_unreadable_and_compared_by_bytes(
        judged, monkeypatch):
    """The core is stdlib-only at import and `iniconfig` is pytest's own
    dependency, reached lazily. Where it is missing an ini file's section
    reads as unreadable (``None``) and `_section_key` compares the bytes, so
    a non-pytest edit of `setup.cfg` is refused too -- the fail-closed side
    to land on. `pyproject.toml` still parses: `tomllib` is the standard
    library's. Pins a fallback that was there, undeclared."""
    import sys
    monkeypatch.setitem(sys.modules, "iniconfig", None)
    assert judge.pytest_section("tox.ini", b"[pytest]\naddopts = -q\n") is None
    assert judge.pytest_section(
        "pyproject.toml",
        b"[tool.pytest.ini_options]\naddopts = '-q'\n") is not None
    assert judge.tests_mutated(judged) == (), "the same bytes are the same"
    text = (judged / "setup.cfg").read_text(encoding="utf-8")
    _write(judged, "setup.cfg", text.replace("name = pkg", "name = renamed"))
    assert judge.tests_mutated(judged) == ("setup.cfg",)


def _unreadable(monkeypatch, name):
    """`Path.read_bytes` raising for one file NAME -- the shape of a permission
    error, an antivirus hold, or another process's exclusive share on Windows
    -- and reading every other path as before."""
    real = Path.read_bytes

    def fake(self):
        if self.name == name:
            raise PermissionError(13, "held by another process", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", fake)


def test_a_config_file_that_exists_but_cannot_be_read_is_unknown_not_absent(
        judged, monkeypatch):
    """`pyproject.toml` present, its pytest section unreadable NOW. Read as an
    absent file it compared equal to a baseline that had no section, and a
    child's `addopts = --deselect ...` went unreported: the one fail-open path
    in a module whose every "cannot tell" is ``None``. Unknown, not clean --
    `check` refuses on ``None``."""
    _write(judged, "pyproject.toml", '[project]\nname = "pkg"\n')
    _git(judged, "add", "-A")
    _git(judged, "commit", "-qm", "no pytest section at the baseline")
    _write(judged, "pyproject.toml", '[project]\nname = "pkg"\n\n'
                                     "[tool.pytest.ini_options]\naddopts = "
                                     '"--deselect tests/test_writer.py::test_w"\n')
    assert judge.tests_mutated(judged) == ("pyproject.toml",), "canary"
    _unreadable(monkeypatch, "pyproject.toml")
    assert judge.tests_mutated(judged) is None


def test_a_snapshot_leaves_out_a_config_file_it_could_not_read(judged,
                                                                monkeypatch):
    """Total the way `_file_digest` already is: an unreadable file is not in
    the map, never in it as "absent" -- and a snapshot compared against a tree
    where that file is present but unreadable answers ``None``."""
    good = judge.snapshot_tests(judged)
    assert "setup.cfg" in good, "canary"
    _unreadable(monkeypatch, "setup.cfg")
    assert "setup.cfg" not in judge.snapshot_tests(judged)
    assert judge.tests_mutated(judged, snapshot=good) is None
