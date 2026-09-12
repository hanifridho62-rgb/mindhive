# src/mindhive/orchestrator/lintester.py
"""`LintesterOrchestrator.execute_mission(intent)` — the single public entry.

One instruction in, one `Mission` out. Everything between is automatic, and
every step of it is a shipped module doing the job it already owned:

    intent
      -> autodeclare.headline / derive_id      name the root task
      -> decompose.run                         subtasks (needs an agent)
      -> metrics.calculate_workload_weight     quantitative weight per unit
      -> clusters.shard_plan                   weighted, ownership-safe lanes
      -> orchestrator.worktree.acquire_lanes   .worktrees/lane-N, one per lane
      -> brief.wave_context / brief.build      each task's OWN material
      -> queue.WorkStealingScheduler           LIFO local, FIFO steal
      -> gate.SuiteGate.check                  each unit judged by its own tests
      -> resilience.RepairLoop                 broken, rolled back, repaired
      -> integrate.commit_lane                 each validated unit committed, in its lane
      -> integrate.integrate_lanes             lanes merged, probe-first, into one branch
      -> gate.SuiteGate.full                   the whole suite, once, on that branch's tree
      -> worktree.release_lanes                teardown; a lane holding work is kept

## Information Hiding is enforced by the shape, not by a promise

`AgentTask` is frozen and its field set is closed: an id, a title, a
description, its own brief, its own MUST-USE clause, its own weight, its lane,
and its lane's path. There is no ledger handle on it, no sibling list, no
mission-wide status. The PRD's third principle is that a sub-agent sees only its
own scope; a dataclass with nowhere to put the rest is a stronger guarantee than
a convention that the rest must not be read.

`tests/test_lintester.py` asserts that field set STRUCTURALLY, so a later edit
that adds a field reaching global state fails there rather than quietly widening
what every child can see.

## Failure is a field, never an exception

`Mission` reports `failed`, `kept`, `reason` and `outcome`. Nothing in this
module raises at a caller: a lane whose worktree could not be created, a child
that died, a ledger that would not accept a row — each is recorded and the other
lanes keep going. `mindnet/orchestrator.py`'s `Orchestration` makes the same
bargain, for the same reason.

## Validated means the lane's tests passed

With a `validator` (`gate.SuiteGate`) wired, a unit is validated only when its
child exited cleanly AND the tests that mention what it changed pass in its
lane; one full suite then runs on the integration tree (per lane, for work
that never merged), and a red suite makes the outcome `partial`. The library
default `None` is the exit-code path every runner-fake test was written
against; `cli.py` wires the gate on every run that starts work. A red gate is
the unit's failure, never the machine's -- `gate.py` says why that distinction
is load-bearing.

## The branch is the deliverable

With `merge_back` on, every validated unit is committed on its lane's branch
and the lanes are merged, probe-first, into `lint/<mission>/integration`, made
from the lanes' base -- `integrate.py` says why the orchestrator commits and
why a commit failure is never the unit's failure. The main checkout's HEAD
never moves. Library default off; `cli.py` turns it on for every run that
starts work, so `--execute --no-gate` commits unverified work, and its help
says so.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import threading
from dataclasses import dataclass, field

from . import environment as env_mod
from . import fence as fence_mod
from . import gate as gate_mod
from . import integrate as integrate_mod
from . import metrics as metrics_mod
from . import payoff as payoff_mod
from . import queue as queue_mod
from . import resilience as res_mod
from . import worktree as wt_mod
# Three carve-outs at the size cap, all kept reachable from here because these
# were the names in use: the closed per-child surface (`lintester.AgentTask`
# and `lintester._RunOutcome` -- `gate.py`'s local import and
# `orchestrator/__init__.py` both name them that way), the dry run's
# decomposer (`lintester._unit`, `lintester.preview_units`; `preview.py` says
# why `MAX_TASKS` moved with them) and the report (`lintester.Mission`, and
# the four words for where a unit list came from -- `mission.py` carries the
# comment that explains them).
from .agenttask import (MAX_MUST_USE, AgentTask, _clean, _RunOutcome,
                        _skill_name)
from .mission import AGENT, INJECTED, PREVIEW, ROOT_ONLY, Mission  # noqa: F401
from .preview import MAX_TASKS, _PARA, _slug, _unit, preview_units  # noqa: F401

MISSION_PREFIX = "lm"

# Lane default. Not "one per task": a lane costs a real `git worktree add`, and
# past four the checkout cost starts to dominate a mission of ordinary size.
DEFAULT_LANES = 4

DEFAULT_TIMEOUT_S = 900

# The posture every child is spawned with; `""` or None omits the flag. Probed
# 2026-09-08 on claude 2.1.198: a child spawned with NO --permission-mode had its
# Write declined, exited 0 clean, and `_run_child` scored it ok -- a fan-out would
# have timed N children that could not touch the tree. `autoresume` and
# `loop.config` pick the same value; bypassPermissions would need a
# --disallowedTools fence this seam cannot pass.
DEFAULT_PERMISSION_MODE = "acceptEdits"


@dataclass
class LintesterOrchestrator:
    """The mission pipeline. One instance per mission is the intended use.

    Every collaborator is injectable, and that is not decoration: the default
    decomposer and the default runner both spawn real agents, so a suite that
    could not replace them could only test the parts that do not matter."""

    root: object = None
    project: object = None
    lanes: int = DEFAULT_LANES
    timeout_s: int = DEFAULT_TIMEOUT_S
    # See `DEFAULT_PERMISSION_MODE`; injectable like every collaborator here.
    permission_mode: str = DEFAULT_PERMISSION_MODE
    limits: res_mod.Limits = field(default_factory=res_mod.Limits)
    max_attempts: int = res_mod.DEFAULT_MAX_ATTEMPTS
    decomposer: object = None
    runner: object = None
    reflector: object = None
    # The per-unit test gate: anything with `check(lane_path, task, attempt)`
    # and `full(cwd, mission_id=)` answering a `gate.GateResult`; `None` is
    # the exit-code path (see "Validated means" above).
    validator: object = None
    # The merge-back (`integrate.py`): each validated unit committed on its
    # lane branch, the lanes merged probe-first into the integration branch.
    # Off by default -- every runner-fake test was written against a teardown
    # that made no branch -- and on for every `cli.py` run that starts work.
    merge_back: bool = False
    evs: object = None
    # WB7's observation seam. `bus` is anything with `emit(kind, **fields)`;
    # `observer` is an async context manager FACTORY the caller supplies, run
    # around the lane phase. Both are duck-typed on purpose: this module must
    # not import a renderer, so the front door (`cli.py`) is what knows the
    # board exists and hands one in. See `dashboard.py`'s first paragraph.
    bus: object = None
    observer: object = None
    # What the seam LOST, held the way the two sibling producers hold it.
    # `init=False`: these are a tally, not a collaborator.
    observer_drops: int = field(default=0, init=False, repr=False, compare=False)
    observer_error: str = field(default="", init=False, repr=False, compare=False)
    # The symbol map, built at most once per mission and only when a unit
    # actually owns a file. `init=False` because it is a cache, not a
    # collaborator: nothing outside this object should be able to hand it one.
    _smap: object = field(default=None, init=False, repr=False, compare=False)
    # What the gate answered, one dict per run, appended from the scheduler's
    # executor threads (`list.append` is atomic) and folded into `Mission`.
    _gates: list = field(default_factory=list, init=False, repr=False,
                         compare=False)
    # One row per lane commit the merge-back made (or refused), same shape.
    _commits: list = field(default_factory=list, init=False, repr=False,
                           compare=False)
    # The ceiling the children actually ran under, built on first use and only
    # when `limits` asked for one. `init=False` because it is this object's own
    # record of what happened, not a collaborator: a caller that could inject a
    # fence could inject one that always answers `kernel`, which is the single
    # claim this field exists to make un-fakeable.
    _fence: object = field(default=None, init=False, repr=False, compare=False)
    # `_fence` is built from the scheduler's worker threads, so the build is a
    # check-then-set several lanes reach at once. Measured: a real `Fence()`
    # costs ~3 ms, because `fencing` probes `import resource` and calls into
    # kernel32 through ctypes, and both release the GIL — four lanes released
    # together built more than one fence in 200 of 200 rounds. Every fence but
    # the last is then orphaned, and an orphaned fence's downgrade is a
    # downgrade nothing reports: the mission prints `kernel` over the very
    # child that escaped. Hence a lock, not an argument about the GIL.
    _fence_lock: object = field(default_factory=threading.Lock, init=False,
                                repr=False, compare=False)

    # ---- naming -------------------------------------------------------------

    def _mission_id(self, intent) -> str:
        digest = hashlib.sha1(intent.encode("utf-8")).hexdigest()[:8]
        return f"{MISSION_PREFIX}-{digest}"

    def _emit(self, kind, **fields) -> None:
        """Publish one state change. A board must never fail a mission.

        Counted, the way `queue.WorkStealingScheduler._emit` and
        `resilience.RepairLoop._emit` count. This one used a bare `pass` and so
        was the one producer of the three whose losses left no trace at all —
        which is the failure the other two exist to avoid, in the module that
        publishes the header the operator reads."""
        bus = self.bus
        if bus is None:
            return
        try:
            bus.emit(kind, **fields)
        except Exception as exc:                        # noqa: BLE001
            self.observer_drops += 1
            if not self.observer_error:
                self.observer_error = f"{type(exc).__name__}: {exc}"

    def _gate_note(self, detail) -> None:
        """One board line per gate run, under the lane phase. The phase
        literal stays in THIS file, where the declared-phase scan looks."""
        self._emit("brain_phase", phase="Running Lanes", detail=str(detail))

    def _merge_note(self, detail) -> None:
        """One board line per lane at the merge. `integrate_lanes` speaks
        through `emit(text)`; the phase literal stays in this file."""
        self._emit("brain_phase", phase="Merging Worktrees", detail=str(detail))

    def _root(self):
        """The main checkout for every lane call. `None` means the working
        directory, as it does for `_files_for` -- but git needs a real path,
        and `Path(None)` was a `TypeError` on a run with `--root` unset."""
        from pathlib import Path

        return self.root if self.root else Path.cwd()

    def _name(self, intent) -> tuple:
        """``(task_id, title)`` for the root task, from the incumbent deriver."""
        try:
            from ..mindnet import autodeclare as ad_mod
            title = ad_mod.headline(intent) or intent[:80]
            task_id = ad_mod.derive_id(intent)
        except Exception:                               # noqa: BLE001
            title = intent[:80]
            task_id = self._mission_id(intent)
        return str(task_id), str(title)

    # ---- decomposition ------------------------------------------------------

    def _subtasks(self, root_id, title, intent, dry_run) -> tuple:
        """``([(task_id, title, description, files)], source, reason)`` for the
        units.

        *files* is `None` on every row but the preview's — see `_unit`.

        *source* names WHERE the split came from, and it is returned rather than
        inferred because the four origins are not interchangeable: an injected
        decomposer, the real `decompose.run` agent, the local preview below, or
        no split at all. `Mission.decomposition` carries it out to the operator
        so a preview can never be read as the agent's answer.

        *reason* says WHY the split is not what was asked for, and is empty when
        it is. It exists because `root-only` on the agent path is four different
        facts — the agent answered an empty list, the agent never ran, every
        proposal was refused, or the decomposer itself raised — and a report
        that prints the same word for all four sent an operator hunting an
        "already atomic" intent when the real story was a wiring bug. The
        `except` below still catches, so a decomposer fault degrades the
        mission to one unit rather than killing it; it no longer catches
        SILENTLY."""
        if self.decomposer is not None:
            try:
                out = [_unit(u) for u
                       in (self.decomposer(root_id, title, intent) or ())]
                why = ""
            except Exception as exc:                    # noqa: BLE001
                out = []
                why = (f"the injected decomposer raised "
                       f"{type(exc).__name__}: {exc}")
            return out[:MAX_TASKS], INJECTED, why
        if dry_run:
            units = preview_units(root_id, title, intent,
                                  self._files_for(intent), self.root)
            if units:
                return units[:MAX_TASKS], PREVIEW, ""
            return [(root_id, title, intent, None)], ROOT_ONLY, ""
        try:
            from ..mindnet import decompose as dec_mod
            from ..mindnet import tasks as t_mod
            # A `tasks.Task`, NOT an `AgentTask`. `decompose.validate` probes
            # the graph with `tasks.check_graph`, whose contract is a dict of
            # `Task`s — it reads `parent_id` and `dependencies` off every
            # value. `AgentTask` deliberately carries neither (information
            # hiding: a child must not see the graph), so the conversion
            # belongs HERE, at the one seam where the orchestrator's narrow
            # unit meets the graph's wide node, and not on `AgentTask`.
            graph = {root_id: t_mod.Task(task_id=root_id, title=title,
                                         description=intent)}
            # `spawn=` for the same reason `_run_child` passes it: this is a
            # real `claude` child, and it is the FIRST one every real mission
            # runs. Before it was fenced, a mission could hold all four lanes
            # to the ceiling and still report `kernel` over a decomposition
            # agent that had none.
            got = dec_mod.run(root_id, graph, project=self.project,
                              root=self.root, spawn=self._spawn())
            proposals = list(getattr(got, "usable", ()) or ())
            why = _clean(getattr(got, "reason", ""))
            if not proposals and not why:
                offered = list(getattr(got, "proposals", ()) or ())
                if offered:
                    first = _clean(getattr(offered[0], "reason", ""))
                    why = (f"the agent proposed {len(offered)} subtask(s) and "
                           f"none was usable" + (f": {first}" if first else ""))
                else:
                    why = ("the agent proposed no subtasks — the intent reads "
                           "as already atomic")
        except Exception as exc:                        # noqa: BLE001
            proposals = []
            why = f"decomposition raised {type(exc).__name__}: {exc}"
        if not proposals:
            return [(root_id, title, intent, None)], ROOT_ONLY, why
        return [(str(p.task_id), str(p.title),
                 str(getattr(p, "description", "") or p.title), None)
                for p in proposals][:MAX_TASKS], AGENT, ""

    # ---- weighting and lane packing -----------------------------------------

    def _files_for(self, description) -> tuple:
        """Repo paths named in *description* that actually exist.

        Deliberately conservative: a token that does not resolve to a file is not
        a file, and guessing would weight a task by a path that is not there.

        CONTAINED to the repo root, and that is a security boundary rather than
        tidiness. A description is model-written — `decompose.run` composes it —
        and `Path(base) / "/etc/passwd"` discards *base* entirely on POSIX, while
        `..` walks out of it on both platforms. Anything that resolves outside
        the root would then be opened and AST-parsed by
        `metrics.calculate_workload_weight` and handed to `git log`. Nothing is
        echoed back into a prompt, so the leak is only the derived weight — but
        reading arbitrary files off the operator's disk on a sub-agent's say-so
        is not a thing this should do at all."""
        from pathlib import Path

        base = Path(self.root) if self.root else Path.cwd()
        try:
            root = base.resolve()
        except OSError:                                 # pragma: no cover - odd fs
            return ()
        out = []
        for token in str(description or "").replace(",", " ").split():
            token = token.strip("`'\"()[]<>;:")
            if "/" not in token and "\\" not in token:
                continue
            try:
                candidate = (base / token).resolve()
                if not candidate.is_relative_to(root):
                    continue
                if not candidate.is_file():
                    continue
            except (OSError, ValueError):
                continue
            out.append(candidate.as_posix())
        return tuple(dict.fromkeys(out))

    def _weigh(self, task_id, description, files) -> int:
        """One unit's weight: its files' fused weight, plus its own usage.

        A unit that names no file still gets a floor of 1, so `shard_plan` can
        order it — a zero weight would make every fileless task look identical
        and collapse them into one lane."""
        total = 0
        for path in files:
            total += metrics_mod.calculate_workload_weight(
                path, root=self.root, evs=self.evs).total
        total += metrics_mod.W_USAGE * metrics_mod.usage(
            task_id, evs=self.evs, project=self.project)
        return max(1, int(total))

    def _lane_plan(self, units) -> list:
        """Pack *units* into weighted lanes via the incumbent bin-packer.

        Each unit is one cluster keyed by its task id, so `shard_plan`'s
        file-ownership invariant — a cluster is placed WHOLE — becomes "a task is
        never split across lanes", which is the property this needs."""
        from .. import clusters as clusters_mod

        groups = {tid: list(files) or [tid] for tid, _t, _d, files, _w in units}
        weights = {tid: w for tid, _t, _d, _f, w in units}
        try:
            return clusters_mod.shard_plan(groups, weights, self.lanes)
        except ValueError:
            raise
        except Exception:                               # noqa: BLE001
            return [{"lane": 0, "clusters": sorted(groups), "files": [],
                     "est_complexity": sum(weights.values())}]

    # ---- per-task material --------------------------------------------------

    def _context(self):
        """The wave context every task's brief is resolved against.

        `symbols=True` opts THIS mission into the AST slice explicitly rather
        than setting `MINDHIVE_SYMBOLS` in the process. An env write inside a
        scheduler is visible to every concurrent lane and to the parent shell,
        and would silently switch the slice on for every other caller in the
        process for as long as the mission ran."""
        try:
            from ..mindnet import brief as brief_mod
            return brief_mod.wave_context(project=self.project, evs=self.evs,
                                          root=self.root, symbols=True)
        except Exception:                               # noqa: BLE001
            return None

    def _skills_for(self, task, ctx) -> tuple:
        """The MUST-USE skill names for one task, resolved per task.

        `promptgate` already emits this directive, but once per session against
        the session's prompt. A mission fans out N children with N different
        jobs, and one session-wide clause names skills that are right for at most
        one of them."""
        if ctx is None:
            return ()
        try:
            from ..mindnet import advice as adv_mod
            resolved = adv_mod.resolve(task,
                                       evs=list(getattr(ctx, "evs", ()) or ()),
                                       project=getattr(ctx, "project", None),
                                       reco=getattr(ctx, "reco", None))
            names = [_skill_name(s) for s in getattr(resolved, "skills", ()) or ()]
        except Exception:                               # noqa: BLE001
            return ()
        return tuple(dict.fromkeys(n for n in names if n))[:MAX_MUST_USE]

    def _symbol_map(self):
        """The repository's parsed symbol map, built at most once per mission.

        Lazy, because a mission whose units name no file never needs it and the
        scan is a real parse of every module in the tree. Cached on the instance
        for the same reason `wave_context` is resolved once: N units would
        otherwise pay N scans of one unchanged tree."""
        if self._smap is None:
            try:
                from .. import symbols as sym_mod
                self._smap = sym_mod.build_map(self.root)
            except Exception:                           # noqa: BLE001
                self._smap = False                      # asked once, and failed
        return self._smap or None

    def _task_context(self, files, ctx):
        """*ctx* with the symbol slice narrowed to what THIS unit can reach.

        `wave_context` resolves one repo-wide slice and every task in the wave
        was briefed with the same bytes — correct for the incumbent, which runs
        a wave of related tasks, and wrong here: a lintester mission fans units
        that own DIFFERENT directories, and a map of the whole repository tells
        each of them mostly about code it does not own.

        Falls back to the wave slice whenever the narrow one comes back empty —
        a unit that owns no module is better served by the repository's shape
        than by a header saying it has none. The context is frozen, so this
        replaces it rather than mutating what the other lanes are reading."""
        if ctx is None or not files:
            return ctx
        smap = self._symbol_map()
        if smap is None:
            return ctx
        try:
            from .. import symbols as sym_mod
            sliced = sym_mod.focus(smap, files)
        except Exception:                               # noqa: BLE001
            return ctx
        if not sliced:
            return ctx
        return dataclasses.replace(ctx, symbols=sliced)

    def _compose(self, task_id, title, description, files, weight, lane_id,
                 worktree_path, ctx) -> AgentTask:
        partial = AgentTask(task_id=task_id, title=title,
                            description=description, files=tuple(files),
                            weight=int(weight), lane_id=int(lane_id),
                            worktree_path=str(worktree_path or ""))
        brief_text = ""
        task_ctx = self._task_context(files, ctx)
        try:
            from ..mindnet import brief as brief_mod
            brief_text = str(brief_mod.build(partial, task_ctx).text or "")
        except Exception:                               # noqa: BLE001
            brief_text = ""
        return dataclasses.replace(partial, brief_text=brief_text,
                                   must_use=self._skills_for(partial, task_ctx))

    # ---- running ------------------------------------------------------------

    def _spawn(self):
        """The fenced spawn callable for a real child, or `None`.

        `None` whenever no ceiling was asked for, and that is the point:
        `claude_cli.run_headless` then resolves `subprocess.run` itself and
        makes byte for byte the call it has always made. Routing every existing
        run through a fence that fences nothing would put new code on the one
        path this project cannot test end to end without credentials.

        One `Fence` per mission, shared by every lane — and built under
        `_fence_lock`, because several lanes reach this line together and the
        build is a check-then-set. The verdict it accumulates is a claim about
        all of them; a per-lane fence, or a second fence left orphaned by a
        race, could only ever say that ONE child was held."""
        if self.limits is None or self.limits.empty:
            return None
        with self._fence_lock:
            if self._fence is None:
                self._fence = fence_mod.Fence(self.limits)
            return self._fence.spawn

    def _fencing(self) -> tuple:
        """``(enforcement, reason)`` — what held the children, not what the
        platform predicted would.

        `resilience.fencing` answers the prediction, and for every report made
        before a child runs the prediction is all there is. Once children have
        run, the fence's own record outranks it: a job this machine could not
        create is evidence, and a mission that printed `kernel` over an unfenced
        child is the exact defect `fence.py` was written to end. With no fence
        (no ceiling asked for, or an injected runner that never used ours) there
        is nothing observed, so the prediction stands."""
        if self._fence is not None:
            return self._fence.enforcement, self._fence.reason
        return res_mod.fencing(self.limits)

    def _run_child(self, entry, lane):
        """Default runner: one sub-agent, in the lane checkout it may edit."""
        task = entry.task
        try:
            from ..mindnet import agent as ag_mod
            # ONE string for the run_key and the child's `MINDHIVE_SESSION`,
            # and both matter for a different reason.
            #
            # The session overlay is the identity the whole authorship control
            # rests on. Without it the child inherits the OPERATOR's session, so
            # (a) it appends to the operator's ledger segment -- concurrent
            # agents sharing a segment is exactly what tore the seven rows
            # `events._append_lock` was written for -- and (b) every `hive log`
            # / `hive sop` row it writes is attributed to a human, which is what
            # lets an injected agent put a MANDATORY SOP in front of the
            # operator. `mindnet.orchestrator` has always set this
            # (`orchestrator.py:520`); this spawner never did.
            #
            # `MISSION_PREFIX` is in `advice.ORCHESTRATION_PREFIXES`, asserted
            # against the imported constant by
            # `test_the_lintester_mission_prefix_is_covered`. An identity no
            # consumer recognises would be no identity at all.
            run_key = f"{MISSION_PREFIX}-{task.task_id}"
            # The posture (see `DEFAULT_PERMISSION_MODE`) is bounded by the
            # directory below it, which IS the lane checkout: an approved edit
            # reaches no sibling lane and not the main tree.
            res = ag_mod.run(run_key, task.prompt,
                             root=(lane.worktree_path or self.root),
                             project=self.project, timeout_s=self.timeout_s,
                             env={"MINDHIVE_SESSION": run_key},
                             permission_mode=(self.permission_mode or None),
                             spawn=self._spawn())
        except Exception as exc:                        # noqa: BLE001
            return _RunOutcome(ok=False,
                               reason=f"{type(exc).__name__}: {exc}",
                               result="spawn-failed")
        try:
            ran = bool(ag_mod.ran(res))
        except Exception:                               # noqa: BLE001
            ran = False
        # A result that says no child was started keeps saying so. This used to
        # fold `refused` into `fail`, and the repair loop then did everything a
        # failure deserves to a machine that had merely said "Not logged in" --
        # see `environment.py`'s first paragraph.
        kind = str(getattr(res, "result", "") or "")
        try:
            started = bool(res.executed)
        except AttributeError:
            started = ran
        # `ran` says a child was created, not that it succeeded: a child that
        # exited 1 or was killed at the cap also ran. Treating `ran` as `ok`
        # reported both as validated.
        ok = ran and kind not in ("fail", "timeout")
        if ok:
            kind = "ok"
        elif kind not in env_mod.NEVER_RAN and kind != "timeout":
            kind = "fail"
        return _RunOutcome(ok=ok, result=kind, executed=started,
                           exit_code=getattr(res, "exit_code", None),
                           reason=str(getattr(res, "reason", "") or ""),
                           stderr=str(getattr(res, "stderr", "") or ""))

    def _rollback_for(self, lane):
        """Roll one lane's checkout back to its last commit. `RepairLoop`'s seam.

        A method rather than a lambda because it carries the root the guard in
        `worktree.rollback` checks the lane path against, and that guard is the
        only thing standing between a repair and the main checkout."""
        return wt_mod.rollback(self._root(), getattr(lane, "worktree", None))

    def _calibrate(self, mission_id, weights, entries):
        """Score the weights against what the units really cost, and keep it.

        Called on the real path only. The estimate is what the packer believed;
        `elapsed_ms` is what the scheduler measured; until these two were put
        beside each other nothing in this repository could say whether the
        weight was worth computing.

        Total: a mission that ran is not failed by a store that cannot be
        written or by a correlation that cannot be computed."""
        durations = {}
        # Gate time is the SUITE's, not the child's: the weight predicts how
        # long a child runs, so the pytest run beside it comes off the sample.
        gate_ms: dict = {}
        for row in self._gates:
            tid = str(row.get("task_id", "") or "")
            gate_ms[tid] = (gate_ms.get(tid, 0.0)
                            + float(row.get("duration_s", 0.0) or 0.0) * 1000.0)
        for entry in entries or ():
            # A unit whose child never started spent its time on the machine's
            # refusal, not on the work the weight predicts. Recording it would
            # put a near-zero sample beside real ones and score the weight
            # against a run that did not happen.
            try:
                started = entry.result.executed
            except AttributeError:
                started = True
            if started is False:
                continue
            tid = str(getattr(entry, "task_id", "") or "")
            # FLOAT milliseconds. `QueueEntry` measures in nanoseconds so that a
            # unit faster than a millisecond is still a measurement; coercing to
            # `int` here would floor it back to zero, and the `> 0` guard below
            # would then drop it as a unit that never ran.
            ms = float(getattr(entry, "elapsed_ms", 0.0) or 0.0)
            ms = max(0.0, ms - gate_ms.get(tid, 0.0))
            if tid and ms > 0:
                durations[tid] = durations.get(tid, 0.0) + ms
        try:
            cal = payoff_mod.calibrate(weights, durations)
        except Exception:                               # noqa: BLE001
            return payoff_mod.Calibration()
        try:
            payoff_mod.record_samples(cal.samples, mission_id=mission_id,
                                      project=self.project)
            payoff_mod.record_result(cal, mission_id=mission_id,
                                     project=self.project)
        except Exception:                               # noqa: BLE001
            pass
        return cal

    def _full_suites(self, mission_id, trees, completed) -> tuple:
        """The whole suite, once per lane that validated a unit, before the
        teardown. The fallback: with the merge-back, `_closing_suite` runs it
        ONCE on the integration tree, and this loop is for the work that never
        reached one -- there each lane's tree is the only place its validated
        work exists, so each is judged where it stands."""
        if self.validator is None:
            return ()
        done = {int(getattr(e, "lane_id", -1)) for e in completed or ()}
        rows = []
        for tree in trees or ():
            lane_id = int(getattr(tree, "lane_id", -1))
            if not getattr(tree, "ok", False) or lane_id not in done:
                continue
            self._emit("brain_phase", phase="Running Suite",
                       detail=f"lane {lane_id}: {tree.path}")
            rows.append(gate_mod.full_row(self.validator, tree.path, lane_id,
                                          mission_id))
        return tuple(rows)

    def _closing_suite(self, mission_id, integration, trees, completed) -> tuple:
        """The whole suite ONCE, on the integration tree, when one exists;
        else `_full_suites`, per lane, for the work that never merged."""
        path = str(getattr(integration, "path", "") or "")
        if self.validator is None or not path:
            return self._full_suites(mission_id, trees, completed)
        self._emit("brain_phase", phase="Running Suite",
                   detail=f"integration: {path}")
        return (gate_mod.full_row(self.validator, path,
                                  integrate_mod.INTEGRATION_LANE, mission_id),)

    def _committer(self, mission_id):
        """`gated`'s `after`: commit a validated unit's lane on the child's
        behalf and keep the row. The session id is the child's own run key,
        so the change-log entry is attributed to the agent, not the operator."""
        def commit(entry, lane, out):
            task = entry.task
            path = (str(getattr(lane, "worktree_path", "") or "")
                    or str(getattr(task, "worktree_path", "") or ""))
            row = integrate_mod.commit_lane(
                self._root(), path, task, mission_id, project=self.project,
                session_id=f"{MISSION_PREFIX}-{task.task_id}")
            self._commits.append(row._asdict())
        return commit

    # ---- the entry point ----------------------------------------------------

    def execute_mission(self, intent, dry_run=True, checkpoint_dir=None) -> Mission:
        """Carry *intent* through the whole pipeline. Answers a `Mission`.

        `dry_run` defaults to True and that default is deliberate: a front-door
        command that fans real sub-agents at a repository on a bare invocation is
        a hazard, not a convenience."""
        text = _clean(intent)
        # `resilience.fencing` owns this, and asking it rather than deriving it
        # here is the fix for a real cross-platform defect: this used to take the
        # reason straight off `limits_available()`, which answers an empty string
        # wherever `resource` imports, so every POSIX run reported `timeout-only`
        # and gave the operator no reason for it. Windows hid the bug, because
        # there the reason is non-empty for an unrelated cause.
        enforcement, why = res_mod.fencing(self.limits)
        if not text:
            return Mission(outcome="refused", enforcement=enforcement,
                           enforcement_reason=why,
                           reason="an empty intent has nothing to orchestrate")

        mission_id = self._mission_id(text)
        root_id, title = self._name(text)
        # In place, not rebound: `gated` below holds this list by reference.
        del self._gates[:]
        # One instance per mission is the intended use, but a reused one must
        # not report the last mission's downgrade as this one's. Under the same
        # lock `_spawn` builds through: nothing has been spawned yet, so this is
        # discipline rather than necessity, and discipline is what keeps the
        # next reader from adding a second unguarded write here.
        with self._fence_lock:
            self._fence = None
        # Announced BEFORE the shape is known, and announced again below once
        # it is. A header that stays blank through the slowest phase of the
        # mission is a header that is missing exactly when it is wanted.
        self._emit("mission_started", mission_id=mission_id, intent=text)
        self._emit("brain_phase", phase="Decomposing Intent", detail=title)
        subtasks, split, split_why = self._subtasks(root_id, title, text,
                                                    dry_run)
        # The decomposition child has run by now (on the execute path it is the
        # first child of the mission), so from here on the prediction above is
        # no longer the best answer available. Rebound at the seam rather than
        # at each of the four refusals below, because a refusal that reports
        # `kernel` over a decomposer the machine could not fence is the same
        # laundering as a completed mission that does.
        enforcement, why = self._fencing()
        if not subtasks:
            return Mission(intent=text, mission_id=mission_id, outcome="refused",
                           enforcement=enforcement, enforcement_reason=why,
                           decomposition=split, decomposition_reason=split_why,
                           reason=("the graph is empty — decomposition produced "
                                   "no unit and the intent yielded none"))

        self._emit("brain_phase", phase="Parsing AST",
                   detail=f"{len(subtasks)} unit(s)")
        units = []
        for tid, ttl, desc, given in subtasks:
            # A unit that came with its own file set keeps it. Only the preview
            # does, and see `_unit` for why re-deriving those would collapse the
            # split it just made.
            files = self._files_for(desc) if given is None else given
            # Per unit, not once for the loop: `_weigh` is where the AST parse
            # and the churn lookup actually happen, so a phase line emitted
            # after the loop would name the work as starting when it has ended.
            self._emit("brain_phase", phase="Calculating Weights",
                       detail=str(tid))
            units.append((str(tid), str(ttl), str(desc), files,
                          self._weigh(str(tid), desc, files)))

        self._emit("brain_phase", phase="Packing Lanes",
                   detail=f"{len(units)} unit(s)")
        plan = self._lane_plan(units)
        by_id = {u[0]: u for u in units}

        self._emit("brain_phase", phase="Isolating Worktrees",
                   detail=f"{len(plan)} lane(s)")
        trees = ([wt_mod.LaneWorktree(lane_id=ln["lane"], ok=True)
                  for ln in plan] if dry_run
                 else wt_mod.acquire_lanes(self._root(), len(plan), mission_id))
        ctx = self._context()
        self._emit("brain_phase", phase="Composing Briefs",
                   detail=f"{len(units)} unit(s)")

        tasks: list = []
        lanes_out: list = []
        lane_entries: list = []
        for lane_spec, tree in zip(plan, trees):
            lane_id = int(lane_spec["lane"])
            path = str(getattr(tree, "path", "") or "")
            entries = []
            names = []
            for tid in lane_spec["clusters"]:
                unit = by_id.get(tid)
                if unit is None:
                    continue
                task = self._compose(unit[0], unit[1], unit[2], unit[3],
                                     unit[4], lane_id, path, ctx)
                tasks.append(task)
                names.append(task.task_id)
                entries.append(queue_mod.QueueEntry(task=task, lane_id=lane_id))
            lanes_out.append({"lane": lane_id, "tasks": names,
                              "worktree": path,
                              "weight": int(lane_spec.get("est_complexity", 0)),
                              "ok": bool(getattr(tree, "ok", False)),
                              "reason": str(getattr(tree, "reason", "") or "")})
            lane_entries.append((lane_id, entries, tree))

        self._emit("mission_started", mission_id=mission_id, intent=text,
                   lanes=len(lanes_out), units=len(tasks))

        if dry_run:
            self._emit("mission_finished", mission_id=mission_id,
                       outcome="dry-run", reason="nothing was started")
            return Mission(intent=text, mission_id=mission_id, outcome="dry-run",
                           reason=("nothing was started; re-run with the execute "
                                   "flag to fan agents at these lanes"),
                           tasks=tuple(tasks), lanes=tuple(lanes_out),
                           enforcement=enforcement, enforcement_reason=why,
                           decomposition=split, decomposition_reason=split_why,
                           dry_run=True)

        runnable = [(lid, ents, tree) for lid, ents, tree in lane_entries
                    if getattr(tree, "ok", False) and ents]
        if not runnable:
            self._emit("mission_finished", mission_id=mission_id,
                       outcome="refused",
                       reason="no lane could be isolated")
            wt_mod.release_lanes(self._root(), trees)
            return Mission(intent=text, mission_id=mission_id, outcome="refused",
                           reason=("no lane could be isolated — see each lane's "
                                   "reason"),
                           tasks=tuple(tasks), lanes=tuple(lanes_out),
                           enforcement=enforcement, enforcement_reason=why,
                           decomposition=split, decomposition_reason=split_why,
                           dry_run=False)

        breaker = res_mod.ToolCircuitBreaker()
        repair = res_mod.RepairLoop(breaker=breaker,
                                    max_attempts=self.max_attempts,
                                    project=self.project,
                                    reflector=self.reflector,
                                    # Wired HERE and nowhere else. This is the
                                    # one call in the mission that can delete an
                                    # agent's files, and it is only ever handed
                                    # to a run that made the lane checkouts it
                                    # is pointed at — never a dry run, which
                                    # returns above, and never a library
                                    # default.
                                    rollback=self._rollback_for,
                                    bus=self.bus)
        lanes = [queue_mod.WorkerLane(lid, ents, worktree=tree)
                 for lid, ents, tree in runnable]
        runner = self.runner or self._run_child
        committer = self._committer(mission_id) if self.merge_back else None
        if self.validator is not None or committer is not None:
            # The gate sits BETWEEN the child and the scheduler's verdict: the
            # one place `validated` is earned by a test run, not an exit code.
            # The committer hangs after it, so nothing unvalidated is committed.
            runner = gate_mod.gated(runner, gate=self.validator,
                                    record=self._gates, emit=self._gate_note,
                                    after=committer)
        scheduler = queue_mod.WorkStealingScheduler(
            lanes, runner, repair=repair,
            mission_id=mission_id, checkpoint_dir=checkpoint_dir,
            # Both halves of the breaker, not just the failing one. `repair`
            # alone made it failure-only: it could open and never close.
            on_success=repair.succeeded, bus=self.bus)
        # The brake, wired after both ends exist. The first entry whose failure
        # is the machine's -- not logged in, quota gone, no CLI -- stops every
        # lane from taking the next unit, because the next unit would only say
        # the same thing. `mindnet.orchestrator` has braked on this for its
        # waves since MN-8; the lanes had nothing.
        repair.halt = scheduler.halt
        self._emit("brain_phase", phase="Running Lanes",
                   detail=f"{len(lanes)} lane(s)")

        async def _drive():
            """The lane phase, with the caller's observer wrapped around it.

            Inside the loop on purpose. The bus hands work published from a
            worker thread back through `call_soon_threadsafe`, so it has to be
            told which loop that is — and a board that subscribed before the
            loop existed would build its queue against the wrong one."""
            binder = getattr(self.bus, "bind", None)
            if callable(binder):
                try:
                    binder()
                except Exception:                       # noqa: BLE001
                    pass
            if self.observer is None:
                return await scheduler.run()
            async with self.observer():
                return await scheduler.run()

        try:
            completed, failed = asyncio.run(_drive())
        except KeyboardInterrupt:
            enforcement, why = self._fencing()
            released = wt_mod.release_lanes(self._root(), trees)
            # The reason must not promise a sidecar nobody asked for. With no
            # `checkpoint_dir` the scheduler's writer is a no-op, and telling the
            # operator "the checkpoint holds what was left" would send them to
            # look for a file that was never written.
            left = ("the checkpoint holds what was left"
                    if checkpoint_dir else
                    "nothing was checkpointed — no checkpoint directory was given")
            return Mission(intent=text, mission_id=mission_id, outcome="partial",
                           reason=f"interrupted; {left}",
                           tasks=tuple(tasks), lanes=tuple(lanes_out),
                           kept=tuple(r.reason for r in released if r.kept),
                           enforcement=enforcement, enforcement_reason=why,
                           decomposition=split, decomposition_reason=split_why,
                           dry_run=False)

        # Every child that was going to run has run, so from here down the
        # report says what the fence OBSERVED: a prediction that a real spawn
        # contradicted would be a claim the mission cannot support. The
        # interrupt path asks the same question for the same reason.
        #
        # Only the empty-intent refusal above still carries the prediction, and
        # it is the one return where nothing has been spawned at all.
        enforcement, why = self._fencing()

        # BEFORE the teardown: the suite is judged where the work still is --
        # on the integration tree once the lanes have merged, else per lane.
        integration = None
        if self.merge_back:
            self._emit("brain_phase", phase="Merging Worktrees",
                       detail=f"{len(trees)} lane(s)")
            integration = integrate_mod.integrate_lanes(
                self._root(), trees, mission_id, emit=self._merge_note)
            suites = self._closing_suite(mission_id, integration, trees,
                                         completed)
            # A merged lane's base moves to the integration commit, so the
            # teardown finds nothing ahead and removes it; an unmerged lane
            # keeps its base and is kept.
            trees = integrate_mod.merged_trees(trees, integration)
        else:
            suites = self._full_suites(mission_id, trees, completed)
            self._emit("brain_phase", phase="Merging Worktrees",
                       detail=f"{len(trees)} lane(s)")
        released = wt_mod.release_lanes(self._root(), trees)
        # The integration checkout goes; its branch is the deliverable.
        retired = integrate_mod.retire_integration(self._root(), integration)
        kept = [r.reason for r in released if r.kept]
        if getattr(retired, "kept", False):
            kept.append(str(getattr(retired, "reason", "") or ""))
        done = tuple(e.task_id for e in completed)
        bad = tuple(f"{e.task_id}: {e.reason}" for e in failed)
        halted = str(scheduler.halted or "")
        left = tuple(e.task_id for e in scheduler.left())
        env_kind = str((repair.environment or [{}])[0].get("kind", "") or "")
        if halted:
            outcome = "environment"
            reason = halted + (f"; {len(left)} unit(s) never started"
                               if left else "")
        else:
            # A red full suite is a mission-level fact -- the units passed
            # their own tests, the tree they left does not pass its suite --
            # reported as `partial` naming the lane, never as a unit's failure.
            red = [s for s in suites if not s.get("ok")]
            uncommitted = [c for c in self._commits if not c.get("ok")]
            unmerged = integration is not None and not integration.ok
            outcome = ("ok" if not (bad or red or uncommitted or unmerged)
                       else "partial")
            parts = [f"{len(bad)} of {len(tasks)} unit(s) failed"] if bad else []
            if uncommitted:
                parts.append(f"{len(uncommitted)} validated unit(s) not "
                             f"committed ("
                             + "; ".join(str(c.get("reason") or "no reason")
                                         for c in uncommitted) + ")")
            if unmerged:
                parts.append(f"merge-back: {integration.reason}")
            parts += ["the full suite is red "
                      + ("on the integration tree"
                         if int(s.get("lane_id", -1)) < 0
                         else f"in lane {s.get('lane_id')}")
                      + f" ({s.get('reason') or 'no reason recorded'})"
                      for s in red]
            reason = "; ".join(parts)
        cal = self._calibrate(mission_id,
                              {tid: u[4] for tid, u in by_id.items()},
                              list(completed) + list(failed))
        self._emit("mission_finished", mission_id=mission_id, outcome=outcome,
                   completed=len(done), failed=len(bad),
                   steals=int(scheduler.steals), reason=reason)
        return Mission(
            intent=text, mission_id=mission_id, outcome=outcome, reason=reason,
            environment=env_kind if halted else "", left=left,
            tasks=tuple(tasks), lanes=tuple(lanes_out), completed=done,
            failed=bad, kept=tuple(kept),
            enforcement=enforcement, enforcement_reason=why,
            steals=int(scheduler.steals),
            reflections=tuple(str(r) for r in repair.reflections),
            recoveries=tuple(str(r) for r in repair.recoveries),
            open_keys=tuple(breaker.open_keys()),
            rollbacks=tuple(str(r) for r in repair.rollbacks),
            calibration=cal.as_dict(),
            gates=tuple(dict(g) for g in self._gates), suites=suites,
            commits=tuple(dict(c) for c in self._commits),
            integration=integrate_mod.report(integration, retired),
            decomposition=split, decomposition_reason=split_why,
            dry_run=False)


def run_mission(intent, root=None, project=None, lanes=DEFAULT_LANES,
                dry_run=True, checkpoint_dir=None, **kwargs) -> Mission:
    """One-call convenience over `LintesterOrchestrator`.

    *checkpoint_dir* is named explicitly because it belongs to `execute_mission`,
    not to the constructor: left in `**kwargs` it was forwarded to
    `LintesterOrchestrator(...)`, which has no such field, so the one caller who
    tried to resume a mission through this function got a `TypeError` instead."""
    return LintesterOrchestrator(root=root, project=project, lanes=lanes,
                                 **kwargs).execute_mission(
        intent, dry_run=dry_run, checkpoint_dir=checkpoint_dir)
