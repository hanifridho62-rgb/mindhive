# tests/test_rotation_supervisor.py
"""Contract for the `hive session run` outer loop — unit U6.

The spawner and the clock are injected, so the loop's decisions are testable
without launching a real CLI or sleeping. What a fake spawner CANNOT prove is
that the interactive TUI renders correctly as a child process; that is an
empirical gate, run by hand and reported as such. These tests cover the control
flow around it, which is where the loop bugs live: stale artifacts, spin
death-spirals, and mistaking a user quitting for a rotation.
"""
from mindhive.rotation import artifact, supervisor


class FakeClock:
    """Monotonic seconds under test control."""

    def __init__(self, step):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


class FakeSpawner:
    """Stands in for the child CLI. `rotate_for` runs of it end by leaving a
    ROTATION_PENDING artifact, exactly as the real Stop hook would."""

    def __init__(self, rotate_for=0, raises=None):
        self.rotate_for = rotate_for
        self.raises = raises
        self.calls = []

    def __call__(self, handoff):
        self.calls.append(handoff)
        if self.raises is not None and len(self.calls) == 1:
            raise self.raises
        if len(self.calls) <= self.rotate_for:
            artifact.write(session_id=f"sess-{len(self.calls)}", occupancy=203820,
                           threshold=1000, model="claude-opus-5")
        return 0


def _run(spawner, **kw):
    kw.setdefault("now", FakeClock(step=10))
    kw.setdefault("reseed", lambda pending: f"handoff after {pending['session_id']}")
    return supervisor.run(spawn=spawner, **kw)


# --- the ordinary path -----------------------------------------------------

def test_no_artifact_means_the_user_quit(tmp_path):
    sp = FakeSpawner(rotate_for=0)
    result = _run(sp)
    assert len(sp.calls) == 1          # spawned once, did not relaunch
    assert result.rotations == 0
    assert result.reason == "user_quit"
    assert result.exit_code == 0


def test_one_rotation_then_a_clean_exit_spawns_twice(tmp_path):
    sp = FakeSpawner(rotate_for=1)
    result = _run(sp)
    assert len(sp.calls) == 2
    assert result.rotations == 1
    assert result.reason == "user_quit"
    assert result.exit_code == 0


def test_the_successor_is_seeded_and_the_first_session_is_not(tmp_path):
    sp = FakeSpawner(rotate_for=1)
    _run(sp)
    assert sp.calls[0] is None                     # nothing to hand off yet
    assert sp.calls[1] == "handoff after sess-1"   # carries the prior session


def test_reseed_runs_once_per_rotation(tmp_path):
    seen = []
    sp = FakeSpawner(rotate_for=2)
    _run(sp, now=FakeClock(step=3600), reseed=lambda p: seen.append(p) or "x")
    assert len(seen) == 2
    assert [p["session_id"] for p in seen] == ["sess-1", "sess-2"]


def test_artifact_is_cleared_after_a_rotation_is_consumed(tmp_path):
    _run(FakeSpawner(rotate_for=1))
    assert artifact.read() is None


# --- stale artifacts -------------------------------------------------------

def test_a_stale_artifact_is_not_this_run_s_rotation(tmp_path):
    """A crash last run can leave ROTATION_PENDING behind. Treating it as a
    rotation would double-spawn on every single start — so the loop clears
    before each spawn, not after."""
    artifact.write(session_id="from-last-week", occupancy=1, threshold=2, model="m")
    sp = FakeSpawner(rotate_for=0)
    result = _run(sp)
    assert len(sp.calls) == 1
    assert result.rotations == 0
    assert result.reason == "user_quit"


# --- the spin guard --------------------------------------------------------

def test_spin_guard_trips_on_rapid_rotations(tmp_path):
    """Every child rotates immediately, 10s apart: three inside the window
    means something is wrong (a mis-set threshold, a stuck transcript) and
    relaunching forever would burn the account."""
    sp = FakeSpawner(rotate_for=99)
    result = _run(sp, now=FakeClock(step=10), max_rotations=3, window_s=180)
    assert result.reason == "spin_guard"
    assert result.exit_code != 0
    assert result.rotations == 3
    assert len(sp.calls) == 3


def test_spin_guard_is_time_windowed_not_a_plain_counter(tmp_path):
    """DISCRIMINATING. Same spawner, same max_rotations — only the clock differs.
    An hour between rotations is a healthy long session, not a spiral. An
    implementation that just counts rotations trips here too and fails."""
    sp = FakeSpawner(rotate_for=5)
    result = _run(sp, now=FakeClock(step=3600), max_rotations=3, window_s=180)
    assert result.reason == "user_quit"
    assert result.rotations == 5
    assert len(sp.calls) == 6          # 5 rotations, then one clean session


def test_spin_guard_reports_its_numbers(tmp_path):
    result = _run(FakeSpawner(rotate_for=99), now=FakeClock(step=10),
                  max_rotations=2, window_s=180)
    assert result.reason == "spin_guard"
    assert "2" in result.detail and "180" in result.detail


# --- interruption ----------------------------------------------------------

def test_ctrl_c_is_a_user_quit_not_a_rotation(tmp_path):
    """With inherited stdio the child is in the parent's console group and gets
    Ctrl+C directly; the supervisor sees KeyboardInterrupt out of wait(). It must
    exit, not relaunch — relaunching on Ctrl+C makes the loop unquittable."""
    sp = FakeSpawner(rotate_for=99, raises=KeyboardInterrupt())
    result = _run(sp)
    assert result.reason == "user_quit"
    assert result.exit_code == 0
    assert len(sp.calls) == 1


def test_a_spawn_failure_stops_rather_than_looping(tmp_path):
    sp = FakeSpawner(raises=FileNotFoundError("claude not on PATH"))
    result = _run(sp)
    assert result.reason == "spawn_failed"
    assert result.exit_code != 0
    assert len(sp.calls) == 1


def test_reseed_failure_does_not_kill_the_supervisor(tmp_path):
    """Consolidation is best-effort. A failed reseed costs the successor its
    context, which is bad; taking the whole loop down is worse."""
    def boom(_pending):
        raise RuntimeError("consolidate blew up")

    sp = FakeSpawner(rotate_for=1)
    result = _run(sp, reseed=boom)
    assert len(sp.calls) == 2
    assert sp.calls[1] is None          # relaunched, just unseeded
    assert result.reason == "user_quit"


# --- CLI wiring ------------------------------------------------------------

def test_cli_session_run_returns_the_supervisor_exit_code(tmp_path, monkeypatch):
    from mindhive import __main__ as cli_mod
    monkeypatch.setattr(supervisor, "run",
                        lambda **_k: supervisor.SessionResult(0, 1, 0, "user_quit", ""))
    assert cli_mod.main(["session", "run"]) == 0

    monkeypatch.setattr(supervisor, "run",
                        lambda **_k: supervisor.SessionResult(3, 3, 3, "spin_guard", "x"))
    assert cli_mod.main(["session", "run"]) == 3
