# SPDX-License-Identifier: Apache-2.0
"""Dashboard, sync and replenishment handlers: `dashboard`, `sync`,
`harvest`, `autosync`, the knowledge `graph` group and the `replenish`
group. The module is `mindhive.commands.dashboard`; the web server it
starts is `mindhive.dashboard`, imported lazily inside the handler.
"""
import json
import os
import sys
from .. import events, projector

__all__ = ["_UNATTRIBUTABLE_SESSIONS", "cmd_replenish_run",
    "cmd_replenish_explain", "cmd_replenish_status", "cmd_graph_build",
    "cmd_graph_related", "cmd_graph_export", "cmd_dashboard", "cmd_ui",
    "cmd_sync", "cmd_harvest", "cmd_autosync_register", "cmd_autosync_run"]


# Session ids that exist but cannot own work: the hook's stdin-parse fallback and
# the store's default for anything appended outside a session.
_UNATTRIBUTABLE_SESSIONS = ("unknown", "manual")

def cmd_replenish_run(args) -> int | None:
    """One replenishment round for a session. Run by session_end BEFORE
    `roadmap autoclose`, so the injected steps exist when the plan is closed and
    therefore reach STATE.md in the same pass."""
    from .. import replenish as rp_mod, replenish_sinks as sink_mod
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
    from .. import replenish as rp_mod
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
    from .. import replenish as rp_mod
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

def cmd_graph_build(args) -> int | None:
    from .. import graph as graph_mod
    result = graph_mod.build(semantic=args.semantic)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return None

def cmd_graph_related(args) -> int | None:
    from .. import graph as graph_mod
    neighbors = graph_mod.related(args.query, depth=args.depth)
    print(json.dumps(neighbors, ensure_ascii=False, indent=2))
    return None

def cmd_graph_export(args) -> int | None:
    from .. import graphview
    dest = graphview.export(out=args.out, semantic=args.semantic)
    print(str(dest))
    if args.open_after:
        import webbrowser
        webbrowser.open(dest.as_uri())
    return None

def cmd_dashboard(args) -> int | None:
    """The 24/7 panel. Starts, and stays up until someone stops it.

    `--idle-cutoff` is the ONLY thing that can change that, and the branch
    below is written so the default path is byte-for-byte the call it has
    always been -- no `lifecycle=None` kwarg, so a panel someone expects to
    find tomorrow cannot acquire a way to shut itself down through a default
    that later moves. `hive ui` is the same page with the flag always on."""
    from .. import dashboard
    cutoff = getattr(args, "idle_cutoff", None)
    if cutoff is None:
        dashboard.serve(port=args.port, open_browser=not args.no_browser)
        return None
    from ..server.lifecycle import HeartbeatMonitor, default_interval_for
    try:
        # Derived, not defaulted, for the same reason `serve_ui` derives it:
        # a monitor refuses an interval that cannot fit inside its cutoff, so
        # `--idle-cutoff 5` would otherwise be a usage error rather than a
        # short run. At the blueprint's own numbers it returns 15.0.
        monitor = HeartbeatMonitor(idle_cutoff_s=cutoff,
                                   interval_s=default_interval_for(cutoff))
    except ValueError as e:
        # A cutoff no interval can fit under — zero or negative. Refuse
        # before binding, as a sentence rather than a traceback.
        print(f"error: {e}", file=sys.stderr)
        return 2
    dashboard.serve(port=args.port, open_browser=not args.no_browser,
                    lifecycle=monitor)
    return None

def cmd_ui(args) -> int | None:
    """The panel as a PAGE: it goes away once the tab does (blueprint P1k).

    The two exceptions caught here both come out of `serve_ui`'s preflight,
    which runs before anything binds: `HeartbeatMonitor` refuses numbers that
    cannot work (ValueError), and the ASGI adapter refuses to pretend the
    extras are installed (RuntimeError, naming the pip line). Printing the
    exception's own text rather than a sentence of our own is deliberate --
    it stays accurate whatever raised it, and the extras message already
    says exactly what to type.

    `--idle-cutoff` absent, the cutoff is MINDHIVE_UI_IDLE_CUTOFF_S when that
    holds a positive number of seconds, else the blueprint's 60 (P8) -- read
    through `lifecycle.idle_cutoff_from_env`, which also hands back the one
    sentence to print when the variable held something it could not use.
    The flag beats the variable, and then the variable is not even read, so
    a bad value it holds is not reported for a run it did not decide.
    `hive dashboard` never reads it; the helper's docstring says why."""
    from ..server import app
    from ..server.lifecycle import idle_cutoff_from_env
    cutoff = args.idle_cutoff
    if cutoff is None:
        cutoff, note = idle_cutoff_from_env()
        if note:
            print(f"hive ui: {note}", file=sys.stderr)
    try:
        app.serve_ui(port=args.port, open_browser=not args.no_browser,
                     idle_cutoff_s=cutoff, asgi=args.asgi)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return None

def cmd_sync(args) -> int | None:
    from .. import sync as sync_mod
    folder = args.folder or os.getcwd()
    native_path = sync_mod.sync(folder, include_briefing=not args.no_briefing)
    print(str(native_path))
    # Say it here, where the file was just written. The digest's caps bound item
    # counts, not lines, so this is the moment the file can cross the loader's
    # limit — and the loader's own warning appears in the NEXT session, to the
    # agent rather than to the person who ran sync.
    from .. import context as context_mod
    pressure = context_mod.memory_pressure(native_path)
    if pressure["readable"] and pressure["truncated"]:
        print(f"warning: {context_mod.pressure_headline(pressure)}; the loader "
              f"will not reach: {context_mod.pressure_lost(pressure)}. "
              f"See `hive context --check`.", file=sys.stderr)
    return None

def cmd_harvest(args) -> int | None:
    from .. import harvest as harvest_mod
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
    from .. import autosync as autosync_mod
    print(json.dumps(autosync_mod.register(args.project, args.transcripts, args.folder),
                     ensure_ascii=False, indent=2))
    return None

def cmd_autosync_run(args) -> int | None:
    from .. import autosync as autosync_mod
    print(json.dumps(autosync_mod.run_all(), ensure_ascii=False, indent=2))
    return None
