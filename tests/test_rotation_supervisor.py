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


def test_a_slow_spiral_still_trips_the_absolute_backstop(tmp_path):
    """DISCRIMINATING. Rotations 200s apart against a 180s window never put two
    stamps in the same window, so the time-based guard alone never fires and the
    loop relaunches forever — the exact death spiral the guard exists to stop.
    An implementation with only the sliding window hangs this test rather than
    failing it, which is why the total bound is separate and rate-independent."""
    sp = FakeSpawner(rotate_for=999)
    result = _run(sp, now=FakeClock(step=200), max_rotations=3, window_s=180,
                  max_total_rotations=5)
    assert result.reason == "spin_guard"
    assert result.rotations == 5
    assert "total" in result.detail


def test_the_absolute_backstop_does_not_fire_on_healthy_sessions(tmp_path):
    """CANARY for the test above: the same slow clock, under the bound, runs to
    a normal finish. Proves the backstop is a ceiling, not a hair trigger."""
    sp = FakeSpawner(rotate_for=3)
    result = _run(sp, now=FakeClock(step=200), max_rotations=3, window_s=180,
                  max_total_rotations=40)
    assert result.reason == "user_quit"
    assert result.rotations == 3


# --- the loop's only accumulating state -------------------------------------

def test_prune_window_drops_everything_older_than_the_window():
    assert supervisor.prune_window([10.0, 50.0, 190.0], 200.0, 180.0) == [50.0, 190.0, 200.0]


def test_prune_window_stays_flat_across_a_long_slow_session():
    """The 'memory leak in the while-loop' concern, answered with evidence.

    A session rotating hourly for a simulated year: the list must not grow with
    elapsed time, only with rotation RATE. Unbounded growth here would be a slow
    leak in a process the user leaves running all day."""
    recent = []
    for i in range(1, 8761):                 # one rotation an hour for a year
        recent = supervisor.prune_window(recent, i * 3600.0, 180.0)
    assert len(recent) == 1                  # flat, not 8760


def test_prune_window_is_bounded_even_when_rotations_are_fast():
    """Rapid rotations inside the window accumulate — but the loop trips the
    spin guard at max_rotations, so the list is bounded twice over."""
    recent = []
    for i in range(1, 1001):
        recent = supervisor.prune_window(recent, i * 1.0, 10.0)
    assert len(recent) <= 11                 # window/step + 1, not 1000


# --- the spiral verdict, tested directly -------------------------------------

def _verdict(rotations, recent, *, max_rotations=3, max_total=40, window_s=180.0):
    return supervisor.spin_verdict(rotations, recent, max_rotations=max_rotations,
                                   max_total_rotations=max_total,
                                   window_s=window_s, pending=None)


def test_verdict_is_none_for_a_healthy_loop():
    assert _verdict(2, [10.0, 20.0]) is None


def test_verdict_catches_the_fast_spiral_via_the_window():
    got = _verdict(3, [10.0, 20.0, 30.0])
    assert got and "within" in got


def test_verdict_catches_the_slow_spiral_via_the_total():
    """One rotation in the window — the sliding guard sees nothing wrong — but
    40 rotations overall. This is the case a window-only guard misses entirely."""
    got = _verdict(40, [10.0])
    assert got and "total" in got


def test_verdict_reports_numbers_not_just_that_a_limit_exists():
    got = _verdict(3, [1.0, 2.0, 3.0], max_rotations=3, window_s=180.0)
    assert "3" in got and "180" in got


def test_verdict_includes_the_occupancy_when_a_pending_rotation_is_given():
    got = supervisor.spin_verdict(
        3, [1.0, 2.0, 3.0], max_rotations=3, max_total_rotations=40,
        window_s=180.0, pending={"occupancy": 203820, "threshold": 1000})
    assert "203820" in got and "1000" in got


# --- the successor inherits the goal, not just the history -------------------

def test_active_ask_is_empty_without_a_roadmap():
    assert supervisor.active_ask() == ""


def test_active_ask_returns_the_original_ask_with_progress():
    """A successor reseeded from the briefing alone knows what happened but not
    what it was for, and drifts toward whatever the last events were about."""
    from mindhive import roadmap
    roadmap.record("set", "p1", prompt="Build the rotation layer",
                   title="t", steps=["a", "b", "c"])
    roadmap.record("step", "p1", index=0, status="done")
    got = supervisor.active_ask()
    assert "Build the rotation layer" in got
    assert "1/3" in got


def test_active_ask_is_total_when_the_ledger_is_unreadable(monkeypatch):
    """Losing the ask should cost the successor its ask, never its whole
    context — this runs inside the reseed path of a live rotation."""
    import mindhive.events as events_mod
    monkeypatch.setattr(events_mod, "read_all",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")))
    assert supervisor.active_ask() == ""


# --- the child's environment ------------------------------------------------

def test_child_env_enables_rotation():
    assert supervisor._child_env({})["MINDHIVE_ROTATION"] == "1"


def test_child_env_strips_an_inherited_headless_flag():
    """Every hooks/*.ps1 exits immediately on MINDHIVE_HEADLESS=1, stop.ps1
    included. The flag is inherited, so running the supervisor from inside a
    headless session (a reflection run, an autoresume delivery) would pass it
    down and silently disable rotation in every child — no error, just sessions
    that never rotate."""
    env = supervisor._child_env({"MINDHIVE_HEADLESS": "1", "PATH": "/x"})
    assert "MINDHIVE_HEADLESS" not in env
    assert env["MINDHIVE_ROTATION"] == "1"
    assert env["PATH"] == "/x"          # the rest of the environment survives


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


def test_an_unexpected_spawn_exception_does_not_crash_the_supervisor(tmp_path):
    """The supervisor is the user's session wrapper — if it dies, their whole
    working session goes with it. OSError covers the documented spawn failures,
    but anything a custom or future spawner raises must degrade to a reported
    stop rather than a traceback out of the loop."""
    sp = FakeSpawner(raises=RuntimeError("something nobody anticipated"))
    result = _run(sp)
    assert result.reason == "spawn_failed"
    assert result.exit_code != 0
    assert "something nobody anticipated" in result.detail
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
