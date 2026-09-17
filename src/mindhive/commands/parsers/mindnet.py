# SPDX-License-Identifier: Apache-2.0
"""argparse wiring for the `mindnet` group. The `brief` and `ab` verbs
register themselves onto the group's subparsers from their own modules.
"""
from ... import cliparse
from ..mindnet_orchestrate import cmd_mindnet_enforce, cmd_mindnet_orchestrate
from ..mindnet_satisfy import cmd_mindnet_satisfy
from ..mindnet_tasks import (cmd_mindnet_declare,
                             cmd_mindnet_enter,
                             cmd_mindnet_fall_back,
                             cmd_mindnet_restat,
                             cmd_mindnet_validate)
from ..mindnet_view import (cmd_mindnet_decompose,
                            cmd_mindnet_depend,
                            cmd_mindnet_list,
                            cmd_mindnet_rebuild,
                            cmd_mindnet_show)

__all__ = ["add_mindnet"]



def add_mindnet(sub) -> None:
    """Register `hive mindnet`."""
    # --- mindnet (MN-1: the deterministic phase spine over the task graph) ---
    # Mirrors the `roadmap` subparser above. Two deliberate omissions:
    #
    #   * no `--verified-by` anywhere (KTD2) — the value is derived by whatever
    #     check actually ran, and a flag would hand the untrusted party the
    #     field that says whether to trust them;
    #   * no argparse `choices=PHASES` on the phase arguments. `tasks.PHASES` is
    #     the ONE legend (invariant 10) and `controller.can_advance` already
    #     answers with it; a second copy in the parser would be a place for the
    #     legend to drift, and would swap a reason an operator can act on for
    #     argparse's bare "invalid choice".
    mn = sub.add_parser("mindnet", help="MindNetwork task graph + phase spine: "
                        "declare tasks, walk the 6-phase pipeline, and record "
                        "the evidence that earned each move")
    mnsub = mn.add_subparsers(dest="mindnet_cmd", required=True)

    mnd = mnsub.add_parser("declare", help="declare a task on BOTH axes (the "
                           "graph node and its phase record)")
    mnd.add_argument("task_id", help="stable kebab-case id, e.g. mn1-u7")
    mnd.add_argument("--title", required=True, help="the actionable ask")
    mnd.add_argument("--parent", default=None, help="parent task_id")
    mnd.add_argument("--description", default=None)
    mnd.add_argument("--validation-script", default=None,
                     dest="validation_script",
                     help="the command `hive mindnet validate` runs (argv, "
                          "never a shell)")
    mnd.add_argument("--language", action="append", default=None,
                     help="permitted language (repeatable; read by MN-4)")
    mnd.add_argument("--faculty", default=None, help="MN-5 cat feed label")
    mnd.add_argument("--triviality-bypass", action="store_true",
                     dest="triviality_bypass",
                     help="ADR-0005's one-line-fix bypass: born at 'execution' "
                          "and gated by the bug audit. FIRST DECLARE ONLY")
    mnd.add_argument("--project", type=cliparse.project_arg, default=None)
    mnd.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mnd.set_defaults(func=cmd_mindnet_declare)

    mne = mnsub.add_parser("enter", help="move a task to another phase "
                           "(forward moves are evidence-gated)")
    mne.add_argument("task_id")
    mne.add_argument("phase", help="target phase")
    mne.add_argument("--project", type=cliparse.project_arg, default=None)
    mne.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mne.set_defaults(func=cmd_mindnet_enter)

    mns = mnsub.add_parser("satisfy", help="record that the task's CURRENT "
                           "phase cleared its exit criterion")
    mns.add_argument("task_id")
    mns.add_argument("--ref", default=None,
                     help="worktree-root-relative document path (file phases)")
    mns.add_argument("--detail", default=None,
                     help="one line of context; REQUIRED with "
                          "--agent-reported, where it carries the reason "
                          "verbatim")
    mns.add_argument("--agent-reported", action="store_true",
                     dest="agent_reported",
                     help="no Python check ran — record the weaker claim "
                          "honestly instead of stamping 'python'")
    mns.add_argument("--project", type=cliparse.project_arg, default=None)
    mns.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mns.set_defaults(func=cmd_mindnet_satisfy)

    mnf = mnsub.add_parser("fall-back", help="move a task backward and "
                           "invalidate every satisfaction at or past the "
                           "target (bumps the epoch)")
    mnf.add_argument("task_id")
    mnf.add_argument("phase", help="the phase to go back to")
    mnf.add_argument("--detail", required=True,
                     help="what failed — the one transition that exists to "
                          "record a failure may not record nothing about it")
    mnf.add_argument("--project", type=cliparse.project_arg, default=None)
    mnf.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mnf.set_defaults(func=cmd_mindnet_fall_back)

    mnv = mnsub.add_parser("validate", help="run the task's validation script "
                           "behind the fail-closed gate and record the result "
                           "as python-verified evidence")
    mnv.add_argument("task_id")
    mnv.add_argument("--script", default=None,
                     help="override the stored validation_script (a one-off "
                          "re-check)")
    mnv.add_argument("--timeout", type=int, default=None,
                     help="seconds the run may take (default: 600)")
    mnv.add_argument("--project", type=cliparse.project_arg, default=None)
    mnv.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mnv.set_defaults(func=cmd_mindnet_validate)

    mnr = mnsub.add_parser("restat", help="re-check the file evidence behind "
                           "the current phase (a failure marks it stale; it "
                           "never moves the task)")
    mnr.add_argument("task_id")
    mnr.add_argument("--project", type=cliparse.project_arg, default=None)
    mnr.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mnr.set_defaults(func=cmd_mindnet_restat)

    mnw = mnsub.add_parser("show", help="one task in full: phase, evidence "
                           "with its verifier tier, refusals, orphans")
    mnw.add_argument("task_id")
    mnw.add_argument("--project", type=cliparse.project_arg, default=None)
    mnw.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mnw.add_argument("--json", action="store_true")
    mnw.set_defaults(func=cmd_mindnet_show)

    mnl = mnsub.add_parser("list", help="every task with its phase and status")
    mnl.add_argument("--all", action="store_true",
                     help="include completed and abandoned tasks")
    mnl.add_argument("--project", type=cliparse.project_arg, default=None)
    mnl.add_argument("--json", action="store_true")
    mnl.set_defaults(func=cmd_mindnet_list)

    mnb = mnsub.add_parser("rebuild", help="re-derive the phase sidecar from "
                           "the ledger (the repair for a stale or corrupt one)")
    mnb.add_argument("--project", type=cliparse.project_arg, default=None)
    mnb.set_defaults(func=cmd_mindnet_rebuild)

    mndc = mnsub.add_parser("decompose", help="ask an agent what subtasks a task "
                            "actually requires (a DRY RUN unless --execute)")
    mndc.add_argument("task_id", help="the task to break down")
    mndc.add_argument("--execute", action="store_true",
                      help="actually declare the proposed subtasks")
    mndc.add_argument("--timeout", type=int, default=None,
                      help="seconds the agent may take (default: 300)")
    mndc.add_argument("--project", type=cliparse.project_arg, default=None)
    mndc.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mndc.add_argument("--json", action="store_true")
    mndc.set_defaults(func=cmd_mindnet_decompose)

    mndep = mnsub.add_parser("depend", help="record that one task cannot start "
                             "until another is completed (the edge the "
                             "orchestrator walks)")
    mndep.add_argument("task_id", help="the task that has to wait")
    mndep.add_argument("on", help="the task_id it waits for")
    mndep.add_argument("--project", type=cliparse.project_arg, default=None)
    mndep.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mndep.set_defaults(func=cmd_mindnet_depend)

    # MN-7's orchestrator. No `choices=` on --limit/--max-waves for the same
    # reason the phase arguments carry none: the bound belongs to the
    # orchestrator, and a second copy in the parser is a place for it to drift.
    mno = mnsub.add_parser("orchestrate", help="walk the graph in waves, "
                           "running every unblocked task concurrently (a DRY "
                           "RUN unless --execute is given; kept, but no "
                           "longer the front door)")
    mno.add_argument("--execute", action="store_true",
                     help="actually run the tasks. Without this the command "
                          "plans the waves and starts nothing")
    mno.add_argument("--limit", type=int, default=None,
                     help="most tasks to take from the frontier per wave — which "
                          "tasks run AT ALL this wave (default: all of them). "
                          "For a concurrency brake use --max-workers")
    mno.add_argument("--max-waves", type=int, default=None, dest="max_waves",
                     help="runaway backstop (default: "
                          "orchestrator.MAX_WAVES)")
    mno.add_argument("--max-workers", type=int, default=None,
                     dest="max_workers",
                     help="ceiling on CONCURRENT children, independent of how "
                          "wide the frontier is (default: cpu count). Distinct "
                          "from --limit, which decides which tasks run at all")
    mno.add_argument("--timeout", type=int, default=None,
                     help="seconds any one task may take (default: 1800 for "
                          "agents, 600 for --via-script)")
    mno.add_argument("--via-script", action="store_true", dest="via_script",
                     help="run each task's stored validation_script instead of "
                          "spawning an agent. This only RE-CHECKS tasks; it does "
                          "not do their work")
    mno.add_argument("--isolation", choices=("none", "worktree"), default=None,
                     help="`worktree` (the DEFAULT for agent runs) gives each agent "
                          "its own git worktree and ledger segment, so a wave runs "
                          "in PARALLEL. `none` runs them one at a time in the "
                          "current checkout — they would corrupt each other's "
                          "edits otherwise, so it is the unsafe mode, not the safe "
                          "one")
    mno.add_argument("--allow-shared-tree", action="store_true",
                     dest="allow_shared_tree",
                     help="let agents run concurrently in ONE working tree. They "
                          "will collide on each other's edits and the git index "
                          "— without this, agents run one at a time")
    mno.add_argument("--project", type=cliparse.project_arg, default=None)
    mno.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mno.add_argument("--json", action="store_true")
    mno.set_defaults(func=cmd_mindnet_orchestrate)

    # Self-registering, the same trade `bench` makes; see briefcli's docstring.
    from ...mindnet import briefcli as briefcli_mod
    briefcli_mod.register(mnsub)
    from ...mindnet import ab as mn_ab_mod
    mn_ab_mod.register(mnsub)

    # MN-6's blast-radius gate. Bare = status; the two directions are mutually
    # exclusive because `--enable --disable` has no defensible reading and
    # silently picking one is how a toggle for a repo-freezing rule goes wrong.
    mnx = mnsub.add_parser("enforce", help="read or toggle whether MN-4's "
                           "PreToolUse teeth may deny anything in THIS "
                           "checkout (opt-in; dormant by default)")
    mnxg = mnx.add_mutually_exclusive_group()
    mnxg.add_argument("--enable", action="store_true",
                      help="arm enforcement for this checkout root")
    mnxg.add_argument("--disable", action="store_true",
                      help="disarm it for this checkout root — recorded, so it "
                           "also out-ranks an armed parent checkout")
    mnx.add_argument("--project", type=cliparse.project_arg, default=None)
    mnx.add_argument("--session", type=cliparse.session_id_arg, default=None)
    mnx.add_argument("--json", action="store_true")
    mnx.set_defaults(func=cmd_mindnet_enforce)
