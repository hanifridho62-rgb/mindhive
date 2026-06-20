# src/mindhive/__main__.py
import argparse
import json
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
