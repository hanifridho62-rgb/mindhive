# tests/test_rotation_artifact.py
"""Contract for the ROTATION_PENDING artifact — unit U4.

The artifact is the ENTIRE channel between the Stop hook and the supervisor: a
hook cannot terminate a session, so it leaves a file behind and the supervisor
finds it after the child exits. That makes two properties load-bearing: it must
survive the child's death (a file does), and reading a damaged one must never
take the supervisor down with it.
"""
from mindhive import paths
from mindhive.rotation import artifact


def test_write_then_read_round_trips():
    artifact.write(session_id="sess-1", occupancy=203820,
                   threshold=500_000, model="claude-opus-5", now="2026-08-03T00:00:00Z")
    got = artifact.read()
    assert got["session_id"] == "sess-1"
    assert got["occupancy"] == 203820
    assert got["threshold"] == 500_000
    assert got["model"] == "claude-opus-5"
    assert got["ts"] == "2026-08-03T00:00:00Z"


def test_read_with_no_artifact_is_none():
    assert artifact.read() is None


def test_read_corrupt_artifact_is_none_not_a_raise():
    """A half-written artifact must not crash the supervisor loop — the whole
    point of the file is that it survives an abrupt child exit, which is exactly
    when a partial write happens."""
    artifact.path().parent.mkdir(parents=True, exist_ok=True)
    artifact.path().write_text('{"session_id": "sess-1", "occ', encoding="utf-8")
    assert artifact.read() is None


def test_read_non_object_artifact_is_none():
    artifact.path().parent.mkdir(parents=True, exist_ok=True)
    artifact.path().write_text("[1, 2, 3]", encoding="utf-8")
    assert artifact.read() is None


def test_clear_removes_it():
    artifact.write(session_id="s", occupancy=1, threshold=2, model="m")
    assert artifact.read() is not None
    artifact.clear()
    assert artifact.read() is None


def test_clear_on_missing_artifact_is_a_noop():
    # The supervisor clears before every spawn; a missing file is the normal case.
    artifact.clear()
    artifact.clear()
    assert artifact.read() is None


def test_pending_reports_presence():
    assert artifact.pending() is False
    artifact.write(session_id="s", occupancy=1, threshold=2, model="m")
    assert artifact.pending() is True


def test_write_is_confined_to_the_isolated_store():
    """CANARY. `paths.project_dir()` resolves MINDHIVE_HOME at call time, which is
    what keeps the suite off the live ledger. A module that hand-built a
    '~/.mindhive' path would pass every other test in this file while writing to
    the user's real store — this is the test that catches it."""
    artifact.write(session_id="s", occupancy=1, threshold=2, model="m")
    written = artifact.path().resolve()
    assert written.is_file()
    assert written.is_relative_to(paths.store_home().resolve())


def test_write_creates_missing_parent_directory():
    # The hook may fire before anything else has created the project dir.
    target = artifact.path().parent
    if target.exists():
        for child in target.iterdir():
            if child.is_file():
                child.unlink()
    artifact.write(session_id="s", occupancy=1, threshold=2, model="m")
    assert artifact.read() is not None


def test_write_supplies_a_timestamp_when_none_is_given():
    artifact.write(session_id="s", occupancy=1, threshold=2, model="m")
    got = artifact.read()
    assert isinstance(got["ts"], str) and got["ts"].endswith("Z")
