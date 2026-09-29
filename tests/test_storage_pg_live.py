# tests/test_storage_pg_live.py — the storage SPI against a real PostgreSQL.
#
# Marked `pg` and deselected by default, exactly as `tests/test_pg_live.py` is
# and for the reason stated there: CI has no server, and a tier that errors on
# every machine without one is a tier people learn to ignore. Run it with:
#
#     pytest -m pg
#
# with MINDHIVE_PG_DSN_ADMIN pointing at a cluster you do not mind having
# rewritten. Every test uses its own namespace under a tmp MINDHIVE_HOME.
#
# WHAT ONLY A SERVER CAN ANSWER. `tests/test_storage_postgres_driver.py` and
# `tests/test_storage_migration.py` drive fakes, and a fake will happily accept
# a statement PostgreSQL would reject. It cannot tell you that
# `%s::jsonb, %s::jsonb` binds, that `ON CONFLICT (project, segment, seg_line)`
# names a real unique index, that `rowcount` really is 0 on a swallowed
# conflict, or that a replayed ledger comes back through `read_stream` with the
# SAME chain hashes it went in with. Those are the claims here.
#
# HONESTY NOTE, because this file has never run. It was written on a machine
# with no PostgreSQL, alongside the code it covers, and has therefore never
# been executed against a server; `IMPLEMENTATION_PLAN.md` records that, as it
# records that the pgvector branch of `007` is unverified for the same reason.
# Written-and-unrun is worth more than not-written -- it is the checklist the
# first operator with a cluster runs -- but it is not evidence, and calling it
# evidence would be the exact failure the directives at the top of this project
# forbid.
import os

import pytest

from mindhive import chain, events, pgmirror
from mindhive.storage.local_driver import LocalEventStore
from mindhive.storage.migration import EventReplayMigrationEngine
from mindhive.storage.postgres_driver import PostgresEventStore

pytestmark = pytest.mark.pg

ADMIN = "MINDHIVE_PG_DSN_ADMIN"

needs_server = pytest.mark.skipif(
    not os.environ.get(ADMIN),
    reason=f"needs {ADMIN} pointing at a live PostgreSQL cluster")


@pytest.fixture
def ns(tmp_path, monkeypatch):
    """An isolated ledger and the namespace name to mirror it under.

    Derived from pytest's own tmp_path name for `test_pg_live.py`'s reason: it
    is already unique per test, and being DETERMINISTIC means a re-run reuses
    the namespace instead of leaving a fresh pile of rows behind each time --
    and starts it EMPTY, see below."""
    from mindhive import paths
    name = paths.normalize(f"spitest-{tmp_path.name}")
    home = tmp_path / "home"
    (home / name / "events").mkdir(parents=True)
    monkeypatch.setenv("MINDHIVE_HOME", str(home))
    monkeypatch.setenv("MINDHIVE_PROJECT", name)
    pgmirror.apply_migrations()
    # The name being deterministic, on a persistent cluster a re-run would
    # otherwise start on LAST run's rows: every `events_inserted` count and
    # round-trip hash below would then be off, wrongly (observed: six failures
    # on the second run against one cluster, none on the first). `refresh` over
    # the still-empty ledger is the loader's own `DELETE ... WHERE project = %s`
    # and a COPY of nothing -- no statement of this file's own. It does not
    # touch `event_embeddings`, so the three vector tests below are not
    # covered by it.
    pgmirror.refresh(name)
    return name


def _seed(project, *sessions):
    store = LocalEventStore(project)
    for session_id, count in sessions:
        for i in range(count):
            store.append(session_id, "decision",
                         {"text": f"{session_id}-{i}", "n": i},
                         tags=["spi"])
    return store


def _dest(project):
    return PostgresEventStore.from_dsn(project=project)


# --------------------------------------------------------------------------
# the replay
# --------------------------------------------------------------------------

@needs_server
def test_a_fresh_replay_inserts_every_event_and_the_chain_verifies(ns):
    local, pg = _seed(ns, ("s1", 5), ("s2", 3)), _dest(ns)
    report = EventReplayMigrationEngine().migrate_to_postgres(local, pg)
    assert report.error is None
    assert report.chain_ok is True
    assert report.sessions == 2
    assert report.events_read == 8
    assert report.events_inserted == 8
    assert report.events_skipped == 0


@needs_server
def test_the_mirror_agrees_with_the_ledger_about_the_chain_head(ns):
    """`pgmirror.parity` folds both sides. It is the one check that says the
    replay produced the ledger and not merely eight rows shaped like it."""
    local, pg = _seed(ns, ("s1", 4)), _dest(ns)
    EventReplayMigrationEngine().migrate_to_postgres(local, pg)
    verdict = pgmirror.parity(ns)
    assert verdict["ok"] is True, verdict


@needs_server
def test_a_second_run_skips_everything_and_writes_nothing(ns):
    """Idempotence by arithmetic: `max(seg_line)` per session, then
    subtraction. The number that proves it is `events_inserted == 0` WITH
    `events_read` unchanged -- a run that read nothing would also insert
    nothing and would mean something else entirely."""
    local, pg = _seed(ns, ("s1", 4), ("s2", 2)), _dest(ns)
    first = EventReplayMigrationEngine().migrate_to_postgres(local, pg)
    second = EventReplayMigrationEngine().migrate_to_postgres(local, _dest(ns))
    assert first.events_inserted == 6
    assert second.events_read == 6
    assert second.events_inserted == 0
    assert second.events_skipped == 6
    assert second.chain_ok is True


@needs_server
def test_a_replay_that_resumes_from_the_middle_inserts_only_the_tail(ns):
    """The interrupted-run case, which is the one the batching exists for."""
    local, pg = _seed(ns, ("s1", 3)), _dest(ns)
    EventReplayMigrationEngine().migrate_to_postgres(local, pg)
    local.append("s1", "decision", {"text": "later"}, tags=["spi"])
    again = EventReplayMigrationEngine().migrate_to_postgres(
        LocalEventStore(ns), _dest(ns))
    assert again.events_read == 4
    assert again.events_skipped == 3
    assert again.events_inserted == 1


@needs_server
def test_the_records_come_back_with_the_hashes_they_went_in_with(ns):
    """The round trip the whole SPI is for. `read_stream` on the mirror folds
    the rows it stored; if a single canonical byte differed -- a jsonb key
    reordered, a tag list respelled, `ts` normalised -- the chain hashes would
    not match and this is where that shows."""
    local, pg = _seed(ns, ("s1", 6)), _dest(ns)
    EventReplayMigrationEngine().migrate_to_postgres(local, pg)
    mine = list(local.read_stream("s1"))
    theirs = list(pg.read_stream("s1"))
    assert [r.seg_line for r in theirs] == [1, 2, 3, 4, 5, 6]
    assert [r.chain_hash for r in theirs] == [r.chain_hash for r in mine]
    assert [r.prev_hash for r in theirs] == [r.prev_hash for r in mine]
    assert [r.event_id for r in theirs] == [r.event_id for r in mine]


@needs_server
def test_the_mirrors_head_is_the_ledgers_head(ns):
    local, pg = _seed(ns, ("s1", 3)), _dest(ns)
    EventReplayMigrationEngine().migrate_to_postgres(local, pg)
    assert list(pg.read_stream("s1"))[-1].chain_hash == \
        chain.write_chain(ns)["head"]


@needs_server
def test_the_payload_survives_jsonb(ns):
    """jsonb does NOT preserve key order or duplicate keys, and the canonical
    form is sorted anyway -- so what this asserts is that the VALUES survive,
    which is what the hash above already implies and what a reader will look
    for when the hash test fails."""
    local, pg = _seed(ns, ("s1", 2)), _dest(ns)
    EventReplayMigrationEngine().migrate_to_postgres(local, pg)
    got = list(pg.read_stream("s1"))
    assert [r.payload for r in got] == [{"text": "s1-0", "n": 0},
                                        {"text": "s1-1", "n": 1}]
    assert [list(r.tags) for r in got] == [["spi"], ["spi"]]


@needs_server
def test_the_segment_list_is_the_ledgers_segment_list(ns):
    local, pg = _seed(ns, ("s1", 1), ("s2", 1), ("s3", 1)), _dest(ns)
    EventReplayMigrationEngine().migrate_to_postgres(local, pg)
    assert sorted(pg.list_sessions()) == ["s1", "s2", "s3"]


@needs_server
def test_a_quarantined_session_reads_back_by_the_stem_the_mirror_lists(ns):
    """A refused id (`../escape`) lands in `unsafe-<hash>.jsonl` on disk and
    under that same segment in the mirror. `list_sessions` answers the stem,
    and the SPI says the stem IS the id the store can address it by -- so a
    read by that name yields the row, not the empty iterator that resolving
    the stem through the write rule (a second quarantine) produced.

    Mirrored with `pgmirror.refresh`, the loader that scans the segment FILES,
    because that is the path that stores the on-disk name verbatim."""
    local = LocalEventStore(ns, strict_session=False)
    rec = local.append("../escape", "decision", {"text": "quarantined"},
                       tags=["spi"])
    pgmirror.refresh(ns)
    pg = _dest(ns)
    (stem,) = pg.list_sessions()
    assert stem.startswith(events.QUARANTINE_PREFIX)
    got = list(pg.read_stream(stem))
    assert [(r.session_id, r.seg_line, r.chain_hash) for r in got] \
        == [("../escape", 1, rec.chain_hash)]


@needs_server
def test_an_insert_that_conflicts_is_swallowed_not_raised(ns):
    """`ON CONFLICT ... DO NOTHING` with `rowcount == 0`. Driven directly
    rather than through the engine, because the engine's skip-by-arithmetic
    means it normally never offers a conflicting row at all -- and the day it
    does (two replays racing) this behaviour is what stops one of them
    aborting the other's transaction."""
    local, pg = _seed(ns, ("s1", 2)), _dest(ns)
    EventReplayMigrationEngine().migrate_to_postgres(local, pg)
    first = list(local.read_stream("s1"))[0]
    with pg.transaction() as cur:
        assert pg.insert_record(cur, first) is False


# --------------------------------------------------------------------------
# the vectors — the BYTEA path, which is the one this cluster has
# --------------------------------------------------------------------------

def _stub_encoder(docs):
    """Deterministic, three-dimensional, and NOT unit length, so the driver's
    normalisation is exercised rather than accidentally satisfied."""
    return [[float(len(d)), 1.0, -2.0] for d in docs]


@needs_server
def test_vectors_round_trip_through_bytea_and_rank_by_similarity(ns):
    pytest.importorskip("numpy")
    from mindhive.storage.pgvector_driver import PgVectorIndex

    local = _seed(ns, ("s1", 3))
    idx = PgVectorIndex(PostgresEventStore.from_dsn(project=ns)._connect,
                        project=ns, encoder=_stub_encoder)
    records = list(local.read_stream("s1"))
    assert idx.embed_batch("events", records) == 3
    hits = idx.query("events", "s1-0", top_k=3)
    assert len(hits) == 3
    assert [score for _, score in hits] == sorted(
        (score for _, score in hits), reverse=True)


@needs_server
def test_indexing_the_same_records_twice_writes_nothing_the_second_time(ns):
    pytest.importorskip("numpy")
    from mindhive.storage.pgvector_driver import PgVectorIndex

    local = _seed(ns, ("s1", 2))
    idx = PgVectorIndex(PostgresEventStore.from_dsn(project=ns)._connect,
                        project=ns, encoder=_stub_encoder)
    records = list(local.read_stream("s1"))
    assert idx.embed_batch("events", records) == 2
    assert idx.embed_batch("events", records) == 0


@needs_server
def test_a_hit_carries_the_chain_coordinates_007_added(ns):
    """The whole point of `prev_hash`/`chain_hash` on `event_embeddings`: a
    similarity hit becomes a full `EventRecord` without re-reading the
    segment it came from."""
    pytest.importorskip("numpy")
    from mindhive.storage.pgvector_driver import PgVectorIndex

    local = _seed(ns, ("s1", 2))
    idx = PgVectorIndex(PostgresEventStore.from_dsn(project=ns)._connect,
                        project=ns, encoder=_stub_encoder)
    mine = list(local.read_stream("s1"))
    idx.embed_batch("events", mine)
    by_id = {r.event_id: r for r in mine}
    for rec, _score in idx.query("events", "s1-0", top_k=2):
        assert rec.chain_hash == by_id[rec.event_id].chain_hash
        assert rec.prev_hash == by_id[rec.event_id].prev_hash
