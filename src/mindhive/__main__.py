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
              f"{c.get('skills_total', 0)} skills) -> stored plugin_catalog")
    engine = payload.get("engine", "claude-opus-4-8")
    print(f"\nTop {len(payload.get('recommendations', []))} free plugins "
          f"recommended by {engine} (by breadth score):")
    for i, r in enumerate(payload.get("recommendations", []), 1):
        print(f"  {i}. {r['name']}  [score {r['breadth_score']:g}]")
        if r.get("note"):
            print(f"       {r['note']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hive", description="MIND HIVE memory CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("append", help="append an event")
    a.add_argument("type")
    a.add_argument("payload", nargs="?", default=None)
    a.add_argument("--tags", default="")
    a.add_argument("--session", default=None)
    a.add_argument("--json", action="store_true", help="parse payload as JSON dict")
    a.add_argument("--stdin", action="store_true", help="read payload from stdin")

    sub.add_parser("project", help="rebuild projections")
    sub.add_parser("state", help="print STATE.md")
    sub.add_parser("verify", help="verify hash chain")
    sub.add_parser("selftest", help="run test suite")

    r = sub.add_parser("recall", help="search ledger")
    r.add_argument("query")
    r.add_argument("--semantic", action="store_true")
    r.add_argument("--limit", type=int, default=10)
    r.add_argument("--force", action="store_true")

    e = sub.add_parser("export", help="write handoff.md")
    e.add_argument("--out", default=None)
    e.add_argument("--recent", type=int, default=20)

    em = sub.add_parser("embed", help="build vector index (on-demand)")
    em.add_argument("--force", action="store_true")

    # --- briefing ---
    sub.add_parser("briefing", help="build and print BRIEFING.md")

    # --- graph build / graph related ---
    gp = sub.add_parser("graph", help="knowledge graph commands")
    gsub = gp.add_subparsers(dest="graph_cmd", required=True)
    gb = gsub.add_parser("build", help="build graph.json")
    gb.add_argument("--semantic", action="store_true")
    gr = gsub.add_parser("related", help="find related nodes")
    gr.add_argument("query")
    gr.add_argument("--depth", type=int, default=1)

    ge = gsub.add_parser("export", help="write standalone graph.html")
    ge.add_argument("--out", default=None)
    ge.add_argument("--semantic", action="store_true")
    ge.add_argument("--open", dest="open_after", action="store_true",
                    help="open the file in the default browser")

    # --- plugins (scan free skills/plugins, score, recommend) ---
    pl = sub.add_parser("plugins", help="catalog & recommend free skills/plugins")
    plsub = pl.add_subparsers(dest="plugins_cmd", required=True)
    pls = plsub.add_parser("scan", help="scan cache, score, store a catalog snapshot")
    pls.add_argument("--cache", default=None, help="plugin cache dir (default: ~/.claude/plugins/cache)")
    pls.add_argument("--top", type=int, default=5, help="how many to recommend")
    pla = plsub.add_parser("autoscan",
                           help="scan only if the catalog is missing or stale (cheap; safe every session)")
    pla.add_argument("--cache", default=None, help="plugin cache dir (default: ~/.claude/plugins/cache)")
    pla.add_argument("--top", type=int, default=5, help="how many to recommend")
    pla.add_argument("--max-age-hours", type=float, default=24.0, dest="max_age_hours",
                     help="rescan when the latest snapshot is older than this (default: 24)")
    pla.add_argument("--force", action="store_true", help="rescan even if the catalog is fresh")
    plr = plsub.add_parser("recommend", help="print the latest catalog's top recommendations")

    # --- sync ---
    sp = sub.add_parser("sync", help="write native MEMORY.md for desktop auto-load")
    sp.add_argument("--folder", default=None,
                    help="project folder to encode (default: cwd)")
    sp.add_argument("--no-briefing", action="store_true",
                    help="omit BRIEFING.md from native MEMORY.md")

    # --- dashboard (one-window control panel; powers MindHive.exe) ---
    dp = sub.add_parser("dashboard", help="open the web control panel for all features")
    dp.add_argument("--port", type=int, default=0, help="port (default: a free one)")
    dp.add_argument("--no-browser", dest="no_browser", action="store_true",
                    help="don't auto-open the browser")

    # --- harvest (auto-capture from Claude Code transcripts; no hooks needed) ---
    hv = sub.add_parser("harvest", help="capture events from Claude Code transcripts")
    hv.add_argument("--transcripts", required=True)
    hv.add_argument("--project", required=True)

    # --- autosync (registry-driven: harvest + project + briefing + graph + sync) ---
    asp = sub.add_parser("autosync", help="automated sync for registered projects")
    asub = asp.add_subparsers(dest="autosync_cmd", required=False)
    areg = asub.add_parser("register", help="register a project for autosync")
    areg.add_argument("--project", required=True)
    areg.add_argument("--transcripts", required=True)
    areg.add_argument("--folder", required=True)

    args = p.parse_args(argv)

    if args.cmd == "append":
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
    elif args.cmd == "project":
        print(json.dumps(projector.project()))
    elif args.cmd == "state":
        s = paths.project_dir() / "STATE.md"
        print(s.read_text(encoding="utf-8") if s.exists()
              else "(no STATE.md — run: hive project)")
    elif args.cmd == "verify":
        print(json.dumps(chain.verify()))
    elif args.cmd == "recall":
        for h in recall_mod.recall(args.query, semantic=args.semantic,
                                   limit=args.limit, force=args.force):
            print(f"{h['file']}\n  {h['snippet']}")
    elif args.cmd == "export":
        print(str(export_mod.export(out=args.out, recent=args.recent)))
    elif args.cmd == "embed":
        from . import vectors
        print(json.dumps(vectors.embed(force=args.force)))
    elif args.cmd == "selftest":
        return selftest_mod.run()
    elif args.cmd == "briefing":
        from .briefing import briefing as build_briefing
        bpath = build_briefing()
        print(str(bpath))
        print(bpath.read_text(encoding="utf-8"))
    elif args.cmd == "graph":
        from . import graph as graph_mod
        if args.graph_cmd == "build":
            result = graph_mod.build(semantic=args.semantic)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.graph_cmd == "related":
            neighbors = graph_mod.related(args.query, depth=args.depth)
            print(json.dumps(neighbors, ensure_ascii=False, indent=2))
        elif args.graph_cmd == "export":
            from . import graphview
            dest = graphview.export(out=args.out, semantic=args.semantic)
            print(str(dest))
            if args.open_after:
                import webbrowser
                webbrowser.open(dest.as_uri())
    elif args.cmd == "dashboard":
        from . import dashboard
        dashboard.serve(port=args.port, open_browser=not args.no_browser)
    elif args.cmd == "plugins":
        from . import plugins as plugins_mod
        if args.plugins_cmd == "scan":
            payload = plugins_mod.scan_and_store(cache_dir=args.cache, top_n=args.top)
            _print_recommendations(payload, scanned=True)
        elif args.plugins_cmd == "autoscan":
            res = plugins_mod.ensure_fresh(cache_dir=args.cache, top_n=args.top,
                                           max_age_hours=args.max_age_hours, force=args.force)
            if not res["scanned"]:
                print(f"catalog fresh ({res['age_hours']:.1f}h old) - skipping scan")
            _print_recommendations(res["payload"], scanned=res["scanned"])
        elif args.plugins_cmd == "recommend":
            payload = plugins_mod.latest_catalog()
            if not payload:
                print("(no plugin_catalog yet - run: hive plugins scan)")
            else:
                _print_recommendations(payload, scanned=False)
    elif args.cmd == "sync":
        from . import sync as sync_mod
        folder = args.folder or os.getcwd()
        native_path = sync_mod.sync(folder, include_briefing=not args.no_briefing)
        print(str(native_path))
    elif args.cmd == "harvest":
        from . import harvest as harvest_mod
        print(json.dumps(harvest_mod.harvest(args.transcripts, args.project)))
    elif args.cmd == "autosync":
        from . import autosync as autosync_mod
        if getattr(args, "autosync_cmd", None) == "register":
            print(json.dumps(autosync_mod.register(args.project, args.transcripts, args.folder),
                             ensure_ascii=False, indent=2))
        else:
            print(json.dumps(autosync_mod.run_all(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
