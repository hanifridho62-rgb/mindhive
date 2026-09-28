# SPDX-License-Identifier: Apache-2.0
# src/mindhive/orchestrator/cli.py
"""`hive lintester "<intent>"` — the PRD's single user-facing instruction.

Self-registering, the trade `bench`, `briefcli`, `clusterscli` and `ab` all
make: the parser module (`src/mindhive/__main__.py` under a shrink-only ratchet
when this was written, `commands/parsers/__init__.py` since the P9 split, capped
at 500 lines by `tests/test_commands_reexport.py`) pays two lines, and the
argparse wiring and the command body live here.

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

import contextlib
import json
import os
import sys

from .. import cliparse
from . import environment as env_mod


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
    ledger = _ledger(args)
    orch = LintesterOrchestrator(
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
        validator=_validator(args, started),
        # The merge-back, on for every run that starts work: a mission whose
        # validated work reaches no branch has produced nothing a person can
        # pick up. `--no-gate` keeps it on, and its help says what that costs.
        merge_back=started,
        # The ceiling, off unless asked for. Zero on both is `Limits.empty`,
        # which `fence.py` never wraps a spawn for at all -- so the default run
        # makes exactly the call it always made. `hive lintester` is the one
        # place a person types a number for this; the library takes a `Limits`.
        limits=_limits(args),
        bus=bus, observer=observer,
    )
    # The lane budget, wired HERE and nowhere else (K21). `lintester.py` is
    # zero-edit, so the guard goes on through the `runner` field the
    # orchestrator already injects: `_run_child` is what it would have used, so
    # wrapping it changes nothing for a run with no ceiling -- `guard` returns
    # the runner unchanged when there is no quota, exactly as `_limits`/`_spawn`
    # leave an unfenced run alone. Before the gate rather than after it: the
    # gate is wrapped around whatever `runner` holds, so the order on a real
    # unit is gate -> quota -> child, and a refusal costs no child at all.
    if ledger is not None:
        orch.runner = ledger.guard(orch._run_child)
    with _tests_locked(args, started):
        mission = orch.execute_mission(intent, dry_run=not started,
                                       checkpoint_dir=args.checkpoint_dir)

    _close_board(bus, board, board_stream)
    _record_quota(ledger, args.project)
    report = mission.as_dict()
    if ledger is not None:
        report["lane_quota"] = _quota_report(ledger)
    scored = _payoff(mission) if getattr(args, "payoff", False) else None
    if scored is not None:
        report["payoff"] = scored.as_dict()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return _status(mission.outcome)

    _print(mission)
    _print_quota(ledger)
    if scored is not None:
        _print_payoff(scored)
    return _status(mission.outcome)


# The machine refused, not the mission: the same status the preflight answers,
# and distinct from `1` so a wrapper can tell "some units failed" from "nothing
# could start here" without parsing the report. Bound to the gate's own name,
# never retyped: two literals that agree today are two authorities tomorrow.
ENVIRONMENT_STATUS = env_mod.EXIT_ENVIRONMENT_REFUSAL


def _status(outcome) -> int:
    if outcome in ("ok", "dry-run"):
        return 0
    return ENVIRONMENT_STATUS if outcome == "environment" else 1


def _preflight():
    """`environment.preflight`, behind a seam a test can replace."""
    return env_mod.preflight()


def _refuse_environment(args, kind, detail) -> int:
    """Say what is wrong with the machine and name the fix, then stop.

    The sentence goes to stderr whether or not `--json` was asked for, and the
    JSON surface gets a report of the same shape a mission would print, so a
    caller parsing stdout sees an `environment` outcome rather than nothing.
    The line is the gate's own `refusal_line`, so it opens with
    `FATAL_REFUSAL_FRAME` and a wrapper finds this refusal and the gate's by
    one anchored string. Why this door keeps its own writer rather than
    calling `enforce_preflight_gate` is in `environment`'s docstring."""
    reason = env_mod.describe(kind, detail)
    print(env_mod.refusal_line("lintester",
                               f"refused before any lane was made -- {reason}"),
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


@contextlib.contextmanager
def _tests_locked(args, started):
    """Hold `worktree.LOCK_TESTS` for the length of one mission (K13).

    A module flag rather than an argument because `lintester.py` sits between
    this front door and `acquire_lanes`, and this blueprint does not edit it.
    Restored in a `finally` so a mission that raises does not leave the policy
    switched on for whatever runs next in this process.

    It only ever adds the chmod deterrent. The check that decides is
    `gate.tests_mutated`, which `_validator` carries, and switching this off
    does not switch that off."""
    from . import worktree as wt_mod

    want = bool(started) and bool(getattr(args, "immutable_tests", True)) \
        and not bool(getattr(args, "allow_test_edits", False))
    before = wt_mod.LOCK_TESTS
    wt_mod.LOCK_TESTS = want
    try:
        yield want
    finally:
        wt_mod.LOCK_TESTS = before


def _validator(args, started):
    """The per-unit test gate a run that starts work gets, or ``None``.

    ``None`` for a plan — nothing runs, nothing to judge — and for a run that
    opted out. The opt-out is a flag and its help text says what it costs:
    without the gate a unit is validated on its child's exit code alone, which
    is the claim the gate exists to stop making. The gate's root is the
    mission's, so its lane guard and the lanes agree on where the checkout is."""
    if not started or bool(getattr(args, "no_gate", False)):
        return None
    from . import gate as gate_mod

    return gate_mod.SuiteGate(
        root=args.root, project=args.project,
        allow_untested=bool(getattr(args, "allow_untested", False)),
        immutable_tests=bool(getattr(args, "immutable_tests", True)),
        allow_test_edits=bool(getattr(args, "allow_test_edits", False)))


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


def _print_overrides(gates) -> None:
    """One warning line for the units nothing judged.

    A green mission whose units all passed under `--allow-untested` reads
    exactly like a green mission that was tested, and the per-unit lines say
    `untested_override` in a place nobody counts. This is the count."""
    from . import gate as gate_mod

    n = gate_mod.count_scopes(gates).get(gate_mod.UNTESTED_OVERRIDE, 0)
    if n:
        print(f"  warning: {n} untested override(s) — no test judged that "
              f"work; --allow-untested passed it")


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
    for row in mission.gates:
        print(f"  gate {row.get('task_id')} (attempt {row.get('attempt')}): "
              f"{'ok' if row.get('ok') else 'RED'} [{row.get('scope')}] "
              f"{len(row.get('targets') or ())} test file(s)"
              + (f" — {row['reason']}" if row.get("reason") else ""))
    _print_overrides(mission.gates)
    for row in mission.commits:
        if not row.get("ok"):
            what = f"FAILED — {row.get('reason') or 'no reason recorded'}"
        elif not row.get("changed"):
            what = "nothing to commit"
        else:
            what = str(row.get("sha") or "")[:10]
        print(f"  commit {row.get('task_id')} (lane {row.get('lane_id')}): {what}"
              + (f"; {row['logged']}" if row.get("logged") else ""))
    integ = mission.integration or {}
    if integ.get("branch"):
        print(f"  integration: {integ['branch']} @ "
              f"{str(integ.get('sha') or '')[:10]} "
              f"({len(integ.get('merged') or [])} lane(s) merged, "
              f"{len(integ.get('skipped') or [])} not)")
        for m in integ.get("skipped") or []:
            print(f"      lane {m.get('lane_id')} not merged: "
                  f"{m.get('reason') or 'no reason recorded'}")
        if integ.get("kept"):
            print(f"      {integ['kept']}")
        if integ.get("next"):
            print(f"      next: {integ['next']}")
    elif integ:
        print(f"  integration: {integ.get('reason') or 'nothing merged'}")
    for row in mission.suites:
        counts = row.get("counts") or {}
        where = ("integration" if int(row.get("lane_id", -1)) < 0
                 else f"lane {row.get('lane_id')}")
        print(f"  suite {where}: "
              f"{'ok' if row.get('ok') else 'RED'}"
              + (f" — {row['reason']}" if row.get("reason") else
                 f" — {counts.get('passed', 0)} passed"))
    if mission.environment:
        print(f"  environment: {mission.environment}")
    if mission.left:
        print(f"  never started: {', '.join(mission.left)}")
    verdict = str((mission.calibration or {}).get("verdict") or "")
    if verdict:
        print(f"  weight against measured time: {verdict}")


def _limits(args):
    """The per-child ceiling the flags asked for.

    Megabytes at the front door because that is the unit an operator thinks in;
    `resilience.Limits` takes bytes because that is what `setrlimit` and
    `JOBOBJECT_EXTENDED_LIMIT_INFORMATION.ProcessMemoryLimit` take. Negative is
    clamped to zero rather than refused: zero already means "no ceiling for this
    resource", so a typo costs the fence, not the mission."""
    from .resilience import Limits

    mb = max(0, int(getattr(args, "memory_mb", 0) or 0))
    cpu = max(0, int(getattr(args, "cpu_seconds", 0) or 0))
    return Limits(memory_bytes=mb * 1024 * 1024, cpu_seconds=cpu)


def _quota(args):
    """The per-lane ceiling the flags asked for, or ``None`` for no ceiling.

    ``None`` rather than an empty `LaneQuota` so the enforcement is off by
    construction, not by a branch someone can forget: `LaneLedger.guard`
    returns the runner it was handed and the report prints nothing. Negative is
    clamped to zero, the same choice `_limits` makes -- a typo costs the
    budget, not the mission. Zero is a REAL ceiling, and the strictest one: a
    lane may then spend nothing that is measured."""
    from .breaker import LaneQuota

    tokens = getattr(args, "max_lane_tokens", None)
    cost = getattr(args, "max_lane_cost", None)
    if tokens is None and cost is None:
        return None
    return LaneQuota(max_tokens=None if tokens is None else max(0, int(tokens)),
                     max_cost_usd=None if cost is None else max(0.0, float(cost)))


def _ledger(args):
    """The `LaneLedger` this run enforces with, or ``None``."""
    quota = _quota(args)
    if quota is None:
        return None
    from .breaker import LaneLedger

    return LaneLedger(quota)


def _quota_report(ledger) -> dict:
    """The `--json` shape for the lane budget. `measured` is the load-bearing
    key: with it False the totals are UNMEASURED, not zero (see `quota.py`)."""
    quota = ledger.quota
    return {
        "max_tokens": quota.max_tokens,
        "max_cost_usd": quota.max_cost_usd,
        "measured": ledger.measured(),
        "lanes": {str(lane): {"tokens": use.tokens, "cost_usd": use.cost_usd}
                  for lane, use in sorted(ledger.totals().items())},
        "refused": [{"lane_id": lane, "task_id": task, "reason": why}
                    for lane, task, why in ledger.refusals()],
    }


def _print_quota(ledger) -> None:
    """The lane budget, for a run that asked for one."""
    if ledger is None:
        return
    quota = ledger.quota
    ceiling = ", ".join(
        part for part in (
            f"{quota.max_tokens} token(s)" if quota.max_tokens is not None else "",
            f"${quota.max_cost_usd:g}" if quota.max_cost_usd is not None else "")
        if part)
    print(f"  lane quota: {ceiling}")
    if not ledger.measured():
        # Said plainly rather than printed as a confident zero: this runner
        # reports no spend, so nothing was refused and nothing could have been.
        print("      usage: unavailable — this runner reports no token or cost "
              "figure, so no lane was measured against the ceiling")
        return
    for lane, use in sorted(ledger.totals().items()):
        print(f"      lane {lane}: {use.tokens} token(s), ${use.cost_usd:g}")
    for lane, task, why in ledger.refusals():
        print(f"      refused {task or '(unnamed unit)'} on lane {lane}: {why}")


def _record_quota(ledger, project=None) -> None:
    """One anomaly per lane the budget refused, so `hive rules` sees it.

    `record_anomaly` is the existing `tool_failed` door the tool breaker
    already writes through -- a parallel record only this package could read
    would be the worse choice. Total: it swallows its own failures, and a run
    that refused nothing writes nothing."""
    if ledger is None:
        return
    lanes: dict = {}
    for lane, task, why in ledger.refusals():
        lanes.setdefault(lane, [why, []])[1].append(task or "(unnamed unit)")
    if not lanes:
        return
    from . import resilience as res_mod

    for lane, (why, tasks) in sorted(lanes.items()):
        res_mod.record_anomaly(
            f"lane-{lane}", "lane_quota_exceeded",
            detail=f"lane {lane} took no further unit: {why} "
                   f"({len(tasks)} unit(s) refused: {', '.join(tasks)})",
            project=project)


def register(subparsers) -> None:
    """Wire `lintester` onto the root subparser set built by
    `commands.parsers._build_parser`."""
    p = subparsers.add_parser(
        "lintester",
        help="one intent -> weighted lanes, isolated worktrees, briefed agents")
    p.add_argument("intent", nargs="?", default="",
                   help="what the mission should accomplish, in plain language")
    p.add_argument("--lanes", type=int, default=None,
                   help="parallel lanes (default: one per unit, clamped to [1, n])")
    p.add_argument("--root", default=None, help="repo root (default: cwd)")
    p.add_argument("--project", type=cliparse.project_arg, default=None, help="target namespace")
    p.add_argument("--checkpoint-dir", default=None,
                   help="where per-lane resume sidecars are written")
    p.add_argument("--json", action="store_true",
                   help="emit the mission report as JSON")
    p.add_argument("--execute", action="store_true",
                   help="actually start sub-agents (default: plan only)")
    p.add_argument("--no-gate", action="store_true",
                   help="skip the per-unit test gate and the closing full "
                        "suite; a unit is then validated on its child's exit "
                        "code alone, and the merge-back commits and merges "
                        "that unverified work. The lane's tests stay "
                        "read-only regardless: only --allow-test-edits or "
                        "--no-immutable-tests stands that down")
    p.add_argument("--allow-untested", action="store_true",
                   help="pass a unit whose change no test could name — a "
                        "README, a migration, a config file. It does NOT "
                        "excuse a new module: there the gate names the test "
                        "file to write, and writing it is the way through")
    p.add_argument("--allow-test-edits", action="store_true",
                   help="let a unit modify test files the lane was checked "
                        "out with. For a unit whose job really is the suite; "
                        "otherwise a red test has a second cure — change what "
                        "it demands — and a child under repair will find it")
    p.add_argument("--immutable-tests", dest="immutable_tests",
                   action="store_true", default=True,
                   help="refuse a unit that edited an existing test (default)")
    p.add_argument("--no-immutable-tests", dest="immutable_tests",
                   action="store_false",
                   help="switch that check off for the whole mission")
    p.add_argument("--memory-mb", type=int, default=0,
                   help="per-child memory ceiling in MB (0: none). Held by a "
                        "Windows job object or a POSIX rlimit; the report's "
                        "`fencing:` line says which, and says so honestly when "
                        "the machine could not hold it")
    p.add_argument("--cpu-seconds", type=int, default=0,
                   help="per-child CPU-time ceiling in seconds (0: none). "
                        "Distinct from the wall-clock timeout, which always "
                        "applies")
    p.add_argument("--max-lane-tokens", type=int, default=None,
                   help="ceiling on the tokens ONE lane may spend (MEB K21); "
                        "omitted means no ceiling")
    p.add_argument("--max-lane-cost", type=float, default=None,
                   help="ceiling in USD on what ONE lane may spend; omitted "
                        "means no ceiling. Both ceilings need a runner that "
                        "reports usage — the report says so when none did")
    p.add_argument("--payoff", action="store_true",
                   help="also score this unit set under both scheduling "
                        "policies (TODOS 27b)")
    p.add_argument("--board", action="store_true",
                   help="watch the lanes live in the terminal (WB7). Needs a run that starts work; install the tui extra for the full board")
    p.add_argument("--calibration", action="store_true",
                   help="print what past runs measured — the weight against "
                        "the wall time it predicted — and start no mission")
    p.set_defaults(func=cmd_lintester)
