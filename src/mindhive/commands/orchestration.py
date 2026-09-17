# SPDX-License-Identifier: Apache-2.0
"""Autonomous-loop handlers: `hive loop`. The blueprint's later phases add
`worksession` and the `ci` pass-through here.
"""
import json
import sys
from .. import events

__all__ = ["cmd_loop"]


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
