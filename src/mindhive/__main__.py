# src/mindhive/__main__.py
import argparse
import json
import os
import sys
from . import events, projector, chain, paths
from . import recall as recall_mod
from . import export as export_mod
from . import selftest as selftest_mod


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

    # --- sync ---
    sp = sub.add_parser("sync", help="write native MEMORY.md for desktop auto-load")
    sp.add_argument("--folder", default=None,
                    help="project folder to encode (default: cwd)")
    sp.add_argument("--no-briefing", action="store_true",
                    help="omit BRIEFING.md from native MEMORY.md")

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
