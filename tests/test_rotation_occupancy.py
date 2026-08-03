# tests/test_rotation_occupancy.py
"""Contract for reading true context occupancy out of a Claude Code transcript.

Encodes unit U3 of
docs/plans/2026-08-03-002-feat-smart-zone-interactive-rotation-plan.md.

Totality is the headline contract: this runs inside a Stop hook, so every
malformed-input shape must produce a typed unknown, never an exception. Tests
assert on the stable `reason` rule id, never on message prose.
"""
import json

from mindhive.rotation import occupancy


# --- fixture builders: valid by default, one thing corrupted per test -------

def _assistant(*, input_tokens=2, cache_creation=4826, cache_read=198992,
               output_tokens=661, model="claude-opus-5", sidechain=False,
               ts="2026-08-03T00:00:00Z") -> dict:
    """A transcript row in the real shape — top-level `type`/`isSidechain`,
    usage nested under `message`."""
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "sessionId": "sess-1",
        "timestamp": ts,
        "uuid": "u-1",
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output_tokens,
            },
        },
    }


def _user(text="hello") -> dict:
    return {"type": "user", "isSidechain": False,
            "message": {"role": "user", "content": text}}


def _write(tmp_path, rows, name="transcript.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r
                           for r in rows) + "\n", encoding="utf-8")
    return p


# --- happy path ------------------------------------------------------------

def test_reads_occupancy_from_latest_assistant_turn(tmp_path):
    p = _write(tmp_path, [_user(), _assistant()])
    got = occupancy.read_occupancy(p)
    assert got.tokens == 203820          # 2 + 4826 + 198992
    assert got.model == "claude-opus-5"
    assert got.reason == "ok"
    assert got.known is True


def test_newest_assistant_turn_wins(tmp_path):
    """Not the first row, and not the largest — the most recent one. An
    implementation that scans forward and stops, or that takes max(), fails."""
    p = _write(tmp_path, [
        _assistant(input_tokens=1, cache_creation=0, cache_read=900_000,
                   ts="2026-08-03T00:00:00Z"),
        _user(),
        _assistant(input_tokens=1, cache_creation=9, cache_read=90,
                   ts="2026-08-03T01:00:00Z"),
    ])
    got = occupancy.read_occupancy(p)
    assert got.tokens == 100             # 1 + 9 + 90, the LAST row
    assert got.reason == "ok"


def test_non_assistant_rows_are_skipped(tmp_path):
    p = _write(tmp_path, [_assistant(), _user(), _user()])
    assert occupancy.read_occupancy(p).tokens == 203820


# --- the sidechain rule ----------------------------------------------------

def test_sidechain_turns_are_excluded(tmp_path):
    """A subagent's usage is the SUBAGENT's context, not this session's.

    Discriminating: the sidechain row here is both newer and far larger, so an
    implementation that ignores `isSidechain` returns 900_001 and rotates the
    parent session because a child filled up."""
    p = _write(tmp_path, [
        _assistant(input_tokens=1, cache_creation=9, cache_read=90,
                   ts="2026-08-03T00:00:00Z"),
        _assistant(input_tokens=1, cache_creation=0, cache_read=900_000,
                   sidechain=True, ts="2026-08-03T01:00:00Z"),
    ])
    got = occupancy.read_occupancy(p)
    assert got.tokens == 100
    assert got.reason == "ok"


def test_all_sidechain_transcript_is_unknown_not_zero(tmp_path):
    """Zero would read as 'empty context' and suppress rotation forever;
    unknown is the honest answer."""
    p = _write(tmp_path, [_assistant(sidechain=True)])
    got = occupancy.read_occupancy(p)
    assert got.tokens is None
    assert got.reason == "no_usage"
    assert got.known is False


# --- totality: every failure shape is a reason, never a raise --------------

def test_missing_file_is_unknown_not_a_raise(tmp_path):
    got = occupancy.read_occupancy(tmp_path / "nope.jsonl")
    assert got.tokens is None
    assert got.reason == "missing_file"


def test_none_path_is_unknown_not_a_raise():
    assert occupancy.read_occupancy(None).reason == "missing_file"


def test_directory_where_a_file_belongs_is_unknown(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    got = occupancy.read_occupancy(d)
    assert got.tokens is None
    assert got.reason in {"missing_file", "unreadable"}


def test_corrupt_lines_are_skipped_not_fatal(tmp_path):
    """A transcript caught mid-write has a truncated tail. The valid row before
    it must still be found."""
    p = _write(tmp_path, [_assistant(), "{not valid json", '{"type":"assist'])
    got = occupancy.read_occupancy(p)
    assert got.tokens == 203820
    assert got.reason == "ok"


def test_only_corrupt_lines_is_no_usage(tmp_path):
    p = _write(tmp_path, ["{{{", "]]]", ""])
    got = occupancy.read_occupancy(p)
    assert got.tokens is None
    assert got.reason == "no_usage"


def test_empty_file_is_no_usage(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert occupancy.read_occupancy(p).reason == "no_usage"


def test_assistant_row_without_usage_is_skipped(tmp_path):
    p = _write(tmp_path, [
        _assistant(),
        {"type": "assistant", "isSidechain": False,
         "message": {"role": "assistant", "model": "claude-opus-5"}},
    ])
    # Falls back to the older row that DOES carry usage.
    assert occupancy.read_occupancy(p).tokens == 203820


def test_usage_with_missing_fields_defaults_to_zero(tmp_path):
    p = _write(tmp_path, [{
        "type": "assistant", "isSidechain": False,
        "message": {"model": "claude-opus-5", "usage": {"input_tokens": 7}},
    }])
    got = occupancy.read_occupancy(p)
    assert got.tokens == 7
    assert got.reason == "ok"


def test_usage_with_non_numeric_values_is_not_fatal(tmp_path):
    p = _write(tmp_path, [{
        "type": "assistant", "isSidechain": False,
        "message": {"model": "claude-opus-5",
                    "usage": {"input_tokens": "lots", "cache_read_input_tokens": 5}},
    }])
    got = occupancy.read_occupancy(p)
    assert got.tokens == 5          # the unusable field contributes 0, not a crash
    assert got.reason == "ok"


def test_row_that_is_not_a_dict_is_skipped(tmp_path):
    p = _write(tmp_path, ['"just a string"', "[1,2,3]", json.dumps(_assistant())])
    assert occupancy.read_occupancy(p).tokens == 203820


def test_invalid_utf8_does_not_raise(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_bytes(b'\xff\xfe not utf8\n' + json.dumps(_assistant()).encode() + b"\n")
    got = occupancy.read_occupancy(p)
    assert got.tokens == 203820
    assert got.reason == "ok"


def test_model_absent_still_reports_tokens(tmp_path):
    p = _write(tmp_path, [{
        "type": "assistant", "isSidechain": False,
        "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 10}},
    }])
    got = occupancy.read_occupancy(p)
    assert got.tokens == 20
    assert got.model is None        # window_for(None) handles this downstream
    assert got.reason == "ok"
