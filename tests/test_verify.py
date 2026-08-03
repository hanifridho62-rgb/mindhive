# tests/test_verify.py
"""`verify.run_and_record` — the only honest source of a "the suite is green"
claim. Without it, a test-count assertion has nothing to be checked against.
"""
import sys

import pytest

from mindhive import events as ev_mod
from mindhive import verify


def _argv(code: str):
    """A real subprocess, not a mocked boundary. The exit code being real is
    the entire value of this module."""
    return [sys.executable, "-c", code]


# --- summary parsing ----------------------------------------------------------

def test_parses_a_green_pytest_summary():
    got = verify.parse_summary("3580 passed, 1 warning in 262.28s (0:04:22)")
    assert got["passed"] == 3580
    assert got.get("failed", 0) == 0


def test_parses_a_mixed_failure_summary():
    got = verify.parse_summary("4 failed, 7 passed in 0.14s")
    assert got["passed"] == 7
    assert got["failed"] == 4


def test_unparseable_output_yields_absent_counts_not_zeros():
    """CANARY. Zero and "we could not tell" are different facts, and
    collapsing them would let a broken parse read as a suite with no tests
    passing — a fabricated result, which is the thing this module exists to
    prevent."""
    got = verify.parse_summary("the build is fine, trust me")
    assert got == {}
    assert "passed" not in got


def test_parse_summary_is_total_on_junk():
    for junk in ("", None, 42, [], {"a": 1}):
        assert verify.parse_summary(junk) == {}


# --- the run: real subprocess, real exit code ---------------------------------

def test_records_a_passing_run_with_python_authority():
    rec = verify.run_and_record(_argv("print('12 passed in 1.0s')"), echo=False)
    assert rec["exit_code"] == 0
    assert rec["counts"]["passed"] == 12

    evs = ev_mod.read_all()
    rows = [e for e in evs if e.type == "verification"]
    assert len(rows) == 1
    assert rows[0].payload["verified_by"] == "python"
    assert rows[0].payload["exit_code"] == 0


def test_records_a_failing_run_faithfully():
    """No success may be printed over a failing run."""
    rec = verify.run_and_record(
        _argv("import sys; print('1 failed, 2 passed in 0.2s'); sys.exit(1)"),
        echo=False)
    assert rec["exit_code"] == 1
    assert rec["counts"]["failed"] == 1

    row = [e for e in ev_mod.read_all() if e.type == "verification"][0]
    assert row.payload["exit_code"] == 1


def test_unparseable_run_records_the_exit_code_without_counts():
    """`verified_by: python` attaches to the EXIT CODE, which was genuinely
    observed. Counts are a parse, and an absent parse stays absent."""
    rec = verify.run_and_record(_argv("print('no summary here')"), echo=False)
    assert rec["exit_code"] == 0
    assert rec["counts"] == {}

    row = [e for e in ev_mod.read_all() if e.type == "verification"][0]
    assert row.payload["verified_by"] == "python"
    assert row.payload.get("counts", {}) == {}


def test_echo_streams_and_flushes_each_line(capsys, monkeypatch):
    """`echo=True` must actually reach the caller's stdout, flushed.

    Without the explicit flush this is true on a terminal and false everywhere
    else — redirected to a file or a CI log, Python block-buffers at 8KB and a
    four-minute suite prints nothing until it is already finished."""
    flushes = []
    real_flush = sys.stdout.flush
    monkeypatch.setattr(sys.stdout, "flush",
                        lambda: flushes.append(1) or real_flush())
    verify.run_and_record(
        _argv("print('line one'); print('2 passed in 0.1s')"), echo=True)
    assert "line one" in capsys.readouterr().out
    assert flushes, "output was written but never flushed"


def test_echo_false_stays_quiet(capsys):
    verify.run_and_record(_argv("print('3 passed in 0.1s')"), echo=False)
    assert capsys.readouterr().out == ""


def test_a_missing_program_is_recorded_not_raised():
    rec = verify.run_and_record(["definitely-not-a-real-program-xyz"], echo=False)
    assert rec["exit_code"] != 0
    assert rec["error"]


def test_module_never_passes_shell_to_a_subprocess():
    """The repo's standing no-`shell=True` mandate, pinned structurally.

    Parsed rather than grepped: a substring search hits this module's own
    docstring, which NAMES the flag in order to say it is not used. An AST walk
    asks the precise question — does any call site pass a `shell` keyword —
    so the test cannot be satisfied or defeated by prose."""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(verify.__file__).read_text(encoding="utf-8"))
    offenders = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 for kw in node.keywords if kw.arg == "shell"]
    assert offenders == []


def test_verification_is_a_registered_event_type():
    """CANARY. Without the events.py registration, append_event raises
    `unknown event type` and every test above fails for that reason instead."""
    assert "verification" in ev_mod.VALID_TYPES
    assert "verification" not in ev_mod.SEMANTIC_TYPES


# --- the honesty contract on the raw append surface ---------------------------

def test_raw_append_surface_refuses_to_forge_a_mechanical_stamp():
    """Mirrors `mindnet_phase`: the surface that accepts an arbitrary payload
    from a user or an agent must NOT be able to claim a verification nothing
    performed. The writer's own shape is validated by `validate_stored`."""
    with pytest.raises(ValueError):
        verify.validate_payload({"exit_code": 0, "argv": ["pytest"],
                                 "verified_by": "python"})


def test_raw_append_surface_accepts_an_unstamped_payload():
    verify.validate_payload({"exit_code": 0, "argv": ["pytest"]})


def test_validate_stored_requires_the_python_stamp():
    with pytest.raises(ValueError):
        verify.validate_stored({"exit_code": 0, "argv": ["pytest"],
                                "verified_by": "agent"})
    verify.validate_stored({"exit_code": 0, "argv": ["pytest"],
                            "verified_by": "python"})


def test_validate_payload_rejects_malformed_shapes():
    for bad in (None, [], "text", {"argv": ["pytest"]}, {"exit_code": 0},
                {"exit_code": "zero", "argv": ["pytest"]},
                {"exit_code": 0, "argv": []}):
        with pytest.raises(ValueError):
            verify.validate_payload(bad)


# --- reading the record back --------------------------------------------------

def test_last_run_returns_the_most_recent_record():
    verify.run_and_record(_argv("print('5 passed in 0.1s')"), echo=False)
    verify.run_and_record(_argv("print('9 passed in 0.1s')"), echo=False)
    got = verify.last_run(ev_mod.read_all())
    assert got["counts"]["passed"] == 9


def test_last_run_is_none_on_an_empty_store():
    assert verify.last_run(ev_mod.read_all()) is None
    assert verify.last_run(None) is None
