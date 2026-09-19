# SPDX-License-Identifier: Apache-2.0
"""Autonomous-loop and work-session handlers: `hive loop` and `hive
worksession` (blueprint P1b). The blueprint's later phases add the `ci`
pass-through here.
"""
import json
import os
import sys
from .. import events

__all__ = ["cmd_loop", "_ws_session", "_ws_print", "_ws_record", "_ws_status",
           "_ws_initiative", "_step_doc", "_ws_list_steps", "_ws_list", "_ws_migrate", "_WS_ACTIONS",
           "cmd_worksession"]


def cmd_loop(args) -> int | None:
    import os as _os
    from pathlib import Path as _Path
    from ..loop import config as loop_config, scaffold as loop_scaffold
    from ..loop import state as loop_state, driver as loop_driver, engine as loop_engine
    loop_dir = _Path(_os.getcwd()) / ".hive-loop"
    if args.loop_cmd == "init":
        created = loop_scaffold.scaffold(loop_dir)
        ok = loop_driver.preflight("claude")
        print(f"scaffolded .hive-loop/ ({', '.join(created) or 'all files already present'})")
        print("claude CLI: " + ("found on PATH" if ok else
              "NOT on PATH — install Claude Code before running `hive loop`"))
        return 0
    if args.loop_cmd == "status":
        s = loop_state.last_run_status()
        if not s:
            print("no loop runs recorded yet")
        else:
            print(json.dumps(s, indent=2))
        return 0
    # run
    cfg = loop_config.load_config(loop_dir / "loop.toml")
    if args.dry_run:
        from ..loop import task as loop_task, prompt as loop_prompt
        from ..briefing import build_briefing as _bb
        tasks = loop_task.load_tasks(loop_dir)
        ctx = loop_prompt.build_loop_context(_bb(events.read_all()))
        tp = loop_prompt.build_task_prompt(tasks, [])
        from ..loop import router as loop_router
        req = loop_task.required_remaining_count(tasks.fix_plan)
        size = loop_router.size_class(req, len(tasks.prompt))
        tier = (loop_router.pick_initial_tier(
                    size, loop_router.run_history(events.read_all()))
                if cfg.model is None else None)
        print("DRY-RUN — would run claude with:")
        print(f"  required items remaining: {req}")
        print(f"  model routing: " + (f"size={size} -> tier={tier}"
              if tier else f"pinned model {cfg.model} (routing bypassed)"))
        print(f"  context_window={cfg.context_window} smart_zone_ratio={cfg.smart_zone_ratio}")
        print(f"  allowed_tools={cfg.allowed_tools}")
        print(f"  permission_mode={cfg.permission_mode or '(allowedTools only)'}")
        print(f"  task prompt ({len(tp)} chars), loop context ({len(ctx)} chars)")
        return 0
    if not loop_driver.preflight("claude"):
        print("error: claude CLI not on PATH — install Claude Code first", file=sys.stderr)
        return 2
    # Honor the LIVE_LOCK safety gate BEFORE doing anything with side effects
    # (dashboard spawn + auto-harvest). run_loop re-checks this as defense in
    # depth, but gating here means a run refused while V17 is live-trading never
    # spawns a background server. Clean refusal instead of a raw traceback.
    from .. import guard as _guard
    try:
        _guard.ensure_safe(force=args.force)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    # Bring up the web dashboard automatically for a real run so the loop can be
    # watched live (init/status/--dry-run returned above, so this fires on runs
    # only). Best-effort: a launch failure must never abort the loop. Opt out per
    # run with --no-dashboard, or globally with MINDHIVE_LOOP_DASHBOARD=0.
    _env = _os.environ.get("MINDHIVE_LOOP_DASHBOARD", "1").lower()
    if not args.no_dashboard and _env not in ("0", "false", "off", "no"):
        try:
            from .. import dashboard as _dashboard
            res = _dashboard.launch_background()
            if res.get("launched"):
                print(f"dashboard: {res['url']}")
            elif res.get("already_running"):
                print(f"dashboard: already running — {res['url']}")
            else:
                print(f"dashboard: not launched ({res.get('reason')})", file=sys.stderr)
        except Exception as e:  # never let the dashboard disturb the loop
            print(f"dashboard: not launched ({e})", file=sys.stderr)
    result = loop_engine.run_loop(
        project=None, loop_dir=loop_dir, cfg=cfg,
        driver=loop_driver.CLIDriver("claude"),
        live=args.live, force=args.force)
    print(json.dumps(result, indent=2))
    return 0 if result["reason"] in ("completed",) else 1


def _ws_session(args) -> str:
    """The session a `hive worksession` write lands in: ``--session``, else
    ``MINDHIVE_SESSION``, else ``manual`` -- `events.append_event`'s own
    fallback, so the payload's ``session_id`` always names the segment the row
    is written to."""
    return args.session or os.environ.get("MINDHIVE_SESSION") or "manual"


def _ws_print(doc, as_json: bool, text: str) -> None:
    print(json.dumps(doc, indent=2, ensure_ascii=False) if as_json else text)


def _ws_record(args, ws) -> int | None:
    from dataclasses import replace
    sid = _ws_session(args)
    if not args.initiative and (args.reason or args.ref):
        # BAB 5.3.2: a reason and an incident describe an AUTONOMOUS step; the
        # step would otherwise be stored as the user's spec, silently.
        print("error: --reason/--ref describe an autonomous step; add --initiative "
              "(without it the step is recorded as [USER_SPEC])", file=sys.stderr)
        return 2
    budget = args.token_budget if args.token_budget is not None else ws.DEFAULT_TOKEN_BUDGET
    try:
        step = ws.create_work_session_step(args.phase, args.action, args.initiative,
                                           initiative_reason=args.reason or "",
                                           tokens_used=args.tokens_used,
                                           token_budget=budget, project=args.project,
                                           initiative_ref=args.ref)
        if args.status:
            step = replace(step, status=args.status)
        # An [INITIATIVE] step is its own declaration: `initiatives.initiatives`
        # lists it, so no second row is written (and none can go missing).
        step = ws.record_work_session_step(sid, step, project=args.project)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    doc = {"session_id": sid, **_step_doc(step)}
    _ws_print(doc, args.json, f"{step.step_id} ({sid}) {step.technical_action} "
                              f"{step.initiative_tag} {step.usage_tag} [{step.status}]")
    return 0


def _ws_status(args, ws) -> int | None:
    sid = _ws_session(args)
    folded = ws.fold(ws.session_events(sid, args.project)).get(sid)
    if folded is None or args.step_id not in {s.step_id for s in folded.steps}:
        print(f"error: step '{args.step_id}' is not in work session '{sid}' "
              f"(hive worksession list --session {sid})", file=sys.stderr)
        return 2
    try:
        ev = ws.record_step_status(sid, args.step_id, args.status, project=args.project)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    _ws_print(ev.payload, args.json,
              f"work_session {args.step_id} ({sid}) -> {args.status} @ {ev.ts}")
    return 0


def _ws_initiative(args, ws) -> int | None:
    from .. import initiatives
    sid = _ws_session(args)
    try:
        tag = initiatives.declare_initiative(sid, args.reason, args.ref or "",
                                             project=args.project)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    reason, ref = ws.parse_initiative_tag(tag)
    _ws_print({"session_id": sid, "reason": reason, "ref": ref, "tag": tag},
              args.json, tag)
    return 0


def _step_doc(step) -> dict:
    """A step as every work-session JSON surface spells it: the ledger payload's
    key names (``phase``, ``ws_tag``), not the dataclass field names."""
    return {"step_id": step.step_id, "phase": step.phase_tag, "ws_tag": step.technical_action,
            "initiative_tag": step.initiative_tag, "usage_tag": step.usage_tag,
            "status": step.status, "timestamp": step.timestamp}


def _ws_list_steps(args, ws, project: str) -> int:
    """`list --session S`: one session's steps, with the ids `status` takes."""
    w = ws.fold(ws.session_events(args.session, args.project), project).get(args.session)
    if args.json:
        _ws_print({"session_id": args.session, "project": project,
                   "status": w.status if w else "",
                   "active_step_index": w.active_step_index if w else 0,
                   "steps": [_step_doc(s) for s in w.steps] if w else []}, True, "")
    elif w is None:
        print(f"(no steps in work session '{args.session}' -- hive worksession record "
              f"--session {args.session} --phase <phase> --action <key|text>)")
    else:
        for i, s in enumerate(w.steps):
            mark = "*" if i == w.active_step_index else " "
            print(f"{mark} {s.step_id}  [{s.status}]  {s.technical_action}  "
                  f"{s.initiative_tag}  {s.usage_tag}")
        print(f"\n{len(w.steps)} step(s), session {w.status}; move one with: hive "
              f"worksession status --session {args.session} --step-id <id> --status <status>")
    return 0


def _ws_list(args, ws) -> int | None:
    from .. import initiatives, paths
    project = args.project or paths.detect_project()
    if args.session and not args.initiatives:
        return _ws_list_steps(args, ws, project)
    evs = events.read_all(args.project)
    if args.initiatives:
        # `--session` narrows the initiatives to that session's declarations and
        # [INITIATIVE] steps; it never swaps the listing for the step list.
        rows = [r for r in initiatives.initiatives(evs)
                if not args.session or r["session_id"] == args.session]
        if args.json:
            _ws_print(rows, True, "")
        elif not rows:
            where = f" in work session '{args.session}'" if args.session else ""
            print(f'(no initiatives{where} -- hive worksession record --initiative '
                  f'--reason "..." or hive worksession initiative --reason "...")')
        else:
            for r in rows:
                step = f"  step {r['step_id']}" if r["step_id"] else ""
                print(f"  {r['ts']}  {r['session_id']}{step}  {r['tag']}")
            print(f"\n{len(rows)} initiative(s)")
        return 0
    rows = ws.session_rows(evs, project)
    if args.json:
        _ws_print(rows, True, "")
    elif not rows:
        print("(no work sessions -- hive worksession record --phase <phase> "
              "--action <key|text> --session <id>)")
    else:
        for r in rows:
            print(f"  {r['session_id']}  [{r['phase']}] {r['steps']} step(s)  "
                  f"{r['status']}  {r['ws_tag']}")
        print(f"\n{len(rows)} work session(s); step ids: hive worksession list --session <id>")
    return 0


def _ws_migrate(args, ws) -> int | None:
    from .. import worksession_migrate as mig
    report = mig.plan_migration(events.read_all(args.project), plan_id=args.plan,
                                session=args.session)
    if args.plan and not report:
        print(f"error: no roadmap plan '{args.plan}' (hive roadmap list --all)",
              file=sys.stderr)
        return 2
    for r in report:
        copies, moves = r.pop("copies"), r.pop("moves")
        r.update(written=0, settled=0, dry_run=bool(args.dry_run))
        if not args.dry_run and (copies or moves):
            try:
                r["written"] = mig.record_migrated(r["session_id"], copies,
                                                   project=args.project)
                r["settled"] = mig.settle_moves(moves, project=args.project)
            except (ValueError, OSError) as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
    if args.json:
        _ws_print(report, True, "")
    elif not report:
        print("(no roadmap plans to migrate -- hive roadmap list --all)")
    else:
        verb = "would write" if args.dry_run else "wrote"
        for r in report:
            n = r["pending"] if args.dry_run else r["written"]
            notes = [f"{r['stale']} stale (the roadmap text changed since the copy)"
                     if r["stale"] else "",
                     f"{r['drifted']} drifted (the roadmap status moved)"
                     if r["drifted"] else "",
                     f"{r['orphaned']} orphaned (the step left the roadmap)"
                     if r["orphaned"] else "",
                     (f"{'would settle' if args.dry_run else 'settled'} "
                      f"{r['settle'] if args.dry_run else r['settled']} status move(s)")
                     if r["settle"] else ""]
            print(f"  {r['plan_id']} -> {r['session_id']}: {r['steps']} step(s), "
                  f"{verb} {n}, {r['present']} already present"
                  + "".join(f", {x}" for x in notes if x))
            if r["hand_moved"]:
                print(f"    {r['hand_moved']} copy(ies) whose status was moved by hand "
                      f"left as they are: hive worksession list --session {r['session_id']}")
    return 0


_WS_ACTIONS = {"record": _ws_record, "status": _ws_status, "initiative": _ws_initiative,
               "list": _ws_list, "migrate": _ws_migrate}


def cmd_worksession(args) -> int | None:
    """`hive worksession record|status|initiative|list|migrate` (blueprint P1b,
    BAB 5): the CLI writer of ``work_session`` and ``initiative_declared`` rows,
    and the command every payload-contract hint for those two types names."""
    from .. import worksessions as ws
    return _WS_ACTIONS[args.ws_cmd](args, ws)
