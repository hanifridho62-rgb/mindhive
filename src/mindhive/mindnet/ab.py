# src/mindhive/mindnet/ab.py
"""`hive mindnet ab` — run one task graph twice, brief OFF then ON, and compare.

## Why this exists

TODOS 23 is the gate on Branch 2 (the rolling frontier, the `hive lintester`
front door, the repair loop), and it says plainly what was missing: *"what is
missing is the harness that runs both arms and the decision about what counts as
better."* Branch 1 shipped the composer and the telemetry proving a brief
ARRIVES; neither can show it HELPS. The owner shipped `brief.enabled()` default
ON conditional on that gate being written down, so the composer is running for
every orchestrated wave right now on an unmeasured assumption.

This module is the harness half. The decision half is below, in `verdict_for`,
written here rather than left to whoever reads the numbers — because a metric
chosen after seeing the result is not a measurement.

## The decision about what counts as better

**Primary metric: validated completions.** A task counts only if it passed the
completion gate — `orchestrator.completion_gate_default` -> `tasks.can_complete`.
Not "the agent finished", not "the run returned": the only outcome this system
verifies rather than reports. TODOS 23 offers this or "agent turn count to first
evidence"; turn count is not recorded per task anywhere in the ledger (the
`loop_iteration` payload carries no task id), so it cannot be computed without
building a second instrument first.

**Zero against zero is INCONCLUSIVE, never neutral.** This is the half that
matters and it is not a formality. On the agent path `agent.run`'s success result
is `ran-unverified`, which `_run_wave` routes to `skipped` with no status row —
so a completely successful ten-task run scores ZERO validated completions in both
arms, and a naive comparison reports "no difference" from data that could not
have shown one. This project has already been bitten by exactly that shape once:
its own record names a scoring lens whose threshold sat above the base score, so
three conditions "could never land, silently". A dead metric that reports
`neutral` would close the Branch 2 gate on nothing at all, which is worse than
leaving it open. So `verdict_for` refuses to grade an arm pair that never scored.

**A dry run is also inconclusive.** `orchestrate(execute=False)` never reaches
`_run_wave`; both arms come back `dry-run` with empty everything, and that is a
shape check, not a measurement.

## What this harness does NOT settle

It measures a ONE-WAVE brief. TODOS 23's red-team note establishes that on the
agent success path the wave loop can only run once — `before == after`, the loop
breaks — so `brief.wave_context`'s stated justification for resolving per wave
("wave N would be briefed without wave N-1's failures") describes a property the
termination predicate forbids in the successful case. Whatever this reports is a
statement about the first wave. Reading it as a verdict on multi-wave briefing
would be reading it for more than it says.
"""
from __future__ import annotations

import contextlib
import copy
import os

from .. import cliparse
from dataclasses import dataclass, field

from . import brief as br_mod
from . import orchestrator as orc_mod

# The two arms, in the order they run. OFF first, deliberately: the ON arm is the
# one under suspicion, and running the control first means a harness that dies
# half way still leaves the baseline measured rather than only the flattering half.
ARMS = ("off", "on")

METRIC = "validated completions"

# Verdicts. `inconclusive` is not a failure of the harness — it is the correct
# answer whenever the metric could not have discriminated, and it is the one this
# module exists to be able to say.
HELPS, HURTS, NEUTRAL, INCONCLUSIVE = "helps", "hurts", "neutral", "inconclusive"
VERDICTS = (HELPS, HURTS, NEUTRAL, INCONCLUSIVE)


@dataclass(frozen=True)
class Arm:
    """One arm's result. Counts, not the `Orchestration` — the comparison is over
    numbers, and holding the whole run object invites reading a field the verdict
    did not consider."""

    name: str
    brief_on: bool
    outcome: str = ""
    completed: tuple = ()
    blocked: tuple = ()
    skipped: tuple = ()

    @property
    def score(self) -> int:
        """The primary metric. See the module docstring."""
        return len(self.completed)


@dataclass(frozen=True)
class Trial:
    """Both arms and the graded comparison."""

    arms: tuple = ()
    metric: str = METRIC
    verdict: str = INCONCLUSIVE
    reason: str = ""
    delta: int = 0
    notes: tuple = field(default_factory=tuple)

    def arm(self, name):
        for a in self.arms:
            if a.name == name:
                return a
        return None


@contextlib.contextmanager
def switched(on: bool):
    """`brief.ENV_SWITCH` forced for the block, and RESTORED after — including
    restoring its ABSENCE, which is not the same as setting it to the default.

    An arm that leaked its setting would make every later command in the same
    process run under the last arm's switch, and the ON arm runs second."""
    key = br_mod.ENV_SWITCH
    had = key in os.environ
    prev = os.environ.get(key)
    os.environ[key] = "1" if on else "0"
    try:
        yield
    finally:
        if had:
            os.environ[key] = prev
        else:
            os.environ.pop(key, None)


def verdict_for(off, on):
    """``(verdict, reason, delta)`` for one arm pair.

    Separated from `run` so the grading can be tested without orchestrating
    anything, and so the rule is readable in one screen rather than inferred from
    a run."""
    if (off.outcome == orc_mod.OUTCOME_DRY_RUN
            or on.outcome == orc_mod.OUTCOME_DRY_RUN):
        return (INCONCLUSIVE,
                "a dry run measures nothing: `orchestrate(execute=False)` never "
                "reaches the wave, so neither arm ran an agent", 0)
    delta = on.score - off.score
    if off.score == 0 and on.score == 0:
        return (INCONCLUSIVE,
                "no task passed the completion gate in EITHER arm, so "
                + METRIC + " could not have told them apart. On the agent path "
                "`ran-unverified` routes to `skipped` with no status row — a "
                "fully successful run scores zero here. Grade this with a metric "
                "that can move, or run it against a graph whose tasks reach "
                "validation", 0)
    if delta > 0:
        return (HELPS, f"the briefed arm validated {delta} more task(s) "
                       f"({on.score} vs {off.score})", delta)
    if delta < 0:
        return (HURTS, f"the briefed arm validated {-delta} fewer task(s) "
                       f"({on.score} vs {off.score})", delta)
    return (NEUTRAL, f"both arms validated {on.score} task(s); the brief made no "
                     f"difference to {METRIC}", 0)


def run(graph, runner=None, execute=False, **kwargs) -> Trial:
    """Orchestrate *graph* once per arm and grade the pair.

    Each arm gets a DEEP COPY of *graph*. That is not defensive tidiness: statuses
    are applied to the graph in place, so the second arm would otherwise start
    from a board the first arm had already advanced and would be measured against
    work it did not do. It is the one way this harness could silently produce a
    confident wrong number, so it is done here rather than asked of the caller."""
    arms = []
    for name in ARMS:
        on = name == "on"
        with switched(on):
            kw = dict(kwargs)
            if runner is not None:
                kw["runner"] = runner
            out = orc_mod.orchestrate(copy.deepcopy(graph), execute=execute, **kw)
        arms.append(Arm(name=name, brief_on=on, outcome=out.outcome,
                        completed=tuple(out.completed),
                        blocked=tuple(out.blocked),
                        skipped=tuple(out.skipped)))

    off, on_arm = arms[0], arms[1]
    verdict, reason, delta = verdict_for(off, on_arm)
    notes = []
    if execute:
        notes.append("one wave only — see this module's docstring on why a "
                     "successful agent wave never runs a second")
    return Trial(arms=tuple(arms), verdict=verdict, reason=reason, delta=delta,
                 notes=tuple(notes))


def record(trial, project=None) -> None:
    """Append the trial as an `experiment_result` — the repo's own semantic type.

    Written through `events.append_event`, never by hand: the ledger is
    append-only and hash-chained. Only called for a trial that actually executed;
    recording a dry run would put a measurement-shaped row in the ledger that
    measured nothing, and `hive recall` cannot tell those apart later."""
    from .. import events as ev_mod

    ev_mod.append_event(
        "experiment_result",
        {"experiment": "mindnet-brief-ab",
         "metric": trial.metric,
         "verdict": trial.verdict,
         "reason": trial.reason,
         "delta": trial.delta,
         "arms": [{"name": a.name, "brief_on": a.brief_on,
                   "outcome": a.outcome, "score": a.score,
                   "completed": list(a.completed),
                   "blocked": list(a.blocked),
                   "skipped": list(a.skipped)} for a in trial.arms]},
        project=project)


# --- CLI -----------------------------------------------------------------------
#
# Self-registering on the `mindnet` subparser, the trade `bench`, `briefcli` and
# `clusterscli` already take: `__main__.py` is under a shrink-only ratchet, so an
# inline argparse block here would cost it ~30 lines it does not have.

REFUSED = 2


def register(subparsers) -> None:
    """Wire `ab` onto the `mindnet` subparser set."""
    p = subparsers.add_parser(
        "ab", help="run the task graph twice, brief off then on, and compare "
                   "(TODOS 23: the gate on Branch 2)")
    p.add_argument("--execute", action="store_true",
                   help="actually run agents. WITHOUT this both arms are a dry "
                        "run and the verdict is inconclusive by construction")
    p.add_argument("--project", type=cliparse.project_arg, default=None,
                   help="the project namespace to read (default: the active one)")
    p.add_argument("--limit", type=int, default=None,
                   help="at most this many tasks per wave")
    p.add_argument("--record", action="store_true",
                   help="append the trial to the ledger as an experiment_result "
                        "(ignored for a dry run, which measured nothing)")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="machine-readable output")
    p.set_defaults(func=cmd_ab)


def cmd_ab(args) -> int | None:
    """Run both arms and print the graded comparison."""
    import json

    from .. import events as ev_mod
    from . import tasks as t_mod

    project = getattr(args, "project", None)
    graph = t_mod.fold(ev_mod.read_types(("mindnet_task",), project))
    if not graph:
        print("mindnet ab: no tasks on the graph — `hive mindnet list` shows "
              "what is there", file=__import__("sys").stderr)
        return REFUSED

    # Read the flag off args directly rather than through a getattr with a bare
    # string name: invariant 10 (tests/test_mindnet_phases.py) forbids phase-name
    # synonyms as string literals anywhere under src/mindhive, and this comment
    # may not spell one either. argparse always sets a store_true dest, so the
    # getattr default was unreachable in any case.
    execute = bool(args.execute)
    trial = run(graph, execute=execute, project=project,
                limit=getattr(args, "limit", None))

    if getattr(args, "record", False) and execute:
        record(trial, project=project)

    if getattr(args, "as_json", False):
        print(json.dumps({
            "metric": trial.metric, "verdict": trial.verdict,
            "reason": trial.reason, "delta": trial.delta,
            "notes": list(trial.notes),
            "arms": {a.name: {"brief_on": a.brief_on, "outcome": a.outcome,
                              "score": a.score, "completed": list(a.completed),
                              "blocked": list(a.blocked),
                              "skipped": list(a.skipped)}
                     for a in trial.arms},
        }, indent=2, ensure_ascii=False, default=str))
        return None

    print(f"mindnet ab: {trial.verdict.upper()}  (metric: {trial.metric})")
    for a in trial.arms:
        print(f"  brief {a.name:<3}  score={a.score}  outcome={a.outcome}  "
              f"completed={len(a.completed)} blocked={len(a.blocked)} "
              f"skipped={len(a.skipped)}")
    print(f"  {trial.reason}")
    for n in trial.notes:
        print(f"  note: {n}")
    if getattr(args, "record", False) and not execute:
        print("  not recorded: a dry run measured nothing")
    return None
