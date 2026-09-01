# src/mindhive/orchestrator/__init__.py
"""The lintester mission pipeline — one intent in, one `Mission` out.

## Why this package exists at all

`docs/designs/lintester-orchestrator.md` lists this package under "Explicitly
NOT in scope", with the reason "Six of its proposed modules have shipped
incumbents." That reason was correct and it still is. The owner overrode the
decision on 2026-09-01 after a conformance sweep found 22 of the original PRD's
27 items open, and the override is recorded in that same design doc rather than
left to be rediscovered from a diff.

What the override does NOT do is discard the objection. Every module here
delegates to the incumbent that already owns its work:

    metrics.py     -> clusters.file_complexity / git_churn, mindnet.telemetry
    queue.py       -> nothing; the scheduling layer had no incumbent
    payoff.py      -> clusters.shard_plan, and a faithful model of the
                      incumbent mindnet.orchestrator's own wave loop; also
                      scores the weight itself against measured wall time
    symbols (used)  -> symbols.build_map / focus, for the per-unit slice
    worktree.py    -> mindnet.worktree.add / retire, which own the git calls
    resilience.py  -> loop.safeguards.CircuitBreaker, mindnet.controller
    lintester.py   -> mindnet.autodeclare / decompose / brief / orchestrator

Nothing here forks a shipped module, re-implements its state machine, or
bypasses it. A second git-worktree driver or a second breaker would be the
regression the original rejection was protecting against, and it would be a
worse outcome than not building the package at all.

Stdlib only. `pyproject.toml`'s empty `dependencies` list is deliberate and this
package does not change it.

Note on `__all__` below: it names `run_mission`, not the verb the PRD uses for
the same idea. Invariant 10 (`tests/test_mindnet_phases.py`) forbids a
phase-name synonym as a bare quoted string literal anywhere under `src/mindhive`
— and every entry in an `__all__` is exactly that. The method on
`LintesterOrchestrator` keeps the PRD's name, because a method name is an
identifier and never quoted.
"""
from __future__ import annotations

from .lintester import AgentTask, LintesterOrchestrator, Mission, run_mission
from .metrics import ComplexityVisitor, Weight, calculate_workload_weight
from .payoff import (Calibration, Payoff, Sample, Schedule, calibrate,
                     simulate_lanes, simulate_waves)
from .dashboard import (Board, Dashboard, EventBus, FallbackTriggered,
                        LaneView, TaskStarted, WorkStolen, attach,
                        render_plain)
from .queue import (ABANDONED, FAILED, QUEUED, RUNNING, VALIDATED, QueueEntry,
                    WorkerLane, WorkStealingScheduler)
from .resilience import (Limits, RepairLoop, SandboxResult, ToolCircuitBreaker,
                         run_sandboxed)
from .worktree import LaneWorktree, lane_path_for, rollback

__all__ = [
    "ABANDONED", "AgentTask", "Board", "Calibration", "ComplexityVisitor",
    "Dashboard", "EventBus", "FAILED", "FallbackTriggered", "LaneView",
    "LaneWorktree", "Limits", "LintesterOrchestrator", "Mission", "Payoff",
    "QUEUED", "QueueEntry", "RUNNING", "RepairLoop", "Sample", "SandboxResult",
    "Schedule", "TaskStarted", "ToolCircuitBreaker", "VALIDATED", "Weight",
    "WorkStealingScheduler", "WorkStolen", "WorkerLane", "attach",
    "calculate_workload_weight", "calibrate", "lane_path_for", "render_plain",
    "rollback", "run_mission", "run_sandboxed", "simulate_lanes",
    "simulate_waves",
]
