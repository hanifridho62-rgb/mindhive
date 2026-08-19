# tests/test_pgmigrations.py
"""The DDL, checked as text — because there is no server to check it against.

These files have never been executed: PostgreSQL is not installed on this
machine, which is the standing blocker for half of this feature. That makes the
usual "apply it twice and diff `information_schema`" test impossible today, and
it makes these text assertions the only guard the three .sql files have until it
becomes possible.

So the scope here is deliberate. A text test cannot prove the SQL is *valid*;
what it can prove is that the four properties this schema was designed around
are still spelled out in the file, and each of those four is a property whose
loss is silent:

* **`ts` stays TEXT.** `chain.compute_chain` hashes the canonical JSON of the
  timestamp STRING. A TIMESTAMPTZ round trip normalises `+00:00` to `Z`, drops
  trailing zeros, and makes the original bytes unrecoverable — so the mirror
  would still load, still query, still look right, and never again match the
  file ledger's chain head.
* **`ts_utc` is not generated.** Postgres rejects a non-IMMUTABLE generation
  expression, and every text-to-timestamp path is STABLE. A future edit "fixing"
  this column into a generated one fails at `CREATE TABLE`, a long way from
  whoever wrote it.
* **`COLLATE "C"` on every text column.** Python compares strings by code point;
  a locale collation orders punctuation differently, which would make the
  mirror's row order disagree with `events.read_all()` for hyphenated session
  ids — and row ORDER is hashed.
* **Every statement is re-runnable.** `apply_migrations` is allowed to run
  against a database that already has the schema.

Every scan below is paired with a canary that plants the exact defect the scan
exists to notice, because a scan that finds nothing passes every emptiness
assertion in this file.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "src" / "mindhive" / "pgmigrations"
NAMES = ("001_schema.sql", "002_indexes.sql", "003_roles_and_views.sql")

# A column definition inside a CREATE TABLE body: four spaces, a name, a type.
# One-or-more spaces, not two-or-more: the DDL aligns its types, but a column
# whose name is long enough to leave only one space would otherwise slip out of
# every scan below — silently, which is the failure mode these tests exist to
# prevent. (`applied_at TIMESTAMPTZ` really did escape a `{2,}` version of this.)
_COLUMN = re.compile(r"^ {4}(\w+) +(BIGINT|TEXT|INTEGER|JSONB|TIMESTAMPTZ|BOOLEAN)\b")
_TEXTY = {"TEXT"}


def _sql(name: str) -> str:
    return (MIGRATIONS / name).read_text(encoding="utf-8")


def _all_sql() -> str:
    return "\n".join(_sql(n) for n in NAMES)


def _code(sql: str) -> str:
    """`sql` with every `--` line comment removed.

    Every scan for a FORBIDDEN construct has to run over this rather than the
    raw text, because these files explain their own decisions in prose: the
    comment on `ts_utc` names `ts::timestamptz` in order to say why it is NOT
    used, and a scan that could not tell code from commentary would force the
    schema to stop documenting the traps it avoids. No `--` appears inside a
    string literal in any of the three files, which is what makes the naive
    strip safe here."""
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def _table_bodies(sql: str) -> dict[str, list[str]]:
    """`{table name: [line, ...]}` for every `CREATE TABLE` block, body only."""
    out = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+([\w.]+)\s*\((.*?)\n\);",
                         sql, re.S):
        out[m.group(1)] = m.group(2).splitlines()
    return out


def _columns_with_preceding_comment(lines: list[str]) -> list[tuple[str, str, str]]:
    """`[(column, type, the comment block directly above it)]`."""
    out, comment = [], []
    for line in lines:
        stripped = line.strip()
        m = _COLUMN.match(line)
        if m:
            out.append((m.group(1), m.group(2), " ".join(comment)))
            comment = []
        elif stripped.startswith("--"):
            comment.append(stripped.lstrip("- "))
        elif not stripped:
            pass                      # a blank line does not break the block
        else:
            comment = []
    return out


# --------------------------------------------------------------------------
# the extractors (canaries first)
# --------------------------------------------------------------------------

def test_all_three_migration_files_exist_and_are_not_empty():
    for name in NAMES:
        p = MIGRATIONS / name
        assert p.is_file(), f"{name} is missing"
        assert len(p.read_text(encoding="utf-8").strip()) > 200, f"{name} is a stub"


def test_the_filenames_sort_into_application_order():
    """`apply_migrations` sorts by filename, so schema-before-indexes-before-
    grants is a property of the NAMES, not of anything in the code."""
    assert sorted(NAMES) == list(NAMES)


def test_the_table_parser_finds_the_real_tables():
    """CANARY. Every per-column assertion below iterates the parser's output,
    and a parser that returned `{}` would pass all of them."""
    tables = _table_bodies(_sql("001_schema.sql"))
    assert set(tables) == {"mindhive.events", "mindhive.event_type_registry",
                           "mindhive.mirror_state", "mindhive.schema_migrations"}
    cols = _columns_with_preceding_comment(tables["mindhive.events"])
    assert len(cols) == 12, f"expected 12 columns on mindhive.events, got {cols}"


def test_the_column_parser_pairs_a_column_with_the_comment_above_it():
    """CANARY for the pairing itself: a parser that always returned an empty
    comment would make the 'every column is explained' test fail for the wrong
    reason, and one that always returned the file would make it vacuous."""
    body = ["    -- why this is text", "    -- and not varchar",
            "    thing       TEXT COLLATE \"C\" NOT NULL,",
            "    unexplained INTEGER NOT NULL,"]
    got = _columns_with_preceding_comment(body)
    assert got[0][0] == "thing" and "why this is text" in got[0][2]
    assert got[1] == ("unexplained", "INTEGER", "")


# --------------------------------------------------------------------------
# the three settled corrections
# --------------------------------------------------------------------------

def test_ts_is_text_and_never_a_timestamp():
    """CORRECTION 1. The hashed timestamp is a byte-exact string; a TIMESTAMPTZ
    round trip makes the original spelling unrecoverable and silently ends
    chain-head parity."""
    events = _table_bodies(_sql("001_schema.sql"))["mindhive.events"]
    types = {name: sql_type for name, sql_type, _ in
             _columns_with_preceding_comment(events)}
    assert types["ts"] == "TEXT"
    assert types["ts_key"] == "TEXT"
    assert not re.search(r"^\s+ts\s+TIMESTAMPTZ", "\n".join(events), re.M)


def test_nothing_casts_ts_to_a_timestamp_anywhere_in_the_ddl():
    """The same correction from the other side: an index or a view that casts
    `ts` would reintroduce the STABLE-expression problem and, worse, invite a
    read path that returns the cast value instead of the stored bytes."""
    assert "ts::timestamptz" not in _code(_all_sql())
    assert "ts::timestamp" not in _code(_all_sql())
    assert "to_timestamp(" not in _code(_all_sql())


def test_ts_utc_is_a_plain_column_the_loader_fills():
    """CORRECTION 2. Postgres requires a generated column's expression to be
    IMMUTABLE and every text-to-timestamp path is STABLE, so
    `GENERATED ALWAYS AS (ts::timestamptz) STORED` is rejected at CREATE TABLE.
    The parse happens in Python, in the same language as `events.now_iso`."""
    sql = _sql("001_schema.sql")
    body = "\n".join(_table_bodies(sql)["mindhive.events"])
    m = re.search(r"^\s+ts_utc\s+TIMESTAMPTZ(.*)$", body, re.M)
    assert m, "ts_utc is not declared TIMESTAMPTZ"
    assert "GENERATED" not in m.group(1), "ts_utc must not be a generated column"


def test_ts_key_is_the_generated_column_and_uses_coalesce():
    """The one column that genuinely IS generated, because COALESCE is immutable
    — and it is the column the ordering depends on, which is why it exists at
    all: NULL and '' must never disagree with Python about which sorts first."""
    body = "\n".join(_table_bodies(_sql("001_schema.sql"))["mindhive.events"])
    assert re.search(r"ts_key\s+TEXT COLLATE \"C\" NOT NULL\s*\n\s*--?.*?"
                     r"GENERATED ALWAYS AS \(COALESCE\(ts, ''\)\) STORED",
                     body, re.S) or \
        "GENERATED ALWAYS AS (COALESCE(ts, '')) STORED" in body


def test_every_text_column_declares_collate_c():
    """CORRECTION 3. C collation on a UTF-8 database is EXACTLY Python's
    code-point ordering. Declared on the column definition rather than on each
    index and each ORDER BY, so every index and every ad-hoc query inherits it
    and there is no place left to forget it."""
    naked = []
    for table, lines in _table_bodies(_sql("001_schema.sql")).items():
        for line in lines:
            m = _COLUMN.match(line)
            if m and m.group(2) in _TEXTY and 'COLLATE "C"' not in line:
                naked.append(f"{table}.{m.group(1)}")
    assert naked == [], f"text column(s) without COLLATE \"C\": {naked}"


def test_the_collation_scan_catches_a_naked_text_column():
    """CANARY for the scan above: it is an emptiness assertion over a regex, and
    a regex that stopped matching would report no violations forever."""
    body = ['    project     TEXT NOT NULL,']
    naked = [m.group(1) for line in body
             if (m := _COLUMN.match(line)) and m.group(2) in _TEXTY
             and 'COLLATE "C"' not in line]
    assert naked == ["project"]


def test_the_order_index_is_keyed_on_segment_not_session_id():
    """The divergence the brief did not anticipate: `'-' (0x2D) < '.' (0x2E)`,
    so ordering by `session_id` is NOT equivalent to ordering by
    `session_id || '.jsonl'`. `manual` and `manual-2` come out in opposite
    orders, and `manual` is the real default id. The file order is derivable
    from the row only if the row carries the filename."""
    sql = _sql("002_indexes.sql")
    m = re.search(r"CREATE INDEX IF NOT EXISTS events_order_idx\s*\n?\s*"
                  r"ON mindhive\.events \((.*?)\);", sql, re.S)
    assert m, "events_order_idx is missing"
    keys = [k.strip() for k in m.group(1).split(",")]
    assert keys == ["project", "ts_key", "segment", "seg_line"], \
        f"the ledger-order index is keyed on {keys}"


# --------------------------------------------------------------------------
# every column says why it has that type
# --------------------------------------------------------------------------

@pytest.mark.parametrize("table", ["mindhive.events", "mindhive.event_type_registry",
                                   "mindhive.mirror_state",
                                   "mindhive.schema_migrations"])
def test_every_column_records_why_it_has_that_type(table):
    """A type choice with no reason attached gets 'tidied' by the next reader —
    which is precisely how `ts` would become a timestamp. The comment is the
    only thing standing between this schema and that edit."""
    lines = _table_bodies(_sql("001_schema.sql"))[table]
    thin = [name for name, _, comment in _columns_with_preceding_comment(lines)
            if len(comment) < 40]
    assert thin == [], f"{table} column(s) with no recorded reason: {thin}"


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------

def _non_idempotent(sql: str) -> list[str]:
    """Statements that would fail on a second `apply_migrations` run."""
    bad = []
    for m in re.finditer(r"CREATE\s+(?:UNIQUE\s+)?(TABLE|SCHEMA|INDEX)\s+"
                         r"(IF NOT EXISTS\s+)?([\w.]+)", _code(sql)):
        if m.group(2) is None:
            bad.append(f"CREATE {m.group(1)} {m.group(3)}")
    for m in re.finditer(r"CREATE\s+VIEW\s+([\w.]+)", _code(sql)):
        bad.append(f"CREATE VIEW {m.group(1)}")
    return bad


def test_every_statement_is_re_runnable():
    """Applying twice must leave the same catalog: `apply_migrations` is allowed
    to run against a database that already has the schema, and a second run that
    errors is indistinguishable to the operator from a broken migration."""
    assert _non_idempotent(_all_sql()) == []


def test_the_idempotency_scan_catches_a_bare_create():
    """CANARY. This is the exact mutation the migration-idempotency test's own
    canary uses on a live server (`CREATE INDEX IF NOT EXISTS` ->
    `CREATE INDEX`), asserted here at the text level because there is no server
    yet."""
    assert _non_idempotent("CREATE INDEX events_x ON mindhive.events (project);")
    assert _non_idempotent("CREATE VIEW mindhive_ro.events AS SELECT 1;")
    assert _non_idempotent(
        "CREATE INDEX IF NOT EXISTS events_x ON mindhive.events (project);") == []


def test_role_creation_is_guarded_because_create_role_has_no_if_not_exists():
    """`CREATE ROLE` has no `IF NOT EXISTS` in any Postgres version, so the DO
    block IS the idempotency for this file."""
    sql = _code(_sql("003_roles_and_views.sql"))
    for role in ("mindhive_owner", "mindhive_reader"):
        assert re.search(rf"IF NOT EXISTS \(SELECT 1 FROM pg_roles "
                         rf"WHERE rolname = '{role}'\)", sql), role
    assert sql.count("CREATE ROLE") == 2


def test_no_index_is_created_concurrently():
    """`apply_migrations` runs each file in ONE transaction, and CREATE INDEX
    CONCURRENTLY cannot run in a transaction block. This is a mistake that only
    shows up on a live server, so it is worth catching in text."""
    assert "CONCURRENTLY" not in _code(_all_sql()), \
        "a concurrent index build cannot run in a transaction block"


# --------------------------------------------------------------------------
# L5 — the layer that actually holds
# --------------------------------------------------------------------------

def test_the_reader_view_is_a_security_barrier_and_not_security_invoker():
    """Not `security_invoker`: the view executes with the OWNER's rights, which
    is the whole mechanism that lets the reader hold no privilege on the base
    table at all. `security_barrier` keeps a leaky function from being evaluated
    against other namespaces' rows before the project filter."""
    sql = _sql("003_roles_and_views.sql")
    assert "WITH (security_barrier = true)" in sql
    assert "security_invoker" not in _code(sql)


def test_the_reader_view_carries_the_namespace_predicate_itself():
    """Constraint 6 satisfied structurally rather than by asking the model to
    remember a WHERE clause. An unset setting is NULL, which yields zero rows —
    fail-closed."""
    assert "WHERE  project = current_setting('mindhive.project', true)" \
        in _sql("003_roles_and_views.sql")


def test_the_reader_is_granted_nothing_but_select():
    """L5 stated positively. A GRANT of INSERT/UPDATE/DELETE to the reader would
    quietly demote the read-only guarantee to the Python string check."""
    sql = _sql("003_roles_and_views.sql")
    for stmt in re.findall(r"^GRANT\s+(.*?)\s+ON", sql, re.M):
        assert not {"INSERT", "UPDATE", "DELETE", "ALL"} & set(stmt.upper().split(", ")), \
            f"reader granted more than SELECT: GRANT {stmt}"
    assert "REVOKE ALL ON SCHEMA mindhive FROM mindhive_reader" in sql


def test_the_reader_role_carries_its_own_braces():
    """Role-level defaults survive a bug in the Python layer, because they are
    set on the ROLE rather than per connection."""
    sql = _sql("003_roles_and_views.sql")
    for setting in ("default_transaction_read_only = on",
                    "statement_timeout = '5s'",
                    "idle_in_transaction_session_timeout = '15s'",
                    "search_path = mindhive_ro, pg_catalog"):
        assert f"ALTER ROLE mindhive_reader SET {setting}" in sql, setting


def test_the_project_column_is_not_exposed_to_the_reader():
    """The base table is namespaced; the view is not. If `project` ever appears
    in the select list, `SELECT * FROM events` stops meaning 'this namespace'
    and the model is one forgotten predicate away from a cross-namespace read."""
    m = re.search(r"CREATE OR REPLACE VIEW\s+mindhive_ro\.events\b.*?"
                  r"\bAS\s+SELECT\s+(.*?)\bFROM\b",
                  _sql("003_roles_and_views.sql"), re.S)
    assert m and "project" not in m.group(1)


# --------------------------------------------------------------------------
# repo gates
# --------------------------------------------------------------------------

def test_no_credential_literal_in_any_migration():
    """The recommended DSN is `service=mindhive_ro`, resolved through libpq's
    .pg_service.conf with the password in PGPASSFILE — so neither the repo nor
    the environment holds a secret. `003` deliberately does not set a password:
    re-running it must not be able to reset one."""
    code = _code(_all_sql()).lower()
    assert "password" not in code, \
        "003 must not set a password: re-running it would reset the operator's"
    raw = _all_sql().lower()
    assert "postgres://" not in raw and "postgresql://" not in raw
    assert not re.search(r"password\s*=", raw)


def test_the_natural_key_makes_a_double_load_a_constraint_violation():
    """A row loaded twice changes the chain head, so it has to be impossible
    rather than merely unlikely — and the load's idempotency (ON CONFLICT DO
    NOTHING) needs this unique constraint to have something to conflict on."""
    sql = _sql("001_schema.sql")
    assert "CONSTRAINT events_natural_key UNIQUE (project, segment, seg_line)" in sql
    assert "CONSTRAINT events_seg_line_nonneg CHECK (seg_line >= 0)" in sql
