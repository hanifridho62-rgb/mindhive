# SPDX-License-Identifier: Apache-2.0
# src/mindhive/orchestrator/fence.py
"""The ceiling a real lintester child actually runs under.

## What this exists to fix

`LintesterOrchestrator` has carried a `limits` field and reported an
`enforcement` on every `Mission` since the sandbox landed, and **nothing was
ever fenced**. `execute_mission` asked `resilience.fencing(self.limits)` — which
answers what the PLATFORM can do — and printed `kernel`. The child meanwhile
went `_run_child` -> `mindnet.agent.run` -> `claude_cli.run_headless` -> a bare
`subprocess.run`: no job object, no rlimit, no ceiling of any kind, and no
`_terminate_job`, so a timed-out `claude` left its grandchildren alive. A report
that says `kernel` over a child nothing was holding is exactly the laundering
`resilience`'s own docstring forbids, and it was live.

## Why not just call `run_sandboxed`

Because `run_sandboxed` owns the whole run, and the child's argv is not this
package's to build. `claude_cli.run_headless` owns the `.CMD` shim resolution,
the prompt-via-stdin rule (cmd.exe dies at ~8k of command line), the
`MINDHIVE_HEADLESS` recursion guard and the `--resume` identity — none of which
may be duplicated here. `run_sandboxed` also answers a `SandboxResult` and
swallows the timeout, while `run_headless` needs a `CompletedProcess` and the
raised `TimeoutExpired` it maps to exit 124.

So the fence is INVERTED: instead of the sandbox running the command, the
command is handed a `spawn` callable and this module IS that callable. It
composes exactly what `run_sandboxed` composes — `_preexec_for`, `_job_for`,
`_run_capped`, `_close_job` — and nothing else. Anything it did differently
would be a second sandbox drifting from the first.

## The prediction may only get WORSE

`Fence.enforcement` starts at `resilience.fencing`'s answer, which is what the
platform *says* it can do, and every real spawn can only downgrade it. A job
that could not be created, or that the child never joined, is evidence the
prediction was wrong on this machine, and evidence outranks prediction. Nothing
is ever upgraded: a clean run proves only that THAT child was held, and one
child that escaped its ceiling is enough to make `kernel` a false report for the
mission. The first downgrade's reason is the one kept — it names the cause,
where a later one names only the most recent symptom.

## The POSIX half is where the same bug would have recurred

`jobs._job_for` answers `(None, "")` on POSIX — deliberately NOT a downgrade,
because `_preexec_for`'s `setrlimit` closure is what fences there. A fence that
built a job and forgot the `preexec_fn` would therefore report `kernel` on Linux
over a child holding no ceiling at all: the defect above, reintroduced one layer
up. `spawn` applies both, and `tests/test_orchestrator_fence.py` pins it with a
canary.

## What the verdict covers, exactly

Every child the orchestrator spawns through `LintesterOrchestrator._spawn` —
today the decomposition agent and every lane child. It does NOT cover the
breaker's reflection: `RepairLoop` calls the injected *reflector*, which on the
CLI path reaches `resolve.resolve_batch` -> `reflect._claude_runner` ->
`run_headless` with no `spawn=`, in a chain this package does not own. That leg
is conditional (a breaker has to open first) and is named in TODOS #34 rather
than papered over. A fence that quietly widened its claim to cover a child it
never saw would be the same laundering one subsystem further out.
"""
from __future__ import annotations

import threading

from .. import paths
from . import resilience as res_mod

# Imported into THIS module's globals for the reason `resilience` states about
# its own copies: `spawn` calls these unqualified, so a test that patches
# `fence._job_for` still patches what runs. Note that `_run_capped` resolves
# `_assign` from `resilience`'s globals, not this module's — the assignment seam
# stays where it was built and is not duplicated here.
from .jobs import _close_job, _job_for
from .resilience import _preexec_for, _run_capped


class Fence:
    """A `subprocess.run`-shaped callable that holds its child to *limits*.

    One instance per mission, shared across lanes: `spawn` is called from the
    scheduler's worker threads, so every write to the verdict and the counters
    is taken under `self._lock`. It is a real lock and not a GIL argument:
    `self._runs += 1` is LOAD/ADD/STORE, and `_downgrade` is a check-then-act
    over three attributes, so two lanes downgrading together could both pass the
    `KERNEL` guard and leave the SECOND reason standing — the exact opposite of
    the first-reason-wins rule above.

    A per-lane instance would be simpler and wrong — the verdict is a claim
    about every child the mission ran, so it has to be accumulated in one place.
    `LintesterOrchestrator._spawn` builds this under its own lock for the same
    reason: two lanes that each built one would leave one of them orphaned, and
    an orphaned fence's downgrade is a downgrade nothing reports.
    """

    def __init__(self, limits=None):
        self.limits = limits if limits is not None else res_mod.Limits()
        # The prediction. `fencing` is the ONE place this is derived — deriving
        # it a second time here is the cross-platform defect `lintester` already
        # had once, where a POSIX run reported `timeout-only` with no reason.
        self._enforcement, self._reason = res_mod.fencing(self.limits)
        self._runs = 0
        self._downgrades = 0
        self._lock = threading.Lock()

    @property
    def enforcement(self) -> str:
        """`resilience.KERNEL` only if every child that ran was really held."""
        return self._enforcement

    @property
    def reason(self) -> str:
        """Why it is not `kernel`, or "" when it is."""
        return self._reason

    @property
    def runs(self) -> int:
        """Children spawned through this fence. Zero means the verdict above is
        still the platform's prediction and nothing has tested it."""
        return self._runs

    @property
    def downgrades(self) -> int:
        return self._downgrades

    def spawn(self, argv, *, input=None, timeout=None, cwd=None, env=None,
              creationflags=None, capture_output=True, text=True, **kwargs):
        """Run *argv* under the ceiling, with `subprocess.run`'s call shape.

        Passed as `spawn=` to `claude_cli.run_headless`, so the accepted
        keywords are fixed by the one call that module makes: `capture_output`,
        `text`, `timeout`, `env`, `input`, `cwd`, `creationflags`.
        `capture_output` and `text` are accepted and not forwarded because
        `_run_capped` already pipes all three streams and decodes them — it has
        no other mode, and a parameter that could only be passed at its one
        supported value is not a choice.

        Answers a real `CompletedProcess` and raises the same `TimeoutExpired`
        `run_headless` already maps to exit 124, so nothing downstream needs a
        branch for whether it was fenced.

        `input` becomes `stdin_text`: the prompt must never reach argv, because
        the Windows CLI is a `.CMD` shim hosted by cmd.exe and cmd.exe rejects a
        command line past ~8k characters.
        """
        # POSIX fences with `setrlimit` in the child and Windows with a job
        # object; a platform may have neither, and `fencing` has already said so
        # in `self._reason`. Both are asked for here because `_job_for` answers
        # `(None, "")` on POSIX — no job AND no complaint — and a fence that
        # read that as "held" would report a ceiling nothing was applying.
        extra = dict(kwargs)
        preexec = _preexec_for(self.limits)
        if preexec is not None:
            # Only when there is something to apply: `preexec_fn` forces the
            # slow fork path even when its body does nothing.
            extra["preexec_fn"] = preexec

        job, job_why = _job_for(self.limits)
        state: dict = {}
        try:
            return _run_capped(
                [str(a) for a in (argv or ())], job, state,
                cwd=str(cwd) if cwd else None, env=env, timeout=timeout,
                stdin_text=input,
                creationflags=(paths.NO_WINDOW if creationflags is None
                               else creationflags),
                **extra)
        finally:
            # In a `finally` so the timeout path — the one that most needs the
            # tree taken — records and closes exactly like the clean one.
            try:
                self._observe(job, job_why, state)
            finally:
                # Nested, so bookkeeping can never cost a handle: a raise from
                # `_observe` would otherwise leak a KILL_ON_JOB_CLOSE job, and
                # a leaked job holds its whole process tree alive.
                # `_run_capped` has already called `_terminate_job` by then;
                # this is still the single close `jobs._close_job` documents,
                # because closing a raw HANDLE twice is worse than leaking it.
                if job is not None:
                    _close_job(job)

    def _observe(self, job, job_why, state) -> None:
        """Fold one child's real outcome into the mission's verdict.

        The two questions are `run_sandboxed._fenced`'s, asked for the same
        reason: a job that could not be CREATED and a job the child never JOINED
        are different failures and both leave the child unheld.

        The whole fold is one critical section, so a lane that downgrades and a
        lane that merely counts can never interleave into a verdict neither of
        them observed."""
        with self._lock:
            self._runs += 1
            if job_why:
                self._downgrade(job_why)
            elif job is not None and not state.get("assigned", True):
                self._downgrade(state.get("why") or "the job was not assigned")

    def _downgrade(self, why) -> None:
        """Record that a child was not held after all. Caller holds `_lock`.

        Only from `KERNEL`. When `fencing` already answered `timeout-only` the
        platform's own reason is the true one and a per-run symptom must not
        overwrite it — and when nothing was asked for, `_job_for` answers no
        complaint at all, so an empty ceiling can never look like a degradation.
        """
        if self._enforcement != res_mod.KERNEL:
            return
        self._enforcement = res_mod.TIMEOUT_ONLY
        self._reason = str(why or "").strip() or "the child was not fenced"
        self._downgrades += 1
