# tests/test_pg_numeric_parity.py
"""Does a payload NUMBER survive the mirror's JSONB round trip byte-for-byte?

WHY THIS FILE EXISTS. `tests/test_pg_live.py` already asserts the claim the whole
mirror rests on -- the file ledger's hash-chain head equals the mirror's -- over
the real 37k-event ledger. On the NUMERIC dimension that gate is vacuous, and the
ledger says so when counted: 10,841 numbers across 38,924 payload rows, and ZERO
of them in any shape Postgres would render or store differently (no exponent-form
float, no trailing `.0`, no integer past 2**53). A gate whose input cannot
exercise a failure mode cannot report it, and reads green either way.

This is the same vacuity the ordering tie-break already has an answer for.
`docs/DATA_MODEL.md` records that one as "proven on a constructed fixture,
because the real ledger cannot exercise the tie-break". Same problem, same
remedy -- a constructed fixture, here for numbers.

WHAT THE ROUND TRIP IS, and why it is not obviously lossless. The chain hashes
`canonical_json(to_dict(ev))`, so the bytes that decide parity are the ones
Python re-emits AFTER the value has been through Postgres. `payload` is `JSONB`,
which stores every number as `numeric`: exact decimal, arbitrary precision,
scale preserved -- but with no memory of EXPONENT notation. `1e-07` is stored as
one ten-millionth and printed back as `0.0000001`. The chain survives that only
because parsing the printed form lands on the SAME float64, so `json.dumps`
re-emits `1e-07` again. Key sorting, the property `docs/DATA_MODEL.md` names,
does not cover this at all: that is about key ORDER, and this is about value
SPELLING.

THREE KINDS OF HAZARD, kept apart because they are proven by different means:

* SPELLING_COLLAPSES -- text in differs from text out. The model below shows it.
* PRECISION_HAZARDS -- text in equals text out, so no rendering bug touches
  them; what breaks them is a store that narrows the value. Proven here against
  the `real[]` (float32) design this repo considered and rejected.
* SERVER_ONLY -- neither. Nothing a Python model can say about them is evidence;
  they are carried for the `pg` tier, and named so their presence is never
  mistaken for coverage.

WHY THIS IS A SIMULATION, AND WHAT THAT COSTS. `numeric_out` runs in the server,
and the default tier has no server, so the round trip below is modelled in
Python from `numeric`'s documented semantics. The MODEL is what is green here.
`tests/test_pg_live.py::test_adversarial_number_shapes_survive_the_jsonb_round_trip`
asks the same questions of a real PostgreSQL and is the only place the model is
CONFIRMED -- said plainly so a green run here is never mistaken for one there.
"""
import json
import math
import struct
from decimal import Decimal

from mindhive import chain, events


# --------------------------------------------------------------------------
# the server's rendering, modelled
# --------------------------------------------------------------------------

def numeric_out(literal: str) -> str:
    """Render a JSON number literal the way Postgres' `numeric_out` would.

    Three properties of `numeric` drive every assertion in this file:

    * it is EXACT decimal and KEEPS its scale, so `100.0` stays `100.0` and no
      precision is lost in storage;
    * it is PLAIN -- `numeric_out` emits no exponent notation, which is why
      `1e-07` returns as `0.0000001` and `1e+30` as thirty-one digits;
    * it has NO SIGNED ZERO. `-0.0` and `0.0` are one value. Python's `Decimal`
      keeps that sign, so it is dropped here on purpose: this function models
      the server, not Python, and inheriting Python's signed zero would hide
      the one shape that does not survive.
    """
    value = Decimal(literal)
    if value == 0:
        value = abs(value)
    return format(value, "f")


def through_jsonb(payload):
    """`payload` as it comes back out of a `JSONB` column.

    The text `COPY` sends is `canonical_json`'s. The text coming back is what
    `numeric_out` printed. psycopg hands the caller whatever `json.loads` makes
    of that. Applying the rendering through `parse_float`/`parse_int` rather
    than by walking the structure means nesting is covered for free -- a number
    six levels down in a dict inside a list is rendered the same way, which is
    the case a hand-written walker forgets.
    """
    sent = events.canonical_json(payload)
    return json.loads(sent,
                      parse_float=lambda s: float(numeric_out(s)),
                      parse_int=lambda s: int(numeric_out(s)))


def through_float32(payload):
    """The same round trip, through the `real[]` design this repo rejected.

    Not a strawman: `docs/DATA_MODEL.md` records `real[]` being measured against
    `BYTEA` for the embedding vectors, so a 32-bit float store is a design that
    was genuinely on the table. It is used here only as the WRONG
    implementation the precision fixtures must be able to kill -- an equality
    test proves nothing until something makes it unequal.
    """
    sent = events.canonical_json(payload)
    narrow = lambda s: struct.unpack("<f", struct.pack("<f", float(s)))[0]
    return json.loads(sent, parse_float=narrow, parse_int=narrow)


def one_event(payload):
    return events.Event(ts="2026-01-01T00:00:00Z", session_id="numparity",
                        type="decision", payload=payload, tags=[])


def head_of(payloads) -> str:
    return chain.compute_chain([one_event(p) for p in payloads])["head"]


# --------------------------------------------------------------------------
# the fixture, grouped by what proves each shape belongs
# --------------------------------------------------------------------------

# Exponent notation has no representation in `numeric`, so the text changes.
SPELLING_COLLAPSES = {
    "large_exponent": 1e+30,                   # '1e+30' -> 31 plain digits
    "small_exponent": 1e-07,                   # '1e-07' -> '0.0000001'
    "nested": {"deep": [{"inner": 2.5e-08}]},  # rendering must reach any depth
}

# Text-identical through `numeric`, and destroyed by any narrowing store.
PRECISION_HAZARDS = {
    "seventeen_digits": 0.30000000000000004,   # shortest-repr float64
    "big_int": 12345678901234567890,           # past float64's exact range
}

# `numeric` keeps scale, so this is text-identical AND survives float32 (100.0
# is exactly representable). No Python model can discriminate it; it rides along
# for the `pg` tier, where only a real `numeric` can answer whether the scale
# came back. Listed separately so it is never counted as proof of anything here.
SERVER_ONLY = {"trailing_zero": 100.0}

ADVERSARIAL = {**SPELLING_COLLAPSES, **PRECISION_HAZARDS, **SERVER_ONLY}

# What the live ledger actually holds, measured: integers and short decimals.
# Here to prove the EXISTING gate cannot fail, not to test the mirror.
ORDINARY = {"count": 62, "ratio": 0.5, "weight": 1.25, "delta": -3}


# --------------------------------------------------------------------------
# the fixture is armed
# --------------------------------------------------------------------------

def test_every_spelling_collapse_really_collapses():
    """Each of these is spelled differently coming back than going in.

    Without this the group could quietly decay into ordinary numbers, which are
    byte-identical through the round trip and therefore agree with a completely
    broken rendering."""
    for name, value in SPELLING_COLLAPSES.items():
        if isinstance(value, dict):
            continue
        sent = json.dumps(value)
        assert sent != numeric_out(sent), (
            f"{name} is rendered identically ({sent!r}) and so proves nothing")


def test_every_precision_hazard_is_text_identical_yet_kills_a_narrow_store():
    """Why this group is in the fixture at all.

    `numeric` renders them exactly as sent, so no rendering assertion can fail
    on them. What they discriminate is the STORE: routed through 32-bit floats
    both change value, so they are the shapes that would have caught `real[]`."""
    for name, value in PRECISION_HAZARDS.items():
        sent = json.dumps(value)
        assert sent == numeric_out(sent), f"{name} belongs in the other group"
        assert head_of([{name: value}]) != head_of([through_float32({name: value})]), (
            f"{name} survives float32 and so discriminates nothing")


def test_the_numbers_the_real_ledger_holds_are_rendered_identically():
    """WHY the live 37k-event parity run is silent on this dimension.

    For every number shape the ledger actually contains, the text Postgres
    returns equals the text it was sent, so no round trip could change anything
    and the gate could not have failed. This is the vacuity, exercised rather
    than asserted about."""
    for name, value in ORDINARY.items():
        sent = json.dumps(value)
        assert sent == numeric_out(sent), f"{name} would discriminate after all"


def test_one_ulp_of_difference_changes_the_head():
    """THE CANARY for the head comparison itself. Every claim below is an
    equality, and an equality proves nothing unless the thing it compares can
    also come out unequal. One unit in the last place is the smallest numeric
    change that exists, and the head must move."""
    nudged = dict(ADVERSARIAL,
                  seventeen_digits=math.nextafter(0.30000000000000004,
                                                  math.inf))
    assert head_of([ADVERSARIAL]) != head_of([nudged])


# --------------------------------------------------------------------------
# the claim
# --------------------------------------------------------------------------

def test_every_adversarial_shape_reaches_the_same_chain_head():
    """`numeric`'s collapse of exponent spellings is invisible to the chain,
    because the printed form parses back to the same float64."""
    assert head_of([ADVERSARIAL]) == head_of([through_jsonb(ADVERSARIAL)])


def test_each_shape_survives_on_its_own_so_a_failure_names_itself():
    """One assertion per shape. A single combined payload would report "the head
    changed" and leave the reader to bisect which number did it."""
    for name, value in ADVERSARIAL.items():
        payload = {name: value}
        assert head_of([payload]) == head_of([through_jsonb(payload)]), name


def test_a_big_int_comes_back_an_int_and_not_a_float():
    """`12345678901234567890` is past 2**53. Routed through `float` it would
    return `1.2345678901234567e+19` -- a different value AND a different type,
    and `canonical_json` would write it differently."""
    out = through_jsonb({"big_int": PRECISION_HAZARDS["big_int"]})["big_int"]
    assert isinstance(out, int) and out == PRECISION_HAZARDS["big_int"]


def test_negative_zero_is_the_one_shape_that_does_not_survive():
    """The boundary, pinned rather than left to be discovered in production.

    `numeric` has no signed zero, so `-0.0` returns as `0.0`, `canonical_json`
    writes `0.0` instead of `-0.0`, and the chain head moves. The mirror is NOT
    unconditionally faithful, and this is the exact and only exception found.

    It is currently unreachable: the measured ledger holds no float ending in
    `.0` at all, signed or not. And it is not silent if it ever arrives --
    `hive pg parity` compares these very heads, so the shape that breaks the
    round trip is the shape the gate reports. That is the whole reason parity is
    a hash comparison and not a row count.
    """
    payload = {"signed_zero": -0.0}
    assert json.dumps(payload["signed_zero"]) == "-0.0"
    returned = through_jsonb(payload)
    assert json.dumps(returned["signed_zero"]) == "0.0"
    assert head_of([payload]) != head_of([returned])
