# tests/test_events_sec.py
"""MEB SEC-01 (`events.py`): `WINDOWS_RESERVED_NAMES` and the FOUR refusals of
`validate_session_id`, each with its message verbatim from the blueprint:

    Format session_id '<value>' tidak valid: tipe data harus string.
    Format session_id '<value>' tidak valid: karakter tidak diizinkan.
    Format session_id '<value>' tidak valid: nama perangkat khusus Windows ditolak.
    Format session_id '<value>' tidak valid: inkonsistensi nama segmen fisik.

`tests/test_tier1_invariants.py` pins WHAT is refused (the biconditional with
the sink at `nt`, purity, the `re.search` contract). This file pins WHICH
message says so, in which order the checks run, and that the accept set did
not move when the omnibus message was split into four.
"""
import argparse

import pytest

from mindhive import cliparse, events, paths

MSG_TYPE = "tipe data harus string."
MSG_CHARS = "karakter tidak diizinkan."
MSG_DEVICE = "nama perangkat khusus Windows ditolak."
MSG_SEGMENT = "inkonsistensi nama segmen fisik."
MESSAGES = (MSG_TYPE, MSG_CHARS, MSG_DEVICE, MSG_SEGMENT)


def _quoted(text: str) -> str:
    """The MEB rendering, written here independently of `events._quote`."""
    return "'" + text.encode("unicode_escape").decode("ascii") + "'"


def _refusal(value) -> str:
    with pytest.raises(ValueError) as exc:
        events.validate_session_id(value)
    return str(exc.value)


# --- the reserved set ----------------------------------------------------------

def test_windows_reserved_names_is_the_meb_set_verbatim():
    expected = frozenset({"CON", "PRN", "AUX", "NUL"}
                         | {f"COM{i}" for i in range(1, 10)}
                         | {f"LPT{i}" for i in range(1, 10)})
    assert events.WINDOWS_RESERVED_NAMES == expected
    assert isinstance(events.WINDOWS_RESERVED_NAMES, frozenset)
    assert len(events.WINDOWS_RESERVED_NAMES) == 22
    assert all(name == name.upper() for name in events.WINDOWS_RESERVED_NAMES)


def test_the_reserved_set_is_a_proper_subset_of_the_sinks_device_list():
    """The sink's list (`paths.WINDOWS_DEVICE_NAMES`) is lower-case and also
    holds `com0`/`lpt0`; the MEB set is what the THIRD check names, and the
    sink is asked afterwards for the rest -- so nothing the sink refuses is
    accepted, it is only reported under the fourth message."""
    lowered = {name.lower() for name in events.WINDOWS_RESERVED_NAMES}
    assert lowered < paths.WINDOWS_DEVICE_NAMES
    assert paths.WINDOWS_DEVICE_NAMES - lowered == {"com0", "lpt0"}


# --- the four messages, in order -----------------------------------------------

@pytest.mark.parametrize("value", [None, 123, 4.5, b"s1", ["s1"], object()],
                         ids=["None", "int", "float", "bytes", "list", "object"])
def test_1_a_non_string_is_refused_as_a_type_error(value):
    msg = _refusal(value)
    assert msg.startswith("Format session_id '")
    assert msg.endswith("' tidak valid: " + MSG_TYPE)
    assert chr(10) not in msg


@pytest.mark.parametrize("sid", [
    "../x", "", "a/b", "C:x", ".hidden", "-lead", "x" * 129,
    "a" + chr(10) + "b", "s1" + chr(10), "a*b", "a" + chr(0) + "b",
    "CON" + chr(10),          # the whitelist is asked BEFORE the device stem
    "unsafe-" + chr(10),      # ... and before the sink
], ids=repr)
def test_2_a_whitelist_failure_is_refused_as_characters(sid):
    assert _refusal(sid) == f"Format session_id {_quoted(sid)} tidak valid: {MSG_CHARS}"


@pytest.mark.parametrize("sid", [
    "CON", "con", "Con", "PRN", "AUX", "NUL", "nul", "nul.jsonl", "aux.txt",
    "com1", "COM9", "lpt1", "Lpt9", "con.x.y",   # the stem is the text before the first dot
], ids=repr)
def test_3_a_windows_device_stem_is_refused_by_name(sid):
    assert _refusal(sid) == f"Format session_id {_quoted(sid)} tidak valid: {MSG_DEVICE}"


@pytest.mark.parametrize("sid", [
    "unsafe-abc", "UNSAFE-abc", "Unsafe-abc",
    events.QUARANTINE_PREFIX + "0" * 16,        # the chosen-merge id
    "com0", "lpt0", "COM0",                     # the sink's list, not the MEB set
], ids=repr)
def test_4_what_only_the_sink_refuses_is_a_segment_inconsistency(sid):
    assert events.SESSION_ID_REGEX.fullmatch(sid), sid   # past the whitelist ...
    assert sid.split(".", 1)[0].upper() not in events.WINDOWS_RESERVED_NAMES
    assert _refusal(sid) == f"Format session_id {_quoted(sid)} tidak valid: {MSG_SEGMENT}"


def test_every_refusal_carries_exactly_one_of_the_four_messages():
    vectors = [None, 7, "../x", "", "CON", "com1", "unsafe-x", "com0", "UNSAFE-x"]
    for value in vectors:
        msg = _refusal(value)
        assert sum(msg.endswith(" tidak valid: " + m) for m in MESSAGES) == 1, msg
        assert "wajib alfanumerik" not in msg       # the old omnibus text is gone


# --- the rendering -----------------------------------------------------------------

def test_the_value_is_rendered_in_meb_single_quotes():
    assert _refusal("../x").startswith("Format session_id '../x' tidak valid: ")
    assert _refusal("CON").startswith("Format session_id 'CON' tidak valid: ")
    assert _refusal(123).startswith("Format session_id '123' tidak valid: ")


@pytest.mark.parametrize("sid", ["a" + chr(10) + "b", chr(10), "s1" + chr(13) + chr(10),
                                 "a" + chr(0) + "b", "caf" + chr(233)], ids=repr)
def test_a_control_or_non_ascii_character_is_escaped_never_raw(sid):
    msg = _refusal(sid)
    assert chr(10) not in msg and chr(13) not in msg and chr(0) not in msg
    assert msg.isascii()
    assert _quoted(sid) in msg


def test_quote_survives_a_value_whose_str_raises():
    class Hostile:
        def __str__(self):
            raise RuntimeError("no")

        def __repr__(self):
            return "<hostile>"
    msg = _refusal(Hostile())
    assert msg == "Format session_id '<hostile>' tidak valid: " + MSG_TYPE


# --- the accept set did not move -------------------------------------------------

@pytest.mark.parametrize("sid", ["s1", "mn9-abc.def_1", "A" * 128,
                                 "session-2026-09-13.run_01", "console", "aux1",
                                 "com10", "lpt10", "conx", "nul_"], ids=repr)
def test_a_valid_id_comes_back_verbatim(sid):
    assert events.validate_session_id(sid) is sid


def test_the_checks_are_pure_and_ordered(monkeypatch):
    """No filesystem, and the order is type -> whitelist -> device -> sink:
    a value failing an earlier check never reaches a later one, which the
    sink-spy proves for the third check."""
    calls = []
    real = events.segment_name

    def spy(session_id, os_name=None):
        calls.append(session_id)
        return real(session_id, os_name)
    monkeypatch.setattr(events, "segment_name", spy)
    _refusal(123)
    _refusal("../x")
    _refusal("CON")
    assert calls == []                       # none of the first three asked the sink
    _refusal("unsafe-abc")
    assert calls == ["unsafe-abc"]           # the fourth did, once


# --- the two strict surfaces say the same thing --------------------------------------

@pytest.mark.parametrize("sid, message", [
    ("../x", MSG_CHARS), ("CON", MSG_DEVICE), ("unsafe-abc", MSG_SEGMENT),
], ids=["chars", "device", "segment"])
def test_strict_append_refuses_with_the_same_message_and_writes_nothing(tmp_path, sid, message):
    with pytest.raises(ValueError) as exc:
        events.append_event("decision", {"text": "x"}, session_id=sid, strict_session=True)
    assert str(exc.value) == f"Format session_id {_quoted(sid)} tidak valid: {message}"
    assert list(tmp_path.rglob("*.jsonl")) == []


@pytest.mark.parametrize("sid, message", [
    ("../x", MSG_CHARS), ("CON", MSG_DEVICE), ("unsafe-abc", MSG_SEGMENT),
], ids=["chars", "device", "segment"])
def test_cliparse_composes_its_prefix_with_the_meb_message(sid, message):
    with pytest.raises(argparse.ArgumentTypeError) as exc:
        cliparse.session_id_arg(sid)
    text = str(exc.value)
    assert text.startswith("invalid session id " + repr(sid) + ": ")
    assert text.endswith(f"Format session_id {_quoted(sid)} tidak valid: {message}")
