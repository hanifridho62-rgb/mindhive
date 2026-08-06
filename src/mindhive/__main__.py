# src/mindhive/__main__.py
import argparse
import json
import os
import sys
from contextlib import contextmanager
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
    # A registry type's contract is only mandatory if the generic append path
    # can't smuggle a malformed lifecycle event past it. The event store itself
    # stays type-agnostic — the contract is enforced here, at a surface that
    # accepts an arbitrary type+payload from the user. There are two such
    # surfaces (this one and the dashboard's POST /api/append), so the dispatch
    # they share lives in events.validate_typed_payload; keeping the branch
    # list in one place is what stops the two from drifting apart.
    try:
        events.validate_typed_payload(args.type, payload)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    tags = [t for t in args.tags.split(",") if t]
    ev = events.append_event(args.type, payload, session_id=args.session, tags=tags)
    print(f"appended {ev.type} @ {ev.ts}")
    return None


def cmd_hook(args) -> int | None:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if args.stage == "post_tool_use":
        # Imported in this branch too: a later branch imports it, which makes the
        # name function-local for the WHOLE function, so referencing it here
        # without its own import is an UnboundLocalError on every tool call.
        from . import hookcapture, promptgate
        print(json.dumps(hookcapture.record_post_tool_use(raw)))
        # MN-19: sustain whatever this turn lit. Claude producing tool calls IS the
        # evidence it is still working, so the beat rides that signal rather than a
        # clock — a wedged turn stops beating and dims on schedule, which is what
        # keeps the dim band honest. Self-throttling (one `stat` when not due) and
        # never-raising, because this runs on every single tool call and stdout
        # above is the hook's contract.
        promptgate.beat_faculties(session_id=_hook_session(raw))
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
    elif args.stage == "stop":
        # Smart-zone rotation: measure this session's real context occupancy and,
        # when it is past the boundary, leave the ROTATION_PENDING artifact for
        # the `hive session run` supervisor to find after the child exits.
        # Print ONLY when there is something to say (stdout is the hook's channel),
        # and never a `decision` — a Stop hook's "block" means CONTINUE working,
        # so blocking here would pin a full session open instead of rotating it.
        from .rotation import hook as rotation_hook
        decision = rotation_hook.hook_decision(raw)
        if decision:
            print(json.dumps(decision))
    elif args.stage == "user_prompt_submit":
        # Prompt-quality gate: inject skill-trigger directives when the typed
        # prompt is underspecified. Print ONLY when the gate fires (stdout is
        # the hook's context channel); a clear prompt prints nothing. When
        # prompt enrichment is enabled (the default), the fired context also
        # carries the clarifying questions (see enrich.extend_hook_context —
        # additive, sidecar-only read, fail-open).
        from . import enrich, premise, promptgate, sopgate, style
        from .mindnet import inject as mn_inject
        context = promptgate.hook_context(raw)
        if context:
            context = enrich.extend_hook_context(context, raw)
        # MN-1 U9: the phase spine steers EVERY turn, including a clear prompt —
        # that unconditional reach is the whole point, so this call sits outside
        # the `if` above rather than riding the gate's decision. Sidecar-only
        # read, additive, fail-open; it returns `context` untouched (possibly
        # falsy) whenever there is nothing to say or anything goes wrong, so the
        # print below still fires only when a subsystem actually spoke.
        # The sidecar is read ONCE here and threaded in, rather than letting the
        # injector read it and then reading it again to ask what it used (MN-17).
        try:
            from .mindnet import sidecar as mn_sidecar
            spine = mn_sidecar.read()
        except Exception:                                   # noqa: BLE001
            spine = None
        context = mn_inject.extend_hook_context(context, raw, sc=spine)
        # §0's mechanical half: check the checkable factual claims this prompt
        # makes against the filesystem, the roadmap fold, and the last recorded
        # test run, and report contradictions. Unconditional for the same reason
        # the spine above is, and with a sharper edge — `promptgate` stops
        # scanning at PROMPT_SCAN_CAP, so the long, assertion-dense prompts most
        # likely to carry a stale or fabricated premise are exactly the ones its
        # gate never sees. Additive and fail-open: it returns `context`
        # unchanged whenever nothing is contradicted or anything goes wrong.
        context = premise.extend_hook_context(context, raw)
        # The MANDATORY SOP pass: the user's own standing procedures, verbatim,
        # on every prompt. Unconditional for the same reason the spine and the
        # style pass are — a procedure declared to hold on every task, delivered
        # only at session start, is exactly the instruction that decays by turn
        # 80. Reads the materialized sidecar, never the ledger: `read_all` is
        # 0.36 s on the live store and this sits on the per-prompt hot path.
        context = sopgate.extend_hook_context(context, raw)
        # The standing reply-style pass: caveman by default, normal prose when
        # the prompt asks to be taught. Unconditional on human-typed prompts for
        # the same reason the spine above is — a style that only re-asserts
        # itself when some other subsystem happened to speak is not a standing
        # style. It runs LAST so it prepends to whatever the earlier passes
        # produced, and it is the one pass that creates the context from nothing.
        context = style.extend_hook_context(context, raw)
        if context:
            print(json.dumps(context))
        # Durable causal trace: record which skills the gate directed so Oversee
        # can later show the honest "directed by prompt-gate" why. Best-effort
        # and self-guarding — never disturbs the context printed above.
        promptgate.record_direction(raw)
        # MN-17: light the cats for the faculties this turn actually used. AFTER the
        # print, for the same reason `record_direction` is: stdout is the context
        # channel, and a telemetry write must never be able to interleave with it.
        # The catalog and history cats are announced inside `record_direction`, which
        # already holds what the block named; the spine's SOP and past-failure cats are
        # announced here, where the advice is CONSULTED — emitting them where advice is
        # RESOLVED would put a ledger write inside a projection rebuild.
        if spine is not None:
            # Same session id `record_direction` used above, so both announces of
            # this turn land on ONE beat marker and get unioned. Without it they
            # write two markers and the heartbeat sustains only whichever the
            # PostToolUse hook happens to find — the catalog cats would dim
            # mid-turn, which is the very thing the beat exists to prevent.
            promptgate.announce_faculties(mn_inject.engaged_faculties(spine),
                                          session_id=_hook_session(raw))
        # MN-18: bring a task into existence so the spine has something to speak about.
        # Nothing else in the tree did this — `hive mindnet declare` is hand-run and
        # `decompose` needs a parent — so the phase spine, the SOP section and the
        # Improvements section were built and reached by no prompt.
        #
        # Here, in the side-effect layer, for the same reasons the two calls above are:
        # the printed context is the product, and this must not be able to disturb it. The
        # sidecar read above is reused rather than repeated, and there is deliberately no
        # rebuild — that costs 0.32 s against this path's ~0.4 s budget, and the next
        # prompt's own write would invalidate the result anyway.
        try:
            from .mindnet import autodeclare as mn_auto
            mn_auto.maybe_declare(_hook_prompt(raw), spine,
                                  session_id=_hook_session(raw))
        except Exception:                                   # noqa: BLE001
            pass
    return None


def rotation_supervisor_defaults():
    """Spin-guard defaults, read from the supervisor rather than duplicated as
    argparse literals — two copies of a safety bound drift, and the copy that
    drifts is always the one nobody is looking at."""
    from .rotation import supervisor
    return supervisor.MAX_ROTATIONS, supervisor.SPIN_WINDOW_S


def cmd_session_run(args) -> int | None:
    """Supervise an interactive Claude Code session, rotating it when the
    smart-zone boundary is crossed. Returns the supervisor's own exit code so a
    tripped spin guard is visible to whatever launched us."""
    from .rotation import supervisor
    result = supervisor.run(project=getattr(args, "project", None),
                            max_rotations=args.max_rotations,
                            window_s=args.spin_window)
    print(f"{result.reason}: {result.spawns} session(s), "
          f"{result.rotations} rotation(s)"
          + (f" — {result.detail}" if result.detail else ""))
    return result.exit_code or None


def cmd_session_install_hook(args) -> int | None:
    from .rotation import install as install_mod
    result = install_mod.install(args.settings, args.command, dry_run=args.dry_run)
    print(json.dumps(result))
    if not result["ok"]:
        return 2
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
    # MN-18 U4: refresh the MindNetwork projection here, stale-gated. The session-start
    # hook runs `hive state`, this is the only command in that hook belonging to this repo,
    # and the fold costs 0.32 s — invisible once per session, decisive on the prompt path
    # where the hook must stay under ~0.4 s. Without it the projection never returns to
    # fresh and per-task phase direction never appears. Silent on failure: printing STATE.md
    # is this command's job and a projection rebuild must not be able to stop it.
    try:
        from .mindnet import autodeclare as mn_auto
        mn_auto.refresh_projection(project)
    except Exception:                                       # noqa: BLE001
        pass
    # Self-heal the mandatory-SOP sidecar. `sop.record` writes it on every
    # change, so this only fires for a store that never had one (fresh clone,
    # new worktree) or one that was corrupted — and those are exactly the cases
    # that would otherwise fail SILENTLY forever, since `sopgate` deliberately
    # has no ledger fallback and a missing sidecar simply injects nothing.
    # Gated on a parse of the small sidecar rather than on `exists()`, so a
    # corrupt file heals too; the 0.36 s `read_all` is only paid when it is
    # actually broken. Session start is the right place: this is the one
    # command in that hook that belongs to this repo.
    try:
        from . import sop as _sop
        if not _sop.read_sidecar(project):
            _sop.write_sidecar(project)
    except Exception:                                       # noqa: BLE001
        pass
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


def cmd_premise_check(args) -> int | None:
    """Check a text's checkable factual claims against the record.

    Exit code is the product: non-zero when anything is CONTRADICTED, so this
    composes into a script. UNKNOWN exits zero — "could not check" is not a
    finding, and failing on it would train the operator to pass `|| true`."""
    from pathlib import Path

    from . import events as ev_mod, premise
    if getattr(args, "file", None):
        try:
            text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"cannot read {args.file}: {e}")
            return 2
    else:
        text = sys.stdin.read() if not sys.stdin.isatty() else ""

    found = premise.extract(text)
    if not found["scanned"] and text:
        # Reported, not silently passed: "too big to scan" and "nothing
        # asserted" are different answers and must not look alike.
        print(f"(not scanned — {len(text)} chars exceeds the "
              f"{premise.PREMISE_SCAN_CAP}-char budget)")
        return 0
    verdicts = premise.check(found["claims"], evs=ev_mod.read_all(args.project))
    print(premise.report(verdicts))
    for kind, dropped in (found.get("truncated") or {}).items():
        print(f"(+{dropped} further {kind} claim(s) not checked — per-class cap)")
    return 1 if any(v["outcome"] == premise.CONTRADICTED for v in verdicts) else 0


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
                                  files=args.files or None, session_id=sid,
                                  task_id=getattr(args, "task", None))
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


def cmd_log_dismiss(args) -> int | None:
    """Discharge queued debt no change-log entry will ever cover.

    The gate's exit. `record()` only discharges what its own entry named, so
    without this a path nothing will legitimately cover — a reverted experiment,
    an abandoned worktree's file — would block STATE.md forever, and the only
    ways out would be deleting a sidecar by hand or writing an entry naming files
    that were never changed. Both are worse than an audited dismissal."""
    from . import changelog as changelog_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    try:
        marker = changelog_mod.dismiss(args.files, session_id=sid)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    remaining = len(marker["files"]) if marker else 0
    print(f"dismissed {len(args.files)} file(s); {remaining} still queued")
    return None


# Session ids that exist but cannot own work: the hook's stdin-parse fallback and
# the store's default for anything appended outside a session.
_UNATTRIBUTABLE_SESSIONS = ("unknown", "manual")


def cmd_replenish_run(args) -> int | None:
    """One replenishment round for a session. Run by session_end BEFORE
    `roadmap autoclose`, so the injected steps exist when the plan is closed and
    therefore reach STATE.md in the same pass."""
    from . import replenish as rp_mod, replenish_sinks as sink_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    if not sid:
        # Exit 2 rather than the silent no-op `log queue` and `review queue` use
        # in the same spot. Their work is idempotent lifecycle bookkeeping; this
        # writes real steps under a plan id, so an unattributable run is an
        # operator error worth reporting, not something to absorb.
        print("no session id — pass --session or set MINDHIVE_SESSION",
              file=sys.stderr)
        return 2
    if sid in _UNATTRIBUTABLE_SESSIONS:
        # session_end.ps1 falls back to "unknown" when it cannot parse the id off
        # stdin. That was harmless while the hook only ran idempotent lifecycle
        # actions; injecting under it would pile every unparsed session's work
        # into one shared plan that never closes cleanly. Decline, and say so.
        print(f"session id {sid!r} cannot own a plan — skipping this round")
        return None
    evs = events.read_all()
    write = not args.dry_run
    # BOTH flags: the core's governs the audit rows, the sink's governs the work
    # itself. Passing only the first would leave a real roadmap step behind on a
    # run the operator asked to be a preview.
    #
    # `evs=evs` is not an optimisation detail: this runs inside the SessionEnd
    # hook, which already pays for several full ledger reads, and the sink would
    # otherwise read all 27k+ events a second time. Nothing is appended between
    # this read and the sink, so the snapshot is current.
    # The session surface runs a PROFILE, not the whole registry. Without this it
    # would run every registered lens — including `innovation`, which is gated to
    # `ambitious` because it spends a model call, and which would therefore fire a
    # headless `claude` on every single session end. There is no loop.toml here, so
    # the surface takes the documented default position.
    profile = rp_mod.PROFILES[rp_mod.DEFAULT_AMBITION]
    try:
        res = rp_mod.run_round(evs, surface="session",
                               sink=sink_mod.roadmap_sink(sid, evs=evs,
                                                          append=write),
                               cfg=rp_mod.ReplenishConfig.for_profile(profile),
                               lenses=rp_mod.lenses_for(profile),
                               session_id=sid, append=write)
    except Exception as exc:                                    # noqa: BLE001
        # The SessionEnd hook pipes stdout to Out-Null and a native non-zero
        # exit does not reach PowerShell's catch, so an unhandled traceback here
        # is invisible: the injector would stop working and nobody would learn
        # it had. Say so on stderr in one line, and fail deliberately.
        print(f"replenish failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not res.ran:
        print(f"replenish skipped: {res.skipped}")
        return None
    prefix = "would inject" if args.dry_run else "injected"
    for cand, place in res.landed:
        print(f"{prefix} [{cand.lens}] {cand.title}")
        for item in cand.evidence:
            print(f"    evidence: {item}")
        if not args.dry_run:
            print(f"    landed on roadmap {place.plan_id} step "
                  f"{place.step_index}")
    for cand, reason in res.refused:
        print(f"refused [{cand.lens}] {cand.title} — {reason}")
    if res.dropped:
        print(f"{res.dropped} candidate(s) did not fit this round")
    for name, message in res.lens_errors:
        print(f"lens {name} failed: {message}", file=sys.stderr)
    if res.dry:
        # Only honest when every lens actually ran. A round where they all threw
        # also finds nothing, and claiming the record proves nothing outstanding
        # would be a statement about the ledger that was never checked.
        print("nothing outstanding that the record proves" if not res.lens_errors
              else f"no candidates — but {len(res.lens_errors)} lens(es) failed, "
                   "so the record was not fully read")
    return None


def cmd_replenish_explain(args) -> int | None:
    """Score every lens's candidates against every dial position, and change
    nothing.

    The command this subsystem needed and did not have. A lens whose best score
    sits below every threshold it runs under produces candidates the gate is
    guaranteed to refuse — dead config — and that is invisible from every other
    surface: a round prints nothing, the ledger holds nothing (a below-the-bar
    refusal is deliberately never recorded, because recording it would spend the
    fingerprint), and the test suite stays green because producing a doomed
    candidate looks exactly like working. Here it shows up as a `lands at` column
    reading `nowhere`."""
    from . import replenish as rp_mod
    lenses = None
    if args.ambition:
        choice = rp_mod.resolve_ambition(args.ambition)
        if choice.fell_back:
            print(f"unknown ambition {args.ambition!r} — reporting "
                  f"{choice.profile.name} instead. Valid: "
                  f"{', '.join(sorted(rp_mod.PROFILES))}", file=sys.stderr)
        lenses = rp_mod.lenses_for(choice.profile)
    if lenses is not None and not args.with_agentic:
        # The agentic lens spends a real model call. Asking what the record
        # currently holds is not the same as asking to pay for a proposal, so it
        # takes its own flag even when the chosen position would run it.
        lenses = {n: l for n, l in lenses.items() if n != "innovation"}
    elif lenses is None and args.with_agentic:
        lenses = dict(rp_mod.LENSES)

    report = rp_mod.explain(events.read_all(), lenses=lenses, tune=True)
    if args.json:
        print(json.dumps(report, indent=2))
        return None

    positions = report["positions"]
    print(f"as of {report['now']} · lenses: {', '.join(report['lenses'])} · "
          f"{report['spent_fingerprints']} spent fingerprint(s)")
    print("positions: " + "  ".join(
        f"{name}>={info['threshold']}" for name, info in sorted(
            positions.items(), key=lambda kv: -kv[1]["threshold"])))
    tuning = report["tuning"]
    print(f"tuning: {tuning['success_streak']} green in a row, "
          f"{tuning['failure_streak']} bad in a row"
          + (f" · {tuning['reason']}" if tuning["reason"] else ""))
    print()

    if not report["candidates"]:
        print("no lens found a candidate")
    for row in report["candidates"]:
        lands = ", ".join(row["lands_at"]) or "nowhere"
        flags = "".join([" [spent]" if row["spent"] else "",
                         " [spike]" if row["spike"] else "",
                         " [vetoed]" if row["veto_reason"] else ""])
        kind = f"/{row['kind']}" if row["kind"] else ""
        print(f"{row['score']:>5}  {row['lens']}{kind}  -> {lands}{flags}")
        print(f"       {row['title']}")
        print("       terms: " + ", ".join(
            f"{name} {value:+d}" if name != "total" else f"total {value}"
            for name, value in row["terms"].items()))
    if report["silent_lenses"]:
        print()
        print("found nothing: " + ", ".join(report["silent_lenses"]))
    for err in report["lens_errors"]:
        print(f"lens {err['lens']} failed: {err['error']}", file=sys.stderr)
    return None


def cmd_replenish_status(args) -> int | None:
    """What the injector knows, folded from the ledger — there is no sidecar."""
    from . import replenish as rp_mod
    hist = rp_mod.history(events.read_all(), surface="session")
    print(json.dumps({
        "surface": "session",
        # Named for what it is. A round that writes no rows — every candidate a
        # spent duplicate, nothing landed, no cap hit — leaves the counter where
        # it was, so this is the last round that RECORDED something, not the
        # number of rounds the surface has run.
        "last_recorded_round": hist.last_round,
        "dry_rounds": hist.dry_rounds,
        "spent_fingerprints": sorted(hist.fingerprints),
        "lenses": sorted(rp_mod.LENSES),
    }, indent=2))
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
    from . import sop as sop_mod
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
        print(f"  {i}. {tier}[{r['sop_id']} v{r['version']}] {r['title']}  "
              f"(updated {r['updated']})")
    tail = (f" ({n_must} mandatory — re-asserted on every prompt)"
            if n_must else "")
    print(f"\n{len(sops)} active SOP(s){tail}")
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


def _preflight_report(project=None) -> dict:
    """The pre-execution report the mandatory SOP names, as data.

    Exists because an instruction with no verifier is a wish. "Confirm from the
    ledger whether this session is new or a continuation" and "prepare the
    tools, ledgers and improvements before executing" are both claims an
    assistant can *say* it did; this makes them claims it can only make by
    running something whose output is quotable. Split from the printer so the
    verdict is testable without parsing stdout."""
    from . import changelog as changelog_mod
    from . import incidents as inc
    from . import review as review_mod
    from . import roadmap as roadmap_mod
    from . import sop as sop_mod
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
        from . import plugins as plugins_mod
        from . import recommend
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
        "mandatory_sops": [{"sop_id": r["sop_id"], "title": r["title"],
                            "version": r["version"]}
                           for r in sop_mod.mandatory(evs)],
        "improvements": {
            "active": sum(1 for r in rules if r["status"] == "active"),
            "candidate": sum(1 for r in rules if r["status"] == "candidate"),
            "top": rules[:10],
        },
        "catalog": catalog,
    }


def cmd_sop_preflight(args) -> int | None:
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
    must = rep["mandatory_sops"]
    print(f"\nMandatory SOPs ({len(must)}):")
    for r in must:
        print(f"  - [{r['sop_id']} v{r['version']}] {r['title']}")
    if not must:
        print("  (none — add one: hive sop add <id> --title … --file … "
              "--mandatory)")
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
        print(f"  {i}. {glyphs[s['status']]} {roadmap_mod.new_prefix(s)}"
              f"{s['text']}{note}")
    print("\nhistory (oldest first):")
    for e in hist:
        print(f"  {e['ts']}  {e['action']:9s}  session {e['session']}")
    return None


# --- mindnet: the operator surface for MN-1's phase spine --------------------
# `tasks.py` owns the graph axis, `phases.py` the evidence contract,
# `controller.py` the phase axis, `executor.py` the validation runs and
# `sidecar.py` the projection. Nothing below re-implements any of that; these
# handlers are the argv-to-contract adapter, and three rules shape all of them.
#
# 1. A DECLARE WRITES BOTH AXES. A `mindnet_task` declare does not create a
#    `PhaseRecord` — only a `mindnet_phase` declare does (`controller.fold`,
#    create-only). A command that wrote the graph half alone would print a task
#    and then silently orphan every phase event that task ever received, which
#    is exactly the failure `test_a_task_declared_only_on_the_graph_axis_orphans_
#    its_phase_events` pins in the controller suite.
#
# 2. THE SIDECAR IS WRITTEN LAST. `sidecar._staleness` compares segment size and
#    mtime, so ANY event appended after the write makes the sidecar read stale —
#    and `telemetry.busy` appends two `mindnet_activity` rows around every
#    mutation. A stale sidecar sends U9's injector to its phase-agnostic block,
#    which silently disables the phase-aware injection MN-1 exists to deliver.
#    `_mn_work` is the ONE place that ordering lives, so it cannot be got wrong
#    per-command.
#
# 3. `verified_by` IS DERIVED HERE, NEVER TAKEN FROM THE OPERATOR (KTD2). There
#    is deliberately no `--verified-by` flag. A stat is a Python check, a real
#    subprocess exit code is a Python check — and nothing else is. A phase this
#    surface cannot honestly check is REFUSED with the flag that records the
#    weaker claim, rather than stamped `python` for the operator's convenience.

# The faculty a mindnet command wakes (U10's cat feed). One name, drawn from
# `telemetry.FACULTIES` — invariant 10 forbids a second spelling, and
# `test_a_mutating_command_emits_faculty_telemetry` asserts membership rather
# than trusting the string.
MINDNET_FACULTY = "task-graph"

# Exit codes. 2 keeps the file's existing meaning — REFUSED, nothing recorded —
# and 1 is the case this surface adds: the command ran and recorded something,
# but the outcome is not the one asked for (a failing validation run, a re-stat
# that found the evidence gone, an agent-reported satisfy the fold will not
# accept). Printing a success line over any of those is the specific failure
# mode this machine exists to prevent.
MN_REFUSED = 2
MN_NOOP = 1


def _mn_error(reason) -> int:
    print(f"error: {reason}", file=sys.stderr)
    return MN_REFUSED


def _mn_read(args):
    """``(project, session, evs, tasks, PhaseState)`` — ONE ledger read per
    command, threaded into every module that would otherwise read it again."""
    from .mindnet import controller as c_mod
    from .mindnet import tasks as t_mod
    project = getattr(args, "project", None)
    sid = getattr(args, "session", None) or os.environ.get("MINDHIVE_SESSION")
    evs = events.read_all(project)
    tasks = t_mod.fold(evs)
    return project, sid, evs, tasks, c_mod.fold(evs, tasks)


def _mn_refresh(project):
    """Rebuild the sidecar from the ledger. The ledger is the source of truth,
    so a projection that could not be refreshed is a NOTE, never a failed
    command — the write itself already landed."""
    from .mindnet import sidecar as sc_mod
    out = sc_mod.rebuild(project)
    if not out.written:
        print(f"note: sidecar not refreshed: {out.reason}", file=sys.stderr)
    return out


def _mn_projection(project):
    """The projection as the sidecar now reads it — i.e. what the block that
    just ran actually wrote. Cheaper than a second `read_all`, and it reports
    the fold's own answer rather than the handler's guess at it."""
    from .mindnet import sidecar as sc_mod
    return sc_mod.read(project).state


@contextmanager
def _mn_work(project, sid, task_id=None, phase=None):
    """Wake the faculty for the duration of a mutation, then write the sidecar —
    IN THAT ORDER, ALWAYS, including on the way out of an exception.

    Rule 2 above, in one place. `telemetry.busy` emits its closing `idle` from a
    `finally`, so the refresh here is genuinely the last write of the command;
    doing it inside the block would stamp the sidecar before the `idle` row
    landed and leave it reading stale. The refresh runs even when the body
    raised, because the telemetry rows landed either way."""
    from .mindnet import telemetry as tel_mod
    try:
        with tel_mod.busy(MINDNET_FACULTY, task_id=task_id, phase=phase,
                          project=project, session_id=sid):
            yield
    finally:
        _mn_refresh(project)


def _mn_stored(payload):
    """``None`` when *payload* satisfies the WRITER contract, else the reason.

    `phases.validate_stored` immediately before the write is the roadmap/sop
    convention, and calling it BEFORE the telemetry block matters: a contract
    violation then costs no events at all rather than a pair of orphaned
    activity rows."""
    from .mindnet import phases as ph_mod
    try:
        ph_mod.validate_stored(payload)
    except ValueError as e:
        return str(e)
    return None


def _hook_payload(raw):
    """The hook's stdin payload as a dict, or `{}`.

    Total: the hook's own contract is that malformed input yields silence, never a raise,
    and three call sites now need the same two fields out of it."""
    try:
        obj = json.loads(raw or "{}")
        return obj if isinstance(obj, dict) else {}
    except Exception:                                       # noqa: BLE001
        return {}


def _hook_prompt(raw) -> str:
    value = _hook_payload(raw).get("prompt")
    return value if isinstance(value, str) else ""


def _hook_session(raw):
    """The prompt's session id, so a declare lands in the same segment as the turn that
    caused it — the same reasoning `record_direction` documents."""
    return _hook_payload(raw).get("session_id") or None


def _mn_phase_event(payload, sid, project):
    return events.append_event("mindnet_phase", payload, session_id=sid,
                               tags=["mindnet", "phase"], project=project)


def _mn_no_record(task_id) -> int:
    return _mn_error(f"no phase record for '{task_id}' — declare it on both "
                     f"axes first with: hive mindnet declare {task_id} "
                     f'--title "…"')


def cmd_mindnet_declare(args) -> int | None:
    """Declare a task on BOTH axes (rule 1). Idempotent by construction: the
    graph fold upserts a re-declare and the phase fold ignores it, so re-running
    this to sharpen a title never resets accumulated progress."""
    from .mindnet import tasks as t_mod
    project = getattr(args, "project", None)
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    task_payload = {"action": "declare", "task_id": args.task_id,
                    "title": args.title}
    for key, value in (("parent_id", args.parent),
                       ("description", args.description),
                       ("validation_script", args.validation_script),
                       ("faculty", args.faculty)):
        if value:
            task_payload[key] = value
    if args.language:
        task_payload["language_context"] = list(args.language)
    if args.triviality_bypass:
        # FIRST DECLARE ONLY (KTD10) — `tasks.fold` records a later change as a
        # violation and drops it. Not re-checked here: one home for the rule.
        task_payload["triviality_bypass"] = True
    # The phase-axis declare names NO phase: the entry point is derived by
    # `tasks.fold` (the gate, or `execution` for a bypassed task), and a
    # caller-named entry phase is the teleport MN-2 stopped accepting.
    phase_payload = {"action": "declare", "task_id": args.task_id,
                     "verified_by": "python"}
    try:
        t_mod.validate_payload(task_payload)
    except ValueError as e:
        return _mn_error(e)
    why = _mn_stored(phase_payload)
    if why:
        return _mn_error(why)

    with _mn_work(project, sid, args.task_id):
        events.append_event("mindnet_task", task_payload, session_id=sid,
                            tags=["mindnet", "task"], project=project)
        ev = _mn_phase_event(phase_payload, sid, project)
    rec = _mn_projection(project).records.get(args.task_id)
    print(f"mindnet declare: {args.task_id} at "
          f"'{rec.phase if rec is not None else t_mod.PHASES[0]}' @ {ev.ts}")
    return None


def cmd_mindnet_enter(args) -> int | None:
    """Move a task forward (or back) one phase, gated by `can_advance`."""
    from .mindnet import controller as c_mod
    project, sid, _evs, tasks, state = _mn_read(args)
    tid, to_phase = args.task_id, args.phase
    rec = state.records.get(tid)
    if rec is None:
        return _mn_no_record(tid)
    ok, why = c_mod.can_advance(state.records, tasks, tid, to_phase)
    if not ok:
        # roadmap.record's idiom: name the command that unsticks it. The two
        # moves that ever can are earning the exit criterion and recording that
        # it was not earned.
        return _mn_error(
            f'{why}. Earn it with: hive mindnet satisfy {tid} …  ·  or record '
            f'the failure with: hive mindnet fall-back {tid} <phase> '
            f'--detail "…"')
    payload = {"action": "enter", "task_id": tid, "phase": to_phase,
               "epoch": rec.epoch, "verified_by": "python"}
    why = _mn_stored(payload)
    if why:
        return _mn_error(why)
    with _mn_work(project, sid, tid, to_phase):
        ev = _mn_phase_event(payload, sid, project)
    print(f"mindnet enter: {tid} '{rec.phase}' -> '{to_phase}' @ {ev.ts}")
    return None


def _mn_file_evidence(args, rec):
    """File evidence for a document phase: a stat, which IS a Python check — so
    `python` is the honest label for it and nothing weaker is accepted.

    KTD7 rides along: the resolving worktree root and branch go onto the
    evidence, so a later re-stat from a sibling checkout answers `unknown`
    rather than `fail`. `detect_project` collapses every worktree onto one
    namespace (SOP 10), so a branch-specific plan file is legitimately absent
    from another checkout and calling that a failure would be a lie."""
    from .mindnet import controller as c_mod
    from .mindnet import phases as ph_mod
    ref = str(args.ref or "").strip()
    if not ref:
        wanted = ", ".join(ph_mod.descriptor(rec.phase)["required_dir"])
        return None, (f"'{rec.phase}' takes file evidence — name the document "
                      f"with: hive mindnet satisfy {args.task_id} --ref "
                      f"<path under {wanted}>")
    root = paths.worktree_root()
    target, why = ph_mod.resolve_ref(rec.phase, ref, root)
    if target is None:
        return None, why
    try:
        present = target.is_file()
    except OSError as e:
        return None, f"cannot stat '{ref}': {type(e).__name__}: {e}"
    if not present:
        return None, (f"'{ref}' does not exist under {root} — a satisfaction "
                      f"for evidence that is not there is precisely the claim "
                      f"this machine exists to refuse")
    detail = args.detail or f"stat: '{ref}' exists under {root}"
    evidence = {"kind": "file", "ref": ref, "result": "ok", "detail": detail,
                "root": str(root)}
    # Reuses `controller._head_branch` rather than respelling `git rev-parse`
    # here: `controller.restat` compares against exactly this value, and two
    # spellings of the same git question are two things that can drift apart
    # without either one looking wrong.
    branch = c_mod._head_branch(root)
    if branch:
        evidence["branch"] = branch
    return _mn_satisfy_payload(args.task_id, rec, detail, evidence), None


def _mn_event_evidence(args, evs, rec):
    """`execution`'s exit criterion (KTD16): a ``code_change`` event that names
    this task.

    The join key is optional and additive, so historical rows simply do not
    match. `hive log --task` is U8's producer; until it lands the corrective
    hint names the raw append that writes the same row today, because a hint
    pointing at a flag that does not exist is worse than no hint."""
    from .mindnet import phases as ph_mod
    tid = args.task_id
    hit = None
    for ev in evs or []:
        if getattr(ev, "type", None) != "code_change":
            continue
        p = getattr(ev, "payload", None)
        if isinstance(p, dict) and p.get("task_id") == tid:
            hit = ev            # the LAST match: the newest change-set for it
    if hit is None:
        return None, (
            f"no code_change event on the ledger carries task_id '{tid}', so "
            f"'{rec.phase}' has nothing to verify. Record the change-set "
            f"against this task with: hive append code_change "
            f'\'{{"category": "update", "text": "…", "files": ["…"], '
            f'"task_id": "{tid}"}}\' --json')
    detail = (args.detail
              or f"code_change @ {hit.ts}: {hit.payload.get('text') or ''}")
    detail = str(detail)[:ph_mod.MAX_DETAIL_CHARS]
    evidence = {"kind": "event", "ref": "", "result": "ok", "detail": detail}
    return _mn_satisfy_payload(tid, rec, detail, evidence), None


def _mn_satisfy_payload(task_id, rec, detail, evidence) -> dict:
    return {"action": "satisfy", "task_id": task_id, "phase": rec.phase,
            "epoch": rec.epoch, "detail": detail,
            # Derived by THIS code path, never accepted from the operator
            # (KTD2). Both callers above ran a real check to get here.
            "verified_by": "python", "evidence": evidence}


def _mn_python_evidence(args, evs, rec):
    """``(payload, None)`` when this surface can honestly stamp `python` for
    *rec*'s current phase, else ``(None, reason)``.

    Two refusals before any evidence is looked at, and both are KTD2 rather than
    ergonomics:

    - a phase whose descriptor names a verifier WEAKER than `python` has no
      machine check to run (KTD17: no review event type exists and `hive review
      clear` appends nothing), so a `python` claim for it would be a lie;
    - a command-kind phase's only honest producer is a real run, which is
      `hive mindnet validate` — a hand-written satisfy there attests to a
      subprocess that never happened."""
    from .mindnet import phases as ph_mod
    desc = ph_mod.descriptor(rec.phase) or {}
    need, kind = desc.get("verified_by"), desc.get("kind")
    if need != "python":
        return None, (
            f"'{rec.phase}' is {need}-reported by design — there is no machine "
            f"check that can honestly clear it, so this surface will not stamp "
            f"one. Record it with: hive mindnet satisfy {args.task_id} "
            f'--agent-reported --detail "…"')
    if kind == "command":
        return None, (
            f"'{rec.phase}' takes command evidence, and the only honest "
            f"producer of that is a real run: hive mindnet validate "
            f"{args.task_id}  ·  if the gate refused the script, record the "
            f"refusal with: hive mindnet satisfy {args.task_id} "
            f'--agent-reported --detail "<the refusal reason>"')
    if kind == "file":
        return _mn_file_evidence(args, rec)
    if kind == "event":
        return _mn_event_evidence(args, evs, rec)
    return None, (f"'{rec.phase}' has no evidence descriptor this surface "
                  f"knows how to check")


def _mn_agent_satisfy(args, project, sid, evs, tasks, rec) -> int | None:
    """The KTD15 escape hatch: an AGENT-reported satisfy carrying its reason
    verbatim, so a fail-closed gate's false positive cannot deadlock a task
    while the ledger still shows Python did not verify it.

    `executor.agent_satisfy` returns ``clears`` and this MUST read it. Against
    the committed descriptors it is False for `audit-bug` and, since MN-8b, for
    `pr-green` — both name `python` as their verifier, so
    `controller._apply_satisfy` refuses the weaker claim, the event folds as a
    violation, and the phase stays unsatisfied. Printing a success line over
    that no-op is the exact dishonesty the two-validator contract exists to
    stop.

    The two are not the same situation and the warning is worth reading in
    both. At `audit-bug` a False here is a GAP — the python verifier refused
    and the hatch cannot close it. At `pr-green` it is the DESIGN: a task must
    not be closeable by an agent asserting its own PR is green. Either way the
    remedy is the same, a mechanical run through `hive mindnet validate`."""
    from .mindnet import executor as x_mod
    tid = args.task_id
    if not str(args.detail or "").strip():
        return _mn_error(
            f"--agent-reported must carry the reason verbatim: hive mindnet "
            f'satisfy {tid} --agent-reported --detail "…" — an unexplained '
            f"agent claim is the self-attestation this contract exists to stop")
    out = err = None
    with _mn_work(project, sid, tid, rec.phase):
        try:
            out = x_mod.agent_satisfy(tid, args.detail, evs=evs, tasks=tasks,
                                      project=project, session_id=sid)
        except ValueError as e:
            err = e
    if err is not None:
        return _mn_error(err)
    print(f"mindnet satisfy: {tid} '{rec.phase}'@{rec.epoch} recorded "
          f"(verified by agent) @ {out.event.ts}")
    if not out.clears:
        # Flushed first so the warning cannot surface AHEAD of the line it
        # qualifies: stdout is block-buffered when piped and stderr is not, so
        # a log would otherwise read "did NOT clear" before "recorded".
        sys.stdout.flush()
        print(f"warning: this did NOT clear '{rec.phase}' — {out.note}",
              file=sys.stderr)
        return MN_NOOP
    return None


def cmd_mindnet_satisfy(args) -> int | None:
    """Record that the task's CURRENT phase cleared its exit criterion.

    The phase is never named on the command line: KTD5 binds a satisfaction to
    the phase the task is actually in, so accepting one here would only produce
    events the fold records as violations."""
    project, sid, evs, tasks, state = _mn_read(args)
    tid = args.task_id
    rec = state.records.get(tid)
    if rec is None:
        return _mn_no_record(tid)
    if args.agent_reported:
        return _mn_agent_satisfy(args, project, sid, evs, tasks, rec)
    payload, why = _mn_python_evidence(args, evs, rec)
    if payload is None:
        return _mn_error(why)
    why = _mn_stored(payload)
    if why:
        return _mn_error(why)
    with _mn_work(project, sid, tid, rec.phase):
        ev = _mn_phase_event(payload, sid, project)
    print(f"mindnet satisfy: {tid} '{rec.phase}'@{rec.epoch} ok "
          f"(verified by python) @ {ev.ts}")
    return None


def cmd_mindnet_fall_back(args) -> int | None:
    """Move a task BACKWARD and pay the epoch cost (KTD4): every satisfaction at
    or past the target is invalidated, so walking forward again needs new
    evidence at the new generation."""
    from .mindnet.tasks import phase_index
    project, sid, _evs, _tasks, state = _mn_read(args)
    tid, to_phase = args.task_id, args.phase
    rec = state.records.get(tid)
    if rec is None:
        return _mn_no_record(tid)
    to_i, cur_i = phase_index(to_phase), phase_index(rec.phase)
    if to_i is None:
        from .mindnet.tasks import PHASES
        return _mn_error(f"'{to_phase}' is not a phase — one of: "
                         + ", ".join(PHASES))
    if cur_i is not None and to_i > cur_i:
        return _mn_error(
            f"'{to_phase}' is ahead of '{rec.phase}' — falling back means "
            f"going back. Move forward with: hive mindnet enter {tid} "
            f"{to_phase}")
    payload = {"action": "fall_back", "task_id": tid, "phase": to_phase,
               "epoch": rec.epoch, "detail": args.detail,
               "verified_by": "python"}
    why = _mn_stored(payload)
    if why:
        return _mn_error(why)
    with _mn_work(project, sid, tid, to_phase):
        ev = _mn_phase_event(payload, sid, project)
    new = _mn_projection(project).records.get(tid)
    print(f"mindnet fall-back: {tid} '{rec.phase}' -> '{to_phase}' "
          f"(epoch {rec.epoch + 1 if new is None else new.epoch}) @ {ev.ts}")
    return None


def cmd_mindnet_validate(args) -> int | None:
    """Run the task's ``validation_script`` behind U6's fail-closed gate and
    record what came back as python-verified command evidence."""
    from .mindnet import executor as x_mod
    project, sid, evs, tasks, state = _mn_read(args)
    tid = args.task_id
    rec = state.records.get(tid)
    if rec is None:
        return _mn_no_record(tid)
    with _mn_work(project, sid, tid, rec.phase):
        out = x_mod.validate(
            tid, script=args.script, evs=evs, tasks=tasks, project=project,
            session_id=sid,
            timeout_s=args.timeout or x_mod.DEFAULT_TIMEOUT_S)
    run = out.run
    if out.event is None:
        # A refusal appends nothing at all — the run never happened, so there
        # is no evidence and no honest satisfy to write.
        return _mn_error(out.reason or run.reason)
    print(f"mindnet validate: {tid} '{rec.phase}' {run.result} "
          f"(exit {run.exit_code} in {run.duration_s}s) @ {out.event.ts}")
    if run.artifact:
        print(f"  artifact: {run.artifact}")
    if run.result != "ok":
        print(f"  {run.reason}")
        return MN_NOOP
    return None


def cmd_mindnet_restat(args) -> int | None:
    """Re-check the file evidence behind the task's current phase (KTD8).

    A failure never moves the task — it marks the phase stale, which blocks the
    way out of it. A re-stat from another checkout answers `unknown`, never
    `fail` (KTD7)."""
    from .mindnet import controller as c_mod
    project, sid, evs, tasks, state = _mn_read(args)
    tid = args.task_id
    rec = state.records.get(tid)
    if rec is None:
        return _mn_no_record(tid)
    with _mn_work(project, sid, tid, rec.phase):
        out = c_mod.restat(tid, evs=evs, tasks=tasks, project=project,
                           session_id=sid)
    print(f"mindnet restat: {tid} {out.status} — {out.reason}")
    return MN_NOOP if out.status == "stale" else None


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
    from .mindnet import controller as c_mod
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
    from .mindnet import controller as c_mod
    from .mindnet import tasks as t_mod
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
    from .mindnet import sidecar as sc_mod
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
    from .mindnet import decompose as dc_mod
    from .mindnet import sidecar as sc_mod
    from .mindnet import tasks as t_mod
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
    from .mindnet import sidecar as sc_mod
    from .mindnet import tasks as t_mod
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


def cmd_mindnet_orchestrate(args) -> int | None:
    """Walk the task graph in waves, running every unblocked task concurrently.

    **Bare is a dry run.** `--execute` is the explicit opt-in, because this is
    the one command here that starts real child processes and the safe form must
    be the one an operator gets by accident.

    Reads the LEDGER rather than the sidecar, like `list` and for the same
    reason: this is not the hook path, and an orchestration that could disagree
    with `hive mindnet list` about the graph would be worse than a slow one.

    Exit 1 on `stalled` — work remains and nothing can advance it, which is
    operator-actionable. A zero exit there would let a wrapping CI step pass
    while the graph is stuck. `capped` is the same: it means the run gave up."""
    from dataclasses import asdict
    from .mindnet import agent as ag_mod
    from .mindnet import executor as x_mod
    from .mindnet import orchestrator as orc_mod
    from .mindnet import tasks as t_mod
    project = getattr(args, "project", None)
    tasks = t_mod.fold(events.read_all(project))
    if not tasks:
        print('(no mindnet tasks to orchestrate — declare one: hive mindnet '
              'declare <id> --title "…")')
        return None

    # Two runners, paired explicitly with their payload builders because they take
    # DIFFERENT content in the same argument position. The agent is the default:
    # `validation_script` only ever re-checks a task, which is exactly why the
    # orchestrator could not do any actual work before this.
    workers = args.max_workers
    timeout = args.timeout
    # DEFAULT: isolate agent runs. Sharing one working tree is the DANGEROUS mode
    # — two agents editing it corrupt each other's edits and the git index — so
    # `none` cannot be what an operator gets by accident. It also delivers the
    # founding ask that parallel work happen "secara paksa": `--execute` alone now
    # fans out. `--via-script` re-checks a task in place and has nothing to
    # isolate, so it defaults the other way.
    isolation = args.isolation
    if isolation is None:
        isolation = "none" if args.via_script else "worktree"
    isolate = isolation == "worktree"
    if isolate and args.via_script:
        # A validation script re-checks a task in place; isolating it would run the
        # check against an empty branch and prove nothing. Refusing beats silently
        # ignoring the flag the operator typed.
        return _mn_error("--isolation worktree is for agent runs; a "
                         "--via-script run re-checks a task in place and has "
                         "nothing to isolate")
    runner_kwargs = None
    if args.via_script:
        runner, payload_for = x_mod.execute, orc_mod.validation_script_of
    else:
        runner, payload_for = ag_mod.run, ag_mod.prompt_for
        if timeout is None:
            timeout = ag_mod.DEFAULT_TIMEOUT_S
        # The smart-zone gate. Without this the agent runner stays the one-shot
        # it always was: it only chains — and therefore only rotates before the
        # dumb zone — when it is handed a context config. Built from the loop's
        # own config so ONE file governs the ceiling everywhere, rather than the
        # fleet growing a second, quietly-diverging copy of the number.
        #
        # No guard is passed: `agent.run` makes its own PER-RUN one. A single
        # guard shared across a wave is shared mutable state across threads, and
        # its whole job is to halt — so one task's rotations would stop another
        # task that had done nothing wrong.
        from pathlib import Path as _Path
        from .loop import config as loop_cfg_mod
        from .loop import context as ctx_mod
        _root = paths.worktree_root()
        _lc = loop_cfg_mod.load_config(
            _Path(_root or ".") / ".hive-loop" / "loop.toml")
        runner_kwargs = {
            "context_cfg": ctx_mod.ContextConfig(
                _lc.context_window, _lc.smart_zone_ratio, _lc.absolute_ceiling),
        }
        # With `--isolation worktree` each agent gets its OWN checkout and its own
        # ledger segment, so the shared-tree cap no longer applies and the pool is
        # free to fan out. Without it every agent runs with cwd=root — the same
        # tree — and two editing it concurrently corrupt each other's edits and
        # the git index.
        if isolate or args.allow_shared_tree:
            pass                                    # caller's --max-workers stands
        else:
            if workers is not None and workers > ag_mod.MAX_SAFE_WORKERS:
                print(f"note: --max-workers {workers} ignored — agents share one "
                      f"working tree, so they run one at a time. Use "
                      f"`--isolation worktree` for real parallelism, or "
                      f"--allow-shared-tree to let them collide.",
                      file=sys.stderr)
            workers = ag_mod.MAX_SAFE_WORKERS

    try:
        out = orc_mod.orchestrate(
            tasks, runner=runner, payload_for=payload_for,
            execute=bool(args.execute), limit=args.limit,
            # None lets the orchestrator derive the cap from the graph; passing
            # MAX_WAVES verbatim capped a legal long dependency chain.
            max_waves=args.max_waves,
            project=project, timeout_s=timeout,
            max_workers=workers, isolate=isolate,
            runner_kwargs=runner_kwargs)
    except Exception as exc:
        # An orchestrator fault is an operator-facing failure, not a stack trace.
        # `main()` only catches JSONDecodeError, so without this a ledger or
        # projection error surfaces as a raw traceback — and the two faults
        # `_persist` raises are precisely the ones carrying the instructions for
        # what to do next.
        return _mn_error(f"{type(exc).__name__}: {exc}")

    if args.json:
        print(json.dumps(asdict(out), indent=2, ensure_ascii=False,
                         default=str))
    else:
        verb = "would run" if not args.execute else "ran"
        print(f"mindnet orchestrate: {out.outcome} — {len(out.waves)} wave(s), "
              f"{verb} {sum(len(w.tasks) for w in out.waves)} task(s)")
        for w in out.waves:
            line = f"  wave {w.index + 1}: " + ", ".join(w.tasks)
            if args.execute:
                line += (f"   [ok: {len(w.completed)}, "
                         f"blocked: {len(w.blocked)}]")
            print(line)
        # Named WITH ITS REASON, not counted: an operator cannot act on
        # "3 blocked", and pointing them at `hive mindnet show` was worse than
        # useless while the status rows carried no reason at all.
        for label, ids in (("blocked", out.blocked), ("skipped", out.skipped)):
            if not ids:
                continue
            if label == "skipped":
                # The distinction is load-bearing: skipped tasks never ran and
                # keep their status, so they are fixable and re-runnable. Blocked
                # ones ran and failed.
                print("  skipped (nothing ran; still pending):")
            else:
                print("  blocked:")
            for tid in ids:
                why = out.reasons.get(tid) or "no reason recorded"
                print(f"    {tid}: {why}")
        if out.branches:
            print("  branches: " + ", ".join(
                f"{t}={b}" for t, b in sorted(out.branches.items())))
        if out.kept:
            # The one thing an operator MUST act on: a checkout `retire` refused to
            # delete because it still held work nobody has looked at.
            print("  worktrees kept (they hold work):")
            for tid, path in sorted(out.kept.items()):
                print(f"    {tid}: {path}")
        if not args.execute:
            print("  this was a dry run — add --execute to actually run it")
    # `dry-run` and `settled` are successes; the other two are not.
    return 1 if out.outcome in (orc_mod.OUTCOME_STALLED,
                                orc_mod.OUTCOME_CAPPED) else None


def cmd_mindnet_enforce(args) -> int | None:
    """Read or toggle MN-6's opt-in: whether the MN-4 policy checks may deny
    anything in THIS checkout.

    Bare, it is a status read — and the one place an operator sees
    `optin.DORMANT_NOTE`, so "why is nothing being enforced?" has an answer that
    does not require reading source.

    Two properties this handler must keep, both learned the hard way in MN-4:

    * **No ledger append.** `sidecar._staleness` counts every event, so a row
      written here without also rewriting the sidecar would read STALE and
      silently degrade U9's phase-aware injection. It also means disarming a
      frozen repository never depends on the ledger being writable.
    * **Reachable while the teeth are biting.** Every remedy MN-4 documented ran
      through `Bash`, which the phase block denied unconditionally — the kill
      switch was locked inside the room it opened. `hive` is exempt from the
      phase block, the language lock only inspects writes, and this writes via
      `paths.write_text_atomic` rather than a `Write` tool call, so all three
      rules are satisfied at once rather than each alone."""
    from .mindnet import optin
    project = getattr(args, "project", None)
    if args.enable or args.disable:
        out = optin.set_enabled(
            bool(args.enable), project=project,
            session=getattr(args, "session", None)
            or os.environ.get("MINDHIVE_SESSION"))
        st = optin.status(project)
        if args.json:
            print(json.dumps({**st, "wrote": out}, indent=2))
            return None
        verb = "enabled" if args.enable else "disabled"
        print(f"mindnet enforce: {verb} for {out['root']} -> {out['path']}")
        # The recorded root and the EFFECTIVE answer can legitimately differ:
        # MINDNETWORK_ENFORCE out-ranks the file, and a deeper recorded root
        # out-ranks a shallower one. Saying only "enabled" there would be a lie
        # the operator finds out about at the next denial.
        if st["armed"] != bool(args.enable):
            print(f"  note: effective state here is "
                  f"{'armed' if st['armed'] else 'dormant'} "
                  f"({st['source']}) — {st['note']}")
        return None
    st = optin.status(project)
    if args.json:
        print(json.dumps(st, indent=2))
        return None
    print(f"mindnet enforce: {'armed' if st['armed'] else 'dormant'} "
          f"({st['source']})")
    print(f"  {st['note']}")
    # The remedy has to name the mechanism that is actually deciding. The
    # environment out-ranks the record, so suggesting `--enable` while
    # MINDNETWORK_ENFORCE=0 is holding the gate shut sends an operator to a flag
    # that will change the file and nothing else — the same class of
    # unactionable advice MN-4's original one-sentence refusal was.
    if st["source"] == "env":
        print(f"  {optin.ENV_VAR} is set in this process and out-ranks the "
              f"record — unset it to fall back to the per-checkout opt-in")
    elif not st["armed"]:
        print("  turn it on for this checkout: hive mindnet enforce --enable")
    else:
        print("  turn it off for this checkout: hive mindnet enforce --disable"
              f"  ·  or for this process only: {optin.ENV_VAR}=0")
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
                             "pre_tool_use", "user_prompt_submit", "stop"],
                    help="which hook fired")
    hk.set_defaults(func=cmd_hook)

    # Smart-zone rotation for INTERACTIVE sessions. `hive loop` already does this
    # headlessly; nothing inside an interactive session can clear its own context,
    # so the loop has to live outside it — here.
    se = sub.add_parser("session", help="supervise an interactive Claude session "
                                        "and rotate it when context fills")
    se_sub = se.add_subparsers(dest="session_cmd", required=True)

    se_run = se_sub.add_parser("run", help="run an interactive session under "
                                           "smart-zone rotation")
    se_run.add_argument("--project", default=None,
                        help="target namespace explicitly (default: detect from cwd)")
    se_run.add_argument("--max-rotations", type=int,
                        default=rotation_supervisor_defaults()[0],
                        help="rotations allowed inside the spin window before "
                             "the circuit breaker trips")
    se_run.add_argument("--spin-window", type=float,
                        default=rotation_supervisor_defaults()[1],
                        help="seconds the spin guard looks back over")
    se_run.set_defaults(func=cmd_session_run)

    se_install = se_sub.add_parser(
        "install-hook", help="register the Stop hook in a settings.json "
                             "(idempotent; the hook stays inert until "
                             "MINDHIVE_ROTATION is set)")
    se_install.add_argument("settings", help="path to the settings.json to edit")
    se_install.add_argument("--command", default=None,
                            help="the hook command to register (default: this "
                                 "checkout's hooks/stop.ps1). Worth setting from "
                                 "a worktree, where the default resolves to the "
                                 "worktree's own copy rather than the main one")
    se_install.add_argument("--dry-run", action="store_true",
                            help="report what would change without writing")
    se_install.set_defaults(func=cmd_session_install_hook)

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

    # §0's operator-facing half. Deliberately NOT folded into `hive verify`,
    # which already means "verify the hash chain" — one command answering two
    # unrelated questions is how an operator ends up trusting the wrong answer.
    pre = sub.add_parser(
        "premise",
        help="check a prompt or handoff doc's factual claims against the record")
    pres = pre.add_subparsers(dest="premise_cmd", required=True)
    prec = pres.add_parser(
        "check",
        help="report asserted paths, roadmap ids and test counts the record "
             "contradicts (exits non-zero on any contradiction)")
    prec.add_argument("--file", default=None,
                      help="read the text from PATH (default: stdin)")
    prec.add_argument("--project", default=None,
                      help="target namespace explicitly (default: detect from cwd)")
    prec.set_defaults(func=cmd_premise_check)

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
        # The MindNetwork join key (KTD16). `execution` is the one phase whose
        # evidence is an EVENT rather than a file, and it looks for a
        # code_change carrying this task_id — so without a way to stamp it from
        # the surface that writes every change-log entry, the contract accepts
        # a field nothing produces. Omitted entirely when absent, so an
        # untracked entry stays byte-identical to every historical one.
        lc.add_argument("--task", default=None, metavar="TASK_ID",
                        help="mindnet task this change-set belongs to; stamps "
                             "the join key its `execution` phase looks for")
        lc.set_defaults(func=cmd_log_add, category=_cat)
    lgq = lgsub.add_parser("queue", help="queue this session's unlogged source files (run by session_end)")
    lgq.add_argument("--session", default=None,
                     help="session id to queue (default: $MINDHIVE_SESSION)")
    lgq.set_defaults(func=cmd_log_queue)
    lgsub.add_parser("show", help="print change-log entries, newest first").set_defaults(func=cmd_log_show)
    lgsub.add_parser("pending", help="print the pending unlogged-changes marker, if any").set_defaults(func=cmd_log_pending)
    lgd = lgsub.add_parser("dismiss",
                           help="discharge queued files no change-log entry "
                                "will cover (records the dismissal)")
    lgd.add_argument("--files", nargs="+", required=True,
                     help="paths to drop from the pending marker")
    lgd.add_argument("--session", default=None,
                     help="session id to attribute the dismissal to "
                          "(default: $MINDHIVE_SESSION)")
    lgd.set_defaults(func=cmd_log_dismiss)

    # --- replenish (adaptive work injection) ---
    rp = sub.add_parser("replenish",
                        help="propose work the record proves is outstanding "
                             "(run by session_end)")
    rpsub = rp.add_subparsers(dest="replenish_cmd", required=True)
    rpr = rpsub.add_parser("run", help="run one replenishment round for a session")
    rpr.add_argument("--session", default=None,
                     help="session id to replenish (default: $MINDHIVE_SESSION)")
    rpr.add_argument("--dry-run", action="store_true",
                     help="decide and print without appending anything")
    rpr.set_defaults(func=cmd_replenish_run)
    rpe = rpsub.add_parser("explain",
                           help="score every lens's candidates against every "
                                "dial position, changing nothing")
    rpe.add_argument("--ambition", default=None,
                     help="report only the lenses this position runs "
                          "(conservative / balanced / ambitious)")
    rpe.add_argument("--with-agentic", action="store_true",
                     help="include the innovation lens, which spends a model "
                          "call (excluded by default even at --ambition "
                          "ambitious)")
    rpe.add_argument("--json", action="store_true",
                     help="machine-readable report")
    rpe.set_defaults(func=cmd_replenish_explain)
    rpsub.add_parser("status",
                     help="what the injector knows: rounds, dry streak, spent "
                          "fingerprints, active lenses"
                     ).set_defaults(func=cmd_replenish_status)

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
    # Tri-state on purpose (None = leave the tier alone). A plain store_true
    # would default to False, and every title-only `sop update` would then
    # silently demote a mandatory procedure to an ordinary one.
    soa.add_argument("--mandatory", dest="mandatory", action="store_true",
                     default=None,
                     help="holds on EVERY task: rendered first and un-elided "
                          "in STATE.md and re-asserted on every prompt")
    soa.add_argument("--no-mandatory", dest="mandatory", action="store_false",
                     help="ordinary, scope-matched SOP (the default)")
    soa.set_defaults(func=cmd_sop_add)
    sou = sosub.add_parser("update", help="revise an SOP's title and/or body")
    sou.add_argument("sop_id")
    sou.add_argument("--title", default=None)
    sou.add_argument("--text", default=None)
    sou.add_argument("--file", default=None)
    sou.add_argument("--stdin", action="store_true")
    sou.add_argument("--tags", default="")
    sou.add_argument("--session", default=None)
    sou.add_argument("--mandatory", dest="mandatory", action="store_true",
                     default=None, help="promote to the mandatory tier")
    sou.add_argument("--no-mandatory", dest="mandatory", action="store_false",
                     help="demote to an ordinary, scope-matched SOP")
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
    sop_pf = sosub.add_parser(
        "preflight",
        help="pre-execution report: is this session NEW or a CONTINUATION "
             "(with the ledger evidence), which SOPs are mandatory, what the "
             "improvements record already warns about, and what is in the "
             "catalog")
    sop_pf.add_argument("--project", default=None)
    sop_pf.add_argument("--json", action="store_true")
    sop_pf.set_defaults(func=cmd_sop_preflight)

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

    # --- mindnet (MN-1: the deterministic phase spine over the task graph) ---
    # Mirrors the `roadmap` subparser above. Two deliberate omissions:
    #
    #   * no `--verified-by` anywhere (KTD2) — the value is derived by whatever
    #     check actually ran, and a flag would hand the untrusted party the
    #     field that says whether to trust them;
    #   * no argparse `choices=PHASES` on the phase arguments. `tasks.PHASES` is
    #     the ONE legend (invariant 10) and `controller.can_advance` already
    #     answers with it; a second copy in the parser would be a place for the
    #     legend to drift, and would swap a reason an operator can act on for
    #     argparse's bare "invalid choice".
    mn = sub.add_parser("mindnet", help="MindNetwork task graph + phase spine: "
                        "declare tasks, walk the 6-phase pipeline, and record "
                        "the evidence that earned each move")
    mnsub = mn.add_subparsers(dest="mindnet_cmd", required=True)

    mnd = mnsub.add_parser("declare", help="declare a task on BOTH axes (the "
                           "graph node and its phase record)")
    mnd.add_argument("task_id", help="stable kebab-case id, e.g. mn1-u7")
    mnd.add_argument("--title", required=True, help="the actionable ask")
    mnd.add_argument("--parent", default=None, help="parent task_id")
    mnd.add_argument("--description", default=None)
    mnd.add_argument("--validation-script", default=None,
                     dest="validation_script",
                     help="the command `hive mindnet validate` runs (argv, "
                          "never a shell)")
    mnd.add_argument("--language", action="append", default=None,
                     help="permitted language (repeatable; read by MN-4)")
    mnd.add_argument("--faculty", default=None, help="MN-5 cat feed label")
    mnd.add_argument("--triviality-bypass", action="store_true",
                     dest="triviality_bypass",
                     help="ADR-0005's one-line-fix bypass: born at 'execution' "
                          "and gated by the bug audit. FIRST DECLARE ONLY")
    mnd.add_argument("--project", default=None)
    mnd.add_argument("--session", default=None)
    mnd.set_defaults(func=cmd_mindnet_declare)

    mne = mnsub.add_parser("enter", help="move a task to another phase "
                           "(forward moves are evidence-gated)")
    mne.add_argument("task_id")
    mne.add_argument("phase", help="target phase")
    mne.add_argument("--project", default=None)
    mne.add_argument("--session", default=None)
    mne.set_defaults(func=cmd_mindnet_enter)

    mns = mnsub.add_parser("satisfy", help="record that the task's CURRENT "
                           "phase cleared its exit criterion")
    mns.add_argument("task_id")
    mns.add_argument("--ref", default=None,
                     help="worktree-root-relative document path (file phases)")
    mns.add_argument("--detail", default=None,
                     help="one line of context; REQUIRED with "
                          "--agent-reported, where it carries the reason "
                          "verbatim")
    mns.add_argument("--agent-reported", action="store_true",
                     dest="agent_reported",
                     help="no Python check ran — record the weaker claim "
                          "honestly instead of stamping 'python'")
    mns.add_argument("--project", default=None)
    mns.add_argument("--session", default=None)
    mns.set_defaults(func=cmd_mindnet_satisfy)

    mnf = mnsub.add_parser("fall-back", help="move a task backward and "
                           "invalidate every satisfaction at or past the "
                           "target (bumps the epoch)")
    mnf.add_argument("task_id")
    mnf.add_argument("phase", help="the phase to go back to")
    mnf.add_argument("--detail", required=True,
                     help="what failed — the one transition that exists to "
                          "record a failure may not record nothing about it")
    mnf.add_argument("--project", default=None)
    mnf.add_argument("--session", default=None)
    mnf.set_defaults(func=cmd_mindnet_fall_back)

    mnv = mnsub.add_parser("validate", help="run the task's validation script "
                           "behind the fail-closed gate and record the result "
                           "as python-verified evidence")
    mnv.add_argument("task_id")
    mnv.add_argument("--script", default=None,
                     help="override the stored validation_script (a one-off "
                          "re-check)")
    mnv.add_argument("--timeout", type=int, default=None,
                     help="seconds the run may take (default: 600)")
    mnv.add_argument("--project", default=None)
    mnv.add_argument("--session", default=None)
    mnv.set_defaults(func=cmd_mindnet_validate)

    mnr = mnsub.add_parser("restat", help="re-check the file evidence behind "
                           "the current phase (a failure marks it stale; it "
                           "never moves the task)")
    mnr.add_argument("task_id")
    mnr.add_argument("--project", default=None)
    mnr.add_argument("--session", default=None)
    mnr.set_defaults(func=cmd_mindnet_restat)

    mnw = mnsub.add_parser("show", help="one task in full: phase, evidence "
                           "with its verifier tier, refusals, orphans")
    mnw.add_argument("task_id")
    mnw.add_argument("--project", default=None)
    mnw.add_argument("--session", default=None)
    mnw.add_argument("--json", action="store_true")
    mnw.set_defaults(func=cmd_mindnet_show)

    mnl = mnsub.add_parser("list", help="every task with its phase and status")
    mnl.add_argument("--all", action="store_true",
                     help="include completed and abandoned tasks")
    mnl.add_argument("--project", default=None)
    mnl.add_argument("--json", action="store_true")
    mnl.set_defaults(func=cmd_mindnet_list)

    mnb = mnsub.add_parser("rebuild", help="re-derive the phase sidecar from "
                           "the ledger (the repair for a stale or corrupt one)")
    mnb.add_argument("--project", default=None)
    mnb.set_defaults(func=cmd_mindnet_rebuild)

    mndc = mnsub.add_parser("decompose", help="ask an agent what subtasks a task "
                            "actually requires (a DRY RUN unless --execute)")
    mndc.add_argument("task_id", help="the task to break down")
    mndc.add_argument("--execute", action="store_true",
                      help="actually declare the proposed subtasks")
    mndc.add_argument("--timeout", type=int, default=None,
                      help="seconds the agent may take (default: 300)")
    mndc.add_argument("--project", default=None)
    mndc.add_argument("--session", default=None)
    mndc.add_argument("--json", action="store_true")
    mndc.set_defaults(func=cmd_mindnet_decompose)

    mndep = mnsub.add_parser("depend", help="record that one task cannot start "
                             "until another is completed (the edge the "
                             "orchestrator walks)")
    mndep.add_argument("task_id", help="the task that has to wait")
    mndep.add_argument("on", help="the task_id it waits for")
    mndep.add_argument("--project", default=None)
    mndep.add_argument("--session", default=None)
    mndep.set_defaults(func=cmd_mindnet_depend)

    # MN-7's orchestrator. No `choices=` on --limit/--max-waves for the same
    # reason the phase arguments carry none: the bound belongs to the
    # orchestrator, and a second copy in the parser is a place for it to drift.
    mno = mnsub.add_parser("orchestrate", help="walk the graph in waves, "
                           "running every unblocked task concurrently (a DRY "
                           "RUN unless --execute is given)")
    mno.add_argument("--execute", action="store_true",
                     help="actually run the tasks. Without this the command "
                          "plans the waves and starts nothing")
    mno.add_argument("--limit", type=int, default=None,
                     help="most tasks to take from the frontier per wave — which "
                          "tasks run AT ALL this wave (default: all of them). "
                          "For a concurrency brake use --max-workers")
    mno.add_argument("--max-waves", type=int, default=None, dest="max_waves",
                     help="runaway backstop (default: "
                          "orchestrator.MAX_WAVES)")
    mno.add_argument("--max-workers", type=int, default=None,
                     dest="max_workers",
                     help="ceiling on CONCURRENT children, independent of how "
                          "wide the frontier is (default: cpu count). Distinct "
                          "from --limit, which decides which tasks run at all")
    mno.add_argument("--timeout", type=int, default=None,
                     help="seconds any one task may take (default: 1800 for "
                          "agents, 600 for --via-script)")
    mno.add_argument("--via-script", action="store_true", dest="via_script",
                     help="run each task's stored validation_script instead of "
                          "spawning an agent. This only RE-CHECKS tasks; it does "
                          "not do their work")
    mno.add_argument("--isolation", choices=("none", "worktree"), default=None,
                     help="`worktree` (the DEFAULT for agent runs) gives each agent "
                          "its own git worktree and ledger segment, so a wave runs "
                          "in PARALLEL. `none` runs them one at a time in the "
                          "current checkout — they would corrupt each other's "
                          "edits otherwise, so it is the unsafe mode, not the safe "
                          "one")
    mno.add_argument("--allow-shared-tree", action="store_true",
                     dest="allow_shared_tree",
                     help="let agents run concurrently in ONE working tree. They "
                          "will collide on each other's edits and the git index "
                          "— without this, agents run one at a time")
    mno.add_argument("--project", default=None)
    mno.add_argument("--session", default=None)
    mno.add_argument("--json", action="store_true")
    mno.set_defaults(func=cmd_mindnet_orchestrate)

    # MN-6's blast-radius gate. Bare = status; the two directions are mutually
    # exclusive because `--enable --disable` has no defensible reading and
    # silently picking one is how a toggle for a repo-freezing rule goes wrong.
    mnx = mnsub.add_parser("enforce", help="read or toggle whether MN-4's "
                           "PreToolUse teeth may deny anything in THIS "
                           "checkout (opt-in; dormant by default)")
    mnxg = mnx.add_mutually_exclusive_group()
    mnxg.add_argument("--enable", action="store_true",
                      help="arm enforcement for this checkout root")
    mnxg.add_argument("--disable", action="store_true",
                      help="disarm it for this checkout root — recorded, so it "
                           "also out-ranks an armed parent checkout")
    mnx.add_argument("--project", default=None)
    mnx.add_argument("--session", default=None)
    mnx.add_argument("--json", action="store_true")
    mnx.set_defaults(func=cmd_mindnet_enforce)

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
