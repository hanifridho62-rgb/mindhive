# src/mindhive/__main__.py
import argparse
import json
import os
import sys
from . import events, projector, chain, paths
from . import recall as recall_mod
from . import export as export_mod
from . import selftest as selftest_mod


def _print_recommendations(payload: dict, scanned: bool) -> None:
    c = payload.get("counts", {})
    if scanned:
        print(f"scanned {c.get('plugins_total', 0)} plugins "
              f"({c.get('free', 0)} free, {c.get('paid', 0)} paid, "
              f"{c.get('skills_total', 0)} skills) -> stored plugin catalog")
    engine = payload.get("engine", "claude-opus-4-8")
    print(f"\nTop {len(payload.get('recommendations', []))} free plugins "
          f"recommended by {engine} (by breadth score):")
    for i, r in enumerate(payload.get("recommendations", []), 1):
        print(f"  {i}. {r['name']}  [score {r['breadth_score']:g}]")
        if r.get("note"):
            print(f"       {r['note']}")


# --- command handlers -------------------------------------------------------
# Each handler takes the parsed argparse.Namespace and returns an exit code, or
# None to mean 0. They are bound to their subparser via set_defaults(func=...)
# in _build_parser(), so dispatch is a single args.func(args) call instead of a
# long if/elif ladder. Per-command heavy imports stay local to the handler that
# needs them (keeps `hive` startup cheap and import side effects scoped).

def cmd_append(args) -> int | None:
    if args.stdin:
        raw = sys.stdin.read()
    elif args.payload is not None:
        raw = args.payload
    else:
        print("error: provide a payload argument or use --stdin", file=sys.stderr)
        return 2
    payload = json.loads(raw) if args.json else {"text": raw}
    tags = [t for t in args.tags.split(",") if t]
    ev = events.append_event(args.type, payload, session_id=args.session, tags=tags)
    print(f"appended {ev.type} @ {ev.ts}")
    return None


def cmd_hook(args) -> int | None:
    from . import hookcapture
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if args.stage == "post_tool_use":
        print(json.dumps(hookcapture.record_post_tool_use(raw)))
    return None


def cmd_project(args) -> int | None:
    print(json.dumps(projector.project()))
    return None


def cmd_state(args) -> int | None:
    s = paths.project_dir() / "STATE.md"
    print(s.read_text(encoding="utf-8") if s.exists()
          else "(no STATE.md — run: hive project)")
    return None


def cmd_directives(args) -> int | None:
    from . import directives as directives_mod
    print(directives_mod.directives())
    return None


def cmd_verify(args) -> int | None:
    print(json.dumps(chain.verify()))
    return None


def cmd_recall(args) -> int | None:
    for h in recall_mod.recall(args.query, semantic=args.semantic,
                               limit=args.limit, force=args.force):
        score = h.get("score")
        tag = f" [score {score:g}]" if isinstance(score, (int, float)) else ""
        print(f"{h['file']}{tag}\n  {h['snippet']}")
    return None


def cmd_export(args) -> int | None:
    print(str(export_mod.export(out=args.out, recent=args.recent)))
    return None


def cmd_embed(args) -> int | None:
    from . import vectors
    print(json.dumps(vectors.embed(force=args.force)))
    return None


def cmd_selftest(args) -> int | None:
    return selftest_mod.run()


def cmd_briefing(args) -> int | None:
    from .briefing import briefing as build_briefing
    bpath = build_briefing()
    print(str(bpath))
    print(bpath.read_text(encoding="utf-8"))
    return None


def cmd_review_queue(args) -> int | None:
    from . import review as review_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    marker = review_mod.queue(events.read_all(), session_id=sid)
    if marker:
        print(f"queued {len(marker['files'])} file(s) in "
              f"{marker['batch_count']} batch(es) for thermo-nuclear review")
    else:
        print("nothing in scope to review")
    return None


def cmd_review_clear(args) -> int | None:
    from . import review as review_mod
    print("cleared pending review" if review_mod.clear()
          else "no pending review")
    return None


def cmd_review_show(args) -> int | None:
    from . import review as review_mod
    print(json.dumps(review_mod.load_pending(), indent=2))
    return None


def cmd_graph_build(args) -> int | None:
    from . import graph as graph_mod
    result = graph_mod.build(semantic=args.semantic)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return None


def cmd_graph_related(args) -> int | None:
    from . import graph as graph_mod
    neighbors = graph_mod.related(args.query, depth=args.depth)
    print(json.dumps(neighbors, ensure_ascii=False, indent=2))
    return None


def cmd_graph_export(args) -> int | None:
    from . import graphview
    dest = graphview.export(out=args.out, semantic=args.semantic)
    print(str(dest))
    if args.open_after:
        import webbrowser
        webbrowser.open(dest.as_uri())
    return None


def cmd_dashboard(args) -> int | None:
    from . import dashboard
    dashboard.serve(port=args.port, open_browser=not args.no_browser)
    return None


def cmd_plugins_scan(args) -> int | None:
    from . import plugins as plugins_mod
    payload = plugins_mod.scan_and_store(cache_dir=args.cache, top_n=args.top)
    _print_recommendations(payload, scanned=True)
    return None


def cmd_plugins_autoscan(args) -> int | None:
    from . import plugins as plugins_mod
    res = plugins_mod.ensure_fresh(cache_dir=args.cache, top_n=args.top,
                                   max_age_hours=args.max_age_hours, force=args.force)
    if not res["scanned"]:
        print(f"catalog fresh ({res['age_hours']:.1f}h old) - skipping scan")
    _print_recommendations(res["payload"], scanned=res["scanned"])
    return None


def cmd_plugins_recommend(args) -> int | None:
    from . import plugins as plugins_mod
    payload = plugins_mod.latest_catalog()
    if not payload:
        print("(no plugin_catalog yet - run: hive plugins scan)")
    else:
        _print_recommendations(payload, scanned=False)
    return None


def cmd_sync(args) -> int | None:
    from . import sync as sync_mod
    folder = args.folder or os.getcwd()
    native_path = sync_mod.sync(folder, include_briefing=not args.no_briefing)
    print(str(native_path))
    return None


def cmd_harvest(args) -> int | None:
    from . import harvest as harvest_mod
    if args.catch_up:
        res = harvest_mod.catch_up(skip_captured=not args.include_captured,
                                   known_only=not args.all_projects)
        for proj in res["projects"]:           # refresh projections for touched namespaces
            projector.project(proj)
        print(json.dumps(res))
    elif args.transcripts and args.project:
        print(json.dumps(harvest_mod.harvest(args.transcripts, args.project,
                                             skip_captured=not args.include_captured)))
    else:
        print("error: give --transcripts and --project, or use --catch-up", file=sys.stderr)
        return 2
    return None


def cmd_autosync_register(args) -> int | None:
    from . import autosync as autosync_mod
    print(json.dumps(autosync_mod.register(args.project, args.transcripts, args.folder),
                     ensure_ascii=False, indent=2))
    return None


def cmd_autosync_run(args) -> int | None:
    from . import autosync as autosync_mod
    print(json.dumps(autosync_mod.run_all(), ensure_ascii=False, indent=2))
    return None


def cmd_loop(args) -> int | None:
    import os as _os
    from pathlib import Path as _Path
    from .loop import config as loop_config, scaffold as loop_scaffold
    from .loop import state as loop_state, driver as loop_driver, engine as loop_engine
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
        from .loop import task as loop_task, prompt as loop_prompt
        from .briefing import build_briefing as _bb
        tasks = loop_task.load_tasks(loop_dir)
        ctx = loop_prompt.build_loop_context(_bb(events.read_all()))
        tp = loop_prompt.build_task_prompt(tasks, [])
        print("DRY-RUN — would run claude with:")
        print(f"  required items remaining: {loop_task.required_remaining_count(tasks.fix_plan)}")
        print(f"  context_window={cfg.context_window} smart_zone_ratio={cfg.smart_zone_ratio}")
        print(f"  allowed_tools={cfg.allowed_tools}")
        print(f"  permission_mode={cfg.permission_mode or '(allowedTools only)'}")
        print(f"  task prompt ({len(tp)} chars), loop context ({len(ctx)} chars)")
        return 0
    if not loop_driver.preflight("claude"):
        print("error: claude CLI not on PATH — install Claude Code first", file=sys.stderr)
        return 2
    result = loop_engine.run_loop(
        project=None, loop_dir=loop_dir, cfg=cfg,
        driver=loop_driver.CLIDriver("claude"),
        live=args.live, force=args.force)
    print(json.dumps(result, indent=2))
    return 0 if result["reason"] in ("completed",) else 1


def cmd_failures_capture(args) -> int | None:
    from . import failures as failures_mod, incidents as incidents_mod
    res = failures_mod.capture_dir(args.transcripts, project=args.project)
    # Advance the rule lifecycle on capture: stamp activation for signatures that
    # just crossed the recurrence threshold, so leak-based auto-demotion is
    # reachable from the natural flow without a separate `hive rules audit`.
    incidents_mod.audit(project=args.project)
    incidents_mod.project(project=args.project)
    print(json.dumps(res))
    return None


def cmd_failures_list(args) -> int | None:
    from . import incidents as incidents_mod
    acc = incidents_mod._collect(args.project)
    if not acc:
        print("(no failures captured yet)")
        return None
    for signature, rec in sorted(acc.items(), key=lambda kv: -kv[1]["occurrences"]):
        status = "active" if rec["occurrences"] >= incidents_mod.THRESHOLD else "candidate"
        print(f"{rec['occurrences']:>3}x  [{status:9}] {signature}")
    return None


def cmd_rules_list(args) -> int | None:
    from . import incidents as inc
    acc = inc._collect(args.project)
    if not acc:
        print("(no incidents yet)")
        return None
    for signature, rec in sorted(acc.items(), key=lambda kv: -kv[1]["occurrences"]):
        status = "active" if rec["occurrences"] >= inc.THRESHOLD else "candidate"
        print(f"{rec['occurrences']:>3}x  [{status:9}] {signature}")
    return None


def cmd_rules_show(args) -> int | None:
    from . import incidents as inc
    path = inc._incident_dir(args.project) / f"{inc._safe_name(args.signature)}.md"
    print(path.read_text(encoding="utf-8") if path.exists() else "(no such incident)")
    return None


def cmd_rules_reflect(args) -> int | None:
    from . import reflect as reflect_mod, incidents as inc
    acc = inc._collect(args.project)
    done = 0
    for signature, rec in acc.items():
        if rec["occurrences"] >= inc.THRESHOLD:      # active only
            if reflect_mod.reflect_incident(signature, project=args.project).get("reflected"):
                done += 1
    print(json.dumps({"reflected": done}))
    return None


def cmd_rules_reconcile(args) -> int | None:
    from . import incidents as inc
    print(json.dumps(inc.reconcile(project=args.project)))
    return None


def cmd_rules_audit(args) -> int | None:
    from . import incidents as inc
    res = inc.audit(project=args.project)
    inc.project(project=args.project)
    print(json.dumps(res))
    return None


def cmd_rules_inject(args) -> int | None:
    from . import incidents as inc
    block = inc.inject(project=args.project)
    if block:
        print(block)
    return None


def cmd_rules_promote(args) -> int | None:
    from . import incidents as inc
    print(json.dumps(inc.promote(args.signature, project=args.project)))
    return None


def cmd_rules_demote(args) -> int | None:
    from . import incidents as inc
    print(json.dumps(inc.demote(args.signature, project=args.project)))
    return None


def _build_parser() -> argparse.ArgumentParser:
    """Construct the full `hive` argument parser. Every leaf subcommand binds its
    handler with set_defaults(func=...), so _run can dispatch on args.func."""
    p = argparse.ArgumentParser(prog="hive", description="MIND HIVE memory CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("append", help="append an event")
    a.add_argument("type")
    a.add_argument("payload", nargs="?", default=None)
    a.add_argument("--tags", default="")
    a.add_argument("--session", default=None)
    a.add_argument("--json", action="store_true", help="parse payload as JSON dict")
    a.add_argument("--stdin", action="store_true", help="read payload from stdin")
    a.set_defaults(func=cmd_append)

    hk = sub.add_parser("hook", help="handle a live Claude Code hook payload (reads stdin)")
    hk.add_argument("stage", choices=["post_tool_use"],
                    help="which hook fired")
    hk.set_defaults(func=cmd_hook)

    sub.add_parser("project", help="rebuild projections").set_defaults(func=cmd_project)
    sub.add_parser("state", help="print STATE.md").set_defaults(func=cmd_state)
    sub.add_parser("directives", help="print the session operating directives").set_defaults(func=cmd_directives)
    sub.add_parser("verify", help="verify hash chain").set_defaults(func=cmd_verify)
    sub.add_parser("selftest", help="run test suite").set_defaults(func=cmd_selftest)

    r = sub.add_parser("recall", help="search ledger")
    r.add_argument("query")
    r.add_argument("--semantic", action="store_true")
    r.add_argument("--limit", type=int, default=10)
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_recall)

    e = sub.add_parser("export", help="write handoff.md")
    e.add_argument("--out", default=None)
    e.add_argument("--recent", type=int, default=20)
    e.set_defaults(func=cmd_export)

    em = sub.add_parser("embed", help="build vector index (on-demand)")
    em.add_argument("--force", action="store_true")
    em.set_defaults(func=cmd_embed)

    # --- briefing ---
    sub.add_parser("briefing", help="build and print BRIEFING.md").set_defaults(func=cmd_briefing)

    # --- review (thermo-nuclear review queue) ---
    rv = sub.add_parser("review", help="thermo-nuclear review queue")
    rvsub = rv.add_subparsers(dest="review_cmd", required=True)
    rvq = rvsub.add_parser("queue", help="queue this session's touched source files for review")
    rvq.add_argument("--session", default=None,
                     help="session id to queue (default: $MINDHIVE_SESSION)")
    rvq.set_defaults(func=cmd_review_queue)
    rvsub.add_parser("clear", help="clear the pending-review marker (after the review runs)").set_defaults(func=cmd_review_clear)
    rvsub.add_parser("show", help="print the pending-review marker, if any").set_defaults(func=cmd_review_show)

    # --- graph build / graph related ---
    gp = sub.add_parser("graph", help="knowledge graph commands")
    gsub = gp.add_subparsers(dest="graph_cmd", required=True)
    gb = gsub.add_parser("build", help="build graph.json")
    gb.add_argument("--semantic", action="store_true")
    gb.set_defaults(func=cmd_graph_build)
    gr = gsub.add_parser("related", help="find related nodes")
    gr.add_argument("query")
    gr.add_argument("--depth", type=int, default=1)
    gr.set_defaults(func=cmd_graph_related)

    ge = gsub.add_parser("export", help="write standalone graph.html")
    ge.add_argument("--out", default=None)
    ge.add_argument("--semantic", action="store_true")
    ge.add_argument("--open", dest="open_after", action="store_true",
                    help="open the file in the default browser")
    ge.set_defaults(func=cmd_graph_export)

    # --- plugins (scan free skills/plugins, score, recommend) ---
    pl = sub.add_parser("plugins", help="catalog & recommend free skills/plugins")
    plsub = pl.add_subparsers(dest="plugins_cmd", required=True)
    pls = plsub.add_parser("scan", help="scan cache, score, store a catalog snapshot")
    pls.add_argument("--cache", default=None, help="plugin cache dir (default: ~/.claude/plugins/cache)")
    pls.add_argument("--top", type=int, default=5, help="how many to recommend")
    pls.set_defaults(func=cmd_plugins_scan)
    pla = plsub.add_parser("autoscan",
                           help="scan only if the catalog is missing or stale (cheap; safe every session)")
    pla.add_argument("--cache", default=None, help="plugin cache dir (default: ~/.claude/plugins/cache)")
    pla.add_argument("--top", type=int, default=5, help="how many to recommend")
    pla.add_argument("--max-age-hours", type=float, default=24.0, dest="max_age_hours",
                     help="rescan when the latest snapshot is older than this (default: 24)")
    pla.add_argument("--force", action="store_true", help="rescan even if the catalog is fresh")
    pla.set_defaults(func=cmd_plugins_autoscan)
    plsub.add_parser("recommend", help="print the latest catalog's top recommendations").set_defaults(func=cmd_plugins_recommend)

    # --- sync ---
    sp = sub.add_parser("sync", help="write native MEMORY.md for desktop auto-load")
    sp.add_argument("--folder", default=None,
                    help="project folder to encode (default: cwd)")
    sp.add_argument("--no-briefing", action="store_true",
                    help="omit BRIEFING.md from native MEMORY.md")
    sp.set_defaults(func=cmd_sync)

    # --- dashboard (one-window control panel; powers MindHive.exe) ---
    dp = sub.add_parser("dashboard", help="open the web control panel for all features")
    dp.add_argument("--port", type=int, default=None,
                    help="port (default: fixed 54461 for a stable URL; pass 0 for an ephemeral port)")
    dp.add_argument("--no-browser", dest="no_browser", action="store_true",
                    help="don't auto-open the browser")
    dp.set_defaults(func=cmd_dashboard)

    # --- harvest (auto-capture from Claude Code transcripts; no hooks needed) ---
    hv = sub.add_parser("harvest", help="capture events from Claude Code transcripts")
    hv.add_argument("--transcripts", default=None,
                    help="transcript DIRECTORY (omit with --catch-up)")
    hv.add_argument("--project", default=None,
                    help="target namespace (omit with --catch-up — it auto-detects per session)")
    hv.add_argument("--catch-up", dest="catch_up", action="store_true",
                    help="scan ALL Claude transcript dirs and backfill sessions the hooks missed")
    hv.add_argument("--include-captured", dest="include_captured", action="store_true",
                    help="also (re)harvest sessions the live hooks already recorded (risks double-counting)")
    hv.add_argument("--all-projects", dest="all_projects", action="store_true",
                    help="with --catch-up, also harvest into namespaces that don't exist yet (default: only known projects)")
    hv.set_defaults(func=cmd_harvest)

    # --- autosync (registry-driven: harvest + project + briefing + graph + sync) ---
    asp = sub.add_parser("autosync", help="automated sync for registered projects")
    asp.set_defaults(func=cmd_autosync_run)   # `autosync` with no subcommand -> run_all
    asub = asp.add_subparsers(dest="autosync_cmd", required=False)
    areg = asub.add_parser("register", help="register a project for autosync")
    areg.add_argument("--project", required=True)
    areg.add_argument("--transcripts", required=True)
    areg.add_argument("--folder", required=True)
    areg.set_defaults(func=cmd_autosync_register)

    # --- loop (autonomous Claude Code loop with MindHive memory) ---
    lp = sub.add_parser("loop", help="run an autonomous Claude Code loop until done")
    lp.add_argument("loop_cmd", nargs="?", choices=["init", "status"], default=None,
                    help="init: scaffold .hive-loop/; status: show last run; omit to run")
    lp.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="build prompts and show projected math without calling the CLI")
    lp.add_argument("--live", action="store_true", help="stream Claude output")
    lp.add_argument("--force", action="store_true", help="override the LIVE_LOCK guard")
    lp.set_defaults(func=cmd_loop)

    # --- failures (Learning Loop capture stage) ---
    fp = sub.add_parser("failures", help="capture tool failures + list incidents")
    fsub = fp.add_subparsers(dest="failures_cmd", required=True)
    fc = fsub.add_parser("capture", help="scan a transcript dir, store tool_failed events")
    fc.add_argument("--transcripts", required=True, help="transcript DIRECTORY")
    fc.add_argument("--project", default=None, help="target namespace")
    fc.set_defaults(func=cmd_failures_capture)
    fl = fsub.add_parser("list", help="list incident signatures by occurrence")
    fl.add_argument("--project", default=None)
    fl.set_defaults(func=cmd_failures_list)

    # --- rules (Learning Loop reflection stage) ---
    rp = sub.add_parser("rules", help="reflect on incidents; manage prevention rules")
    rsub = rp.add_subparsers(dest="rules_cmd", required=True)
    rl = rsub.add_parser("list", help="list incidents by occurrence")
    rl.add_argument("--project", default=None)
    rl.set_defaults(func=cmd_rules_list)
    rsh = rsub.add_parser("show", help="print one incident record")
    rsh.add_argument("signature")
    rsh.add_argument("--project", default=None)
    rsh.set_defaults(func=cmd_rules_show)
    rr = rsub.add_parser("reflect", help="run the reflection pass over active incidents")
    rr.add_argument("--project", default=None)
    rr.set_defaults(func=cmd_rules_reflect)
    ra = rsub.add_parser("audit", help="stamp activations for newly-recurring signatures")
    ra.add_argument("--project", default=None)
    ra.set_defaults(func=cmd_rules_audit)
    ri = rsub.add_parser("inject", help="print the active prevention-rules block")
    ri.add_argument("--project", default=None)
    ri.set_defaults(func=cmd_rules_inject)
    rpr = rsub.add_parser("promote", help="promote a rule into canonical SOUL.md")
    rpr.add_argument("signature")
    rpr.add_argument("--project", default=None)
    rpr.set_defaults(func=cmd_rules_promote)
    rdm = rsub.add_parser("demote", help="manually demote a rule")
    rdm.add_argument("signature")
    rdm.add_argument("--project", default=None)
    rdm.set_defaults(func=cmd_rules_demote)
    rc = rsub.add_parser("reconcile", help="record human edits as rule_revised events")
    rc.add_argument("--project", default=None)
    rc.set_defaults(func=cmd_rules_reconcile)

    return p


def main(argv=None) -> int:
    """CLI entry point. Frames a corrupt event log as a clean error instead of
    leaking a traceback — read_all itself stays fail-loud so the tamper-evident
    hash chain is never silently weakened; we only present the failure nicely."""
    try:
        return _run(argv)
    except json.JSONDecodeError as e:
        print(f"error: corrupt event log ({e})", file=sys.stderr)
        print("       a line in the event log is not valid JSON; "
              "run `hive verify` to locate it.", file=sys.stderr)
        return 1


def _run(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    rc = args.func(args)
    return rc if rc is not None else 0


if __name__ == "__main__":
    sys.exit(main())
