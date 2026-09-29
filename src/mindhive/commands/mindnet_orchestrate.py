# SPDX-License-Identifier: Apache-2.0
"""The `mindnet orchestrate` wave runner and the `mindnet enforce` toggle.
`MINDNET_ORCHESTRATE_SIGNPOST` is the constant `orchestrate` prints when
it is not the front door; tests import it from `mindhive.__main__`.
"""
import json
import os
import sys
from .. import events, paths
from .mindnet_tasks import _mn_error

__all__ = ["MINDNET_ORCHESTRATE_SIGNPOST", "cmd_mindnet_orchestrate",
    "cmd_mindnet_enforce"]


# Printed by `cmd_mindnet_orchestrate` on a dry run. A module constant so the
# test can assert equality rather than a substring — the same argument
# `resilience.NO_LIMITS_REASON` records, and for the same reason: a substring
# assertion is what let two sites word one fact differently until CI caught them.
MINDNET_ORCHESTRATE_SIGNPOST = (
    "note: there are two orchestrators. This one walks the mindnet task graph "
    "you declared; `hive lintester \"<intent>\"` decomposes one sentence into "
    "its own mission and lanes. Neither reads the other's input, so pick by "
    "what you have.")

def cmd_mindnet_orchestrate(args) -> int | None:
    """Walk the task graph in waves, running every unblocked task concurrently.

    **Bare is a dry run.** `--execute` is the explicit opt-in, because this is
    the one command here that starts real child processes and the safe form must
    be the one an operator gets by accident.

    Reads the LEDGER rather than the sidecar, like `list` and for the same
    reason: this is not the hook path, and an orchestration that could disagree
    with `hive mindnet list` about the graph would be worse than a slow one.

    Exit 1 on `stalled`, `capped` and `awaiting-validation` — work remains and a
    zero exit would let a wrapping CI step pass while the graph is stuck, gave
    up, or holds agent output nothing has verified. See DATA_MODEL.md §4.6.

    **Soft-deprecated: what that means, and what it deliberately still does
    not say.** Every non-JSON run opens with the mindnet orchestrator's own
    `PRIMARY_DOOR_NOTICE` — new work goes through the mission pipeline, and
    this command is no longer where the machine is developed. It is NOT
    removed, disabled, or scheduled for either, and it does not say "use the
    other one instead": the two take DIFFERENT inputs — this one walks a task
    graph somebody declared, the other decomposes one intent sentence — so a
    redirect would send an operator with a declared graph to a command that
    cannot read it. That fact is what `MINDNET_ORCHESTRATE_SIGNPOST` says,
    after the notice, on a dry run. The notice's own wording lives in the
    mindnet module (the signpost's is right above this function) so that this
    paragraph carries no second copy of the other command's name: the ratchet
    counts every mention in this file, and prose drifts from wiring.

    Notice on every run, signpost on the dry run only, both on stderr — stdout
    is the report, and under `--json` neither is printed at all."""
    from dataclasses import asdict
    from ..mindnet import agent as ag_mod
    from ..mindnet import executor as x_mod
    from ..mindnet import orchestrator as orc_mod
    from ..mindnet import tasks as t_mod
    # Before ANY branch: the no-tasks return below is a newcomer's likeliest
    # run and it points them DEEPER into this door, so it must carry the notice.
    if not args.json:
        print(orc_mod.PRIMARY_DOOR_NOTICE, file=sys.stderr)
    project = getattr(args, "project", None)
    tasks = t_mod.fold(events.read_all(project))
    if not tasks:
        print('(no mindnet tasks to orchestrate — declare one: hive mindnet '
              'declare <id> --title "…")')
        return None

    # Two runners, paired explicitly with their payload builders because they take
    # DIFFERENT content in the same argument position. The agent is the default:
    # `validation_script` only ever re-checks a task, which is exactly why the
    # orchestrator could not do any actual work before this.
    workers = args.max_workers
    timeout = args.timeout
    # DEFAULT: isolate agent runs. Sharing one working tree is the DANGEROUS mode
    # — two agents editing it corrupt each other's edits and the git index — so
    # `none` cannot be what an operator gets by accident. It also delivers the
    # founding ask that parallel work happen "secara paksa": `--execute` alone now
    # fans out. `--via-script` re-checks a task in place and has nothing to
    # isolate, so it defaults the other way.
    isolation = args.isolation
    if isolation is None:
        isolation = "none" if args.via_script else "worktree"
    isolate = isolation == "worktree"
    if isolate and args.via_script:
        # A validation script re-checks a task in place; isolating it would run the
        # check against an empty branch and prove nothing. Refusing beats silently
        # ignoring the flag the operator typed.
        return _mn_error("--isolation worktree is for agent runs; a "
                         "--via-script run re-checks a task in place and has "
                         "nothing to isolate")
    runner_kwargs = None
    if args.via_script:
        runner, payload_for = x_mod.execute, orc_mod.validation_script_of
    else:
        runner, payload_for = ag_mod.run, ag_mod.prompt_for
        if timeout is None:
            timeout = ag_mod.DEFAULT_TIMEOUT_S
        # The smart-zone gate. Without this the agent runner stays the one-shot
        # it always was: it only chains — and therefore only rotates before the
        # dumb zone — when it is handed a context config. Built from the loop's
        # own config so ONE file governs the ceiling everywhere, rather than the
        # fleet growing a second, quietly-diverging copy of the number.
        #
        # No guard is passed: `agent.run` makes its own PER-RUN one. A single
        # guard shared across a wave is shared mutable state across threads, and
        # its whole job is to halt — so one task's rotations would stop another
        # task that had done nothing wrong.
        from pathlib import Path as _Path
        from ..loop import config as loop_cfg_mod
        from ..loop import context as ctx_mod
        _root = paths.worktree_root()
        _lc = loop_cfg_mod.load_config(
            _Path(_root or ".") / ".hive-loop" / "loop.toml")
        runner_kwargs = {
            "context_cfg": ctx_mod.ContextConfig(
                _lc.context_window, _lc.smart_zone_ratio, _lc.absolute_ceiling),
        }
        # With `--isolation worktree` each agent gets its OWN checkout and its own
        # ledger segment, so the shared-tree cap no longer applies and the pool is
        # free to fan out. Without it every agent runs with cwd=root — the same
        # tree — and two editing it concurrently corrupt each other's edits and
        # the git index.
        if isolate or args.allow_shared_tree:
            pass                                    # caller's --max-workers stands
        else:
            if workers is not None and workers > ag_mod.MAX_SAFE_WORKERS:
                print(f"note: --max-workers {workers} ignored — agents share one "
                      f"working tree, so they run one at a time. Use "
                      f"`--isolation worktree` for real parallelism, or "
                      f"--allow-shared-tree to let them collide.",
                      file=sys.stderr)
            workers = ag_mod.MAX_SAFE_WORKERS

    try:
        out = orc_mod.orchestrate(
            tasks, runner=runner, payload_for=payload_for,
            execute=bool(args.execute), limit=args.limit,
            # None lets the orchestrator derive the cap from the graph; passing
            # MAX_WAVES verbatim capped a legal long dependency chain.
            max_waves=args.max_waves,
            project=project, timeout_s=timeout,
            max_workers=workers, isolate=isolate,
            runner_kwargs=runner_kwargs)
    except Exception as exc:
        # An orchestrator fault is an operator-facing failure, not a stack trace.
        # `main()` only catches JSONDecodeError, so without this a ledger or
        # projection error surfaces as a raw traceback — and the two faults
        # `_persist` raises are precisely the ones carrying the instructions for
        # what to do next.
        return _mn_error(f"{type(exc).__name__}: {exc}")

    if args.json:
        print(json.dumps(asdict(out), indent=2, ensure_ascii=False,
                         default=str))
    else:
        verb = "would run" if not args.execute else "ran"
        print(f"mindnet orchestrate: {out.outcome} — {len(out.waves)} wave(s), "
              f"{verb} {sum(len(w.tasks) for w in out.waves)} task(s)")
        if not args.execute:
            print(MINDNET_ORCHESTRATE_SIGNPOST, file=sys.stderr)
        for w in out.waves:
            line = f"  wave {w.index + 1}: " + ", ".join(w.tasks)
            if args.execute:
                line += (f"   [ok: {len(w.completed)}, "
                         f"blocked: {len(w.blocked)}]")
            print(line)
        # Named WITH ITS REASON, not counted: an operator cannot act on
        # "3 blocked", and pointing them at `hive mindnet show` was worse than
        # useless while the status rows carried no reason at all.
        for label, ids in (("blocked", out.blocked), ("skipped", out.skipped)):
            if not ids:
                continue
            if label == "skipped":
                # The distinction is load-bearing: skipped tasks never ran and
                # keep their status, so they are fixable and re-runnable. Blocked
                # ones ran and failed.
                print("  skipped (nothing ran; still pending):")
            else:
                print("  blocked:")
            for tid in ids:
                why = out.reasons.get(tid) or "no reason recorded"
                print(f"    {tid}: {why}")
        if out.branches:
            print("  branches: " + ", ".join(
                f"{t}={b}" for t, b in sorted(out.branches.items())))
        if out.kept:
            # The one thing an operator MUST act on: a checkout `retire` refused to
            # delete because it still held work nobody has looked at.
            print("  worktrees kept (they hold work):")
            for tid, path in sorted(out.kept.items()):
                print(f"    {tid}: {path}")
        if not args.execute:
            print("  this was a dry run — add --execute to actually run it")
    # `dry-run` and `settled` are the successes. `awaiting-validation` is NOT one:
    # the agents ran, but nothing is verified until `hive mindnet validate` says so.
    return 1 if out.outcome in (orc_mod.OUTCOME_STALLED, orc_mod.OUTCOME_CAPPED,
                                orc_mod.OUTCOME_AWAITING_VALIDATION) else None

def cmd_mindnet_enforce(args) -> int | None:
    """Read or toggle MN-6's opt-in: whether the MN-4 policy checks may deny
    anything in THIS checkout.

    Bare, it is a status read — and the one place an operator sees
    `optin.DORMANT_NOTE`, so "why is nothing being enforced?" has an answer that
    does not require reading source.

    Two properties this handler must keep, both learned the hard way in MN-4:

    * **No ledger append.** `sidecar._staleness` counts every event, so a row
      written here without also rewriting the sidecar would read STALE and
      silently degrade U9's phase-aware injection. It also means disarming a
      frozen repository never depends on the ledger being writable.
    * **Reachable while the teeth are biting.** Every remedy MN-4 documented ran
      through `Bash`, which the phase block denied unconditionally — the kill
      switch was locked inside the room it opened. `hive` is exempt from the
      phase block, the language lock only inspects writes, and this writes via
      `paths.write_text_atomic` rather than a `Write` tool call, so all three
      rules are satisfied at once rather than each alone."""
    from ..mindnet import optin
    project = getattr(args, "project", None)
    if args.enable or args.disable:
        out = optin.set_enabled(
            bool(args.enable), project=project,
            session=getattr(args, "session", None)
            or os.environ.get("MINDHIVE_SESSION"))
        st = optin.status(project)
        if args.json:
            print(json.dumps({**st, "wrote": out}, indent=2))
            return None
        verb = "enabled" if args.enable else "disabled"
        print(f"mindnet enforce: {verb} for {out['root']} -> {out['path']}")
        # The recorded root and the EFFECTIVE answer can legitimately differ:
        # MINDNETWORK_ENFORCE out-ranks the file, and a deeper recorded root
        # out-ranks a shallower one. Saying only "enabled" there would be a lie
        # the operator finds out about at the next denial.
        if st["armed"] != bool(args.enable):
            print(f"  note: effective state here is "
                  f"{'armed' if st['armed'] else 'dormant'} "
                  f"({st['source']}) — {st['note']}")
        return None
    st = optin.status(project)
    if args.json:
        print(json.dumps(st, indent=2))
        return None
    print(f"mindnet enforce: {'armed' if st['armed'] else 'dormant'} "
          f"({st['source']})")
    print(f"  {st['note']}")
    # The remedy has to name the mechanism that is actually deciding. The
    # environment out-ranks the record, so suggesting `--enable` while
    # MINDNETWORK_ENFORCE=0 is holding the gate shut sends an operator to a flag
    # that will change the file and nothing else — the same class of
    # unactionable advice MN-4's original one-sentence refusal was.
    if st["source"] == "env":
        print(f"  {optin.ENV_VAR} is set in this process and out-ranks the "
              f"record — unset it to fall back to the per-checkout opt-in")
    elif not st["armed"]:
        print("  turn it on for this checkout: hive mindnet enforce --enable")
    else:
        print("  turn it off for this checkout: hive mindnet enforce --disable"
              f"  ·  or for this process only: {optin.ENV_VAR}=0")
    return None
