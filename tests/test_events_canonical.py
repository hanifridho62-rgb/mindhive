# tests/test_events_canonical.py
"""MEB SEC-02: `canonical_json` aligned with RFC 8785 (JCS) -- `-0.0` becomes
`0.0` at every depth, nesting is capped at `MAX_NESTING_DEPTH`, NaN/Infinity
stay refused -- and, the property everything downstream depends on, the bytes
are IDENTICAL to the previous serialiser for every payload that carries no
`-0.0` and sits within the cap, so no stored chain head moves.

`_normalise` is copy-on-write: the caller's payload is never mutated, an
untouched payload is returned as the same object, and an untouched subtree of
a touched payload is shared rather than copied.
"""
import json
import math
import random

import pytest

from mindhive import chain, events


def _legacy(obj) -> str:
    """The serialiser as it was before SEC-02, spelled out here so the
    byte-identity claim is checked against the OLD code, not the new."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _is_negative_zero(x) -> bool:
    return isinstance(x, float) and x == 0.0 and math.copysign(1.0, x) < 0


# --- -0.0 -> 0.0 ---------------------------------------------------------------------

def test_negative_zero_is_normalised_at_every_depth():
    assert events.canonical_json({"a": -0.0}) == '{"a":0.0}'
    assert events.canonical_json({"a": [0.0, -0.0, [-0.0]]}) == '{"a":[0.0,0.0,[0.0]]}'
    assert events.canonical_json({"a": {"b": {"c": -0.0}}}) == '{"a":{"b":{"c":0.0}}}'
    assert events.canonical_json(-0.0) == "0.0"
    assert events.canonical_json([(-0.0,)]) == "[[0.0]]"
    assert events.canonical_json({"x": -0.0, "y": {"z": [1, -0.0]}}) == '{"x":0.0,"y":{"z":[1,0.0]}}'


def test_ints_bools_none_and_positive_zero_are_untouched():
    payload = {"i": 0, "j": -3, "t": True, "f": False, "z": 0.0, "n": None, "s": "-0.0"}
    assert events.canonical_json(payload) == \
        '{"f":false,"i":0,"j":-3,"n":null,"s":"-0.0","t":true,"z":0.0}'
    assert events.canonical_json(payload) == _legacy(payload)


def test_the_payload_is_never_mutated_and_untouched_objects_come_back_as_is():
    plain = {"a": [1, 2.5, {"b": "x"}], "z": 0.0, "w": -1.5}
    assert events._normalise(plain) is plain

    touched = {"a": [-0.0], "k": {"m": 1}, "t": (1, 2)}
    out = events._normalise(touched)
    assert out is not touched
    assert out["a"] == [0.0] and not _is_negative_zero(out["a"][0])
    assert _is_negative_zero(touched["a"][0])        # the caller's object is intact
    assert out["k"] is touched["k"]                   # untouched subtree shared
    assert out["t"] is touched["t"]
    assert events.canonical_json(touched) == '{"a":[0.0],"k":{"m":1},"t":[1,2]}'


def test_json_dumps_alone_still_spells_negative_zero():
    """The normalisation is `canonical_json`'s, not Python's -- if `json`
    ever started collapsing it the test corpus below would stop proving
    anything, so the premise is pinned."""
    assert json.dumps(-0.0) == "-0.0"
    assert _legacy({"z": -0.0}) == '{"z":-0.0}'


# --- byte identity with the previous serialiser --------------------------------------

def _corpus(seed: int, n: int):
    rng = random.Random(seed)
    words = ["a", "b", "ключ", "ñ", "tags", "payload", "session_id", "x y", "", "0"]

    def scalar():
        k = rng.randrange(7)
        if k == 0:
            return rng.randrange(-10 ** 6, 10 ** 6)
        if k == 1:
            v = rng.uniform(-1e6, 1e6)
            return v if v != 0.0 else 1.0           # never a signed zero here
        if k == 2:
            return rng.choice([1e-07, 1e+30, 0.30000000000000004, 2 ** 60, 100.0, 0.0])
        if k == 3:
            return rng.choice([True, False, None])
        return rng.choice(words) + str(rng.randrange(100))

    def value(depth):
        if depth >= 6 or rng.random() < 0.4:
            return scalar()
        if rng.random() < 0.5:
            return {rng.choice(words) + str(i): value(depth + 1) for i in range(rng.randrange(0, 5))}
        return [value(depth + 1) for _ in range(rng.randrange(0, 5))]

    for _ in range(n):
        yield {"text": rng.choice(words), "n": rng.randrange(1000), "v": value(0)}


def test_byte_identical_to_the_previous_serialiser_over_a_random_corpus():
    seen = 0
    for payload in _corpus(seed=1, n=500):
        assert events.canonical_json(payload) == _legacy(payload), payload
        seen += 1
    assert seen == 500


def test_the_ledgers_own_row_shape_is_byte_identical():
    ev = events.Event(ts="2026-01-01T00:00:00Z", session_id="s1", type="decision",
                      payload={"text": "x", "ratio": 0.5, "count": 62, "delta": -3,
                               "nested": {"deep": [{"inner": 2.5e-08}]}},
                      tags=["a", "b"])
    row = events.to_dict(ev)
    assert events.canonical_json(row) == _legacy(row)
    assert events.compute_chain_hash(events.GENESIS_PREV_HASH, events.canonical_json(row)) \
        == events.compute_chain_hash(events.GENESIS_PREV_HASH, _legacy(row))


# --- the nesting cap ------------------------------------------------------------------

def _nested_lists(n):
    obj = "leaf"
    for _ in range(n):
        obj = [obj]
    return obj


def _nested_dicts(n):
    obj = 1
    for _ in range(n):
        obj = {"k": obj}
    return obj


def test_max_nesting_depth_is_one_hundred_containers():
    assert events.MAX_NESTING_DEPTH == 100
    assert events.canonical_json(_nested_lists(100)) == "[" * 100 + '"leaf"' + "]" * 100
    assert events.canonical_json(_nested_dicts(100)) == '{"k":' * 100 + "1" + "}" * 100
    with pytest.raises(ValueError, match="MAX_NESTING_DEPTH"):
        events.canonical_json(_nested_lists(101))
    with pytest.raises(ValueError, match="MAX_NESTING_DEPTH"):
        events.canonical_json(_nested_dicts(101))
    with pytest.raises(ValueError, match="MAX_NESTING_DEPTH"):
        events.canonical_json({"outer": _nested_lists(100)})    # 101 with the wrapper


def test_width_is_not_depth():
    wide = {"k": list(range(20_000)), "m": [[i] for i in range(5_000)]}
    assert events.canonical_json(wide) == _legacy(wide)
    assert events._normalise(wide) is wide


def test_a_depth_overflow_raises_before_json_dumps_recurses():
    """A `RecursionError` from inside the append lock is what the cap
    prevents; the refusal must be a `ValueError`, not the interpreter's."""
    with pytest.raises(ValueError):
        events.canonical_json(_nested_lists(5_000))


# --- NaN and Infinity stay refused ---------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")],
                         ids=["nan", "inf", "-inf"])
def test_non_finite_floats_are_still_refused(bad):
    with pytest.raises(ValueError):
        events.canonical_json({"v": bad})
    with pytest.raises(ValueError):
        events.canonical_json({"v": [1, {"w": bad}]})


# --- the chain over a real store ---------------------------------------------------

def test_a_stored_chain_verifies_and_a_written_negative_zero_lands_positive(tmp_path):
    for i, z in enumerate([0.5, -1.25, 0.0, -0.0, 1e-07]):
        events.append_event("decision", {"text": f"row {i}", "z": z}, session_id="canon")
    rows = events.read_all()
    assert len(rows) == 5
    stored = [r.payload["z"] for r in rows]
    assert not any(_is_negative_zero(z) for z in stored)
    assert stored[3] == 0.0
    head = chain.write_chain()["head"]
    v = chain.verify()
    assert v["ok"] is True, v
    assert chain.compute_chain(rows)["head"] == head
    # the physical line of the -0.0 row is the +0.0 spelling, byte for byte
    seg = next(p for p in tmp_path.rglob("canon.jsonl"))
    line = seg.read_text(encoding="utf-8").split("\n")[3]
    assert '"z":0.0' in line and "-0.0" not in line
