# tests/test_storage_migration.py
"""`storage.migration` — replay, verified, idempotent, chunked.

The destination is a fake whose `transaction()` yields a STAGING LIST rather
than a cursor, so "committed" and "rolled back" are observable facts rather
than counted calls: a chunk that rolls back leaves nothing in `inserted`,
which is the property the chunked-commit design rests on.

The source is the real `LocalEventStore` wherever the question is about
replaying a real ledger, and a hand-built stream wherever the question is
about a BROKEN chain — because a stream that came out of `records.fold_rows`
is self-consistent by construction and cannot break. That limit is asserted
directly below rather than left for someone to discover.
"""
from contextlib import contextmanager

import pytest

from mindhive import chain, events, paths, pgmirror
from mindhive.core.models import GENESIS_PREV_HASH
from mindhive.storage import migration, records
from mindhive.storage.interface import DataIntegrityError
from mindhive.storage.local_driver import LocalEventStore
from mindhive.storage.migration import (EventReplayMigrationEngine,
                                        HashChainVerifier, MigrationReport)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakePg:
    """A destination that stages inserts and only keeps them on commit."""

    def __init__(self, have=None, refuse=()):
        self.have = dict(have or {})
        self.refuse = set(refuse)
        self.inserted: list = []
        self.transactions = self.commits = self.rollbacks = 0
        self.max_calls: list = []

    def segment_of(self, session_id) -> str:
        return events.segment_name(session_id)

    def max_seg_line(self, segment) -> int:
        self.max_calls.append(segment)
        return self.have.get(segment, -1)

    @contextmanager
    def transaction(self):
        self.transactions += 1
        staged: list = []
        try:
            yield staged
        except BaseException:
            self.rollbacks += 1
            raise
        self.commits += 1
        self.inserted.extend(staged)

    def insert_record(self, cur, rec) -> bool:
        segment = self.segment_of(rec.session_id)
        key = (segment, rec.seg_line - 1)
        if key in self.refuse:
            return False
        cur.append((segment, rec.seg_line - 1, rec.timestamp,
                    rec.session_id, rec.event_type))
        return True


class FakeLocal:
    """A source that hands back exactly the records it was given, so a test
    can present a chain that a real fold could never produce."""

    def __init__(self, streams):
        self.streams = dict(streams)

    def list_sessions(self) -> list:
        return sorted(self.streams)

    def read_stream(self, session_id, from_line: int = 1):
        for rec in self.streams[session_id]:
            if rec.seg_line >= from_line:
                yield rec


def _append(n, session_id="s1"):
    return [events.append_event("decision", {"text": f"d{i}"},
                                session_id=session_id) for i in range(n)]


def _fold(session_id="s1"):
    seg = paths.events_dir() / events.segment_name(session_id)
    return records.fold_rows(records.segment_rows(seg))


def _engine(**kw):
    kw.setdefault("clock", iter(range(1000)).__next__)
    return EventReplayMigrationEngine(**kw)


# --------------------------------------------------------------------------
# HashChainVerifier
# --------------------------------------------------------------------------

def test_a_clean_chain_walks_from_genesis_to_the_final_head():
    _append(3)
    fold = _fold()
    v = HashChainVerifier()
    assert v.head == GENESIS_PREV_HASH
    for rec in fold:
        assert v.verify_next(rec) == rec.chain_hash
    assert (v.head, v.checked) == (fold[-1].chain_hash, 3)


def test_a_record_pointing_at_another_chain_is_refused():
    _append(2)
    first, second = _fold()
    v = HashChainVerifier()
    with pytest.raises(DataIntegrityError) as e:
        v.verify_next(second)                  # skipping `first`
    assert second.event_id in str(e.value)
    assert "prev_hash" in str(e.value)


def test_a_record_whose_own_hash_does_not_match_its_bytes_is_refused():
    """The second check, which the first cannot make: a correct `prev_hash`
    with an edited payload passes the link test and fails here."""
    _append(1)
    (rec,) = _fold()
    import dataclasses
    tampered = dataclasses.replace(rec, payload={"text": "edited"})
    with pytest.raises(DataIntegrityError) as e:
        HashChainVerifier().verify_next(tampered)
    assert "chain_hash" in str(e.value)
    assert rec.event_id in str(e.value)


def test_a_record_spliced_from_another_chain_fails_the_link_test():
    """And the first check, which the second cannot make: the record is
    perfectly self-hashed — it just belongs somewhere else."""
    _append(2, session_id="alpha")
    _append(2, session_id="beta")
    alpha, beta = _fold("alpha"), _fold("beta")
    v = HashChainVerifier()
    v.verify_next(alpha[0])
    with pytest.raises(DataIntegrityError):
        v.verify_next(beta[1])


def test_the_messages_are_short_enough_to_read():
    """Digests are truncated: a message carrying two 64-character hashes in
    full is a message an operator skims past."""
    _append(2)
    _first, second = _fold()
    with pytest.raises(DataIntegrityError) as e:
        HashChainVerifier().verify_next(second)
    assert len(str(e.value)) < 200
    assert second.prev_hash not in str(e.value), "the full digest was printed"


def test_a_verifier_carried_across_segments_in_name_order_is_the_namespace():
    """PARITY with `chain.write_chain`. The engine uses a verifier PER session,
    because that is the chain `read_stream` produces; this pins that the same
    class computes the namespace chain when it is fed the namespace order, so
    the two folds are one piece of arithmetic asked two questions."""
    _append(2, session_id="alpha")
    _append(3, session_id="beta")
    v = HashChainVerifier()
    for seg in sorted(paths.events_dir().glob("*.jsonl"), key=lambda p: p.name):
        for _line, ev in records.segment_rows(seg):
            rec = records.build_record(ev, _line, v.head)
            v.verify_next(rec)
    assert v.head == chain.write_chain()["head"]
    assert chain.verify()["ok"] is True


def test_a_verifier_can_be_started_from_a_head_that_is_not_genesis():
    _append(3)
    fold = _fold()
    v = HashChainVerifier(fold[0].chain_hash)
    assert v.verify_next(fold[1]) == fold[1].chain_hash


def test_a_locally_folded_stream_cannot_break_and_that_is_stated():
    """The limit, asserted. `fold_rows` COMPUTES both hashes, so no amount of
    tampering with the SEGMENT produces a stream this verifier rejects — the
    fold simply yields a different, equally self-consistent chain. Detecting an
    edited ledger is `chain.verify`'s job, and `hive db migrate` runs it."""
    _append(3)
    seg = paths.events_dir() / events.segment_name("s1")
    text = seg.read_text(encoding="utf-8").replace('"d1"', '"edited"')
    seg.write_text(text, encoding="utf-8")
    v = HashChainVerifier()
    for rec in _fold():
        v.verify_next(rec)                    # no raise: the fold is self-made
    assert chain.verify()["ok"] is False, "the checkpoint is what notices"


# --------------------------------------------------------------------------
# a fresh replay
# --------------------------------------------------------------------------

def test_a_fresh_replay_inserts_every_row_of_every_session():
    _append(2, session_id="alpha")
    _append(3, session_id="beta")
    pg = FakePg()
    report = _engine().migrate_to_postgres(LocalEventStore(), pg)
    assert (report.sessions, report.events_read) == (2, 5)
    assert (report.events_inserted, report.events_skipped) == (5, 0)
    assert report.chain_ok is True and report.error is None
    assert report.dry_run is False
    assert len(pg.inserted) == 5


def test_what_the_destination_received_is_what_the_mirror_loader_would_load():
    """THE parity pin. `pgmirror.scan_segments` is what `hive pg rebuild`
    loads; a replay that produced different `(segment, seg_line)` coordinates
    would build a mirror that disagrees with a rebuild of the same ledger, and
    nothing downstream would notice until a parity check months later."""
    _append(2, session_id="alpha")
    _append(3, session_id="beta")
    pg = FakePg()
    _engine().migrate_to_postgres(LocalEventStore(), pg)
    want = sorted((seg, line, ev.ts, ev.session_id, ev.type)
                  for seg, line, ev in pgmirror.scan_segments("test"))
    assert sorted(pg.inserted) == want


def test_the_coordinates_handed_over_are_the_mirrors_zero_based_ones():
    _append(3)
    pg = FakePg()
    _engine().migrate_to_postgres(LocalEventStore(), pg)
    assert [row[1] for row in pg.inserted] == [0, 1, 2]


def test_an_empty_ledger_is_a_clean_report_not_an_error():
    report = _engine().migrate_to_postgres(LocalEventStore(), FakePg())
    assert report.as_dict() == {"sessions": 0, "sessions_quarantined": 0, "events_read": 0,
                                "events_inserted": 0, "events_skipped": 0,
                                "chain_ok": True, "duration_s": 1,
                                "dry_run": False, "error": None}


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------

def test_a_quarantined_session_is_replayed_not_walked_empty():
    """`list_sessions` names a quarantined segment by its `unsafe-<hash>`
    stem, and a read by that stem used to find nothing: the run reported the
    session with zero rows read, silently. Every row of it is offered now,
    under the segment its rows' own raw id maps to -- the one the
    destination's `insert_record` computes -- and the report counts it."""
    events.append_event("decision", {"text": "q0"}, session_id="../escape")
    events.append_event("decision", {"text": "q1"}, session_id="../escape")
    events.append_event("decision", {"text": "q2"}, session_id="../escape")
    _append(2)                                        # a plain session beside it
    local = LocalEventStore()
    quarantined = events.segment_name("../escape")
    assert local.list_sessions() == sorted([quarantined[:-6], "s1"])
    pg = FakePg()
    report = _engine().migrate_to_postgres(local, pg)
    assert (report.sessions, report.sessions_quarantined) == (2, 1)
    assert (report.events_read, report.events_inserted) == (5, 5)
    assert {seg for seg, *_ in pg.inserted} == {quarantined, "s1.jsonl"}
    assert sorted(sid for _, _, _, sid, _ in pg.inserted) == \
        ["../escape"] * 3 + ["s1"] * 2
    # Idempotent by arithmetic for the quarantined session too: the second
    # run asks the destination about the segment the rows really landed in.
    pg.have = {quarantined: 2, "s1.jsonl": 1}
    again = _engine().migrate_to_postgres(local, pg)
    assert (again.events_read, again.events_inserted, again.events_skipped) == \
        (5, 0, 5)
    assert quarantined in pg.max_calls


def test_a_plain_ledger_counts_no_quarantined_sessions():
    _append(2)
    report = _engine().migrate_to_postgres(LocalEventStore(), FakePg())
    assert (report.sessions, report.sessions_quarantined) == (1, 0)
    assert report.as_dict()["sessions_quarantined"] == 0


def test_a_second_run_skips_everything_without_writing():
    """One SELECT per session and no writes at all — the number worth
    reporting, because a run that re-wrote everything would look identical
    without it."""
    _append(3)
    pg = FakePg(have={"s1.jsonl": 2})
    report = _engine().migrate_to_postgres(LocalEventStore(), pg)
    assert (report.events_read, report.events_inserted,
            report.events_skipped) == (3, 0, 3)
    assert pg.inserted == []
    assert pg.max_calls == ["s1.jsonl"]


def test_a_partially_migrated_session_resumes_from_where_it_stopped():
    _append(5)
    pg = FakePg(have={"s1.jsonl": 1})
    report = _engine().migrate_to_postgres(LocalEventStore(), pg)
    assert (report.events_inserted, report.events_skipped) == (3, 2)
    assert [row[1] for row in pg.inserted] == [2, 3, 4]


def test_the_skip_is_arithmetic_not_a_per_row_probe():
    """`max_seg_line` once per session, not once per row: the mirror's lines
    are contiguous from 0, so the highest one it holds answers for all of
    them."""
    _append(50)
    pg = FakePg(have={"s1.jsonl": 49})
    _engine().migrate_to_postgres(LocalEventStore(), pg)
    assert len(pg.max_calls) == 1


def test_a_row_the_destinations_conflict_swallowed_counts_as_skipped():
    """A race, not a failure: another writer got there first, the row is
    present, and `ON CONFLICT DO NOTHING` reported it honestly."""
    _append(3)
    pg = FakePg(refuse={("s1.jsonl", 1)})
    report = _engine().migrate_to_postgres(LocalEventStore(), pg)
    assert (report.events_inserted, report.events_skipped) == (2, 1)


# --------------------------------------------------------------------------
# a broken chain
# --------------------------------------------------------------------------

def _broken_stream():
    """Three records whose middle one points at nothing on this chain."""
    evs = [events.Event(ts=f"2026-01-01T00:00:0{i}Z", session_id="s1",
                        type="decision", payload={"text": f"d{i}"}, tags=[])
           for i in range(3)]
    good = records.fold_rows([(i + 1, ev) for i, ev in enumerate(evs)])
    import dataclasses
    return [good[0], dataclasses.replace(good[1], prev_hash="de" * 32), good[2]]


def test_a_break_stops_the_session_and_reports_it():
    pg = FakePg()
    source = FakeLocal({"s1": _broken_stream()})
    report = _engine().migrate_to_postgres(source, pg)
    assert report.chain_ok is False
    assert "rantai putus" in report.error and "s1:2" in report.error


def test_a_break_rolls_the_chunk_back_rather_than_committing_it():
    pg = FakePg()
    _engine().migrate_to_postgres(FakeLocal({"s1": _broken_stream()}), pg)
    assert (pg.rollbacks, pg.commits) == (1, 0)
    assert pg.inserted == [], "the good first row was committed anyway"


def test_a_rolled_back_chunk_reports_no_inserts():
    """The counts describe what HAPPENED, not what was attempted. The reads
    are counted because they really did happen."""
    pg = FakePg()
    report = _engine().migrate_to_postgres(FakeLocal({"s1": _broken_stream()}),
                                           pg)
    assert (report.events_inserted, report.events_skipped) == (0, 0)
    assert report.events_read == 1


def test_the_first_break_stops_the_run_not_just_its_session():
    """A ledger broken at session 1 is a ledger under investigation; migrating
    session 2 would produce a destination that is partly trustworthy with
    nothing recording which part."""
    source = FakeLocal({"a": _broken_stream(), "b": _broken_stream()})
    report = _engine().migrate_to_postgres(source, FakePg())
    assert report.sessions == 1


def test_earlier_committed_chunks_survive_a_later_break():
    """The chunked-commit trade, made explicit: a prefix stays, and a re-run
    skips it. That is safe only because every insert is idempotent."""
    evs = [events.Event(ts=f"2026-01-01T00:00:0{i}Z", session_id="s1",
                        type="decision", payload={"text": f"d{i}"}, tags=[])
           for i in range(4)]
    good = records.fold_rows([(i + 1, ev) for i, ev in enumerate(evs)])
    import dataclasses
    stream = good[:2] + [dataclasses.replace(good[2], prev_hash="de" * 32),
                         good[3]]
    pg = FakePg()
    report = _engine(batch=2).migrate_to_postgres(FakeLocal({"s1": stream}), pg)
    assert (pg.commits, pg.rollbacks) == (1, 1)
    assert [row[1] for row in pg.inserted] == [0, 1]
    assert (report.events_inserted, report.events_read) == (2, 2)
    assert report.chain_ok is False


# --------------------------------------------------------------------------
# chunked commits
# --------------------------------------------------------------------------

def test_a_session_is_committed_in_chunks_of_batch():
    _append(7)
    pg = FakePg()
    _engine(batch=3).migrate_to_postgres(LocalEventStore(), pg)
    assert pg.transactions == pg.commits == 3     # 3 + 3 + 1
    assert len(pg.inserted) == 7


def test_a_session_that_fits_in_one_batch_is_one_transaction():
    _append(3)
    pg = FakePg()
    _engine(batch=500).migrate_to_postgres(LocalEventStore(), pg)
    assert pg.transactions == 1


def test_an_exactly_full_batch_does_not_open_an_empty_second_one():
    """It does open one more to discover the stream is finished — the cheapest
    honest way to ask, and it commits nothing. Pinned so the count is a
    decision rather than a surprise."""
    _append(4)
    pg = FakePg()
    _engine(batch=4).migrate_to_postgres(LocalEventStore(), pg)
    assert pg.transactions == 2
    assert len(pg.inserted) == 4


def test_a_batch_below_one_is_refused():
    for bad in (0, -1):
        with pytest.raises(ValueError, match="at least 1"):
            EventReplayMigrationEngine(batch=bad)


# --------------------------------------------------------------------------
# dry runs
# --------------------------------------------------------------------------

def test_a_dry_run_writes_nothing_and_opens_no_transaction():
    _append(3)
    pg = FakePg()
    report = _engine(dry_run=True).migrate_to_postgres(LocalEventStore(), pg)
    assert report.dry_run is True
    assert (report.events_read, report.events_inserted) == (3, 3)
    assert (pg.transactions, pg.inserted) == (0, [])


def test_a_dry_run_still_asks_what_the_destination_holds():
    """Otherwise its `events_inserted` would be a guess rather than a
    forecast."""
    _append(3)
    pg = FakePg(have={"s1.jsonl": 0})
    report = _engine(dry_run=True).migrate_to_postgres(LocalEventStore(), pg)
    assert (report.events_inserted, report.events_skipped) == (2, 1)


def test_no_destination_at_all_is_a_dry_run_by_definition():
    """There is nowhere to write, so claiming inserts would be a lie the
    report would carry into a log."""
    _append(2)
    report = _engine().migrate_to_postgres(LocalEventStore(), None)
    assert report.dry_run is True
    assert (report.events_read, report.events_inserted) == (2, 2)


def test_a_chain_is_still_verified_with_no_destination():
    report = _engine().migrate_to_postgres(FakeLocal({"s1": _broken_stream()}),
                                           None)
    assert report.chain_ok is False and "rantai putus" in report.error


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def test_the_duration_comes_from_the_clock_seam():
    _append(1)
    ticks = iter([100.0, 104.5])
    engine = EventReplayMigrationEngine(clock=lambda: next(ticks))
    assert engine.migrate_to_postgres(LocalEventStore(),
                                      FakePg()).duration_s == 4.5


def test_the_report_cannot_be_edited_after_the_fact():
    with pytest.raises(Exception):
        MigrationReport().sessions = 9


def test_as_dict_carries_every_field_the_cli_prints():
    report = MigrationReport(sessions=1, events_read=2, events_inserted=2,
                             events_skipped=0, chain_ok=False,
                             duration_s=0.5, dry_run=True, error="boom")
    assert report.as_dict() == {"sessions": 1, "sessions_quarantined": 0,
                                "events_read": 2,
                                "events_inserted": 2, "events_skipped": 0,
                                "chain_ok": False, "duration_s": 0.5,
                                "dry_run": True, "error": "boom"}


def test_the_engine_names_no_database_and_no_driver():
    """The engine writes THROUGH a destination it was handed; it must not know
    what that destination is. A `pgmirror` or `psycopg` name here would make
    the replay untestable without a server and would put the owner-DSN module
    on the storage package's import graph."""
    import ast
    tree = ast.parse(open(migration.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "psycopg" not in imported and "pgmirror" not in imported
    assert imported <= {"time", "dataclasses", "", "core", "interface",
                        "postgres_driver"}, sorted(imported)
