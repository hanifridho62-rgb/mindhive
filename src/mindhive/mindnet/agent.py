# src/mindhive/mindnet/agent.py
"""MN-8 — what a spawned agent actually RUNS for a task.

This module answers the question that made MN-7's orchestrator decoration.

`Task.validation_script` is a **validator**, not an implementer: it is the command
`hive mindnet validate` runs to prove a phase cleared. The orchestrator was
handing that to `executor.execute`, so a task with no script was refused and a
task with one merely re-ran its own check. Nothing anywhere could take a task and
*do the work*. The graph, the phases, the frontier, the wave loop and the
durability were all real; the thing they existed to schedule did not exist.

## The command is a headless `claude`, and the primitive already existed

`claude_cli.run_headless` is the repo's one home for a one-shot agent spawn, and
it already solves the three things that bite here: the Windows `.CMD` shim
(`shutil.which`, because `subprocess.run(["claude", ...])` cannot launch a bare
`.CMD` name), the prompt travelling on **stdin** rather than argv (cmd.exe rejects
command lines past ~8k chars, which a composed task prompt clears easily), and
`MINDHIVE_HEADLESS=1`, the recursion guard that stops the spawned session
re-firing this project's hooks. Writing a second spawn path would have
reimplemented all three, badly.

## The outcome mapping is where the MN-7 P0 lesson applies again

`RunResult.executed` means "a child process was created", and MN-7 learned the
hard way that a task whose run never started must not get a status row. The same
distinction has a new and much more likely shape here:

**A usage-limit or auth failure returns exit 0 with `is_error` set** — the CLI ran,
the model never worked. Classifying that as `fail` would mark the task `blocked`,
and `tasks.ready` would then remove its whole subtree permanently. Hitting a
weekly limit mid-orchestration would poison the graph. So an `is_error` envelope
maps to `refused`/`executed=False`: nothing ran, nothing is recorded, the task
stays PENDING and the reason says why.

## Concurrency is capped until isolation exists

Every child runs with `cwd=root` — the SAME checkout. Two agents editing one
working tree concurrently corrupt each other's edits and the git index, so agent
mode refuses to run more than one at a time unless the caller explicitly accepts
that. `MAX_SAFE_WORKERS` is that cap, and per-task git worktrees are the real fix
(see the residual notes); this module deliberately ships the sequential-but-honest
version rather than a parallel one that eats work.
"""
from __future__ import annotations

import time

from .. import claude_cli, runlog
from . import executor as x_mod
from . import phases as ph_mod

# Agents do real work. `executor.DEFAULT_TIMEOUT_S` is 600s, sized for a
# validation command; a task implementation needs materially longer, and a cap
# that fires mid-edit leaves a half-written tree.
DEFAULT_TIMEOUT_S = 1800

# How many agents may share one working tree. One. See the module docstring.
MAX_SAFE_WORKERS = 1

# Ceiling on spawns per task when the smart-zone chain is active. A runaway
# backstop, not a budget — the wall-clock cap and the agent's own EXIT_SIGNAL
# are what normally end a chain. Sized so a task that genuinely needs several
# rotations can have them, while a non-terminating agent cannot spawn forever.
DEFAULT_MAX_ITERATIONS = 8

# Kept verbatim from `claude_cli.run_headless`'s documented contract rather than
# re-derived: 127 is "CLI not installed", 124 is "timed out".
_EXIT_NOT_FOUND = 127
_EXIT_TIMEOUT = 124

# The agent ran and replied, and nothing has verified the outcome. Deliberately
# NOT in `executor.RUN_RESULTS`: that tuple is the validation executor's closed
# vocabulary, and an implementer's "I replied" is a different kind of answer from
# a validator's "the check passed". The orchestrator classifies it as writing no
# status row — see `agent.run`'s closing comment for the false-COMPLETED bug this
# exists to prevent.
RESULT_RAN_UNVERIFIED = "ran-unverified"

# Marker prefixed onto the reason when the model could not run because of a QUOTA
# or AUTH condition rather than anything about the task. The founding prompt asked
# for parallelism only "jika usage memungkinkan"; this is the only usage signal the
# CLI actually gives us, and it is retrospective — so the orchestrator uses it as a
# brake (stop fanning out) rather than as a predicate (predict before spawning).
USAGE_MARK = "[usage]"

# Substrings in an `is_error` envelope that mean "the account, not the task".
# `api_error_status` 429 is the reliable one; the rest catch the CLI's own prose for
# conditions that arrive without a status.
_USAGE_HINTS = ("429", "usage limit", "weekly limit", "rate limit", "quota",
                "not logged in", "please run /login", "credit balance")


def is_usage_limit(res) -> bool:
    """Whether *res* failed for an account reason rather than a task reason.

    Read off the reason string because that is where `error_text` already put the
    CLI's own words — inventing a second channel would mean two things to keep in
    sync. Substring matching is crude on purpose: its failure direction is braking
    when it did not need to, which costs one sequential wave, versus spawning N
    agents that will all fail identically."""
    return str(getattr(res, "reason", "") or "").startswith(USAGE_MARK)


def ran(res) -> bool:
    """Whether a child process was actually created for this run.

    Lives here because this module defines the field, and both the orchestrator and
    the decomposer must answer the question identically: MN-7's P0 was treating a
    run that never started as a task failure, which permanently killed the task's
    whole subtree. Two copies of that test is two chances to reintroduce it.

    Direct attribute access rather than `getattr(res, "<name>", False)`: the repo's
    invariant-10 guard forbids a phase-name synonym as a *string literal* anywhere in
    `src/`, and this field's name reads as one for `execution`. Total anyway — a
    runner returning an object without the field answers False, which is the safe
    direction, since nothing then gets recorded."""
    try:
        return bool(res.executed)
    except AttributeError:
        return False


def prompt_for(task, phase=None) -> str:
    """The instruction a spawned agent receives for *task*.

    Built from the graph rather than from prose the operator has to maintain: the
    id, title and description are the task's own, the phase's exit criterion comes
    from `phases.descriptor` (invariant 10 — never a second copy of what clears a
    phase), and `language_context` is restated because MN-4's language lock will
    enforce it on the agent whether or not the agent was told.

    The two closing instructions are this project's own standing rules, and an
    agent that does not know them produces work the repo then rejects: the
    change-log entry is mandatory, and committing or pushing is the operator's
    call, not the swarm's."""
    tid = str(getattr(task, "task_id", "") or "?")
    title = str(getattr(task, "title", "") or "").strip()
    desc = str(getattr(task, "description", "") or "").strip()
    langs = [str(x) for x in (getattr(task, "language_context", ()) or ())]
    ph = phase or str(getattr(task, "phase", "") or "")

    lines = [
        f"You are implementing one task from the MindNetwork task graph.",
        "",
        f"Task id: {tid}",
        f"Title:   {title or '(none recorded)'}",
    ]
    if desc:
        lines += ["", "Description:", desc]
    if ph:
        desc_ph = ph_mod.descriptor(ph) or {}
        need = " or ".join(desc_ph.get("required_dir") or ()) or None
        lines += ["", f"Current phase: {ph}"]
        if need:
            lines.append(f"This phase is cleared by evidence under {need}.")
    if langs:
        lines += ["", f"This task is restricted to: {', '.join(sorted(langs))}. "
                      "Writing another file type will be refused by the "
                      "PreToolUse enforcer."]
    lines += [
        "",
        "Do the work. When you are done:",
        f"  1. Record the change with: hive log <category> \"<what changed>\"",
        "  2. Do NOT commit, push, or open a pull request — the operator owns "
        "that step.",
        "",
        "If you cannot complete the task, say so plainly and explain what "
        "blocked you rather than partially applying a change.",
    ]
    return "\n".join(lines)


def _artifact(run_key, text, project=None):
    """Persist the run's transcript and answer its path, or ``None``.

    MN-0's guarantee applied to agent runs: a child's output has to survive the
    child failing, because a failed agent's explanation of WHY is the only thing
    that makes the failure actionable."""
    try:
        return runlog.write_once(str(run_key), text, project=project)
    except Exception:
        return None


def _iteration_prompt(base_prompt, iteration, rotated) -> str:
    """What the agent is asked on iteration *n*.

    The first iteration is the caller's prompt verbatim — unchanged from the
    one-shot era. A RESUMED iteration needs almost nothing, because the
    conversation is still there. A ROTATED one needs everything: the fresh
    session has no history at all, so the original ask is restated in full or
    the agent works on nothing."""
    if iteration == 1:
        return base_prompt
    if not rotated:
        return ("Continue the task. When the whole task is genuinely finished "
                "and verified, say so plainly and set EXIT_SIGNAL: true.")
    return (
        "# Continuing after a session rotation\n\n"
        "Your previous session reached the context ceiling and was ended to "
        "keep you in the zone where you work best. This session has NONE of "
        "that conversation — treat the task below as the whole of what you "
        "know, and re-read any files you need rather than relying on memory "
        "of them.\n\n"
        "## The task, restated in full\n\n"
        f"{base_prompt}\n\n"
        "Work out what is already done by INSPECTING the tree (git status, "
        "git diff, the files themselves) rather than assuming — then continue "
        "from there. Do not start over, and do not redo work that is already "
        "applied."
    )


def run(run_key, prompt, root=None, timeout_s=DEFAULT_TIMEOUT_S, project=None,
        env=None, model=None, permission_mode=None,
        context_cfg=None, max_iterations=DEFAULT_MAX_ITERATIONS,
        guard=None, now_fn=time.monotonic) -> x_mod.RunResult:
    """Spawn a headless agent for *prompt*; answer an `executor.RunResult`.

    ## One shot, or a chain

    With ``context_cfg=None`` — every caller before the smart-zone gate existed
    — this is exactly the single spawn it always was, and the tests assert that
    verbatim. Given a `loop.context.ContextConfig` it becomes a CHAIN: each
    call resumes the previous session, and the gate runs between calls.

    That boundary is the whole point. A task used to be one `claude -p` capped
    at 1800s with no ``--resume``, so its context could be neither measured nor
    rotated — there was no point at which anything could be done about it. The
    chain gives the gate somewhere to stand, and it stands BETWEEN calls, never
    inside one: killing a child mid-edit is precisely the half-written-tree
    hazard the timeout path below already warns about.

    ## The budget is the task's, not the call's

    ``timeout_s`` bounds the WHOLE chain. Sizing it per-iteration instead would
    let a ten-iteration run park the machine for five hours under a cap that
    reads as thirty minutes.

    ## Rotation is not evidence

    A rotated run reports exactly what an unrotated one reports. Rotation
    changes where the work pauses; it says nothing about whether the work is
    done, and `RESULT_RAN_UNVERIFIED` remains the answer for a clean exit.

    The return type is `executor.RunResult` on purpose, not a shape of its own:
    the orchestrator classifies outcomes through `RESULT_STATUS` /
    `NON_EXECUTING_RESULTS` and neither has to learn a second vocabulary. That
    also means `executed` carries the same meaning here as there.

    *env* is a real overlay on the child's environment as of MN-9 — the
    orchestrator uses it to give each concurrent agent its own `MINDHIVE_SESSION`,
    because `events.append_event` appends to ``<session_id>.jsonl`` without a lock
    and two agents sharing a segment interleave into corrupt JSONL.
    `claude_cli._child_env` forces `MINDHIVE_HEADLESS` on after the overlay, so
    passing one can never defeat the recursion guard."""
    key = str(run_key)
    header = (f"agent run: {key}\n"
              f"cwd:       {root}\n"
              f"model:     {model or '(cli default)'}\n"
              f"cap:       {timeout_s}s\n"
              f"perm:      {permission_mode or '(cli default)'}\n"
              f"\n--- prompt ---\n{prompt}\n")

    # --- the chain ------------------------------------------------------------
    # `iterations` is 1 unless a context config was supplied, which keeps every
    # pre-existing caller on precisely the one-shot path they were written
    # against — asserted directly by
    # `test_without_a_context_cfg_the_run_stays_a_single_shot`.
    iterations = max(1, int(max_iterations)) if context_cfg is not None else 1
    if guard is None and context_cfg is not None:
        # PER RUN, never shared. The guard's job is to HALT, and a wave runs
        # agents concurrently in threads — one instance across the wave would
        # be shared mutable state whose failure mode is one task's rotations
        # stopping a different task that had done nothing wrong.
        from ..loop import safeguards as sg_mod
        guard = sg_mod.RotationGuard(sg_mod.RotationGuardConfig())
    session_id = None
    deadline = None
    rotated = False
    transcript = []
    out = None
    code = 0
    stdout = ""

    for iteration in range(1, iterations + 1):
        now = now_fn()
        if deadline is None:
            deadline = now + float(timeout_s)
            # The first call gets the configured cap VERBATIM, not
            # `deadline - now`. Those are algebraically identical and not
            # numerically identical: `time.monotonic()` can be large enough
            # that (now + 1800.0) - now loses sub-integer precision, and the
            # int() below then truncates 1799.999... to 1799. That shaves time
            # off every run, and it is invisible until a platform whose clock
            # happens to be big enough — CI caught it on windows/py3.12 while
            # three other jobs passed.
            remaining = float(timeout_s)
        else:
            remaining = deadline - now
        if iteration > 1 and remaining <= 0:
            # The task's own budget is spent. Stop rather than starting a call
            # that would run past the cap the operator set.
            break

        step_prompt = _iteration_prompt(prompt, iteration, rotated)
        rotated = False

        try:
            out = claude_cli.run_headless(
                # round(), not truncation: a budget derived from clock
                # arithmetic lands a hair under the whole second it means, and
                # truncating each hop compounds the loss across a chain.
                step_prompt, model=model, timeout_s=max(1, round(remaining)),
                permission_mode=permission_mode, env=env,
                cwd=str(root) if root else None, session_id=session_id)
        except Exception as exc:                        # noqa: BLE001
            return _spawn_raised(exc, key, header, transcript, root, project)

        code = int(out.get("exit", 1))
        stdout = out.get("stdout", "") or ""
        transcript.append(
            f"\n--- iteration {iteration} (session={session_id or 'new'}) ---\n"
            f"{stdout}\n--- exit {code} ---\n")

        # A terminal outcome ends the chain immediately and is reported exactly
        # as the one-shot path always reported it. Only a CLEAN, UNFINISHED
        # reply is allowed to continue.
        if not _is_clean_reply(stdout, code):
            break
        if _says_finished(stdout):
            break
        if iteration >= iterations:
            break

        occupancy, measurable = _occupancy_of(out)
        if measurable and ctx_needs_reset(occupancy, context_cfg, model):
            if guard is not None:
                if not guard.allow(now=now_fn()):
                    break
                guard.record(now=now_fn(), turns_in_session=iteration)
                if guard.state == "HALTED":
                    break
            session_id = None
            rotated = True
        else:
            session_id = out.get("session_id") or session_id

    body = header + "".join(transcript)
    artifact = _artifact(key, body, project)
    text = claude_cli.result_text(stdout)
    excerpt = (text or stdout)[:runlog.EXCERPT_CHARS]
    return _classify(code, stdout, timeout_s, key, root, artifact, excerpt)


def _spawn_raised(exc, key, header, transcript, root, project):
    """`run_headless` maps its own faults to exit codes, so reaching here means
    something unforeseen. Reported as never-started: the safe direction is a
    task that stays PENDING rather than one whose subtree is deleted."""
    detail = f"agent spawn raised {type(exc).__name__}: {exc}"
    return x_mod.RunResult(
        result="spawn-failed", reason=detail, exit_code=_EXIT_NOT_FOUND,
        executed=False, run_key=key, cwd=str(root or ""),
        artifact=_artifact(key, header + "".join(transcript) + "\n" + detail,
                           project))


def _occupancy_of(out) -> tuple:
    """``(occupancy, measurable)`` for one spawn's usage block.

    ``measurable`` is separate from a zero occupancy on purpose. `run_headless`
    answers ``usage: None`` whenever the envelope did not parse or reported
    ``is_error`` — and treating "we could not measure" as "carrying nothing"
    would suppress a rotation that may well be due."""
    usage = (out or {}).get("usage")
    if not isinstance(usage, dict):
        return 0, False
    from ..loop import context as ctx_mod
    return ctx_mod.occupancy_from(
        usage.get("input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0),
        usage.get("cache_read_input_tokens", 0)), True


def ctx_needs_reset(occupancy, cfg, model):
    """The smart-zone gate, imported lazily so this module keeps no hard
    dependency on the loop package at import time."""
    from ..loop import context as ctx_mod
    return ctx_mod.needs_reset(occupancy, cfg, model)


def _is_clean_reply(stdout: str, code: int) -> bool:
    """Whether this spawn is the one shape the chain may continue from: the CLI
    ran, the envelope parsed, it reported no error, and the process exited 0.
    Everything else is terminal and keeps its existing classification."""
    if not stdout:
        return False
    if claude_cli.error_text(stdout):
        return False
    if not claude_cli.envelope_ok(stdout):
        return False
    return code == 0


def _says_finished(stdout: str) -> bool:
    """Whether the agent signalled the whole task is done.

    Reuses `loop.contract.parse_status` rather than re-deriving the signal —
    invariant 10, and a second reading of EXIT_SIGNAL is how the two begin to
    disagree. An agent that says nothing is treated as UNFINISHED, so the
    absent-contract case ends the chain via the iteration cap rather than by
    silently declaring success."""
    from ..loop.contract import parse_status
    try:
        return parse_status(claude_cli.result_text(stdout)).exit_signal is True
    except Exception:                                   # noqa: BLE001
        return True         # unreadable contract: do not keep spawning


def _classify(code, stdout, timeout_s, key, root, artifact, excerpt):
    """The outcome mapping, unchanged from the one-shot era and deliberately
    factored out whole rather than rewritten: the ORDER of these checks is the
    correctness of this module, and it is not the chain's business."""

    def result(kind, reason, executed):
        return x_mod.RunResult(
            result=kind, reason=reason, exit_code=code, executed=executed,
            artifact=artifact, excerpt=excerpt, stdout=stdout[:runlog.EXCERPT_CHARS],
            run_key=key, cwd=str(root or ""))

    # THE ENVELOPE IS CHECKED BEFORE THE EXIT CODE, and the order is the whole
    # correctness of this function.
    #
    # `is_error` means the CLI ran and the MODEL never worked — auth, a usage
    # limit, a refused request. It is not a task failure, and recording one would
    # mark the task `blocked`, which removes its entire subtree from every future
    # frontier permanently (`tasks.ready`) with no CLI able to undo it.
    #
    # This ordering is not theoretical. Checking the exit code first was the first
    # implementation, and a real spawn produced `is_error: true` with
    # `"Not logged in"` at **exit 1** — so an unauthenticated or rate-limited
    # machine blocked every task it touched. The unit test had asserted the exit-0
    # variant, because `claude_cli`'s own docstring says a 401 "sets is_error even
    # on a zero exit"; "even on" is not "only on". Testing the shape that avoids
    # the bug, for the third time in this feature's history.
    err = claude_cli.error_text(stdout)
    if err:
        low = err.lower()
        # Marked when it is the ACCOUNT, not the task. The orchestrator brakes on
        # this: eight tasks against an exhausted weekly limit produce eight
        # identical failures, and the ninth tells nobody anything new.
        mark = (f"{USAGE_MARK} " if any(h in low for h in _USAGE_HINTS) else "")
        return result("refused",
                      f"{mark}the agent could not run: {err} — nothing was "
                      f"attempted, so the task is left untouched", False)

    if code == _EXIT_NOT_FOUND:
        return result("spawn-failed",
                      "the `claude` CLI is not installed or not on PATH, so no "
                      "agent could be started", False)
    if code == _EXIT_TIMEOUT:
        # It DID run. A timeout may have left work half-applied, which is a real
        # failure the operator has to look at — unlike the cases above.
        return result("timeout",
                      f"the agent exceeded its {timeout_s}s cap and was killed; "
                      f"the tree may hold partial work", True)
    if not claude_cli.envelope_ok(stdout):
        # No readable envelope at all: whether anything happened cannot be
        # established, so nothing is recorded either way.
        return result("refused",
                      "the agent returned no readable result envelope, so "
                      "whether it did anything cannot be established", False)
    if code != 0:
        return result("fail", f"the agent exited {code}", True)

    # Exit 0 means THE MODEL REPLIED. It does not mean the task is done.
    #
    # `RESULT_STATUS["ok"] -> STATUS_COMPLETED` was written for
    # `executor.execute`, where exit 0 is a VALIDATION COMMAND passing — real
    # evidence. Routing an implementer through the same mapping made an agent's
    # say-so sufficient to mark a task COMPLETED, and `prompt_for` explicitly
    # invites the failing reply ("If you cannot complete the task, say so
    # plainly"). That reply is exit 0 with a clean envelope, so a task nobody
    # worked on was written COMPLETED, its dependents unblocked, and the run
    # reported `settled`. Nothing downstream re-checks: `can_complete` has no
    # production caller and `fold` is last-write-wins.
    #
    # `ran-unverified` is therefore its own result: the agent ran (so this is not
    # `refused` — the transcript and any branch are real and worth looking at) but
    # nothing has verified the outcome, so the orchestrator writes no status row.
    # `hive mindnet validate` is what turns it into evidence.
    return result(RESULT_RAN_UNVERIFIED,
                  "the agent ran and replied, but nothing has verified that the "
                  "task is actually done — inspect the transcript or run "
                  "`hive mindnet validate`", True)
