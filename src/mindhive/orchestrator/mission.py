# src/mindhive/orchestrator/mission.py
"""`Mission` -- what one `execute_mission` did, in full -- and the four words
for where its unit list came from.

Carved out of `lintester.py` at the size cap, the way `preview.py` was.
Nothing here runs anything: the report is a frozen record, and `as_dict` is
the one JSON shape `hive lintester --json` and the plain report both read, so
the two can never disagree about a field. Every name stays reachable as
`lintester.Mission` / `lintester.ROOT_ONLY` -- those were the names in use,
in the tests and at the front door, and `lintester.py` re-imports them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import resilience as res_mod

# Where a mission's unit list came from. Reported on `Mission.decomposition`,
# because the four are not the same claim and an operator reading a fan-out plan
# is entitled to know which one they are looking at: a real split proposed by an
# agent, a caller's own decomposer, a directory split this module derived from
# the intent's own file paths, or no split at all.
INJECTED = "injected"
AGENT = "agent"
PREVIEW = "local-preview"
ROOT_ONLY = "root-only"


@dataclass(frozen=True)
class Mission:
    """What one `execute_mission` did, in full.

    `outcome` is one of `ok`, `partial`, `refused`, `dry-run`, `environment`.
    `enforcement` is `resilience`'s answer about the sandbox, carried up so a
    report can say plainly whether the children were fenced by the kernel or
    only by a clock.

    `environment` is the kind behind an `environment` outcome -- the machine
    could not start a child, and `reason` names the fix -- and `left` is every
    unit the halt left unstarted. Neither is a failure of the unit named."""

    intent: str = ""
    mission_id: str = ""
    outcome: str = "refused"
    reason: str = ""
    tasks: tuple = ()
    lanes: tuple = ()
    completed: tuple = ()
    failed: tuple = ()
    kept: tuple = ()
    enforcement: str = res_mod.TIMEOUT_ONLY
    enforcement_reason: str = ""
    steals: int = 0
    reflections: tuple = ()
    # A target that failed and then passed. Reported separately from
    # `open_keys` because "it broke once and recovered" and "it is still broken"
    # are different facts, and a report carrying only the first is the one that
    # sends an operator hunting a fault that already cleared.
    recoveries: tuple = ()
    open_keys: tuple = ()
    dry_run: bool = True
    decomposition: str = ROOT_ONLY
    # Why `decomposition` is not what was asked for; empty when it is. See
    # `LintesterOrchestrator._subtasks`.
    decomposition_reason: str = ""
    # What each lane's checkout was rolled back to before a retry, and what was
    # refused. Empty on a dry run, which starts no lane and therefore discards
    # nothing.
    rollbacks: tuple = ()
    # The weight scored against the time the units really took — see
    # `payoff.calibrate`. Empty on a dry run: nothing ran, so nothing was timed,
    # and a zeroed report would read as a measurement that came back flat.
    calibration: dict = field(default_factory=dict)
    environment: str = ""
    left: tuple = ()
    # The per-unit gate rows and the closing full-suite rows, as dicts
    # (`gate.GateResult.as_dict`). Empty when no validator was wired, and on a
    # dry run, which judges nothing.
    gates: tuple = ()
    suites: tuple = ()
    # The merge-back: one row per lane commit (`integrate.LaneCommit`) and the
    # `integrate.Integration` report plus what became of its checkout. Empty
    # when `merge_back` is off, and on a dry run.
    commits: tuple = ()
    integration: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "intent": self.intent, "mission": self.mission_id,
            "outcome": self.outcome, "reason": self.reason,
            "environment": self.environment, "left": list(self.left),
            "dry_run": bool(self.dry_run),
            "decomposition": self.decomposition,
            "decomposition_reason": self.decomposition_reason,
            "enforcement": self.enforcement,
            "enforcement_reason": self.enforcement_reason,
            "steals": int(self.steals),
            "lanes": [{"lane": ln.get("lane"), "tasks": ln.get("tasks", []),
                       "worktree": ln.get("worktree", ""),
                       "weight": ln.get("weight", 0)} for ln in self.lanes],
            "tasks": [{"task_id": t.task_id, "title": t.title,
                       "weight": t.weight, "lane": t.lane_id,
                       "skills": list(t.must_use),
                       "brief_chars": len(t.brief_text)} for t in self.tasks],
            "completed": list(self.completed), "failed": list(self.failed),
            "kept": list(self.kept),
            "reflections": list(self.reflections),
            "recoveries": list(self.recoveries),
            "open_keys": list(self.open_keys),
            "rollbacks": list(self.rollbacks),
            "calibration": dict(self.calibration),
            "gates": [dict(g) for g in self.gates],
            "suites": [dict(s) for s in self.suites],
            "commits": [dict(c) for c in self.commits],
            "integration": dict(self.integration),
        }
