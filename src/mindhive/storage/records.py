# SPDX-License-Identifier: Apache-2.0
"""The ONE fold from ledger rows to `EventRecord`s (blueprint P1i, MEB MIG-01).

`chain._scan`/`_fold` answer one question -- the head over a whole namespace --
and answer it by walking every segment. The storage SPI needs a different
shape of the same arithmetic: the records of ONE segment, each carrying the
`prev_hash` it was folded onto, so a driver can hand a reader row 400 without
rebuilding rows 1-399 from scratch on every call, and so a migration can
verify the chain as it replays it.

Two shapes, one rule. Everything here is built from `events._segment_parts`
and `events._row_to_event`, which is where the four total readers and
`chain._scan` already agree about where a row ends and which rows survive.
This module does not split lines itself, does not call `splitlines()`, and
must not learn to: `tests/test_architecture_drift.py` pins that rule, and the
tamper-evidence hole it closed (a raw U+2028 shifting every later `seg_line`
in a segment) is reachable again by any edit that reintroduces a second
splitter.

**Coordinates.** `EventRecord.seg_line` is the 1-BASED physical line, counting
blank and malformed lines exactly as `events._segment_lines` and
`pgmirror.scan_segments` count them -- those two number from 0, and the
conversion happens in exactly one place on each side of
`storage.postgres_driver`. `chain._scan`'s `line_index` is the same number
minus one.
"""
from .. import events, paths
from ..core.models import GENESIS_PREV_HASH, EventRecord, compute_chain_hash

__all__ = ["record_row", "event_of", "build_record", "fold_rows",
           "segment_rows", "is_quarantine_stem", "listed_segment_name"]


def record_row(rec) -> dict:
    """The five `Event` keys a record's `chain_hash` was computed over.

    Byte-for-byte the material `events.to_dict` produces, which is what makes
    `canonical_json(record_row(rec))` and `canonical_json(to_dict(ev))` the
    same string for the same row. An `EventRecord` carries four more fields --
    `event_id`, `seg_line`, `prev_hash`, `chain_hash` -- and none of them may
    reach this dict: every checkpoint on disk was folded over the five, so a
    sixth key would move every head in the store on the first verify."""
    return {"ts": rec.timestamp,
            "session_id": rec.session_id,
            "type": rec.event_type,
            "payload": rec.payload,
            "tags": list(rec.tags)}


def event_of(ev_record):
    """The `Event` a record was folded from.

    `record_row` returns exactly `Event`'s five fields, so this is a
    reconstruction rather than a translation -- which is what makes
    `vectors.event_id(event_of(rec))` provably the id `hive embed` would give
    the same event, and what lets the Postgres driver hand a record back to a
    writer that only speaks events."""
    return events.Event(**record_row(ev_record))


def build_record(ev, seg_line: int, prev_hash: str) -> EventRecord:
    """One `Event` at one physical line, chained onto *prev_hash*.

    `event_id` is `f"{session_id}:{seg_line}"` -- the PHYSICAL address, the
    same key `watcher`'s queue and `vectors.VectorStoreManager.embed_batch`
    use, so a row indexed by the watcher and a row replayed by the migration
    engine are the same id. It is deliberately NOT `vectors.event_id`, which
    is content-addressed: two identical events at two lines are one vector and
    two ledger rows, and this module speaks the ledger's coordinates."""
    canonical = events.canonical_json(events.to_dict(ev))
    return EventRecord(
        event_id=f"{ev.session_id}:{seg_line}",
        session_id=ev.session_id,
        seg_line=seg_line,
        timestamp=ev.ts,
        event_type=ev.type,
        payload=ev.payload,
        tags=list(ev.tags or []),
        prev_hash=prev_hash,
        chain_hash=compute_chain_hash(prev_hash, canonical))


def fold_rows(rows, prev_hash: str = GENESIS_PREV_HASH) -> list:
    """`(seg_line, Event)` pairs folded into records, in the order given.

    *prev_hash* is the head the first row chains onto, so a caller that
    already folded lines 1-99 can continue from line 100 without re-reading
    them. Carrying one head across segments taken in NAME order reproduces
    `chain._fold` over the whole namespace, which is what
    `EventReplayMigrationEngine` relies on and `tests/test_storage_records.py`
    pins against `chain.compute_chain`."""
    out: list = []
    prev = prev_hash
    for seg_line, ev in rows:
        rec = build_record(ev, seg_line, prev)
        out.append(rec)
        prev = rec.chain_hash
    return out


def segment_rows(seg) -> list:
    """One segment's `(seg_line, Event)` pairs -- COMPLETE lines only.

    A segment ending mid-row contributes every whole line and not the
    fragment, for the reason `chain._scan` gives: those trailing bytes are
    still mutable (the next append concatenates onto them), so a record built
    over them would attest to something that can still change. Blank and torn
    lines are skipped but still COUNTED, because `seg_line` is a physical
    address and a reader that renumbered around a blank line would hand the
    mirror a join key meaning something else."""
    parts = events._segment_parts(seg)
    complete = len(parts) - 1          # the tail after the last \n is partial
    out: list = []
    for i, raw in enumerate(parts):
        if i >= complete:
            break
        text = raw.strip()
        if not text:
            continue
        ev = events._row_to_event(text)
        if ev is None:
            continue                   # torn row: reads stay total
        out.append((i + 1, ev))        # 1-BASED, per the module docstring
    return out


def is_quarantine_stem(stem) -> bool:
    """Whether *stem*, as `EventStore.list_sessions` reports it, names a
    quarantine segment: one safe component (`events.SESSION_ID_RE`) carrying
    the reserved prefix in any case -- exactly the ids `events.segment_name`
    refuses for the reservation alone."""
    raw = paths.text_of(stem)
    return (isinstance(raw, str)
            and events.SESSION_ID_RE.fullmatch(raw) is not None
            and raw[:len(events.QUARANTINE_PREFIX)].casefold()
            == events.QUARANTINE_PREFIX)


def listed_segment_name(stem) -> str:
    """The segment file a stem `list_sessions` reported names: the inverse
    of that listing, for READS only.

    A quarantined session is listed by its `unsafe-<hash>` stem because that
    is the only name the store can address it by (`storage.interface`). Put
    back through `events.segment_name`, the stem is quarantined a SECOND
    time -- the reservation that keeps a caller from writing into a
    quarantine slot by naming it -- and lands on a segment nothing ever
    wrote. That is how `hive db migrate` walked every quarantined session
    reading zero rows and reported it as replayed. So a quarantine stem is
    its own file here, and anything else is what `segment_name` makes of
    it: a stem that is not one safe component cannot name a file by this
    route either. Never for writes -- `append_event` keeps the reservation."""
    if is_quarantine_stem(stem):
        return paths.text_of(stem) + ".jsonl"
    return events.segment_name(stem)
