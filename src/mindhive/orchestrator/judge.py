# src/mindhive/orchestrator/judge.py
# SPDX-License-Identifier: Apache-2.0
"""The judge a unit may not edit (blueprint P2, MEB K13 / risk #4).

A red test has two cures -- fix the code, or change what the test demands --
and the second is always available and always cheaper. So `gate.SuiteGate.check`
refuses, before a single test runs, a unit that changed the JUDGE. This module
says what the judge is and whether a lane changed it; the gate decides what to
do about that, and `worktree.lock_tests_readonly` locks the same files as a
deterrent.

## What the judge is

Any file under `tests/`; any `conftest.py` pytest would load; the interpreter's
startup hooks `sitecustomize`/`usercustomize` in EVERY spelling the import
system accepts -- a module, a package, a `.pyw`, a sourceless `.pyc`, an
extension module -- and the `*.dist-info`/`*.egg-info` metadata on the import
path whose `pytest11` entry points pytest loads; the root pytest config files
(`pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`); and the pytest
section -- that section only -- of `pyproject.toml`, `setup.cfg` and `tox.ini`.

A NEW file counts when merely existing changes a run: a `conftest.py`, a
`pytest.ini`, a package marker under `tests/` -- and a module whose NAME is
one the run already imports. `python -m pytest` in the lane puts the lane
first on `sys.path` and the gate puts its `src/` next, ahead of the standard
library and the installed pytest, and pytest's default import mode puts a test
file's own directory ahead of both; a new `src/json.py`, `pytest.py` or
`tests/pkg.py` replaces what every test imports by that name. A new test
module with a name of its own does not count, because writing one is exactly
what the greenfield refusal asks a child to do.

## Against what

The commit the lane stood at before the unit's FIRST attempt, which
`gate.SuiteGate.begin` pins and `gate.gated` asks it to pin ahead of every
attempt. Not the lane's `HEAD` when the gate runs: a child told not to commit
still can, and committing its edit makes `HEAD` agree with the edit. Not a
snapshot taken at the first gate run either, which is taken after the first
attempt already ran. `snapshot_tests` remains for a caller that can take one
before any child does. And on a lane that ran an EARLIER unit, `HEAD` may
already hold what that unit committed under a refused attempt -- rollback
keeps commits -- so `judge_changes` compares the judge at two commits, and
`begin` trusts a later commit only when nothing of the judge changed since
the last one it trusted.

## Read at the top level, the way pytest reads it

git asked inside a checkout answers for the tree around it, and a lane whose
`.git` link is gone is a directory inside the MAIN checkout. Both git readers
ask `mindnet.worktree.is_toplevel` first and answer "unknown" otherwise, and
the on-disk walk stops at a symlink or a Windows junction, which `os.walk`
would enter as a plain directory. Names come back as git holds them (`-z`).
A shared config file's section is read with pytest's own parser -- `tomllib`
for TOML; `iniconfig` for the ini files: key case kept, `[DEFAULT]` an
ordinary section, lines split wherever `str.splitlines` splits -- and decoded
strictly, because a byte pytest cannot decode is a config pytest never ran
under. A file that is there but cannot be read NOW is unknown, not absent:
the readers answer ``None`` for it, and `gate.SuiteGate.check` refuses.

## What this does not catch

A child that hides an edit from git itself (index flags, a clean filter,
replace refs), writes outside its lane, edits a module a `conftest.py` names in
`pytest_plugins`, or writes a new test module whose import-time code patches
what an existing test calls -- the targeted run collects the unit's own tests
beside the rest. That is deliberate concealment, not the repair-loop shortcut
this closes. The read-only lock is the deterrent beside it, and an operator's
review is the line behind both.

Stdlib only at import; git is reached through `mindnet.worktree`, lazily, and
`iniconfig` -- pytest's own dependency, not this package's, which imports
clean on any Python -- only when an ini section is read. Where it is missing
the section reads as unreadable and `_section_key` compares the bytes
instead: a wider net, still fail-closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

__all__ = [
    "IMPLICIT_LOAD_NAMES", "MODULE_SUFFIXES", "PYTEST_SECTIONS",
    "ROOT_JUDGE_FILES", "STARTUP_HOOKS", "TESTS_DIR",
    "is_implicit_load", "is_judge_path", "judge_changes", "judge_files",
    "pytest_section", "snapshot_tests", "tests_mutated",
]

#: Where a source tree keeps its suite -- the directory `gate` targets, and
#: every file under which is part of the judge.
TESTS_DIR = "tests"
_TESTS_DIR = TESTS_DIR


#: Root-level files pytest loads as a plugin or reads WHOLE as its config.
#: Editing one changes what the suite runs without touching a test file.
#: pytest 9 looks for `pytest.toml` and `.pytest.toml` BEFORE `pytest.ini`,
#: and one is the whole configuration even when empty; the judge's tests parse
#: the installed pytest's own `locate_config` for the list, so a future name
#: fails loudly rather than handing a child an unwatched config file.
ROOT_JUDGE_FILES = ("conftest.py", "pytest.toml", ".pytest.toml", "pytest.ini",
                    ".pytest.ini")

#: Root-level files shared with everything else, which judge only through
#: their pytest section. A dependency bump in `pyproject.toml` is not a test
#: edit; `addopts = "--deselect tests/test_x.py::test_the_red_one"` is one.
PYTEST_SECTIONS = {"pyproject.toml": "tool.pytest", "setup.cfg": "tool:pytest",
                   "tox.ini": "pytest"}

#: Files a run loads by NAME and PLACE, with no test importing them: a
#: `conftest.py` is a pytest plugin for every test below it, and the
#: interpreter imports `sitecustomize`/`usercustomize` from `sys.path` at
#: startup -- and the gate puts the lane's `src/` on it.
IMPLICIT_LOAD_NAMES = ("conftest.py", "sitecustomize.py", "usercustomize.py")

#: The startup hooks by the NAME the interpreter imports them under, in
#: whatever form the import system finds first -- `_by_name` matches every
#: form, where `IMPLICIT_LOAD_NAMES` matched the one file name.
STARTUP_HOOKS = ("sitecustomize", "usercustomize")

#: The forms a module takes on disk: source, Windows source, sourceless
#: bytecode, and an extension module (`x.cp314-win_amd64.pyd` is `.pyd`).
MODULE_SUFFIXES = (".py", ".pyw", ".pyc", ".pyd", ".so")

#: Distribution metadata pytest scans on `sys.path` for `pytest11` entry
#: points: a new one loads a plugin no test names.
_METADATA_DIRS = (".dist-info", ".egg-info")

#: Under `tests/`, more of them: a package marker runs before every module in
#: its package, and pytest takes its configuration from the FIRST config file
#: it meets walking up from the tests it was given -- so a new
#: `tests/pytest.ini` outranks the whole root configuration.
_TESTS_IMPLICIT = tuple(dict.fromkeys((*IMPLICIT_LOAD_NAMES, "__init__.py",
                                       *ROOT_JUDGE_FILES, *PYTEST_SECTIONS)))

#: Directory names pytest's default `norecursedirs` never descends into, so a
#: `conftest.py` inside one is never loaded (dot-directories and `*.egg` are
#: matched by `_skipped_dir`).
_NO_RECURSE = frozenset({"build", "dist", "node_modules", "venv", "cvs",
                         "_darcs", "{arch}", "__pycache__"})

#: Where a module file lands on the run's `sys.path`, earliest first: pytest's
#: default import mode inserts a test file's own directory at the front, the
#: lane root is where `python -m pytest` runs, and the gate's `PYTHONPATH`
#: adds `src/`. A module at an EARLIER entry replaces one of the same name at
#: a later entry -- or outside the lane altogether.
_BASE_RANK = {"tests": 0, "": 1, "src": 2}


def _norm(rel) -> str:
    return str(rel or "").replace("\\", "/").strip("/")


def _skipped_dir(name) -> bool:
    name = str(name).lower()
    return name.startswith(".") or name.endswith(".egg") or name in _NO_RECURSE


def _in_tests(parts) -> bool:
    return len(parts) > 1 and parts[0].lower() == _TESTS_DIR


def _module(filename) -> tuple:
    """``(stem, suffix)`` of a file name as the import system reads it: the
    name before the first dot, and the last suffix lower-cased --
    `sitecustomize.cp314-win_amd64.pyd` is `sitecustomize` as `.pyd`. A name
    with no dot has the empty suffix."""
    name = str(filename)
    stem, dot, rest = name.partition(".")
    return stem, ("." + rest.rsplit(".", 1)[-1].lower()) if dot else ""


def _by_name(parts) -> bool:
    """The judge files named by what IMPORTS them rather than by where they
    sit: a startup hook in any form the import system accepts, and plugin
    metadata. Never inside a directory pytest does not enter, where nothing
    imports from either."""
    if any(_skipped_dir(p) for p in parts[:-1]):
        return False
    parent = parts[-2].lower() if len(parts) > 1 else ""
    if parent.endswith(_METADATA_DIRS):
        return True
    stem, suffix = _module(parts[-1])
    if suffix not in MODULE_SUFFIXES:
        return False
    stem = stem.lower()
    if stem in STARTUP_HOOKS:
        return True
    return stem == "__init__" and parent in STARTUP_HOOKS


def _sieve(rel) -> tuple:
    """What `is_judge_path` and `is_implicit_load` decide alike, decided once:
    ``(verdict, parts, name)`` with *verdict* ``False`` for an empty path, a
    `__pycache__` entry or bytecode beside its source, ``True`` for what
    `_by_name` matches, and ``None`` where the two part ways. *parts* is the
    lane-relative path split, *name* its last part lower-cased."""
    rel = _norm(rel)
    if not rel:
        return False, (), ""
    parts = rel.split("/")
    if "__pycache__" in parts:
        return False, parts, ""
    if _by_name(parts):
        return True, parts, ""
    name = parts[-1].lower()
    if name.endswith((".pyc", ".pyo")):
        return False, parts, name
    return None, parts, name


def _loaded_below(parts, name) -> bool:
    """An `IMPLICIT_LOAD_NAMES` file anywhere pytest would look -- not inside
    a directory it never enters."""
    return (name in IMPLICIT_LOAD_NAMES
            and not any(_skipped_dir(p) for p in parts[:-1]))


def is_judge_path(rel) -> bool:
    """Whether *rel* (lane-relative) is part of the JUDGE rather than the work.

    Every file under `tests/` -- test modules, and just as much `conftest.py`,
    `__init__.py`, helper modules and the data fixtures a test compares
    against, because editing any of them changes what the suite demands. The
    startup hooks and plugin metadata `_by_name` matches. Every
    `IMPLICIT_LOAD_NAMES` file outside the directories pytest never enters. And
    the root files pytest reads whole. The shared config files in
    `PYTEST_SECTIONS` are judged by their section, in `tests_mutated`.

    Bytecode in a `__pycache__/`, or beside its source, is written by the
    interpreter the gate runs, so neither a baseline nor a lock may include
    it; a sourceless `sitecustomize.pyc` is the hook itself, and counts.
    Names compare case-insensitively: on Windows `CONFTEST.PY` is the file
    pytest opens as `conftest.py`, and elsewhere counting it costs nothing.
    What this cannot see is listed in the module docstring."""
    verdict, parts, name = _sieve(rel)
    if verdict is not None:
        return verdict
    if _in_tests(parts):
        return True
    if len(parts) == 1:
        return name in ROOT_JUDGE_FILES
    return _loaded_below(parts, name)


def is_implicit_load(rel) -> bool:
    """Whether a NEW file at *rel* changes a run just by existing -- the
    additions `tests_mutated` refuses, where a new test module is welcome.

    At the root, `ROOT_JUDGE_FILES` and the interpreter's startup hooks; under
    `tests/`, `_TESTS_IMPLICIT`; anywhere else pytest would look,
    `IMPLICIT_LOAD_NAMES`; and everywhere, what `_by_name` matches. A new
    `pyproject.toml`, `setup.cfg` or `tox.ini` at the root is judged by
    whether it brings a pytest section, and a new module by whether its name
    shadows one the run imports -- both in `tests_mutated`, not here."""
    verdict, parts, name = _sieve(rel)
    if verdict is not None:
        return verdict
    if len(parts) == 1:
        return name in ROOT_JUDGE_FILES or name in IMPLICIT_LOAD_NAMES
    if _in_tests(parts):
        return name in _TESTS_IMPLICIT
    return _loaded_below(parts, name)


def _tree(lane_path):
    """*lane_path* as a directory to read, or ``None``.

    An EMPTY path is refused, not read as `.`: `Path("")` IS the working
    directory, so a judge walked -- or, for the lock, chmodded -- there would
    be whichever checkout the caller happened to be standing in."""
    text = str(lane_path or "")
    if not text:
        return None
    lane = Path(text)
    return lane if lane.is_dir() else None


def judge_files(lane_path) -> tuple:
    """Lane-relative POSIX paths of every judge file the checkout at
    *lane_path* holds ON DISK, sorted: `is_judge_path` applied to a walk. The
    one list `snapshot_tests` hashes and `worktree.lock_tests_readonly` locks,
    so what is compared and what is protected cannot drift apart.

    Outside `tests/` the walk skips what pytest's own `norecursedirs` skips: a
    `conftest.py` in `.venv/` or `build/` is never loaded, and a walk into
    `.git` or another checkout (`.worktrees/`, `.claude/worktrees/`) would read
    -- or lock -- files that belong to something else. Under `tests/` only
    caches are skipped, because every file there is the judge. A symlinked
    directory is not entered, and neither is a Windows junction, which
    `os.walk(followlinks=False)` alone would walk into as a plain directory:
    either could name a tree the lock must never chmod. Total: ``()`` for an
    empty path or one that is not a directory, and an unreadable subtree is
    skipped, not raised."""
    from ..mindnet.worktree import is_link

    lane = _tree(lane_path)
    if lane is None:
        return ()
    out = []
    for top, dirs, files in os.walk(lane):
        try:
            here = Path(top).relative_to(lane).as_posix()
        except ValueError:                              # pragma: no cover - odd fs
            continue
        prefix = "" if here == "." else here + "/"
        under_tests = prefix.split("/", 1)[0].lower() == _TESTS_DIR
        dirs[:] = sorted(d for d in dirs if d != "__pycache__"
                         and (under_tests or not _skipped_dir(d))
                         and not is_link(Path(top, d)))
        out.extend(prefix + name for name in files
                   if is_judge_path(prefix + name))
    return tuple(sorted(out))


# --- the pytest section of a shared config file ----------------------------------

def pytest_section(rel, text) -> str | None:
    """The pytest-owned part of a shared config file, canonicalised so two
    spellings of one configuration compare equal and two configurations
    pytest would read differently compare unequal.

    ``"{}"`` for an absent file or an absent section. A PRESENT section, even
    an empty one, is a configuration (``{"pytest": {}}``): a root `tox.ini`
    holding a bare `[pytest]` outranks the `setup.cfg` beside it. ``None``
    when *text* is not that format, or holds a byte pytest could not decode
    -- which compares unequal to any side that DOES parse, so a config pytest
    could read at the baseline and cannot read now counts as changed.

    Read the way pytest reads it: `tomllib` for `pyproject.toml`, and
    `iniconfig` -- pytest's own parser, never `configparser` -- for the ini
    files. Key case is kept, `[DEFAULT]` is an ordinary section, and a line
    breaks wherever `str.splitlines` breaks it (a bare CR, a form feed);
    `configparser` lower-cases, merges and breaks only at a newline, and so
    compared four edits equal to the original they no longer were.

    `iniconfig` is imported here and nowhere else, on purpose: the core is
    stdlib-only (`pyproject.toml` declares no dependency, and every module
    imports clean on any Python), and `iniconfig` ships with the pytest the
    gate runs, so it is there wherever a section can matter. Where it is
    not, an ini section reads as ``None`` and `_section_key` compares the
    file's bytes instead: a non-pytest edit of `setup.cfg` is then a change
    too, which is the side to err on."""
    name = Path(str(rel or "")).name.lower()
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return None
    text = str(text or "")
    try:
        if name == "pyproject.toml":
            import tomllib
            key = "tool.pytest"
            section = tomllib.loads(text).get("tool", {}).get("pytest")
        else:
            from iniconfig import IniConfig, ParseError
            key = PYTEST_SECTIONS.get(name, "")
            try:
                section = IniConfig(name, data=text).sections.get(key)
            except ParseError:
                return None
            if section is not None:
                section = dict(section)
    except Exception:                                   # noqa: BLE001
        return None
    if section is None:
        return "{}"
    return json.dumps({key: section}, sort_keys=True, default=str)


def _digest(data) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


def _file_digest(path):
    """sha256 of a file's bytes, or ``None`` for one that cannot be read."""
    try:
        return _digest(Path(path).read_bytes())
    except OSError:
        return None


def _bytes_of(path):
    """A file's bytes; ``b""`` for one that is not there, which is how an
    absent config file reads to pytest too; ``None`` for one that is there
    and cannot be read -- a permission refused, an antivirus hold, another
    process's exclusive share on Windows. The two are different answers: an
    unreadable file compared as an absent one was a section a child added
    that the gate never saw."""
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return b""
    except OSError:
        return None


def _section_key(name, raw) -> str:
    """What a shared config file is compared BY: the digest of its pytest
    section, or, when pytest could not read it, of the bytes themselves --
    unreadable at the baseline and unreadable now is only "the same" when it
    is the same bytes."""
    section = pytest_section(name, raw)
    if section is None:
        return _digest(b"unreadable\x00" + bytes(raw or b""))
    return _digest(section.encode("utf-8"))


# --- the judge by import name ---------------------------------------------------

def _provides(rel):
    """``(name, base)`` when the file at lane-relative *rel* PROVIDES a
    top-level import name to the run -- a module file directly on one of the
    run's path entries, or the marker of a regular package there -- else
    ``None``. *base* is the entry (`_BASE_RANK`): `tests` for any directory
    under `tests/`, which pytest's default import mode puts first; `""` for
    the lane root; `src`. A module deeper inside a package provides nothing
    by itself, and a directory without a marker is a namespace portion, which
    never outranks a regular package."""
    parts = _norm(rel).split("/")
    if _in_tests(parts):
        base, rest = "tests", parts[1:]
        rest = rest[-2:] if _module(rest[-1])[0] == "__init__" else rest[-1:]
    elif len(parts) > 1 and parts[0] == "src":
        base, rest = "src", parts[1:]
    else:
        base, rest = "", parts
    if len(rest) == 1:
        stem, suffix = _module(rest[0])
        if suffix in MODULE_SUFFIXES and stem != "__init__" and stem.isidentifier():
            return stem, base
    elif len(rest) == 2:
        stem, suffix = _module(rest[1])
        if (stem == "__init__" and suffix in (".py", ".pyw", ".pyc")
                and rest[0].isidentifier()):
            return rest[0], base
    return None


def _lane_names(paths) -> dict:
    """Import name -> the set of path-entry ranks at which *paths* provide it."""
    out = {}
    for rel in paths:
        prov = _provides(rel)
        if prov is not None:
            out.setdefault(prov[0], set()).add(_BASE_RANK[prov[1]])
    return out


def _outside_entries(lane) -> list:
    """The `sys.path` entries that stand for what the lane's run imports from
    OUTSIDE the lane: the standard library and the environment's
    site-packages, where the installed pytest and its plugins live. Not this
    process's working directory, the directories pytest inserted for its own
    collection, or a `.pth`-added project tree -- those are the gate's, not
    the run's -- and never anything inside the lane."""
    import site

    roots = {sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix}
    try:
        roots.update(site.getsitepackages())
        roots.add(site.getusersitepackages())
    except Exception:                                   # noqa: BLE001 - site off
        pass
    roots = {os.path.normcase(os.path.abspath(r)) for r in roots if r}
    inside = os.path.normcase(os.path.abspath(str(lane)))
    out = []
    for entry in sys.path:
        if not entry:
            continue
        here = os.path.normcase(os.path.abspath(entry))
        if here == inside or here.startswith(inside + os.sep):
            continue
        if any(here == r or here.startswith(r + os.sep) for r in roots):
            out.append(entry)
    return out


def _resolves_outside(name, lane) -> bool:
    """Whether the interpreter finds a regular module or package called
    *name* on the entries `_outside_entries` names. A namespace portion does
    not count: a regular package placed earlier does not replace one, it is
    what the name resolved to anyway. Never raises."""
    from importlib.machinery import PathFinder

    try:
        spec = PathFinder.find_spec(name, _outside_entries(lane))
    except Exception:                                   # noqa: BLE001
        return False
    return bool(spec is not None and spec.origin and spec.origin != "namespace")


def _shadows(name, base, names, lane) -> bool:
    """Whether a module providing *name* from path entry *base* replaces what
    the run imports by that name: a provider the lane holds at a LATER entry
    (*names*, from `_lane_names`), or one found outside the lane."""
    rank = _BASE_RANK[base]
    if any(r > rank for r in names.get(name, ())):
        return True
    return _resolves_outside(name, lane)


def _modules_on_disk(lane, judge=None) -> tuple:
    """Lane-relative paths of every file on disk that `_provides` an import
    name: the module files and package markers directly at the lane root and
    under `src/`, and -- from the judge walk, which lists everything there --
    under `tests/`. Sorted; total."""
    out = []
    for sub in ("", "src"):
        base = lane / sub if sub else lane
        try:
            entries = sorted(base.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            rel = f"{sub}/{entry.name}" if sub else entry.name
            try:
                is_dir = entry.is_dir()
            except OSError:                             # pragma: no cover - odd fs
                continue
            if not is_dir:
                if _provides(rel) is not None:
                    out.append(rel)
                continue
            if _skipped_dir(entry.name):
                continue
            for marker in ("__init__.py", "__init__.pyw", "__init__.pyc"):
                cand = f"{rel}/{marker}"
                if (entry / marker).is_file() and _provides(cand) is not None:
                    out.append(cand)
                    break
    for rel in (judge_files(lane) if judge is None else judge):
        if _in_tests(rel.split("/")) and _provides(rel) is not None:
            out.append(rel)
    return tuple(sorted(dict.fromkeys(out)))


def _classify(edited, added, names, lane) -> list:
    """The two PATH rules every reader of the judge applies, in one place, so
    `_mutated_since`, `tests_mutated` and `judge_changes` cannot come to
    different verdicts about one file. Of *edited* -- paths modified, deleted
    or retyped since the baseline -- those `is_judge_path` names. Of *added*
    -- paths the baseline did not hold -- those `is_implicit_load` names, or
    that `_shadows` a name in *names* (`_lane_names` over what the baseline
    held) or one the run imports from outside *lane*. Each reader reads its
    own data source, and judges the pytest sections itself by
    `_section_key`, because those compare bytes it alone can fetch."""
    out = [rel for rel in edited if is_judge_path(rel)]
    for rel in added:
        if is_implicit_load(rel):
            out.append(rel)
            continue
        prov = _provides(rel)
        if prov is not None and _shadows(prov[0], prov[1], names, lane):
            out.append(rel)
    return out


# --- the explicit snapshot --------------------------------------------------------

def snapshot_tests(lane_path) -> dict:
    """A digest of every part of the judge the checkout at *lane_path* holds,
    keyed by lane-relative POSIX path: sha256 of each file `judge_files`
    lists; for each `PYTEST_SECTIONS` file present, `_section_key`; and, so
    that `tests_mutated` can tell a new module that shadows an existing one,
    sha256 of every module file `_modules_on_disk` names -- those entries
    record presence, and a change to one is the work, not the judge.

    An explicit baseline for a caller that can take one BEFORE a child runs.
    `tests_mutated` prefers git's own -- see there. Total: an unreadable file
    is simply not in the map, and an empty path or one that is not a
    directory gives `{}`."""
    lane = _tree(lane_path)
    out = {}
    if lane is None:
        return out
    files = judge_files(lane)
    for rel in files:
        digest = _file_digest(lane / rel)
        if digest is not None:
            out[rel] = digest
    for name in PYTEST_SECTIONS:
        if (lane / name).is_file():
            raw = _bytes_of(lane / name)
            if raw is not None:
                out[name] = _section_key(name, raw)
    for rel in _modules_on_disk(lane, files):
        if rel not in out:
            digest = _file_digest(lane / rel)
            if digest is not None:
                out[rel] = digest
    return out


def _mutated_since(lane, was):
    """`tests_mutated` against an explicit `snapshot_tests` map: what judge
    file changed or vanished, what pytest section changed, plus what appeared
    that `is_implicit_load` names, that brings a pytest section with it, or
    that shadows a module the snapshot held or the run imports. ``None`` when
    a shared config file is there now but could not be read: a snapshot
    leaves such a file out, and "not in the map" must not read as absent."""
    now = snapshot_tests(lane)
    for name in PYTEST_SECTIONS:
        if name not in now and (lane / name).is_file():
            return None
    edited = [rel for rel, digest in was.items()
              if rel not in PYTEST_SECTIONS and now.get(rel) != digest]
    added = [rel for rel in now if rel not in was and rel not in PYTEST_SECTIONS]
    out = _classify(edited, added, _lane_names(was), lane)
    for name in PYTEST_SECTIONS:
        absent = _section_key(name, b"")
        if was.get(name, absent) != now.get(name, absent):
            out.append(name)
    return tuple(sorted(dict.fromkeys(out)))


# --- against git ------------------------------------------------------------------

def tests_mutated(lane_path, snapshot=None, base="HEAD"):
    """Sorted lane-relative paths of the parts of the judge this lane CHANGED
    since *base* -- ``None`` when that cannot be told. `is_judge_path` says
    what the judge is.

    Three kinds of change count. An EDIT: a judge file *base* held that is now
    modified, deleted or retyped, by a commit or in the working tree alike. An
    ADDITION of a file that changes a run by existing (`is_implicit_load`),
    or of a shared config file whose pytest section differs from none
    (`_section_key`). And a NEW MODULE whose name shadows one the run imports
    (`_shadows`): one *base* held at a later path entry, or one the standard
    library or site-packages provide. A new test module with a name of its
    own is none of these and is never refused -- it is what the greenfield
    flow tells a child to write -- and `modified_tracked`'s
    `--diff-filter=DMT` keeps it out whether it is staged or not.

    *base* is the commit compared against. `SuiteGate.check` passes the one
    `SuiteGate.begin` pinned before the unit's first attempt, so a child that
    commits its edit moves `HEAD` and not the baseline. The default, `HEAD`, is
    for a caller holding no pin; a committing child CAN move that one.

    A *snapshot* from `snapshot_tests` is honoured instead when given, for a
    caller holding a baseline git cannot express.

    ``None`` rather than ``()`` when git cannot answer, when there is no
    directory at *lane_path* to ask about, when *lane_path* is not a
    checkout's top level (`is_toplevel`: git asked in `src/` answers for the
    repository around it, and every path would be read against the wrong
    root), or when a shared config file is there and cannot be read now
    (`_bytes_of`) -- because "nothing I could see changed" is not "nothing
    changed" and `check` refuses on it. Never raises."""
    lane = _tree(lane_path)
    if lane is None:
        return None
    if snapshot is not None:
        return _mutated_since(lane, dict(snapshot))
    try:
        from ..mindnet import worktree as mn_wt
        if not mn_wt.is_toplevel(lane):
            return None
        changed = mn_wt.modified_tracked(lane, base)
        held = mn_wt.tracked_at(lane, base)
        if changed is None or held is None:
            return None
        on_disk = judge_files(lane)
        added = [rel for rel in (*on_disk, *_modules_on_disk(lane, on_disk))
                 if rel not in held]
        out = _classify(changed, added, _lane_names(held), lane)
        for name in PYTEST_SECTIONS:
            if name in held:
                if name not in changed:
                    continue
                was = mn_wt.show_at(lane, name, base)
                if was is None:
                    return None
            else:
                was = b""
            now = _bytes_of(lane / name)
            if now is None:
                return None
            if _section_key(name, was) != _section_key(name, now):
                out.append(name)
    except Exception:                                   # noqa: BLE001
        return None
    return tuple(sorted(dict.fromkeys(out)))


def judge_changes(root, base, head):
    """Sorted lane-relative paths of the parts of the judge that differ
    between the commits *base* and *head* in the checkout at *root* -- the
    working tree no part of the question -- or ``None`` when git cannot say.

    What `SuiteGate.begin` asks before it trusts a later commit on a lane an
    earlier unit ran on. The same three kinds of change as `tests_mutated`:
    a judge file *head* holds edited, deleted or retyped against *base*; an
    addition that `is_implicit_load` names or that shadows a module *base*
    held elsewhere or the run imports from outside; a pytest section that
    reads differently at the two commits, an absent file reading as none.
    The names are *base*'s, as they were for the unit that made the commits:
    `src/twin.py` and `tests/twin.py` added together replace nothing the run
    imported, and read with *head*'s names the second would shadow the first
    and no commit on the lane would ever be trusted again.
    ``()`` is "the judge at *head* is the judge at *base*". ``None`` for a
    path that is not a checkout's top level (`is_toplevel`), a revision git
    cannot resolve, or one shaped like an option. Never raises."""
    lane = _tree(root)
    if lane is None:
        return None
    try:
        from ..mindnet import worktree as mn_wt
        if not mn_wt.is_toplevel(lane):
            return None
        rows = mn_wt.changes_between(lane, base, head)
        held = mn_wt.tracked_at(lane, base)
        if rows is None or held is None:
            return None
        status, edited, added = {}, [], []
        for kind, rel in rows:
            rel = _norm(rel)
            if rel in PYTEST_SECTIONS:
                status[rel] = kind
            elif kind == "A":
                added.append(rel)
            else:
                edited.append(rel)
        out = _classify(edited, added, _lane_names(held), lane)
        for name, kind in status.items():
            was = b"" if kind == "A" else mn_wt.show_at(lane, name, base)
            now = b"" if kind == "D" else mn_wt.show_at(lane, name, head)
            if was is None or now is None:
                return None
            if _section_key(name, was) != _section_key(name, now):
                out.append(name)
    except Exception:                                   # noqa: BLE001
        return None
    return tuple(sorted(dict.fromkeys(out)))
