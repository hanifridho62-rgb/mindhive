# SPDX-License-Identifier: Apache-2.0
"""Roadmap and auto-resume handlers: the `roadmap` group (set, step,
interrupt, resume, complete, archive, link, merge-state, retire,
autoclose, list, show) and the `autoresume` delivery group. They share
`_cancel_pending_autoresume`, which is why the two groups sit together.
"""
import json
import os
import sys
from .. import events

__all__ = ["_roadmap_steps", "_roadmap_work_link", "_cmd_roadmap_write",
    "cmd_roadmap_set", "cmd_roadmap_step", "cmd_roadmap_interrupt",
    "_cancel_pending_autoresume", "cmd_roadmap_resume", "cmd_roadmap_complete",
    "cmd_roadmap_archive", "cmd_roadmap_link", "_print_merge_answer",
    "cmd_roadmap_merge_state", "cmd_roadmap_retire", "cmd_roadmap_autoclose",
    "cmd_roadmap_list", "cmd_roadmap_show", "cmd_autoresume_schedule",
    "cmd_autoresume_list", "cmd_autoresume_cancel", "cmd_autoresume_fire",
    "cmd_autoresume_wait"]


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

def _roadmap_work_link(args) -> dict:
    """The `--branch/--pr/--commit` trio as `roadmap.record` keywords.

    An omitted flag stays absent rather than becoming an empty value, because
    the fold carries the recorded link forward across events: attaching a PR
    number next week must not erase the branch recorded today."""
    link = {}
    if getattr(args, "branch", None):
        link["work_branch"] = args.branch
    if getattr(args, "pr", None):
        link["work_pr"] = args.pr
    if getattr(args, "commit", None):
        link["work_commits"] = list(args.commit)
    return link

def _cmd_roadmap_write(args, action) -> int | None:
    from .. import roadmap as roadmap_mod
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
        elif action == "archive":
            kwargs.update(reason=args.reason)
        kwargs.update(_roadmap_work_link(args))
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
    from .. import autoresume as autoresume_mod
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
    from .. import autoresume as autoresume_mod
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

def cmd_roadmap_archive(args) -> int | None:
    """Retire a plan whose last step nothing can verify, on a stated reason.
    The honest third exit, next to `complete` (it finished) and leaving it open
    (it has not)."""
    rc = _cmd_roadmap_write(args, "archive")
    if rc:
        return rc
    _cancel_pending_autoresume(args)
    return None

def cmd_roadmap_link(args) -> int | None:
    """Point a plan at the code it produced, without touching its status."""
    return _cmd_roadmap_write(args, "link")

def _print_merge_answer(plan_id, answer, link_text) -> None:
    """One merge answer, said plainly. `unknown` prints the reason it is
    unknown, because "we could not check" and "it has not landed" are different
    facts and a reader who cannot tell them apart has learned nothing."""
    from .. import repo as repo_mod
    marks = {repo_mod.MERGED: "merged", repo_mod.CLOSED: "closed unmerged",
             repo_mod.OPEN: "not landed yet", repo_mod.UNKNOWN: "unknown"}
    head = f"{plan_id}: {marks.get(answer['state'], answer['state'])}"
    if link_text:
        head += f"  [{link_text}]"
    print(head)
    if answer["evidence"]:
        print(f"  evidence: {answer['evidence']}")
    if answer["state"] == repo_mod.UNKNOWN:
        print(f"  reason: {answer['reason']}")
        if answer["detail"]:
            print(f"  detail: {answer['detail']}")

def cmd_roadmap_merge_state(args) -> int | None:
    """Probe one plan's work link against git and `gh`. Read-only: it never
    retires anything, so it is safe to run on a card you are only asking about."""
    from .. import repo as repo_mod, roadmap as roadmap_mod
    evs = events.read_all(getattr(args, "project", None))
    rec = roadmap_mod.get(evs, args.plan_id)
    if rec is None:
        print(f"error: no roadmap '{args.plan_id}' in the ledger",
              file=sys.stderr)
        return 2
    answer = repo_mod.merge_state(**roadmap_mod.work_link(rec))
    if args.json:
        print(json.dumps(dict(answer, plan_id=rec["plan_id"]), indent=2,
                         ensure_ascii=False))
        return None
    _print_merge_answer(rec["plan_id"], answer,
                        roadmap_mod.describe_work_link(rec))
    return None

def cmd_roadmap_retire(args) -> int | None:
    """Retire every live plan whose work link proves the work is finished.

    Prints what was examined, not only what changed: a run that retires nothing
    because every PR is still open has to be distinguishable from one that
    retired nothing because `gh` is missing."""
    from .. import roadmap as roadmap_mod
    proj = getattr(args, "project", None)
    outcomes = roadmap_mod.retire_landed(
        project=proj, session_id=args.session or os.environ.get(
            "MINDHIVE_SESSION"))
    if args.json:
        print(json.dumps(outcomes, indent=2, ensure_ascii=False))
        return None
    if not outcomes:
        print("(no live roadmaps to examine — hive roadmap list --all)")
        return None
    retired = [o for o in outcomes if o["action"]]
    for outcome in outcomes:
        _print_merge_answer(outcome["plan_id"], outcome,
                            f"-> {outcome['action']}" if outcome["action"]
                            else "")
    print(f"\n{len(retired)} of {len(outcomes)} roadmap(s) retired on evidence")
    if not retired:
        print("nothing retired: no examined plan's work link proved the work "
              "landed (see each reason above)")
    return None

def cmd_roadmap_autoclose(args) -> int | None:
    """Close the session's auto-synced roadmap (run by the SessionEnd hook).
    Silent no-op when the session has no auto-owned open plan — the hook must
    never fail or chatter."""
    from .. import roadmap as roadmap_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    if not sid:
        return None
    ev = roadmap_mod.autoclose_session(sid)
    if ev is not None:
        print(f"roadmap {ev.payload['action']}: {sid} @ {ev.ts}")
    return None

def cmd_roadmap_list(args) -> int | None:
    from .. import roadmap as roadmap_mod
    recs = roadmap_mod.plans(events.read_all(getattr(args, "project", None)),
                             include_completed=args.all)
    if args.json:
        print(json.dumps(recs, indent=2, ensure_ascii=False))
        return None
    if not recs:
        print('(no roadmaps — declare one: hive roadmap set <id> '
              '--prompt "…" --step "…")')
        return None
    # `-` for archived mirrors the `[-]` a skipped step already prints: retired
    # without having been finished.
    marks = {"active": ">", "interrupted": "!", "completed": "x",
             "archived": "-"}
    for i, r in enumerate(recs, 1):
        p = r["progress"]
        print(f"  {i}. {marks.get(r['status'], '?')} "
              f"[{r['plan_id']} v{r['version']}] "
              f"{r['title'] or r['prompt'][:60]}  "
              f"{p['done']}/{p['total']} done  ({r['status']})")
    print(f"\n{len(recs)} roadmap(s)")
    return None

def cmd_roadmap_show(args) -> int | None:
    from .. import roadmap as roadmap_mod
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
    if rec["status"] == roadmap_mod.ARCHIVED:
        print(f"archived {rec.get('archived_at', '')} "
              f"(reason: {rec.get('archive_reason', '')})")
    link = roadmap_mod.describe_work_link(rec)
    # Printed offline on purpose: `show` reads the ledger and nothing else, so
    # it stays instant and works with no network. `hive roadmap merge-state`
    # is the command that actually asks git and `gh`.
    print(f"work: {link}" if link
          else "work: (no link — hive roadmap link "
               f"{rec['plan_id']} --pr … --branch …)")
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

def cmd_autoresume_schedule(args) -> int | None:
    from .. import autoresume as autoresume_mod
    from .. import roadmap as roadmap_mod
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
    from .. import autoresume as autoresume_mod
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
    from .. import autoresume as autoresume_mod
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
    from .. import autoresume as autoresume_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    payload = autoresume_mod.fire(args.plan_id,
                                  project=getattr(args, "project", None),
                                  session_id=sid, timeout_s=args.timeout)
    print(f"autoresume fired: {args.plan_id} -> {payload['outcome']}"
          + (f" — {payload['detail']}" if payload.get("detail") else ""))
    return 1 if payload["outcome"] == "error" else None

def cmd_autoresume_wait(args) -> int | None:
    from .. import autoresume as autoresume_mod
    fired = autoresume_mod.wait(getattr(args, "project", None),
                                poll_s=args.poll)
    print(f"autoresume wait: {fired} delivery(ies) fired")
    return None
