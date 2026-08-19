# tests/test_pgshape.py
"""The two properties that make the shaper's own module safe to have.

`pgshape` was lifted out of `pgtools` (TODOS 18). The shaping behaviour itself
is already covered where it always was, in `tests/test_pgtools.py` — moving a
block does not earn it a second copy of its tests. What is NEW, and what this
file holds, is what the SPLIT could break and the old single module could not:

1. **The shaper imports nothing from this package and no driver.** Shaping is
   what a caller reaches for precisely when the backend is gone: `error_envelope`
   exists for that moment. A shaper that could not be imported without `psycopg`
   would turn every outage into an ImportError inside the agent's turn — the
   exact failure this project already shipped once, where 37 passing tests
   surrounded a `main()` that died on startup.

2. **Every moved name is re-exported, and is the SAME OBJECT.** This repo has
   been bitten by a mechanical split before: "a mechanical module split silently
   breaks monkeypatch addresses". Identity is the strong form of that check — a
   re-export that had drifted into a copy would satisfy `hasattr` and still leave
   a `monkeypatch.setattr(pgtools, ...)` patching an object nobody calls.

Both are structural claims, so both are asserted structurally rather than by
running a tool and hoping the answer would have differed.
"""
import ast
import subprocess
import sys
import types
from pathlib import Path

from mindhive import pgshape, pgtools

# The names `pgtools` must keep answering to. Derived from `pgshape` itself
# rather than typed out, so a helper added there without a re-export is caught
# by construction instead of by whoever remembers to update a list.
MOVED = sorted(n for n, v in vars(pgshape).items()
               if not n.startswith("__")
               and not isinstance(v, types.ModuleType))

# What a module is allowed to import and still be importable with the database
# down and no driver installed. Everything here ships with CPython.
STDLIB_OK = {"json", "numbers", "os", "re", "math", "struct", "datetime",
             "decimal", "collections", "typing"}


def _imports(path):
    """`(module, names)` for every import in the file, relative ones included.

    A relative `from . import pgstore` has `node.module is None` and `level=1`,
    which a naive scan reads as "no module" and skips — the single most likely
    way this gate would pass while the thing it forbids sat in the file."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((a.name, a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * (node.level or 0)
            found.extend((prefix + (node.module or ""), a.name)
                         for a in node.names)
    return found


def test_the_shaper_imports_only_the_standard_library():
    """THE GATE. Not "no psycopg" as a named exclusion — an allowlist, because
    the next dependency to sneak in is by definition one nobody thought to
    forbid."""
    offenders = [(mod, name) for mod, name in _imports(pgshape.__file__)
                 if mod.split(".")[0] not in STDLIB_OK]
    assert offenders == [], (
        f"pgshape imports outside the standard library: {offenders}. The error "
        f"envelope must be constructible with the backend gone; an import of "
        f"pgstore, pgmirror or psycopg here makes an outage an ImportError.")


def test_no_relative_import_reaches_back_into_the_package():
    """Stated separately from the allowlist because it is the specific edge the
    scanner above had to be written carefully to see at all."""
    relative = [(mod, name) for mod, name in _imports(pgshape.__file__)
                if mod.startswith(".")]
    assert relative == [], f"pgshape reaches back into the package: {relative}"


def test_the_import_scanner_sees_imports_at_all():
    """CANARY. Both assertions above are emptiness claims, and a scanner that
    returned nothing — a moved file, a parse that silently failed — would
    satisfy both while checking nothing. `pgtools` is the same tree and is known
    to import from this package; the scanner must see that."""
    tools = _imports(pgtools.__file__)
    assert tools, "the scanner found no imports in pgtools.py at all"
    assert any(mod.startswith(".") for mod, _ in tools), \
        "the scanner did not see pgtools' own relative imports"
    shaper = _imports(pgshape.__file__)
    assert {m for m, _ in shaper} >= {"json", "os"}, \
        f"the scanner did not see pgshape's stdlib imports: {shaper}"


def test_every_moved_name_is_still_reachable_through_pgtools():
    """The addresses other modules and tests already hold. `pgsearch` calls
    `pgtools.envelope`, `pgtools.shape_rows`, `pgtools._fit_response` and
    `pgtools.error_envelope` by those names; so does the rest of the tree."""
    missing = [n for n in MOVED if not hasattr(pgtools, n)]
    assert missing == [], (
        f"moved out of pgtools without a re-export: {missing}. Every name in "
        f"pgshape must stay addressable as pgtools.<name>.")


def test_each_re_export_is_the_same_object_not_a_copy():
    """The strong form. `hasattr` passes for a name that was RETYPED into
    pgtools as well as for one that was re-exported, and the retyped case is
    precisely the silent one: patching pgtools.envelope would then leave
    pgsearch calling the other envelope."""
    forked = [n for n in MOVED
              if hasattr(pgtools, n)
              and getattr(pgtools, n) is not getattr(pgshape, n)]
    assert forked == [], (
        f"pgtools holds its own copy of: {forked}. A re-export must be the "
        f"same object, or monkeypatching one address leaves the other live.")


def test_the_identity_check_can_actually_fail(monkeypatch):
    """CANARY for the check above, which is another emptiness claim. Fork one
    name deliberately and require the comparison to notice."""
    monkeypatch.setattr(pgtools, "envelope", lambda *a, **k: None)
    forked = [n for n in MOVED
              if hasattr(pgtools, n)
              and getattr(pgtools, n) is not getattr(pgshape, n)]
    assert forked == ["envelope"], \
        "a deliberately forked re-export was not detected"


def test_the_names_the_tests_patch_did_not_move():
    """The failure this repo has already had, named directly. Splitting
    `dashboard.py` moved three monkeypatch targets and the tests kept passing
    against code nobody ran. These six are patched by module path in
    `tests/test_pgtools.py` and `tests/test_mcp_server.py`, so they must live in
    `pgtools` and must NOT have followed the shaper out."""
    for name in ("_session", "_run", "_freshness", "_parity_refusal",
                 "_availability", "_project"):
        assert hasattr(pgtools, name), f"{name} left pgtools"
        assert not hasattr(pgshape, name), \
            f"{name} moved into pgshape; every monkeypatch address for it is stale"


def test_the_shaper_answers_with_the_package_and_the_driver_unimportable():
    """The behavioural half, in a FRESH interpreter because the in-process one
    has already imported everything. Blocks `psycopg`, `mindhive.pgstore` and
    `mindhive.pgmirror` at the finder, then builds an error envelope — the exact
    call a caller makes when the backend is the thing that is gone."""
    probe = (
        "import sys" "\n"
        "BLOCKED = ('psycopg', 'mindhive.pgstore', 'mindhive.pgmirror')" "\n"
        "class Refuse:" "\n"
        "    @staticmethod" "\n"
        "    def find_spec(name, path=None, target=None):" "\n"
        "        if name in BLOCKED or name.startswith('psycopg'):" "\n"
        "            raise ModuleNotFoundError(name)" "\n"
        "        return None" "\n"
        "sys.meta_path.insert(0, Refuse)" "\n"
        "from mindhive import pgshape" "\n"
        "env = pgshape.error_envelope('query_database', 'backend_unavailable'," "\n"
        "                             'no server', project='p')" "\n"
        "assert env['ok'] is False, env" "\n"
        "assert env['error']['code'] == 'backend_unavailable', env" "\n"
        "assert env['error']['remedy'], env" "\n"
        "assert 'mindhive.pgstore' not in sys.modules" "\n"
        "print('OK')" "\n"
    )
    src = str(Path(pgshape.__file__).parent.parent)
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, cwd=src, timeout=120)
    assert out.returncode == 0, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert "OK" in out.stdout
