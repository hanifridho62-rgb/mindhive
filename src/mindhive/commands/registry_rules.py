# SPDX-License-Identifier: Apache-2.0
"""Prevention-rule handlers: the `rules` group (list, summaries, show,
reflect, resolve, reconcile, enforce, audit, inject, promote, demote)
and the trained `triggers` group (train, list, check).
"""
import json
from .. import paths
from .maintenance import _print_incident_lines

__all__ = ["cmd_rules_list", "cmd_rules_summaries", "cmd_rules_show",
    "_safe_reflect_batch", "cmd_rules_reflect", "cmd_rules_resolve",
    "cmd_rules_reconcile", "cmd_rules_enforce", "cmd_rules_audit",
    "cmd_rules_inject", "cmd_rules_promote", "cmd_rules_demote",
    "cmd_triggers_train", "cmd_triggers_list", "cmd_triggers_check"]


def cmd_rules_list(args) -> int | None:
    _print_incident_lines(args.project, "(no incidents yet)")
    return None

def cmd_rules_summaries(args) -> int | None:
    """Structured lesson records (the same shape the dashboard Improvements view
    reads) for an agent working from the CLI. --json emits the raw list; the
    default is a human-readable digest per signature. Both carry resolution +
    triage (who should act: claude vs dismiss) — CLI/HTTP parity with the
    dashboard's Improvements cards."""
    from .. import events as ev_mod
    from .. import incidents as inc
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
    from .. import incidents as inc
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
    from .. import reflect as reflect_mod
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
    from .. import resolve as resolve_mod
    if args.list:
        from .. import incidents as inc
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
    from .. import incidents as inc
    print(json.dumps(inc.reconcile(project=args.project)))
    return None

def cmd_rules_enforce(args) -> int | None:
    """Manage the PreToolUse enforcement manifest — the enforce rung of the
    nudge->enforce ladder. Default action prints the manifest."""
    from .. import enforce
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
    from .. import incidents as inc
    res = inc.audit(project=args.project)
    inc.project(project=args.project)
    print(json.dumps(res))
    return None

def cmd_rules_inject(args) -> int | None:
    from .. import incidents as inc
    block = inc.inject(project=args.project)
    if block:
        print(block)
    return None

def cmd_rules_promote(args) -> int | None:
    from .. import incidents as inc
    print(json.dumps(inc.promote(args.signature, project=args.project)))
    return None

def cmd_rules_demote(args) -> int | None:
    from .. import incidents as inc
    print(json.dumps(inc.demote(args.signature, project=args.project)))
    return None

def cmd_triggers_train(args) -> int | None:
    from .. import promptgate
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
    from .. import promptgate
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
    from .. import promptgate
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
