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


def test_stream_retention_is_bounded_but_the_pipe_is_still_drained(monkeypatch):
    """A runaway process must not grow this one until it dies.

    Asserted on `_stream` rather than through `run_and_record`, because every
    outer surface caps independently — `runlog` truncates at write time and
    `excerpt` at EXCERPT_CHARS — so an unbounded in-memory buffer would look
    identical from outside while still being the thing that OOMs. This is the
    only place the property is observable.

    Retention is bounded to the cap plus at most one line: a line arrives whole
    and is not split mid-read. The exit code proves the pipe kept draining past
    the bound — stop reading and the child blocks on a full pipe and the run
    hangs, which would be a worse bug than the one being fixed."""
    monkeypatch.setattr(verify.runlog, "MAX_ARTIFACT_BYTES", 200)
    code, out, err = verify._stream(
        _argv("for i in range(200): print('y' * 500)"), False)
    assert code == 0, "the child must run to completion, not block on the pipe"
    assert not err
    assert len(out) < 5_000, "retention is unbounded (would be ~100_000)"


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


# --- the seams the per-unit gate needs ----------------------------------------

def _rows():
    return [e for e in ev_mod.read_all() if e.type == "verification"]


def test_a_run_past_its_cap_is_killed_and_answers_124():
    """`orchestrator/gate.py` runs a child's tests under a cap; a hung suite
    must not hang the mission. 124 is `claude_cli`'s own timeout code, so a
    killed suite and a killed child read alike."""
    import time

    started = time.monotonic()
    rec = verify.run_and_record(_argv("import time; time.sleep(30)"),
                                echo=False, timeout_s=1, record=False)
    assert time.monotonic() - started < 20, "the cap did not fire"
    assert rec["exit_code"] == verify.TIMEOUT_EXIT == 124
    assert "cap" in rec["error"]


def test_cwd_is_where_the_command_runs(tmp_path):
    from pathlib import Path

    rec = verify.run_and_record(_argv("import os; print(os.getcwd())"),
                                echo=False, cwd=tmp_path, record=False)
    assert rec["exit_code"] == 0
    assert Path(rec["excerpt"].strip()).resolve() == tmp_path.resolve()


def test_record_false_runs_for_real_and_appends_nothing():
    """The targeted gate's contract: a real exit code, no `verification`
    event — `replenish` would read one as the whole tree verified."""
    rec = verify.run_and_record(_argv("print('2 passed in 0.1s')"),
                                echo=False, record=False)
    assert rec["exit_code"] == 0 and rec["counts"]["passed"] == 2
    assert _rows() == []


def test_extra_labels_the_payload_and_cannot_forge_its_stamp():
    verify.run_and_record(_argv("print('1 passed in 0.1s')"), echo=False,
                          extra={"scope": "full", "tree": "/x",
                                 "verified_by": "human", "exit_code": 7,
                                 "argv": ["forged"]})
    payload = _rows()[0].payload
    assert payload["scope"] == "full" and payload["tree"] == "/x"
    assert payload["verified_by"] == "python"
    assert payload["exit_code"] == 0
    assert payload["argv"][-1] != "forged"


def test_the_tail_is_the_end_of_the_output_and_the_excerpt_its_head():
    rec = verify.run_and_record(
        _argv("print('x' * 5000); print('the last line')"), echo=False,
        record=False)
    assert rec["excerpt"].startswith("xxx")
    assert "the last line" not in rec["excerpt"]
    assert rec["tail"].endswith("the last line")


def test_the_defaults_leave_the_recorded_shape_unchanged():
    """Every caller before the gate passed none of the new arguments, and
    what they record must not grow a key by accident."""
    rec = verify.run_and_record(_argv("print('4 passed in 0.1s')"), echo=False)
    assert {"exit_code", "counts", "error", "artifact", "excerpt"} <= set(rec)
    payload = _rows()[0].payload
    assert payload["counts"]["passed"] == 4
    assert "scope" not in payload and "tail" not in payload
