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
      -> resilience.Guard / RepairLoop         fenced, broken, repaired
      -> worktree.release_lanes                merge-back and teardown

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
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from dataclasses import dataclass, field
from typing import NamedTuple

from . import environment as env_mod
from . import metrics as metrics_mod
from . import payoff as payoff_mod
from . import queue as queue_mod
from . import resilience as res_mod
from . import worktree as wt_mod

MISSION_PREFIX = "lm"

# Lane default. Not "one per task": a lane costs a real `git worktree add`, and
# past four the checkout cost starts to dominate a mission of ordinary size.
DEFAULT_LANES = 4

# Hard ceiling on units in one mission, matching `decompose.MAX_PROPOSED`'s
# spirit. A decomposition that answers fifty subtasks did not decompose, it
# enumerated, and fanning fifty children at one repo is how a machine falls over.
MAX_TASKS = 24

# Where a mission's unit list came from. Reported on `Mission.decomposition`,
# because the four are not the same claim and an operator reading a fan-out plan
# is entitled to know which one they are looking at: a real split proposed by an
# agent, a caller's own decomposer, a directory split this module derived from
# the intent's own file paths, or no split at all.
INJECTED = "injected"
AGENT = "agent"
PREVIEW = "local-preview"
ROOT_ONLY = "root-only"

DEFAULT_TIMEOUT_S = 900

MUST_USE_LEAD = ("Skills the record says work here. Use them via the Skill tool "
                 "before the first edit:")

# How many skills one task's clause may name. `advice.resolve` is documented in
# this project's ledger as noisy — a dashboard task once matched
# `market-sizing-analysis` — and a directive that names eight skills directs
# nothing. Three is a clause a child can act on.
MAX_MUST_USE = 3

# `advice` returns each skill as "<name> — <the catalog's whole description>",
# which is right for the fenced context block and wrong for a directive: the
# clause is an instruction naming a tool, and pasting a 200-character blurb into
# it buries the name it exists to say.
_SKILL_SPLITS = (" — ", " - ", ": ")


def _skill_name(value) -> str:
    text = _clean(value)
    for sep in _SKILL_SPLITS:
        text = text.split(sep, 1)[0]
    return text.strip()


class _RunOutcome(NamedTuple):
    """A child's result, normalised to what the scheduler and repair loop read."""

    ok: bool = False
    reason: str = ""
    stderr: str = ""
    result: str = "fail"
    # Whether a child process was created at all. `False` is what separates a
    # task that failed from a machine that could not start one, and
    # `environment.environment_failure` reads it -- so it must survive the
    # normalisation rather than be folded into `result`.
    executed: bool = True
    exit_code: object = None


@dataclass(frozen=True)
class AgentTask:
    """One independently-executable unit, carrying its own scope and nothing else.

    Frozen on purpose, and the frozen-ness is load-bearing twice: a child cannot
    be handed a task another lane is mutating, and the closed field set is what
    `test_lintester.py` asserts against to keep global repo state from creeping
    back in. Lifecycle lives on `queue.QueueEntry`, which is the mutable half."""

    task_id: str
    title: str = ""
    description: str = ""
    brief_text: str = ""
    must_use: tuple = ()
    weight: int = 0
    lane_id: int = -1
    worktree_path: str = ""
    files: tuple = ()

    def with_repair(self, suffix) -> "AgentTask":
        """A copy carrying a repair block appended to its description.

        *suffix* has already been through `resilience.defused_stderr` by the time
        it reaches here — `RepairLoop` composes it — so this does no sanitising
        of its own and must not be called with raw child output."""
        text = str(suffix or "").strip()
        if not text:
            return self
        joined = f"{self.description}\n\n{text}".strip()
        return dataclasses.replace(self, description=joined)

    @property
    def must_use_block(self) -> str:
        lines = [f"- MUST USE: {s} via the Skill tool" for s in self.must_use if s]
        return "\n".join([MUST_USE_LEAD, *lines]) if lines else ""

    @property
    def prompt(self) -> str:
        """Everything this child is told, in a fixed order.

        Fixed so two runs of one task read the same way; an irreproducible prompt
        is one nobody can diff when the run goes wrong."""
        parts = [f"# {self.title}" if self.title else "", self.description,
                 self.must_use_block, self.brief_text]
        return "\n\n".join(p for p in parts if p).strip()


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
        }


def _clean(text) -> str:
    try:
        return str(text or "").strip()
    except (ValueError, TypeError):
        return ""


def _unit(item) -> tuple:
    """``(task_id, title, description, files_or_None)`` from a decomposer row.

    A decomposer — injected, or `decompose.run` — answers three fields and lets
    `_files_for` derive the rest from the description. The local preview answers
    four, because its whole point is that each unit owns ONE directory:
    re-deriving a preview unit's files from a description that still carries the
    entire intent would hand every unit every file, and two lanes claiming the
    same file is the exact thing `clusters.shard_plan` exists to prevent."""
    row = tuple(item)
    files = row[3] if len(row) > 3 else None
    return (str(row[0]), str(row[1]), str(row[2]),
            None if files is None else tuple(files))


# The blank line between a preview unit's intent and its scope note. A named
# constant rather than an inline escape: the description is composed inside a
# nested f-string expression, where an escape is both hard to read and easy to
# mangle into a syntax error.
_PARA = """

"""


def _slug(text) -> str:
    """A task-id-safe form of a cluster key. Ids are read by people and used in
    paths, so `src/mindhive/orchestrator` may not travel as itself."""
    out = "".join(c if (c.isalnum() or c == "-") else "-"
                  for c in str(text).lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "cluster"


def preview_units(root_id, title, intent, files, root=None) -> list:
    """One unit per directory the *intent*'s own file paths fall in.

    Honest by construction, and that constraint is the whole design. A real run
    asks `decompose.run`, which needs an agent; inventing subtasks locally and
    letting them stand in for that agent's answer would make a dry run show a
    plan the real run would never produce. So this splits on nothing but a fact
    the intent already carries — the repo files it names — grouped by the same
    `clusters.cluster_key` the lane packer packs by. The caller labels the
    result `PREVIEW`, and `Mission.decomposition` carries that label out, so no
    reader downstream can mistake it for what an agent proposed.

    Fewer than two directories is not a split, and the caller falls back to the
    root task alone rather than dressing a single unit up as a decomposition."""
    from pathlib import Path

    from .. import clusters as clusters_mod

    base = Path(root) if root else Path.cwd()
    try:
        anchor = base.resolve()
    except OSError:                                     # pragma: no cover - odd fs
        return []
    groups: dict = {}
    for path in files or ():
        try:
            full = Path(path).resolve()
            rel = full.relative_to(anchor).as_posix()
        except (OSError, ValueError):
            continue
        groups.setdefault(clusters_mod.cluster_key(rel), {})[rel] = full.as_posix()
    if len(groups) < 2:
        return []
    out = []
    for key in sorted(groups):
        members = groups[key]
        # The description shows repo-relative paths because a person reads it;
        # the file tuple carries the resolved ones because `_weigh` opens them.
        out.append((f"{root_id}-{_slug(key)}", f"{title} [{key}]",
                    _PARA.join((intent, f"This unit owns {key} only: "
                                + ", ".join(sorted(members)))),
                    tuple(members[k] for k in sorted(members))))
    return out[:MAX_TASKS]


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
    limits: res_mod.Limits = field(default_factory=res_mod.Limits)
    max_attempts: int = res_mod.DEFAULT_MAX_ATTEMPTS
    decomposer: object = None
    runner: object = None
    reflector: object = None
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
            got = dec_mod.run(root_id, graph, project=self.project,
                              root=self.root)
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

    def _run_child(self, entry, lane):
        """Default runner: one real sub-agent, in its lane's checkout."""
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
            res = ag_mod.run(run_key, task.prompt,
                             root=(lane.worktree_path or self.root),
                             project=self.project, timeout_s=self.timeout_s,
                             env={"MINDHIVE_SESSION": run_key})
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
        return wt_mod.rollback(self.root, getattr(lane, "worktree", None))

    def _calibrate(self, mission_id, weights, entries):
        """Score the weights against what the units really cost, and keep it.

        Called on the real path only. The estimate is what the packer believed;
        `elapsed_ms` is what the scheduler measured; until these two were put
        beside each other nothing in this repository could say whether the
        weight was worth computing.

        Total: a mission that ran is not failed by a store that cannot be
        written or by a correlation that cannot be computed."""
        durations = {}
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
        # Announced BEFORE the shape is known, and announced again below once
        # it is. A header that stays blank through the slowest phase of the
        # mission is a header that is missing exactly when it is wanted.
        self._emit("mission_started", mission_id=mission_id, intent=text)
        self._emit("brain_phase", phase="Decomposing Intent", detail=title)
        subtasks, split, split_why = self._subtasks(root_id, title, text,
                                                    dry_run)
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
                 else wt_mod.acquire_lanes(self.root, len(plan), mission_id))
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
            wt_mod.release_lanes(self.root, trees)
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
        scheduler = queue_mod.WorkStealingScheduler(
            lanes, self.runner or self._run_child, repair=repair,
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
            released = wt_mod.release_lanes(self.root, trees)
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

        self._emit("brain_phase", phase="Merging Worktrees",
                   detail=f"{len(trees)} lane(s)")
        released = wt_mod.release_lanes(self.root, trees)
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
            outcome = "ok" if not bad else "partial"
            reason = "" if not bad else f"{len(bad)} of {len(tasks)} unit(s) failed"
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
            failed=bad, kept=tuple(r.reason for r in released if r.kept),
            enforcement=enforcement, enforcement_reason=why,
            steals=int(scheduler.steals),
            reflections=tuple(str(r) for r in repair.reflections),
            recoveries=tuple(str(r) for r in repair.recoveries),
            open_keys=tuple(breaker.open_keys()),
            rollbacks=tuple(str(r) for r in repair.rollbacks),
            calibration=cal.as_dict(),
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
