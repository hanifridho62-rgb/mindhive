# tests/test_core_locks.py
"""Blueprint P1j, MEB CONC-01: `mindhive.core.locks`, the cross-platform
`O_CREAT|O_EXCL` lock primitive with a randomised, jittered exponential
backoff in [10, 50] ms, stale-holder reclaim (> 30 s), the Windows
DELETE-PENDING answer (ACCESS_DENIED) treated as bounded contention, and a
fail-open mode for the hook path.

The clocks, the sleeper and the random source are seams, so every timing
claim below is checked deterministically; the two real-thread tests are the
only ones that touch a wall clock, and they assert outcomes, not durations.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mindhive import paths
from mindhive.core import locks

REPO = Path(__file__).resolve().parents[1]


def _lock_path(name="probe.lock"):
    d = paths.project_dir("test")
    d.mkdir(parents=True, exist_ok=True)
    return d / name


class _Clock:
    """A fake monotonic clock that advances only when something sleeps."""

    def __init__(self):
        self.t = 100.0
        self.slept = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


# --- the backoff ------------------------------------------------------------------

def test_backoff_stays_inside_ten_to_fifty_milliseconds_for_every_attempt():
    for attempt in range(21):
        lo = locks.backoff_delay(attempt, rand=lambda: 0.0)
        hi = locks.backoff_delay(attempt, rand=lambda: 1.0)
        assert 0.010 <= lo <= hi <= 0.050, (attempt, lo, hi)
        assert lo == 0.010                       # the floor never drops


def test_the_ceiling_doubles_then_saturates():
    ceilings = [locks.backoff_delay(a, rand=lambda: 1.0) for a in range(5)]
    assert ceilings == pytest.approx([0.010, 0.020, 0.040, 0.050, 0.050])
    with pytest.raises(ValueError):
        locks.backoff_delay(-1)


def test_the_policy_defaults_are_the_blueprints_numbers_and_it_validates():
    p = locks.DEFAULT_POLICY
    assert (p.timeout_s, p.base_s, p.max_s, p.stale_s, p.denied_spins, p.fail_open) == \
        (2.0, 0.010, 0.050, 30.0, 5, False)
    for bad in ({"timeout_s": -1}, {"base_s": 0}, {"base_s": 0.06}, {"stale_s": 0},
                {"denied_spins": -1}, {"denied_spins": True}, {"timeout_s": float("nan")}):
        with pytest.raises(ValueError):
            locks.LockPolicy(**bad)


# --- take, hold, release ------------------------------------------------------------

def test_acquire_takes_a_free_lock_writes_its_pid_and_release_removes_it(tmp_path):
    path = _lock_path()
    clock = _Clock()
    handle = locks.acquire(path, sleep=clock.sleep, monotonic=clock.monotonic)
    assert isinstance(handle, locks.LockHandle) and handle.path == path
    assert path.exists()
    pid, mono = path.read_text(encoding="ascii").split(" ")
    assert int(pid) == os.getpid() and float(mono) == 100.0
    assert clock.slept == []                     # no wait for a free lock
    handle.release()
    assert not path.exists()
    handle.release()                             # a second release is harmless


def test_file_lock_yields_true_while_held_and_cleans_up_on_an_exception(tmp_path):
    path = _lock_path()
    with pytest.raises(RuntimeError):
        with locks.file_lock(path) as held:
            assert held is True and path.exists()
            raise RuntimeError("inside")
    assert not path.exists()


def test_a_waiter_gets_the_lock_when_the_holder_thread_releases_it(tmp_path):
    path = _lock_path()
    holder = locks.acquire(path)
    order = []

    def release_later():
        time.sleep(0.1)
        order.append("released")
        holder.release()
    threading.Thread(target=release_later).start()
    with locks.file_lock(path, locks.LockPolicy(timeout_s=5.0)) as held:
        order.append("entered")
        assert held is True
    assert order == ["released", "entered"]


def test_twelve_threads_of_read_modify_write_lose_nothing_under_the_lock(tmp_path):
    path = _lock_path("counter.lock")
    counter = _lock_path("counter.txt")
    counter.write_text("0", encoding="ascii")
    policy = locks.LockPolicy(timeout_s=30.0)

    def worker():
        for _ in range(25):
            with locks.file_lock(path, policy) as held:
                assert held
                n = int(counter.read_text(encoding="ascii"))
                time.sleep(0.0005)              # widen the window a lost update needs
                counter.write_text(str(n + 1), encoding="ascii")
    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counter.read_text(encoding="ascii") == "300"


# --- contention: timeout, fail-open, stale ---------------------------------------------

def test_a_held_lock_times_out_with_the_blueprints_message(tmp_path):
    path = _lock_path()
    holder = locks.acquire(path)
    clock = _Clock()
    policy = locks.LockPolicy(timeout_s=0.05)
    with pytest.raises(locks.LockTimeoutError) as exc:
        locks.acquire(path, policy, name="probe_lock",
                      sleep=clock.sleep, monotonic=clock.monotonic, rand=lambda: 0.5)
    spins = len(clock.slept) + 1
    assert str(exc.value) == f"Gagal memperoleh probe_lock setelah 0.05s (spins={spins})."
    assert spins >= 2
    assert all(0.010 <= s <= 0.050 for s in clock.slept)
    assert issubclass(locks.LockTimeoutError, TimeoutError)
    holder.release()


def test_the_message_names_the_file_when_no_name_is_given(tmp_path):
    path = _lock_path("board.lock")
    holder = locks.acquire(path)
    clock = _Clock()
    with pytest.raises(locks.LockTimeoutError, match=r"^Gagal memperoleh board\.lock setelah 0s \(spins=1\)\.$"):
        locks.acquire(path, locks.LockPolicy(timeout_s=0), sleep=clock.sleep,
                      monotonic=clock.monotonic)
    holder.release()


def test_fail_open_returns_none_and_file_lock_runs_the_block_unlocked(tmp_path):
    path = _lock_path()
    holder = locks.acquire(path)
    clock = _Clock()
    policy = locks.LockPolicy(timeout_s=0.03, fail_open=True)
    assert locks.acquire(path, policy, sleep=clock.sleep, monotonic=clock.monotonic) is None
    ran = []
    with locks.file_lock(path, policy, sleep=clock.sleep, monotonic=clock.monotonic) as held:
        ran.append(held)
    assert ran == [False]
    assert path.exists()                         # the holder's file was not touched
    holder.release()


def test_a_stale_holder_is_reclaimed_without_waiting_and_a_fresh_one_is_not(tmp_path):
    path = _lock_path()
    path.write_text("dead 1.0", encoding="ascii")
    old = time.time() - 31
    os.utime(path, (old, old))
    assert locks.is_stale(path, 30.0) is True
    assert locks.is_stale(path, 1000.0) is False
    assert locks.is_stale(path.with_name("absent.lock"), 30.0) is False
    clock = _Clock()
    handle = locks.acquire(path, sleep=clock.sleep, monotonic=clock.monotonic)
    assert handle is not None and clock.slept == []
    assert path.read_text(encoding="ascii").startswith(f"{os.getpid()} ")
    handle.release()
    # the same file, not stale under a long threshold: a real wait, then timeout
    path.write_text("dead 1.0", encoding="ascii")
    os.utime(path, (old, old))
    clock = _Clock()
    with pytest.raises(locks.LockTimeoutError):
        locks.acquire(path, locks.LockPolicy(timeout_s=0.05, stale_s=1000.0),
                      sleep=clock.sleep, monotonic=clock.monotonic)
    assert clock.slept


# --- ACCESS_DENIED: the DELETE-PENDING arm --------------------------------------------

def test_three_access_denied_answers_are_waited_out_then_the_lock_is_held(tmp_path, monkeypatch):
    path = _lock_path()
    real_open = os.open
    calls = []

    def denied_thrice(p, flags, *a, **kw):
        if str(p) == str(path) and len(calls) < 3:
            calls.append(p)
            raise PermissionError(13, "Permission denied")
        return real_open(p, flags, *a, **kw)
    monkeypatch.setattr(os, "open", denied_thrice)
    clock = _Clock()
    handle = locks.acquire(path, sleep=clock.sleep, monotonic=clock.monotonic)
    assert handle is not None and len(calls) == 3
    assert len(clock.slept) == 3                 # one wait per refusal, then in
    handle.release()


def test_access_denied_past_the_budget_gives_up_by_the_policys_rule(tmp_path, monkeypatch):
    path = _lock_path()
    attempts = []

    def always_denied(p, flags, *a, **kw):
        attempts.append(p)
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(os, "open", always_denied)
    clock = _Clock()
    policy = locks.LockPolicy(timeout_s=60.0, denied_spins=2)
    with pytest.raises(locks.LockTimeoutError, match=r"\(spins=3\)\.$"):
        locks.acquire(path, policy, sleep=clock.sleep, monotonic=clock.monotonic)
    assert len(attempts) == policy.denied_spins + 1
    assert clock.t - 100.0 < 60.0                # the deadline was NOT what ended it
    attempts.clear()
    assert locks.acquire(path, locks.LockPolicy(timeout_s=60.0, denied_spins=2, fail_open=True),
                         sleep=clock.sleep, monotonic=clock.monotonic) is None
    assert len(attempts) == 3


def test_any_other_os_error_is_a_real_fault_after_one_attempt(tmp_path, monkeypatch):
    path = _lock_path()
    attempts = []

    def read_only_fs(p, flags, *a, **kw):
        attempts.append(p)
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(os, "open", read_only_fs)
    clock = _Clock()
    with pytest.raises(OSError) as exc:
        locks.acquire(path, sleep=clock.sleep, monotonic=clock.monotonic)
    assert exc.value.errno == 30 and not isinstance(exc.value, locks.LockTimeoutError)
    assert len(attempts) == 1 and clock.slept == []
    attempts.clear()
    assert locks.acquire(path, locks.LockPolicy(fail_open=True),
                         sleep=clock.sleep, monotonic=clock.monotonic) is None
    assert len(attempts) == 1


# --- the leaf ------------------------------------------------------------------------

def test_core_locks_imports_cold_without_the_ledger():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-W", "error", "-c",
         "import sys, mindhive.core.locks; "
         "print(sorted(m for m in sys.modules if m.startswith('mindhive')))"],
        capture_output=True, text=True, env=env, cwd=str(REPO))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "['mindhive', 'mindhive.core', 'mindhive.core.locks']"
