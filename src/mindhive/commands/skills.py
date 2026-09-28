# SPDX-License-Identifier: Apache-2.0
"""Skill and plugin catalog handlers: the `plugins` group (scan, autoscan,
recommend, list) and the `skills` group (list, invoke, status, stats,
record, recommend, trail).
"""
import json
import sys
from .. import events, paths

__all__ = ["_print_recommendations", "cmd_plugins_scan",
    "cmd_plugins_autoscan", "cmd_plugins_recommend", "cmd_plugins_list",
    "_print_cache_status", "_print_sync", "_cache_check_hook",
    "cmd_plugins_cache_check",
    "cmd_skills_list", "cmd_skills_invoke", "cmd_skills_status",
    "cmd_skills_stats", "cmd_skills_record", "cmd_skills_recommend",
    "cmd_skills_trail"]


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

def cmd_plugins_scan(args) -> int | None:
    from .. import plugins as plugins_mod
    payload = plugins_mod.scan_and_store(cache_dir=args.cache, top_n=args.top)
    _print_recommendations(payload, scanned=True)
    return None

def cmd_plugins_autoscan(args) -> int | None:
    from .. import plugins as plugins_mod
    res = plugins_mod.ensure_fresh(cache_dir=args.cache, top_n=args.top,
                                   max_age_hours=args.max_age_hours, force=args.force)
    if not res["scanned"]:
        print(f"catalog fresh ({res['age_hours']:.1f}h old) - skipping scan")
    _print_recommendations(res["payload"], scanned=res["scanned"])
    return None

def cmd_plugins_recommend(args) -> int | None:
    from .. import plugins as plugins_mod
    payload = plugins_mod.latest_catalog()
    if not payload:
        print("(no plugin_catalog yet - run: hive plugins scan)")
    else:
        _print_recommendations(payload, scanned=False)
    return None

def cmd_plugins_list(args) -> int | None:
    """The whole stored catalog — free and paid — as opposed to `recommend`,
    which prints the top-N free picks.

    Reads the snapshot on disk and never rescans: walking the plugin cache is
    what `scan` and `autoscan` are for, and a `list` that silently paid that
    cost would be a trap on the one command people reach for to look around."""
    from .. import plugins as plugins_mod
    as_json = getattr(args, "json", False)
    payload = plugins_mod.latest_catalog()
    if not payload:
        # The hint goes to stderr under --json and stdout otherwise. `--json` is
        # a stream contract, and the empty case is exactly the one a machine
        # that has never scanned hits first — prose on stdout there means the
        # command is unparseable precisely when it is least expected to be.
        print("(no plugin_catalog yet - run: hive plugins scan)",
              file=sys.stderr if as_json else sys.stdout)
        if as_json:
            print("{}")
        return None
    if as_json:
        print(json.dumps(payload, indent=2))
        return None
    c = payload.get("counts", {})
    print(f"{c.get('plugins_total', 0)} plugins · {c.get('free', 0)} free · "
          f"{c.get('paid', 0)} paid · {c.get('skills_total', 0)} skills "
          f"(scanned {payload.get('scanned_at', '?')})")
    for p in payload.get("free_plugins", []):
        score = p.get("score", {}) or {}
        eff = score.get("effectiveness", {}) or {}
        flag = (f"  [demoted: {eff.get('demote_reason', '')}]"
                if eff.get("demoted") else "")
        print(f"  {p.get('name', '?')} {p.get('version', '')} "
              f"· breadth {score.get('breadth_score', 0)} "
              f"· rank {score.get('rank_score', 0)}{flag}")
    for p in payload.get("paid_plugins", []):
        print(f"  {p.get('name', '?')} {p.get('version', '')} "
              f"  [paid: {p.get('free_reason', '')}]")
    return None

def _print_cache_status(status: dict, plugin_cache) -> None:
    state = status.get("state")
    if state == "in_sync":
        print(f"{status.get('plugin')} {status.get('repo_version')}: plugin cache "
              f"in sync ({status.get('key')} at {status.get('install_path')}, "
              f"tree {str(status.get('repo_hash') or '')[:8]})")
        return
    if state not in plugin_cache.DRIFT_STATES:
        print(f"nothing to compare ({state}): {status.get('reason') or ''}")
        return
    print(plugin_cache.session_notice(status))
    counts = status.get("diff_counts") or {}
    for label, field in (("only in the cache", "only_in_cache"),
                         ("only in this checkout", "only_in_repo"),
                         ("changed", "changed")):
        listed = status.get(field) or []
        for rel in listed:
            print(f"  {label}: {rel}")
        hidden = int(counts.get(field) or 0) - len(listed)
        if hidden > 0:
            print(f"  {label}: ... {hidden} more")


def _print_sync(res: dict, stream=None) -> None:
    out = stream or sys.stdout
    if res.get("ok"):
        print(f"[mindhive] plugin cache synced ({' '.join(res.get('argv') or [])}); "
              "restart Claude Code to load it", file=out)
        return
    detail = " ".join(str(x) for x in (res.get("reason"), res.get("output")) if x)
    print(f"[mindhive] plugin cache sync failed: {detail}", file=out)


def _cache_check_hook(plugin_cache, args) -> int:
    status = plugin_cache.cache_status(repo_root=args.repo_root,
                                       claude_dir=args.claude_dir)
    note = plugin_cache.session_notice(status)
    if not note:
        return 0
    print(note)
    if plugin_cache.autosync_enabled():
        _print_sync(plugin_cache.sync_cache(status))
    return 0


def cmd_plugins_cache_check(args) -> int | None:
    """Bug 8: is the plugin Claude Code loads the one this checkout holds?

    Exit 0 in sync (or with nothing to compare), 1 on drift; with `--sync` the
    update's own outcome decides. `--hook` is the session-start form: a one-line
    notice only on drift, the sync only when MINDHIVE_PLUGIN_AUTOSYNC is set,
    and exit 0 whatever happens -- the hook fires in every session on the
    machine, so a broken check may cost its own line and nothing else."""
    from .. import plugin_cache
    if getattr(args, "hook", False):
        try:
            return _cache_check_hook(plugin_cache, args)
        except Exception:
            return 0
    status = plugin_cache.cache_status(repo_root=args.repo_root,
                                       claude_dir=args.claude_dir)
    drifted = status["state"] in plugin_cache.DRIFT_STATES
    as_json = getattr(args, "json", False)
    if as_json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        _print_cache_status(status, plugin_cache)
    if getattr(args, "sync", False) and drifted:
        res = plugin_cache.sync_cache(status)
        _print_sync(res, sys.stderr if as_json else None)
        return 0 if res.get("ok") else 1
    return 1 if drifted else 0

def cmd_skills_list(args) -> int | None:
    from .. import plugins as plugins_mod
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
    from .. import invoker
    res = invoker.invoke_skill(args.name, plugin=args.plugin, project=args.project)
    status = "ok" if res["ok"] else "FAILED"
    print(f"skill {args.name}: {status} (exit {res['exit']})")
    if res.get("excerpt"):
        print(res["excerpt"])
    return 0 if res["ok"] else 1

def cmd_skills_status(args) -> int | None:
    from .. import plugins as plugins_mod
    status = plugins_mod.call_status()
    if not status:
        print("(no skill calls recorded yet)")
    for name, s in sorted(status.items()):
        print(f"  {name}: {s}")
    return None

def cmd_skills_stats(args) -> int | None:
    from .. import plugins as plugins_mod
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
    from .. import plugins as plugins_mod
    from .. import recommend as recommend_mod
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
    from .. import oversee as oversee_mod
    from .. import paths
    iu = oversee_mod.intended_use_map()
    names = (paths.event_namespaces() if getattr(args, "all", False)
             else [args.project])
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
