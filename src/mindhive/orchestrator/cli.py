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
import sys


def cmd_lintester(args) -> int | None:
    """Plan (and optionally run) one mission from one intent."""
    from .lintester import LintesterOrchestrator

    if getattr(args, "calibration", False):
        # Answered BEFORE the intent check: reading what past missions measured
        # is a question about the record, not a mission, and demanding an intent
        # to ask it would mean planning a mission nobody wanted in order to read
        # one line of history.
        return _print_calibration(args.project)

    intent = str(getattr(args, "intent", "") or "").strip()
    if not intent:
        print("lintester: an intent is required — try: "
              'hive lintester "add retry to the ledger writer"')
        return 2

    started = bool(args.execute)
    bus, observer, why = _observer(args, started)
    if why:
        # stderr, not stdout: `--json` is a machine surface and a friendly
        # note printed into it is a parse error at the other end.
        print(f"lintester: {why}", file=sys.stderr)
    mission = LintesterOrchestrator(
        root=args.root, project=args.project,
        lanes=args.lanes if args.lanes is not None else None,
        # The unattended half of the reflect/resolve loop, wired HERE and not in
        # `RepairLoop`. `reflect_on_open` runs the same `resolve.resolve_batch`
        # that `hive rules resolve` runs, which spends real agent tokens — so a
        # default buried in the library would make every suite that drives a
        # breaker open pay for it. The front door is the one place where "without
        # intervention" actually means something, because it is the one place a
        # person typed the command and walked away.
        #
        # Before this, `reflect_on_open` was called from nothing but its own
        # tests: the seam worked, the breaker opened, and no anomaly was ever
        # recorded, resolved or remembered on a real run.
        reflector=_reflector(),
        bus=bus, observer=observer,
    ).execute_mission(intent, dry_run=not started,
                      checkpoint_dir=args.checkpoint_dir)

    report = mission.as_dict()
    scored = _payoff(mission) if getattr(args, "payoff", False) else None
    if scored is not None:
        report["payoff"] = scored.as_dict()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if mission.outcome in ("ok", "dry-run") else 1

    _print(mission)
    if scored is not None:
        _print_payoff(scored)
    return 0 if mission.outcome in ("ok", "dry-run") else 1


def _observer(args, started):
    """``(bus, observer, why)`` for this invocation.

    *why* is a sentence for the operator when the board was ASKED for and
    is not going to appear. Silence there would be the worse failure: a
    flag that does nothing and says nothing teaches the operator that the
    feature is broken rather than that this run did not qualify.

    Two runs do not qualify. A dry run publishes a handful of phase lines
    and stops, so a full-screen live board would seize the terminal for a
    tenth of a second and take the plan's own report down with it. And
    `--json` owns stdout, which is where the board writes."""
    if not bool(getattr(args, "board", False)):
        return None, None, ""
    if getattr(args, "json", False):
        return None, None, ("the board is off — it writes to stdout, which "
                            "the JSON report owns")
    if not started:
        return None, None, ("the board is off — a plan publishes a few phase "
                            "lines and stops; add the flag that starts work "
                            "to watch lanes run")
    from . import dashboard as dash_mod

    bus = dash_mod.EventBus()
    available, reason = dash_mod.rich_available()
    return bus, (lambda: dash_mod.attach(bus)), ("" if available else reason)


def _reflector():
    """The reflect/resolve callback a real invocation gets.

    A function rather than a direct reference so the import stays lazy and a test
    can see exactly what the command wired."""
    from . import resilience as res_mod

    return res_mod.reflect_on_open


def _payoff(mission):
    """Score THIS mission's own units under both scheduling policies.

    The units a mission plans carry no dependencies — `WorkStealingScheduler`
    has no dependency model — so this measures the one thing that does differ:
    the incumbent takes its batch in sort order, the lanes pack heaviest-first.
    No `actual` is passed, so the durations are the estimates, and `payoff`'s
    own docstring records what that costs: a perfect estimate is the case where
    stealing can never fire."""
    from . import payoff as payoff_mod

    return payoff_mod.compare({t.task_id: int(t.weight) for t in mission.tasks},
                              lanes=max(1, len(mission.lanes)))


def _print_calibration(project=None) -> int:
    """Print every sample recorded so far, scored as one set."""
    from . import payoff as payoff_mod

    cal = payoff_mod.folded(project)
    print(f"weight calibration over {cal.units} recorded sample(s): "
          f"{cal.verdict}")
    for sample in cal.samples[-12:]:
        print(f"  {sample.unit}: weight {sample.weight} -> "
              f"{sample.ms:.3f} ms "
              f"({sample.ms_per_weight:.3f} ms per weight-unit)")
    if not cal.samples:
        print("  no mission has recorded a measured duration yet; a real run "
              "with --execute records one sample per unit")
    return 0


def _print_payoff(scored) -> None:
    print(f"  payoff over {scored.units} unit(s): {scored.verdict}")
    print(f"    static waves: makespan {scored.waves.makespan}, "
          f"idle {scored.waves.idle}, "
          f"{scored.waves.waves} wave(s) on {scored.waves.workers} worker(s)")
    print(f"    weighted lanes: makespan {scored.lanes.makespan}, "
          f"idle {scored.lanes.idle}, {scored.lanes.steals} steal(s) "
          f"across {scored.lanes.workers} lane(s)")
    if not scored.comparable:
        print("    (not a like-for-like comparison — see the verdict)")


def _print(mission) -> None:
    head = f"mission {mission.mission_id}: {mission.outcome}"
    if mission.reason:
        head += f" — {mission.reason}"
    print(head)
    print(f"  fencing: {mission.enforcement}"
          + (f" ({mission.enforcement_reason})" if mission.enforcement_reason
             else ""))
    print(f"  {len(mission.tasks)} unit(s) across {len(mission.lanes)} lane(s)"
          f", split: {mission.decomposition}")
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
    for line in mission.rollbacks:
        print(f"  rollback: {line}")
    verdict = str((mission.calibration or {}).get("verdict") or "")
    if verdict:
        print(f"  weight against measured time: {verdict}")


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
    p.add_argument("--payoff", action="store_true",
                   help="also score this unit set under both scheduling "
                        "policies (TODOS 27b)")
    p.add_argument("--board", action="store_true",
                   help="watch the lanes live in the terminal (WB7). Needs a run that starts work; install the tui extra for the full board")
    p.add_argument("--calibration", action="store_true",
                   help="print what past runs measured — the weight against "
                        "the wall time it predicted — and start no mission")
    p.set_defaults(func=cmd_lintester)
