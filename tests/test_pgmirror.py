# tests/test_pgmirror.py
"""The admin half — and the boundary that keeps it away from the AI.

`pgmirror` is the only module that may hold the owner DSN. Everything that can
write to the mirror lives behind that credential, so the security property is
not "the AI path is careful with the admin connection" but "the AI path never
learns its name". A boundary is testable; carefulness is not.

Almost every function here needs a server that does not exist, so this file
tests the two things that do not:

* **the boundary** — who may import this module, which credential it reads, and
  that `pgstore` cannot reach the owner DSN;
* **the migration inventory** — that the three .sql files are discoverable in
  application order through `importlib.resources` and hash stably, which is what
  `apply_migrations` refuses on when an applied file's bytes change.

The server-touching bodies are asserted to FAIL CLOSED. A skeleton that returned
`{"ok": True}` while doing nothing is how a caller comes to believe a mirror was
rebuilt that never existed.
"""
import ast
import re
import hashlib
from pathlib import Path

import pytest

from mindhive import pgmirror, pgstore

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "mindhive"
ADMIN_ENV = "MINDHIVE_PG_DSN_ADMIN"


@pytest.fixture(autouse=True)
def _no_inherited_dsn(monkeypatch):
    monkeypatch.delenv(ADMIN_ENV, raising=False)
    monkeypatch.delenv("MINDHIVE_PG_DSN_RO", raising=False)


# --------------------------------------------------------------------------
# the credential boundary
# --------------------------------------------------------------------------

def test_the_admin_dsn_is_unset_by_default():
    """`MINDHIVE_PG_DSN_ADMIN` is absent in normal sessions on purpose: a
    session physically cannot write if the credential is not in its
    environment, however every other layer fails."""
    assert pgmirror.admin_dsn() is None


def test_the_admin_dsn_comes_from_its_own_variable(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "service=mindhive_owner")
    assert pgmirror.admin_dsn() == "service=mindhive_owner"


def test_a_blank_admin_dsn_is_unconfigured(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "   ")
    assert pgmirror.admin_dsn() is None


def test_the_reader_dsn_is_never_accepted_as_the_admin_one(monkeypatch):
    """The tempting shortcut — "fall back to the DSN we do have" — would hand
    every migration and every rebuild to a role with no privilege to run them,
    and would put the two credentials on one code path."""
    monkeypatch.setenv("MINDHIVE_PG_DSN_RO", "service=mindhive_ro")
    assert pgmirror.admin_dsn() is None


def test_migrating_without_the_admin_credential_refuses_by_name(monkeypatch):
    with pytest.raises(RuntimeError) as e:
        pgmirror.apply_migrations()
    assert ADMIN_ENV in str(e.value)


def test_pgstore_cannot_reach_the_admin_credential(monkeypatch):
    """The other half of the boundary, from the AI side."""
    monkeypatch.setenv(ADMIN_ENV, "service=mindhive_owner")
    assert pgstore.dsn() is None
    with pytest.raises(ValueError):
        pgstore.dsn("admin")


# --------------------------------------------------------------------------
# the import graph
# --------------------------------------------------------------------------

def _importers_of(module: str) -> list[str]:
    """Every module under src/mindhive that imports `module`."""
    out = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                     # pragma: no cover - not our file
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names] + [(node.module or "")]
            elif isinstance(node, ast.Import):
                names = [a.name.split(".")[-1] for a in node.names]
            if module in [n.split(".")[-1] for n in names if n]:
                out.append(str(path.relative_to(SRC)).replace("\\", "/"))
                break
    return out


def _imports_are_all_inside_functions(name: str, module: str) -> bool:
    """True when every import of `module` in `name` sits inside a function body.

    A module-scope import is paid by everyone who imports the file; an import
    inside a function is paid only by the caller that reaches it. For an admin
    module that is the difference between "one operator entry point can write"
    and "importing the sync path drags the owner-DSN module into memory"."""
    tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
    inner = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner.update(id(n) for n in ast.walk(fn))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names] + [(node.module or "")]
        elif isinstance(node, ast.Import):
            names = [a.name.split(".")[-1] for a in node.names]
        if module in [n.split(".")[-1] for n in names if n] and id(node) not in inner:
            return False
    return True


def test_nothing_on_the_ai_read_path_imports_the_admin_module():
    """Stated as an allowlist rather than a denylist, and each entry earns its
    place by being an OPERATOR entry point rather than a session path:

    * `pgcli.py` — a human typing `hive pg migrate` / `rebuild` / `refresh` is the
      original intended caller.
    * `autosync.py` — the scheduled task that makes the mirror FOLLOW the ledger.
      Nothing on the append path writes to Postgres, so the mirror has to advance
      somewhere, and the schedule is that somewhere. It is not an AI path: no
      tool, no MCP handler and no dashboard request reaches it, and it gates on
      `MINDHIVE_PG_DSN_ADMIN` before importing anything.

    Anything else appearing here is a module that just gained the ability to
    write, which is the whole reason this assertion exists."""
    allowed = {"pgcli.py", "autosync.py"}
    assert set(_importers_of("pgmirror")) <= allowed, \
        f"unexpected importer(s) of pgmirror: {_importers_of('pgmirror')}"


def test_the_allowed_importers_reach_the_admin_module_lazily():
    """The teeth on the allowlist above. Being permitted to import `pgmirror` is
    not permission to import it at module scope: `autosync` is imported by six
    modules (`__main__`, `dashboard_data`, `enrich`, `reflect`, `workspace`, and
    the scheduled hook), and a top-level `from . import pgmirror` would put the
    owner-DSN module on every one of those import graphs.

    Add a module to the allowlist and this test starts guarding it too."""
    for name in ("pgcli.py", "autosync.py"):
        assert _imports_are_all_inside_functions(name, "pgmirror"), \
            f"{name} imports pgmirror at module scope — it must be lazy"


def test_the_importer_scan_finds_real_edges():
    """CANARY. `_importers_of` returning [] for everything would make the
    assertion above pass no matter who imported what."""
    assert "pgstore.py" in _importers_of("paths"), \
        "the scan cannot see an import that certainly exists"
    assert _importers_of("no_such_module_anywhere") == []


def test_the_admin_module_does_not_import_the_reader_path():
    """`pgmirror` may use `pgstore`'s DSN helpers later, but it must never be
    reachable in the other direction — a cycle here would put the owner DSN one
    attribute lookup from the AI path."""
    assert "pgmirror" not in [n.split(".")[-1] for n in _module_imports("pgstore.py")]


def _module_imports(name: str) -> list[str]:
    out = []
    for node in ast.walk(ast.parse((SRC / name).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            out.extend(a.name for a in node.names)
            out.append(node.module or "")
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return [n for n in out if n]


# --------------------------------------------------------------------------
# the migration inventory
# --------------------------------------------------------------------------

def test_the_migrations_are_discoverable_in_application_order():
    """Applied in LEXICOGRAPHIC filename order, so the ordering is a property of
    the names — schema, then indexes, then roles and views, then the two a real
    server forced: 004 makes `mirror_state` readable by the reader role (003 had
    revoked the schema out from under it, so `mirror_health` could only ever
    fail closed) and hands the views to `mindhive_owner`; 005 drops the ordering
    index 002 created, because EXPLAIN showed it is never matched.

    The list is spelled out rather than derived, so ADDING a migration is a
    deliberate edit here — an inventory that updates itself proves only that the
    directory can be listed."""
    assert pgmirror.migration_files() == [
        "001_schema.sql", "002_indexes.sql", "003_roles_and_views.sql",
        "004_mirror_state_view_and_ownership.sql",
        "005_drop_unmatchable_order_index.sql",
        "006_event_embeddings.sql"]


def _executable_names(name: str) -> set[str]:
    """Every identifier a module actually EVALUATES.

    Prose has to be excluded rather than grepped around: this module documents
    the traps it avoids, so `__file__` and `TRUNCATE` both appear in its
    docstrings precisely because it does not use them. A scan that could not
    tell a decision from its explanation would force the code to stop explaining
    itself."""
    tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | \
        {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def _executed_strings(name: str) -> str:
    """Every string literal that is not a docstring, concatenated."""
    tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return "\n".join(n.value for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and n.value not in docstrings)


def test_migrations_are_read_as_package_data_not_from_a_path():
    """`importlib.resources`, not a path computed from `__file__`: the frozen
    PyInstaller build has no `__file__` directory to walk, and a migration
    runner that works only from a source checkout is one that fails exactly
    where an operator cannot debug it."""
    src = (SRC / "pgmirror.py").read_text(encoding="utf-8")
    assert "importlib.resources" in src or "from importlib import resources" in src
    assert "__file__" not in _executable_names("pgmirror.py")


def test_each_migration_hashes_to_its_own_content():
    """`apply_migrations` records this digest and REFUSES to continue when a
    recorded file's bytes have changed — silently re-running it and silently
    skipping it are both worse than stopping."""
    for name in pgmirror.migration_files():
        text = pgmirror.migration_text(name)
        assert pgmirror.migration_sha256(name) == \
            hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_every_digest_is_distinct():
    """CANARY. A `migration_sha256` that hashed the filename, or the empty
    string, would satisfy the test above for every file."""
    digests = {pgmirror.migration_sha256(n) for n in pgmirror.migration_files()}
    assert len(digests) == len(pgmirror.migration_files()) == 6


def test_an_unknown_migration_name_is_refused():
    with pytest.raises(Exception):
        pgmirror.migration_text("999_not_a_migration.sql")


# --------------------------------------------------------------------------
# fail closed while the server does not exist
# --------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda: pgmirror.apply_migrations("service=x"),
    lambda: pgmirror.sync_type_registry("service=x"),
    lambda: pgmirror.rebuild("proj"),
])
def test_the_unbuilt_write_paths_raise_rather_than_claim_success(call):
    """A skeleton that returned `{"ok": True}` would tell an operator a mirror
    was rebuilt that does not exist — and the parity gate would then be checking
    a claim rather than a database."""
    with pytest.raises(Exception) as e:
        call()
    assert "server" in str(e.value).lower() or "not" in str(e.value).lower()


def test_parity_answers_rather_than_raising_when_the_mirror_is_unreachable():
    """§9.4d: a DSN pointing at an unreachable port must make `parity()` report
    `ok=False`, not explode. The gate's whole job is to be trustworthy about the
    mirror's state, including the state 'I could not look'."""
    verdict = pgmirror.parity("proj")
    assert verdict["ok"] is False
    assert verdict["reason"]


def test_parity_never_reports_ok_from_a_comparison_it_did_not_make():
    """The non-vacuity guard the design opens with: two empty folds hash to the
    same head, so an empty/empty comparison must FAIL on the `count > 0` guard
    rather than pass on `"0"*64 == "0"*64`."""
    verdict = pgmirror.parity("a-namespace-that-does-not-exist")
    assert verdict["ok"] is False


def test_the_rebuild_path_never_reaches_for_truncate():
    """`rebuild` is `DELETE WHERE project` plus a bulk load, in one transaction.
    TRUNCATE cannot be scoped to a namespace, so a rebuild of one project would
    silently take every other project's rows with it — and MVCC would not even
    give concurrent readers a consistent old snapshot."""
    # Word-boundary, because `parity`'s refusal text contains "TRUNCATED": a
    # substring scan reads that prose as the statement it exists to forbid.
    assert not re.search(r"\bTRUNCATE\b",
                         _executed_strings("pgmirror.py").upper())


def test_the_executed_string_scan_can_see_a_real_statement():
    """CANARY. `_executed_strings` is what excuses the word TRUNCATE appearing
    in the docstring that explains why it is never used — a scanner that
    returned nothing would excuse the statement itself just as happily."""
    assert "no such migration" in _executed_strings("pgmirror.py"), \
        "the scan cannot see a string literal that certainly executes"
