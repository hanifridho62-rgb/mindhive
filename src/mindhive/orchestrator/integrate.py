# SPDX-License-Identifier: Apache-2.0
# src/mindhive/orchestrator/integrate.py
"""The merge-back: each validated unit committed on its lane branch, the lanes
merged probe-first into `lint/<mission>/integration`, made from the lanes'
base. The branch is the mission's deliverable; the main checkout never moves.

## Why the orchestrator commits, and only past a green gate

Children are forbidden to commit (`mindnet/agent.py`): a headless agent that
could `git commit` could also `git push`, and the authorship control rests on
it not being able to. So the work a child leaves in its lane is committed HERE,
on its behalf, by `commit_lane` -- and `gate.gated` calls it only after the
child exited cleanly AND its targeted tests passed. A unit the gate refused is
rolled back by the repair loop instead of committed. But `rollback` keeps
commits on purpose, and nothing STOPS a child from committing, so a refused
unit's edit can be sitting on the lane branch when the merge comes -- under
the next unit's green gate. The branch alone does not say what was validated;
the stamp does.

A commit that fails is an integration fact, never the unit's failure. The gate
already proved the work; routing a failed `git commit` through the repair loop
would roll the lane back and DISCARD it. The row says what happened, the lane
stays on disk (`retire` keeps a dirty tree), and the mission is `partial` for
that reason.

## Only what the gate validated is delivered

`commit_lane` stamps every commit it makes with the `Lintester-Task` trailer
(`commit_message`), and `integrate_lanes` merges a lane only when EVERY commit
past the base on its branch carries it -- read with
`mindnet.worktree.commits_since`, git's own trailer parser, not a search over
messages. A lane holding a commit the orchestrator did not make is HELD:
reported in `skipped` with the foreign shas (`LaneMerge.foreign`), kept
whole, branch and tree, for a person, and nothing of it merged. Whole, because
the stamped commits above a foreign one were validated on top of it, and
delivering them without it is not what the gate saw either. The first version
of this module merged the lane's TIP, and a doctored test a child had
committed under a refused unit rode into the deliverable that way.

This holds even when the foreign commit is validated work a disobedient child
committed itself -- the gate green, `commit_lane` finding nothing left to
commit. Decided so, and pinned in the tests, because from the branch the merge
reads that commit is indistinguishable from a refused unit's: the gate's
verdict is process memory the merge is not handed (`lintester.py` is
zero-edit), and the branch is the only record it has. The cost is a held lane
the report names; the alternative was a rule a doctored test satisfies by
being committed BEFORE an honest unit. A lane git cannot list is held the
same way: unproven is not validated. Not caught, and said so: a child that
forges the trailer or rewrites a stamped commit. That is a hostile child, not
a disobedient one, and telling the two apart from a branch is not something a
merge policy can do.

## Probe first, then merge, then prove

Every lane goes through `mindnet.worktree.merge_probe` -- `git merge-tree`, in
the object database only -- before `merge_into` touches the integration
checkout. A conflicting lane is reported with its paths and kept, branch and
tree, for a person; the lanes after it still merge. After a merge the lane's
branch must be an ancestor of the integration branch (`is_ancestor` answering
exactly ``True``), or the row does not say `merged`.

## Made from base; the main checkout is never touched

The integration checkout is a worktree of its own, created from the lanes'
common base with `add(start_point=base)`, so the branch starts where the lanes
started even when the operator has moved `HEAD` since. `add`'s own rule refuses
to reset a leftover integration branch that holds commits. When the mission
ends the worktree is retired and the BRANCH survives: `git switch
lint/<mission>/integration` is the operator's next step, and the report names
it. Nothing is created when no lane committed anything, or when every lane
that did is held.

## The circuit

Merging is guarded by `breaker.IntegrationBreaker`. Three consecutive
integrations that did not fully land -- a held lane counts as not landed --
open it, and while it is open this module refuses BEFORE `add` makes a
checkout: no integration worktree, no merge, every candidate reported in
`skipped` with the reason, and a line naming the branch a person would switch
to. The lanes are untouched, so a refused mission is
`partial` -- work that ran and did not land -- not a crash. `lintester.py` is
zero-edit and passes no breaker, so one is built here for the namespace the
MISSION's own checkout belongs to: running uncircuited by default would make
the control decorative, and keying it on the process's cwd would let one
repository's bad day stop another's mission.

## One git driver

Every git command here goes through `mindnet.worktree`. This module spawns
nothing itself, and `tests/test_orchestrator_integrate.py` asserts that
structurally, by scanning this file's own text: a second driver drifting from
the incumbent's rules -- its lock, its timeout, its refusal to force -- is how
an agent's work gets deleted by code that looks correct.
"""
from __future__ import annotations

import contextlib
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ..mindnet import worktree as mn_wt
from . import worktree as wt_mod
from .breaker import IntegrationBreaker

INTEGRATION_NAME = "integration"
COMMIT_TRAILER = "Lintester-Task"

# The lane id a closing-suite row carries when it ran on the integration tree
# rather than in a lane. Negative because lanes count from 0.
INTEGRATION_LANE = -1


def integration_path_for(root, mission_id) -> Path:
    mission = str(mission_id or "").strip()
    name = f"{INTEGRATION_NAME}-{mission}" if mission else INTEGRATION_NAME
    return Path(root) / wt_mod.LANE_DIR / name


def integration_branch_for(mission_id) -> str:
    mission = str(mission_id or "").strip()
    if mission:
        return f"{wt_mod.BRANCH_PREFIX}/{mission}/{INTEGRATION_NAME}"
    return f"{wt_mod.BRANCH_PREFIX}/{INTEGRATION_NAME}"


class LaneCommit(NamedTuple):
    """One `commit_lane` outcome, as `Mission.commits` carries it. ``logged``
    is what became of the change-log entry; empty when none was due."""

    task_id: str = ""
    lane_id: int = -1
    ok: bool = False
    sha: str = ""
    changed: bool = False
    reason: str = ""
    logged: str = ""


class LaneMerge(NamedTuple):
    """One lane's fate at the integration branch. ``foreign`` names the
    commits past the base that `commit_lane` did not make, when that is what
    kept the lane out."""

    lane_id: int = -1
    branch: str = ""
    merged: bool = False
    sha: str = ""
    conflicts: tuple = ()
    reason: str = ""
    foreign: tuple = ()

    def as_dict(self) -> dict:
        row = self._asdict()
        row["conflicts"] = list(self.conflicts)
        row["foreign"] = list(self.foreign)
        return row


@dataclass(frozen=True)
class Integration:
    """What `integrate_lanes` did. ``ok`` means every lane that had committed
    anything is on the integration branch; ``path`` is the integration
    checkout while it exists, and empty when nothing was created."""

    ok: bool = False
    branch: str = ""
    sha: str = ""
    base: str = ""
    path: str = ""
    merged: tuple = ()
    skipped: tuple = ()
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": bool(self.ok), "branch": self.branch, "sha": self.sha,
            "base": self.base, "path": self.path,
            "merged": [m.as_dict() for m in self.merged],
            "skipped": [m.as_dict() for m in self.skipped],
            "reason": self.reason,
            "next": (f"git switch {self.branch}"
                     if self.branch and self.merged else ""),
        }


# --- the commit ------------------------------------------------------------------

def commit_message(mission_id, task) -> str:
    """The lane commit's message: a subject naming the mission and the unit, a
    trailer a `git log --grep` can find, and the co-author line every commit
    an agent shaped here carries. The trailer is also the merge's proof of
    provenance: `integrate_lanes` merges no commit past the base without it."""
    task_id = str(getattr(task, "task_id", "") or "")
    title = str(getattr(task, "title", "") or "").strip() or task_id
    return (f"lint({mission_id}) {task_id}: {title}\n\n"
            f"{COMMIT_TRAILER}: {task_id}\n"
            f"Co-Authored-By: Claude <noreply@anthropic.com>")


def _is_cache(rel) -> bool:
    parts = str(rel).replace("\\", "/").strip("/").split("/")
    return any(part in mn_wt.CACHE_DIRS for part in parts)


def _log_change(mission_id, task, files, project, session_id) -> str:
    """CLAUDE.md's mandatory change-log, for a change the orchestrator made on
    a child's behalf. Only in-scope files (`changelog.in_scope`) are recorded;
    a change-set with none is not an entry. Total: the ledger refusing is
    reported, never raised -- the commit it describes has already happened."""
    try:
        from .. import changelog
        scoped = [f for f in files or ()
                  if changelog.in_scope(f) and not _is_cache(f)]
        if not scoped:
            return "no in-scope file to log"
        title = str(getattr(task, "title", "") or "").strip()
        changelog.record("update", f"lintester {mission_id}: {title}".strip(),
                         files=scoped, session_id=session_id or None,
                         project=project)
        return f"logged {len(scoped)} file(s)"
    except Exception as exc:                            # noqa: BLE001
        return f"change-log entry failed: {type(exc).__name__}: {exc}"


def commit_lane(root, lane_path, task, mission_id, project=None,
                session_id="") -> LaneCommit:
    """Commit what the child left in *lane_path*, on the lane's branch.

    Called from `gate.gated`'s `after` seam, in an executor thread, only for a
    unit whose child exited cleanly and whose gate was green. Refuses any path
    that is not a lane checkout under *root* (`worktree.is_lane_path`) -- the
    main checkout above all. Total: answers a row, never raises, and a failure
    here is the row's, not the unit's."""
    task_id = str(getattr(task, "task_id", "") or "")
    lane_id = int(getattr(task, "lane_id", -1))
    path = str(lane_path or "")
    if not path or not wt_mod.is_lane_path(root, path):
        return LaneCommit(task_id=task_id, lane_id=lane_id, ok=False,
                          reason=(f"{path or '(no path)'} is not a "
                                  f"{wt_mod.LANE_DIR}/{wt_mod.LANE_PREFIX}N "
                                  f"checkout under {root}; committing nothing"))
    try:
        # What the child touched, read BEFORE the commit makes it history.
        changed = mn_wt.changed_files(path) or ()
        got = mn_wt.commit_all(path, commit_message(mission_id, task))
    except Exception as exc:                            # noqa: BLE001
        return LaneCommit(task_id=task_id, lane_id=lane_id, ok=False,
                          reason=f"{type(exc).__name__}: {exc}")
    logged = ""
    if got.ok and got.changed:
        logged = _log_change(mission_id, task, changed, project, session_id)
    return LaneCommit(task_id=task_id, lane_id=lane_id, ok=bool(got.ok),
                      sha=str(got.sha or ""), changed=bool(got.changed),
                      reason=str(got.reason or ""), logged=logged)


# --- the merge -------------------------------------------------------------------

def _say(emit, text) -> None:
    if emit is None:
        return
    try:
        emit(str(text))
    except Exception:                                   # noqa: BLE001
        pass


def _project_of(root):
    """The namespace *root* belongs to, or ``None`` to let `paths` decide.

    A *root* that is not a directory answers ``None``: inventing a namespace
    from a string that names nothing is worse than falling back to the default
    one, because the circuit would then be keyed on a name no other process
    would ever compute."""
    try:
        from .. import paths
        path = Path(str(root or ""))
        return paths.detect_project(path) if path.is_dir() else None
    except Exception:                                   # noqa: BLE001
        return None


def _breaker_of(breaker, root=None):
    """The circuit this run merges under. `integrate_lanes` has no `project`
    parameter -- its caller is zero-edit -- so with none passed it builds one
    itself rather than merging uncircuited.

    Keyed on the MISSION's checkout, not on the process's cwd.
    `detect_project(None)` reads `Path.cwd()`, which is wherever `hive
    lintester` happened to be invoked from: two missions in two checkouts would
    then share one circuit, and a mission that kept failing to merge could stop
    an unrelated one. An explicit `MINDHIVE_PROJECT` still wins, inside
    `detect_project`, exactly as it does everywhere else in the store."""
    if breaker is not None:
        return breaker
    try:
        return IntegrationBreaker(project=_project_of(root))
    except Exception:                                   # noqa: BLE001
        return None


def _breaker_refusal(breaker):
    """The verdict when the circuit refuses, and ``None`` when it does not.

    Total on purpose: a breaker that cannot read its own state must not stop
    the merge. The circuit exists to prevent an outage, not to become one."""
    if breaker is None:
        return None
    try:
        verdict = breaker.check()
    except Exception:                                   # noqa: BLE001
        return None
    return None if getattr(verdict, "allowed", True) else verdict


def _breaker_feed(breaker, ok, reason) -> None:
    """The outcome the circuit counts: an integration where every candidate
    landed is a success, one where any lane did not -- a held lane included
    -- is a failure. A run that merged nothing because no lane committed is
    neither, and never reaches here. Total, for the same reason as
    `_breaker_refusal`."""
    if breaker is None:
        return
    try:
        if ok:
            breaker.record_success()
        else:
            breaker.record_failure(str(reason or "the integration did not land"))
    except Exception:                                   # noqa: BLE001
        pass


def _merge_one(root, path, branch, lane_id, lane_branch, mission_id) -> LaneMerge:
    """One lane: probe, merge, prove -- under the incumbent's lock for the
    whole of it, so no other lane's `add` or `prune` interleaves. The lock is
    re-entrant; each primitive takes it again on its own."""
    lock = getattr(mn_wt, "_GIT_LOCK", None) or contextlib.nullcontext()
    with lock:
        probe = mn_wt.merge_probe(path, lane_branch)
        if probe.clean is None:
            return LaneMerge(lane_id, lane_branch,
                             reason=f"the probe could not run: {probe.reason}")
        if probe.clean is False:
            return LaneMerge(lane_id, lane_branch,
                             conflicts=tuple(probe.conflicts),
                             reason="conflicts with the integration tree: "
                                    + ", ".join(probe.conflicts))
        got = mn_wt.merge_into(path, lane_branch,
                               f"lint({mission_id}): merge lane {lane_id}")
        if not got.ok:
            return LaneMerge(lane_id, lane_branch,
                             reason=f"the merge failed: {got.reason}")
        proof = mn_wt.is_ancestor(root, lane_branch, branch)
    if proof is not True:
        return LaneMerge(lane_id, lane_branch, sha=got.sha,
                         reason=("merged, but the lane branch is not an "
                                 "ancestor of the integration branch"
                                 + ("" if proof is False
                                    else " (git could not say)")))
    return LaneMerge(lane_id, lane_branch, merged=True, sha=got.sha)


def _unmerged(rows) -> str:
    """The reason line: one clause per lane that did not land."""
    return "; ".join(f"lane {m.lane_id} was not merged ({m.reason})"
                     for m in rows)


def _held(root, base, tree):
    """The row that keeps a lane out of the merge, or ``None`` when every
    commit past *base* on its branch is one `commit_lane` stamped. A history
    git could not list holds the lane too: unproven is not validated, and a
    held lane loses nothing -- it is kept, branch and tree, as it stands."""
    lane_id = int(getattr(tree, "lane_id", -1))
    lane_branch = str(getattr(tree, "branch", "") or "")
    try:
        entries = mn_wt.commits_since(root, base, lane_branch, COMMIT_TRAILER)
    except Exception:                                   # noqa: BLE001
        entries = None
    if entries is None:
        return LaneMerge(lane_id, lane_branch,
                         reason=("git could not list the commits past the "
                                 "base, so none is known to be validated; "
                                 "the lane is kept as it is"))
    foreign = tuple(str(e.sha) for e in entries if not e.trailers)
    if not foreign:
        return None
    return LaneMerge(lane_id, lane_branch, foreign=foreign,
                     reason=(f"holds {len(foreign)} commit(s) the orchestrator "
                             "did not make: "
                             + ", ".join(sha[:12] for sha in foreign)
                             + "; kept whole for a person "
                             f"(git switch {lane_branch})"))


def integrate_lanes(root, trees, mission_id, emit=None, *,
                    breaker=None) -> Integration:
    """Merge every lane that committed anything into the integration branch.

    *trees* are the mission's `LaneWorktree`s; only the ones that were
    acquired (`ok`) count, and only those whose branch is ahead of the common
    base are candidates. A candidate is merged only when every commit past the
    base on its branch carries `commit_lane`'s stamp (`_held`); one holding a
    commit the orchestrator did not make is kept as it is and reported in
    ``skipped`` with the foreign shas. ``emit(text)`` gets one line per lane.
    Total: every outcome is a row in the answer, and a refusal names its
    reason.

    *breaker* is the `IntegrationBreaker` the merge runs under; with none the
    default project's own is used. An open or quarantined circuit refuses
    before any checkout is made."""
    breaker = _breaker_of(breaker, root)
    lanes = [t for t in trees or ()
             if getattr(t, "ok", False) and getattr(t, "path", "")]
    bases = {str(getattr(t, "base", "") or "") for t in lanes} - {""}
    if len(bases) > 1:
        return Integration(ok=False,
                           reason=("the lanes disagree about their base ("
                                   + ", ".join(sorted(bases))
                                   + "); merging nothing"))
    base = next(iter(bases), "")
    candidates = []
    for tree in lanes:
        if not base:
            break
        try:
            _dirty, ahead = mn_wt.state(tree.path, base)
        except Exception:                               # noqa: BLE001
            ahead = 0
        if ahead > 0:
            candidates.append(tree)
    if not candidates:
        return Integration(ok=True, base=base,
                           reason="no lane committed anything")

    branch = integration_branch_for(mission_id)
    target = integration_path_for(root, mission_id)
    refused = _breaker_refusal(breaker)
    if refused is not None:
        reason = (f"the integration breaker is {refused.state}: "
                  f"{refused.reason}")
        skipped = tuple(LaneMerge(int(getattr(t, "lane_id", -1)),
                                  str(getattr(t, "branch", "") or ""),
                                  reason="the integration breaker refused: "
                                         + str(refused.reason or refused.state))
                        for t in candidates)
        _say(emit, f"integration: refused -- {reason}")
        _say(emit, "integration: nothing was merged and every lane is kept; "
                   f"look at them with git switch {branch}")
        return Integration(ok=False, branch=branch, base=base,
                           skipped=skipped, reason=reason)
    held, proven = [], []
    for tree in candidates:
        row = _held(root, base, tree)
        if row is None:
            proven.append(tree)
            continue
        held.append(row)
        _say(emit, f"lane {row.lane_id}: not merged -- {row.reason}")
    if not proven:
        reason = _unmerged(held)
        _say(emit, "integration: nothing was merged; every lane is kept as it is")
        _breaker_feed(breaker, False, reason)
        return Integration(ok=False, branch=branch, base=base,
                           skipped=tuple(held), reason=reason)
    try:
        iso = mn_wt.add(root, INTEGRATION_NAME, path=target, branch=branch,
                        start_point=base)
    except Exception as exc:                            # noqa: BLE001
        iso = mn_wt.Isolated(task_id=INTEGRATION_NAME, path=str(target),
                             branch=branch, ok=False,
                             reason=f"{type(exc).__name__}: {exc}")
    if not iso.ok:
        reason = str(iso.reason or "the integration checkout could not be made")
        skipped = tuple(held) + tuple(
            LaneMerge(int(t.lane_id), str(t.branch),
                      reason="no integration checkout: " + reason)
            for t in proven)
        _say(emit, f"integration: refused -- {reason}")
        if held:
            reason += "; " + _unmerged(held)
        _breaker_feed(breaker, False, reason)
        return Integration(ok=False, branch=branch, base=base,
                           skipped=skipped, reason=reason)

    path = str(iso.path)
    merged, skipped = [], list(held)
    for tree in proven:
        lane_id = int(getattr(tree, "lane_id", -1))
        row = _merge_one(root, path, branch, lane_id,
                         str(getattr(tree, "branch", "") or ""), mission_id)
        (merged if row.merged else skipped).append(row)
        _say(emit, f"lane {lane_id}: "
                   + ("merged" if row.merged else f"not merged -- {row.reason}"))
    sha = mn_wt.rev_of(root, branch)
    reason = _unmerged(skipped)
    _breaker_feed(breaker, not skipped, reason)
    return Integration(ok=not skipped, branch=branch, sha=sha, base=base,
                       path=path, merged=tuple(merged), skipped=tuple(skipped),
                       reason=reason)


# --- the teardown ----------------------------------------------------------------

def merged_trees(trees, integration) -> list:
    """*trees* with every merged lane's base moved to the integration commit,
    so `worktree.release_lanes` finds nothing ahead of it and removes the
    lane. An unmerged lane keeps its base, stays ahead, and is kept -- branch
    and tree -- for a person."""
    done = {int(m.lane_id) for m in getattr(integration, "merged", ()) or ()
            if getattr(m, "merged", False)}
    sha = str(getattr(integration, "sha", "") or "")
    out = []
    for tree in trees or ():
        if sha and int(getattr(tree, "lane_id", -1)) in done:
            try:
                tree = dataclasses.replace(tree, base=sha)
            except Exception:                           # noqa: BLE001
                pass
        out.append(tree)
    return out


def retire_integration(root, integration):
    """Remove the integration checkout. Its branch survives -- `retire` never
    deletes branches -- and is the deliverable. ``None`` when nothing was
    created. A checkout the closing suite left dirty is kept and reported."""
    path = str(getattr(integration, "path", "") or "")
    if not path:
        return None
    try:
        return mn_wt.retire(root, path,
                            branch=str(getattr(integration, "branch", "") or ""),
                            base=str(getattr(integration, "sha", "") or ""))
    except Exception as exc:                            # noqa: BLE001
        return mn_wt.Retired(removed=False, kept=True,
                             reason=f"{type(exc).__name__}: {exc}")


def report(integration, retired=None) -> dict:
    """`Mission.integration`: the integration's own dict plus what became of
    its checkout. ``{}`` when the merge-back did not run."""
    if integration is None:
        return {}
    row = integration.as_dict()
    row["retired"] = bool(getattr(retired, "removed", False))
    row["kept"] = (str(getattr(retired, "reason", "") or "")
                   if getattr(retired, "kept", False) else "")
    return row
