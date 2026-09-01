# src/mindhive/orchestrator/cli.py
"""`hive lintester "<intent>"` — the PRD's single user-facing instruction.

Self-registering, the trade `bench`, `briefcli`, `clusterscli` and `ab` all
make: `src/mindhive/__main__.py` sits under a shrink-only ratchet
(`tests/test_file_size_cap.py`), so the argparse wiring and the command body
live here and that file pays two lines.

Two things about this module are deliberate and easy to undo by accident.

**A dry run is the default.** A front door that fans real sub-agents at a
repository the moment somebody types it is a hazard wearing a convenience's
clothes. The flag to start work is explicit, and the report a dry run prints is
the same report a real run prints, so the flag is the only difference between
looking and doing.

**No phase-name synonym appears here as a bare quoted string literal**,
including inside comments and docstrings — invariant 10
(`tests/test_mindnet_phases.py`) scans this file as text, not as an AST. That is
why the start flag's value is read as a plain attribute off `args`: argparse
always sets a `store_true` destination, so a lookup by quoted name would be both
unnecessary and a violation.
"""
from __future__ import annotations

import json


def cmd_lintester(args) -> int | None:
    """Plan (and optionally run) one mission from one intent."""
    from .lintester import LintesterOrchestrator

    intent = str(getattr(args, "intent", "") or "").strip()
    if not intent:
        print("lintester: an intent is required — try: "
              'hive lintester "add retry to the ledger writer"')
        return 2

    started = bool(args.execute)
    mission = LintesterOrchestrator(
        root=args.root, project=args.project,
        lanes=args.lanes if args.lanes is not None else None,
    ).execute_mission(intent, dry_run=not started,
                      checkpoint_dir=args.checkpoint_dir)

    if args.json:
        print(json.dumps(mission.as_dict(), indent=2, sort_keys=True))
        return 0 if mission.outcome in ("ok", "dry-run") else 1

    _print(mission)
    return 0 if mission.outcome in ("ok", "dry-run") else 1


def _print(mission) -> None:
    head = f"mission {mission.mission_id}: {mission.outcome}"
    if mission.reason:
        head += f" — {mission.reason}"
    print(head)
    print(f"  fencing: {mission.enforcement}"
          + (f" ({mission.enforcement_reason})" if mission.enforcement_reason
             else ""))
    print(f"  {len(mission.tasks)} unit(s) across {len(mission.lanes)} lane(s)")
    for lane in mission.lanes:
        where = lane.get("worktree") or "(not isolated)"
        print(f"  lane {lane.get('lane')}: {len(lane.get('tasks', []))} unit(s) "
              f"[weight {lane.get('weight', 0)}] -> {where}")
        if not lane.get("ok", True) and lane.get("reason"):
            print(f"      refused: {lane['reason']}")
    for task in mission.tasks:
        skills = ", ".join(task.must_use) or "-"
        print(f"    - {task.task_id} [weight {task.weight}, lane "
              f"{task.lane_id}] brief {len(task.brief_text)} chars; "
              f"skills: {skills}")
    if mission.completed:
        print(f"  completed: {', '.join(mission.completed)}")
    for line in mission.failed:
        print(f"  FAILED {line}")
    for line in mission.kept:
        print(f"  kept: {line}")


def register(subparsers) -> None:
    """Wire `lintester` onto ``__main__``'s top-level subparser set."""
    p = subparsers.add_parser(
        "lintester",
        help="one intent -> weighted lanes, isolated worktrees, briefed agents")
    p.add_argument("intent", nargs="?", default="",
                   help="what the mission should accomplish, in plain language")
    p.add_argument("--lanes", type=int, default=None,
                   help="parallel lanes (default: one per unit, clamped to [1, n])")
    p.add_argument("--root", default=None, help="repo root (default: cwd)")
    p.add_argument("--project", default=None, help="target namespace")
    p.add_argument("--checkpoint-dir", default=None,
                   help="where per-lane resume sidecars are written")
    p.add_argument("--json", action="store_true",
                   help="emit the mission report as JSON")
    p.add_argument("--execute", action="store_true",
                   help="actually start sub-agents (default: plan only)")
    p.set_defaults(func=cmd_lintester)
