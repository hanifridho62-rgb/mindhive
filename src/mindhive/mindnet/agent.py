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

from .. import claude_cli, runlog
from . import executor as x_mod
from . import phases as ph_mod

# Agents do real work. `executor.DEFAULT_TIMEOUT_S` is 600s, sized for a
# validation command; a task implementation needs materially longer, and a cap
# that fires mid-edit leaves a half-written tree.
DEFAULT_TIMEOUT_S = 1800

# How many agents may share one working tree. One. See the module docstring.
MAX_SAFE_WORKERS = 1

# Kept verbatim from `claude_cli.run_headless`'s documented contract rather than
# re-derived: 127 is "CLI not installed", 124 is "timed out".
_EXIT_NOT_FOUND = 127
_EXIT_TIMEOUT = 124


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


def run(run_key, prompt, root=None, timeout_s=DEFAULT_TIMEOUT_S, project=None,
        env=None, model=None, permission_mode=None) -> x_mod.RunResult:
    """Spawn one headless agent for *prompt*; answer an `executor.RunResult`.

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

    try:
        out = claude_cli.run_headless(
            prompt, model=model, timeout_s=int(timeout_s),
            permission_mode=permission_mode, env=env,
            cwd=str(root) if root else None)
    except Exception as exc:                            # noqa: BLE001
        # `run_headless` maps its own faults to exit codes, so reaching here
        # means something unforeseen. Report it as never-started: the safe
        # direction is a task that stays PENDING.
        detail = f"agent spawn raised {type(exc).__name__}: {exc}"
        return x_mod.RunResult(
            result="spawn-failed", reason=detail, exit_code=_EXIT_NOT_FOUND,
            executed=False, run_key=key, cwd=str(root or ""),
            artifact=_artifact(key, header + "\n" + detail, project))

    code = int(out.get("exit", 1))
    stdout = out.get("stdout", "") or ""
    body = header + "\n--- stdout ---\n" + stdout + f"\n--- exit {code} ---\n"
    artifact = _artifact(key, body, project)
    text = claude_cli.result_text(stdout)
    excerpt = (text or stdout)[:runlog.EXCERPT_CHARS]

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
        return result("refused",
                      f"the agent could not run: {err} — nothing was attempted, "
                      f"so the task is left untouched", False)

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
    return result("ok", "", True)
