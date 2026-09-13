# tests/test_orchestrator_fence.py
"""WB-fence — the ceiling a lintester child actually runs under.

## What was broken

`LintesterOrchestrator` has carried a `limits` field and reported an
`enforcement` in every `Mission` since the sandbox landed, and **nothing was
ever fenced**. `execute_mission` asked `resilience.fencing(self.limits)` — which
answers what the PLATFORM can do — and printed `kernel`. The child meanwhile
went `_run_child` → `agent.run` → `claude_cli.run_headless` → a bare
`subprocess.run`: no job object, no rlimit, no ceiling of any kind. A report
saying `kernel` over an unfenced child is precisely the laundering
`resilience`'s own docstring forbids, and it was live.

So the tests here are mostly about HONESTY, not about killing processes. The
kernel mechanisms are already proven in `tests/test_orchestrator_resilience.py`
and `tests/test_jobs.py`; what is new is a fence that must never claim more than
it delivered.

## No test here creates a real job object

`_job_for`, `_run_capped`, `_close_job` and `_preexec_for` are imported into
`fence`'s own globals precisely so they can be patched there — the same reason
`resilience` imports them into its. Driving the real Windows kernel would make
every assertion below platform-conditional, and the POSIX leg would then prove
nothing at all, which is the vacuous-negative failure this suite has been bitten
by before.
"""
import contextlib
import inspect
import subprocess
import sys
import threading

import pytest

from mindhive.orchestrator import fence as fence_mod
from mindhive.orchestrator import resilience as res_mod


# --- fixture builders ---------------------------------------------------------

def _limits(memory_mb=64, cpu_s=5):
    return res_mod.Limits(memory_bytes=memory_mb * 1024 * 1024, cpu_seconds=cpu_s)


def _done(code=0, stdout="out", stderr=""):
    return subprocess.CompletedProcess(["claude"], code, stdout, stderr)


def _strict_capped(argv, job, fence, *, cwd, env, timeout, stdin_text,
                   creationflags, preexec_fn=None):
    """`resilience._run_capped`'s real signature, and nothing wider.

    Every other double in this file takes `**kw`, and that was a hole worth a
    test of its own: a `Fence.spawn` mutated to drop `env=` and `cwd=` and to
    forward `capture_output=`/`text=` kept the whole suite green while being
    four production-fatal `TypeError`s. The real function's keyword-only
    parameters carry NO defaults, so a missing one dies at the call; its
    `**kwargs` goes straight into `Popen`, so an extra one dies there. This
    double dies in both of the same places, at the same seam.

    Dropping `env` is not a cosmetic mis-forward: it is the child losing
    `MINDHIVE_HEADLESS` (the recursion guard) and `MINDHIVE_SESSION` (the
    per-child ledger segment)."""
    fence["assigned"], fence["why"] = True, ""
    return _done()


def _wire(monkeypatch, *, job=("JOB", ""), capped=None, preexec=None):
    """Replace the four primitives `Fence` borrows, and record every call.

    Returns the record dict. *job* is the `(handle, why)` `_job_for` answers;
    *capped* is a callable standing in for `_run_capped` and defaults to a clean
    assignment and a zero exit."""
    seen = {"capped": [], "closed": [], "job_for": 0}

    def _job_for(limits):
        seen["job_for"] += 1
        seen["limits"] = limits
        return job

    def _default_capped(argv, jb, state, **kw):
        state["assigned"], state["why"] = True, ""
        return _done()

    def _capped(argv, jb, state, **kw):
        seen["capped"].append({"argv": argv, "job": jb, "state": state, "kw": kw})
        return (capped or _default_capped)(argv, jb, state, **kw)

    monkeypatch.setattr(fence_mod, "_job_for", _job_for)
    monkeypatch.setattr(fence_mod, "_run_capped", _capped)
    monkeypatch.setattr(fence_mod, "_close_job", lambda jb: seen["closed"].append(jb))
    monkeypatch.setattr(fence_mod, "_preexec_for", lambda limits: preexec)
    return seen


@contextlib.contextmanager
def _switching_hard():
    """Force the interpreter to switch threads as often as it can.

    Scheduling PRESSURE, not proof: measured here, an unlocked `_observe`
    passes the counting tests below at 1e-6 s exactly as it does at the default
    5 ms, because the critical section is a handful of bytecodes. `_Latch` is
    what actually discriminates. This is kept because it costs nothing and a
    lost increment is cheaper to catch by accident than never.
    `sys.setswitchinterval` is process-global, hence the restore."""
    was = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(was)


def _kernel(monkeypatch):
    """Make `resilience.fencing` answer `kernel` regardless of this platform."""
    monkeypatch.setattr(res_mod, "limits_available", lambda: (True, ""))


def _no_mechanism(monkeypatch, why="this is not Windows and `resource` is absent"):
    monkeypatch.setattr(res_mod, "limits_available", lambda: (False, why))


# --- the prediction the fence starts from -------------------------------------

def test_a_fence_asked_for_no_ceiling_reports_the_no_ceiling_reason():
    f = fence_mod.Fence(res_mod.Limits())
    assert f.enforcement == res_mod.TIMEOUT_ONLY
    # The shared constant, not merely a truthy string: the Ubuntu legs caught
    # two call sites disagreeing about this exact text.
    assert f.reason == res_mod.NO_LIMITS_REASON


def test_a_fence_on_a_kernel_that_can_hold_it_says_so_before_any_run(monkeypatch):
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    assert (f.enforcement, f.reason) == (res_mod.KERNEL, "")
    assert f.runs == 0


def test_a_platform_with_no_mechanism_is_timeout_only_from_the_start(monkeypatch):
    _no_mechanism(monkeypatch, "no resource module and no job objects")
    f = fence_mod.Fence(_limits())
    assert f.enforcement == res_mod.TIMEOUT_ONLY
    assert "no resource module" in f.reason


# --- spawn behaves like the call `run_headless` makes --------------------------

def test_spawn_answers_the_completed_process_run_capped_returned(monkeypatch):
    _wire(monkeypatch, capped=lambda argv, jb, st, **kw: _done(3, "hello", "err"))
    _kernel(monkeypatch)
    proc = fence_mod.Fence(_limits()).spawn(["claude", "-p"], input="do x")
    assert isinstance(proc, subprocess.CompletedProcess)
    assert (proc.returncode, proc.stdout, proc.stderr) == (3, "hello", "err")


def test_the_prompt_rides_stdin_and_never_argv(monkeypatch):
    seen = _wire(monkeypatch)
    _kernel(monkeypatch)
    big = "x" * 20000
    fence_mod.Fence(_limits()).spawn(["claude", "-p"], input=big)
    call = seen["capped"][0]
    assert call["kw"]["stdin_text"] == big
    assert all(big not in str(part) for part in call["argv"])


def test_spawn_accepts_exactly_the_call_run_headless_makes(monkeypatch):
    """The discriminating test: a seam the real caller cannot call is no seam.

    These are the keywords `claude_cli.run_headless` passes verbatim, and the
    double is the STRICT one, so this fails on a mis-forward rather than only
    on a missing parameter."""
    _wire(monkeypatch, capped=_strict_capped)
    _kernel(monkeypatch)
    proc = fence_mod.Fence(_limits()).spawn(
        ["claude", "--output-format", "json", "-p"],
        capture_output=True, text=True, timeout=180, env={"PATH": "x"},
        input="prompt", cwd=None,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0


def test_spawn_forwards_every_keyword_run_capped_needs(monkeypatch):
    """What actually reaches the mechanism, asserted value by value.

    Before this, only `stdin_text`, `timeout`, `argv` and `preexec_fn` were
    pinned anywhere. `cwd`, `env` and `creationflags` had no assertion in any
    file, and neither did fence.py's explicit claim that `capture_output` and
    `text` are accepted and NOT forwarded."""
    seen = _wire(monkeypatch, capped=_strict_capped)
    _kernel(monkeypatch)
    fence_mod.Fence(_limits()).spawn(
        ["claude", "-p"], capture_output=True, text=True, timeout=180,
        env={"PATH": "x", "MINDHIVE_SESSION": "lm-1"}, input="prompt",
        cwd="C:/lane-0", creationflags=7)
    kw = seen["capped"][0]["kw"]

    assert kw["env"] == {"PATH": "x", "MINDHIVE_SESSION": "lm-1"}
    assert kw["cwd"] == "C:/lane-0"
    assert kw["timeout"] == 180
    assert kw["stdin_text"] == "prompt"
    assert kw["creationflags"] == 7
    # `_run_capped` already pipes all three streams and decodes them; it has no
    # other mode, and either keyword inside `Popen` is a TypeError.
    assert "capture_output" not in kw and "text" not in kw


def test_a_cwd_reaches_run_capped_as_a_string(monkeypatch):
    """`run_headless` is given a `WorkerLane.worktree_path`, which may be a
    Path. `_run_capped` hands it to `Popen`."""
    import pathlib
    seen = _wire(monkeypatch, capped=_strict_capped)
    _kernel(monkeypatch)
    fence_mod.Fence(_limits()).spawn(["claude"], cwd=pathlib.Path("C:/lane-0"))
    assert isinstance(seen["capped"][0]["kw"]["cwd"], str)


def test_no_creationflags_means_the_no_window_flag_this_project_uses(monkeypatch):
    """`run_headless` always passes one, but the default is what a caller that
    forgot would get, and a console window per child is what `paths.NO_WINDOW`
    exists to prevent."""
    from mindhive import paths

    seen = _wire(monkeypatch, capped=_strict_capped)
    _kernel(monkeypatch)
    fence_mod.Fence(_limits()).spawn(["claude"])
    assert seen["capped"][0]["kw"]["creationflags"] == paths.NO_WINDOW


def test_what_spawn_forwards_binds_to_the_real_run_capped(monkeypatch):
    """The double above is only as good as its likeness to the real function.

    This binds the recorded call against `resilience._run_capped`'s ACTUAL
    signature, so the day that function grows a required keyword — or renames
    one — this fails here instead of on the first real `--memory-mb` mission."""
    seen = _wire(monkeypatch)
    _kernel(monkeypatch)
    fence_mod.Fence(_limits()).spawn(["claude", "-p"], input="p", timeout=9,
                                     cwd="C:/x", env={"PATH": "x"})
    call = seen["capped"][0]
    inspect.signature(res_mod._run_capped).bind(
        call["argv"], call["job"], call["state"], **call["kw"])


def test_the_timeout_reaches_run_capped_as_its_cap(monkeypatch):
    seen = _wire(monkeypatch)
    _kernel(monkeypatch)
    fence_mod.Fence(_limits()).spawn(["claude"], timeout=42)
    assert seen["capped"][0]["kw"]["timeout"] == 42


def test_argv_reaches_run_capped_as_strings(monkeypatch):
    """`_run_capped` hands argv straight to `Popen`, which rejects a Path on
    some platforms and silently stringifies it on others."""
    import pathlib
    seen = _wire(monkeypatch)
    _kernel(monkeypatch)
    fence_mod.Fence(_limits()).spawn([pathlib.Path("claude.CMD"), "-p"])
    assert all(isinstance(part, str) for part in seen["capped"][0]["argv"])


def test_a_timeout_propagates_rather_than_being_swallowed(monkeypatch):
    """`run_headless` maps `TimeoutExpired` to exit 124. A fence that returned a
    result instead would turn every capped run into a silent success."""
    def _boom(argv, jb, state, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    _wire(monkeypatch, capped=_boom)
    _kernel(monkeypatch)
    with pytest.raises(subprocess.TimeoutExpired):
        fence_mod.Fence(_limits()).spawn(["claude"], timeout=1)


# --- the job handle ------------------------------------------------------------

def test_the_job_is_closed_exactly_once_per_run(monkeypatch):
    """`jobs._close_job`'s docstring: the handle is closed exactly once. Closing
    a raw HANDLE twice is worse than leaking it, because the value can be reused
    by another open in between."""
    seen = _wire(monkeypatch)
    _kernel(monkeypatch)
    fence_mod.Fence(_limits()).spawn(["claude"])
    assert seen["closed"] == ["JOB"]


def test_the_job_is_still_closed_when_the_child_times_out(monkeypatch):
    def _boom(argv, jb, state, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    seen = _wire(monkeypatch, capped=_boom)
    _kernel(monkeypatch)
    with pytest.raises(subprocess.TimeoutExpired):
        fence_mod.Fence(_limits()).spawn(["claude"], timeout=1)
    assert seen["closed"] == ["JOB"], "KILL_ON_JOB_CLOSE never fires on a leak"


def test_a_timed_out_child_is_still_counted_by_the_fence(monkeypatch):
    """The close half of the timeout path had a test; the RECORD half — the
    whole reason `_observe` sits in the `finally` — had none."""
    def _boom(argv, jb, state, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    _wire(monkeypatch, capped=_boom)
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    with pytest.raises(subprocess.TimeoutExpired):
        f.spawn(["claude"], timeout=1)
    assert f.runs == 1, "a child that hit its cap is still a child that ran"


def test_a_timed_out_child_whose_job_never_existed_downgrades_the_fence(
        monkeypatch):
    """The timeout path is where the ceiling is actually being exercised, so a
    `kernel` printed over it is the worst version of the laundering fence.py
    exists to end: the operator is told the cap held on the one run where the
    cap was the whole story."""
    def _boom(argv, jb, state, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    _wire(monkeypatch, job=(None, "CreateJobObject failed (WinError 5)"),
          capped=_boom)
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    with pytest.raises(subprocess.TimeoutExpired):
        f.spawn(["claude"], timeout=1)
    assert f.enforcement == res_mod.TIMEOUT_ONLY
    assert "CreateJobObject failed (WinError 5)" in f.reason
    assert f.downgrades == 1


def test_a_timed_out_child_that_never_joined_its_job_downgrades_the_fence(
        monkeypatch):
    """`_run_capped` fills `assigned`/`why` in BEFORE the wait, precisely so the
    assignment outcome survives a timeout. This double does the same, and the
    fence must read it on the way out."""
    def _boom(argv, jb, state, **kw):
        state["assigned"] = False
        state["why"] = "AssignProcessToJobObject failed (WinError 5)"
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    _wire(monkeypatch, capped=_boom)
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    with pytest.raises(subprocess.TimeoutExpired):
        f.spawn(["claude"], timeout=1)
    assert f.enforcement == res_mod.TIMEOUT_ONLY
    assert "AssignProcessToJobObject" in f.reason


def test_a_clean_timeout_leaves_the_kernel_verdict_standing(monkeypatch):
    """The canary for the two above. A child killed at a cap that really held
    it is a ceiling working, not a ceiling failing — a fence that downgraded on
    every `TimeoutExpired` would pass both and be wrong about every capped run.
    """
    def _boom(argv, jb, state, **kw):
        state["assigned"], state["why"] = True, ""
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    _wire(monkeypatch, capped=_boom)
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    with pytest.raises(subprocess.TimeoutExpired):
        f.spawn(["claude"], timeout=1)
    assert (f.enforcement, f.reason) == (res_mod.KERNEL, "")
    assert f.runs == 1 and f.downgrades == 0


def test_a_run_with_no_job_closes_nothing(monkeypatch):
    """POSIX answers `(None, "")` — `_preexec_for` fences there. `_close_job`
    is never called with `None`."""
    seen = _wire(monkeypatch, job=(None, ""))
    _kernel(monkeypatch)
    fence_mod.Fence(_limits()).spawn(["claude"])
    assert seen["closed"] == []


def test_every_run_gets_its_own_job(monkeypatch):
    """One job per child, not one per mission: `KILL_ON_JOB_CLOSE` on a shared
    job would take a sibling lane's child with it."""
    seen = _wire(monkeypatch)
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    f.spawn(["claude"])
    f.spawn(["claude"])
    assert seen["job_for"] == 2
    assert seen["closed"] == ["JOB", "JOB"]
    assert f.runs == 2


# --- the POSIX half, which is where the laundering would have recurred ---------

def test_an_rlimit_platform_gets_its_preexec_fn(monkeypatch):
    """On POSIX `_job_for` answers `(None, "")` — NOT a downgrade — because
    `_preexec_for` is what fences there. A fence that forgot it would report
    `kernel` over a child holding no ceiling at all: the exact defect this
    module exists to remove, reintroduced one layer up."""
    sentinel = object()
    seen = _wire(monkeypatch, job=(None, ""), preexec=sentinel)
    _kernel(monkeypatch)
    fence_mod.Fence(_limits()).spawn(["claude"])
    assert seen["capped"][0]["kw"].get("preexec_fn") is sentinel


def test_no_preexec_fn_is_passed_when_there_is_nothing_to_apply(monkeypatch):
    """The canary for the test above. `_preexec_for`'s docstring: passing a
    `preexec_fn` that does nothing still forces the slow fork path, so the key
    must be ABSENT rather than `None`."""
    seen = _wire(monkeypatch, preexec=None)
    _kernel(monkeypatch)
    fence_mod.Fence(_limits()).spawn(["claude"])
    assert "preexec_fn" not in seen["capped"][0]["kw"]


# --- the prediction may only get worse ----------------------------------------

def test_a_job_that_could_not_be_created_downgrades_the_fence(monkeypatch):
    _wire(monkeypatch, job=(None, "CreateJobObject failed (WinError 5)"))
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    assert f.enforcement == res_mod.KERNEL          # the prediction
    f.spawn(["claude"])
    assert f.enforcement == res_mod.TIMEOUT_ONLY    # the evidence
    assert "CreateJobObject failed (WinError 5)" in f.reason


def test_a_job_that_could_not_be_assigned_downgrades_the_fence(monkeypatch):
    """A created job that the child never joined holds nothing. `_run_capped`
    fills the state dict in before any wait precisely so this is readable."""
    def _unassigned(argv, jb, state, **kw):
        state["assigned"] = False
        state["why"] = "AssignProcessToJobObject failed (WinError 5)"
        return _done()

    _wire(monkeypatch, capped=_unassigned)
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    f.spawn(["claude"])
    assert f.enforcement == res_mod.TIMEOUT_ONLY
    assert "AssignProcessToJobObject" in f.reason


def test_a_clean_run_leaves_the_prediction_standing(monkeypatch):
    _wire(monkeypatch)
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    f.spawn(["claude"])
    assert (f.enforcement, f.reason) == (res_mod.KERNEL, "")
    assert f.downgrades == 0


def test_a_later_clean_run_never_upgrades_a_downgraded_fence(monkeypatch):
    """The mission's verdict is about every child it ran, not the last one. One
    child that escaped its ceiling is enough to make `kernel` a false report."""
    state = {"fail": True}

    def _first_fails(limits):
        if state["fail"]:
            state["fail"] = False
            return None, "CreateJobObject failed (WinError 5)"
        return "JOB", ""

    seen = _wire(monkeypatch)
    monkeypatch.setattr(fence_mod, "_job_for", _first_fails)
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    f.spawn(["claude"])
    f.spawn(["claude"])
    assert f.enforcement == res_mod.TIMEOUT_ONLY
    assert "CreateJobObject failed" in f.reason
    assert f.runs == 2 and seen["closed"] == ["JOB"]


def test_the_first_downgrade_is_the_reason_that_is_kept(monkeypatch):
    """Two failures, two causes. The first is the one that explains the machine;
    the last is just the most recent symptom."""
    reasons = iter(["CreateJobObject failed (WinError 5)",
                    "SetInformationJobObject failed (WinError 87)"])
    monkeypatch.setattr(fence_mod, "_run_capped",
                        lambda argv, jb, st, **kw: _done())
    monkeypatch.setattr(fence_mod, "_close_job", lambda jb: None)
    monkeypatch.setattr(fence_mod, "_preexec_for", lambda limits: None)
    monkeypatch.setattr(fence_mod, "_job_for", lambda limits: (None, next(reasons)))
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    f.spawn(["claude"])
    f.spawn(["claude"])
    assert "CreateJobObject" in f.reason
    assert "SetInformationJobObject" not in f.reason
    assert f.downgrades == 1


def test_a_downgrade_on_a_platform_that_never_promised_the_kernel_keeps_its_reason(
        monkeypatch):
    """`fencing` already said `timeout-only` and why. A per-run failure must not
    overwrite the platform's answer with a symptom of it."""
    _wire(monkeypatch, job=(None, "CreateJobObject failed (WinError 5)"))
    _no_mechanism(monkeypatch, "this is not Windows")
    f = fence_mod.Fence(_limits())
    f.spawn(["claude"])
    assert f.enforcement == res_mod.TIMEOUT_ONLY
    assert f.reason == "this is not Windows"


def test_an_empty_ceiling_is_never_reported_as_a_downgrade(monkeypatch):
    """`_job_for` answers `(None, "")` for empty limits and `fencing` calls that
    "no ceiling was requested" — not a degradation. A fence that counted it as
    one would put a warning in front of every operator who asked for nothing."""
    _wire(monkeypatch, job=(None, ""))
    f = fence_mod.Fence(res_mod.Limits())
    f.spawn(["claude"])
    assert f.downgrades == 0
    assert f.reason == res_mod.NO_LIMITS_REASON


# --- one fence, many lanes ----------------------------------------------------

class _Latch:
    """Falsy — but it records whether two threads reached it at the same time.

    `_observe` tests `if job_why:` INSIDE its critical section, on every run, so
    this object's `__bool__` runs there and nowhere else. Under the lock only
    one lane can be inside, the barrier can never fill, and every wait times out
    into `BrokenBarrierError`; without the lock the lanes arrive together and it
    fills on the first pass.

    This indirection earns its keep. The obvious concurrency test — many threads
    incrementing, assert the total — is NOT discriminating on CPython: the
    switch interval lets each short section run to completion, and the unlocked
    `_observe` passes it every time (proven with a mutant, at 1e-6 s as well as
    at the default). Mutual exclusion is the property actually being claimed, so
    it is the property asserted."""

    def __init__(self, parties, timeout=0.2):
        self._gate = threading.Barrier(parties, timeout=timeout)
        self.overlapped = False

    def __bool__(self):
        try:
            self._gate.wait()
            self.overlapped = True
        except threading.BrokenBarrierError:
            pass
        return False


def test_two_lanes_are_never_inside_the_fences_bookkeeping_at_once(monkeypatch):
    """The verdict and the counters are shared mutable state written from the
    scheduler's worker threads. `self._runs += 1` is LOAD/ADD/STORE and
    `_downgrade` is a check-then-act over three attributes: two lanes inside
    together can lose a run, or both pass the `KERNEL` guard and leave the
    SECOND reason standing — the exact opposite of the first-reason-wins rule
    the class is built on."""
    lanes = 8
    latch = _Latch(lanes)
    _wire(monkeypatch, job=("JOB", latch))
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    start = threading.Barrier(lanes)

    def race():
        start.wait(timeout=10)
        f.spawn(["claude"])

    threads = [threading.Thread(target=race) for _ in range(lanes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not latch.overlapped, (
        "two lanes were inside `_observe` together — the verdict is unguarded")
    assert f.runs == lanes


def test_concurrent_lanes_all_get_counted(monkeypatch):
    """The property the mutual-exclusion test above protects, asserted end to
    end. On its own this one is a regression guard, not a discriminator: the
    unlocked `_observe` passes it (see `_Latch`)."""
    _wire(monkeypatch)
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    lanes = 16
    gate = threading.Barrier(lanes)

    def race():
        gate.wait(timeout=10)
        for _ in range(20):
            f.spawn(["claude"])

    threads = [threading.Thread(target=race) for _ in range(lanes)]
    with _switching_hard():
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert f.runs == lanes * 20


def test_concurrent_downgrades_keep_the_first_reason_and_count_once(monkeypatch):
    """`_downgrade` is a check-then-act over three attributes. Two lanes
    downgrading together could both pass the `KERNEL` guard, leaving the SECOND
    reason standing and the count at two — contradicting the first-reason-wins
    rule this class is built on."""
    _wire(monkeypatch, job=(None, "CreateJobObject failed (WinError 5)"))
    _kernel(monkeypatch)
    f = fence_mod.Fence(_limits())
    lanes = 16
    gate = threading.Barrier(lanes)

    def race():
        gate.wait(timeout=10)
        for _ in range(20):
            f.spawn(["claude"])

    threads = [threading.Thread(target=race) for _ in range(lanes)]
    with _switching_hard():
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert f.enforcement == res_mod.TIMEOUT_ONLY
    assert f.reason == "CreateJobObject failed (WinError 5)"
    assert f.downgrades == 1, "the guard let a second downgrade through"


# --- the limits actually reach the mechanism ----------------------------------

def test_the_fences_limits_are_the_ones_the_job_is_built_from(monkeypatch):
    seen = _wire(monkeypatch)
    _kernel(monkeypatch)
    lim = _limits(memory_mb=128, cpu_s=30)
    fence_mod.Fence(lim).spawn(["claude"])
    assert seen["limits"] is lim
