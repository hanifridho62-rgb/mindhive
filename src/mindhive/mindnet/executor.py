# SPDX-License-Identifier: Apache-2.0
# src/mindhive/mindnet/executor.py
"""MN-1 U6 — the validation executor: the one place in this codebase where
Python runs a string an agent authored.

`tasks.py` owns the graph axis, `phases.py` the evidence contract,
`controller.py` the phase axis. This module owns exactly one thing: taking a
task's ``validation_script``, running it as an argv vector behind a fail-closed
gate, and turning what comes back into durable, machine-verified evidence.

## argv, never a shell

``shlex.split(script, posix=False)`` -> argv, then ``subprocess.run`` with
``shell=False``. No shell means ``;``, ``&&``, ``|``, redirection and expansion
are inert: the tail of a second command arrives as plain arguments to the first
rather than as a second process.

**``posix=False`` is load-bearing on this machine and must not be "tidied".**
POSIX-mode ``shlex`` treats a backslash as an escape character, so
``pytest C:\\Users\\hanif\\mindhive\\tests`` silently becomes
``pytest C:Usershanifmindhivetests``. The command then fails for a reason that
has nothing to do with the code under test, and this module records a FALSE
``result=fail`` as python-verified evidence — the single worst outcome available
to a machine whose entire purpose is honest verification.

Non-POSIX mode has one cost: it keeps the quote characters inside the token, so
``"C:\\a b\\x.py"`` arrives with its quotes attached and would reach the child
as a literal. `_dequote` strips one surrounding matched pair per token, which
buys back quoted paths without giving back the escape processing.

## The gate fails CLOSED, and that inverts the advisory tier on purpose

`gateguard.is_destructive_command` runs as a PRE-FLIGHT REFUSAL, never as an
authorization. Two refusal triggers, and the second is the whole point:

1. any reason string it returns; and
2. **any exception raised inside it.**

`enforce.evaluate_policies` wraps the same classifier in
``except Exception: return None`` — allow — and its destructive tier is ``ask``,
not ``deny``. That is correct there: it is an advisory hook, and a crash in a
classifier must not block a real tool call the operator is making by hand. Here
the identical crash would mean "no reason was found, therefore run it". Fail
open is right for an advisory gate and fail-DANGEROUS for an execution gate; the
asymmetry is deliberate and `test_the_advisory_hook_fails_open_where_this_gate_fails_closed`
pins both halves side by side.

gateguard is used rather than a hand-rolled denylist of command prefixes because
it is already tested and already defeats the evasions a prefix check cannot:
`_strip_quoted` and `_explode_subshells` catch ``"rm" -rf x``, ``x && rm -rf y``
and ``$(rm -rf y)``, none of which start with ``rm``.

## The publish denylist, and why it is separate

gateguard does NOT flag `gh`, and it does not flag a plain `git` publish — only
the ``--force`` spellings. So a second, narrower refusal runs on the RESOLVED
argv: the program's basename (so an absolute path or a quoted token cannot
smuggle it past) against `DENIED_PROGRAMS`, plus `DENIED_GIT_SUBCOMMANDS`
scanned across every non-option token of a `git` invocation.

Scanning every token rather than parsing git's option grammar is deliberately
the more aggressive choice — gateguard's own `_split_segments` makes the same
tradeoff for the same reason. It cannot be evaded by an option-value trick, and
a false positive is recoverable through `agent_satisfy`.

This is what turns the repo's GitHub boundary from a claim into something
enforced. A source-grep test mirrors `tests/test_research.py`'s clause (d).

It used to be paired with a second claim — that `pr-green` therefore stays
agent-reported — and MN-8b severed the two. The boundary is unchanged and the
reasoning behind it still holds: `gh` is refused, so nothing here can read
GitHub's CI matrix, and nothing here may claim to. What did not follow is the
conclusion. The terminal gate's evidence is the LOCAL suite's exit code, which
this module produces by running a real subprocess, so `python` is the honest
label for it rather than an overreach. See `phases.py`'s `PHASE_EVIDENCE`
comment for the bounded version of the claim.

## Refusal must not wedge a task

`agent_satisfy` is the escape hatch KTD15 requires: a satisfy carrying the
refusal reason VERBATIM and stamped ``agent``, so a classifier false positive
cannot deadlock a task while the ledger still shows Python did not verify it.

It returns ``clears`` alongside the event, and callers must read it. Against the
committed contracts ``clears`` is **False for `audit-bug` and for `pr-green`**:
both descriptors name ``python`` as their verifier and `controller._apply_satisfy`
refuses any weaker claim, so the event lands honestly labelled and the phase
stays unsatisfied. Surfacing that beats printing a success line over a no-op.

`pr-green` joined the set in MN-8b, and there the refusal is the POINT rather
than a gap: a task must not be closeable by an agent asserting its own PR is
green. That is only safe because the mechanical route is live — `validate` runs
the suite and stamps `python` — and because `fall_back` is still open. Both
exits are pinned in `tests/test_mindnet_executor.py`.

`audit-bug` remains the unclosed case: its python verifier having REFUSED is
exactly when the hatch is needed, and a fallback authority in `phases.py` or
`controller.py` is still its own slice.

## Artifacts, and how ``n`` is derived

Output goes to MN-0's store through ``runlog.RunLog(run_key=f"mn-<task>-<n>")``,
opened BEFORE the first write, then `finalise`, whose absolute path becomes the
evidence ref. MN-0's contract is honoured unchanged: the 1 MiB cap, the in-file
truncation marker, the 2000-char event excerpt. A lost artifact degrades to the
excerpt and never raises.

``n`` is DERIVED, never passed: one plus the number of prior command-kind
``satisfy`` rows for that ``task_id`` **on the raw ledger**.

*Deviation, deliberate:* the plan says to count them in the folded controller
state, and that is not monotonic. `controller._resolve` collapses every record
for one ``(phase, epoch)`` into a SINGLE `Satisfaction`, and a validation run
does not move the phase — so a third run at the same gate would be handed the
same key as the second and overwrite its evidence, which is precisely the
durability failure MN-0 exists to prevent. A refused or violating row folds to
nothing at all, with the same result. Counting raw rows is monotonic by
construction (the ledger is append-only), survives across processes, and is
stable under `rebuild`, which re-reads the same rows. `controller.py` learned
this exact lesson in `_restat_fail_recorded`: ask the ledger, not the fold.

## Honesty

``verified_by`` is set by this code path and is not a parameter of any public
function here (KTD2). A run that never happened emits no event at all: a
refusal, a task with no record, a task parked at a phase that does not take
command evidence, and a task with no script all return without touching the
chain.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from typing import NamedTuple

from .. import events as ev_mod
from .. import gateguard
from .. import paths
from .. import runlog
from . import controller as c_mod
from . import phases as ph_mod
from . import tasks as t_mod

# The authority this module may claim, and the only one it ever stamps. A real
# subprocess ran and a real exit code was read, so `python` is the honest label
# (KTD2) — and it is the STRONGEST, which is why nothing else may write it.
VERIFIED_BY = "python"

# What `agent_satisfy` stamps instead. Weaker on purpose: no Python check ran.
AGENT_VERIFIED_BY = "agent"

# Ten minutes. This repo's own suite is the archetypal validation script and
# runs in a couple of minutes; the cap exists so a hung run cannot park the
# machine forever, not to be a performance budget.
DEFAULT_TIMEOUT_S = 600

# Refused outright on the resolved argv, whatever path or quoting spells them.
DENIED_PROGRAMS = ("gh",)
DENIED_GIT_SUBCOMMANDS = ("push",)

RUN_RESULTS = ("ok", "fail", "timeout", "refused", "spawn-failed")

# `research.run_experiment`'s spelling for "the process never started".
SPAWN_FAILED_EXIT = 127

_PATH_HEAD = re.compile(r"^.*[\\/]")
_EXE_SUFFIX = re.compile(r"\.exe$", re.IGNORECASE)


# --- shapes ----------------------------------------------------------------------

class RunResult(NamedTuple):
    """One validation run's outcome.

    ``executed`` is the SECURITY property and is deliberately not derivable from
    ``result``: it says a child process was actually created. Every test that
    asserts a refusal refused reads it, alongside the mock's marker file — a
    return value alone could be produced after the damage was already done."""

    result: str                     # one of RUN_RESULTS
    reason: str = ""                # why it was refused / why it failed
    exit_code: int | None = None
    executed: bool = False
    artifact: str | None = None     # absolute path, or None when it was lost
    excerpt: str = ""               # the bounded copy that rides the event
    # What the agent actually said, uncut. `excerpt` is a LEDGER bound and was
    # for a while the only copy kept, so a caller that had to READ the reply --
    # `decompose.run` -- parsed a string cut at `runlog.EXCERPT_CHARS`. A reply
    # longer than that lost its closing brace mid-string and was reported as
    # unparseable json, which is a claim about the agent that was not true. Held
    # separately rather than by widening `excerpt`, because every event detail
    # is cut from that one and none of them should grow.
    text: str = ""
    # Turns the child took, from the envelope. Read for ONE reason: a refusal
    # that arrives after real turns must not tell the operator the tree is
    # untouched. It deliberately does NOT feed `executed` -- see `agent._classify`.
    turns: int = 0
    stdout: str = ""
    stderr: str = ""
    argv: tuple = ()
    run_key: str = ""
    cwd: str = ""
    duration_s: float = 0.0

    @property
    def evidence_result(self) -> str:
        """`phases.RESULTS` has only ``ok`` and ``fail``. A run the machine
        killed at the cap is not a pass, so everything that is not a clean exit
        records `fail` and says why in the detail."""
        return "ok" if self.result == "ok" else "fail"


class Validation(NamedTuple):
    """`validate`'s answer: the run, the event it appended (None when nothing
    was verified), and the reason nothing was."""

    run: RunResult
    event: object = None
    reason: str = ""


class AgentSatisfy(NamedTuple):
    """`agent_satisfy`'s answer. ``clears`` is not decoration — see the module
    docstring: against the committed descriptors it is False for `audit-bug`
    and for `pr-green`, and a caller that ignores it will report success for a
    no-op."""

    event: object
    clears: bool
    note: str = ""


# --- the gate ---------------------------------------------------------------------

def _dequote(token) -> str:
    """Strip ONE surrounding matched quote pair. ``posix=False`` keeps the
    quotes inside the token, so without this a quoted path reaches the child
    with its quotes attached — and a quoted program name walks straight past a
    basename denylist."""
    text = str(token)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _program(token) -> str:
    """``/usr/bin/gh``, ``"gh"``, ``GH.exe`` -> ``gh``. Mirrors
    `gateguard._basename`, applied after dequoting."""
    text = _PATH_HEAD.sub("", _dequote(token))
    return _EXE_SUFFIX.sub("", text).lower()


def _destructive_refusal(raw):
    """gateguard as a pre-flight REFUSAL. Fail closed on both triggers."""
    try:
        reason = gateguard.is_destructive_command(raw)
    except Exception as e:                       # noqa: BLE001 — intentional
        # THE inversion (KTD15). `enforce.evaluate_policies` swallows this and
        # allows, because it is an advisory hook where a crashing classifier
        # must not block the operator's own tool call. Here the same crash would
        # read as "no reason found, therefore run it". An execution gate that
        # cannot classify has not cleared anything.
        return (f"the destructive-command classifier raised "
                f"{type(e).__name__}: {e} — an execution gate that cannot "
                f"classify a command refuses it. Fix the classifier, or "
                f"record the result with an agent-reported satisfy")
    if reason:
        return (f"destructive-command gate: {reason}. A validation script runs "
                f"unattended with your privileges; if this is genuinely what "
                f"the check needs, narrow it or record the result with an "
                f"agent-reported satisfy")
    return None


def _denylist_refusal(argv):
    """The narrower refusal gateguard does not make, on the RESOLVED argv."""
    program = _program(argv[0])
    if program in DENIED_PROGRAMS:
        return (f"denylist: '{program}' is refused here — a validation run "
                f"verifies locally and may not reach for the forge's own "
                f"client. The terminal gate's evidence is this repository's "
                f"own suite exiting zero, not GitHub's answer about it")
    if program != "git":
        return None
    for token in argv[1:]:
        word = _dequote(token)
        if word == "--":
            break
        if word.startswith("-"):
            continue
        if word.lower() in DENIED_GIT_SUBCOMMANDS:
            return (f"denylist: the git '{word.lower()}' subcommand is refused "
                    f"here — a validation run verifies, it does not publish")
    return None


def gate(script):
    """``(argv, None)`` when *script* may run, ``(None, reason)`` when it may
    not. Pure: no clock, no I/O, no store access — the same shape
    `phases.resolve_ref` uses, so a caller can ask without doing.

    FAIL CLOSED at every step. Order matters and is fixed:

    1. an empty or blank script;
    2. `gateguard.is_destructive_command` over the RAW text, refusing on a
       reason string AND on any exception (see the module docstring) — the raw
       text is what it wants, because separators and substitutions are exactly
       what it exists to see through;
    3. ``shlex.split(script, posix=False)``, refusing an unparseable command;
    4. the publish denylist over the RESOLVED, dequoted argv, which step 3 is a
       precondition for — a basename cannot be taken before the tokens are."""
    raw = "" if script is None else str(script)
    if not raw.strip():
        return None, ("there is no validation script to run — set one with "
                      "`hive mindnet declare --validation-script ...`")
    reason = _destructive_refusal(raw)
    if reason:
        return None, reason
    try:
        tokens = shlex.split(raw, posix=False)
    except ValueError as e:
        return None, (f"cannot parse the validation script into an argv "
                      f"vector ({e}) — it is split as a command, never handed "
                      f"to a shell")
    argv = [_dequote(t) for t in tokens]
    if not argv or not argv[0].strip():
        return None, "the validation script names no program to run"
    reason = _denylist_refusal(argv)
    if reason:
        return None, reason
    return tuple(argv), None


# --- running -----------------------------------------------------------------------

def _as_text(value) -> str:
    """Captured output as str. ``text=True`` should make this a no-op, but
    `TimeoutExpired` carries whatever the post-kill drain produced and a total
    read is cheaper than a surprise."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _header(script, argv, root, timeout_s) -> str:
    """Written to the artifact BEFORE the child is spawned, so an artifact
    exists even for a run that produced no output at all, and so the artifact
    records what was actually executed rather than what was requested."""
    return ("mindnet validation run\n"
            f"script: {script}\n"
            f"argv:   {list(argv)}\n"
            f"cwd:    {root}\n"
            f"cap:    {timeout_s}s\n")


def _finalise(rl):
    """The artifact path, or None. MN-0's contract: losing evidence degrades
    every consumer to the excerpt and breaks nothing, so this never raises."""
    try:
        return rl.finalise()
    except OSError:
        return None


def _child_env(extra):
    """``os.environ`` merged with *extra*, or ``None`` to inherit unchanged.

    **Merged, never substituted.** A bare replacement drops `PATH` and — on
    Windows — `SYSTEMROOT`, and a child with no `SYSTEMROOT` does not start at
    all: the failure arrives as an opaque `spawn-failed`, which looks like a bad
    command rather than a bad environment.

    A value of ``None`` REMOVES the variable, so a caller can hand a child a
    clean slate for one setting instead of only overwriting it. Anything that is
    not a mapping is ignored: this module maps every fault to a typed result, and
    a bad argument must not be the one thing that escapes as a crash.

    **The SOP process token is withheld LAST** (blueprint P1d,
    `advice.scrub_agent_env`), after the overlay, so no caller can hand it to a
    child. With no token in the parent that is the identity -- ``None`` still
    means "inherit unchanged" -- so every pre-P1d caller spawns exactly as it
    did."""
    from .. import advice as authorship
    if not isinstance(extra, dict) or not extra:
        return authorship.scrub_agent_env(None)
    merged = dict(os.environ)
    for key, value in extra.items():
        if value is None:
            merged.pop(str(key), None)
        else:
            merged[str(key)] = str(value)
    return authorship.scrub_agent_env(merged)


def execute(run_key, script, root=None, timeout_s=DEFAULT_TIMEOUT_S,
            project=None, max_bytes=None, env=None,
            _clock=time.monotonic) -> RunResult:
    """Run *script* as argv and capture everything. Appends nothing to the
    ledger — `validate` owns that, so this stays callable from a dry run.

    *env* is an optional overlay on the child's environment (MN-7): the
    orchestrator uses it so a spawned child inherits the parent's MN-6 arming
    decision. ``None`` inherits the parent environment unchanged, which is what
    every pre-MN-7 caller relies on — see `_child_env` for the merge rules.

    Exceptions are narrowed to ``(OSError, subprocess.SubprocessError)`` and
    mapped to a typed result, mirroring `research.run_experiment`: a spawn
    failure is a recorded outcome, never an untyped crash escaping into a
    caller's phase transition.

    *root* is the worktree the command runs in. ``None`` resolves
    `paths.worktree_root`, and no worktree is a REFUSAL rather than a fallback
    to the current directory — `phases.resolve_ref` and `controller.restat`
    refuse for the same reason: evidence produced somewhere unrelated is not
    this task's evidence.

    ``timeout`` kills the direct child only. `research.py` carries a whole
    `_kill_tree` for the process-group case; a validation command is not the
    long-lived trainer that needed it, and the plan pins this call shape."""
    key = str(run_key)
    argv, reason = gate(script)
    if argv is None:
        return RunResult(result="refused", reason=reason, run_key=key)
    if root is None:
        root = paths.worktree_root()
    if root is None:
        return RunResult(
            result="refused", run_key=key, argv=argv,
            reason=("no git worktree here — a validation run is resolved "
                    "against the worktree root, so there is nothing to run it "
                    "in"))

    # A bare `python` that PATH would hand to the Windows Store redirector is
    # swapped for the interpreter running this process, and the swap is written
    # into the artifact: the header's contract is what was actually run.
    argv, note = paths.resolve_interpreter(argv)
    rl = runlog.RunLog(key, project=project, max_bytes=max_bytes)
    rl.append(_header(script, argv, root, timeout_s))
    if note:
        rl.append(f"note:   {note}\n")
    start = _clock()
    timed_out = False
    try:
        proc = subprocess.run(
            list(argv), shell=False, capture_output=True, text=True,
            timeout=timeout_s, cwd=str(root),
            env=paths.no_bytecode_env(_child_env(env)),
            creationflags=paths.NO_WINDOW)
        out, err, code = (_as_text(proc.stdout), _as_text(proc.stderr),
                          proc.returncode)
    except subprocess.TimeoutExpired as e:
        # Listed first: TimeoutExpired IS a SubprocessError, so the broad clause
        # below would otherwise swallow it and lose the partial output the
        # post-kill drain already recovered.
        timed_out = True
        out, err, code = _as_text(e.stdout), _as_text(e.stderr), None
    except (OSError, subprocess.SubprocessError) as e:
        detail = f"spawn failed: {type(e).__name__}: {e}"
        rl.append("\n" + detail + "\n")
        return RunResult(
            result="spawn-failed", reason=detail, exit_code=SPAWN_FAILED_EXIT,
            executed=False, artifact=_finalise(rl), excerpt=rl.excerpt(),
            argv=argv, run_key=key, cwd=str(root),
            duration_s=round(_clock() - start, 3))

    duration = round(_clock() - start, 3)
    if out:
        rl.append("\n--- stdout ---\n" + out)
    if err:
        rl.append("\n--- stderr ---\n" + err)
    if timed_out:
        result = "timeout"
        reason = (f"the validation run exceeded its {timeout_s}s cap and was "
                  f"killed; the output it had already produced is kept")
        rl.append(f"\n--- killed: timeout after {timeout_s}s ---\n")
    else:
        result = "ok" if code == 0 else "fail"
        reason = "" if code == 0 else f"the validation run exited {code}"
        rl.append(f"\n--- exit {code} after {duration}s ---\n")
    return RunResult(
        result=result, reason=reason, exit_code=code, executed=True,
        artifact=_finalise(rl), excerpt=rl.excerpt(),
        stdout=out[:runlog.EXCERPT_CHARS], stderr=err[:runlog.EXCERPT_CHARS],
        argv=argv, run_key=key, cwd=str(root), duration_s=duration)


# --- the run index -------------------------------------------------------------------

def run_key(task_id, n) -> str:
    """MN-1's runlog key family (plan Assumption 4). ``runlog._safe`` sanitizes
    the components on the way to a filename, so a raw ``task_id`` is safe."""
    return f"mn-{task_id}-{n}"


def next_run_index(evs, task_id) -> int:
    """One plus the number of command-kind ``satisfy`` rows already on the
    ledger for *task_id*.

    Counted over the RAW rows, not over the folded record — see the module
    docstring. The fold collapses contradicting records for one
    ``(phase, epoch)`` into a single `Satisfaction` and drops refused ones
    entirely, either of which hands two runs the same key and lets the second
    overwrite the first's evidence.

    Total, like every read here: a malformed row is skipped, never raised."""
    n = 0
    for ev in evs or []:
        if getattr(ev, "type", None) != "mindnet_phase":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        if p.get("action") != "satisfy" or p.get("task_id") != task_id:
            continue
        evidence = p.get("evidence")
        if isinstance(evidence, dict) and evidence.get("kind") == "command":
            n += 1
    return n + 1


# --- evidence emission -------------------------------------------------------------

def _refused(reason) -> Validation:
    return Validation(RunResult(result="refused", reason=reason), None, reason)


def _summary(run) -> str:
    if run.result == "timeout":
        return (f"validation run {run.run_key}: timeout after "
                f"{run.duration_s}s — killed at the cap")
    if run.result == "spawn-failed":
        return f"validation run {run.run_key}: {run.reason}"
    return (f"validation run {run.run_key}: exit {run.exit_code} in "
            f"{run.duration_s}s")


def _satisfy_payload(task_id, rec, run) -> dict:
    ref = run.artifact or ""
    if len(ref) > ph_mod.MAX_REF_CHARS:
        # A ref the contract would reject must not fail the run: MN-0's rule is
        # that a consumer degrades to the excerpt, which is already in the
        # evidence detail below.
        ref = ""
    evidence = {"kind": "command", "ref": ref, "result": run.evidence_result,
                "detail": run.excerpt[:ph_mod.MAX_DETAIL_CHARS]}
    if run.cwd:
        evidence["root"] = run.cwd[:ph_mod.MAX_REF_CHARS]
    return {
        "action": "satisfy", "task_id": task_id, "phase": rec.phase,
        "epoch": rec.epoch, "detail": _summary(run)[:ph_mod.MAX_DETAIL_CHARS],
        # Derived here, never accepted (KTD2). A subprocess ran and its exit
        # code was read by this code path; nothing else may claim that.
        "verified_by": VERIFIED_BY,
        "evidence": evidence,
    }


def validate(task_id, script=None, root=None, timeout_s=DEFAULT_TIMEOUT_S,
             evs=None, tasks=None, project=None, session_id=None, ts=None,
             max_bytes=None) -> Validation:
    """Run *task_id*'s validation script and record what happened.

    Four refusals happen BEFORE anything is spawned, and each one appends
    nothing: no phase record, a phase that does not take command evidence, no
    script, and the gate itself. A satisfy the fold would only record as a
    violation is worse than no satisfy at all — it grows the hash chain and
    tells the operator the opposite of the truth.

    *script* defaults to the task's stored ``validation_script``; passing one
    explicitly is for tests and for a one-off re-check."""
    if evs is None:
        evs = ev_mod.read_all(project)
    if tasks is None:
        tasks = t_mod.fold(evs)
    rec = c_mod.fold(evs, tasks).records.get(task_id)
    if rec is None:
        return _refused(f"no phase record for '{task_id}' — declare it on both "
                        f"axes first: hive mindnet declare {task_id}")
    desc = ph_mod.descriptor(rec.phase) or {}
    if desc.get("kind") != "command":
        return _refused(
            f"'{task_id}' is at '{rec.phase}', which takes "
            f"{desc.get('kind') or 'no'} evidence — a command-kind satisfy "
            f"there folds as a violation, so nothing was run")
    if script is None:
        script = getattr(tasks.get(task_id), "validation_script", "")
    if not str(script or "").strip():
        return _refused(f"'{task_id}' has no validation_script, so there is "
                        f"nothing for '{rec.phase}' to verify")

    run = execute(run_key(task_id, next_run_index(evs, task_id)), script,
                  root=root, timeout_s=timeout_s, project=project,
                  max_bytes=max_bytes)
    if run.result == "refused":
        return Validation(run, None, run.reason)

    payload = _satisfy_payload(task_id, rec, run)
    ph_mod.validate_stored(payload)          # the writer contract, before the write
    ev = ev_mod.append_event("mindnet_phase", payload, session_id=session_id,
                             tags=["mindnet", "phase", "validation"], ts=ts,
                             project=project)
    _record_task_evidence(task_id, run, session_id=session_id, ts=ts,
                          project=project)
    return Validation(run, ev, run.reason)


def _record_task_evidence(task_id, run, session_id=None, ts=None, project=None):
    """Mirror a successful run's artifact onto the task's GRAPH-axis evidence.

    `tasks.can_complete` refuses a non-bypassed task with ``"has no evidence — a
    validation run must record its artifact before this task can complete"``. That
    sentence described a producer that did not exist: `Task.evidence` is written
    only by a `mindnet_task` ``action: "evidence"`` row, and nothing in the codebase
    ever wrote one. So the field was permanently empty and the gate was
    unsatisfiable — which went unnoticed only because nothing called the gate
    either. Wiring the gate without this would have made every normal task
    permanently incompletable.

    This is the honest producer: a validation run is exactly the thing that
    "records its artifact", and `run.artifact` is that artifact.

    Best-effort. The phase-axis satisfy above is the load-bearing write and has
    already landed; failing the whole validation because a mirror row could not be
    appended would turn a successful run into a reported failure. A missing
    artifact (MN-0 lost the file) simply records nothing — `evidence` requires a
    non-empty ``ref`` by contract, and inventing one would be worse than absent."""
    ref = (run.artifact or "")[:t_mod.MAX_REF_CHARS]
    if not ref.strip() or run.evidence_result != "ok":
        return None
    payload = {"action": "evidence", "task_id": task_id, "ref": ref}
    try:
        t_mod.validate_payload(payload)
        return ev_mod.append_event("mindnet_task", payload,
                                   session_id=session_id,
                                   tags=["mindnet", "task", "validation"],
                                   ts=ts, project=project)
    except Exception:
        return None


# --- the escape hatch ---------------------------------------------------------------

def agent_satisfy(task_id, reason, evs=None, tasks=None, project=None,
                  session_id=None, ts=None) -> AgentSatisfy:
    """Record an AGENT-reported satisfy carrying *reason* verbatim (KTD15).

    A classifier false positive must not deadlock a task forever, while the
    ledger still shows that Python did not verify it. The reason rides both the
    payload ``detail`` and the evidence ``detail`` unaltered, so the refusal is
    legible at either read depth.

    Read ``clears``. Against the committed descriptors it is False for
    `audit-bug` and for `pr-green`: both name ``python`` as their verifier, and
    `controller._apply_satisfy` refuses any weaker claim, so this event is
    recorded as a violation and the phase stays unsatisfied. The task can still
    fall back; it cannot pass that gate until something stronger clears it.

    At `pr-green` (MN-8b) that refusal is deliberate rather than a shortfall —
    the terminal gate exists precisely so a task cannot be closed by assertion —
    and the mechanical route through `validate` remains open."""
    text = "" if reason is None else str(reason)
    if not text.strip():
        raise ValueError(
            "an agent-reported satisfy must carry the refusal reason verbatim "
            "— an unexplained one is the self-attestation the two-validator "
            "contract exists to stop")
    if len(text) > ph_mod.MAX_DETAIL_CHARS:
        raise ValueError(f"reason exceeds {ph_mod.MAX_DETAIL_CHARS} chars")
    if evs is None:
        evs = ev_mod.read_all(project)
    if tasks is None:
        tasks = t_mod.fold(evs)
    rec = c_mod.fold(evs, tasks).records.get(task_id)
    if rec is None:
        raise ValueError(f"no phase record for '{task_id}' — there is no phase "
                         f"to satisfy")
    desc = ph_mod.descriptor(rec.phase) or {}
    kind = desc.get("kind")
    if kind == "file":
        raise ValueError(
            f"'{rec.phase}' takes file evidence — satisfy it with the document "
            f"ref, not with a run refusal")

    need = desc.get("verified_by")
    try:
        clears = (ph_mod.VERIFIERS.index(AGENT_VERIFIED_BY)
                  <= ph_mod.VERIFIERS.index(need))
    except ValueError:
        clears = False
    note = "" if clears else (
        f"'{rec.phase}' is verified by {need}, so the fold records this "
        f"agent-reported satisfy as a violation and the phase stays "
        f"unsatisfied — the task may still fall back, but it cannot pass this "
        f"gate until a {need} check clears it")

    payload = {
        "action": "satisfy", "task_id": task_id, "phase": rec.phase,
        "epoch": rec.epoch, "detail": text,
        # Weaker on purpose and set here, not accepted: no Python check ran.
        "verified_by": AGENT_VERIFIED_BY,
        "evidence": {"kind": kind or "command", "ref": "", "result": "ok",
                     "detail": text},
    }
    ph_mod.validate_stored(payload)
    ev = ev_mod.append_event("mindnet_phase", payload, session_id=session_id,
                             tags=["mindnet", "phase", "validation"], ts=ts,
                             project=project)
    return AgentSatisfy(ev, clears, note)
