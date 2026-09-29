# SPDX-License-Identifier: Apache-2.0
"""MindNetwork read-side and graph-shaping handlers: `mindnet show|list|
rebuild|decompose|depend`, with the satisfaction-line renderer the
`show` and `list` views share.
"""
import json
import os
from .. import events
from .mindnet_tasks import _mn_error, _mn_read, _mn_refresh

__all__ = ["MN_TIERS", "_mn_sat_line", "_mn_orphan_lines", "cmd_mindnet_show",
    "cmd_mindnet_list", "cmd_mindnet_rebuild", "cmd_mindnet_decompose",
    "cmd_mindnet_depend"]


# `verified_by` spelled for a human. The tier is the point of the whole
# two-validator contract, so `show` never renders a satisfaction without it.
MN_TIERS = {"python": "python", "agent": "agent-reported",
            "human": "human-attested"}

def _mn_sat_line(s) -> str:
    """One satisfaction, with its verifier tier and why it does or does not
    count. Order matters: an invalidated record and a stale one are both also
    "not ok", and reporting them as a bare failure loses the reason an operator
    can act on."""
    if not s.valid:
        glyph, note = "[-]", "  (superseded — a fall-back bumped the epoch)"
    elif s.stale:
        glyph, note = "[!]", ("  (STALE — a later check at the same authority "
                              "contradicted it)")
    elif s.result != "ok":
        glyph, note = "[!]", "  (failed)"
    else:
        glyph = "[x]" if s.verified_by == "python" else "[~]"
        note = ""
    tier = MN_TIERS.get(s.verified_by, s.verified_by or "unattested")
    ref = f"  {s.ref}" if s.ref else ""
    return f"{glyph} {s.phase}@{s.epoch}  {s.result}  {tier}{ref}{note}"

def _mn_orphan_lines(task_id, rows) -> list:
    return ([f"  orphans ({len(rows)}) — phase events for '{task_id}' with "
             f"nowhere to land:"]
            + [f"    - {o.action} '{o.phase or '?'}' @ {o.ts} "
               f"(session {o.session_id}): {o.reason}" for o in rows])

def cmd_mindnet_show(args) -> int | None:
    """One task in full: phase, epoch, every satisfaction with its verifier
    tier, the fold's refusals, and any orphaned phase events aimed at the id.

    Re-stats the current phase's file evidence. KTD8 puts that here and in
    `hive mindnet restat` and NOWHERE else — never on the injection hook path,
    where a stat per prompt would be a latency bill and an append would grow the
    hash chain once per keystroke."""
    from dataclasses import asdict
    from ..mindnet import controller as c_mod
    project, sid, evs, tasks, state = _mn_read(args)
    tid = args.task_id
    rec, task = state.records.get(tid), tasks.get(tid)
    orphans = state.orphans.get(tid, ())
    if rec is None and task is None and not orphans:
        return _mn_error(f"no task '{tid}' in the ledger — declare it with: "
                         f'hive mindnet declare {tid} --title "…"  ·  or see '
                         f"what is there: hive mindnet list")
    check = None
    if rec is not None:
        check = c_mod.restat(tid, evs=evs, tasks=tasks, project=project,
                             session_id=sid)
        if check.event is not None:
            # The finding is now on the ledger; re-fold so this render shows the
            # phase as stale, and refresh the sidecar the append just aged.
            _mn_refresh(project)
            evs = events.read_all(project)
            state = c_mod.fold(evs, tasks)
            rec = state.records.get(tid)

    if args.json:
        print(json.dumps({
            "task_id": tid,
            "task": asdict(task) if task is not None else None,
            "record": asdict(rec) if rec is not None else None,
            "orphans": [asdict(o) for o in orphans],
            "restat": (None if check is None
                       else {"status": check.status, "reason": check.reason}),
        }, indent=2, ensure_ascii=False, default=str))
        return None

    if rec is not None:
        title = task.title if task is not None else "(no graph record)"
        print(f"mindnet {tid} — {title}  [{rec.phase}, epoch {rec.epoch}]")
        if task is not None:
            bypass = "  triviality-bypass" if task.triviality_bypass else ""
            print(f"  status: {task.status}   parent: "
                  f"{task.parent_id or '(root)'}   depth: {task.depth}{bypass}")
        print("  evidence:")
        for s in rec.satisfactions:
            print("    " + _mn_sat_line(s))
        if not rec.satisfactions:
            print("    (none recorded)")
        print(f"  re-stat: {check.status} — {check.reason}")
        if rec.violations:
            print("  violations (refusals the fold recorded):")
            for v in rec.violations:
                print(f"    - {v}")
    elif task is not None:
        print(f"mindnet {tid} — {task.title}  [on the graph axis only — no "
              f"phase record, so every phase event for it orphans]")
        print(f'  fix: hive mindnet declare {tid} --title "{task.title}"')
    else:
        print(f"mindnet {tid} — nothing on either axis")
    for line in (_mn_orphan_lines(tid, orphans) if orphans else ()):
        print(line)
    return None

def cmd_mindnet_list(args) -> int | None:
    """Every task with its current phase and status, plus the orphan bucket.

    Reads the LEDGER, not the sidecar: this is not the hook path, and a listing
    that could disagree with `hive mindnet show` would be worse than a slow
    one."""
    from dataclasses import asdict
    from ..mindnet import controller as c_mod
    from ..mindnet import tasks as t_mod
    project = getattr(args, "project", None)
    evs = events.read_all(project)
    tasks = t_mod.fold(evs)
    state = c_mod.fold(evs, tasks)
    rows = [(tid, tasks[tid], state.records.get(tid)) for tid in sorted(tasks)
            if args.all or tasks[tid].status not in t_mod.CLOSED_STATUSES]
    if args.json:
        print(json.dumps(
            {"tasks": {tid: {"task": asdict(t),
                             "phase": c_mod.phase_of(state.records, tasks, tid),
                             "record": None if r is None else asdict(r)}
                       for tid, t, r in rows},
             "orphans": {tid: [asdict(o) for o in v]
                         for tid, v in state.orphans.items()}},
            indent=2, ensure_ascii=False, default=str))
        return None
    if not rows and not state.orphans:
        print('(no mindnet tasks — declare one: hive mindnet declare <id> '
              '--title "…")')
        return None
    for i, (tid, task, rec) in enumerate(rows, 1):
        phase = c_mod.phase_of(state.records, tasks, tid)
        # `?` is the one an operator has to act on: declared on the graph axis
        # and nowhere on the phase axis, so every phase event for it orphans.
        mark = "?" if rec is None else ("!" if rec.violations else ">")
        indent = "  " * min(max(task.depth, 0), t_mod.MAX_DEPTH)
        print(f"  {i}. {mark} {indent}[{tid}] {task.title}  {phase}  "
              f"epoch {0 if rec is None else rec.epoch}  ({task.status})")
    print(f"\n{len(rows)} task(s)")
    if state.orphans:
        total = sum(len(v) for v in state.orphans.values())
        print(f"{total} orphaned phase event(s) under "
              f"{len(state.orphans)} unrecorded task id(s): "
              + ", ".join(sorted(state.orphans))
              + " — inspect one with: hive mindnet show <id>")
    return None

def cmd_mindnet_rebuild(args) -> int | None:
    """Re-derive the sidecar from the ledger. What makes the projection honest
    rather than authoritative — and the repair for a stale or corrupt one."""
    from ..mindnet import sidecar as sc_mod
    project = getattr(args, "project", None)
    out = sc_mod.rebuild(project)
    if not out.written:
        return _mn_error(out.reason)
    print(f"mindnet rebuild: {out.records} record(s) from "
          f"{out.events_seen} event(s) -> {sc_mod.sidecar_path(project)}")
    return None

def cmd_mindnet_decompose(args) -> int | None:
    """Ask an agent what subtasks *task_id* actually requires, and optionally
    declare them.

    A DRY RUN unless `--execute`, for the same reason `orchestrate` is: this writes
    to an append-only ledger where a mistake cannot be deleted, only superseded.

    Every refusal is decided before anything is appended — `tasks.fold` drops an
    unsound edge SILENTLY, so a half-landed decomposition would report success and
    leave gaps the operator never sees."""
    from ..mindnet import decompose as dc_mod
    from ..mindnet import sidecar as sc_mod
    from ..mindnet import tasks as t_mod
    project = getattr(args, "project", None)
    sid = args.session or os.environ.get("MINDHIVE_SESSION")

    graph = t_mod.fold(events.read_all(project))
    if args.task_id not in graph:
        return _mn_error(f"no such task '{args.task_id}' — see: "
                         f"hive mindnet list")

    out = dc_mod.run(args.task_id, graph, project=project,
                     timeout_s=args.timeout)
    if out.reason and not out.proposals:
        return _mn_error(out.reason)

    if args.execute:
        out = dc_mod.declare(out, project=project, session_id=sid)
        # Once, after the whole batch — six subtasks must not pay six folds.
        sc_mod.rebuild(project)

    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(out), indent=2, ensure_ascii=False))
        return None

    usable = out.usable
    if not out.proposals:
        print(f"mindnet decompose: {args.task_id} — the agent proposed no "
              f"subtasks (it may already be atomic)")
        return None
    verb = "declared" if args.execute else "would declare"
    print(f"mindnet decompose: {args.task_id} — {verb} {len(usable)} subtask(s)")
    for p in out.proposals:
        mark = ">" if p.ok else "!"
        print(f"  {mark} [{p.task_id or '(no id)'}] {p.title}")
        if not p.ok:
            print(f"      refused: {p.reason}")
    if not args.execute:
        print("  this was a dry run — add --execute to declare them")
    # A refusal the operator should see rather than a silent partial success.
    return 1 if usable and len(usable) != len(out.proposals) else None

def cmd_mindnet_depend(args) -> int | None:
    """Record that *task_id* cannot start until *on* is COMPLETED.

    The edge that makes a graph a graph — and until now the only way to write one
    was `hive append mindnet_task '{"action":"depend",...}' --json`, raw JSON for
    the relation the whole orchestrator is built on.

    **Every refusal is checked HERE, before the append.** `tasks.fold` declines an
    unsound edge by silently `continue`-ing, so a handler that appended first and
    reported second would print success for an event that folds to nothing — the
    no-op-must-not-print-success trap this surface already guards on `satisfy`.
    The checks are the fold's own (`dependency_cycle`, `cross_graph_conflict`,
    `MAX_DEPS_PER_TASK`) run against the same probe graph, so the two cannot
    disagree about what is legal."""
    from dataclasses import replace
    from ..mindnet import sidecar as sc_mod
    from ..mindnet import tasks as t_mod
    project = getattr(args, "project", None)
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    tid, on = args.task_id, args.on

    tasks = t_mod.fold(events.read_all(project))
    if tid not in tasks:
        return _mn_error(f"no such task '{tid}' — see: hive mindnet list")
    if on not in tasks:
        return _mn_error(f"no such task '{on}' to depend on — see: "
                         f"hive mindnet list")
    if tid == on:
        return _mn_error(f"'{tid}' cannot depend on itself")

    cur = tasks[tid]
    if on in cur.dependencies:
        print(f"mindnet depend: {tid} already depends on {on} (no change)")
        return None
    if len(cur.dependencies) >= t_mod.MAX_DEPS_PER_TASK:
        return _mn_error(
            f"'{tid}' already has {len(cur.dependencies)} dependencies, the "
            f"maximum is {t_mod.MAX_DEPS_PER_TASK} — a node waiting on more "
            f"than that is under-decomposed")

    probe = dict(tasks)
    probe[tid] = replace(cur, dependencies=cur.dependencies + (on,))
    cyc = t_mod.dependency_cycle(probe)
    if cyc is not None:
        return _mn_error("that edge would close a dependency cycle: "
                         + " -> ".join(cyc))
    dead = t_mod.cross_graph_conflict(probe)
    if dead is not None:
        return _mn_error(
            f"that edge would deadlock: '{dead[0]}' would depend on its own "
            f"relative '{dead[1]}' — a parent cannot wait on its own child, and "
            f"a child cannot wait on its own ancestor")

    ev = events.append_event("mindnet_task",
                             {"action": "depend", "task_id": tid, "on": on},
                             session_id=sid, tags=["mindnet", "task"],
                             project=project)
    # Sidecar LAST: `_staleness` counts every event, so rebuilding before this
    # append would leave the projection stale and silently degrade MN-1's
    # phase-aware injection.
    sc_mod.rebuild(project)
    print(f"mindnet depend: {tid} now waits for {on} @ {ev.ts}")
    return None
