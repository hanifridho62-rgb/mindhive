# tests/test_orchestrator_driver.py
"""Process-group spawn and whole-tree kill (blueprint 10.14.4, Bug 16; MEB ORCH-04).

The claim under test is about processes this test does NOT hold a handle to: a
grandchild must stop when its grandparent's cap fires. So the real tests here
spawn a real tree and watch a heartbeat file stop growing; the branch the host
platform cannot run is driven through stand-ins for the module's `sys`, `os`,
`signal` and `subprocess` names -- never by patching the real `sys.platform`,
which every other module in the process reads too."""
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from mindhive import paths, verify
from mindhive.orchestrator import driver, resilience

# A RED run -- the bug these tests pin -- leaves the grandchild alive by
# definition, so it ends ITSELF: long enough to outlive every cap, drain and
# assertion below (the longest window is a 5 s cap plus a 5 s drain), short
# enough that a failing run does not leak a process, and on Windows a busy tmp
# dir, for good. A surviving grandchild is still beating when `_assert_stopped`
# looks, so the bound takes nothing from what the tests can see.
GRANDCHILD_LIFETIME_S = 45

GRANDCHILD = (
    "import sys, time\n"
    "path = sys.argv[1]\n"
    f"end = time.monotonic() + {GRANDCHILD_LIFETIME_S}\n"
    "while time.monotonic() < end:\n"
    "    with open(path, 'a', encoding='utf-8') as fh:\n"
    "        fh.write('x')\n"
    "    time.sleep(0.1)\n")

PARENT = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
    "print('spawned', flush=True)\n"
    "time.sleep(60)\n")

# Round 2, review finding C0. `PARENT` sleeps, so in EVERY case above the direct
# child is still alive when the cap fires and a kill by PID can find it. This one
# exits the moment it has spawned: the grandchild is orphaned, still holds the
# inherited pipe, and the leader whose pid the kill would walk from is gone. That
# is the shape a crashing agent, a fast-exiting wrapper, or a test that leaves a
# daemon actually has.
ORPHAN = (
    "import subprocess, sys\n"
    "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
    "print('spawned', flush=True)\n")


def _tree(tmp_path, parent_src=PARENT):
    """``(argv, heartbeat)``: a parent that spawns a heartbeat-writing grandchild
    and then sleeps -- or, given `ORPHAN`, exits at once and leaves it running --
    as script FILES (no quoting to get wrong on either shell)."""
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(GRANDCHILD, encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(parent_src, encoding="utf-8")
    heartbeat = tmp_path / "hb.txt"
    return [sys.executable, str(parent), str(grandchild), str(heartbeat)], heartbeat


def _wait_for_beats(heartbeat, deadline_s=20.0):
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if heartbeat.exists() and heartbeat.stat().st_size >= 2:
            return
        time.sleep(0.05)
    raise AssertionError("the grandchild never wrote a heartbeat")


def _assert_stopped(heartbeat):
    time.sleep(0.5)
    first = heartbeat.stat().st_size
    time.sleep(0.5)
    assert heartbeat.stat().st_size == first, "the grandchild outlived the tree kill"


# --- the spawn ------------------------------------------------------------------------------

def test_a_clean_run_answers_a_completed_process(tmp_path):
    done = driver.run_agent_process_isolated([sys.executable, "-c", "print(1)"], cwd=tmp_path)
    assert isinstance(done, subprocess.CompletedProcess)
    assert (done.returncode, done.stdout.strip(), done.stderr) == (0, "1", "")
    failed = driver.run_agent_process_isolated(
        [sys.executable, "-c", "import sys; sys.exit(3)"], cwd=str(tmp_path))
    assert failed.returncode == 3


def test_the_child_runs_in_cwd_with_the_input_it_was_given(tmp_path):
    done = driver.run_agent_process_isolated(
        [sys.executable, "-c", "import os, sys; print(os.getcwd()); print(sys.stdin.read())"],
        cwd=tmp_path, input_text="halo")
    where, said = done.stdout.strip().splitlines()
    assert where.lower() == str(tmp_path).lower() and said == "halo"


def test_the_spawn_heads_its_own_process_group(tmp_path, monkeypatch):
    seen = {}
    real = subprocess.Popen

    def spy(argv, **kwargs):
        seen.update(kwargs)
        return real(argv, **kwargs)
    monkeypatch.setattr(driver.subprocess, "Popen", spy)
    driver.run_agent_process_isolated([sys.executable, "-c", "pass"], cwd=tmp_path)
    assert seen["shell"] is False
    if sys.platform == "win32":
        assert seen["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert seen["creationflags"] & paths.NO_WINDOW == paths.NO_WINDOW
        assert "start_new_session" not in seen and "preexec_fn" not in seen
    else:
        assert seen["start_new_session"] is True and "preexec_fn" not in seen
        assert not seen.get("creationflags")


def test_isolation_kwargs_on_each_platform(monkeypatch):
    monkeypatch.setattr(driver, "sys", SimpleNamespace(platform="linux"))
    assert driver.isolation_kwargs() == {"start_new_session": True}
    assert driver.isolation_kwargs(0x08000000) == {"start_new_session": True}
    monkeypatch.setattr(driver, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(driver, "subprocess", SimpleNamespace(CREATE_NEW_PROCESS_GROUP=0x200))
    assert driver.isolation_kwargs() == {"creationflags": paths.NO_WINDOW | 0x200}
    assert driver.isolation_kwargs(0) == {"creationflags": 0x200}


# --- the environment (MEB risk 7) -------------------------------------------------------------

def test_no_bytecode_is_set_unless_the_operator_chose(monkeypatch):
    monkeypatch.delenv(driver.NO_BYTECODE_ENV, raising=False)
    assert driver.NO_BYTECODE_ENV == "PYTHONDONTWRITEBYTECODE"
    assert driver.no_bytecode_env()[driver.NO_BYTECODE_ENV] == "1"
    assert driver.no_bytecode_env({"A": "b"}) == {"A": "b", driver.NO_BYTECODE_ENV: "1"}
    assert driver.no_bytecode_env({driver.NO_BYTECODE_ENV: "0"})[driver.NO_BYTECODE_ENV] == "0"
    monkeypatch.setenv(driver.NO_BYTECODE_ENV, "")
    assert driver.no_bytecode_env()[driver.NO_BYTECODE_ENV] == ""
    given = {"A": "b"}
    driver.no_bytecode_env(given)
    assert given == {"A": "b"}                       # a copy, never the caller's dict


def test_a_python_child_writes_no_bytecode(tmp_path, monkeypatch):
    monkeypatch.delenv(driver.NO_BYTECODE_ENV, raising=False)
    (tmp_path / "mod_under_test.py").write_text("VALUE = 1\n", encoding="utf-8")
    done = driver.run_agent_process_isolated(
        [sys.executable, "-c",
         "import sys, mod_under_test; print(sys.dont_write_bytecode)"], cwd=tmp_path)
    assert done.stdout.strip() == "True", done.stderr
    assert not (tmp_path / "__pycache__").exists()


# --- the kill, branch by branch -----------------------------------------------------------------

class _Proc:
    def __init__(self, pid=4321, alive=True):
        self.pid, self.alive, self.killed = pid, alive, 0

    def poll(self):
        return None if self.alive else 0

    def kill(self):
        self.killed += 1


def test_windows_takes_the_tree_with_taskkill(monkeypatch):
    calls = []
    monkeypatch.setattr(driver, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(driver, "subprocess", SimpleNamespace(
        run=lambda argv, **kw: calls.append((argv, kw)),
        SubprocessError=subprocess.SubprocessError))
    proc = _Proc()
    driver.terminate_process_tree(proc)
    (argv, kwargs), = calls
    assert argv == ["taskkill", "/F", "/T", "/PID", "4321"]
    assert kwargs["capture_output"] is True and kwargs["creationflags"] == paths.NO_WINDOW
    assert kwargs["timeout"] == driver.TASKKILL_TIMEOUT_S
    assert proc.killed == 1                          # and the direct child, whatever taskkill did


def test_a_taskkill_that_cannot_run_still_kills_the_child(monkeypatch):
    def broken(argv, **kw):
        raise FileNotFoundError("taskkill")
    monkeypatch.setattr(driver, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(driver, "subprocess", SimpleNamespace(
        run=broken, SubprocessError=subprocess.SubprocessError))
    proc = _Proc()
    driver.terminate_process_tree(proc)
    assert proc.killed == 1


def _posix(monkeypatch, getpgid):
    killed = []
    monkeypatch.setattr(driver, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(driver, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(driver, "os", SimpleNamespace(
        getpgid=getpgid, killpg=lambda pgid, sig: killed.append((pgid, sig))))
    return killed


def test_posix_signals_the_childs_whole_group(monkeypatch):
    killed = _posix(monkeypatch, lambda pid: 777 if pid else 100)
    proc = _Proc()
    driver.terminate_process_tree(proc)
    assert killed == [(777, 9)] and proc.killed == 1


def test_posix_never_signals_the_callers_own_group(monkeypatch):
    """A child not started through `isolation_kwargs` shares our group: `killpg`
    on it would take this process -- and the test runner -- with it."""
    killed = _posix(monkeypatch, lambda pid: 100)
    proc = _Proc()
    driver.terminate_process_tree(proc)
    assert killed == [] and proc.killed == 1


def test_posix_falls_back_to_the_child_when_the_group_is_gone(monkeypatch):
    def gone(pid):
        raise ProcessLookupError(pid)
    killed = _posix(monkeypatch, gone)
    proc = _Proc()
    driver.terminate_process_tree(proc)
    assert killed == [] and proc.killed == 1


def test_an_exited_process_is_left_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(driver, "subprocess", SimpleNamespace(
        run=lambda *a, **k: calls.append(a), SubprocessError=subprocess.SubprocessError))
    proc = _Proc(alive=False)
    driver.terminate_process_tree(proc)
    assert calls == [] and proc.killed == 0


def test_a_kill_that_raises_is_swallowed(monkeypatch):
    class Stubborn(_Proc):
        def kill(self):
            raise PermissionError("access denied")
    monkeypatch.setattr(driver, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(driver, "subprocess", SimpleNamespace(
        run=lambda *a, **k: None, SubprocessError=subprocess.SubprocessError))
    driver.terminate_process_tree(Stubborn())        # total: nothing escapes


# --- the kill, for real ---------------------------------------------------------------------------

def test_no_grandchild_survives_the_tree_kill(tmp_path):
    argv, heartbeat = _tree(tmp_path)
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **driver.isolation_kwargs())
    try:
        _wait_for_beats(heartbeat)
        driver.terminate_process_tree(proc)
        assert proc.wait(timeout=10) is not None
        _assert_stopped(heartbeat)
        driver.terminate_process_tree(proc)          # idempotent on a dead tree
    finally:
        driver.terminate_process_tree(proc)


def test_the_cap_takes_the_tree_and_still_raises_timeout_expired(tmp_path):
    argv, heartbeat = _tree(tmp_path)
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        driver.run_agent_process_isolated(argv, cwd=tmp_path, timeout_s=3.0)
    elapsed = time.monotonic() - began
    # The grandchild inherited the pipes. Killed with its parent, it cannot hold
    # the drain open: the call returns at the cap, not at the grandchild's end.
    assert elapsed < 3.0 + driver.DRAIN_GRACE_S, elapsed
    assert caught.value.timeout == 3.0 and "spawned" in (caught.value.output or "")
    assert heartbeat.exists()
    _assert_stopped(heartbeat)


def test_any_other_exit_from_the_wait_takes_the_tree_too(tmp_path, monkeypatch):
    """Round 2, review finding C2. The patch is ONE-SHOT and the assertions are
    about the child, not about the spy.

    `driver.subprocess is subprocess`, so replacing `Popen` wholesale also
    replaced the one inside `terminate_process_tree`'s own
    ``subprocess.run(["taskkill", ...])``: that raised a SECOND KeyboardInterrupt,
    which escaped the ``except (OSError, SubprocessError)`` guard, so `proc.kill()`
    and `reap(proc)` were never reached and each run leaked a 30-second child --
    while `len(taken) == 1` went on passing. A line tracer over driver.py named
    the reap and the re-raise as never executed."""
    taken, reaped, spawned = [], [], []
    real_tree, real_reap, real_popen = (driver.terminate_process_tree, driver.reap,
                                        subprocess.Popen)

    def tree(proc):
        taken.append(proc.pid)
        return real_tree(proc)

    def reap(proc, grace_s=driver.DRAIN_GRACE_S):
        reaped.append(proc.pid)
        return real_reap(proc, grace_s)
    monkeypatch.setattr(driver, "terminate_process_tree", tree)
    monkeypatch.setattr(driver, "reap", reap)

    class Interrupted(real_popen):
        def communicate(self, *a, **k):
            raise KeyboardInterrupt

    def one_shot(argv, **kwargs):
        proc = (Interrupted if not spawned else real_popen)(argv, **kwargs)
        spawned.append(proc)
        return proc
    monkeypatch.setattr(driver.subprocess, "Popen", one_shot)
    with pytest.raises(KeyboardInterrupt) as caught:
        driver.run_agent_process_isolated(
            [sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path)
    child = spawned[0]
    assert caught.value.__context__ is None      # the ORIGINAL interrupt, not the kill's
    assert taken == [child.pid] and reaped == taken
    assert child.poll() is not None, "the interrupted run leaked its child"


# --- verify._stream: the cap that used to kill the child only -------------------------------------

def test_the_verify_cap_calls_the_tree_kill_and_answers_timeout_exit(monkeypatch):
    taken = []
    real = driver.terminate_process_tree

    def spy(proc):
        taken.append(proc.pid)
        real(proc)
    monkeypatch.setattr(driver, "terminate_process_tree", spy)
    code, _, error = verify._stream(
        [sys.executable, "-c", "import time; time.sleep(10)"], echo=False, timeout_s=0.2)
    assert code == verify.TIMEOUT_EXIT and "0.2s cap" in error
    assert len(taken) == 1


def test_the_verify_spawn_heads_its_own_group(monkeypatch):
    seen = {}
    real = subprocess.Popen

    def spy(argv, **kwargs):
        seen.update(kwargs)
        return real(argv, **kwargs)
    monkeypatch.setattr(verify.subprocess, "Popen", spy)
    code, out, _ = verify._stream([sys.executable, "-c", "print('ok')"], echo=False)
    assert code == 0 and out.strip() == "ok"
    expected = driver.isolation_kwargs()
    assert {k: seen[k] for k in expected} == expected


def test_a_capped_verify_run_leaves_no_grandchild_holding_the_pipe(tmp_path):
    """The limit `_stream`'s docstring used to record: `Popen.kill` reached the
    child alone, and a grandchild holding the inherited pipe kept the read open
    past the cap."""
    argv, heartbeat = _tree(tmp_path)
    began = time.monotonic()
    code, out, error = verify._stream(argv, echo=False, timeout_s=3.0)
    elapsed = time.monotonic() - began
    assert code == verify.TIMEOUT_EXIT and "spawned" in out
    assert elapsed < 3.0 + driver.DRAIN_GRACE_S, elapsed
    _assert_stopped(heartbeat)


def test_an_interrupted_verify_run_takes_the_tree_before_it_propagates(monkeypatch):
    """The child no longer shares the console's Ctrl+C group on Windows, so an
    interrupt that reaches only this process must end the child itself."""
    taken = []
    real = driver.terminate_process_tree

    def spy(proc):
        taken.append(proc.pid)
        real(proc)
    monkeypatch.setattr(driver, "terminate_process_tree", spy)

    def interrupted(proc, *a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(verify, "_drain", interrupted)
    with pytest.raises(KeyboardInterrupt):
        verify._stream([sys.executable, "-c", "import time; time.sleep(30)"], echo=False)
    assert len(taken) == 1


# --- resilience._run_capped: the tree kill for a child nothing is holding -------------------------

def _capped(monkeypatch, *, job, assigned):
    """Drive `_run_capped` past a 1s cap with the job layer stood in. Answers
    ``(taken, terminated, fence)``: the pids the tree kill was asked for, the
    jobs `_terminate_job` was handed, and the fence record the caller reads."""
    taken, terminated = [], []
    real = driver.terminate_process_tree

    def tree(proc):
        taken.append(proc.pid)
        real(proc)
    monkeypatch.setattr(driver, "terminate_process_tree", tree)
    monkeypatch.setattr(resilience, "_assign",
                        lambda _job, _proc: (assigned, "" if assigned else "refused"))
    monkeypatch.setattr(resilience, "_terminate_job", terminated.append)
    fence = {}
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        resilience._run_capped([sys.executable, "-c", "import time; time.sleep(60)"], job, fence,
                               cwd=None, env=None, timeout=1.0, stdin_text=None,
                               creationflags=paths.NO_WINDOW)
    assert caught.value.timeout == 1.0
    return taken, terminated, fence


def test_an_unfenced_child_past_its_cap_goes_through_the_tree_kill(monkeypatch):
    taken, terminated, fence = _capped(monkeypatch, job=None, assigned=False)
    assert len(taken) == 1 and terminated == [None]
    assert fence == {"drained": True}


def test_a_child_whose_job_assignment_failed_is_not_left_to_the_job(monkeypatch):
    job = object()
    taken, terminated, fence = _capped(monkeypatch, job=job, assigned=False)
    assert len(taken) == 1 and terminated == [job]
    assert fence == {"assigned": False, "why": "refused", "drained": True}


def test_a_child_its_job_is_holding_is_killed_by_the_job_not_walked(monkeypatch):
    """The job already takes the tree, atomically; a `taskkill` walk beside it
    would be a second, slower answer to a question the kernel has answered."""
    job = object()
    taken, terminated, fence = _capped(monkeypatch, job=job, assigned=True)
    assert taken == [] and terminated == [job]
    assert fence == {"assigned": True, "why": "", "drained": True}


@pytest.mark.skipif(sys.platform != "win32",
                    reason="on POSIX an unheld child shares this process's group by design "
                           "(Ctrl+C must reach it), so the driver kills it alone")
def test_an_unfenced_sandbox_run_takes_its_grandchild_on_windows(tmp_path):
    argv, heartbeat = _tree(tmp_path)
    began = time.monotonic()
    got = resilience.run_sandboxed(argv, timeout_s=5.0, cwd=str(tmp_path))
    elapsed = time.monotonic() - began
    assert got.result == "timeout" and "pipes were still held" not in got.reason, got.reason
    assert elapsed < 5.0 + driver.DRAIN_GRACE_S, elapsed
    assert heartbeat.exists()
    _assert_stopped(heartbeat)


# --- the SOP process token (blueprint P1d) never reaches what this runs ---------------------------

def test_the_isolated_runner_withholds_the_sop_process_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_ORCHESTRATION_SECRET", "k")
    monkeypatch.setenv("MINDHIVE_PROCESS_TOKEN", "k")
    probe = ("import os; print(sorted(k.upper() for k in os.environ "
             "if k.upper() in ('MINDHIVE_PROCESS_TOKEN', 'MINDHIVE_ORCHESTRATION_SECRET')))")
    done = driver.run_agent_process_isolated([sys.executable, "-c", probe], cwd=tmp_path)
    assert done.stdout.strip() == "['MINDHIVE_ORCHESTRATION_SECRET']", done.stderr
    explicit = driver.run_agent_process_isolated(
        [sys.executable, "-c", probe], cwd=tmp_path,
        env={**os.environ, "MINDHIVE_PROCESS_TOKEN": "k"})
    assert explicit.stdout.strip() == "['MINDHIVE_ORCHESTRATION_SECRET']", explicit.stderr


# --- round 2: the tree has to outlive its leader (review finding C0) ------------------------------

def test_the_kill_says_what_it_actually_reached(monkeypatch):
    """`verify._stream` writes its answer into a `verification` event stamped
    `verified_by="python"`. A kill that reached nothing must not come back
    wearing the same word as one that took the whole tree."""
    monkeypatch.setattr(driver, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(driver, "subprocess", SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(returncode=0),
        SubprocessError=subprocess.SubprocessError))
    took = driver.terminate_process_tree(_Proc())
    assert (took.how, took.took_the_tree, took.reached) == ("taskkill", True, True)

    monkeypatch.setattr(driver, "subprocess", SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(returncode=128),   # "process not found"
        SubprocessError=subprocess.SubprocessError))
    child_only = driver.terminate_process_tree(_Proc())
    assert child_only.how == "child"
    assert child_only.reached is True and child_only.took_the_tree is False

    nothing = driver.terminate_process_tree(_Proc(alive=False))
    assert (nothing.how, nothing.reached, nothing.took_the_tree) == ("nothing", False, False)
    assert "already exited" in nothing.reason


def test_the_posix_kill_says_whether_it_took_the_group(monkeypatch):
    _posix(monkeypatch, lambda pid: 777 if pid else 100)
    assert driver.terminate_process_tree(_Proc()).how == "group"
    _posix(monkeypatch, lambda pid: 100)
    assert driver.terminate_process_tree(_Proc()).how == "child"


def test_a_held_posix_child_is_signalled_by_pid_even_once_it_is_reaped(monkeypatch):
    """`os.getpgid(pid)` raises ESRCH the moment `poll()` reaps the leader, which
    is why looking the group up could never work here. The group OUTLIVES its
    leader and its id is the leader's pid, so a child we know was started with
    `start_new_session` is signalled by pid directly."""
    def reaped(pid):
        raise ProcessLookupError(pid)
    killed = _posix(monkeypatch, reaped)
    proc = _Proc(alive=False)
    driver.hold_tree(proc)
    try:
        got = driver.terminate_process_tree(proc)
    finally:
        driver.release_tree(proc)
    assert killed == [(4321, 9)] and got.how == "group" and got.took_the_tree


def test_the_isolated_spawn_is_held_for_the_life_of_the_run(tmp_path, monkeypatch):
    """Windows cannot walk a dead pid's descendants -- `taskkill /F /T` on one
    answers rc 128, "process not found" -- so the tree is put in a Job Object at
    spawn, which reaches descendants whether or not the leader is still alive."""
    held, released = [], []
    real_hold, real_release = driver.hold_tree, driver.release_tree

    def hold(proc, **kw):
        held.append(proc)
        return real_hold(proc, **kw)

    def release(proc):
        released.append(proc)
        return real_release(proc)
    monkeypatch.setattr(driver, "hold_tree", hold)
    monkeypatch.setattr(driver, "release_tree", release)
    done = driver.run_agent_process_isolated([sys.executable, "-c", "print(2)"], cwd=tmp_path)
    assert done.stdout.strip() == "2"
    assert len(held) == 1 and released == held


def test_the_tree_is_still_reachable_after_its_leader_has_gone(tmp_path):
    argv, heartbeat = _tree(tmp_path, ORPHAN)
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **driver.isolation_kwargs())
    try:
        driver.hold_tree(proc)
        _wait_for_beats(heartbeat)
        assert proc.wait(timeout=20) is not None       # the leader is already gone
        got = driver.terminate_process_tree(proc)
        assert got.took_the_tree, got.reason
        _assert_stopped(heartbeat)
    finally:
        driver.release_tree(proc)


def test_an_orphans_grandchild_does_not_defeat_the_isolated_cap(tmp_path):
    """Measured RED at 8.69s against a 2.0s cap, with the run still recording
    "killed at the 2.0s cap" -- a kill that provably never happened."""
    argv, heartbeat = _tree(tmp_path, ORPHAN)
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        driver.run_agent_process_isolated(argv, cwd=tmp_path, timeout_s=3.0)
    elapsed = time.monotonic() - began
    assert elapsed < 3.0 + driver.DRAIN_GRACE_S, elapsed
    assert caught.value.timeout == 3.0
    assert heartbeat.exists()
    _assert_stopped(heartbeat)


def test_an_orphans_grandchild_does_not_defeat_the_verify_cap(tmp_path):
    argv, heartbeat = _tree(tmp_path, ORPHAN)
    began = time.monotonic()
    code, out, error = verify._stream(argv, echo=False, timeout_s=3.0)
    elapsed = time.monotonic() - began
    assert code == verify.TIMEOUT_EXIT and "spawned" in out
    assert elapsed < 3.0 + driver.DRAIN_GRACE_S, elapsed
    assert "cap" in error
    _assert_stopped(heartbeat)


# --- round 2: reap is bounded (review finding C1) -------------------------------------------------

class _Blocking:
    """A pipe whose `close` sits on the buffered-reader lock a live reader thread
    owns -- which is what `communicate`'s daemon reader is doing for exactly as
    long as an unkilled holder keeps the write end open."""

    def __init__(self, hold_s=10.0):
        self.hold_s, self.closed = hold_s, False

    def close(self):
        time.sleep(self.hold_s)
        self.closed = True


def test_reap_is_bounded_even_when_a_pipe_close_blocks():
    """`reap`'s docstring promises "a bounded wait"; it bounded `proc.wait` and
    then blocked in `pipe.close()` for as long as the holder lived -- measured at
    5.13s for a grace of 0.0, and at 40.92s against a 2.0s cap. A pipe a reader
    still owns is left to that reader (CPython's `_readerthread` closes it when
    its read returns): a leaked handle is a bounded cost, a pinned lane thread
    is not."""
    class _Piped(_Proc):
        def __init__(self):
            super().__init__()
            self.stdin, self.stderr = None, None
            self.stdout = _Blocking()

        def wait(self, timeout=None):
            return None

    proc = _Piped()
    began = time.monotonic()
    driver.reap(proc, grace_s=0.0)
    elapsed = time.monotonic() - began
    assert elapsed < driver.CLOSE_GRACE_S + 2.0, elapsed
    assert proc.stdout.closed is False                 # still held, deliberately


def test_a_cap_that_could_not_take_the_tree_does_not_claim_it_did(monkeypatch):
    """The honesty half of C0: gate.py records this string in a `verification`
    event under `verified_by="python"`, so it must not assert an action nothing
    performed."""
    real = driver.terminate_process_tree

    def hollow(proc):
        real(proc)                                     # really end it, so the read ends
        return driver.TreeKill("nothing", "the direct child had already exited")
    monkeypatch.setattr(driver, "terminate_process_tree", hollow)
    code, _, error = verify._stream(
        [sys.executable, "-c", "import time; time.sleep(20)"], echo=False, timeout_s=0.3)
    assert code == verify.TIMEOUT_EXIT
    assert "killed" not in error, error
    assert "0.3s cap" in error and "already exited" in error


def test_the_verify_drain_cannot_outlive_the_cap_by_more_than_the_grace(tmp_path, monkeypatch):
    """A holder that escaped the job and the group would otherwise pin the caller
    forever -- `hive selftest` runs with `timeout_s=None`, so nothing else bounds
    it. The read is abandoned instead, and the answer says the output is short."""
    argv, _ = _tree(tmp_path, ORPHAN)
    monkeypatch.setattr(driver, "terminate_process_tree",
                        lambda proc: driver.TreeKill("nothing", "nothing was reached"))
    began = time.monotonic()
    code, _, error = verify._stream(argv, echo=False, timeout_s=2.0)
    elapsed = time.monotonic() - began
    assert code == verify.TIMEOUT_EXIT
    assert elapsed < 2.0 + driver.DRAIN_GRACE_S + 2.0, elapsed
    assert "abandoned" in error, error
