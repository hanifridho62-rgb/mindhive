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
    index 002 created, because EXPLAIN showed it is never matched; 006 stores
    the vectors; 007 appends `append_seq`, the stamp that orders a same-second
    tie by write order rather than by segment filename.

    The list is spelled out rather than derived, so ADDING a migration is a
    deliberate edit here — an inventory that updates itself proves only that the
    directory can be listed."""
    assert pgmirror.migration_files() == [
        "001_schema.sql", "002_indexes.sql", "003_roles_and_views.sql",
        "004_mirror_state_view_and_ownership.sql",
        "005_drop_unmatchable_order_index.sql",
        "006_event_embeddings.sql", "007_append_seq.sql"]


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
    assert len(digests) == len(pgmirror.migration_files()) == 7


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


# --- the loader's line index, which needs no server ---------------------------
#
# `scan_segments` is pure file reading, so it belongs in this default-tier file
# rather than the pg tier: the number it produces is stored in Postgres as
# `seg_line`, part of `UNIQUE (project, segment, seg_line)` on `mindhive.events`
# and of `event_embeddings`' primary key, and nothing downstream would notice it
# drifting. `pgmirror.parity` discards the index entirely (it folds Events), and
# the staleness fingerprint is `st_size` per segment -- a split-semantics change
# preserves every byte. So the value has to be pinned here or nowhere.

def _three_rows_one_carrying_a_separator(sep="\u2028"):
    from mindhive import events
    for i, text in enumerate(["before", "mid" + sep + "dle", "after"]):
        events.append_event("decision", {"text": text}, session_id="s",
                            ts="2026-06-20T10:00:0%d" % i + "Z")


def test_scan_segments_seg_line_is_the_physical_newline_index():
    """Three rows, three newlines, so three consecutive indices. Under
    `splitlines()` the middle row tore in two and `after` was numbered 3 -- a
    stored join key off by one for every row after any separator-bearing row."""
    _three_rows_one_carrying_a_separator()
    rows = pgmirror.scan_segments("test")
    assert [(seg, ln, ev.payload["text"]) for seg, ln, ev, _ in rows] == [
        ("s.jsonl", 0, "before"),
        ("s.jsonl", 1, "mid\u2028dle"),
        ("s.jsonl", 2, "after"),
    ]


def test_the_mirror_loader_and_the_chain_scan_index_every_row_alike():
    """The cross-module invariant the four readers and the chain must hold: one
    row, one index, one meaning. They forked when `chain._scan` moved to
    `split("\\n")` and the loader stayed on `splitlines()`; both now go through
    `events._segment_lines`, and this is what says so."""
    from mindhive import chain, events
    _three_rows_one_carrying_a_separator()
    scan_rows, _ = chain._scan("test")
    assert [(s, i) for s, i, _, _ in scan_rows] == [
        (s, i) for s, i, _, _ in pgmirror.scan_segments("test")]
    assert len(scan_rows) == len(events.read_all("test")) == 3


def test_the_chain_excludes_an_unterminated_tail_the_readers_still_show():
    """The ONE place the chain and the readers deliberately differ, pinned here so
    it is not "fixed" in either direction. They split by the same rule and give
    every shared row the same index -- but a row with no terminating newline is
    still MUTABLE (the next append concatenates onto it), so the seal must not
    cover it, while a total read must still show what is on disk. That is a
    different row SET, not a different splitter."""
    from mindhive import chain, events, paths
    events.append_event("decision", {"text": "complete"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    tail = events.canonical_json({"ts": "2026-06-20T10:00:01Z", "type": "decision",
                                  "session_id": "s", "payload": {"text": "torn"},
                                  "tags": []})
    with open(paths.events_dir("test") / "s.jsonl", "a", encoding="utf-8") as f:
        f.write(tail)                              # parseable, but NO trailing \n

    scan_rows, lines = chain._scan("test")
    assert [(s, i) for s, i, _, _ in scan_rows] == [("s.jsonl", 0)]  # tail not sealed
    assert lines == {"s.jsonl": 1}
    assert [(s, i, e.payload["text"]) for s, i, e, _ in pgmirror.scan_segments("test")] == [
        ("s.jsonl", 0, "complete"), ("s.jsonl", 1, "torn")]        # tail visible
    assert [e.payload["text"] for e in events.read_all("test")] == ["complete", "torn"]


# --- the append stamp, which the loader stores as `append_seq` ----------------
#
# Same reason as the line index above: `scan_segments` is the order the loader
# COPYs and the file side of the parity gate, and a gate comparing two sides that
# share a wrong tie-break passes. So the order it produces is pinned here, against
# `events.read_all`, with no server.

SAME_SECOND = "2026-06-20T10:00:00Z"


def _same_second_pair():
    """`zzz-writer` appends first, `aaa-editor` second, inside one second.

    Filename order is the REVERSE of append order for these two, which is the
    only shape where breaking the tie on the segment name is visibly wrong."""
    from mindhive import events
    events.append_event("decision", {"text": "first"}, session_id="zzz-writer",
                        ts=SAME_SECOND)
    events.append_event("decision", {"text": "second"}, session_id="aaa-editor",
                        ts=SAME_SECOND)


def test_scan_segments_orders_a_same_second_tie_by_append_stamp_like_read_all():
    from mindhive import events
    _same_second_pair()
    rows = pgmirror.scan_segments("test")
    assert [ev.payload["text"] for _, _, ev, _ in rows] == ["first", "second"]
    assert [ev for _, _, ev, _ in rows] == events.read_all("test")
    seqs = [seq for _, _, _, seq in rows]
    assert all(type(s) is int for s in seqs) and seqs[0] < seqs[1], seqs


def test_the_same_second_pair_really_inverts_under_the_old_mirror_key():
    """CANARY. The key this replaced, `(ts, segment, seg_line)`, must put the
    fixture in the wrong order -- otherwise the test above passes for a loader
    that never looked at the stamp."""
    _same_second_pair()
    rows = pgmirror.scan_segments("test")
    old = sorted(rows, key=lambda t: ((t[2].ts or ""), t[0], t[1]))
    assert [ev.payload["text"] for _, _, ev, _ in old] == ["second", "first"]


def test_an_unstamped_row_reaches_the_loader_as_none_not_as_the_sentinel():
    """`append_seq` stores NULL for a row with no stamp. The -1 belongs to the
    ORDER BY; a loader that stored it would make 'never stamped' look like a
    stamp to anyone querying the column."""
    from mindhive import events, paths
    d = paths.events_dir("test")
    d.mkdir(parents=True, exist_ok=True)
    legacy = events.canonical_json({"ts": SAME_SECOND, "type": "decision",
                                    "session_id": "legacy",
                                    "payload": {"text": "old"}, "tags": []})
    (d / "legacy.jsonl").write_text(legacy + "\n", encoding="utf-8")
    assert [(t[0], t[3]) for t in pgmirror.scan_segments("test")] == [
        ("legacy.jsonl", None)]


def test_scan_segments_carries_a_stamp_down_its_segment_like_read_all():
    """An unstamped row BELOW a stamped one in its segment -- a pre-`seq`
    process appending to `manual.jsonl` after a stamping one -- is ordered by
    the stamp above it (`events._segment_events`), and that carried value is
    what the loader stores as `append_seq`. Stored raw, the row would be NULL,
    `COALESCE(append_seq, -1)` would sort it first, and the mirror would replay
    the update before its add while the file readers did not."""
    from mindhive import events, paths
    events.append_event("decision", {"text": "add"}, session_id="manual",
                        ts=SAME_SECOND)
    late = events.canonical_json({"ts": SAME_SECOND, "type": "decision",
                                  "session_id": "manual",
                                  "payload": {"text": "update"}, "tags": []})
    with open(paths.events_dir("test") / "manual.jsonl", "a",
              encoding="utf-8") as f:
        f.write(late + "\n")

    rows = pgmirror.scan_segments("test")
    assert [ev.payload["text"] for _, _, ev, _ in rows] == ["add", "update"]
    assert [ev for _, _, ev, _ in rows] == events.read_all("test")
    assert type(rows[0][3]) is int and rows[1][3] == rows[0][3], rows


class _FakeStateConn:
    """Just enough of a psycopg connection for `recorded_fingerprint`."""

    def __init__(self, row):
        self.row, self.sql = row, []

    def cursor(self):
        conn = self

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                conn.sql.append(sql)

            def fetchone(self):
                return conn.row

        return _Cursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


_RECORDED = {"segments": {"s.jsonl": 10}, "bytes": 10}


@pytest.mark.parametrize("version", [1, None, "2"])
def test_a_mirror_stamped_by_another_schema_version_has_no_usable_fingerprint(
        monkeypatch, version):
    """Version 2 added `append_seq`. A mirror loaded at version 1 holds NULL in
    it for EVERY row, so reloading only the segments whose bytes moved would
    leave the rest ordered as unstamped rows while the fingerprint called them
    current. None is what makes `refresh_changed` reload everything."""
    monkeypatch.setenv(ADMIN_ENV, "service=fake")
    monkeypatch.setattr(pgmirror, "_connect_admin",
                        lambda *a, **k: _FakeStateConn((_RECORDED, version)))
    assert pgmirror.recorded_fingerprint("test") is None


def test_a_mirror_stamped_by_this_schema_version_keeps_its_fingerprint(
        monkeypatch):
    """CANARY. A `recorded_fingerprint` that answered None for everything would
    pass the test above and quietly turn every refresh into a full reload."""
    monkeypatch.setenv(ADMIN_ENV, "service=fake")
    fake = _FakeStateConn((_RECORDED, pgmirror.SCHEMA_VERSION))
    monkeypatch.setattr(pgmirror, "_connect_admin", lambda *a, **k: fake)
    assert pgmirror.recorded_fingerprint("test") == _RECORDED
    assert "schema_version" in fake.sql[0]


# --- only a LOAD may vouch for what was loaded --------------------------------
#
# `mirror_state.schema_version` and `ledger_fingerprint` are claims about a LOAD:
# which loader filled the rows, and which ledger bytes it read. `parity` proves
# the rows agree with the ledger, but it sees neither fact, so a parity run with
# no load behind it has to leave both as stored.
#
# The sequence that made this matter, from review of the append-stamp change:
# the mirror sits at schema version 1; every autosync refresh fails until
# `hive pg migrate` runs, because the COPY names a column 007 has not added yet;
# rows appended in that window never reach the mirror. The operator migrates,
# then runs `hive pg parity` as a check, and it passes on a correct prefix. When
# that parity stamped version 2 and the ledger's CURRENT fingerprint, the version
# gate meant to force a full reload was cleared, `changed_segments` answered [],
# and a closed segment's window rows were never mirrored while freshness called
# the mirror fresh.

_STATE_COLUMNS = ("project", "schema_version", "row_count", "file_head",
                  "pg_head", "parity_ok", "ledger_fingerprint")


# Matched against `_canonical_sql`'s output: lower case, no space beside
# `( ) , . =`.
_UPSERT_SHAPE = re.compile(
    r"insert into mindhive\.mirror_state\(([^)]*)\)values\((.*?)\)"
    r"on conflict\(project\)do update set (.*)")
_SET_FROM_EXCLUDED = re.compile(r"(\w+)=excluded\.(\w+)")
_SET_REBUILT_AT = re.compile(r"rebuilt_at=now\(\)")


def _canonical_sql(sql):
    """*sql* as Postgres's lexer sees it, so that only a reformat Postgres runs
    differently can change what the shape check reads.

    A `--` or `/* */` comment is whitespace, taken left to right so a `/*`
    inside a `--` comment opens nothing. Whitespace collapses, and goes
    entirely beside `( ) , . = ;`. One trailing `;` is dropped. An unquoted
    word folds to lower case, but not a placeholder's letter, so `%S` is
    refused as psycopg refuses it. A double-quoted `\\w+` identifier loses its
    quotes but keeps its case and stays one token, so `"Parity_OK"` names no
    column and `"row_"count` is two words. String literals, nested comments
    and a quoted keyword (`"set"`) are not modelled: no statement the fake
    reads has any."""
    s = re.sub(r"/\*.*?\*/|--[^\n]*", " ", sql, flags=re.DOTALL)
    s = re.sub(r'"(\w+)"|(?<![%\w])\w+',
               lambda m: f" {m.group(1)} " if m.group(1) else m.group().lower(), s)
    s = re.sub(r"\s*([(),.=;])\s*", r"\1", " ".join(s.split()))
    return s[:-1] if s.endswith(";") else s


class _FakeMirrorState:
    """`mindhive.mirror_state` as a dict, driven by the statements pgmirror sends.

    Not a SQL engine. It answers `_RECORDED_FP_SQL` and models one other shape,
    read through `_canonical_sql`:
    `INSERT INTO mindhive.mirror_state (cols) VALUES (...) ON CONFLICT (project)
    DO UPDATE SET items`, each value `%s` or `now()`, each item exactly
    `col = EXCLUDED.col` for a column the INSERT binds, or `rebuilt_at = now()`.
    Anything else -- another statement, an expression in an item
    (`parity_ok = EXCLUDED.parity_ok AND mirror_state.parity_ok`), a `WHERE` on
    the update, a column assigned twice or from another column -- fails the test
    through `pytest.fail`, which raises past `_record_state`'s
    `except Exception` rather than becoming `recorded: False`.

    It used to collect `col = EXCLUDED.col` matches anywhere in the raw text. A
    `--`-commented item still matched, so every test passed on a `_VERDICT_SQL`
    that Postgres runs without updating that column. The shape check that
    replaced it stripped only `--` comments and matched literal spacing and
    case, so `ON CONFLICT(project)`, `excluded.`, `NOW()`, a trailing `;` or a
    `/* */` line -- all run identically by Postgres -- failed every verdict
    test. A `Jsonb` parameter is stored as the value it wraps, which is what
    reading the column back returns."""

    def __init__(self, rows=None):
        self.rows = {k: dict(v) for k, v in (rows or {}).items()}
        self.statements = []

    def connect(self, *a, **k):
        return _FakeStateDbConn(self)

    def execute(self, sql, params):
        self.statements.append((sql, params))
        if sql == pgmirror._RECORDED_FP_SQL:
            row = self.rows.get(params[0])
            return None if row is None else (row["ledger_fingerprint"],
                                             row["schema_version"])
        shape = _UPSERT_SHAPE.fullmatch(_canonical_sql(sql))
        if not shape:
            pytest.fail(f"a statement this fake does not model: {sql!r}")
        cols = [c.strip() for c in shape.group(1).split(",")]
        values = [v.strip() for v in shape.group(2).split(",")]
        if len(values) != len(cols) or values.count("%s") != len(params) \
                or set(values) - {"%s", "now()"}:
            pytest.fail(f"VALUES this fake does not model: {sql!r}")
        new = {c: getattr(p, "obj", p)
               for c, p in zip([c for c, v in zip(cols, values) if v == "%s"],
                               params)}
        if set(_STATE_COLUMNS) - set(new):
            pytest.fail(f"an INSERT that binds too few columns: {sql!r}")
        updated = []
        for item in (i.strip() for i in shape.group(3).split(",")):
            pair = _SET_FROM_EXCLUDED.fullmatch(item)
            if pair and pair.group(1) == pair.group(2) and pair.group(1) in new:
                updated.append(pair.group(1))
            elif not _SET_REBUILT_AT.fullmatch(item):
                pytest.fail(f"a SET item this fake does not model: {item!r}")
        if len(updated) != len(set(updated)):
            pytest.fail(f"a column assigned twice: {sql!r}")
        if new["project"] in self.rows:
            self.rows[new["project"]].update({c: new[c] for c in updated})
        else:
            self.rows[new["project"]] = {c: new[c] for c in _STATE_COLUMNS}
        return None


class _FakeStateDbConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        db = self.db

        class _Cursor:
            _row = None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                self._row = db.execute(sql, params)

            def fetchone(self):
                return self._row

        return _Cursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _loaded_then_a_window():
    """Two rows a load read (`aaa-old`), then two it never saw (`zzz-window`).

    Returns the fingerprint that load took and the rows the mirror holds, which
    are exactly the ledger's first two: a correct prefix, so parity passes."""
    from mindhive import events
    for i in range(2):
        events.append_event("decision", {"text": f"old-{i}"},
                            session_id="aaa-old", ts=f"2026-06-20T10:00:0{i}Z")
    loaded = pgmirror._fingerprint("test")
    mirrored = [t[2] for t in pgmirror.scan_segments("test")]
    for i in range(2, 4):
        events.append_event("decision", {"text": f"window-{i}"},
                            session_id="zzz-window",
                            ts=f"2026-06-20T10:00:0{i}Z")
    return loaded, mirrored


def _state_row(version, fingerprint, parity_ok=False):
    """A stored `mirror_state` row for namespace `test`.

    Every verdict column holds a value the fixture's parity run cannot produce:
    99 rows, heads no fold hashes to, and by default a FAILED verdict. Seeded
    with the verdict's own values (2 rows and a pass, as it once was), a
    `_VERDICT_SQL` that dropped `row_count`, `file_head` or `parity_ok` from its
    update list left each column already right, and every test passed."""
    return {"test": {"project": "test", "schema_version": version,
                     "row_count": 99, "file_head": "0" * 64,
                     "pg_head": "0" * 64, "parity_ok": parity_ok,
                     "ledger_fingerprint": fingerprint}}


# `mirror_state` column -> the verdict key `parity` records in it.
_VERDICT_COLUMNS = {"row_count": "rows", "file_head": "file_head",
                    "pg_head": "pg_head", "parity_ok": "ok"}


def _assert_verdict_recorded(seeded, row, verdict):
    """Each verdict column now holds the verdict's value, and the seed did not
    already: a column the upsert never updated fails on the second assert
    instead of passing on what the fixture held."""
    for col, key in _VERDICT_COLUMNS.items():
        assert seeded[col] != verdict[key], f"the seed already holds {col}"
        assert row[col] == verdict[key], f"the verdict's {col} was not recorded"


def _serve(monkeypatch, db, mirrored):
    monkeypatch.setenv(ADMIN_ENV, "service=fake")
    monkeypatch.setattr(pgmirror, "_connect_admin", db.connect)
    monkeypatch.setattr(pgstore, "read_all_strict",
                        lambda project=None: list(mirrored))


def test_a_parity_run_alone_never_clears_the_version_gate(monkeypatch):
    """The review's sequence, after `hive pg migrate`: a version-1 row, two rows
    the mirror never loaded, and a standalone `parity()` that passes on a correct
    prefix. The verdict columns are parity's to write; the version and the
    fingerprint are not, so the next refresh must still reload everything.

    Kills the `_record_state` that upserted `SCHEMA_VERSION` and a fresh
    `_fingerprint(name)` on every verdict, and a `_VERDICT_SQL` whose update
    list drops any one of `row_count`, `file_head`, `pg_head` or `parity_ok`."""
    loaded, mirrored = _loaded_then_a_window()
    db = _FakeMirrorState(_state_row(1, loaded))
    seeded = dict(db.rows["test"])
    _serve(monkeypatch, db, mirrored)
    assert pgmirror.recorded_fingerprint("test") is None, \
        "the fixture's version gate is not armed"

    verdict = pgmirror.parity("test")

    assert (verdict["ok"], verdict["exact"], verdict["lag"]) == (True, False, 2)
    assert verdict["recorded"] is True
    row = db.rows["test"]
    assert row["schema_version"] == 1, \
        "parity raised the schema version with no load behind it"
    assert row["ledger_fingerprint"] == loaded, \
        "parity stamped ledger bytes no load ever read"
    assert pgmirror.changed_segments(
        "test", pgmirror.recorded_fingerprint("test")) is None, \
        "the next refresh would reload nothing and never mirror the window rows"
    _assert_verdict_recorded(seeded, row, verdict)


def test_a_parity_run_that_follows_a_load_records_that_load(monkeypatch):
    """CANARY for the test above. A `_record_state` that never wrote the version
    or the fingerprint passes it too, and leaves every refresh a full reload
    forever. Also kills recording the LIVE fingerprint in place of the one the
    load took: that would call `zzz-window` loaded, and it was not.

    The verdict columns are asserted too. Every load records through
    `_STATE_SQL`, not `_VERDICT_SQL`, and a `_STATE_SQL` that dropped
    `row_count`, `file_head`, `pg_head` or `parity_ok` from its update list
    passed every other test while the stored row kept the seed's values."""
    loaded, mirrored = _loaded_then_a_window()
    db = _FakeMirrorState(_state_row(1, {"segments": {}, "bytes": 0}))
    seeded = dict(db.rows["test"])
    _serve(monkeypatch, db, mirrored)

    verdict = pgmirror.parity("test", loaded_fingerprint=loaded)

    assert verdict["recorded"] is True
    assert db.rows["test"]["schema_version"] == pgmirror.SCHEMA_VERSION
    assert pgmirror.recorded_fingerprint("test") == loaded
    assert pgmirror.changed_segments(
        "test", pgmirror.recorded_fingerprint("test")) == ["zzz-window.jsonl"]
    _assert_verdict_recorded(seeded, db.rows["test"], verdict)


def test_a_diverged_parity_run_after_a_load_overwrites_a_stored_pass(monkeypatch):
    """The load's upsert in the failing direction. `refresh_changed` records its
    verdict through `_STATE_SQL`, so a load whose parity FAILS must turn a stored
    pass False: `mirror_health` serves that column and
    `pgtools._parity_refusal` refuses on it. The mirror hands back its two rows
    in reverse -- the same count, a different chain.

    Kills a `_STATE_SQL` that dropped `parity_ok` from its update list, under
    which a diverged mirror kept the pass an earlier load recorded."""
    loaded, mirrored = _loaded_then_a_window()
    db = _FakeMirrorState(_state_row(pgmirror.SCHEMA_VERSION, loaded,
                                     parity_ok=True))
    seeded = dict(db.rows["test"])
    _serve(monkeypatch, db, list(reversed(mirrored)))

    verdict = pgmirror.parity("test", loaded_fingerprint=loaded)

    assert (verdict["ok"], verdict["exact"], verdict["lag"]) == (False, False, 2)
    assert "diverges" in verdict["reason"], verdict["reason"]
    assert verdict["recorded"] is True
    row = db.rows["test"]
    assert row["parity_ok"] is False, \
        "a load whose verdict failed left the stored pass standing"
    _assert_verdict_recorded(seeded, row, verdict)


def test_a_diverged_parity_run_overwrites_a_stored_pass(monkeypatch):
    """A standalone `parity()` that FAILS, over a row whose last load recorded a
    pass at this schema version. The stored `parity_ok` is what `mirror_health`
    serves and `pgtools._parity_refusal` refuses on, so it must turn False here,
    while the version and the fingerprint stay as that load stamped them.

    The mirror hands back its two rows in reverse: the same count, a different
    chain. Kills a `_VERDICT_SQL` that dropped `parity_ok` from its update list,
    under which the load's pass kept being served beside a verdict of divergence
    (and for an in-place edit that kept every segment's byte count, with no
    stale notice either), and one that updated the load's two columns as well."""
    loaded, mirrored = _loaded_then_a_window()
    db = _FakeMirrorState(_state_row(pgmirror.SCHEMA_VERSION, loaded,
                                     parity_ok=True))
    seeded = dict(db.rows["test"])
    _serve(monkeypatch, db, list(reversed(mirrored)))

    verdict = pgmirror.parity("test")

    assert (verdict["ok"], verdict["exact"], verdict["lag"]) == (False, False, 2)
    assert "diverges" in verdict["reason"], verdict["reason"]
    assert verdict["recorded"] is True
    row = db.rows["test"]
    assert row["parity_ok"] is False, \
        "a failed verdict left the load's pass standing"
    _assert_verdict_recorded(seeded, row, verdict)
    assert (row["schema_version"], row["ledger_fingerprint"]) == (
        pgmirror.SCHEMA_VERSION, loaded), \
        "a verdict rewrote what only a load may record"


def test_a_first_verdict_with_no_load_behind_it_is_stamped_unloaded(monkeypatch):
    """No row yet, and no load. Both columns are NOT NULL (001_schema.sql), so
    something must be written: a version no loader ever records, and a JSON
    `null` fingerprint -- a value, so the constraint holds, and one
    `recorded_fingerprint` reads as nothing recorded."""
    _, mirrored = _loaded_then_a_window()
    db = _FakeMirrorState()
    _serve(monkeypatch, db, mirrored)

    assert pgmirror.parity("test")["recorded"] is True

    row = db.rows["test"]
    assert row["schema_version"] == pgmirror.UNLOADED_SCHEMA_VERSION
    assert pgmirror.UNLOADED_SCHEMA_VERSION != pgmirror.SCHEMA_VERSION
    assert row["ledger_fingerprint"] is None
    sent = db.statements[-1][1][_STATE_COLUMNS.index("ledger_fingerprint")]
    assert sent is not None, \
        "a Python None binds as SQL NULL, which the NOT NULL column refuses"
    assert pgmirror.recorded_fingerprint("test") is None


def _verdict_sql_with(pattern, repl, flags=0):
    """`_VERDICT_SQL` with *pattern* replaced exactly once: a reformat that
    moves the text fails here, not by leaving the mutation a no-op."""
    sql, n = re.subn(pattern, repl, pgmirror._VERDICT_SQL, flags=flags)
    assert n == 1, f"{pattern!r} matched {n} times in _VERDICT_SQL"
    return sql


# Valid upserts Postgres runs, or refuses, differently from a bare
# `col = EXCLUDED.col` list. `AND` keeps a stored False after a pass, `WHERE`
# skips the update on a stored pass, a second assignment is an error, and a
# crossed one writes the mirror's head into `file_head`.
_UNMODELLED_VERDICT_SQL = {
    "and": (r"(parity_ok\s*=\s*EXCLUDED\.parity_ok)",
            r"\1 AND mirror_state.parity_ok"),
    "where": (r"(rebuilt_at\s*=\s*now\(\))",
              r"\1 WHERE mirror_state.parity_ok IS NOT TRUE"),
    "twice": (r"(parity_ok\s*=\s*EXCLUDED\.parity_ok,)", r"\1 \1"),
    "crossed": (r"(file_head\s*=\s*EXCLUDED\.)file_head", r"\1pg_head"),
}


@pytest.mark.parametrize("mutation", sorted(_UNMODELLED_VERDICT_SQL))
def test_the_state_fake_refuses_an_upsert_it_does_not_model(mutation):
    """The verdict tests above are only as strict as this fake. The raw-text
    regex it replaced recorded each of these as a plain update, so a
    `_VERDICT_SQL` carrying one passed every test while Postgres would store
    something else."""
    sql = _verdict_sql_with(*_UNMODELLED_VERDICT_SQL[mutation])
    db = _FakeMirrorState(_state_row(pgmirror.SCHEMA_VERSION, None))
    with pytest.raises(pytest.fail.Exception):
        db.execute(sql, ("test", 1, 2, "a" * 64, "b" * 64, True, None))


def test_the_state_fake_reads_a_commented_set_item_as_not_updated():
    """Postgres discards a `--` comment before it parses, so a commented-out
    item updates nothing. The old fake matched the commented text and recorded
    the column anyway, and every verdict test passed on that `_VERDICT_SQL`."""
    sql = _verdict_sql_with(r"^(\s*)(parity_ok\s*=)", r"\1-- \2", re.MULTILINE)
    db = _FakeMirrorState(_state_row(pgmirror.SCHEMA_VERSION, None))

    db.execute(sql, ("test", 1, 2, "a" * 64, "b" * 64, True, None))

    assert db.rows["test"]["parity_ok"] is False, \
        "a commented-out SET item was recorded as an update"
    assert db.rows["test"]["row_count"] == 2


def test_refresh_changed_diffs_loads_and_verifies_against_one_snapshot(
        monkeypatch):
    """One `_fingerprint` for the whole run, handed to all three steps. A second
    one taken for the verdict, after the load, would record bytes that arrived
    in between as loaded. Kills `parity(name)` with no fingerprint, and a
    `changed_segments` that stats the ledger a second time."""
    snapshot = {"segments": {"s.jsonl": 10}, "bytes": 10}
    taken, seen = [], {}
    monkeypatch.setattr(pgmirror, "_fingerprint",
                        lambda project: taken.append(project) or snapshot)
    monkeypatch.setattr(pgmirror, "recorded_fingerprint",
                        lambda project: {"segments": {"s.jsonl": 4}, "bytes": 4})

    def load(project, segments=None, fingerprint=None):
        seen["segments"], seen["loaded"] = segments, fingerprint
        return {"project": project, "rows": 0, "deleted": 0,
                "segments": segments, "fingerprint": fingerprint}

    def verify(project=None, loaded_fingerprint=None):
        seen["verified"] = loaded_fingerprint
        return {"ok": True}

    monkeypatch.setattr(pgmirror, "refresh", load)
    monkeypatch.setattr(pgmirror, "parity", verify)

    pgmirror.refresh_changed("test")

    assert seen["segments"] == ["s.jsonl"]
    assert seen["loaded"] is snapshot and seen["verified"] is snapshot
    assert taken == ["test"], f"the ledger was fingerprinted {len(taken)} times"


# --- a load refuses while a migration is pending ------------------------------

class _FakeLoaderConn:
    """The admin connection `refresh` drives: the migration probe, the DELETE and
    the COPY. `applied` is the filenames `schema_migrations` holds, or None
    before 001 has created that table (`to_regclass` answers NULL)."""

    def __init__(self, applied):
        self.applied, self.executed, self.copied = applied, [], []
        self.committed = self.rolled_back = False

    def cursor(self):
        return _FakeLoaderCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class _FakeLoaderCursor:
    rowcount = 0

    def __init__(self, conn):
        self.conn, self.last = conn, ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        self.last = sql

    def fetchone(self):
        assert "to_regclass" in self.last, self.last
        return (None if self.conn.applied is None
                else "mindhive.schema_migrations",)

    def fetchall(self):
        assert "FROM mindhive.schema_migrations" in self.last, self.last
        return [(n, pgmirror.migration_sha256(n)) for n in self.conn.applied]

    def copy(self, sql):
        self.conn.executed.append(sql)
        rows = self.conn.copied

        class _Copy:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def set_types(self, types):
                pass

            def write_row(self, row):
                rows.append(row)

        return _Copy()


def _loader(monkeypatch, applied):
    conn = _FakeLoaderConn(applied)
    monkeypatch.setenv(ADMIN_ENV, "service=fake")
    monkeypatch.setattr(pgmirror, "_connect_admin", lambda *a, **k: conn)
    return conn


def _writes(conn):
    return [s for s in conn.executed
            if s.lstrip().upper().startswith(("DELETE", "COPY"))]


@pytest.mark.parametrize("applied", [pgmirror.migration_files()[:-1], None],
                         ids=["007-pending", "never-migrated"])
@pytest.mark.parametrize("load", [
    lambda: pgmirror.rebuild("test"),
    lambda: pgmirror.refresh("test", segments=["zzz-writer.jsonl"]),
], ids=["rebuild", "refresh"])
def test_a_load_refuses_while_a_migration_is_pending(monkeypatch, applied, load):
    """Between pulling a change that adds a column and running `hive pg migrate`,
    the COPY names a column the table lacks. Postgres refuses it only after the
    DELETE has run, and the operator reads a driver traceback that never
    mentions migrating. The loader checks first, on the same connection, names
    the pending file and the command, and deletes nothing.

    Kills a `refresh` that goes straight to DELETE, and one that checks after."""
    _same_second_pair()
    conn = _loader(monkeypatch, applied)

    with pytest.raises(RuntimeError) as e:
        load()

    assert "007_append_seq.sql" in str(e.value)
    assert "hive pg migrate" in str(e.value)
    assert _writes(conn) == [], "the load wrote before refusing"
    assert conn.rolled_back and not conn.committed


def test_a_load_with_every_migration_applied_goes_on_to_delete_and_copy(
        monkeypatch):
    """CANARY for the refusal above: a check that refused everything passes it
    too, and would leave the mirror unloadable on a fully migrated server."""
    _same_second_pair()
    conn = _loader(monkeypatch, pgmirror.migration_files())

    out = pgmirror.rebuild("test")

    assert [s.lstrip().split()[0] for s in _writes(conn)] == ["DELETE", "COPY"]
    assert len(conn.copied) == out["rows"] == 2 and conn.committed


def test_refresh_records_the_bytes_before_its_scan_not_after(monkeypatch):
    """The fingerprint a load returns is what `mirror_state` will say was loaded.
    Taken AFTER the scan it would cover a row appended between the scan and the
    stat -- a row the COPY never carried -- and every later `changed_segments`
    would call that segment unchanged. Taken BEFORE, the same row makes the
    segment look changed and the next refresh reloads it: an extra reload is the
    safe direction, a row never mirrored is not."""
    from mindhive import events
    events.append_event("decision", {"text": "scanned"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    real_scan = pgmirror.scan_segments

    def scan_then_a_late_append(project=None):
        rows = real_scan(project)
        events.append_event("decision", {"text": "after the scan"},
                            session_id="s", ts="2026-06-20T10:00:01Z")
        return rows

    monkeypatch.setattr(pgmirror, "scan_segments", scan_then_a_late_append)
    conn = _loader(monkeypatch, pgmirror.migration_files())

    out = pgmirror.refresh("test")

    assert len(conn.copied) == 1, "the fixture's late row reached the COPY"
    assert pgmirror.changed_segments("test", out["fingerprint"]) == ["s.jsonl"], \
        "the load recorded a row it never copied as loaded"
