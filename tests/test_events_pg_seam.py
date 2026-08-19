# tests/test_events_pg_seam.py
"""The ONE Postgres seam in `events.py`: repaired, provably neutral, and alone.

There were two, and both were dead. `_maybe_mirror_to_pg` and
`_maybe_read_from_pg` imported `scripts.core.event_write` — a module that lives
in a **different repository** and has never been on this process's path — inside
a bare `except`, so the import failed on every invocation and the failure was
swallowed. Flipping `MINDHIVE_EVENT_BACKEND` to `dual` or `pg` therefore did
nothing at all, silently, for as long as the seams existed.

Only the read seam was repaired. The write seam was DELETED, because it could
never have worked once repaired either: the module it would have called holds
the reader credential, and `mindhive_reader` is read-only at the ROLE level
(`003_roles_and_views.sql`). The mirror is advanced by `pgmirror.refresh_changed`
from the admin path instead — one loader, one credential. So the strongest claim
this file can make about the append path is not "it calls the right module" but
**"it reaches Postgres under no backend value at all"**, and that is what is
asserted below.

Two halves, pulling in opposite directions:

* **The seam works.** Under `pg` the read really reaches `pgstore`, same package.
  These assertions were RED before the repair — they are what proves the seam is
  no longer dead.
* **The seam is still free.** Under the default `file` backend the guard returns
  before any import, so `read_all` behaves exactly as it did. These assertions
  were green before the repair as well, and that is the point: they are a
  ratchet, not a discovery. Their canary is the guard itself — delete the
  `MINDHIVE_EVENT_BACKEND` check and they go red.

Nothing here touches a database, with one deliberate exception: the failure
counter is driven through a patched connector rather than a stubbed `pgstore`,
because a counter that only a stub increments proves nothing about the real path.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import events, pgstore

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "mindhive"
BACKEND = "MINDHIVE_EVENT_BACKEND"


@pytest.fixture(autouse=True)
def _default_backend(monkeypatch):
    """The suite must never inherit a backend from the ambient environment: a
    machine with `MINDHIVE_EVENT_BACKEND=dual` exported would otherwise run a
    different code path than CI and silently disagree with it."""
    monkeypatch.delenv(BACKEND, raising=False)
    pgstore.reset_state()
    yield
    pgstore.reset_state()


class SeamTouched(BaseException):
    """Raised when the file backend reaches `pgstore` at all.

    A `BaseException` rather than an `Exception`, and that is not fussiness — it
    is what makes the probe work. Both seams wrap their body in a blanket
    `except Exception`, so an ordinary exception raised by the stand-in below
    would be CAUGHT BY THE CODE UNDER TEST and the neutrality assertions would
    pass against a seam with no guard at all. That was observed, not
    anticipated: with the guard deleted and an `AssertionError` probe, only the
    structural test went red. `BaseException` passes straight through the
    blanket catch, which is the one place in this suite where the difference
    between the two hierarchies decides whether a test can bite."""


class _Raises:
    """Any attribute access explodes. Standing in for `pgstore` when the claim
    is that the seam never reaches it."""

    def __getattr__(self, name):
        raise SeamTouched(
            f"the file backend reached pgstore.{name} — the guard did not hold")


def _forbid_pgstore(monkeypatch) -> None:
    """Make any use of `pgstore` from the seam explode.

    Both the package attribute and the `events` module attribute are replaced,
    because `from . import pgstore` reads `mindhive.pgstore` — patching only the
    importer's namespace would leave the real module reachable and the
    assertion vacuous."""
    import mindhive
    monkeypatch.setattr(mindhive, "pgstore", _Raises(), raising=False)
    monkeypatch.setattr(events, "pgstore", _Raises(), raising=False)


# --------------------------------------------------------------------------
# the repair: the seam reaches pgstore, in this package
# --------------------------------------------------------------------------

def _imported_module_names(src: str) -> set[str]:
    """Every module name `src` imports, at any nesting depth."""
    import ast
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


def test_the_seams_import_no_module_from_another_repository():
    """RED before the repair. `scripts.core` is not in this distribution, is not
    on any path this process has, and cannot be installed from here — an import
    of it is not a soft dependency, it is a dead line.

    Asserted over the IMPORT NODES rather than over the file's text, because the
    docstring now names the defect on purpose and a substring scan would forbid
    the explanation along with the bug."""
    names = _imported_module_names((SRC / "events.py").read_text(encoding="utf-8"))
    offenders = [n for n in names if n.split(".")[0] == "scripts"
                 or n.endswith("event_write")]
    assert offenders == [], f"events.py imports from another repository: {offenders}"


def test_the_import_scan_would_notice_the_old_seam():
    """CANARY. The assertion above is an emptiness claim over an AST walk, and a
    walk that found no imports at all would satisfy it forever. Feed it the body
    that actually shipped."""
    assert "scripts.core" in _imported_module_names(
        "def _maybe_mirror_to_pg(ev):\n"
        "    from scripts.core import event_write\n")


# THE MIRROR-WRITE SEAM IS GONE, and three tests went with it:
# `..._calls_pgstore_under_dual`, `..._under_pg`, and a case-insensitivity check
# on the write guard. They asserted that `append_event` reached
# `pgstore.mirror_one` — a function that could never succeed, because `pgstore`
# holds the reader credential and `mindhive_reader` is read-only at the ROLE
# level. It returned False on every call for as long as it existed, and the
# tests passed anyway because they stubbed it.
#
# They are not replaced one-for-one. The property that replaced them is stronger
# and lives under neutrality below: the append path reaches `pgstore` under NO
# backend value, not merely under `file`. "There is no write seam" is a better
# thing to be able to assert than "the write seam calls the right module".


def test_the_read_backend_value_is_matched_case_insensitively(monkeypatch):
    """`.lower()` is in the surviving guard and stays byte-identical. An operator
    who exports `PG` gets the backend they asked for."""
    fixture = [events.Event(ts="2026-08-17T09:00:00Z", session_id="s1",
                            type="decision", payload={"text": "from pg"}, tags=[])]
    monkeypatch.setenv(BACKEND, "PG")
    monkeypatch.setattr(pgstore, "read_all_or_none", lambda project=None: fixture)
    assert events.read_all() is fixture


def test_the_read_seam_returns_the_mirrors_rows_under_pg(monkeypatch):
    """RED before the repair. `read_all` must hand back exactly what the mirror
    gave, not a merge and not a re-sort — the mirror already reads in ledger
    order and a second sort here would be a second ordering contract."""
    fixture = [events.Event(ts="2026-08-17T09:00:00Z", session_id="s1",
                            type="decision", payload={"text": "from pg"}, tags=[])]
    monkeypatch.setenv(BACKEND, "pg")
    monkeypatch.setattr(pgstore, "read_all_or_none", lambda project=None: fixture)
    events.append_event("decision", {"text": "on disk"})
    assert events.read_all() is fixture


def test_the_read_seam_falls_back_to_files_when_the_mirror_answers_none(monkeypatch):
    """`None` means "read the files", which is how reads stay total. An empty
    list would look like a namespace with no history, which is the one answer a
    degraded mirror must never give."""
    monkeypatch.setenv(BACKEND, "pg")
    monkeypatch.setattr(pgstore, "read_all_or_none", lambda project=None: None)
    events.append_event("decision", {"text": "on disk"})
    rows = events.read_all()
    assert [e.payload["text"] for e in rows] == ["on disk"]


def test_the_read_seam_stays_on_files_under_dual(monkeypatch):
    """`dual` writes both and reads files: the file store is still canonical
    until a cutover that this change does not take."""
    monkeypatch.setenv(BACKEND, "dual")
    monkeypatch.setattr(pgstore, "read_all_or_none",
                        lambda project=None: (_ for _ in ()).throw(
                            AssertionError("dual must not read from the mirror")))
    events.append_event("decision", {"text": "on disk"})
    assert len(events.read_all()) == 1


# --------------------------------------------------------------------------
# the hook-path guarantees: cannot raise, cannot be silent
# --------------------------------------------------------------------------
#
# `test_a_failing_mirror_cannot_break_an_append` and
# `test_a_mirror_failure_is_counted_rather_than_swallowed` were deleted with the
# write seam. The first is now vacuous by construction — `append_event` cannot be
# broken by a mirror it never consults — and the second's real subject was
# `pgstore`'s failure counter, which `tests/test_pgstore.py` exercises through
# the read seam that still reaches a socket.


def test_a_failing_read_cannot_break_a_fold(monkeypatch):
    """Same guarantee on the other seam. `read_all` feeds every briefing,
    recall, projection and dashboard fold."""
    monkeypatch.setenv(BACKEND, "pg")

    def boom(project=None):
        raise RuntimeError("psycopg exploded")

    monkeypatch.setattr(pgstore, "read_all_or_none", boom)
    events.append_event("decision", {"text": "on disk"})
    assert [e.payload["text"] for e in events.read_all()] == ["on disk"]


def test_a_real_read_failure_is_counted_rather_than_swallowed(monkeypatch):
    """The DIFFERENCE between this seam and the dead one it replaced. The old
    body swallowed its failure into a bare `except` and left no trace anywhere;
    this one is quiet on the hot path and visible on demand — `pgstore.degraded()`
    is what `hive pg status` prints.

    Driven through `events.read_all` with a real (patched) connector rather than
    a stubbed `pgstore`, so the assertion covers the whole path from the seam to
    the counter, which is the part a module-level test cannot reach."""
    monkeypatch.setenv(BACKEND, "pg")
    monkeypatch.setenv("MINDHIVE_PG_DSN_RO", "service=mindhive_ro")
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    monkeypatch.setattr(pgstore, "_connect_raw",
                        lambda d: (_ for _ in ()).throw(OSError("no such host")))
    events.append_event("decision", {"text": "x"})
    assert [e.payload["text"] for e in events.read_all()] == ["x"], \
        "the fold did not fall back to the files"
    d = pgstore.degraded()
    assert d["failures"] == 1, "a real mirror failure left no counter behind"
    assert "no such host" in d["last_error"]


# --------------------------------------------------------------------------
# neutrality under the default backend — the ratchet half
# --------------------------------------------------------------------------

def test_the_file_backend_never_touches_pgstore_at_all(monkeypatch):
    """The load-bearing property, asserted rather than claimed: the guard reads
    the environment and returns BEFORE any import, so at the default backend the
    seam costs one `os.environ.get` and one comparison. `pgstore` is bound to an
    object that raises on any attribute access — if a single line of the seam
    body ran, this goes red."""
    _forbid_pgstore(monkeypatch)
    ev = events.append_event("decision", {"text": "file only"})
    assert ev.type == "decision"
    assert [e.payload["text"] for e in events.read_all()] == ["file only"]


def test_the_append_path_touches_pgstore_under_no_backend_whatsoever(monkeypatch):
    """STRONGER than the test above, and the one that replaces the three deleted
    write-seam tests. Neutrality under `file` is a property of a guard; this is a
    property of the code's SHAPE — there is no write seam, so no value of
    `MINDHIVE_EVENT_BACKEND` can make an append consult Postgres.

    Reads are deliberately excluded from the loop: under `pg` reaching `pgstore`
    is the feature, not the defect. Only `append_event` is exercised."""
    _forbid_pgstore(monkeypatch)
    for backend in ("file", "dual", "pg", "PG", "Dual", "postgres", ""):
        monkeypatch.setenv(BACKEND, backend)
        ev = events.append_event("decision", {"text": f"appended under {backend!r}"})
        assert ev.type == "decision"
    monkeypatch.delenv(BACKEND, raising=False)
    assert len(events.read_all()) == 7


def test_an_unset_backend_and_an_explicit_file_behave_identically(monkeypatch):
    """`file` is both the default and a legal explicit value, and the guard has
    to treat them the same — an operator who writes the default down must not
    get a different code path than one who leaves it out."""
    monkeypatch.setenv(BACKEND, "file")
    _forbid_pgstore(monkeypatch)
    events.append_event("decision", {"text": "explicit file"})
    assert len(events.read_all()) == 1


def test_an_unknown_backend_value_stays_on_files(monkeypatch):
    """A typo (`postgres`, `pgsql`) must not silently enable a mirror. The guard
    is an allowlist of two, not a check for "not file"."""
    _forbid_pgstore(monkeypatch)
    for bogus in ("postgres", "pgsql", "", "  "):
        monkeypatch.setenv(BACKEND, bogus)
        events.append_event("decision", {"text": bogus or "blank"})
        events.read_all()


def test_importing_events_does_not_import_a_postgres_driver():
    """`import mindhive.events` reaches `pgstore` through the seam, and must not
    pay for `psycopg` — nor for `mcp`. Run in a subprocess so the answer is about
    a fresh interpreter rather than about whatever this suite imported earlier.

    PYTHONPATH points at THIS worktree's `src`: the venv's editable install
    resolves to the main checkout, so without it the subprocess would answer for
    a different branch's code."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    out = subprocess.run(
        [sys.executable, "-c",
         "import mindhive.events, sys; "
         "print('psycopg' in sys.modules, 'mcp' in sys.modules)"],
        capture_output=True, text=True, cwd=str(REPO), env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False", out.stdout


def test_an_append_under_the_file_backend_imports_no_driver():
    """The import above is the cheap half. This one runs the seam: an actual
    `append_event` and an actual `read_all` under the default backend must still
    leave `psycopg` unimported."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"),
               MINDHIVE_PROJECT="seamtest")
    env.pop(BACKEND, None)
    out = subprocess.run(
        [sys.executable, "-c",
         "import tempfile, os, sys\n"
         "os.environ['MINDHIVE_HOME'] = tempfile.mkdtemp()\n"
         "from mindhive import events\n"
         "events.append_event('decision', {'text': 'x'})\n"
         "events.read_all()\n"
         "print('psycopg' in sys.modules)\n"],
        capture_output=True, text=True, cwd=str(REPO), env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", out.stdout


def test_an_append_under_the_pg_backend_imports_no_driver_either():
    """The subprocess form of the every-backend claim. `MINDHIVE_EVENT_BACKEND=pg`
    is the most aggressive value an operator can set, and an append under it must
    STILL leave `psycopg` unimported — because there is nothing on the append path
    that could import it.

    This is the one that would go red if a write seam were ever reintroduced:
    a stubbed-`pgstore` test cannot notice a driver import, and a fresh
    interpreter is the only place the question has an honest answer."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"),
               MINDHIVE_PROJECT="seamtest", **{BACKEND: "pg"})
    out = subprocess.run(
        [sys.executable, "-c",
         "import tempfile, os, sys\n"
         "os.environ['MINDHIVE_HOME'] = tempfile.mkdtemp()\n"
         "from mindhive import events\n"
         "events.append_event('decision', {'text': 'x'})\n"
         "print('psycopg' in sys.modules)\n"],
        capture_output=True, text=True, cwd=str(REPO), env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", out.stdout


def test_the_guard_is_the_first_statement_of_the_surviving_seam():
    """Structural, because the neutrality tests above can only prove that
    TODAY's body returns early. The guard has to precede the import textually as
    well, or a later edit that hoists `from . import pgstore` to the top of the
    function keeps every assertion green while making `import mindhive.events`
    pay for the driver."""
    import ast
    tree = ast.parse((SRC / "events.py").read_text(encoding="utf-8"))
    name = "_maybe_read_from_pg"
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    body = [n for n in fn.body if not (isinstance(n, ast.Expr)
                                       and isinstance(n.value, ast.Constant))]
    first = body[0]
    assert not isinstance(first, (ast.Import, ast.ImportFrom)), \
        f"{name} imports before reading MINDHIVE_EVENT_BACKEND"
    assert BACKEND in ast.dump(first), \
        f"{name}'s first statement is not the backend guard"


def test_the_write_seam_is_gone_and_no_new_one_has_appeared():
    """The deletion, made mechanical. `events.py` must define exactly one
    Postgres seam; a helpfully-reintroduced `_maybe_mirror_to_pg` — or any other
    function on the append path that calls into `pgstore` — fails here rather
    than at a role-permission error nobody reads.

    Asserted over the module's own function names AND over `append_event`'s body,
    because a second seam could be spelled anything at all."""
    import ast
    tree = ast.parse((SRC / "events.py").read_text(encoding="utf-8"))
    seams = sorted(n.name for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and "_pg" in n.name)
    assert seams == ["_maybe_read_from_pg"], \
        f"events.py grew a second Postgres seam: {seams}"

    append = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "append_event")
    called = {n.func.attr for n in ast.walk(append)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    called |= {n.func.id for n in ast.walk(append)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    offenders = sorted(c for c in called if "pg" in c.lower() or "mirror" in c.lower())
    assert offenders == [], f"append_event calls a mirror seam: {offenders}"
