# SPDX-License-Identifier: Apache-2.0
"""Ledger-facing `hive` handlers: `append`, the live-hook dispatcher `hook`,
`session run|install-hook`, `project`/`projects`, and the mandatory
code change-log group `log`. The hook helpers (`_hook_payload`,
`_hook_prompt`, `_hook_session`) live here beside `cmd_hook`, their only
caller — the surface every Claude Code hook stage enters through.
"""
import json
import os
import sys
from .. import events, projector, paths

__all__ = ["cmd_append", "cmd_hook", "rotation_supervisor_defaults",
    "cmd_session_run", "cmd_session_install_hook", "cmd_project",
    "cmd_projects", "cmd_log_add", "cmd_log_queue", "cmd_log_show",
    "cmd_log_pending", "_hook_payload", "_hook_prompt", "_hook_session"]


def cmd_append(args) -> int | None:
    sop_write = args.type == "sop"
    if sop_write:
        # One registry, one authorship gate, whichever door is used (blueprint
        # P1d): `hive sop add` refusing an agent means nothing while `hive
        # append sop` beside it does not. Every other type is untouched. Asked
        # BEFORE the payload is read or parsed, as `hive sop add` asks before it
        # reads a body: a refused writer gets the refusal, not a JSON error.
        from .. import advice as authorship
        refusal = authorship.sop_write_refusal(
            args.session or os.environ.get("MINDHIVE_SESSION"), "append")
        if refusal:
            print(f"error: {refusal}", file=sys.stderr)
            return 2
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
    if sop_write:
        tags = authorship.write_tags(tags)
    ev = events.append_event(args.type, payload, session_id=args.session, tags=tags)
    print(f"appended {ev.type} @ {ev.ts}")
    return None

def cmd_hook(args) -> int | None:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if args.stage == "post_tool_use":
        # Imported in this branch too: a later branch imports it, which makes the
        # name function-local for the WHOLE function, so referencing it here
        # without its own import is an UnboundLocalError on every tool call.
        from .. import hookcapture, promptgate
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
        from .. import hookcapture
        print(json.dumps(hookcapture.record_post_tool_use_failure(raw)))
    elif args.stage == "pre_tool_use":
        # Deny the ineffective tool form for enabled-enforcement signatures.
        # Print ONLY on a deny (stdout is the hook's decision channel); an allow
        # prints nothing so the hook stays a silent no-op.
        from .. import enforce
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
        from ..rotation import hook as rotation_hook
        decision = rotation_hook.hook_decision(raw)
        if decision:
            print(json.dumps(decision))
    elif args.stage == "user_prompt_submit":
        # The seven-pass injection pipeline, and the load-bearing ORDER it runs
        # in, live in `injectors.run_pipeline` -- see that docstring. This branch
        # owns only what surrounds it: the one spine read, the print, and the
        # two best-effort telemetry writes that must follow the print.
        from .. import injectors as _inj, promptgate
        from ..mindnet import inject as mn_inject
        # Read ONCE here and threaded in, rather than letting the injector read
        # it and then reading it again to ask what it used (MN-17):
        # `record_direction` and `announce_faculties` below need this same spine.
        try:
            from ..mindnet import sidecar as mn_sidecar
            spine = mn_sidecar.read()
        except Exception:                                   # noqa: BLE001
            spine = None
        context, _rows = _inj.run_pipeline(raw, spine=spine)
        if context:
            print(json.dumps(context))
        # AFTER the print, for the reason `record_direction` below documents:
        # stdout is the context channel and a telemetry write must never be able
        # to interleave with it. Guarded twice -- `record_turn` is already total,
        # and this `except` is the second belt, because the one thing this
        # feature must never do is break the prompt it exists to observe.
        try:
            _inj.record_turn(_rows, session_id=_hook_session(raw))
        except Exception:                                   # noqa: BLE001
            pass
        # Durable causal trace: record which skills the gate directed so Oversee
        # can later show the honest "directed by prompt-gate" why. Best-effort
        # and self-guarding -- never disturbs the context printed above.
        promptgate.record_direction(raw, sc=spine)
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
                                          session_id=_hook_session(raw), sc=spine)
        # MN-18: bring a task into existence so the spine has something to speak about.
        # Nothing else in the tree did this — `hive mindnet declare` is hand-run and
        # `decompose` needs a parent — so the phase spine, the SOP section and the
        # Improvements section were built and reached by no prompt.
        #
        # Here, in the side-effect layer, for the same reasons the two calls above are:
        # the printed context is the product, and this must not be able to disturb it. The
        # sidecar read above is reused rather than repeated, and there is deliberately no
        # rebuild: a full fold is the single most expensive thing this path could do, and
        # the next prompt's own write would invalidate the result anyway.
        #
        # Measured 2026-08-14 on the live store (35,202 events), min of 5:
        # `events.read_all()` 430 ms (the "0.36 s" this comment used to cite — the ledger
        # grew). The ~0.4 s budget the two copies of this note claimed is NOT met: the
        # session-start path (`hive state`, below) measures 699 ms end to end, worst of 5
        # 1468 ms. Stated rather than quietly relabelled — the figure was a real target
        # and the path is over it. What the budget SHOULD be is an open question, and
        # deliberately not answered here: see docs/plans/2026-08-14-001.
        try:
            from ..mindnet import autodeclare as mn_auto
            mn_auto.maybe_declare(_hook_prompt(raw), spine,
                                  session_id=_hook_session(raw))
        except Exception:                                   # noqa: BLE001
            pass
    return None

def rotation_supervisor_defaults():
    """Spin-guard defaults, read from the supervisor rather than duplicated as
    argparse literals — two copies of a safety bound drift, and the copy that
    drifts is always the one nobody is looking at."""
    from ..rotation import supervisor
    return supervisor.MAX_ROTATIONS, supervisor.SPIN_WINDOW_S

def cmd_session_run(args) -> int | None:
    """Supervise an interactive Claude Code session, rotating it when the
    smart-zone boundary is crossed. Returns the supervisor's own exit code so a
    tripped spin guard is visible to whatever launched us."""
    from ..rotation import supervisor
    result = supervisor.run(project=getattr(args, "project", None),
                            max_rotations=args.max_rotations,
                            window_s=args.spin_window)
    print(f"{result.reason}: {result.spawns} session(s), "
          f"{result.rotations} rotation(s)"
          + (f" — {result.detail}" if result.detail else ""))
    return result.exit_code or None

def cmd_session_install_hook(args) -> int | None:
    from ..rotation import install as install_mod
    result = install_mod.install(args.settings, args.command, dry_run=args.dry_run)
    print(json.dumps(result))
    if not result["ok"]:
        return 2
    return None

def cmd_project(args) -> int | None:
    print(json.dumps(projector.project()))
    return None

def cmd_projects(args) -> int | None:
    """List namespaces on disk, marking each registered (in the autosync
    allow-list) or unregistered — surfacing junk/orphan namespaces without
    deleting anything. Registration is `hygiene.registered`'s answer, the one
    `hive gc --prune` acts on, so the two cannot disagree."""
    from .. import hygiene
    names = paths.list_namespaces()
    note = hygiene.unlisted_note(paths.unlisted_namespaces())
    if not names:
        print("(no namespaces on disk yet)")
        if note:
            print(note)
        return None
    reg = hygiene.registered(names)
    for name in names:
        flag = "registered" if reg[name] else "unregistered"
        print(f"  [{flag:12}] {name}")
    unregistered = [n for n in names if not reg[n]]
    print(f"\n{len(names)} namespace(s): "
          f"{len(names) - len(unregistered)} registered, "
          f"{len(unregistered)} unregistered")
    if note:
        print(note)
    return None

def cmd_log_add(args) -> int | None:
    from .. import changelog as changelog_mod
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
    from .. import changelog as changelog_mod
    sid = args.session or os.environ.get("MINDHIVE_SESSION")
    marker = changelog_mod.queue(events.read_all(), session_id=sid)
    if marker:
        print(f"queued {len(marker['files'])} unlogged source file(s) — a "
              "change-log entry is required: hive log <category> \"…\"")
    else:
        print("no unlogged code changes")
    return None

def cmd_log_show(args) -> int | None:
    from .. import changelog as changelog_mod
    print(json.dumps(changelog_mod.entries(events.read_all()),
                     indent=2, ensure_ascii=False))
    return None

def cmd_log_pending(args) -> int | None:
    from .. import changelog as changelog_mod
    print(json.dumps(changelog_mod.load_pending(), indent=2))
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
