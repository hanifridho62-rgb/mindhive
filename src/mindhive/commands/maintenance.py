# SPDX-License-Identifier: Apache-2.0
"""Store-hygiene handlers: `recall`, `gc`, `export`, `embed`, the
thermo-nuclear `review` queue, `consolidate`, `flag`, `enrich` and the
`failures` incident group. `verify` and `selftest` live in
`verification.py` since blueprint P9b (MEB CLI-02).
"""
import json
import os
import sys
from .. import (events,
                paths,
                recall as recall_mod,
                export as export_mod)

__all__ = ["cmd_recall", "cmd_gc", "cmd_export", "cmd_embed",
    "cmd_review_queue", "cmd_review_clear", "cmd_review_show",
    "cmd_flag", "cmd_consolidate", "cmd_failures_capture",
    "_print_incident_lines", "cmd_failures_list", "cmd_enrich"]


def cmd_recall(args) -> int | None:
    """Exit 1 when the semantic index could not be read, 2 when the search
    was refused (LIVE_LOCK, extras missing) -- never an empty result for
    either, which would read as "nothing relevant was ever recorded"."""
    project = getattr(args, "project", None)
    if project is None and paths.is_freshly_minted():
        print(f"warning: no memory for detected project "
              f"'{paths.detect_project()}' — searching an empty namespace. Pass "
              f"--project or see `hive projects`.", file=sys.stderr)
    from ..vectors import SemanticQueryError
    try:
        hits = recall_mod.recall(args.query, project=project,
                                 semantic=args.semantic, limit=args.limit,
                                 force=args.force)
    except SemanticQueryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        if not args.semantic:
            raise
        print(f"error: {e}", file=sys.stderr)
        return 2
    for h in hits:
        score = h.get("score")
        tag = f" [score {score:g}]" if isinstance(score, (int, float)) else ""
        # Two hit shapes reach here. A FILE hit carries `file`; an EVENT
        # hit carries `type`/`ts`/`session_id` and no filename, because the
        # thing found is a memo rather than a document. Rendering the
        # event's own coordinates is the point of indexing events at all --
        # 'decision, 2026-08-15, session manual' is citable,
        # 'LEDGER/decisions.md' is not.
        if "file" in h:
            head = h["file"]
        else:
            head = f"{h.get('type') or 'event'} · {h.get('ts') or 'no ts'}"
            if h.get("session_id"):
                head += f" · {h['session_id']}"
        print(f"{head}{tag}")
        print(f"  {h['snippet']}")
    return None

def cmd_gc(args) -> int | None:
    from .. import hygiene
    if not args.prune:
        print(hygiene.report())
        return None
    if not (args.target or args.orphans or args.graph_blobs):
        print("error: --prune requires a target: a namespace name, --orphans, "
              "or --graph-blobs", file=sys.stderr)
        return 2
    # Refuse destructive store ops while V17 is live-trading (defense in depth).
    from .. import guard
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
    """Index BOTH collections: the LEDGER projections and the events.

    Both, not either, because they answer different questions -- "which file
    covers this topic" against "which memo said this" -- and an operator who ran
    `hive embed` and got only one of them has a semantic layer that silently
    half-works. The event pass is incremental, so the second run costs a scan
    rather than 37k encodes.

    `--rebuild` (blueprint P1h) drops both collections first. It is the only
    way to re-tune an EXISTING index: chroma ignores the metadata argument for
    a collection that already exists, so a collection built under an older
    `HNSW_METADATA` keeps those parameters until something deletes it.

    LIVE_LOCK and missing extras are refusals: one `error:` line and exit 2,
    as `hive recall --semantic` answers them, never a traceback.

    `--project` names the namespace verbatim: it is what a failed semantic
    read tells the operator to run, and MINDHIVE_PROJECT would be normalised
    (`my_proj` -> `my-proj`) into another namespace.

    A write that fails is one `error:` line and exit 1, never a traceback:
    measured on an `events` collection whose configuration chromadb could
    not deserialize, `--rebuild` rebuilt `ledger` and then raised out of
    `hive`. The line says what was written before the failure and the
    repair `vector_errors.embed_failure` names for it. An interrupt stops
    the embed as it is."""
    from .. import guard, vector_errors, vectors
    try:
        guard.ensure_safe(force=args.force)
        vectors._require_deps()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    rebuild = bool(getattr(args, "rebuild", False))
    project = getattr(args, "project", None)
    out, done = {}, []
    for key, name, run in (("files", vectors.LEDGER_COLLECTION, vectors.embed),
                           ("events", vectors.EVENT_COLLECTION,
                            vectors.embed_events)):
        try:
            out[key] = run(project=project, force=args.force, rebuild=rebuild)
        except BaseException as e:                 # noqa: BLE001 -- see is_failure
            if not vector_errors.is_failure(e):
                raise
            print("error: " + vector_errors.embed_failure(
                name, e, project, rebuild=rebuild, done=done), file=sys.stderr)
            return 1
        done.append(name)
    print(json.dumps(out))
    return None

def cmd_review_queue(args) -> int | None:
    from .. import review as review_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    marker = review_mod.queue(events.read_all(), session_id=sid)
    if marker:
        print(f"queued {len(marker['files'])} file(s) in "
              f"{marker['batch_count']} batch(es) for thermo-nuclear review")
    else:
        print("nothing in scope to review")
    return None

def cmd_review_clear(args) -> int | None:
    from .. import review as review_mod
    print("cleared pending review" if review_mod.clear()
          else "no pending review")
    return None

def cmd_review_show(args) -> int | None:
    from .. import review as review_mod
    print(json.dumps(review_mod.load_pending(), indent=2))
    return None

def cmd_flag(args) -> int | None:
    """Self-flag a judgment error (KTD4): appends a tool_failed event carrying a
    judgment:flag:<slug> signature, so the entire existing incident projection
    works unchanged — no new event type. Flags activate at occurrence 1."""
    from .. import incidents as incidents_mod, signature as sig_mod
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
    from .. import consolidate as consolidate_mod
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
    from .. import failures as failures_mod, harvest as harvest_mod, incidents as incidents_mod
    # Under the same cross-process mutex `harvest.catch_up` holds around its own
    # `capture_dir` call. `capture_dir` stamps every `tool_failed` row with the
    # transcript line's own timestamp and dedups against a snapshot taken BEFORE
    # the walk, so a pass racing the dashboard's auto-harvest sweep over the same
    # directory mints byte-identical rows the append-only, hash-chained ledger
    # keeps for good. Refused the way `hive harvest --catch-up` is: report
    # `locked`, append nothing, exit 0 — rerun once that pass is over.
    with harvest_mod._harvest_lock() as got:
        if not got:
            print(json.dumps({"new_failures": 0, "files": 0, "locked": True}))
            return None
        res = failures_mod.capture_dir(args.transcripts, project=args.project)
        # Advance the rule lifecycle on capture: stamp activation for signatures
        # that just crossed the recurrence threshold, so leak-based auto-demotion
        # is reachable from the natural flow without a separate `hive rules audit`.
        incidents_mod.audit(project=args.project)
        incidents_mod.project(project=args.project)
    print(json.dumps(res))
    return None

def _print_incident_lines(project, empty_msg) -> None:
    """One `<n>x  [status] <signature>` line per incident, occurrence-ordered.

    Status is the PROJECTED status, not a re-derivation from the occurrence
    count. Counting alone cannot see the lifecycle: an expired, demoted or
    promoted incident still has every occurrence it ever had, so the old
    `occurrences >= threshold -> "active"` shortcut printed `47x [active]` for
    a bucket the record itself already called `expired` — which is exactly how
    a retired signature goes on looking alive from the CLI.
    """
    from .. import incidents as inc
    acc = inc._collect(project)
    if not acc:
        print(empty_msg)
        return
    projected = {s.get("signature"): s.get("status") or ""
                 for s in inc.summaries(project, include_expired=True)}
    for signature, rec in sorted(acc.items(), key=lambda kv: -kv[1]["occurrences"]):
        status = projected.get(signature) or (
            "active" if rec["occurrences"] >= inc._threshold_for(signature)
            else "candidate")
        print(f"{rec['occurrences']:>3}x  [{status:9}] {signature}")

def cmd_failures_list(args) -> int | None:
    _print_incident_lines(args.project, "(no failures captured yet)")
    return None

def cmd_enrich(args) -> int | None:
    from .. import enrich as enrich_mod
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
