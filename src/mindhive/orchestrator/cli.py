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
import os
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
    if started:
        # Asked BEFORE a lane is made and before the board is built: a machine
        # whose CLI is not logged in can start no child, and every lane it
        # opened would only be torn down again to say so. The run path halts
        # on the same fact if this probe is inconclusive -- see `environment`.
        kind, detail = _preflight()
        if kind:
            return _refuse_environment(args, kind, detail)
    bus, observer, why, board, board_stream = _observer(args, started)
    if why:
        # stderr, not stdout: `--json` is a machine surface and a friendly
        # note printed into it is a parse error at the other end.
        print(f"lintester: {why}", file=sys.stderr)
    mission = LintesterOrchestrator(
        # The flag's help has always said "default: cwd" and the parser has
        # always passed None, which is not a softer default but a crash:
        # `worktree.lane_path_for` builds its lane path OUTSIDE the guard that
        # turns a git fault into a reason, so ``Path(None)`` raised TypeError
        # after the intent had been decomposed and every unit weighed. Resolved
        # here rather than in the parser so a caller that passes root=None into
        # the library keeps whatever behaviour the library gives it.
        root=args.root or os.getcwd(), project=args.project,
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

    _close_board(bus, board, board_stream)
    report = mission.as_dict()
    scored = _payoff(mission) if getattr(args, "payoff", False) else None
    if scored is not None:
        report["payoff"] = scored.as_dict()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return _status(mission.outcome)

    _print(mission)
    if scored is not None:
        _print_payoff(scored)
    return _status(mission.outcome)


# The machine refused, not the mission: the same status the preflight answers,
# and distinct from `1` so a wrapper can tell "some units failed" from "nothing
# could start here" without parsing the report.
ENVIRONMENT_STATUS = 3


def _status(outcome) -> int:
    if outcome in ("ok", "dry-run"):
        return 0
    return ENVIRONMENT_STATUS if outcome == "environment" else 1


def _preflight():
    """`environment.preflight`, behind a seam a test can replace."""
    from . import environment as env_mod

    return env_mod.preflight()


def _refuse_environment(args, kind, detail) -> int:
    """Say what is wrong with the machine and name the fix, then stop.

    The sentence goes to stderr whether or not `--json` was asked for, and the
    JSON surface gets a report of the same shape a mission would print, so a
    caller parsing stdout sees an `environment` outcome rather than nothing."""
    from . import environment as env_mod

    reason = env_mod.describe(kind, detail)
    print(f"lintester: refused before any lane was made -- {reason}",
          file=sys.stderr)
    if getattr(args, "json", False):
        print(json.dumps({"outcome": "environment", "environment": kind,
                          "reason": reason, "dry_run": False,
                          "tasks": [], "lanes": [], "left": []},
                         indent=2, sort_keys=True))
    return ENVIRONMENT_STATUS


def _close_board(bus, board, stream=None) -> None:
    """Render the board one last time, after the mission has returned.

    The live view is torn down with the lane phase, but the mission is not
    finished there: it merges the worktrees and decides its outcome AFTER,
    and publishes both — onto a bus whose only subscriber has already gone.
    Those events sit in the bus's backlog, and this is what collects them.

    Which makes the closing frame the one that persists. `Live` leaves
    nothing behind when it exits; a summary the operator can still read after
    the mission ends is worth more than the frame that scrolled past."""
    if bus is None or board is None:
        return
    from . import dashboard as dash_mod

    # The closing frame follows the live one. With `--json` the board ran on
    # stderr, and printing its summary to stdout would put a human paragraph
    # into the machine surface -- a parse error at the other end, which is the
    # whole reason the two were held apart in the first place.
    out = stream if stream is not None else sys.stdout
    try:
        for event in bus.drain():
            board.apply(event)
        print(dash_mod.render_plain(board, dash_mod.icons_for(out)), file=out)
        # The bus counts what it could not deliver and what it did not
        # recognise, and until now nothing ever read either back: the process
        # exited holding both. Printed only when non-zero — a permanent `0`
        # trains the operator to skim the one line that matters on the day it
        # is not zero, which is the argument `hive mindnet brief` already makes
        # for `failed=` / `dropped=` / `withheld=`.
        if bus.dropped or bus.unknown or bus.unknown_dropped:
            print(f"  board: {bus.dropped} event(s) dropped"
                  + (f", unrecognised {', '.join(bus.unknown)}"
                     if bus.unknown else "")
                  + (f" (+{bus.unknown_dropped} more kind(s) not recorded)"
                     if bus.unknown_dropped else ""), file=out)
    except Exception as exc:                            # noqa: BLE001
        # The same bargain the board keeps everywhere else: a display is
        # derived state, and losing it must never cost the report that
        # follows it.
        print(f"lintester: the closing board could not be drawn — {exc}",
              file=sys.stderr)


def _observer(args, started):
    """``(bus, observer, why, board)`` for this invocation.

    *why* is a sentence for the operator when the board was ASKED for and
    is not going to appear. Silence there would be the worse failure: a
    flag that does nothing and says nothing teaches the operator that the
    feature is broken rather than that this run did not qualify.

    Two runs do not qualify. A dry run publishes a handful of phase lines
    and stops, so a full-screen live board would seize the terminal for a
    tenth of a second and take the plan's own report down with it. And
    `--json` owns stdout, which is where the board writes."""
    if not bool(getattr(args, "board", False)):
        return None, None, "", None, None
    # `--json` no longer switches the board OFF. It only moves it: `Dashboard`
    # already takes `stream=` and `console=`, so the exclusivity was never about
    # the two features conflicting, only about stdout being hardcoded. The JSON
    # report keeps stdout whole -- which is the requirement -- and the operator
    # watching a long run still gets lanes, on stderr.
    to_stderr = bool(getattr(args, "json", False))
    if not started:
        return None, None, ("the board is off — a plan publishes a few phase "
                            "lines and stops; add the flag that starts work "
                            "to watch lanes run"), None, None
    from . import dashboard as dash_mod

    bus = dash_mod.EventBus()
    stream = sys.stderr if to_stderr else sys.stdout
    # The board is built HERE rather than left to `attach`, because this
    # function's caller needs it after the mission returns — see `_close_board`.
    board = dash_mod.Board()
    available, reason = dash_mod.rich_available()
    return (bus,
            (lambda: dash_mod.attach(bus, board=board, stream=stream,
                                     console=_console_for(stream))),
            ("" if available else reason), board, stream)


def _console_for(stream):
    """A `rich` console bound to *stream*, or ``None`` to let `Live` choose.

    ``None`` for stdout keeps the existing behaviour exactly -- `Live` builds
    its own console and this function changes nothing for the common case. Only
    the stderr run needs a console told where to write, and it is built here so
    a machine with no `rich` still reaches the plain path instead of an
    ImportError from the front door."""
    if stream is sys.stdout:
        return None
    try:
        from rich.console import Console
    except ImportError:                                 # pragma: no cover - no rich
        return None
    return Console(file=stream)


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
    if mission.decomposition_reason:
        print(f"      because: {mission.decomposition_reason}")
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
    if mission.environment:
        print(f"  environment: {mission.environment}")
    if mission.left:
        print(f"  never started: {', '.join(mission.left)}")
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
