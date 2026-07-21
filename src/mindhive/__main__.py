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
    if args.type == "code_change":
        # The change-log is only mandatory if the generic append path can't
        # smuggle an uncategorized/unexplained entry past it. The event store
        # itself stays type-agnostic — the contract is enforced here, at the
        # one surface that accepts an arbitrary type+payload from the user.
        from . import changelog as changelog_mod
        try:
            changelog_mod.validate_payload(payload)
        except ValueError as e:
            print(f'error: {e} (use: hive log <category> "...")',
                  file=sys.stderr)
            return 2
    elif args.type == "sop":
        # Same reasoning for the SOP registry: the raw append path must not
        # smuggle a malformed lifecycle event past the sop contract.
        from . import sop as sop_mod
        try:
            sop_mod.validate_payload(payload)
        except ValueError as e:
            print(f"error: {e} (use: hive sop add|update|delete …)",
                  file=sys.stderr)
            return 2
    elif args.type == "roadmap":
        # And for the roadmap registry: a malformed lifecycle event would
        # corrupt no fold (reads are total) but would still be dead weight in
        # the chain — reject it at the one arbitrary-payload surface.
        from . import roadmap as roadmap_mod
        try:
            roadmap_mod.validate_payload(payload)
        except ValueError as e:
            print(f"error: {e} (use: hive roadmap set|step|interrupt|"
                  f"resume|complete …)", file=sys.stderr)
            return 2
    elif args.type == "autoresume":
        # Same stance for the delivery telemetry: the raw path must not
        # smuggle a malformed lifecycle event past the autoresume contract.
        from . import autoresume as autoresume_mod
        try:
            autoresume_mod.validate_payload(payload)
        except ValueError as e:
            print(f"error: {e} (use: hive autoresume schedule|fire|"
                  f"cancel …)", file=sys.stderr)
            return 2
    tags = [t for t in args.tags.split(",") if t]
    ev = events.append_event(args.type, payload, session_id=args.session, tags=tags)
    print(f"appended {ev.type} @ {ev.ts}")
    return None


def cmd_hook(args) -> int | None:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if args.stage == "post_tool_use":
        from . import hookcapture
        print(json.dumps(hookcapture.record_post_tool_use(raw)))
    elif args.stage == "post_tool_use_failure":
        # Structured live failure capture: one tool_failed event with a
        # machine-readable {tool, category, retryable} payload per failure.
        from . import hookcapture
        print(json.dumps(hookcapture.record_post_tool_use_failure(raw)))
    elif args.stage == "pre_tool_use":
        # Deny the ineffective tool form for enabled-enforcement signatures.
        # Print ONLY on a deny (stdout is the hook's decision channel); an allow
        # prints nothing so the hook stays a silent no-op.
        from . import enforce
        decision = enforce.hook_decision(raw)
        if decision:
            print(json.dumps(decision))
    elif args.stage == "user_prompt_submit":
        # Prompt-quality gate: inject skill-trigger directives when the typed
        # prompt is underspecified. Print ONLY when the gate fires (stdout is
        # the hook's context channel); a clear prompt prints nothing. When
        # prompt enrichment is enabled (the default), the fired context also
        # carries the clarifying questions (see enrich.extend_hook_context —
        # additive, sidecar-only read, fail-open).
        from . import enrich, promptgate
        context = promptgate.hook_context(raw)
        if context:
            context = enrich.extend_hook_context(context, raw)
            print(json.dumps(context))
        # Durable causal trace: record which skills the gate directed so Oversee
        # can later show the honest "directed by prompt-gate" why. Best-effort
        # and self-guarding — never disturbs the context printed above.
        promptgate.record_direction(raw)
    return None


def cmd_project(args) -> int | None:
    print(json.dumps(projector.project()))
    return None


def cmd_state(args) -> int | None:
    project = getattr(args, "project", None)
    # Warn (stderr, non-fatal) when a bare `hive state` is about to resolve to a
    # brand-new namespace inferred from cwd — the "wrong project from the wrong
    # directory" failure. An explicit --project or MINDHIVE_PROJECT silences it.
    if project is None and paths.is_freshly_minted():
        print(f"warning: no memory for detected project "
              f"'{paths.detect_project()}' — run from the project dir, set "
              f"MINDHIVE_PROJECT, or pass --project. See `hive projects`.",
              file=sys.stderr)
    s = paths.project_dir(project) / "STATE.md"
    print(s.read_text(encoding="utf-8") if s.exists()
          else "(no STATE.md — run: hive project)")
    return None


def cmd_projects(args) -> int | None:
    """List namespaces on disk, marking each registered (in the autosync
    allow-list) or unregistered — surfacing junk/orphan namespaces without
    deleting anything."""
    known = paths.known_projects()
    names = paths.list_namespaces()
    if not names:
        print("(no namespaces on disk yet)")
        return None
    for name in names:
        flag = "registered" if name in known else "unregistered"
        print(f"  [{flag:12}] {name}")
    unregistered = [n for n in names if n not in known]
    print(f"\n{len(names)} namespace(s): "
          f"{len(names) - len(unregistered)} registered, "
          f"{len(unregistered)} unregistered")
    return None


def cmd_directives(args) -> int | None:
    from . import directives as directives_mod
    print(directives_mod.directives())
    return None


def cmd_context(args) -> int | None:
    """Report what auto-loads into every session — ranked by estimated token
    cost, with trim suggestions. Reporting never mutates anything; the one
    write path is --set-budget, which persists the namespace's token budget.
    --check turns the report into a gate: exit 1 when over the budget (the
    hook adoption flows and scripts rely on), exit 2 when asked to check with
    no budget configured at all."""
    from . import context as context_mod
    project = getattr(args, "project", None)
    origin = getattr(args, "origin", "all")

    if getattr(args, "set_budget", None) is not None:
        data = context_mod.save_budget(args.set_budget, project=project)
        print(f"context budget set to {data['budget']:,} est. tokens "
              f"(stored in {context_mod.BUDGET_FILE})")
        return None

    segments = []
    if origin in ("all", "hive"):
        segments += context_mod.collect_hive(project=project)
    if origin in ("all", "claude"):
        segments += context_mod.collect_claude()

    budget = getattr(args, "budget", None)
    if budget is None:
        budget = context_mod.load_budget(project)

    report = context_mod.analyze(segments, budget=budget)
    found = context_mod.findings(segments)

    if getattr(args, "json", False):
        print(context_mod.to_json(report, found))
    else:
        print(context_mod.render_text(report, found, top=getattr(args, "top", None)))

    if getattr(args, "check", False):
        if budget is None:
            print("error: --check needs a budget — set one with "
                  "`hive context --set-budget N` (or pass --budget N)",
                  file=sys.stderr)
            return 2
        over = report.get("over_budget") or 0
        if over > 0:
            print(f"OVER BUDGET by ~{over:,} est. tokens — trim before adding "
                  f"anything new (see suggestions above)", file=sys.stderr)
            return 1
    return None


def _packs_manifest_path(arg) -> "Path":
    """Resolve the packs manifest: --manifest wins; otherwise
    <git toplevel>/docs/packs/packs.json. git rev-parse is used instead of
    bare cwd so the command works from any subdirectory of the checkout
    (inside a linked worktree, --show-toplevel is the worktree root — the
    checkout being validated, which is exactly right)."""
    from pathlib import Path
    if arg:
        return Path(arg)
    import subprocess
    from . import paths as paths_mod
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=2,
                           creationflags=paths_mod.NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()) / "docs" / "packs" / "packs.json"
    except Exception:
        pass
    return Path("docs") / "packs" / "packs.json"


def cmd_packs_check(args) -> int | None:
    """Run every pack quality gate; exit 1 on any error-severity finding —
    the same gate contract as `hive context --check`. Read-only."""
    from . import packs as packs_mod
    report = packs_mod.analyze(_packs_manifest_path(getattr(args, "manifest", None)))
    if getattr(args, "json", False):
        print(packs_mod.to_json(report))
    else:
        print(packs_mod.render_text(report))
    return None if report.get("ok") else 1


def cmd_packs_report(args) -> int | None:
    """Full pack stats (depth, similarity matrices, findings). Informational:
    always exits 0 — gating belongs to `hive packs check`."""
    from . import packs as packs_mod
    report = packs_mod.analyze(_packs_manifest_path(getattr(args, "manifest", None)))
    only = getattr(args, "pack", None)
    if only:
        stats = report.get("stats") or {}
        packs_stats = stats.get("packs") or {}
        stats["packs"] = {k: v for k, v in packs_stats.items() if k == only}
        report["findings"] = [f for f in report.get("findings") or []
                              if only in f.get("subject", "")]
    if getattr(args, "json", False):
        print(packs_mod.to_json(report))
    else:
        print(packs_mod.render_text(report))
    return None


def cmd_verify(args) -> int | None:
    # Chain integrity is the tamper-evidence check; corrupt_rows is the loud,
    # explicit surfacing of any torn/malformed rows that read_all() skips to stay
    # total. Reporting both here is where "fail loud" now lives — instead of
    # crashing every reader on a single bad line.
    result = chain.verify()
    bad = events.corrupt_rows()
    if bad:
        result = {**result, "corrupt_rows": bad}
    print(json.dumps(result))
    return None


def cmd_recall(args) -> int | None:
    project = getattr(args, "project", None)
    if project is None and paths.is_freshly_minted():
        print(f"warning: no memory for detected project "
              f"'{paths.detect_project()}' — searching an empty namespace. Pass "
              f"--project or see `hive projects`.", file=sys.stderr)
    for h in recall_mod.recall(args.query, project=project, semantic=args.semantic,
                               limit=args.limit, force=args.force):
        score = h.get("score")
        tag = f" [score {score:g}]" if isinstance(score, (int, float)) else ""
        print(f"{h['file']}{tag}\n  {h['snippet']}")
    return None


def cmd_gc(args) -> int | None:
    from . import hygiene
    if not args.prune:
        print(hygiene.report())
        return None
    if not (args.target or args.orphans or args.graph_blobs):
        print("error: --prune requires a target: a namespace name, --orphans, "
              "or --graph-blobs", file=sys.stderr)
        return 2
    # Refuse destructive store ops while V17 is live-trading (defense in depth).
    from . import guard
    try:
        guard.ensure_safe(force=args.force)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    res = hygiene.prune(target=args.target, orphans=args.orphans,
                        graph_blobs=args.graph_blobs, force=args.force)
    if res.get("refused"):
        print(f"refused: {res['refused']}", file=sys.stderr)
        return 2
    print(json.dumps(res))
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


def cmd_log_add(args) -> int | None:
    from . import changelog as changelog_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    try:
        ev = changelog_mod.record(args.category, args.explanation,
                                  files=args.files or None, session_id=sid)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"logged {ev.payload['category']} covering "
          f"{len(ev.payload['files'])} file(s) @ {ev.ts}")
    return None


def cmd_log_queue(args) -> int | None:
    from . import changelog as changelog_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    marker = changelog_mod.queue(events.read_all(), session_id=sid)
    if marker:
        print(f"queued {len(marker['files'])} unlogged source file(s) — a "
              "change-log entry is required: hive log <category> \"…\"")
    else:
        print("no unlogged code changes")
    return None


def cmd_log_show(args) -> int | None:
    from . import changelog as changelog_mod
    print(json.dumps(changelog_mod.entries(events.read_all()),
                     indent=2, ensure_ascii=False))
    return None


def cmd_log_pending(args) -> int | None:
    from . import changelog as changelog_mod
    print(json.dumps(changelog_mod.load_pending(), indent=2))
    return None


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
    from . import sop as sop_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    try:
        text = (args.reason if action == "delete" else _sop_body(args))
        tags = [t for t in (getattr(args, "tags", "") or "").split(",") if t]
        ev = sop_mod.record(action, args.sop_id,
                            title=getattr(args, "title", None), text=text,
                            tags=tags, session_id=sid)
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
    from . import sop as sop_mod
    sops = sop_mod.current(events.read_all(getattr(args, "project", None)))
    if args.json:
        print(json.dumps(sops, indent=2, ensure_ascii=False))
        return None
    if not sops:
        print('(no active SOPs — add one: hive sop add <id> --title "…" '
              '--file <body.md>)')
        return None
    for i, r in enumerate(sops, 1):
        print(f"  {i}. [{r['sop_id']} v{r['version']}] {r['title']}  "
              f"(updated {r['updated']})")
    print(f"\n{len(sops)} active SOP(s)")
    return None


def cmd_sop_show(args) -> int | None:
    from . import sop as sop_mod
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


def _roadmap_steps(args) -> list:
    """Resolve `roadmap set` steps from repeatable --step flags plus an
    optional --file (one step per line) — multi-line plans are painful to
    quote on Windows, mirroring the sop --file escape hatch."""
    steps = list(args.step or [])
    if args.file:
        from pathlib import Path
        steps += [ln.strip()
                  for ln in Path(args.file).read_text(encoding="utf-8").splitlines()
                  if ln.strip()]
    return steps


def _cmd_roadmap_write(args, action) -> int | None:
    from . import roadmap as roadmap_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    try:
        kwargs = {}
        if action == "set":
            kwargs.update(prompt=args.prompt, title=args.title,
                          steps=_roadmap_steps(args))
        elif action == "step":
            kwargs.update(index=args.index, status=args.status,
                          note=args.note)
        elif action == "interrupt":
            kwargs.update(cause=args.cause, note=args.note)
        ev = roadmap_mod.record(action, args.plan_id, session_id=sid,
                                **kwargs)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"roadmap {action}: {args.plan_id} @ {ev.ts}")
    return None


def cmd_roadmap_set(args) -> int | None:
    return _cmd_roadmap_write(args, "set")


def cmd_roadmap_step(args) -> int | None:
    return _cmd_roadmap_write(args, "step")


def cmd_roadmap_interrupt(args) -> int | None:
    rc = _cmd_roadmap_write(args, "interrupt")
    if rc:
        return rc
    # Usage-shaped interruptions get the delivery leg automatically: the
    # marker is scheduled to re-enter Claude after the cooldown. Best-effort
    # — a scheduling problem must never turn a recorded interrupt into a
    # failure.
    from . import autoresume as autoresume_mod
    if args.no_auto_resume or not autoresume_mod.is_usage_cause(args.cause):
        return None
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    try:
        ev = autoresume_mod.schedule(args.plan_id, session_id=sid)
    except (ValueError, OSError) as e:
        print(f"note: auto-resume not scheduled: {e}", file=sys.stderr)
        return None
    p = ev.payload
    print(f"autoresume scheduled: fires {p['due']} (timer: {p['timer']})")
    if p["timer"] == "none":
        print("note: no OS timer registered — deliver with "
              f"`hive autoresume wait` or `hive autoresume fire "
              f"{args.plan_id}`")
    return None


def _cancel_pending_autoresume(args) -> None:
    """A human picked the plan back up (resume/complete): a still-pending
    auto-resume delivery would only fire-and-skip later — withdraw it and
    its OS timer. Silent when nothing is scheduled."""
    from . import autoresume as autoresume_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    try:
        autoresume_mod.cancel(args.plan_id, session_id=sid)
    except (ValueError, OSError):
        return
    print(f"autoresume cancelled: {args.plan_id}")


def cmd_roadmap_resume(args) -> int | None:
    rc = _cmd_roadmap_write(args, "resume")
    if rc:
        return rc
    _cancel_pending_autoresume(args)
    return None


def cmd_roadmap_complete(args) -> int | None:
    rc = _cmd_roadmap_write(args, "complete")
    if rc:
        return rc
    _cancel_pending_autoresume(args)
    return None


def cmd_roadmap_autoclose(args) -> int | None:
    """Close the session's auto-synced roadmap (run by the SessionEnd hook).
    Silent no-op when the session has no auto-owned open plan — the hook must
    never fail or chatter."""
    from . import roadmap as roadmap_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    if not sid:
        return None
    ev = roadmap_mod.autoclose_session(sid)
    if ev is not None:
        print(f"roadmap {ev.payload['action']}: {sid} @ {ev.ts}")
    return None


def cmd_roadmap_list(args) -> int | None:
    from . import roadmap as roadmap_mod
    recs = roadmap_mod.plans(events.read_all(getattr(args, "project", None)),
                             include_completed=args.all)
    if args.json:
        print(json.dumps(recs, indent=2, ensure_ascii=False))
        return None
    if not recs:
        print('(no roadmaps — declare one: hive roadmap set <id> '
              '--prompt "…" --step "…")')
        return None
    marks = {"active": ">", "interrupted": "!", "completed": "x"}
    for i, r in enumerate(recs, 1):
        p = r["progress"]
        print(f"  {i}. {marks.get(r['status'], '?')} "
              f"[{r['plan_id']} v{r['version']}] "
              f"{r['title'] or r['prompt'][:60]}  "
              f"{p['done']}/{p['total']} done  ({r['status']})")
    print(f"\n{len(recs)} roadmap(s)")
    return None


def cmd_roadmap_show(args) -> int | None:
    from . import roadmap as roadmap_mod
    evs = events.read_all(getattr(args, "project", None))
    rec = roadmap_mod.get(evs, args.plan_id)
    hist = [{"ts": e.ts, "session": e.session_id,
             "action": (e.payload or {}).get("action", "")}
            for e in evs
            if e.type == "roadmap" and isinstance(e.payload, dict)
            and e.payload.get("plan_id") == args.plan_id]
    if args.json:
        print(json.dumps({"plan": rec, "history": hist},
                         indent=2, ensure_ascii=False))
        return None
    if rec is None:
        print(f"error: no roadmap '{args.plan_id}' in the ledger",
              file=sys.stderr)
        return 2
    p = rec["progress"]
    print(f"roadmap {rec['plan_id']} v{rec['version']} — "
          f"{rec['title'] or '(untitled)'}  [{rec['status']}]")
    print(f"asked: {rec['prompt']}")
    if rec["status"] == "interrupted":
        print(f"interrupted {rec['interrupted_at']} (cause: {rec['cause']})")
    print(f"progress: {p['done']}/{p['total']} steps done\n")
    glyphs = {"done": "[x]", "active": "[>]", "pending": "[ ]",
              "skipped": "[-]"}
    for i, s in enumerate(rec["steps"]):
        note = f"  — {s['note']}" if s.get("note") else ""
        print(f"  {i}. {glyphs[s['status']]} {s['text']}{note}")
    print("\nhistory (oldest first):")
    for e in hist:
        print(f"  {e['ts']}  {e['action']:9s}  session {e['session']}")
    return None


def cmd_autoresume_schedule(args) -> int | None:
    from . import autoresume as autoresume_mod
    from . import roadmap as roadmap_mod
    project = getattr(args, "project", None)
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    evs = events.read_all(project)
    plan_id = args.plan_id
    if plan_id is None:
        # Bare `hive autoresume schedule` targets the freshest interruption —
        # the "my session just died" one-liner.
        marks = roadmap_mod.interrupted(evs)
        if not marks:
            print("error: no interrupted roadmap to auto-resume "
                  "(hive roadmap list)", file=sys.stderr)
            return 2
        plan_id = sorted(marks,
                         key=lambda r: r["interrupted_at"])[-1]["plan_id"]
    try:
        ev = autoresume_mod.schedule(
            plan_id, project=project, evs=evs, cooldown_hours=args.in_hours,
            at=args.at, model=args.model, cwd=args.cwd, session_id=sid)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    p = ev.payload
    print(f"autoresume scheduled: {plan_id} -> fires {p['due']} "
          f"(attempt {p['attempt']}, timer: {p['timer']})")
    if p["timer"] == "none":
        print("note: no OS timer registered — deliver with "
              f"`hive autoresume wait` or `hive autoresume fire {plan_id}`")
    return None


def cmd_autoresume_list(args) -> int | None:
    from . import autoresume as autoresume_mod
    recs = sorted(
        autoresume_mod.fold(events.read_all(getattr(args, "project",
                                                    None))).values(),
        key=lambda r: (r["updated"], r["plan_id"]))
    if args.json:
        print(json.dumps(recs, indent=2, ensure_ascii=False))
        return None
    if not recs:
        print("(no auto-resume deliveries — schedule one: "
              "hive autoresume schedule <plan_id>)")
        return None
    marks = {"scheduled": "!", "fired": "x", "cancelled": "-"}
    for i, r in enumerate(recs, 1):
        if r["status"] == "scheduled":
            tail = f"fires {r['due']} (attempt {r['attempt']}, " \
                   f"timer {r['timer']})"
        elif r["status"] == "fired":
            tail = f"{r['outcome']} @ {r['updated']}"
        else:
            tail = f"cancelled @ {r['updated']}"
        print(f"  {i}. {marks.get(r['status'], '?')} [{r['plan_id']}] {tail}")
    print(f"\n{len(recs)} delivery record(s)")
    return None


def cmd_autoresume_cancel(args) -> int | None:
    from . import autoresume as autoresume_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    try:
        ev = autoresume_mod.cancel(args.plan_id,
                                   project=getattr(args, "project", None),
                                   session_id=sid)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"autoresume cancelled: {args.plan_id} @ {ev.ts}")
    return None


def cmd_autoresume_fire(args) -> int | None:
    from . import autoresume as autoresume_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    payload = autoresume_mod.fire(args.plan_id,
                                  project=getattr(args, "project", None),
                                  session_id=sid, timeout_s=args.timeout)
    print(f"autoresume fired: {args.plan_id} -> {payload['outcome']}"
          + (f" — {payload['detail']}" if payload.get("detail") else ""))
    return 1 if payload["outcome"] == "error" else None


def cmd_autoresume_wait(args) -> int | None:
    from . import autoresume as autoresume_mod
    fired = autoresume_mod.wait(getattr(args, "project", None),
                                poll_s=args.poll)
    print(f"autoresume wait: {fired} delivery(ies) fired")
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


def cmd_skills_list(args) -> int | None:
    from . import plugins as plugins_mod
    res = plugins_mod.live_inventory()
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
        return None
    inv = res["inventory"]
    c = inv["counts"]
    print(f"{c['plugins']} plugins · {c['skills_total']} skills "
          f"({c['local_skills']} local)")
    for p in inv["plugins"]:
        flag = "" if p.get("present", True) else "  [MISSING ON DISK]"
        print(f"  {p['marketplace']}/{p['name']} {p['version']} "
              f"· {len(p['skills'])} skills{flag}")
    return None


def cmd_skills_invoke(args) -> int | None:
    """Call ANY installed skill through MindHive (headless claude), recording the
    outcome. Returns exit 1 on failure so /circle and /mindhive-orchestrator can gate on it."""
    from . import invoker
    res = invoker.invoke_skill(args.name, plugin=args.plugin, project=args.project)
    status = "ok" if res["ok"] else "FAILED"
    print(f"skill {args.name}: {status} (exit {res['exit']})")
    if res.get("excerpt"):
        print(res["excerpt"])
    return 0 if res["ok"] else 1


def cmd_skills_status(args) -> int | None:
    from . import plugins as plugins_mod
    status = plugins_mod.call_status()
    if not status:
        print("(no skill calls recorded yet)")
    for name, s in sorted(status.items()):
        print(f"  {name}: {s}")
    return None


def cmd_skills_stats(args) -> int | None:
    from . import plugins as plugins_mod
    stats = plugins_mod.skill_stats(project=args.project)
    if args.json:
        print(json.dumps(stats, indent=2))
        return None
    if not stats:
        print("(no skill telemetry yet)")
        return None
    for name, s in sorted(stats.items()):
        rate = (f"{s['completion_rate'] * 100:.0f}%"
                if s["completion_rate"] is not None else "-")
        print(f"  {name}: {s['calls']} calls · {s['ok']} ok · {s['fail']} fail "
              f"· completion {rate} · last {s['last_outcome']} @ {s['last_used']}")
    return None


def cmd_skills_record(args) -> int | None:
    """Explicit skill-outcome write-through for the orchestrator/agent (KTD5):
    --ok appends an invoke_result, --fail a tool_failed with a skill:<name>
    signature (feeding the incident pipeline unchanged). Both carry
    agent-assessed provenance so self-grades never masquerade as measured."""
    note = args.note or ""
    if args.ok:
        ev = events.append_event(
            "invoke_result",
            {"name": args.name, "plugin": args.plugin, "ok": True, "text": note,
             "provenance": "agent-assessed"},
            tags=["skill", "record", "agent-assessed"], project=args.project)
        print(f"recorded ok for {args.name} @ {ev.ts}")
    else:
        signature = f"skill:{args.name}"
        ev = events.append_event(
            "tool_failed",
            {"text": note or f"skill {args.name} failed (agent-assessed)",
             "signature": signature,
             "action": f"skills record {args.name} --fail",
             "provenance": "agent-assessed"},
            tags=["skill", "record", "agent-assessed", "failure"],
            project=args.project)
        print(f"recorded fail for {args.name} ({signature}) @ {ev.ts}")
    return None


def cmd_skills_recommend(args) -> int | None:
    """Deterministic most-needed-skills ranking (KTD6). One ledger read feeds
    both the telemetry stats and the need extraction, so --brief stays cheap
    enough for a session-start hook."""
    from . import plugins as plugins_mod
    from . import recommend as recommend_mod
    payload = plugins_mod.recommend_for(project=args.project, top=args.top)
    if args.json:
        print(json.dumps(payload, indent=2))
        return None
    recs = payload["recommendations"]
    if args.brief:
        for e in recs[:(args.top or 3)]:
            print(recommend_mod.brief_line(e))
        return None
    cov = payload["coverage"]
    print(f"measured {cov['measured']} of {cov['total']} skills")
    if not recs:
        print("(no skills in inventory)")
        return None
    for i, e in enumerate(recs, 1):
        print(f"  {i}. {e['name']}  [score {e['score']:g}, {e['provenance']}]"
              f"{'  (try to measure)' if e.get('try_to_measure') else ''}")
        print(f"       {e['reason']}")
        # Full width (no truncation) but the SAME empty/whitespace handling as
        # brief_line, so an empty description never prints a dangling clause.
        use = recommend_mod._truncate(e.get("description"))
        if use:
            print(f"       intended use: {use}")
        for a in e.get("alternatives", []):
            flag = " — try to measure" if a.get("try_to_measure") else ""
            print(f"       alt: {a['name']} [{a['provenance']}]{flag}")
    return None


def cmd_skills_trail(args) -> int | None:
    """Per-session skill trail from the ledger — the durable view of which
    skills a session used, when, and why (args / prompt-gate direction / the
    skill's intended use). Reuses oversee.skill_trails so the CLI, the read
    API, and the live dashboard cards can never disagree."""
    from . import oversee as oversee_mod
    from . import paths
    iu = oversee_mod.intended_use_map()
    if getattr(args, "all", False):
        home = paths.store_home()
        names = ([d.name for d in sorted(home.iterdir())
                  if d.is_dir() and (d / "events").is_dir()]
                 if home.exists() else [])
    else:
        names = [args.project]
    trails = {}
    for n in names:
        for sid, entries in oversee_mod.skill_trails(project=n,
                                                     intended_use=iu).items():
            if args.session and sid != args.session:
                continue
            trails[sid] = entries
    if args.json:
        print(json.dumps(trails, indent=2))
        return None
    # newest session first, keyed on its last recorded skill
    ordered = sorted(trails.items(),
                     key=lambda kv: (kv[1][-1]["ts"] if kv[1] else ""),
                     reverse=True)
    if not any(entries for _, entries in ordered):
        print("(no skill activity recorded yet)")
        return None
    for sid, entries in ordered:
        if not entries:
            continue
        print(f"session {sid[:12]} · {len(entries)} skill(s)")
        for e in entries:
            mark = "ok  " if e["ok"] else "FAIL"
            name = (e["plugin"] + ":" if e["plugin"] else "") + e["name"]
            line = f"  {e['ts'][11:16]} {mark} {name}"
            why = e["why"] or (f"during: {e['during']}" if e["during"] else "")
            if why:
                line += f"  — {why}"
            print(line)
    return None


def cmd_clusters_build(args) -> int | None:
    """Build the cluster DB (parallel-orchestration layer) and store the sidecar."""
    from . import clusters as clusters_mod
    payload = clusters_mod.build_and_store(root=args.root, lanes=args.lanes,
                                           use_git=args.git)
    c = payload["counts"]
    print(f"clustered {c['files']} files into {c['clusters']} clusters "
          f"-> {c['lanes']} lane(s); stored cluster_db.json")
    return None


def cmd_clusters_show(args) -> int | None:
    from . import clusters as clusters_mod
    payload = clusters_mod.latest()
    if not payload:
        print("(no cluster_db yet - run: hive clusters build)")
        return None
    c = payload["counts"]
    print(f"cluster DB from {payload.get('generated_at', '?')} "
          f"({c['files']} files, {c['clusters']} clusters)")
    for key, info in payload.get("clusters", {}).items():
        print(f"  {key}  [weight {info['weight']}, {len(info['files'])} files]")
    return None


def cmd_clusters_plan(args) -> int | None:
    """Print the file-ownership-safe shard plan the orchestrator fans out to
    parallel sub-agents. Computes fresh (does not require a stored DB)."""
    from . import clusters as clusters_mod
    payload = clusters_mod.build(root=args.root, lanes=args.lanes, use_git=args.git)
    plan = payload["shard_plan"]
    print(f"{len(plan)} parallel lane(s) for {payload['counts']['files']} files:")
    for lane in plan:
        print(f"  lane {lane['lane']}: {len(lane['files'])} files across "
              f"{len(lane['clusters'])} cluster(s) "
              f"[est_complexity {lane['est_complexity']}]")
        for key in lane["clusters"]:
            print(f"      - {key}")
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
        from .loop import router as loop_router
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
    from . import guard as _guard
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
            from . import dashboard as _dashboard
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


def cmd_flag(args) -> int | None:
    """Self-flag a judgment error (KTD4): appends a tool_failed event carrying a
    judgment:flag:<slug> signature, so the entire existing incident projection
    works unchanged — no new event type. Flags activate at occurrence 1."""
    from . import incidents as incidents_mod, signature as sig_mod
    signature = f"judgment:flag:{sig_mod.slug_id(args.text)}"
    tags = ["judgment"] + ([args.category] if args.category else [])
    ev = events.append_event(
        "tool_failed",
        {"text": args.text, "signature": signature, "action": "hive flag"},
        tags=tags, project=args.project)
    # Advance the lifecycle immediately (mirrors failures capture): flags cross
    # their threshold of 1 on the spot, so the incident is active right away.
    incidents_mod.audit(project=args.project)
    incidents_mod.project(project=args.project)
    print(f"flagged {signature} @ {ev.ts}")
    return None


def cmd_consolidate(args) -> int | None:
    """One bounded memory-consolidation ("dreaming") pass: merge near-duplicate
    semantic memories into survivors, exclude the absorbed originals from the
    briefing/MEMORY.md digest, keep everything in the append-only chain."""
    from . import consolidate as consolidate_mod
    try:
        res = consolidate_mod.consolidate(
            project=args.project, limit=args.limit,
            cooldown_hours=args.cooldown_hours, force=args.force)
    except RuntimeError as e:       # LIVE_LOCK guard
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(res))
    return None


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
        status = ("active" if rec["occurrences"] >= incidents_mod._threshold_for(signature)
                  else "candidate")
        print(f"{rec['occurrences']:>3}x  [{status:9}] {signature}")
    return None


def cmd_rules_list(args) -> int | None:
    from . import incidents as inc
    acc = inc._collect(args.project)
    if not acc:
        print("(no incidents yet)")
        return None
    for signature, rec in sorted(acc.items(), key=lambda kv: -kv[1]["occurrences"]):
        status = ("active" if rec["occurrences"] >= inc._threshold_for(signature)
                  else "candidate")
        print(f"{rec['occurrences']:>3}x  [{status:9}] {signature}")
    return None


def cmd_rules_summaries(args) -> int | None:
    """Structured lesson records (the same shape the dashboard Improvements view
    reads) for an agent working from the CLI. --json emits the raw list; the
    default is a human-readable digest per signature. Both carry resolution +
    triage (who should act: claude vs dismiss) — CLI/HTTP parity with the
    dashboard's Improvements cards."""
    from . import events as ev_mod
    from . import incidents as inc
    stats = inc.signal_stats(args.project, evs=ev_mod.read_all(args.project))
    recs = []
    for r in inc.summaries(args.project):
        st = stats.get(r["signature"], {})
        resolution = inc.classify_resolution(r, st)
        recs.append(dict(r, resolution=resolution,
                         **inc.classify_triage(resolution, r)))
    if args.json:
        print(json.dumps(recs, indent=2))
        return None
    if not recs:
        print("(no incidents yet)")
        return None
    marks = {"claude": "-> FIX WITH CLAUDE", "dismiss": "-> ok to dismiss", "": ""}
    for r in sorted(recs, key=lambda r: -r.get("occurrences", 0)):
        lesson = r.get("lesson") or r.get("active_rule") or "(not yet reflected)"
        cat = f" {{{r['category']}}}" if r.get("category") else ""
        print(f"{r.get('occurrences', 0):>3}x  [{r.get('status', '?'):9}]{cat} "
              f"{r.get('signature', '')}  {marks.get(r.get('triage'), '')}"
              f"\n       {lesson}")
    return None


def cmd_rules_show(args) -> int | None:
    from . import incidents as inc
    path = inc._incident_dir(args.project) / f"{inc._safe_name(args.signature)}.md"
    print(path.read_text(encoding="utf-8") if path.exists() else "(no such incident)")
    return None


def _safe_reflect_batch(reflect_mod, project, args) -> dict:
    """One namespace's bounded reflection pass, never raising — session_end
    spawns this command fire-and-forget, so a failing runner must map to a JSON
    result and exit 0, not a traceback."""
    try:
        return reflect_mod.reflect_batch(project=project, limit=args.limit,
                                         cooldown_hours=args.cooldown_hours)
    except Exception as e:
        return {"reflected": 0, "attempted": 0,
                "error": f"{type(e).__name__}: {e}"}


def cmd_rules_reflect(args) -> int | None:
    from . import reflect as reflect_mod
    if args.all_projects:
        # One-time supervised backlog drain over registered namespaces — run
        # manually, never from hooks. LIVE_LOCK is honored inside reflect_batch
        # (whole-token namespace match), so both entry points share one check.
        results = {name: _safe_reflect_batch(reflect_mod, name, args)
                   for name in sorted(paths.known_projects())}
        print(json.dumps(results))
        return 0
    print(json.dumps(_safe_reflect_batch(reflect_mod, args.project, args)))
    return 0


def cmd_rules_resolve(args) -> int | None:
    """Active resolution over the red (unresolved) set: reflect rule-less
    incidents, reroute leaking auto rules to the best alternative route.
    --list prints the red set without spending any tokens."""
    from . import resolve as resolve_mod
    if args.list:
        from . import incidents as inc
        inc.audit(project=args.project)
        inc.project(project=args.project)
        print(json.dumps(resolve_mod.red_summaries(args.project), indent=2))
        return 0
    try:
        res = resolve_mod.resolve_batch(project=args.project, limit=args.limit)
    except Exception as e:              # hook-safe: JSON + exit 0, never a traceback
        res = {"rerouted": 0, "reflected": 0,
               "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(res))
    return 0


def cmd_rules_reconcile(args) -> int | None:
    from . import incidents as inc
    print(json.dumps(inc.reconcile(project=args.project)))
    return None


def cmd_rules_enforce(args) -> int | None:
    """Manage the PreToolUse enforcement manifest — the enforce rung of the
    nudge->enforce ladder. Default action prints the manifest."""
    from . import enforce
    if args.suggest:
        print(json.dumps({"suggested": enforce.suggest(project=args.project)}))
        return None
    if args.enable:
        print(json.dumps(enforce.set_enabled(args.project, args.enable, True)))
        return None
    if args.disable:
        print(json.dumps(enforce.set_enabled(args.project, args.disable, False)))
        return None
    man = enforce.load_manifest(args.project)
    if not man:
        print("(no enforcement manifest — run `hive rules enforce --suggest`)")
        return None
    for sig, entry in sorted(man.items()):
        state = "ENABLED " if entry.get("enabled") else "disabled"
        matcher = "ok" if sig in enforce.MATCHERS else "MISSING-matcher"
        print(f"  [{state}] {sig}  ({matcher})")
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


def cmd_triggers_train(args) -> int | None:
    from . import promptgate
    res = promptgate.train_and_store(project=args.project,
                                     max_age_hours=args.max_age_hours)
    cov = res["manifest"].get("coverage", {})
    verb = "trained" if res["trained"] else \
        f"fresh (age {res['age_hours']:.1f}h) — kept"
    print(f"{verb} {cov.get('rules', 0)} trigger rule(s) "
          f"({cov.get('measured_rules', 0)} measured) from "
          f"{cov.get('skills_total', 0)} skills, "
          f"{cov.get('prompts_scanned', 0)} past prompts scanned")
    return None


def cmd_triggers_list(args) -> int | None:
    from . import promptgate
    man = promptgate.load_manifest(args.project)
    if args.json:
        print(json.dumps(man, indent=2))
        return None
    rules = man.get("rules") or {}
    if not rules:
        print("(no trigger manifest — run `hive triggers train`)")
        return None
    print(f"trained {man.get('trained_at', '?')}")
    for dim, r in sorted(rules.items()):
        rate = r.get("completion_rate")
        rate_s = f"{r.get('ok', 0)}/{r.get('calls', 0)}" if rate is not None else "—"
        print(f"  [{r.get('directive', 'try'):4}] {dim}: {r.get('skill', '')} "
              f"({r.get('provenance', '')}, {rate_s}, "
              f"observed {r.get('observed', 0)}x in history)")
    return None


def cmd_triggers_check(args) -> int | None:
    from . import promptgate
    verdict = promptgate.assess(args.prompt)
    if verdict["skip"]:
        print("skipped: not a human-typed prompt")
        return None
    if verdict["clear"]:
        print("clear: the gate would not fire")
        return None
    print("fires: " + "; ".join(f["dimension"] for f in verdict["findings"]))
    man = promptgate.load_manifest(args.project)
    context = promptgate.render_context(verdict["findings"], man.get("rules"))
    print(context if context else "(no trained rule covers these dimensions)")
    return None


def cmd_enrich(args) -> int | None:
    from . import enrich as enrich_mod
    if args.enrich_cmd == "status":
        st = enrich_mod.settings(args.project)
        print(f"prompt enrichment: "
              f"{'enabled' if st['enabled'] else 'disabled'} "
              f"({st['source']}"
              + (f", updated {st['updated']}" if st.get("updated") else "")
              + ")")
    elif args.enrich_cmd in ("on", "off"):
        st = enrich_mod.set_enabled(args.enrich_cmd == "on",
                                    project=args.project)
        print(f"prompt enrichment "
              f"{'enabled' if st['enabled'] else 'disabled'}")
    elif args.enrich_cmd == "preview":
        res = enrich_mod.enrich(args.prompt, project=args.project)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(res["enriched_prompt"])
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
    hk.add_argument("stage",
                    choices=["post_tool_use", "post_tool_use_failure",
                             "pre_tool_use", "user_prompt_submit"],
                    help="which hook fired")
    hk.set_defaults(func=cmd_hook)

    sub.add_parser("project", help="rebuild projections").set_defaults(func=cmd_project)
    st = sub.add_parser("state", help="print STATE.md")
    st.add_argument("--project", default=None,
                    help="target namespace explicitly (default: detect from cwd)")
    st.set_defaults(func=cmd_state)
    sub.add_parser("projects", help="list namespaces (registered vs unregistered)").set_defaults(func=cmd_projects)
    sub.add_parser("directives", help="print the session operating directives").set_defaults(func=cmd_directives)

    cx = sub.add_parser("context",
                        help="measure the auto-loaded context budget (read-only)")
    cx.add_argument("--project", default=None,
                    help="target namespace explicitly (default: detect from cwd)")
    cx.add_argument("--json", action="store_true", help="machine-readable report")
    cx.add_argument("--budget", type=int, default=None,
                    help="compare against a one-off token budget "
                         "(overrides the persisted one)")
    cx.add_argument("--set-budget", type=int, default=None, dest="set_budget",
                    help="persist the namespace's token budget and exit")
    cx.add_argument("--check", action="store_true",
                    help="exit 1 when the measured total exceeds the budget")
    cx.add_argument("--top", type=int, default=None,
                    help="show only the N heaviest contributors")
    cx.add_argument("--origin", choices=["hive", "claude", "all"], default="all",
                    help="limit the report to one origin (default: all)")
    cx.set_defaults(func=cmd_context)

    # --- packs (stack content pack quality gates) ---
    pk = sub.add_parser("packs",
                        help="stack content pack quality gates (read-only)")
    pksub = pk.add_subparsers(dest="packs_cmd", required=True)
    pkc = pksub.add_parser("check",
                           help="run all gates; exit 1 on error findings")
    pkc.add_argument("--manifest", default=None,
                     help="path to packs.json "
                          "(default: <git root>/docs/packs/packs.json)")
    pkc.add_argument("--json", action="store_true",
                     help="machine-readable report")
    pkc.set_defaults(func=cmd_packs_check)
    pkr = pksub.add_parser("report",
                           help="full stats: depth, similarity matrices, "
                                "findings (always exits 0)")
    pkr.add_argument("--manifest", default=None,
                     help="path to packs.json "
                          "(default: <git root>/docs/packs/packs.json)")
    pkr.add_argument("--json", action="store_true",
                     help="machine-readable report")
    pkr.add_argument("--pack", default=None, help="limit output to one pack")
    pkr.set_defaults(func=cmd_packs_report)

    sub.add_parser("verify", help="verify hash chain").set_defaults(func=cmd_verify)
    sub.add_parser("selftest", help="run test suite").set_defaults(func=cmd_selftest)

    r = sub.add_parser("recall", help="search ledger")
    r.add_argument("query")
    r.add_argument("--project", default=None,
                   help="target namespace explicitly (default: detect from cwd)")
    r.add_argument("--semantic", action="store_true")
    r.add_argument("--limit", type=int, default=10)
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_recall)

    cons = sub.add_parser("consolidate",
                          help="dreaming pass: merge near-duplicate memories "
                               "into survivors (append-only, bounded)")
    cons.add_argument("--project", default=None)
    cons.add_argument("--limit", type=int, default=3,
                      help="max duplicate clusters merged per pass")
    cons.add_argument("--cooldown-hours", dest="cooldown_hours", type=float,
                      default=6.0, help="skip when a pass ran within this window "
                                        "(0 disables)")
    cons.add_argument("--force", action="store_true",
                      help="override the LIVE_LOCK guard")
    cons.set_defaults(func=cmd_consolidate)

    gc = sub.add_parser("gc", help="store hygiene: report sizes; prune junk (opt-in)")
    gc.add_argument("target", nargs="?", default=None,
                    help="namespace to prune (requires --prune)")
    gc.add_argument("--prune", action="store_true",
                    help="delete the target (default: report only)")
    gc.add_argument("--orphans", action="store_true",
                    help="with --prune: remove all unregistered namespaces")
    gc.add_argument("--graph-blobs", dest="graph_blobs", action="store_true",
                    help="with --prune: delete graph.json/graph.html blobs only")
    gc.add_argument("--force", action="store_true",
                    help="override the registered-namespace / LIVE_LOCK guard")
    gc.set_defaults(func=cmd_gc)

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

    # --- log (mandatory code change-log) ---
    from .changelog import CATEGORIES as _LOG_CATS
    lg = sub.add_parser("log", help="mandatory code change-log (code_change events)")
    lgsub = lg.add_subparsers(dest="log_cmd", required=True)
    for _cat in _LOG_CATS:
        lc = lgsub.add_parser(_cat, help=f"record a {_cat} entry for the current change-set")
        lc.add_argument("explanation", help="brief explanation of what changed (mandatory)")
        lc.add_argument("--files", nargs="*", default=None,
                        help="affected files (default: pending marker, then session, then git)")
        lc.add_argument("--session", default=None,
                        help="session id (default: $MINDHIVE_SESSION)")
        lc.set_defaults(func=cmd_log_add, category=_cat)
    lgq = lgsub.add_parser("queue", help="queue this session's unlogged source files (run by session_end)")
    lgq.add_argument("--session", default=None,
                     help="session id to queue (default: $MINDHIVE_SESSION)")
    lgq.set_defaults(func=cmd_log_queue)
    lgsub.add_parser("show", help="print change-log entries, newest first").set_defaults(func=cmd_log_show)
    lgsub.add_parser("pending", help="print the pending unlogged-changes marker, if any").set_defaults(func=cmd_log_pending)

    # --- sop (managed standard operating procedures) ---
    so = sub.add_parser("sop", help="standard operating procedures "
                                    "(event-sourced registry, auto-loaded)")
    sosub = so.add_subparsers(dest="sop_cmd", required=True)
    soa = sosub.add_parser("add", help="add a new SOP")
    soa.add_argument("sop_id", help="stable kebab-case id, e.g. memo-hygiene")
    soa.add_argument("--title", required=True)
    soa.add_argument("--text", default=None, help="procedure body (inline)")
    soa.add_argument("--file", default=None, help="read the body from a file")
    soa.add_argument("--stdin", action="store_true",
                     help="read the body from stdin")
    soa.add_argument("--tags", default="")
    soa.add_argument("--session", default=None)
    soa.set_defaults(func=cmd_sop_add)
    sou = sosub.add_parser("update", help="revise an SOP's title and/or body")
    sou.add_argument("sop_id")
    sou.add_argument("--title", default=None)
    sou.add_argument("--text", default=None)
    sou.add_argument("--file", default=None)
    sou.add_argument("--stdin", action="store_true")
    sou.add_argument("--tags", default="")
    sou.add_argument("--session", default=None)
    sou.set_defaults(func=cmd_sop_update)
    sod = sosub.add_parser("delete", help="retire an SOP (append-only tombstone)")
    sod.add_argument("sop_id")
    sod.add_argument("--reason", default=None)
    sod.add_argument("--session", default=None)
    sod.set_defaults(func=cmd_sop_delete)
    sol = sosub.add_parser("list", help="list active SOPs")
    sol.add_argument("--project", default=None)
    sol.add_argument("--json", action="store_true")
    sol.set_defaults(func=cmd_sop_list)
    sos = sosub.add_parser("show", help="print one SOP in full, with history")
    sos.add_argument("sop_id")
    sos.add_argument("--project", default=None)
    sos.add_argument("--json", action="store_true")
    sos.set_defaults(func=cmd_sop_show)

    # --- roadmap (event-sourced plan registry + interruption markers) ---
    from .roadmap import STATUSES as _RM_STATUSES
    rm = sub.add_parser("roadmap", help="roadmaps: the ask, the plan, and "
                        "per-step progress (interrupted plans surface in "
                        "STATE.md and Oversee)")
    rmsub = rm.add_subparsers(dest="roadmap_cmd", required=True)
    rms = rmsub.add_parser("set", help="declare (or replace) a plan")
    rms.add_argument("plan_id", help="stable kebab-case id, e.g. a run id")
    rms.add_argument("--prompt", required=True,
                     help="the original ask this plan was built from")
    rms.add_argument("--title", default=None)
    rms.add_argument("--step", action="append", default=None,
                     help="one plan step (repeatable, in order)")
    rms.add_argument("--file", default=None,
                     help="read steps from a file, one per line")
    rms.add_argument("--session", default=None)
    rms.set_defaults(func=cmd_roadmap_set)
    rmp = rmsub.add_parser("step", help="update one step's status")
    rmp.add_argument("plan_id")
    rmp.add_argument("index", type=int, help="0-based step index")
    rmp.add_argument("--status", required=True, choices=_RM_STATUSES)
    rmp.add_argument("--note", default=None)
    rmp.add_argument("--session", default=None)
    rmp.set_defaults(func=cmd_roadmap_step)
    rmi = rmsub.add_parser("interrupt", help="mark the plan interrupted "
                           "(records the out-of-usage marker; usage-shaped "
                           "causes auto-schedule the delivery back into "
                           "Claude)")
    rmi.add_argument("plan_id")
    rmi.add_argument("--cause", required=True,
                     help="why work stopped, e.g. usage_limit")
    rmi.add_argument("--note", default=None)
    rmi.add_argument("--session", default=None)
    rmi.add_argument("--no-auto-resume", action="store_true",
                     dest="no_auto_resume",
                     help="record the marker only; do not schedule the "
                     "post-cooldown delivery")
    rmi.set_defaults(func=cmd_roadmap_interrupt)
    rmr = rmsub.add_parser("resume", help="pick an interrupted plan back up")
    rmr.add_argument("plan_id")
    rmr.add_argument("--session", default=None)
    rmr.set_defaults(func=cmd_roadmap_resume)
    rmc = rmsub.add_parser("complete", help="mark the plan finished")
    rmc.add_argument("plan_id")
    rmc.add_argument("--session", default=None)
    rmc.set_defaults(func=cmd_roadmap_complete)
    rma = rmsub.add_parser("autoclose", help="close a session's auto-synced "
                           "roadmap (run by the SessionEnd hook): complete "
                           "when all steps are done/skipped, else interrupt")
    rma.add_argument("--session", default=None,
                     help="session id (defaults to MINDHIVE_SESSION)")
    rma.set_defaults(func=cmd_roadmap_autoclose)
    rml = rmsub.add_parser("list", help="list roadmaps")
    rml.add_argument("--project", default=None)
    rml.add_argument("--json", action="store_true")
    rml.add_argument("--all", action="store_true",
                     help="include completed roadmaps")
    rml.set_defaults(func=cmd_roadmap_list)
    rmw = rmsub.add_parser("show", help="print one roadmap in full, "
                           "with history")
    rmw.add_argument("plan_id")
    rmw.add_argument("--project", default=None)
    rmw.add_argument("--json", action="store_true")
    rmw.set_defaults(func=cmd_roadmap_show)

    # --- autoresume (deliver the interruption marker back into Claude) ---
    ar = sub.add_parser("autoresume", help="auto-resume: after the usage "
                        "cooldown, deliver an interrupted plan's marker "
                        "(the ask, progress, next steps) straight back "
                        "into Claude")
    arsub = ar.add_subparsers(dest="autoresume_cmd", required=True)
    ars = arsub.add_parser("schedule", help="schedule the delivery "
                           "(default: interruption time + 5h)")
    ars.add_argument("plan_id", nargs="?", default=None,
                     help="roadmap id (default: the latest interrupted plan)")
    ars.add_argument("--in-hours", type=float, default=None, dest="in_hours",
                     help="cooldown hours from the interruption (default: 5)")
    ars.add_argument("--at", default=None,
                     help="explicit UTC due time, e.g. 2026-07-18T15:00:00Z")
    ars.add_argument("--model", default=None,
                     help="model for the delivered session (default: the "
                     "CLI's configured model)")
    ars.add_argument("--cwd", default=None,
                     help="where the resumed work runs (default: here)")
    ars.add_argument("--project", default=None)
    ars.add_argument("--session", default=None)
    ars.set_defaults(func=cmd_autoresume_schedule)
    arl = arsub.add_parser("list", help="list delivery records")
    arl.add_argument("--project", default=None)
    arl.add_argument("--json", action="store_true")
    arl.set_defaults(func=cmd_autoresume_list)
    arc = arsub.add_parser("cancel", help="withdraw a pending delivery "
                           "and its OS timer")
    arc.add_argument("plan_id")
    arc.add_argument("--project", default=None)
    arc.add_argument("--session", default=None)
    arc.set_defaults(func=cmd_autoresume_cancel)
    arf = arsub.add_parser("fire", help="deliver the marker now (what the "
                           "OS timer runs at the due time)")
    arf.add_argument("plan_id")
    arf.add_argument("--timeout", type=int, default=None,
                     help="seconds the delivered session may run "
                     "(default: 3600)")
    arf.add_argument("--project", default=None)
    arf.add_argument("--session", default=None)
    arf.set_defaults(func=cmd_autoresume_fire)
    arw = arsub.add_parser("wait", help="portable fallback: block until "
                           "each pending delivery is due, then fire it")
    arw.add_argument("--poll", type=float, default=60.0,
                     help="max seconds between due checks (default: 60)")
    arw.add_argument("--project", default=None)
    arw.set_defaults(func=cmd_autoresume_wait)

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

    # --- skills: the invoke channel /mindhive-orchestrator and /circle drive ---
    sk = sub.add_parser("skills", help="list, invoke, or check status of installed skills")
    sksub = sk.add_subparsers(dest="skills_cmd", required=True)
    skl = sksub.add_parser("list", help="list installed plugins + skills (live)")
    skl.add_argument("--json", action="store_true", help="emit the raw inventory JSON")
    skl.set_defaults(func=cmd_skills_list)
    ski = sksub.add_parser("invoke", help="call a skill via headless claude and record the outcome")
    ski.add_argument("name", help="skill name (as shown by `skills list`)")
    ski.add_argument("--plugin", default=None, help="owning plugin, when disambiguation is needed")
    ski.add_argument("--project", default=None, help="target namespace")
    ski.set_defaults(func=cmd_skills_invoke)
    sksub.add_parser("status", help="green/red last-call outcome per skill").set_defaults(func=cmd_skills_status)
    sks = sksub.add_parser("stats", help="per-skill completion-rate telemetry from the ledger")
    sks.add_argument("--json", action="store_true", help="emit the raw stats JSON")
    sks.add_argument("--project", default=None, help="target namespace")
    sks.set_defaults(func=cmd_skills_stats)
    skr = sksub.add_parser("record",
                           help="record a skill outcome (agent-assessed write-through)")
    skr.add_argument("name", help="skill name")
    outcome = skr.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--ok", action="store_true", help="the skill completed")
    outcome.add_argument("--fail", action="store_true", help="the skill failed")
    skr.add_argument("--note", default="", help="one-line context for the outcome")
    skr.add_argument("--plugin", default=None, help="owning plugin, if known")
    skr.add_argument("--project", default=None, help="target namespace")
    skr.set_defaults(func=cmd_skills_record)
    skc = sksub.add_parser("recommend",
                           help="deterministic most-needed-skills ranking from measured data")
    skc.add_argument("--json", action="store_true", help="emit the full ranked payload")
    skc.add_argument("--top", type=int, default=None, help="limit to the top N entries")
    skc.add_argument("--brief", action="store_true",
                     help="<= 3 one-line suggestions (fast; for the session hook)")
    skc.add_argument("--project", default=None, help="target namespace")
    skc.set_defaults(func=cmd_skills_recommend)
    skt = sksub.add_parser("trail",
                           help="per-session skill trail (which skills, when, why) "
                                "from the ledger — survives the live Oversee board")
    skt.add_argument("--session", default=None, help="one session id (default: all)")
    skt.add_argument("--all", action="store_true",
                     help="merge every namespace (HIVE MIND), not just this project")
    skt.add_argument("--json", action="store_true", help="emit the raw trail JSON")
    skt.add_argument("--project", default=None, help="target namespace")
    skt.set_defaults(func=cmd_skills_trail)

    # --- clusters: parallel-orchestration layer for /mindhive-orchestrator ---
    cl = sub.add_parser("clusters",
                        help="cluster the repo, classify file complexity, plan parallel lanes")
    clsub = cl.add_subparsers(dest="clusters_cmd", required=True)
    for _name, _help, _fn in (
        ("build", "cluster the repo + store cluster_db.json", cmd_clusters_build),
        ("plan", "print the file-ownership-safe parallel shard plan", cmd_clusters_plan),
    ):
        _p = clsub.add_parser(_name, help=_help)
        _p.add_argument("--root", default=None, help="repo root to scan (default: cwd)")
        _p.add_argument("--lanes", type=int, default=None,
                        help="number of parallel lanes (default: one per cluster; clamped to [1, n])")
        _p.add_argument("--git", action="store_true",
                        help="weight complexity by git churn (opt-in; no-op if git is absent)")
        _p.set_defaults(func=_fn)
    clsub.add_parser("show", help="print the latest stored cluster DB").set_defaults(func=cmd_clusters_show)

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
    lp.add_argument("--no-dashboard", dest="no_dashboard", action="store_true",
                    help="don't auto-open the web dashboard for this run "
                         "(also disabled by MINDHIVE_LOOP_DASHBOARD=0)")
    lp.set_defaults(func=cmd_loop)

    # --- flag (Learning Loop judgment-error capture, KTD4) ---
    fg = sub.add_parser("flag", help="self-flag a judgment error (active at 1 occurrence)")
    fg.add_argument("text", help="one line: what was misjudged")
    fg.add_argument("--category", default=None,
                    help="optional category tag (e.g. git, scope, permissions)")
    fg.add_argument("--project", default=None, help="target namespace")
    fg.set_defaults(func=cmd_flag)

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
    rsm = rsub.add_parser("summaries",
                          help="structured lesson records (dashboard parity; --json for raw)")
    rsm.add_argument("--project", default=None)
    rsm.add_argument("--json", action="store_true", help="emit the raw list as JSON")
    rsm.set_defaults(func=cmd_rules_summaries)
    rsh = rsub.add_parser("show", help="print one incident record")
    rsh.add_argument("signature")
    rsh.add_argument("--project", default=None)
    rsh.set_defaults(func=cmd_rules_show)
    rr = rsub.add_parser("reflect",
                         help="bounded reflection pass over active un-reflected incidents")
    rr.add_argument("--project", default=None)
    rr.add_argument("--limit", type=int, default=3,
                    help="max incidents to reflect this batch (default: 3)")
    rr.add_argument("--cooldown-hours", type=float, default=6.0, dest="cooldown_hours",
                    help="skip when the last batch started within this window "
                         "(default: 6; pass 0 to disable)")
    rr.add_argument("--all-projects", dest="all_projects", action="store_true",
                    help="drain every registered namespace (manual backlog drain; "
                         "never invoked from hooks; LIVE_LOCK-named namespaces skipped)")
    rr.set_defaults(func=cmd_rules_reflect)
    rrs = rsub.add_parser("resolve",
                          help="actively resolve red (unresolved) rules: reflect "
                               "missing ones, reroute leaking ones to the best "
                               "alternative route")
    rrs.add_argument("--project", default=None)
    rrs.add_argument("--limit", type=int, default=3,
                     help="max red incidents to resolve this pass (default: 3)")
    rrs.add_argument("--list", action="store_true",
                     help="print the red set as JSON without resolving anything")
    rrs.set_defaults(func=cmd_rules_resolve)
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
    ren = rsub.add_parser("enforce",
                          help="manage the PreToolUse enforcement manifest (nudge->enforce ladder)")
    ren.add_argument("--project", default=None)
    ren.add_argument("--suggest", action="store_true",
                     help="add disabled stubs for ruled signatures recurring past the threshold")
    ren.add_argument("--enable", metavar="SIG", default=None,
                     help="turn on enforcement for a signature")
    ren.add_argument("--disable", metavar="SIG", default=None,
                     help="turn off enforcement for a signature")
    ren.set_defaults(func=cmd_rules_enforce)

    # --- triggers (prompt-quality gate: train/list/check the manifest) ---
    tg = sub.add_parser("triggers",
                        help="prompt-quality trigger rules (UserPromptSubmit gate)")
    tgsub = tg.add_subparsers(dest="triggers_cmd", required=True)
    tgt = tgsub.add_parser("train",
                           help="derive trigger rules from the inventory + ledger "
                                "and store rules/triggers.json")
    tgt.add_argument("--project", default=None)
    tgt.add_argument("--max-age-hours", type=float, default=0.0,
                     dest="max_age_hours",
                     help="stale-gate: skip when the manifest is younger than "
                          "this (0 = always retrain)")
    tgt.set_defaults(func=cmd_triggers_train)
    tgl = tgsub.add_parser("list", help="print the trained trigger rules")
    tgl.add_argument("--project", default=None)
    tgl.add_argument("--json", action="store_true", help="emit the raw manifest")
    tgl.set_defaults(func=cmd_triggers_list)
    tgc = tgsub.add_parser("check",
                           help="assess one prompt offline and show what the "
                                "gate would inject")
    tgc.add_argument("prompt")
    tgc.add_argument("--project", default=None)
    tgc.set_defaults(func=cmd_triggers_check)

    # --- enrich (Oversee prompt enrichment: default-on toggle + preview) ---
    en = sub.add_parser("enrich",
                        help="prompt enrichment (Oversee): default-on toggle "
                             "and deterministic prompt preview")
    ensub = en.add_subparsers(dest="enrich_cmd", required=True)
    for _name, _help in (("status", "show whether prompt enrichment is on"),
                         ("on", "enable prompt enrichment"),
                         ("off", "disable prompt enrichment")):
        sp = ensub.add_parser(_name, help=_help)
        sp.add_argument("--project", default=None)
        sp.set_defaults(func=cmd_enrich)
    env = ensub.add_parser("preview",
                           help="enrich one draft prompt: questions, source "
                                "candidates, skills, SOP check, composed text")
    env.add_argument("prompt")
    env.add_argument("--project", default=None)
    env.add_argument("--json", action="store_true",
                     help="emit the full enrichment result as JSON")
    env.set_defaults(func=cmd_enrich)

    return p


def main(argv=None) -> int:
    """CLI entry point. read_all() is now TOTAL — it skips malformed/torn rows so
    a single corrupt line can never crash a `hive` command (it used to: one torn
    write took down every CLI command AND every dashboard section). Corruption is
    surfaced explicitly by `hive verify` (corrupt_rows + any hash mismatch), not
    by crashing readers. This catch stays as defense-in-depth for any other stray
    json.loads on a command path."""
    try:
        return _run(argv)
    except json.JSONDecodeError as e:
        print(f"error: corrupt event log ({e})", file=sys.stderr)
        print("       run `hive verify` to locate the corrupt line(s).",
              file=sys.stderr)
        return 1


def _run(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    rc = args.func(args)
    return rc if rc is not None else 0


if __name__ == "__main__":
    sys.exit(main())
