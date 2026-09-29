# tests/test_events_chain_helpers.py
"""The chain helpers `events.py` exports for readers that build records.

`chain.py` has always folded the ledger into a SHA-256 chain, but the fold
lived INSIDE `chain.compute_chain` and `chain._fold` -- two private loops with
one formula between them and nothing exporting it. The background watcher
(P1h) needs that exact formula: it reads one physical row, wraps it in an
`EventRecord` carrying `prev_hash` / `chain_hash`, and writes those digests
into the vector store's metadata. If the watcher re-derived the formula by
hand it could drift from `chain.py` by one `.encode()` argument and nobody
would notice until `hive verify` and the Chroma metadata disagreed about the
same row. So the primitive moves into `events.py` and this file pins three
things about it:

* **`GENESIS_PREV_HASH` is `chain._ZERO`.** `chain.py` is read-only in this
  phase, so the two constants are not one object; the test pins them equal
  instead, which is the property that matters.
* **`compute_chain_hash(prev, canonical)` is `chain.py`'s fold, byte for
  byte** -- `sha256((prev + canonical).encode("utf-8"))` over the `str` that
  `canonical_json` returns. The PDF's spelling (a `bytes` parameter,
  `prev.encode("ascii")`) is NOT adopted (plan decision AMBIG 3): a second
  scheme would make the watcher's digest and the checkpoint's digest two
  different numbers for one row. The parity tests fold three real appends
  through the helper and demand the head `chain.py` computes for the same
  ledger, in both of `chain.py`'s coordinates (`compute_chain` over
  `read_all`, and `_fold` over `_scan`).
* **The physical row does not change.** `EventRecord` is a frozen RUNTIME
  wrapper that readers construct; nothing serialises it. `append_event`
  still writes exactly `canonical_json(to_dict(ev)) + "\\n"` with the keys
  `{ts, session_id, type, payload, tags}` and still returns `Event`. Writing
  `prev_hash` / `chain_hash` into the row (the PDF's `record_dict`) would
  change the bytes every existing checkpoint was folded over and turn every
  namespace red on the first append.

`canonical_json` keeps its `-> str` contract for the same reason: `chain.py`,
`pgmirror` and every writer consume a str, and the chain covers the UTF-8
bytes of that str. The stability tests here (U+2028 raw, shuffled keys) are
what make "same row, same digest" true across platforms.
"""
import dataclasses
import hashlib
import itertools
import json
import typing

import pytest

from mindhive import chain, events, paths


# --- fixture builders ---------------------------------------------------------

def _add(text, session_id="s", ts="2026-06-20T10:00:00Z", etype="decision"):
    return events.append_event(etype, {"text": text}, session_id=session_id, ts=ts)


def _fold_with_helper(evs) -> str:
    """The fold a reader is expected to run: start at the genesis constant and
    chain every event's canonical row through `compute_chain_hash`. This is
    the loop `chain.compute_chain` runs inline, spelled through the helper."""
    prev = events.GENESIS_PREV_HASH
    for ev in evs:
        prev = events.compute_chain_hash(
            prev, events.canonical_json(events.to_dict(ev)))
    return prev


# The nine fields, in the order the PDF's `EventRecord` declares them (§8.1,
# §4.2.2). Order is part of the contract: a positional constructor call in
# the watcher must mean the same thing in every build.
_RECORD_FIELDS = ("event_id", "session_id", "seg_line", "timestamp",
                  "event_type", "payload", "tags", "prev_hash", "chain_hash")

# The physical row's keys -- `Event`'s fields, which `to_dict` emits and every
# checkpoint on disk was folded over. Unchanged by this phase (AMBIG 3).
_ROW_KEYS = {"ts", "session_id", "type", "payload", "tags"}


def _record(**override) -> "events.EventRecord":
    base = dict(event_id="s:1", session_id="s", seg_line=1,
                timestamp="2026-06-20T10:00:00Z", event_type="decision",
                payload={"text": "x"}, tags=[],
                prev_hash=events.GENESIS_PREV_HASH, chain_hash="a" * 64)
    base.update(override)
    return events.EventRecord(**base)


def _annotates(field_type, builtin) -> bool:
    """True when a dataclass field's annotation names *builtin* -- as the class
    itself, as its name under postponed evaluation, or as a `typing` alias
    whose origin is it (`Dict[str, Any]` is `dict`; the PDF's spelling and the
    repo's builtin-generic style are equivalent here)."""
    if field_type is builtin:
        return True
    if isinstance(field_type, str):
        head = field_type.split("[", 1)[0]
        return head in (builtin.__name__, builtin.__name__.capitalize())
    return typing.get_origin(field_type) is builtin


# --- the genesis constant -----------------------------------------------------

def test_genesis_prev_hash_is_sixty_four_zeros():
    assert events.GENESIS_PREV_HASH == "0" * 64
    assert len(events.GENESIS_PREV_HASH) == 64


def test_genesis_prev_hash_equals_the_checkpoints_zero():
    """`chain.py` is read-only in this phase, so `_ZERO` is not rewritten to
    import the new constant. Equality is the property the watcher depends on:
    a record built from the first row of a segment must carry the same
    `prev_hash` the checkpoint fold started from."""
    assert events.GENESIS_PREV_HASH == chain._ZERO


# --- compute_chain_hash: the formula ------------------------------------------

def test_compute_chain_hash_is_sha256_over_prev_plus_canonical_utf8():
    expected = hashlib.sha256(("0" * 64 + "{}").encode("utf-8")).hexdigest()
    assert events.compute_chain_hash("0" * 64, "{}") == expected


def test_compute_chain_hash_returns_a_lowercase_hex_digest():
    digest = events.compute_chain_hash(events.GENESIS_PREV_HASH, "{}")
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)                     # every character is a hex digit


def test_a_different_prev_hash_gives_a_different_digest():
    same_row = '{"payload":{"text":"x"}}'
    assert (events.compute_chain_hash("0" * 64, same_row)
            != events.compute_chain_hash("1" * 64, same_row))


def test_a_different_canonical_row_gives_a_different_digest():
    prev = events.GENESIS_PREV_HASH
    assert (events.compute_chain_hash(prev, '{"a":1}')
            != events.compute_chain_hash(prev, '{"a":2}'))


def test_the_helper_takes_the_str_canonical_json_returns_and_hashes_its_utf8():
    """The PDF's helper took `bytes` and encoded the prefix as ASCII; the
    plan keeps `chain.py`'s formula, which concatenates two `str`s and encodes
    the result as UTF-8. A non-ASCII row is where the two would diverge, so
    the fixture carries U+2028 -- raw, because `canonical_json` writes with
    `ensure_ascii=False` -- and the expected digest is computed by hand over
    the UTF-8 bytes of that exact str."""
    canonical = events.canonical_json({"text": "mid dle"})
    assert " " in canonical
    expected = hashlib.sha256(
        (events.GENESIS_PREV_HASH + canonical).encode("utf-8")).hexdigest()
    assert events.compute_chain_hash(events.GENESIS_PREV_HASH, canonical) == expected
    # Canary: the ASCII-escaped spelling of the same object is a different
    # str, so it must hash differently -- otherwise the test above would pass
    # against a helper that re-serialised its input.
    escaped = json.dumps({"text": "mid dle"}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True)
    assert escaped != canonical
    assert (events.compute_chain_hash(events.GENESIS_PREV_HASH, escaped)
            != events.compute_chain_hash(events.GENESIS_PREV_HASH, canonical))


# --- parity with chain.py -----------------------------------------------------

def test_three_real_events_folded_through_the_helper_match_compute_chain():
    """THE parity test. One session, strictly increasing ts, so `read_all`'s
    `(ts, segment, line)` order and the append order coincide and there is
    exactly one head for both sides to agree on."""
    _add("one", ts="2026-06-20T10:00:00Z")
    _add("two", ts="2026-06-20T10:00:01Z", etype="caveat")
    _add("three", ts="2026-06-20T10:00:02Z")
    evs = events.read_all()
    assert len(evs) == 3, "fixture did not land three rows -- it proves nothing"
    assert _fold_with_helper(evs) == chain.compute_chain(evs)["head"]


def test_three_real_events_folded_through_the_helper_match_the_checkpoint_fold():
    """The other coordinate system: the checkpoint folds `_scan`'s rows, which
    already carry the canonical JSON per row. The helper fold over the same
    events must reproduce `_fold`'s head and the head `write_chain` stores."""
    _add("one", ts="2026-06-20T10:00:00Z")
    _add("two", ts="2026-06-20T10:00:01Z")
    _add("three", ts="2026-06-20T10:00:02Z")
    rows, _lines = chain._scan()
    assert len(rows) == 3
    head = _fold_with_helper(events.read_all())
    assert head == chain._fold(rows)
    assert chain.write_chain()["head"] == head
    assert chain.verify()["ok"] is True


def test_the_helper_reproduces_fold_over_the_scan_rows_across_segments():
    """Fold `_scan`'s own rows -- the pre-canonicalised third element -- through
    the helper in `_scan`'s own order. Two segments here, so this is the fold
    the checkpoint actually runs on a multi-session namespace, and it holds
    whatever order `read_all` would have chosen."""
    _add("one", session_id="beta")
    _add("two", session_id="alpha")
    _add("three", session_id="beta")
    rows, _lines = chain._scan()
    assert len(rows) == 3
    prev = events.GENESIS_PREV_HASH
    for _segment, _line, canonical in rows:
        prev = events.compute_chain_hash(prev, canonical)
    assert prev == chain._fold(rows)


def test_the_parity_fixture_is_order_sensitive():
    """Canary for the two tests above. A fold that ignored its input -- or a
    helper that hashed only the row and not the prefix -- could satisfy a
    parity assertion by accident. Reversing the events must move the head."""
    _add("one", ts="2026-06-20T10:00:00Z")
    _add("two", ts="2026-06-20T10:00:01Z")
    _add("three", ts="2026-06-20T10:00:02Z")
    evs = events.read_all()
    assert _fold_with_helper(evs) != _fold_with_helper(list(reversed(evs)))


def test_an_empty_ledger_folds_to_the_genesis_constant():
    """`chain.compute_chain([])` reports `_ZERO` as the head of nothing; the
    helper fold must agree, or the first record of a fresh namespace would
    start from a different prefix than the checkpoint did."""
    assert _fold_with_helper([]) == chain.compute_chain([])["head"]
    assert _fold_with_helper([]) == events.GENESIS_PREV_HASH


# --- EventRecord: a frozen runtime wrapper ------------------------------------

def test_event_record_is_a_frozen_dataclass():
    assert dataclasses.is_dataclass(events.EventRecord)
    assert events.EventRecord.__dataclass_params__.frozen is True


def test_event_record_declares_the_nine_fields_in_order():
    names = tuple(f.name for f in dataclasses.fields(events.EventRecord))
    assert names == _RECORD_FIELDS


def test_event_record_field_types_follow_the_schema():
    """§4.2.2's types. `payload` and `tags` accept either the repo's builtin
    generics or the PDF's `typing` spellings -- same runtime class."""
    types = {f.name: f.type for f in dataclasses.fields(events.EventRecord)}
    for name in ("event_id", "session_id", "timestamp", "event_type",
                 "prev_hash", "chain_hash"):
        assert _annotates(types[name], str), (name, types[name])
    assert _annotates(types["seg_line"], int), types["seg_line"]
    assert _annotates(types["payload"], dict), types["payload"]
    assert _annotates(types["tags"], list), types["tags"]


def test_assigning_an_event_record_field_raises_frozen_instance_error():
    rec = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.chain_hash = "b" * 64
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.seg_line = 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.payload = {}
    assert rec.chain_hash == "a" * 64            # nothing moved
    assert rec.seg_line == 1


def test_deleting_an_event_record_field_is_refused_too():
    rec = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        del rec.prev_hash


def test_an_event_record_keeps_what_it_was_built_with():
    rec = _record(event_id="s:3", seg_line=3, prev_hash="c" * 64)
    assert rec.event_id == "s:3"
    assert rec.seg_line == 3
    assert rec.prev_hash == "c" * 64
    assert rec.session_id == "s"
    assert rec.event_type == "decision"
    assert rec.payload == {"text": "x"}
    assert rec.tags == []


def test_event_record_is_not_the_class_append_event_returns():
    """AMBIG 3 from the writer's side: `append_event` keeps returning `Event`.
    Hundreds of callers read `.ts` / `.type` off that object; the PDF's
    `-> EventRecord` return is not adopted."""
    ev = events.append_event("decision", {"text": "x"}, session_id="s1")
    assert isinstance(ev, events.Event)
    assert not isinstance(ev, events.EventRecord)
    assert events.EventRecord is not events.Event


# --- the physical row is unchanged --------------------------------------------

def test_the_physical_row_carries_exactly_the_to_dict_keys():
    """Read the segment back after one append and compare key sets. The row
    must be `to_dict(ev)` and nothing more -- no `prev_hash`, no
    `chain_hash`, no `seg_line`, no `event_id`. Those live on `EventRecord`,
    which readers build and nothing writes."""
    ev = events.append_event("decision", {"text": "x"}, session_id="s1")
    seg = paths.events_dir() / "s1.jsonl"
    text = seg.read_text(encoding="utf-8")
    row = json.loads(text.strip())
    assert set(row) == set(events.to_dict(ev)) == _ROW_KEYS
    for absent in ("prev_hash", "chain_hash", "seg_line", "event_id"):
        assert absent not in row


def test_the_physical_row_is_the_canonical_json_of_to_dict_plus_newline():
    """Byte-level statement of the same fact: the segment holds exactly what
    the chain folds. `read_text`'s universal newlines turn a Windows CRLF back
    into `\\n`, so this holds on both platforms."""
    ev = events.append_event("decision", {"text": "x"}, session_id="s1")
    seg = paths.events_dir() / "s1.jsonl"
    assert seg.read_text(encoding="utf-8") == \
        events.canonical_json(events.to_dict(ev)) + "\n"


def test_every_row_of_a_multi_append_segment_keeps_the_same_keys():
    _add("one")
    _add("two", ts="2026-06-20T10:00:01Z", etype="caveat")
    _add("three", ts="2026-06-20T10:00:02Z")
    rows = [json.loads(line) for _i, line in events._segment_lines(
        paths.events_dir() / "s.jsonl")]
    assert len(rows) == 3
    for row in rows:
        assert set(row) == _ROW_KEYS
        assert "prev_hash" not in row and "chain_hash" not in row


def test_the_event_dataclass_still_has_exactly_the_row_fields():
    """`to_dict` is `asdict(Event)`, so `Event`'s fields ARE the row schema.
    Adding a field here would silently add a key to every new row and move
    every checkpoint head."""
    assert set(events.Event.__dataclass_fields__) == _ROW_KEYS


# --- canonical_json stability -------------------------------------------------

def test_canonical_json_returns_a_str():
    """The PDF's `-> bytes` is not adopted: `chain.py`, `pgmirror` and every
    writer consume a str, and `compute_chain_hash` takes that str."""
    out = events.canonical_json({})
    assert isinstance(out, str)
    assert out == "{}"


def test_canonical_json_writes_u2028_raw_and_is_byte_stable():
    obj = {"z": 1, "a": "mid dle", "m": {"y": 2, "x": 1}}
    out = events.canonical_json(obj)
    assert out == '{"a":"mid dle","m":{"x":1,"y":2},"z":1}'
    assert " " in out                      # raw, not escaped
    assert "\\u2028" not in out
    assert events.canonical_json(obj) == out    # a second call, same bytes
    assert events.canonical_json(obj).encode("utf-8") == out.encode("utf-8")


def test_canonical_json_is_independent_of_insertion_order():
    """Every permutation of four keys -- nested once -- serialises to one str,
    so one row has one digest whatever order its writer built the dict in."""
    items = [("z", 1), ("a", "mid dle"), ("m", {"y": 2, "x": 1}), ("k", [3, 1])]
    reference = events.canonical_json(dict(items))
    for perm in itertools.permutations(items):
        shuffled = dict(perm)
        shuffled["m"] = dict(reversed(list(shuffled["m"].items())))
        assert events.canonical_json(shuffled) == reference
    # And therefore one digest, which is what the whole file is about.
    expected = events.compute_chain_hash(events.GENESIS_PREV_HASH, reference)
    for perm in itertools.permutations(items):
        assert events.compute_chain_hash(
            events.GENESIS_PREV_HASH, events.canonical_json(dict(perm))) == expected


def test_canonical_json_still_refuses_nan():
    """`allow_nan=False` stays: a bare `NaN` token is not JSON, and the chain
    would make it permanent."""
    with pytest.raises(ValueError):
        events.canonical_json({"v": float("nan")})
    with pytest.raises(ValueError):
        events.canonical_json({"v": float("inf")})
