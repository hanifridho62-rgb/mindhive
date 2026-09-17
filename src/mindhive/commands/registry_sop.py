# SPDX-License-Identifier: Apache-2.0
"""Standard-operating-procedure handlers: the `sop` group (`add`, `update`,
`delete`, `list`, `show`, `preflight`, `injection`). `_cmd_sop_write` is
the single write path `add` and `update` share, which is where the
authorship gate (phase P1d of the blueprint) attaches.
"""
import json
import os
import sys
from .. import events, paths

__all__ = ["_sop_body", "_cmd_sop_write", "cmd_sop_add", "cmd_sop_update",
    "cmd_sop_delete", "cmd_sop_list", "cmd_sop_show", "_preflight_report",
    "cmd_sop_preflight", "cmd_sop_injection"]


def _sop_body(args) -> str | None:
    """Resolve an SOP body from --text / --file / --stdin (at most one). A
    procedure body is usually multi-line markdown, which is painful to pass as
    a quoted shell argument on Windows — --file and --stdin avoid that."""
    given = [name for name, val in
             (("--text", args.text), ("--file", args.file),
              ("--stdin", args.stdin or None)) if val]
    if len(given) > 1:
        raise ValueError("pass only one of --text / --file / --stdin")
    if args.stdin:
        return sys.stdin.read()
    if args.file:
        from pathlib import Path
        return Path(args.file).read_text(encoding="utf-8")
    return args.text

def _cmd_sop_write(args, action) -> int | None:
    from .. import sop as sop_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    try:
        text = (args.reason if action == "delete" else _sop_body(args))
        tags = [t for t in (getattr(args, "tags", "") or "").split(",") if t]
        ev = sop_mod.record(action, args.sop_id,
                            title=getattr(args, "title", None), text=text,
                            tags=tags, session_id=sid,
                            mandatory=getattr(args, "mandatory", None))
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"sop {action}: {args.sop_id} @ {ev.ts}")
    return None

def cmd_sop_add(args) -> int | None:
    return _cmd_sop_write(args, "add")

def cmd_sop_update(args) -> int | None:
    return _cmd_sop_write(args, "update")

def cmd_sop_delete(args) -> int | None:
    return _cmd_sop_write(args, "delete")

def cmd_sop_list(args) -> int | None:
    from .. import sop as sop_mod
    sops = sop_mod.current(events.read_all(getattr(args, "project", None)))
    if args.json:
        print(json.dumps(sops, indent=2, ensure_ascii=False))
        return None
    if not sops:
        print('(no active SOPs — add one: hive sop add <id> --title "…" '
              '--file <body.md>)')
        return None
    n_must = 0
    for i, r in enumerate(sops, 1):
        tier = "MANDATORY " if r.get("mandatory") else ""
        n_must += 1 if r.get("mandatory") else 0
        # `safe_title`, but NOT partitioned: `withheld_footer` sends operators
        # to this command by name to read exactly the rows the delivery
        # surfaces withhold, so filtering here would make that instruction a
        # dead end. Sanitizing is still required -- a newline forges a second
        # numbered entry in a list whose numbering is the operator's index.
        print(f"  {i}. {tier}[{sop_mod.safe_title(r['sop_id'])} "
              f"v{r['version']}] {sop_mod.safe_title(r['title'])}  "
              f"(updated {r['updated']})")
    tail = (f" ({n_must} mandatory — re-asserted on every prompt)"
            if n_must else "")
    print(f"\n{len(sops)} active SOP(s){tail}")
    return None

def cmd_sop_show(args) -> int | None:
    from .. import sop as sop_mod
    evs = events.read_all(getattr(args, "project", None))
    rec = sop_mod.get(evs, args.sop_id)
    hist = [e for e in sop_mod.entries(evs) if e["sop_id"] == args.sop_id]
    if args.json:
        print(json.dumps({"active": rec, "history": hist},
                         indent=2, ensure_ascii=False))
        return None
    if rec is None and not hist:
        print(f"error: no SOP '{args.sop_id}' in the ledger", file=sys.stderr)
        return 2
    if rec is None:
        print(f"SOP {args.sop_id}: deleted (history below)")
    else:
        print(f"SOP {rec['sop_id']} v{rec['version']} — {rec['title']}")
        print(f"created {rec['created']}  updated {rec['updated']}\n")
        print(rec["text"])
    print("\nhistory (newest first):")
    for e in hist:
        print(f"  {e['ts']}  {e['action']:6s}  session {e['session']}")
    return None

def _preflight_report(project=None) -> dict:
    """The pre-execution report the mandatory SOP names, as data.

    Exists because an instruction with no verifier is a wish. "Confirm from the
    ledger whether this session is new or a continuation" and "prepare the
    tools, ledgers and improvements before executing" are both claims an
    assistant can *say* it did; this makes them claims it can only make by
    running something whose output is quotable. Split from the printer so the
    verdict is testable without parsing stdout."""
    from .. import changelog as changelog_mod
    from .. import incidents as inc
    from .. import review as review_mod
    from .. import roadmap as roadmap_mod
    from .. import sop as sop_mod
    evs = events.read_all(project)
    here = os.environ.get("MINDHIVE_SESSION") or ""

    prior, latest_ts, latest_sid = set(), "", ""
    for e in evs:
        sid = getattr(e, "session_id", None)
        # "manual" is the shared id detached background writers stamp (see
        # promptgate.train_and_store) — it is not a session anyone worked in,
        # so counting it would report a CONTINUATION on a store whose only
        # other rows were written by a trainer.
        if not sid or sid == here or sid == "manual":
            continue
        prior.add(sid)
        if (e.ts or "") > latest_ts:
            latest_ts, latest_sid = e.ts or "", sid

    evidence = []
    if prior:
        evidence.append(
            f"{len(prior)} prior session(s) in this project's ledger"
            + (f" — latest {latest_sid} at {latest_ts}" if latest_sid else ""))
    interrupted = roadmap_mod.interrupted(evs)
    for rec in interrupted:
        nxt = roadmap_mod.next_step(rec)
        evidence.append(
            f"interrupted plan '{rec.get('plan_id')}' — {rec.get('title') or ''}"
            + (f" (next: {nxt.get('text')})" if isinstance(nxt, dict) else ""))
    rev = review_mod.load_pending(project) or {}
    if rev:
        evidence.append(
            f"pending thermo-nuclear review: {len(rev.get('files') or [])} file(s)")
    unlogged = changelog_mod.load_pending(project) or {}
    if unlogged:
        evidence.append(
            f"unlogged code changes: {len(unlogged.get('files') or [])} file(s)")

    try:
        acc = inc._collect(project) or {}
    except Exception:                                       # noqa: BLE001
        acc = {}
    rules = sorted(
        ({"signature": sig,
          "occurrences": rec.get("occurrences", 0),
          "status": ("active" if rec.get("occurrences", 0)
                     >= inc._threshold_for(sig) else "candidate")}
         for sig, rec in acc.items()),
        key=lambda r: -r["occurrences"])

    try:
        from .. import plugins as plugins_mod
        from .. import recommend
        inv = plugins_mod.live_inventory()["inventory"]
        skills = recommend.flatten_inventory(inv)
        catalog = {"skills": len(skills), "plugins": len(inv or [])}
    except Exception:                                       # noqa: BLE001
        catalog = {"skills": 0, "plugins": 0, "error": "inventory unavailable"}

    return {
        "project": project or paths.detect_project(),
        "session": here or "(unset)",
        "verdict": "CONTINUATION" if evidence else "NEW",
        "evidence": evidence,
        # Partitioned, sanitized, and admitting what it withheld, by id.
        **sop_mod.preflight_mandatory(evs),
        "improvements": {
            "active": sum(1 for r in rules if r["status"] == "active"),
            "candidate": sum(1 for r in rules if r["status"] == "candidate"),
            "top": rules[:10],
        },
        "catalog": catalog,
    }

def cmd_sop_preflight(args) -> int | None:
    from .. import sop as sop_mod
    rep = _preflight_report(getattr(args, "project", None))
    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return None
    print(f"SESSION PREFLIGHT — project {rep['project']}  "
          f"session {rep['session']}\n")
    print(f"Continuity: {rep['verdict']}")
    for line in rep["evidence"]:
        print(f"  - {line}")
    if rep["verdict"] == "CONTINUATION":
        print("  -> absorb the prior context BEFORE working: `hive state`, "
              "`hive briefing`, `hive recall \"<topic>\"`.")
    else:
        print("  - no prior sessions, interrupted plans, or pending markers.")
    print("\n".join(sop_mod.preflight_mandatory_lines(rep)))
    imp = rep["improvements"]
    print(f"\nImprovements — prevention rules "
          f"({imp['active']} active / {imp['candidate']} candidate):")
    for r in imp["top"]:
        print(f"  {r['occurrences']:>3}x  [{r['status']:9}] {r['signature']}")
    if not imp["top"]:
        print("  (no recorded incidents yet)")
    cat = rep["catalog"]
    print(f"\nCatalog: {cat['skills']} skill(s), {cat['plugins']} plugin(s)"
          + (f" — {cat['error']}" if cat.get("error") else "")
          + "  (hive skills list / hive plugins list)")
    return None

def cmd_sop_injection(args) -> int | None:
    """Report what the per-prompt SOP injection is currently delivering.

    Read-only, and deliberately not a gate: `hive context --check` is where the
    always-on surface gets an exit code. Exiting non-zero here would make a
    diagnostic fail in every script that ran it."""
    from .. import sopgate
    result = sopgate.probe(project=getattr(args, "project", None))
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return None
    print(sopgate.render_probe(result))
    return None
