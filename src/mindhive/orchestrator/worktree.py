# src/mindhive/orchestrator/worktree.py
"""Lane-scoped physical isolation: `.worktrees/lane-N`, one per lane, for life.

## What this adds, and what it refuses to add

`mindnet/worktree.py` already owns every git invocation, the never-raise
contract its module docstring states, the `_GIT_LOCK` that serialises `add`
against a global `prune`, and the two rules that make committed work survive a
teardown (refuse to reset a branch holding commits; count ignored files as
work). None of that is re-implemented here. A second git-worktree driver that
drifted from those rules would delete an agent's output and look correct doing
it.

What this module contributes is two things the incumbent has no opinion about:

* **the naming the PRD specifies.** `mindnet.worktree.path_for` keys by task id
  (`.claude/worktrees/mn9-<task>`); the PRD asks for `.worktrees/lane-N`. Both
  are now reachable, because `add` takes an optional explicit path and branch.
* **lifecycle binding.** A lane acquires its tree before its first dispatch and
  releases it after its last, so no task ever runs outside its own lane's tree.
  Per-task trees would mean a `git worktree add` per task rather than per lane —
  for a 12-task mission on 3 lanes that is 12 checkouts instead of 3, and each
  one costs a full working-tree materialisation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# `.worktrees` at the repo root, exactly as the PRD spells it. Not
# `.claude/worktrees`, which is the incumbent's directory and holds task-keyed
# trees a lane teardown must never walk into.
LANE_DIR = ".worktrees"
LANE_PREFIX = "lane-"

# Branch namespace for lane checkouts. Distinct from the incumbent's `mn9/` so a
# `git branch --list` says which subsystem made a leftover.
BRANCH_PREFIX = "lint"


def lane_name(lane_id) -> str:
    return f"{LANE_PREFIX}{int(lane_id)}"


def lane_path_for(root, lane_id) -> Path:
    return Path(root) / LANE_DIR / lane_name(lane_id)


def lane_branch_for(lane_id, mission_id="") -> str:
    mission = str(mission_id or "").strip()
    if mission:
        return f"{BRANCH_PREFIX}/{mission}/{lane_name(lane_id)}"
    return f"{BRANCH_PREFIX}/{lane_name(lane_id)}"


@dataclass(frozen=True)
class LaneWorktree:
    """One lane's checkout, or the reason it has none.

    Mirrors `mindnet.worktree.Isolated` field for field, including `retryable` —
    the caller's bounded retry has to be able to tell git-was-busy from
    this-is-settled, and losing that distinction here would make a permanent
    refusal look like a flake worth hammering."""

    lane_id: int = -1
    path: str = ""
    branch: str = ""
    base: str = ""
    ok: bool = False
    reason: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class Released:
    """What `release` did. `kept=True` means work is still on disk."""

    lane_id: int = -1
    removed: bool = False
    kept: bool = False
    reason: str = ""


def acquire(root, lane_id, mission_id="") -> LaneWorktree:
    """Create `.worktrees/lane-N` for *lane_id*, or answer why not.

    Delegates wholly to `mindnet.worktree.add`, passing the lane path and branch
    explicitly. Never raises — an import failure or a git fault comes back as
    `ok=False` with a reason, because the caller is a mission that has other
    lanes to run."""
    try:
        from ..mindnet import worktree as mn_wt
    except Exception as exc:                            # noqa: BLE001
        return LaneWorktree(lane_id=int(lane_id), ok=False,
                            reason=f"cannot load the worktree driver: "
                                   f"{type(exc).__name__}: {exc}")
    target = lane_path_for(root, lane_id)
    branch = lane_branch_for(lane_id, mission_id)
    try:
        iso = mn_wt.add(root, lane_name(lane_id), path=target, branch=branch)
    except Exception as exc:                            # noqa: BLE001
        return LaneWorktree(lane_id=int(lane_id), path=str(target),
                            branch=branch, ok=False,
                            reason=f"{type(exc).__name__}: {exc}")
    return LaneWorktree(lane_id=int(lane_id), path=str(getattr(iso, "path", "")
                                                       or target),
                        branch=str(getattr(iso, "branch", "") or branch),
                        base=str(getattr(iso, "base", "") or ""),
                        ok=bool(getattr(iso, "ok", False)),
                        reason=str(getattr(iso, "reason", "") or ""),
                        retryable=bool(getattr(iso, "retryable", False)))


def release(root, lane_worktree) -> Released:
    """Remove a lane's checkout — unless it holds work nobody has looked at.

    The keep-versus-remove judgement is entirely `mindnet.worktree.retire`'s,
    including its rule that ignored files count as work. Re-deriving that here is
    how an agent's output under `dist/` gets deleted."""
    lane_id = int(getattr(lane_worktree, "lane_id", -1))
    path = str(getattr(lane_worktree, "path", "") or "")
    if not path:
        return Released(lane_id=lane_id, reason="no path to release")
    try:
        from ..mindnet import worktree as mn_wt
        ret = mn_wt.retire(root, path,
                           branch=str(getattr(lane_worktree, "branch", "") or ""),
                           base=str(getattr(lane_worktree, "base", "") or ""))
    except Exception as exc:                            # noqa: BLE001
        return Released(lane_id=lane_id, kept=True,
                        reason=f"{type(exc).__name__}: {exc}")
    return Released(lane_id=lane_id, removed=bool(getattr(ret, "removed", False)),
                    kept=bool(getattr(ret, "kept", False)),
                    reason=str(getattr(ret, "reason", "") or ""))


def acquire_lanes(root, count, mission_id="") -> list:
    """One `LaneWorktree` per lane, in lane order.

    Failures are returned, not raised and not skipped: a mission that got three
    of four trees should run three lanes and SAY the fourth is missing, and a
    silently shortened list makes that indistinguishable from a mission that only
    ever wanted three."""
    return [acquire(root, i, mission_id) for i in range(max(0, int(count or 0)))]


def release_lanes(root, lane_worktrees) -> list:
    """Release every lane tree. Total — one failure does not skip the rest."""
    out = []
    for lw in lane_worktrees or ():
        if not getattr(lw, "ok", False):
            continue
        out.append(release(root, lw))
    return out
