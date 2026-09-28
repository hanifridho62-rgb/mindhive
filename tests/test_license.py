# tests/test_license.py
"""Blueprint P7 (spec section 2.14, TODOS #1): the repository says what it is
licensed under, everywhere a reader or a tool looks for it.

The spec classifies MIND HIVE as Apache License 2.0 and names the gap: README
publishes a public install command while the root carried no LICENSE, and the
legal default for unlicensed code is all rights reserved. The remedy it
prescribes has four parts, and each is pinned here against the REAL files:

* `LICENSE` -- the Apache 2.0 text, verbatim, pinned by the sha256 of the
  canonical apache.org file (line endings normalised first: `core.autocrlf`
  checks text out with CRLF on Windows);
* `CONTRIBUTING.md` and `CHANGELOG.md` beside it;
* the SPDX identifier on every source file, on the first line the language
  lets a comment occupy (after a shebang, or a PEP 263 encoding declaration);
* the packaging metadata: `pyproject.toml` declares the license as a PEP 639
  SPDX expression with LICENSE attached, and the built metadata says so.

What the sweep deliberately leaves alone is part of the contract, not a gap in
it. `pgmigrations/*.sql` is pinned by the sha256 of each file once applied, so
a header stamped on afterwards would make `apply_migrations` refuse to run on
every mirror that already ran it. `skills/caveman/` is vendored under its own
MIT license, which the root license does not replace. Markdown skills and
commands open with YAML frontmatter Claude Code parses, and JSON has no
comments. `tests/` is not distributed -- neither the wheel nor the plugin ships
it -- and the root LICENSE covers it without a per-file header.
"""
import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from mindhive import pgmirror

REPO = Path(__file__).resolve().parents[1]
SPDX_ID = "SPDX-License-Identifier: Apache-2.0"

# sha256 of https://www.apache.org/licenses/LICENSE-2.0.txt -- 11358 bytes, LF.
APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"

# (root, glob) for every file that is source code rather than data or prose.
SOURCE_GLOBS = (
    ("src/mindhive", "**/*.py"),
    ("src/mindhive", "**/*.js"),
    ("src/mindhive", "**/*.css"),
    ("hooks", "*.sh"),
    ("hooks", "*.ps1"),
    ("scripts", "**/*.py"),
    ("scripts", "**/*.sh"),
    ("build", "*.py"),
    (".", "*.ps1"),
)
# The comment each language gives the identifier; the first form is the one a
# new file should use.
HEADERS = {
    ".py": ("# " + SPDX_ID,),
    ".sh": ("# " + SPDX_ID,),
    ".ps1": ("# " + SPDX_ID,),
    ".js": ("// " + SPDX_ID, "/* " + SPDX_ID + " */"),
    ".css": ("/* " + SPDX_ID + " */",),
}
PRUNED = {"__pycache__"}
# PEP 263: an encoding declaration is honoured on line 1 or 2 only, so it keeps
# its place and the identifier follows it.
_CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")

# The digests `schema_migrations.sha256` holds for the seven migrations that
# existed when the header sweep ran. They carry no header and never will.
APPLIED_MIGRATIONS = {
    "001_schema.sql": "89010bc642131a5ec5c4c23c290e3daa9e49380465d0667ec2c6fe08f2141bd3",
    "002_indexes.sql": "fade55f481539bd5de440d10be55dfc04b5306464d01ca10c5a30874e326f4ac",
    "003_roles_and_views.sql": "068cd153dd3fa89eeb9259fc62503431be661c0886078c4a69eb76526498834f",
    "004_mirror_state_view_and_ownership.sql":
        "4a7e31bf8cb843e39c61ba99886530e1fe172922e62d96a8485f400f8f496f7a",
    "005_drop_unmatchable_order_index.sql":
        "e21e330e8fb7cfda23c9655b6987653dd14d855de5e6b495d5136abd5e1b27e6",
    "006_event_embeddings.sql": "3e954ee02d0a647a972558357237ed7bdffa4d4782348215334e435e1349d96c",
    "007_event_embeddings_hashes.sql":
        "a1bbdf09778c1dc533d80bd92e81aca53dde1e2b3808fa2bf6fdbca5b19d8dd4",
}


# --- helpers ------------------------------------------------------------------

def _text(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def _pyproject() -> dict:
    return tomllib.loads(_text("pyproject.toml"))


def _source_files() -> list:
    found = set()
    for root, pattern in SOURCE_GLOBS:
        base = REPO / root
        if not base.is_dir():
            continue
        for path in base.glob(pattern):
            if path.is_file() and not PRUNED.intersection(path.relative_to(REPO).parts):
                found.add(path)
    return sorted(found)


def spdx_problem(path: Path) -> str | None:
    """None when `path` carries the identifier where it belongs, else why not.

    The identifier must sit on the first line a comment may occupy: line 1, or
    the line after a shebang, or after a PEP 263 encoding declaration in a .py
    file. A UTF-8 byte-order mark in front of it is tolerated."""
    headers = HEADERS.get(path.suffix)
    if headers is None:
        return f"no SPDX comment form is defined for {path.suffix!r} files"
    lines = path.read_bytes().decode("utf-8", errors="replace").lstrip("﻿").splitlines()
    i = 0
    if lines and lines[0].startswith("#!") and path.suffix in (".py", ".sh", ".ps1"):
        i = 1
    if path.suffix == ".py" and i < min(len(lines), 2) and _CODING.match(lines[i]):
        i += 1
    got = lines[i].rstrip() if i < len(lines) else "<end of file>"
    if got in headers:
        return None
    return f"line {i + 1} is {got[:72]!r}, expected {headers[0]!r}"


# --- the license text ---------------------------------------------------------

def test_license_is_the_verbatim_apache_2_0_text():
    raw = (REPO / "LICENSE").read_bytes().replace(b"\r\n", b"\n")
    assert b"Apache License" in raw and b"Version 2.0, January 2004" in raw
    assert hashlib.sha256(raw).hexdigest() == APACHE_2_0_SHA256, (
        "LICENSE is not the canonical Apache 2.0 text. Restore it verbatim -- "
        "project notices belong in README, never inside the license itself.")


def test_the_vendored_mit_component_keeps_its_own_license():
    """`skills/caveman/` is vendored from JuliusBrussee/caveman under MIT. The
    root license does not relicense it, and its notice must survive."""
    assert _text("skills/caveman/LICENSE").startswith("MIT License")


def test_contributing_and_changelog_sit_beside_the_license():
    contributing = _text("CONTRIBUTING.md")
    for needle in ("Apache-2.0", SPDX_ID, "hive log"):
        assert needle in contributing, f"CONTRIBUTING.md does not mention {needle!r}"
    assert "## [Unreleased]" in _text("CHANGELOG.md")


def test_the_changelog_names_the_version_the_plugin_manifest_declares():
    """Pins the REAL manifest: a version bump that skips the changelog fails
    here instead of shipping a release nobody wrote down."""
    version = json.loads(_text(".claude-plugin/plugin.json"))["version"]
    assert f"## [{version}]" in _text("CHANGELOG.md"), version


def test_readme_states_the_license_and_the_vendored_exception():
    readme = _text("README.md")
    assert "\n## License\n" in readme
    section = readme.split("\n## License\n", 1)[1].split("\n## ", 1)[0]
    assert "Apache License, Version 2.0" in section
    assert "(LICENSE)" in section
    assert "skills/caveman" in section and "MIT" in section


# --- packaging metadata ---------------------------------------------------------

def test_pyproject_declares_the_license_as_a_pep_639_expression():
    project = _pyproject()["project"]
    assert project["license"] == "Apache-2.0"
    assert "LICENSE" in project["license-files"]
    # setuptools >= 77 refuses a `License ::` classifier next to an expression
    # (InvalidConfigError -- seen on 81 and 84): the expression replaces it.
    assert [c for c in project.get("classifiers", []) if c.startswith("License ::")] == []


def test_the_build_backend_floor_reads_a_license_expression():
    """A string `project.license` is PEP 639, which setuptools reads from 77 on;
    68 through 76 reject it as a malformed table. The floor has to say so."""
    requires = _pyproject()["build-system"]["requires"]
    floors = [r for r in requires if re.match(r"setuptools\b", r.replace(" ", ""))]
    assert floors, requires
    match = re.search(r">=\s*(\d+)", floors[0])
    assert match and int(match.group(1)) >= 77, floors[0]


def test_the_built_metadata_carries_the_license_expression(tmp_path):
    """The verifier that matters: build the wheel metadata from a copy of this
    checkout and read what a package index would show."""
    setuptools = pytest.importorskip("setuptools")
    major = int(setuptools.__version__.split(".")[0])
    if major < 77:
        pytest.skip(f"setuptools {setuptools.__version__} predates PEP 639 (needs >= 77)")
    for name in ("pyproject.toml", "LICENSE"):
        shutil.copyfile(REPO / name, tmp_path / name)
    shutil.copytree(REPO / "src", tmp_path / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    out = tmp_path / "meta"
    out.mkdir()
    code = ("import sys\nfrom setuptools import build_meta\n"
            "print('DIST_INFO=' + build_meta.prepare_metadata_for_build_wheel(sys.argv[1]))\n")
    proc = subprocess.run([sys.executable, "-c", code, str(out)], cwd=tmp_path,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    dist_info = [ln for ln in proc.stdout.splitlines() if ln.startswith("DIST_INFO=")][-1]
    metadata = (out / dist_info.split("=", 1)[1] / "METADATA").read_text(encoding="utf-8")
    lines = metadata.splitlines()
    assert "License-Expression: Apache-2.0" in lines
    assert "License-File: LICENSE" in lines
    assert [ln for ln in lines if ln.startswith("Classifier: License ::")] == []


# --- the SPDX identifier on every source file ------------------------------------

def test_the_source_walk_reaches_every_tree():
    """CANARY for the sweep below. An empty walk satisfies "no file lacks the
    header" while checking nothing, so require the trees this repo has."""
    rel = {p.relative_to(REPO).as_posix() for p in _source_files()}
    assert len(rel) > 200, f"the walk found only {len(rel)} source files"
    for must in ("src/mindhive/events.py", "src/mindhive/mindnet/controller.py",
                 "src/mindhive/pgmigrations/__init__.py", "hooks/lib.sh",
                 "hooks/session_start.sh", "hooks/session_start.ps1",
                 "scripts/rotation/empirical_gate.py", "scripts/self_reliant/bring_up.sh",
                 "build/build_app.py", "circle.ps1"):
        assert must in rel, f"the walk missed {must}"
    assert [r for r in rel if r.endswith(".sql")] == []


def test_every_source_file_carries_the_spdx_identifier_first():
    problems = {p.relative_to(REPO).as_posix(): why
                for p in _source_files() if (why := spdx_problem(p))}
    assert problems == {}, (
        f"{len(problems)} source file(s) without the SPDX identifier where it "
        f"belongs: {dict(list(problems.items())[:15])}")


# `verify` is the module the plan named that has a docstring (`events` and `sync`
# open with their imports and never had one); the rest traded their line-1 path
# comment for the identifier, or gave back the blank line after their
# docstring, to hold a size ceiling -- the edits most able to disturb one.
@pytest.mark.parametrize("module", [
    "verify", "replenish", "workspace", "research", "mindnet/controller",
    "orchestrator/lintester",
])
def test_the_identifier_leaves_the_module_docstring_first(module):
    """A comment is not a statement: above the docstring it leaves the docstring
    the module's first statement, which `ast.get_docstring`, `help()` and the
    suite's own docstring readers depend on."""
    text = (REPO / "src" / "mindhive" / f"{module}.py").read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# " + SPDX_ID
    assert ast.get_docstring(ast.parse(text)), f"{module}.py lost its docstring"


def _opens_with_a_string(text: str) -> bool:
    """Whether the first line that is neither blank nor a comment starts a string
    literal: a module its author opened with a docstring."""
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s.startswith(('"', "'", 'r"', "r'"))
    return False


def test_every_module_that_opens_with_a_string_keeps_it_as_its_docstring():
    """The same property across the tree: no header line may put a statement
    between a module and its docstring."""
    src = REPO / "src" / "mindhive"
    checked, lost = [], []
    for p in sorted(src.rglob("*.py")):
        if PRUNED.intersection(p.parts):
            continue
        text = p.read_text(encoding="utf-8")
        if _opens_with_a_string(text):
            checked.append(p.relative_to(src).as_posix())
            if not ast.get_docstring(ast.parse(text)):
                lost.append(checked[-1])
    assert len(checked) > 150, f"only {len(checked)} modules open with a string"
    assert "verify.py" in checked and "events.py" not in checked
    assert lost == [], f"modules whose opening string is no longer their docstring: {lost}"


@pytest.mark.parametrize("name, content, ok", [
    ("plain.py", f'# {SPDX_ID}\n"""doc"""\n', True),
    ("path_comment.py", f'# {SPDX_ID}\n# src/x.py\n"""doc"""\n', True),
    ("missing.py", '"""doc"""\n', False),
    ("late.py", f'"""doc"""\n# {SPDX_ID}\n', False),
    ("empty.py", "", False),
    ("shebang.py", f"#!/usr/bin/env python3\n# {SPDX_ID}\n", True),
    ("coding.py", f"# -*- coding: utf-8 -*-\n# {SPDX_ID}\n", True),
    ("wrong_license.py", "# SPDX-License-Identifier: MIT\n", False),
    ("shim.sh", f"#!/bin/sh\n# {SPDX_ID}\nset -e\n", True),
    ("shim_late.sh", f"#!/bin/sh\nset -e\n# {SPDX_ID}\n", False),
    ("hook.ps1", f"# {SPDX_ID}\r\nparam()\r\n", True),
    ("bom.ps1", f"﻿# {SPDX_ID}\r\n", True),
    ("page.js", f"// {SPDX_ID}\n", True),
    ("page_block.js", f"/* {SPDX_ID} */\n", True),
    ("page.css", f"/* {SPDX_ID} */\n", True),
    ("page_line.css", f"// {SPDX_ID}\n", False),
    ("data.sql", f"-- {SPDX_ID}\n", False),
])
def test_the_header_check_accepts_and_rejects(tmp_path, name, content, ok):
    """CANARY for the checker itself: every rejection above is a way the sweep
    could pass vacuously if the check were looser than it reads."""
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8"))
    assert (spdx_problem(path) is None) is ok, spdx_problem(path)


def test_applied_migrations_keep_their_bytes():
    """The one deliberate gap in the sweep, held from the other side:
    `apply_migrations` records each migration's sha256 and refuses to continue
    when a recorded file changes, so stamping a header on an applied file stops
    every mirror that already ran it. A migration may be born with a header;
    these seven were born without one and keep their bytes."""
    got = {name: pgmirror.migration_sha256(name) for name in pgmirror.migration_files()
           if name in APPLIED_MIGRATIONS}
    assert got == APPLIED_MIGRATIONS
    for name in APPLIED_MIGRATIONS:
        assert SPDX_ID not in pgmirror.migration_text(name).splitlines()[0]
