# SPDX-License-Identifier: Apache-2.0
"""Autonomous-loop and work-session handlers: `hive loop` and `hive
worksession` (blueprint P1b), and `hive fabricate`, the PRD assembly line
that compiles a PRD into a loop work order and runs it unattended
(blueprint P10, spec Bug 9; the module is `fabricate.py`). The blueprint's
later phases add the `ci` pass-through here.
"""
import json
import os
import sys
from .. import events

__all__ = ["cmd_loop", "_ws_session", "_ws_print", "_ws_record", "_ws_status",
           "_ws_initiative", "_step_doc", "_ws_list_command", "_ws_listing",
           "_ws_list_steps", "_ws_list", "_ws_migrate", "_WS_ACTIONS",
           "cmd_worksession", "_latest_learning_report", "cmd_fabricate_compile",
           "cmd_fabricate_dispatch", "cmd_fabricate_run", "cmd_fabricate_list"]


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


def _ws_list_command(args) -> str:
    """The `hive worksession list` invocation a paged footer repeats, with the
    listing's own selectors, so the next-page hint pastes back into the same
    listing."""
    cmd = "hive worksession list"
    if getattr(args, "initiatives", False):
        cmd += " --initiatives"
    if getattr(args, "session", None):
        cmd += f" --session {args.session}"
    if getattr(args, "project", None):
        cmd += f" --project {args.project}"
    return cmd


def _ws_listing(args, rows: list, noun: str, tail: str = "") -> tuple:
    """What a `list` surface prints for *rows*: ``(doc, items, start, footer)``
    -- the JSON document, the rows to print, the absolute index of the first
    of them (the active-step marker is an absolute index) and the footer.

    Without ``--page``/``--page-size`` that is the rows themselves, all of
    them from index 0, under the old count footer: the pre-P8 output, byte for
    byte. With either flag, *doc* is the `pgshape.paginate_records` envelope
    (blueprint §10.8.2), *items* its page, and the footer names the page, the
    totals and the exact command for the next page -- or the last page, when
    the one asked for is past the end. The flags reach the helper untouched
    (``None`` for the one not given), so a clamped value -- 0, negative, above
    the ceiling -- means exactly what the helper says it means: one authority
    for the page arithmetic. `getattr`, because `tests/test_worksessions.py`
    builds its Namespace by hand, without the two keys."""
    page, size = getattr(args, "page", None), getattr(args, "page_size", None)
    if page is None and size is None:
        return rows, rows, 0, f"{len(rows)} {noun}(s){tail}"
    from .. import pgshape
    env = pgshape.paginate_records(rows, page=page, page_size=size)
    p, n, size = env["page"], env["total_pages"], env["page_size"]
    footer = f"page {p} of {n}, {len(env['items'])} of {env['total_items']} {noun}(s){tail}"
    if env["has_next"]:
        footer += f"\nnext page: {_ws_list_command(args)} --page {p + 1} --page-size {size}"
    elif n and p > n:
        footer += (f"\npast the end: the last page is {_ws_list_command(args)} "
                   f"--page {n} --page-size {size}")
    return env, env["items"], (p - 1) * size, footer


def _ws_list_steps(args, ws, project: str) -> int:
    """`list --session S`: one session's steps, with the ids `status` takes.
    Paged, the JSON ``steps`` is the envelope, and the plain marker still sits
    on the session's absolute active index."""
    w = ws.fold(ws.session_events(args.session, args.project), project).get(args.session)
    docs = [_step_doc(s) for s in w.steps] if w else []
    doc, items, start, footer = _ws_listing(
        args, docs, "step", f", session {w.status if w else ''}; move one with: hive "
        f"worksession status --session {args.session} --step-id <id> --status <status>")
    if args.json:
        _ws_print({"session_id": args.session, "project": project,
                   "status": w.status if w else "",
                   "active_step_index": w.active_step_index if w else 0,
                   "steps": doc}, True, "")
    elif w is None:
        print(f"(no steps in work session '{args.session}' -- hive worksession record "
              f"--session {args.session} --phase <phase> --action <key|text>)")
    else:
        for i, s in enumerate(items, start):
            mark = "*" if i == w.active_step_index else " "
            print(f"{mark} {s['step_id']}  [{s['status']}]  {s['ws_tag']}  "
                  f"{s['initiative_tag']}  {s['usage_tag']}")
        print(f"\n{footer}")
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
        doc, items, _start, footer = _ws_listing(args, rows, "initiative")
        if args.json:
            _ws_print(doc, True, "")
        elif not rows:
            where = f" in work session '{args.session}'" if args.session else ""
            print(f'(no initiatives{where} -- hive worksession record --initiative '
                  f'--reason "..." or hive worksession initiative --reason "...")')
        else:
            for r in items:
                step = f"  step {r['step_id']}" if r["step_id"] else ""
                print(f"  {r['ts']}  {r['session_id']}{step}  {r['tag']}")
            print(f"\n{footer}")
        return 0
    rows = ws.session_rows(evs, project)
    doc, items, _start, footer = _ws_listing(
        args, rows, "work session", "; step ids: hive worksession list --session <id>")
    if args.json:
        _ws_print(doc, True, "")
    elif not rows:
        print("(no work sessions -- hive worksession record --phase <phase> "
              "--action <key|text> --session <id>)")
    else:
        for r in items:
            print(f"  {r['session_id']}  [{r['phase']}] {r['steps']} step(s)  "
                  f"{r['status']}  {r['ws_tag']}")
        print(f"\n{footer}")
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


# --- fabricate (blueprint P10, spec Bug 9): the PRD assembly line -------------
# Ported by hand from branch claude/prd-assembly-line-40e466 (3a45aea), where
# these four handlers and their helper lived in `__main__.py`; the behaviour
# is the branch's, only the imports are relative to this package.

def _latest_learning_report(evs) -> str:
    """The most recent Self-Reliant PRD report from the ledger's learning
    events (the structured ``report`` field; the display ``text`` is a
    fallback for history written before the field existed)."""
    for e in reversed(evs):
        if e.type == "learning" and isinstance(e.payload, dict):
            report = str(e.payload.get("report")
                         or e.payload.get("text") or "").strip()
            if report:
                return report
    return ""


def cmd_fabricate_compile(args) -> int | None:
    from .. import fabricate as fabricate_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    project = getattr(args, "project", None)
    if args.prd_file:
        from pathlib import Path
        try:
            text = Path(args.prd_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"error: cannot read PRD file: {e}", file=sys.stderr)
            return 2
        source = args.prd_file
    elif args.from_learning:
        text = _latest_learning_report(events.read_all(project))
        if not text:
            print("error: no Self-Reliant learning (PRD) in the ledger — "
                  "run a learn-mode research cycle first, or pass a PRD "
                  "file", file=sys.stderr)
            return 2
        source = "learning:latest"
    else:
        print("error: pass a PRD file, or --from-learning for the latest "
              "Self-Reliant PRD", file=sys.stderr)
        return 2
    try:
        rec = fabricate_mod.compile_work_order(
            text, fab_id=args.id, base_dir=args.dir, source=source,
            project=project, session_id=sid)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"fabricate compiled: {rec['fab_id']} — {rec['title']} "
          f"({rec['steps']} SOP step(s))")
    print(f"  goal dir: {rec['goal_dir']}")
    print(f"  next: hive fabricate dispatch {rec['fab_id']} "
          f"[--at <ts> | --in-hours <h>]")
    return None


def cmd_fabricate_dispatch(args) -> int | None:
    from .. import fabricate as fabricate_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    try:
        p = fabricate_mod.dispatch(args.fab_id,
                                   project=getattr(args, "project", None),
                                   at=args.at, in_hours=args.in_hours,
                                   session_id=sid)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"fabricate dispatched: {args.fab_id} -> runs {p['due']} "
          f"(timer: {p['timer']})")
    if p["timer"] == "none":
        print("note: no OS timer registered — run it yourself when due: "
              f"hive fabricate run {args.fab_id}")
    return None


def cmd_fabricate_run(args) -> int | None:
    from .. import fabricate as fabricate_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    try:
        p = fabricate_mod.run(args.fab_id,
                              project=getattr(args, "project", None),
                              session_id=sid, force=args.force)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"fabricate run: {args.fab_id} -> {p['reason']} "
          f"({p['iterations']} iteration(s), loop run {p['run_id']})")
    return 0 if p["reason"] in ("completed", "api_cap", "budget_cap") else 1


def cmd_fabricate_list(args) -> int | None:
    from .. import fabricate as fabricate_mod
    recs = sorted(
        fabricate_mod.fold(events.read_all(getattr(args, "project",
                                                   None))).values(),
        key=lambda r: (r["updated"], r["fab_id"]))
    if args.json:
        print(json.dumps(recs, indent=2, ensure_ascii=False))
        return None
    if not recs:
        print("(no fabrications — compile a PRD: "
              "hive fabricate compile <prd.md>)")
        return None
    marks = {"compiled": "·", "dispatched": "!", "completed": "x",
             "stopped": "?"}
    for i, r in enumerate(recs, 1):
        if r["status"] == "dispatched":
            tail = f"runs {r['due']} (timer {r['timer']})"
        elif r["status"] in ("completed", "stopped"):
            tail = f"{r['reason']} ({r['iterations']} iteration(s))"
        else:
            tail = f"{r['steps']} step(s) compiled"
        print(f"  {i}. {marks.get(r['status'], '?')} [{r['fab_id']}] "
              f"{r['title'] or '(untitled)'} — {tail}")
    print(f"\n{len(recs)} fabrication(s)")
    return None
