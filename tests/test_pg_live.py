# tests/test_pg_live.py — the tier that needs a real PostgreSQL server.
#
# Marked `pg` and deselected by default, for the same reason as `node` and
# `browser`: CI has no server, and a tier that errors on every machine without
# one is a tier people learn to ignore. Run it with:
#
#     pytest -m pg
#
# with MINDHIVE_PG_DSN_ADMIN and MINDHIVE_PG_DSN_RO pointing at a cluster you do
# not mind having rewritten. Every test here uses its own namespace under a tmp
# MINDHIVE_HOME, so it never touches the real ledger; the one exception is the
# read-only privilege probe, which reads whatever namespace is loaded.
#
# WHAT THIS FILE IS FOR, stated plainly: the rest of the pg tests run against
# fakes, and fakes cannot tell you that `SET LOCAL x = %s` is a syntax error,
# that `GENERATED ALWAYS AS (ts::timestamptz)` will not compile, that the reader
# role can actually read, or that 37k events round-trip to the same chain head.
# Those are the claims a server has to answer, and this is where they are asked.
import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.pg

ADMIN = "MINDHIVE_PG_DSN_ADMIN"
READER = "MINDHIVE_PG_DSN_RO"

needs_server = pytest.mark.skipif(
    not (os.environ.get(ADMIN) and os.environ.get(READER)),
    reason=f"needs {ADMIN} and {READER} pointing at a live PostgreSQL cluster")


def _vector_deps():
    """The two imports `pgsearch._missing_deps` probes, asked the same way.

    Separate from `needs_server` because they are a separate axis: the CI pg job
    installs `.[dev,pg]` and deliberately NOT `.[vectors]`, which would drag
    torch in on every push. The loader tests below need neither import -- they
    replace the encoder and `pack_embedding` is struct + math -- so only the
    SEARCH path is gated here, and it says which extra is missing rather than
    erroring on a machine that was never asked to have an encoder."""
    import importlib.util
    return [m for m in ("numpy", "sentence_transformers")
            if importlib.util.find_spec(m) is None]


needs_vectors = pytest.mark.skipif(
    bool(_vector_deps()),
    reason="semantic search needs `pip install -e .[vectors]`")


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated ledger on disk, and the namespace name to load it under.

    A unique project per test so two tests cannot see each other's rows through
    a shared database — the mirror is namespaced, but a shared name would make
    the isolation depend on test ORDER, which is exactly the property a test
    suite must not have.

    Derived from pytest's own tmp_path name rather than from a hash: it is
    already unique per test, and being DETERMINISTIC means a re-run reuses the
    namespace instead of leaving a fresh pile of rows behind every time."""
    from mindhive import paths
    name = paths.normalize(f"pgtest-{tmp_path.name}")
    home = tmp_path / "home"
    (home / name / "events").mkdir(parents=True)
    monkeypatch.setenv("MINDHIVE_HOME", str(home))
    monkeypatch.setenv("MINDHIVE_PROJECT", name)
    return {"name": name, "events": home / name / "events"}


def write_segment(store, session_id, rows):
    """One `<session_id>.jsonl` segment. `rows` are (ts, text) pairs."""
    path = store["events"] / f"{session_id}.jsonl"
    path.write_text("".join(
        json.dumps({"ts": ts, "session_id": session_id, "type": "decision",
                    "payload": {"text": text}, "tags": []}) + "\n"
        for ts, text in rows), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def migrated():
    """Schema applied once for the module. Idempotent, so a re-run is free."""
    if not (os.environ.get(ADMIN) and os.environ.get(READER)):
        pytest.skip("no server configured")
    from mindhive import pgmirror
    pgmirror.apply_migrations()
    pgmirror.sync_type_registry()
    return True


# --- migrations --------------------------------------------------------------

@needs_server
def test_every_migration_applies_and_a_second_run_applies_nothing(migrated):
    from mindhive import pgmirror
    again = pgmirror.apply_migrations()
    assert again["applied"] == []
    assert sorted(again["skipped"]) == sorted(pgmirror.migration_files())


@needs_server
def test_an_applied_migration_whose_bytes_changed_is_refused(migrated, tmp_path,
                                                             monkeypatch):
    """The immutability rule, exercised rather than asserted about.

    Patched at `migration_sha256` rather than by editing a file on disk: the
    behaviour under test is "the recorded digest and the current digest differ",
    and rewriting a packaged resource to produce that is a slower way to reach
    the same branch — and one that leaves a modified file behind if the test
    fails halfway."""
    from mindhive import pgmirror
    target = pgmirror.migration_files()[0]
    real = pgmirror.migration_sha256

    def tampered(name):
        return "0" * 64 if name == target else real(name)

    monkeypatch.setattr(pgmirror, "migration_sha256", tampered)
    with pytest.raises(RuntimeError) as e:
        pgmirror.apply_migrations()
    assert target in str(e.value)
    assert "immutable" in str(e.value)


@needs_server
def test_the_type_registry_matches_the_frozen_table(migrated):
    from mindhive import pgmirror, pgschema
    out = pgmirror.sync_type_registry()
    assert out["types"] == len(pgschema.EVENT_TYPE_CLASSES)


# --- the loader and the gate -------------------------------------------------

@needs_server
def test_a_loaded_namespace_reaches_the_same_chain_head_as_its_files(migrated,
                                                                    store):
    """The one claim the whole mirror rests on.

    Byte-exact `ts`, a JSONB round trip that preserves payload semantics, and an
    ordering contract that matches Python's — all three reduce to this hash."""
    from mindhive import pgmirror
    write_segment(store, "alpha", [("2026-01-01T00:00:0%dZ" % i, f"m{i}")
                                   for i in range(5)])
    loaded = pgmirror.rebuild(store["name"])
    assert loaded["rows"] == 5

    v = pgmirror.parity(store["name"])
    assert v["ok"] is True and v["exact"] is True and v["lag"] == 0
    assert v["file_head"] == v["pg_head"]


@needs_server
def test_an_empty_mirror_fails_parity_rather_than_passing_vacuously(migrated,
                                                                   store):
    """Two empty folds hash alike. A gate that passed here would prove nothing,
    and would pass hardest exactly when the loader had done nothing at all."""
    from mindhive import pgmirror
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "m")])
    # deliberately NOT rebuilt
    v = pgmirror.parity(store["name"])
    assert v["ok"] is False
    assert v["rows"] == 0
    assert "nothing to compare" in v["reason"]


@needs_server
def test_ordering_follows_the_segment_filename_and_not_the_session_id(migrated,
                                                                     store):
    """THE discriminating test for the ordering contract.

    `-` is 0x2D and `.` is 0x2E, so where one session id is a proper prefix of
    another, filename order and id order INVERT: `verify-e2e-001.jsonl` sorts
    before `verify.jsonl`, while `verify` sorts before `verify-e2e-001`.

    This fixture is necessary because the real 37k-event ledger does NOT
    exercise it — measured, 868 timestamps are shared across segments and not
    one of those pairs inverts. A parity run on real data therefore passes with
    the sort key mutated, which is precisely why this file exists."""
    from mindhive import pgmirror
    same_ts = "2026-01-01T00:00:00Z"
    write_segment(store, "verify", [(same_ts, "from verify")])
    write_segment(store, "verify-e2e-001", [(same_ts, "from verify-e2e-001")])

    triples = pgmirror.scan_segments(store["name"])
    assert [t[0] for t in triples] == ["verify-e2e-001.jsonl", "verify.jsonl"]
    assert [t[2].session_id for t in triples] != sorted(
        t[2].session_id for t in triples), (
        "fixture is not discriminating: the two orders agree here")

    pgmirror.rebuild(store["name"])
    assert pgmirror.parity(store["name"])["ok"] is True


@needs_server
def test_the_gate_bites_when_the_read_order_is_mutated(migrated, store,
                                                       monkeypatch):
    """The canary for the test above: with the fixture in place, ordering by
    `session_id` must FAIL parity. Without it, a passing parity proves only that
    the two sides agree with each other.

    THE MUTATION GOES IN THE READ STATEMENT, and finding that out cost this test
    one wrong version. Mutating the LOADER's sort changes nothing a gate could
    see: row order is not a stored property of the mirror, so COPY may insert in
    any order it likes and `READ_ALL_SQL`'s ORDER BY re-establishes the sequence
    on the way out. That statement is therefore the only place an ordering bug
    can actually live — and the reason `segment` versus `session_id` is spelled
    out in a comment above it rather than left to look arbitrary."""
    from mindhive import pgmirror, pgstore
    same_ts = "2026-01-01T00:00:00Z"
    write_segment(store, "verify", [(same_ts, "from verify")])
    write_segment(store, "verify-e2e-001", [(same_ts, "from verify-e2e-001")])
    pgmirror.rebuild(store["name"])
    assert pgmirror.parity(store["name"])["ok"] is True, "the fixture is broken"

    monkeypatch.setattr(pgstore, "READ_ALL_SQL",
                        pgstore.READ_ALL_SQL.replace("segment, seg_line",
                                                     "session_id, seg_line"))
    v = pgmirror.parity(store["name"])
    assert v["ok"] is False, "the mutant read order was not detected"
    assert v["rows"] == v["file_rows"] == 2, (
        "this must fail on ORDER, not on a row count difference")
    assert "diverges" in v["reason"]


@needs_server
def test_a_mirror_that_is_merely_behind_is_a_proven_prefix_not_a_failure(
        migrated, store):
    """Lag is not divergence, and conflating them makes the feature unusable.

    This ledger is append-only and live: a session writes events while it works,
    so by the time any tool asks, the mirror is already a few rows behind. The
    prefix check PROVES the mirror is right about everything it holds instead of
    assuming it from append-only-ness."""
    from mindhive import pgmirror
    seg = write_segment(store, "alpha", [("2026-01-01T00:00:0%dZ" % i, f"m{i}")
                                         for i in range(3)])
    pgmirror.rebuild(store["name"])
    assert pgmirror.parity(store["name"])["exact"] is True

    seg.write_text(seg.read_text(encoding="utf-8") + json.dumps(
        {"ts": "2026-01-01T00:00:09Z", "session_id": "alpha",
         "type": "decision", "payload": {"text": "later"}, "tags": []}) + "\n",
        encoding="utf-8")

    v = pgmirror.parity(store["name"])
    assert v["ok"] is True
    assert v["exact"] is False
    assert v["lag"] == 1
    assert "prefix" in v["reason"]


@needs_server
def test_an_edited_row_fails_even_though_the_count_still_matches(migrated,
                                                                store):
    """The case a row-count check cannot see. Same number of rows, different
    content — only the chain notices."""
    from mindhive import pgmirror
    seg = write_segment(store, "alpha", [("2026-01-01T00:00:0%dZ" % i, f"m{i}")
                                         for i in range(3)])
    pgmirror.rebuild(store["name"])
    seg.write_text(seg.read_text(encoding="utf-8").replace("m1", "TAMPERED"),
                   encoding="utf-8")

    v = pgmirror.parity(store["name"])
    assert v["ok"] is False
    assert v["lag"] == 0, "the count is unchanged; this must fail on content"
    assert "diverges" in v["reason"]


@needs_server
def test_a_mirror_longer_than_its_source_is_a_failure(migrated, store):
    """Append-only cannot produce this, so it means the mirror was built from a
    different or truncated store — which is worse than being behind, not better."""
    from mindhive import pgmirror
    seg = write_segment(store, "alpha", [("2026-01-01T00:00:0%dZ" % i, f"m{i}")
                                         for i in range(3)])
    pgmirror.rebuild(store["name"])
    seg.write_text("", encoding="utf-8")

    v = pgmirror.parity(store["name"])
    assert v["ok"] is False
    assert v["lag"] < 0
    assert "MORE rows" in v["reason"]


@needs_server
def test_rebuilding_one_namespace_leaves_another_alone(migrated, tmp_path,
                                                       monkeypatch):
    """`DELETE WHERE project`, never `TRUNCATE`. The bug this guards against
    destroys data belonging to a project nobody was working on."""
    from mindhive import pgmirror
    home = tmp_path / "home"
    names = []
    for tag in ("keepme", "rebuildme"):
        name = f"iso_{tag}"
        (home / name / "events").mkdir(parents=True)
        (home / name / "events" / "s.jsonl").write_text(json.dumps(
            {"ts": "2026-01-01T00:00:00Z", "session_id": "s",
             "type": "decision", "payload": {"text": tag}, "tags": []}) + "\n",
            encoding="utf-8")
        names.append(name)
    monkeypatch.setenv("MINDHIVE_HOME", str(home))

    for name in names:
        pgmirror.rebuild(name)
    assert pgmirror.parity(names[0])["ok"] is True

    pgmirror.rebuild(names[1])
    assert pgmirror.parity(names[0])["ok"] is True, (
        "rebuilding one namespace destroyed another's rows")


# --- what the reader role can and cannot do (finding C1) ---------------------

@needs_server
def test_the_connecting_role_is_not_a_superuser_and_is_read_only(migrated):
    """The privilege split, asked of the server rather than of the design doc.

    Every layer below `mindhive_ro` is supposed to be unreachable. Reading the
    design and believing it is how a mirror ends up being read by `postgres`."""
    import psycopg
    with psycopg.connect(os.environ[READER]) as c, c.cursor() as cur:
        cur.execute("SELECT current_user, "
                    "(SELECT usesuper FROM pg_user WHERE usename=current_user)")
        user, superuser = cur.fetchone()
        assert user == "mindhive_reader"
        assert superuser is False

        cur.execute("SELECT current_setting('default_transaction_read_only'), "
                    "current_setting('statement_timeout')")
        assert cur.fetchone() == ("on", "5s")


@needs_server
def test_the_reader_cannot_reach_the_base_table_or_write_to_it(migrated):
    import psycopg
    with psycopg.connect(os.environ[READER]) as c:
        for sql in ("SELECT count(*) FROM mindhive.events",
                    "INSERT INTO mindhive.events (project, segment, seg_line, "
                    "payload, tags) VALUES ('x','y',0,'{}','[]')"):
            with c.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(sql)
            c.rollback()


@needs_server
def test_an_unset_namespace_yields_zero_rows_rather_than_all_of_them(migrated):
    """Fail-closed, and the direction matters: `current_setting(..., true)`
    returns NULL when unset, NULL matches no row, so a caller that forgot to
    scope sees nothing instead of every project's history."""
    import psycopg
    with psycopg.connect(os.environ[READER]) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM mindhive_ro.events")
        assert cur.fetchone()[0] == 0


# --- freshness ---------------------------------------------------------------

@needs_server
def test_mirror_health_reports_a_namespace_that_was_never_built(migrated,
                                                                store):
    from mindhive import pgstore
    pgstore.reset_state()
    h = pgstore.mirror_health(store["name"])
    assert h["available"] is True
    assert h["parity_ok"] is False
    assert "never been built" in h["stale_reason"]


@needs_server
def test_mirror_health_reads_the_verdict_a_parity_run_recorded(migrated, store):
    """Needs migration 004: 003 revoked schema `mindhive` from the reader, which
    left the verdict table unreadable by the role that refuses on it."""
    from mindhive import pgmirror, pgstore
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "m")])
    pgmirror.rebuild(store["name"])
    assert pgmirror.parity(store["name"])["recorded"] is True

    pgstore.reset_state()
    h = pgstore.mirror_health(store["name"])
    assert h["parity_ok"] is True
    assert h["stale"] is False
    assert h["rows"] == 1


@needs_server
def test_growth_since_the_rebuild_is_reported_as_stale_not_as_parity_failure(
        migrated, store):
    """The distinction the AI tools live or die on: a stale mirror still answers
    (with a notice), a diverged one refuses."""
    from mindhive import pgmirror, pgstore
    seg = write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "m")])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])

    seg.write_text(seg.read_text(encoding="utf-8") + json.dumps(
        {"ts": "2026-01-01T00:00:05Z", "session_id": "alpha",
         "type": "decision", "payload": {"text": "later"}, "tags": []}) + "\n",
        encoding="utf-8")

    pgstore.reset_state()
    h = pgstore.mirror_health(store["name"])
    assert h["stale"] is True
    assert "ledger has changed" in h["stale_reason"]
    assert h["parity_ok"] is True, (
        "growth must not be reported as a parity failure — that refuses every "
        "call on a live append-only ledger")


# --- the AI tool surface -----------------------------------------------------

@needs_server
def test_every_registered_tool_answers_against_a_real_server(migrated, store):
    from mindhive import pgmirror, pgstore, pgtools
    write_segment(store, "alpha", [("2026-01-01T00:00:0%dZ" % i, f"m{i}")
                                   for i in range(5)])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])
    pgstore.reset_state()

    calls = {
        "query_database": {"sql": "SELECT count(*) AS n FROM events"},
        "get_recent_events": {"session_id": "alpha"},
        "search_events_by_payload": {"key": "text", "value": "m1"},
        "get_session_summary": {"session_id": "alpha"},
        "get_latest_event": {"session_id": "alpha"},
        "search_events_by_type": {"event_type": "decision"},
        "get_current_state": {},
        # Answers with the empty-index notice here rather than a ranked list,
        # because this namespace was never embedded -- deliberately, so the
        # gate stays cheap and does not load a 90 MB model. The ranked path is
        # covered by the search tests further down, one of which runs the real
        # encoder end to end.
        "search_events_by_meaning": {"query": "the mirror"},
    }
    assert set(calls) == set(pgtools.TOOL_FUNCTIONS), (
        "a tool was registered without being exercised here")
    for name, args in calls.items():
        out = pgtools.call(name, args)
        if name == "search_events_by_meaning" and _vector_deps():
            # Not installed here (see `needs_vectors`), so the contract under
            # test is the REFUSAL: a named code and a runnable remedy, never a
            # traceback and never a silent empty result.
            assert out["ok"] is False
            assert out["error"]["code"] == "deps_missing"
            assert ".[vectors]" in out["error"]["detail"]
            continue
        assert out["ok"] is True, (name, out.get("error"))


@needs_server
def test_paging_returns_disjoint_pages(migrated, store):
    from mindhive import pgmirror, pgstore, pgtools
    write_segment(store, "alpha", [("2026-01-01T00:%02d:00Z" % i, f"m{i}")
                                   for i in range(10)])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])
    pgstore.reset_state()

    first = pgtools.get_recent_events("alpha", limit=4, offset=0)
    second = pgtools.get_recent_events("alpha", limit=4, offset=4)
    assert first["offset"] == 0 and second["offset"] == 4
    ids = {r["global_seq"] for r in first["rows"]}
    assert ids and ids.isdisjoint(r["global_seq"] for r in second["rows"])


@needs_server
def test_a_second_statement_cannot_ride_in_on_query_database(migrated, store):
    """The injection the wrap exists to stop, asked of a real server.

    A fake cannot answer this: whether a second statement executes depends on
    which libpq entry point psycopg chooses, and that depends on whether the
    parameter sequence is truthy."""
    from mindhive import pgmirror, pgstore, pgtools
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "m")])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])
    pgstore.reset_state()

    out = pgtools.query_database(
        "SELECT 1) AS a; CREATE TABLE pwned_by_test(x int); --")
    assert out["ok"] is False

    import psycopg
    with psycopg.connect(os.environ[ADMIN]) as c, c.cursor() as cur:
        cur.execute("SELECT to_regclass('public.pwned_by_test')")
        assert cur.fetchone()[0] is None, "a second statement executed"


@needs_server
def test_the_helper_tools_page_at_twenty_by_default(migrated, store):
    """The user-facing bound. 20, not 200: the context window is the resource
    being protected, and a tool that CAN return hundreds of rows will."""
    from mindhive import pgmirror, pgstore, pgtools
    write_segment(store, "alpha", [("2026-01-01T%02d:00:00Z" % (i % 24), f"m{i}")
                                   for i in range(40)])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])
    pgstore.reset_state()

    assert pgtools.DEFAULT_LIMIT == 20
    for out in (pgtools.get_recent_events("alpha"),
                pgtools.search_events_by_type("decision")):
        assert out["ok"] is True
        assert out["row_count"] == 20
        assert out["truncated"]["rows"] is True


@needs_server
def test_an_unregistered_event_type_teaches_the_vocabulary(migrated, store):
    from mindhive import pgmirror, pgstore, pgtools
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "m")])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])
    pgstore.reset_state()

    out = pgtools.search_events_by_type("code_changes")   # the plural typo
    assert out["ok"] is True and out["row_count"] == 0
    assert "is registered" in out["notice"]
    assert "code_change" in out["notice"]


@needs_server
def test_the_session_summary_splits_semantic_from_deterministic(migrated,
                                                                store):
    """`counts_by_class` was promised in the tool description and computed
    nowhere; every caller got null until the CTE was written."""
    from mindhive import pgmirror, pgstore, pgtools
    write_segment(store, "alpha", [("2026-01-01T00:00:0%dZ" % i, f"m{i}")
                                   for i in range(3)])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])
    pgstore.reset_state()

    summary = pgtools.get_session_summary("alpha")["summary"]
    assert summary["counts_by_class"] == {"semantic": 3}


def test_the_cli_help_names_every_tool_it_can_run():
    """A default-tier test: no server needed to catch a stale count in prose.

    `hive pg mcp`'s help said "the four tools" for as long as there were seven,
    which is the kind of drift a reader trusts precisely because it is specific."""
    out = subprocess.run([sys.executable, "-m", "mindhive", "pg", "--help"],
                         capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(
                             os.path.abspath(__file__))))
    from mindhive import pgtools
    assert "four tools" not in out.stdout
    assert f"{len(pgtools.TOOL_FUNCTIONS)}" in out.stdout or \
           "seven" in out.stdout


# --------------------------------------------------------------------------
# P1-P6: the incremental refresh, against a real server
# --------------------------------------------------------------------------
#
# `refresh` is what makes the mirror FOLLOW the ledger. Nothing on the append
# path writes to Postgres — there is no per-event seam and there deliberately is
# none, because the module on that path holds a role-level read-only credential —
# so the mirror advances here, reloading only the segments whose bytes moved.
#
# Every claim below needs a server. Reordering, row identity, MVCC and the
# `project` predicate are all properties of what Postgres does with the
# statements, not of what the Python says, and a fake would answer yes to all
# six regardless.

def _seqs(project, segment=None) -> list:
    """`(segment, seg_line, global_seq)` for one namespace, admin-side."""
    from mindhive import pgmirror
    conn = pgmirror._connect_admin()
    try:
        with conn.cursor() as cur:
            if segment is None:
                cur.execute("SELECT segment, seg_line, global_seq "
                            "FROM mindhive.events WHERE project = %s "
                            "ORDER BY segment, seg_line", (project,))
            else:
                cur.execute("SELECT segment, seg_line, global_seq "
                            "FROM mindhive.events "
                            "WHERE project = %s AND segment = %s "
                            "ORDER BY seg_line", (project, segment))
            return cur.fetchall()
    finally:
        conn.close()


def _forget(project) -> None:
    """Erase every trace of one namespace from the mirror: rows and state row.

    Needed because namespaces here are deterministic per test, so a re-run of the
    file inherits the previous run's `mirror_state`. Only a test that asserts a
    COLD namespace cares, and only that test calls this."""
    from mindhive import pgmirror
    conn = pgmirror._connect_admin()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mindhive.events WHERE project = %s",
                        (project,))
            cur.execute("DELETE FROM mindhive.mirror_state WHERE project = %s",
                        (project,))
        conn.commit()
    finally:
        conn.close()


def _texts_in_read_order(project) -> list:
    """What a fold sees: the mirror's rows through the read path's own ORDER BY."""
    from mindhive import pgmirror
    conn = pgmirror._connect_admin()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT payload->>'text' FROM mindhive.events "
                        "WHERE project = %s "
                        "ORDER BY COALESCE(ts,''), segment, seg_line", (project,))
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


@needs_server
def test_appending_one_event_and_refreshing_leaves_parity_ok(migrated, store):
    """P1. The whole point, end to end: the ledger grew, `refresh` ran, and the
    hash chains agree again. Everything below this is about what refresh must NOT
    do while achieving it."""
    from mindhive import pgmirror
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "a1")])
    pgmirror.rebuild(store["name"])
    # `parity` is what STAMPS `mirror_state`, including the fingerprint the
    # incremental path reads. A rebuild alone leaves the mirror reporting
    # "never built", which is why `hive pg rebuild` now runs both.
    assert pgmirror.parity(store["name"])["ok"] is True

    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "a1"),
                                   ("2026-01-01T00:00:01Z", "a2")])
    out = pgmirror.refresh_changed(store["name"])
    assert out["segments"] == ["alpha.jsonl"], \
        f"refresh reloaded the wrong set: {out['segments']}"
    assert out["verified"]["ok"] is True, out["verified"]
    assert _texts_in_read_order(store["name"]) == ["a1", "a2"]


@needs_server
def test_an_untouched_segments_rows_are_not_rewritten(migrated, store):
    """P2, and the test that separates a real incremental refresh from a rebuild
    wearing the name.

    MUTANT THAT MUST BREAK IT: make `refresh_changed` pass `segments=None`. The
    parity assertion in P1 stays green — a rebuild is correct, just expensive —
    and only this one notices, because a full reload re-COPYs every row and
    `global_seq` is assigned by the sequence on insert."""
    from mindhive import pgmirror
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "a1")])
    write_segment(store, "beta", [("2026-01-02T00:00:00Z", "b1")])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])          # stamps the fingerprint
    before = _seqs(store["name"], "beta.jsonl")
    assert before, "the fixture never loaded beta"

    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "a1"),
                                   ("2026-01-01T00:00:01Z", "a2")])
    out = pgmirror.refresh_changed(store["name"])

    assert out["segments"] == ["alpha.jsonl"]
    assert _seqs(store["name"], "beta.jsonl") == before, \
        "an untouched segment's rows were rewritten — a rebuild, not a refresh"
    assert out["verified"]["ok"] is True


@needs_server
def test_a_segment_whose_earlier_lines_changed_converges(migrated, store):
    """P3. A segment can be REWRITTEN, not only appended to — a corrected event,
    a repaired torn line — and the reload has to converge rather than accumulate.

    MUTANT THAT MUST BREAK IT: drop the per-segment `DELETE` and keep the `COPY`.
    Rows then duplicate, the row count doubles, and parity fails.

    The replacement text is a DIFFERENT LENGTH from the original on purpose. The
    fingerprint is per-segment byte size, so a same-length in-place edit is
    invisible to it — a real limitation, and one the test below this states
    outright rather than leaving for someone to trip over."""
    from mindhive import pgmirror
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "old"),
                                   ("2026-01-01T00:00:01Z", "keep")])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])          # stamps the fingerprint

    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "rewritten"),
                                   ("2026-01-01T00:00:01Z", "keep")])
    out = pgmirror.refresh_changed(store["name"])

    assert _texts_in_read_order(store["name"]) == ["rewritten", "keep"], \
        "the rewritten segment did not converge"
    assert out["rows"] == 2, f"rows duplicated instead of converging: {out}"
    assert out["verified"]["ok"] is True


@needs_server
def test_a_same_length_in_place_edit_is_invisible_to_the_fingerprint(migrated,
                                                                     store):
    """The LIMIT of the cheap check, asserted so it is a known property rather
    than a surprise. `_fingerprint` is `{segment: st_size}`; an edit that
    preserves the byte count moves nothing it can see, so `changed_segments`
    reports nothing and `refresh` reloads nothing.

    That is acceptable, and the reason is structural rather than optimistic: the
    ledger is append-only and hash-chained, so a same-length in-place edit is
    corruption, not an update — and corruption is `parity`'s job, which compares
    chain heads and catches exactly this at identical row counts. The fingerprint
    is a staleness heuristic cheap enough to take on every tool call; it was
    never the integrity check. `pgstore.mirror_health` says the same thing about
    its own copy of this fingerprint, and the two must keep agreeing.

    If this test ever goes red, the fingerprint gained a field (mtime, a hash) —
    which is fine, but `pgstore._ledger_fingerprint` has to gain it in the SAME
    change or every namespace reports itself permanently stale."""
    from mindhive import pgmirror
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "old")])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])

    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "new")])   # same size
    assert pgmirror.changed_segments(
        store["name"], pgmirror.recorded_fingerprint(store["name"])) == [], \
        "the size fingerprint noticed a same-length edit; update the docstring"

    # And parity — the real check — catches what the heuristic cannot.
    assert pgmirror.parity(store["name"])["ok"] is False, \
        "an edited row passed the chain comparison"


@needs_server
def test_a_legacy_fingerprint_forces_a_full_refresh(migrated, store):
    """P4. `_fingerprint` used to return `{"segments": <int>, "bytes": <int>}`.
    A mirror built by that version carries an integer where the map should be,
    and `changed_segments` must read it as UNKNOWN — reload everything — never as
    "nothing changed".

    MUTANT THAT MUST BREAK IT: treat an unrecognised fingerprint as `[]`. The
    mirror then never refreshes and reports itself fresh forever, which is the
    exact failure this whole change exists to fix — silently."""
    from mindhive import pgmirror
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "a1")])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])          # stamps the fingerprint

    legacy = {"segments": 1, "bytes": 123}
    assert pgmirror.changed_segments(store["name"], legacy) is None, \
        "a legacy integer fingerprint was read as a per-segment map"
    assert pgmirror.changed_segments(store["name"], None) is None
    assert pgmirror.changed_segments(store["name"], {"bytes": 1}) is None

    # And `None` really does mean "every segment" when it reaches `refresh`.
    write_segment(store, "beta", [("2026-01-02T00:00:00Z", "b1")])
    out = pgmirror.refresh(store["name"], segments=None)
    assert out["segments"] is None and out["rows"] == 2, out


@needs_server
def test_refresh_on_a_namespace_never_built_equals_rebuild(migrated, tmp_path,
                                                           monkeypatch):
    """P5. The cold-start path. No `mirror_state` row exists, so
    `recorded_fingerprint` answers None, so every segment is reloaded — a
    `refresh` that quietly did nothing here would leave a namespace empty and
    call it fresh."""
    from mindhive import paths, pgmirror
    name = paths.normalize(f"pgcold-{tmp_path.name}")
    home = tmp_path / "home"
    (home / name / "events").mkdir(parents=True)
    monkeypatch.setenv("MINDHIVE_HOME", str(home))
    monkeypatch.setenv("MINDHIVE_PROJECT", name)
    store = {"name": name, "events": home / name / "events"}
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "a1"),
                                   ("2026-01-01T00:00:01Z", "a2")])

    # Namespace names are DETERMINISTIC per test (see the `store` fixture), so a
    # second run of this file would inherit the first run's `mirror_state` row
    # and the namespace would not be cold at all — the assertion below would then
    # pass on run one and fail on run two, which is the worst shape a test has.
    _forget(name)
    assert pgmirror.recorded_fingerprint(name) is None, \
        "the namespace was not actually cold"
    out = pgmirror.refresh_changed(name)
    assert out["rows"] == 2, out
    assert out["verified"]["ok"] is True
    assert _texts_in_read_order(name) == ["a1", "a2"]


@needs_server
def test_refreshing_one_namespace_leaves_another_alone(migrated, tmp_path,
                                                       monkeypatch):
    """P6. `rebuild` already has this test; `refresh` needs its own, because it
    uses a DIFFERENT `DELETE` — segment-scoped rather than project-scoped — and
    segment names are session ids, which two namespaces can share.

    MUTANT THAT MUST BREAK IT: drop the `project` predicate from
    `_DELETE_SEGS_SQL`. The segment name alone then matches the neighbour's rows
    and refreshing one namespace silently empties part of another."""
    from mindhive import paths, pgmirror
    home = tmp_path / "home"
    names = []
    for tag in ("one", "two"):
        name = paths.normalize(f"pgpair-{tmp_path.name}-{tag}")
        (home / name / "events").mkdir(parents=True)
        names.append(name)

    monkeypatch.setenv("MINDHIVE_HOME", str(home))
    for name, text in zip(names, ("first", "second")):
        monkeypatch.setenv("MINDHIVE_PROJECT", name)
        # THE SAME segment name in both namespaces — that collision is the point.
        write_segment({"events": home / name / "events"}, "alpha",
                      [("2026-01-01T00:00:00Z", text)])
        pgmirror.rebuild(name)
        pgmirror.parity(name)               # stamps the fingerprint

    monkeypatch.setenv("MINDHIVE_PROJECT", names[0])
    write_segment({"events": home / names[0] / "events"}, "alpha",
                  [("2026-01-01T00:00:00Z", "first"),
                   ("2026-01-01T00:00:01Z", "first-again")])
    pgmirror.refresh_changed(names[0])

    assert _texts_in_read_order(names[0]) == ["first", "first-again"]
    assert _texts_in_read_order(names[1]) == ["second"], \
        "refreshing one namespace disturbed another's rows"


@needs_server
def test_an_empty_change_set_costs_no_connection(migrated, store, monkeypatch):
    """The cheap case, asserted rather than assumed: nothing moved, so `refresh`
    returns without opening a socket. `_connect_admin` is replaced by something
    that explodes, which is the only way to prove a connection was not made."""
    from mindhive import pgmirror
    write_segment(store, "alpha", [("2026-01-01T00:00:00Z", "a1")])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])          # stamps the fingerprint
    assert pgmirror.changed_segments(
        store["name"], pgmirror.recorded_fingerprint(store["name"])) == [], \
        "a rebuild left the recorded fingerprint disagreeing with the disk"

    def explode(*a, **k):
        raise AssertionError("refresh opened a connection for an empty change set")

    monkeypatch.setattr(pgmirror, "_connect_admin", explode)
    out = pgmirror.refresh(store["name"], segments=[])
    assert out["rows"] == 0 and out["deleted"] == 0 and out["segments"] == []


# --- V1-V8: the vector index, against the real server ------------------------
#
# The DATABASE behaviour is what needs a server: does the width CHECK really
# bite, does a second run really skip, does a rewritten event really lose its
# stale vector, can the reader really read the two new views and still not the
# base table. The ENCODER is not — it is a pure function from text to floats,
# and loading a 90 MB model to answer "did the loader write the row it said it
# wrote" would add ~14 s to every one of these for no assertion gained.
#
# So most tests below install a tiny deterministic encoder, and exactly one runs
# the real one end to end. That last one is what stops the fake from becoming
# the thing under test.

def write_typed_segment(store, session_id, rows):
    """One segment with per-row event types. `rows` are (ts, type, text)."""
    path = store["events"] / f"{session_id}.jsonl"
    path.write_text("".join(
        json.dumps({"ts": ts, "session_id": session_id, "type": kind,
                    "payload": {"text": text}, "tags": []}) + "\n"
        for ts, kind, text in rows), encoding="utf-8")
    return path


@pytest.fixture
def empty_index(store):
    """Drop this namespace's vectors before the test runs.

    `store` derives its namespace from pytest's tmp_path NAME, which is stable
    across runs -- deliberately, so a re-run reuses the namespace instead of
    leaving a fresh pile of rows behind. That is the right trade for the loader
    tests, which rebuild wholesale. It is the wrong one for every test below,
    which asserts on an INCREMENTAL count: rows left by the previous run make
    `embedded` read 0 the second time the suite runs on one machine.

    Found exactly that way -- green on a fresh database, red on the next run.
    Deleting is cheaper than randomising the namespace, and keeps the property
    the store fixture wanted."""
    import psycopg
    with psycopg.connect(os.environ[ADMIN], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM mindhive.event_embeddings WHERE project = %s",
                (store["name"],))
    return store


@pytest.fixture
def fake_encoder(monkeypatch):
    """A deterministic 4-dim encoder: no model, no download, no 14 seconds.

    A bag-of-characters projection into four buckets: the same text always
    lands on the same vector, which is what the incrementality assertions
    need, and the fixtures below are chosen so the texts under comparison
    land on DIFFERENT buckets. It is not meaningful as an embedding, and no
    test here asserts semantic quality with it installed — that claim is the
    real encoder's to make, in the one test that loads it."""
    from mindhive import vectors

    def encode(texts):
        out = []
        for t in texts:
            h = [0.0, 0.0, 0.0, 0.0]
            for i, ch in enumerate(str(t)):
                h[ord(ch) % 4] += 1.0 + (i % 3)
            out.append(h)
        return out

    monkeypatch.setattr(vectors, "encode", encode)
    return encode


def _embedding_rows(project, model=None):
    """`[(segment, seg_line, event_type, event_id, dim, norm)]` for a namespace,
    read with the ADMIN credential so a test can see rows the reader's view
    would scope away."""
    import psycopg
    from mindhive import vectors
    with psycopg.connect(os.environ[ADMIN], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT segment, seg_line, event_type, event_id, dim, norm "
                "FROM mindhive.event_embeddings "
                "WHERE project = %s AND model = %s "
                "ORDER BY segment, seg_line",
                (project, model or vectors.EMBEDDING_MODEL))
            return cur.fetchall()


@needs_server
def test_only_semantic_events_get_a_vector(migrated, store, empty_index,
                                           fake_encoder):
    """`command_run`, `file_touched` and `assistant_response` are the bulk of a
    real ledger and carry no meaning worth a vector. Indexing them would triple
    the cost and bury the memos in near-duplicate telemetry."""
    from mindhive import pgmirror
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "we chose the mirror"),
        ("2026-01-01T00:00:01Z", "command_run", "pytest -q"),
        ("2026-01-01T00:00:02Z", "caveat", "the fingerprint is size only"),
        ("2026-01-01T00:00:03Z", "file_touched", "src/mindhive/events.py"),
    ])
    out = pgmirror.embed_events(store["name"])
    assert out["eligible"] == 2
    assert out["embedded"] == 2
    types = sorted(r[2] for r in _embedding_rows(store["name"]))
    assert types == ["caveat", "decision"]


@needs_server
def test_a_second_run_encodes_nothing_and_opens_no_encoder(migrated, store,
                                                           empty_index,
                                                           fake_encoder,
                                                           monkeypatch):
    """The property that makes the command runnable on a schedule. Measured on
    the real ledger at 47.9 s then 0.98 s; here it is asserted rather than
    timed, by replacing the encoder with something that explodes."""
    from mindhive import pgmirror, vectors
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "first"),
        ("2026-01-01T00:00:01Z", "caveat", "second"),
    ])
    pgmirror.embed_events(store["name"])

    def explode(texts):
        raise AssertionError("a re-run re-encoded an event it already had")

    monkeypatch.setattr(vectors, "encode", explode)
    out = pgmirror.embed_events(store["name"])
    assert out == {"project": store["name"],
                   "model": vectors.EMBEDDING_MODEL, "dim": None,
                   "eligible": 2, "embedded": 0, "skipped": 2, "removed": 0,
                   "indexed": 2}


@needs_server
def test_an_event_rewritten_in_place_loses_its_stale_vector(migrated, store,
                                                            empty_index,
                                                            fake_encoder):
    """The reason presence alone is not the test. `(segment, seg_line)` is
    stable across a rewrite, so a key-only check would keep the OLD vector
    forever — and a stale vector still returns confident-looking hits."""
    from mindhive import pgmirror
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "the original wording"),
    ])
    pgmirror.embed_events(store["name"])
    before = _embedding_rows(store["name"])
    assert len(before) == 1

    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "an entirely different wording"),
    ])
    out = pgmirror.embed_events(store["name"])
    assert out["embedded"] == 1 and out["removed"] == 1 and out["skipped"] == 0
    after = _embedding_rows(store["name"])
    assert len(after) == 1
    assert after[0][3] != before[0][3], "the content hash did not change"


@needs_server
def test_an_event_that_left_the_ledger_leaves_no_orphan_vector(migrated, store,
                                                               empty_index,
                                                               fake_encoder):
    from mindhive import pgmirror
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "kept"),
        ("2026-01-01T00:00:01Z", "decision", "removed later"),
    ])
    pgmirror.embed_events(store["name"])
    assert len(_embedding_rows(store["name"])) == 2

    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "kept"),
    ])
    out = pgmirror.embed_events(store["name"])
    assert out["removed"] == 1 and out["embedded"] == 0
    rows = _embedding_rows(store["name"])
    assert [r[1] for r in rows] == [0]


@needs_server
def test_rebuild_re_encodes_everything(migrated, store, empty_index,
                                       fake_encoder):
    """The flag to reach for when what changed is WHICH events get embedded, not
    which events exist — a case the content hash cannot see."""
    from mindhive import pgmirror
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "one"),
        ("2026-01-01T00:00:01Z", "caveat", "two"),
    ])
    pgmirror.embed_events(store["name"])
    out = pgmirror.embed_events(store["name"], rebuild=True)
    assert out["embedded"] == 2 and out["removed"] == 2 and out["skipped"] == 0
    assert len(_embedding_rows(store["name"])) == 2


@needs_server
def test_two_models_coexist_rather_than_overwriting_each_other(migrated, store,
                                                               empty_index,
                                                               fake_encoder):
    """`model` is part of the primary key so a migration between encoders adds
    rows instead of silently mixing two incompatible geometries under one name.
    Nothing would raise if it did — the scores would just stop meaning
    anything."""
    from mindhive import pgmirror
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "one"),
    ])
    pgmirror.embed_events(store["name"], model="model-a")
    pgmirror.embed_events(store["name"], model="model-b")
    assert len(_embedding_rows(store["name"], "model-a")) == 1
    assert len(_embedding_rows(store["name"], "model-b")) == 1
    out = pgmirror.embed_events(store["name"], model="model-a")
    assert out["embedded"] == 0 and out["skipped"] == 1


@needs_server
def test_embedding_one_namespace_leaves_another_alone(migrated, tmp_path,
                                                      monkeypatch, fake_encoder):
    """Same property `rebuild` has, and the same reason: one database holds every
    project and a DELETE that forgot its `project` predicate would take the
    others with it."""
    from mindhive import paths, pgmirror
    home = tmp_path / "home"
    names = []
    for tag in ("keep", "churn"):
        name = paths.normalize(f"pgemb-{tmp_path.name}-{tag}")
        (home / name / "events").mkdir(parents=True)
        (home / name / "events" / "alpha.jsonl").write_text(
            json.dumps({"ts": "2026-01-01T00:00:00Z", "session_id": "alpha",
                        "type": "decision", "payload": {"text": tag},
                        "tags": []}) + "\n", encoding="utf-8")
        names.append(name)
    monkeypatch.setenv("MINDHIVE_HOME", str(home))

    pgmirror.embed_events(names[0])
    pgmirror.embed_events(names[1])
    assert len(_embedding_rows(names[0])) == 1

    pgmirror.embed_events(names[1], rebuild=True)
    assert len(_embedding_rows(names[0])) == 1, \
        "rebuilding one namespace's vectors deleted another's"


@needs_server
def test_the_width_check_refuses_a_blob_the_dim_does_not_describe(migrated,
                                                                  store):
    """The DDL constraint, asserted against the server rather than read off the
    file. A short buffer reshapes without error into a matrix of the wrong
    length, so this is the only layer that can refuse it structurally."""
    import psycopg
    with psycopg.connect(os.environ[ADMIN], autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO mindhive.event_embeddings "
                    "(project, segment, seg_line, model, event_id, dim, "
                    " embedding, norm) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (store["name"], "a.jsonl", 0, "m", "e", 4, b"\x00\x11", 1.0))


@needs_server
def test_the_reader_sees_the_two_vector_views_and_still_not_the_base_table(
        migrated, store, empty_index, fake_encoder):
    """The privilege split, extended to the new table. `event_embeddings` is
    metadata only so a `SELECT *` cannot blow a context window; `event_vectors`
    carries the bytes for the one tool that ranks them."""
    import psycopg
    from mindhive import pgmirror
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "visible"),
    ])
    pgmirror.embed_events(store["name"])

    with psycopg.connect(os.environ[READER], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('mindhive.project', %s, false)",
                        (store["name"],))
            cur.execute("SELECT count(*) FROM mindhive_ro.event_embeddings")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM mindhive_ro.event_vectors")
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'mindhive_ro' "
                "AND table_name = 'event_embeddings'")
            names = {r[0] for r in cur.fetchall()}
            assert "embedding" not in names, \
                "the metadata view exposes the raw bytes it exists to hide"
            assert "embedding_bytes" in names
    with psycopg.connect(os.environ[READER], autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT count(*) FROM mindhive.event_embeddings")


@needs_server
def test_an_unset_namespace_hides_every_vector_too(migrated, store,
                                                   empty_index, fake_encoder):
    """The views carry `current_setting('mindhive.project', true)`, which is
    NULL when unset, and NULL matches no row. Zero rows is the fail-closed
    direction and it must hold for the new views as well as the old."""
    import psycopg
    from mindhive import pgmirror
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "scoped away"),
    ])
    pgmirror.embed_events(store["name"])
    with psycopg.connect(os.environ[READER], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mindhive_ro.event_embeddings")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM mindhive_ro.event_vectors")
            assert cur.fetchone()[0] == 0


@needs_server
@needs_vectors
def test_the_semantic_tool_finds_a_memo_over_the_real_server(migrated, store,
                                                             empty_index,
                                                             fake_encoder):
    """End to end with the fake encoder: loader writes, tool reads, the hit is
    hydrated from `mindhive_ro.events`. What this proves is the PLUMBING — that
    the two statements compile, that `(segment, seg_line)` lines the two tables
    up, and that the envelope comes back shaped. Semantic quality is the next
    test's job."""
    from mindhive import pgmirror, pgtools
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "aaaa"),
        ("2026-01-01T00:00:01Z", "caveat", "zzzzzzzzzzzz"),
    ])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])
    pgmirror.embed_events(store["name"])

    env = pgtools.call("search_events_by_meaning",
                       {"query": "aaaa", "limit": 2, "project": store["name"]})
    assert env["ok"] is True, env.get("error")
    assert env["row_count"] == 2
    assert env["rows"][0]["payload"]["text"] == "aaaa"
    assert env["summary"]["vectors_compared"] == 2
    assert env["summary"]["orphaned"] == 0
    assert env["columns"][0] == "score"


@needs_server
@needs_vectors
def test_a_type_filter_narrows_the_search_before_ranking(migrated, store,
                                                         empty_index,
                                                         fake_encoder):
    from mindhive import pgmirror, pgtools
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "aaaa"),
        ("2026-01-01T00:00:01Z", "caveat", "aaaa but a caveat"),
    ])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])
    pgmirror.embed_events(store["name"])

    env = pgtools.call("search_events_by_meaning",
                       {"query": "aaaa", "event_type": "caveat",
                        "project": store["name"]})
    assert env["ok"] is True
    assert env["summary"]["vectors_compared"] == 1
    assert [r["event_type"] for r in env["rows"]] == ["caveat"]


@needs_server
@needs_vectors
def test_searching_a_namespace_with_no_index_names_the_command(migrated, store,
                                                               empty_index,
                                                               fake_encoder):
    """Not an error and not an empty ledger — and the difference is what the
    model has to be told, because the same events are still reachable by type."""
    from mindhive import pgmirror, pgtools
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "present but unindexed"),
    ])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])

    env = pgtools.call("search_events_by_meaning",
                       {"query": "anything", "project": store["name"]})
    assert env["ok"] is True
    assert env["row_count"] == 0
    assert "hive pg embed" in env["notice"]


@needs_server
@needs_vectors
def test_the_real_encoder_ranks_a_paraphrase_above_an_unrelated_memo(migrated,
                                                                     store,
                                                                     empty_index):
    """The one test that loads the actual model, and the only one that can say
    the feature WORKS rather than that it runs. Everything above uses a fake
    encoder, which would happily pass while the real one was never installed.

    The assertion is a RANKING, not a threshold: absolute cosine values move
    with the model, but "the paraphrase beats the unrelated memo" is the
    property the feature is for. Skipped when the extra is absent."""
    pytest.importorskip("sentence_transformers",
                        reason="needs pip install -e .[vectors]")
    from mindhive import pgmirror, pgtools
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision",
         "we rebuilt the database mirror because it had fallen behind the "
         "append-only log"),
        ("2026-01-01T00:00:01Z", "decision",
         "the office coffee machine is broken and nobody has called anyone"),
    ])
    pgmirror.rebuild(store["name"])
    pgmirror.parity(store["name"])
    pgmirror.embed_events(store["name"])

    env = pgtools.call(
        "search_events_by_meaning",
        {"query": "why did the copy of the ledger have to be reloaded",
         "limit": 2, "project": store["name"]})
    assert env["ok"] is True, env.get("error")
    assert env["row_count"] == 2
    top, other = env["rows"]
    assert "mirror" in top["payload"]["text"], \
        "the paraphrase did not rank above an unrelated memo"
    assert top["score"] > other["score"]


@needs_server
def test_the_cli_reports_what_it_indexed(migrated, store, empty_index,
                                         fake_encoder, capsys):
    """`hive pg embed` prints the loader's own dict, so an operator reads the
    same numbers a caller gets — including `skipped`, which is the evidence the
    incremental path worked."""
    from mindhive import __main__ as cli
    write_typed_segment(store, "alpha", [
        ("2026-01-01T00:00:00Z", "decision", "one"),
    ])
    assert cli.main(["pg", "embed", "--project", store["name"]]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["embedded"] == 1 and first["skipped"] == 0

    assert cli.main(["pg", "embed", "--project", store["name"]]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["embedded"] == 0 and second["skipped"] == 1
