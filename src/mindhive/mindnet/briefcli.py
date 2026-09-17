# src/mindhive/mindnet/briefcli.py
"""`hive mindnet brief` — see what a spawned agent would actually be told.

## Why a subcommand and not a flag

The obvious shape was `hive mindnet orchestrate --preview-brief`. It did not
fit: `__main__.py` sat AT its shrink-only ratchet (`tests/test_file_size_cap.py`)
when this was written, and an argparse flag plus its branch did not fit in what
that left. That file's own comment said the fix was splitting it and that
"raising a ceiling is not it" — twice. (It was split, on 2026-09-14, blueprint
P9: the wiring is now `commands/parsers/mindnet.py`, held at or under 500 lines
by `tests/test_commands_reexport.py`.) Deliberately stated without the
arithmetic: an earlier version of this
paragraph quoted a line count that the same change then invalidated, and a reader
budgeting against a stale number overspends.

The same file recorded the way out, also twice: `bench.register` and
`pgcli.register` put ~200 lines of behaviour in a module and cost the parser
module three lines. This is the third, and it registers on the `mindnet`
subparser rather than
the top level, which is the only new thing about it.

One consequence worth writing down, because it looks like a typo otherwise: the
registration comment in `commands/parsers/__init__.py` names `bench` but
deliberately does NOT name the other sibling.
`test_the_registration_is_two_lines_in_main` counts raw source lines in
`commands/parsers/__init__.py` carrying that module's name, so a comment
mentioning it is indistinguishable from a third line of registration and turns a
correct guard red. The guard is right to be that literal — it exists to catch an
argparse block being inlined back in — so the comment moved instead.

The constraint turned out to pick the better design anyway. A flag can only show
you the briefs of a run you are about to start; a command can show you one task's
brief with nothing running at all, which is what an operator actually reaches for
when the answer is "the agent seems to know nothing about this task".

## What it prints, and why the counts come first

`advice` answers EMPTY when a task's words overlap nothing, and the symbol slice
is inert unless `MINDHIVE_SYMBOLS` is set — so a brief being empty is a normal
outcome with several distinct causes, and the counts are what tell them apart.
`gated` is the one that matters most: a source that is switched OFF reporting
zero is indistinguishable from a source that ran and found nothing, and those
need different fixes.

The full text is behind `--verbose` because it is a wall, and the question an
operator has when they run this is almost always "did anything arrive", not
"show me every line".
"""
from __future__ import annotations

import json
import sys

from .. import cliparse

from . import brief as br_mod

# What `hive mindnet` returns when it refuses. A local copy of
# `commands.mindnet_tasks.MN_REFUSED` (re-exported as `__main__.MN_REFUSED`)
# rather than an import, because the parser package imports THIS module to
# register the subcommand and the reverse import closes the cycle.
# `tests/test_cli_mindnet.py::test_brief_refuses_a_missing_task_the_way_its_siblings_do`
# pins the two equal, so the copy cannot drift. (The name cited here until
# 2026-08-30 — `test_briefcli_refuses_like_its_siblings` — greps to nothing; a
# citation that resolves to no test reads as coverage and is not any.)
REFUSED = 2


def register(subparsers) -> None:
    """Wire `brief` onto the `mindnet` subparser set.

    Two lines in the parser module (`commands/parsers/mindnet.py`) — an import
    and one call — instead of the ~40 an inline parser would cost against a
    ratchet with no headroom left. Same trade `bench.register` and
    `pgcli.register` took; see the module docstring."""
    p = subparsers.add_parser(
        "brief", help="show what a spawned agent would be told about a task "
                      "(reads only; starts nothing)")
    p.add_argument("task_id", nargs="?", default=None,
                   help="the task to brief; omitted, every task in the graph")
    # `--project`/`--session` are not optional extras: every sibling `mindnet`
    # subcommand takes both, and without them `cmd_brief`'s project lookup could
    # only ever answer None — the command was permanently pinned to the default
    # namespace while reading as if it were not (`/review`, 2026-08-29).
    p.add_argument("--project", type=cliparse.project_arg, default=None,
                   help="the project namespace to read (default: the active one)")
    p.add_argument("--session", type=cliparse.session_id_arg, default=None,
                   help="unused by this read-only command; accepted so the "
                        "`mindnet` subcommands take one flag set")
    p.add_argument("--verbose", action="store_true",
                   help="print the full briefing text, not just the counts")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="machine-readable output")
    p.set_defaults(func=cmd_brief)


def _rows(task_id=None, project=None):
    """``([(task, Brief)], WaveContext)`` for one task, or for the whole graph.

    The context comes back with the rows rather than being rebuilt: both callers
    read `ctx.gated` and re-resolve the phase against it, and resolving a second
    context to answer that would cost another full ledger read.

    Resolves ONE wave context and briefs every task against it — the same shape a
    real wave uses, so what this prints is what an agent would actually get
    rather than a second code path that agrees by coincidence."""
    from .. import events as ev_mod
    from .. import paths as p_mod
    from . import tasks as t_mod

    # The same union the wave uses, so this preview reads exactly what a real
    # wave reads. `tasks.fold` needs `mindnet_task`; everything else in the
    # tuple is for the `wave_context` call below, which receives THIS list.
    evs = ev_mod.read_types(br_mod.BRIEF_EVENT_TYPES, project)
    graph = t_mod.fold(evs)
    if task_id is not None:
        task = graph.get(str(task_id))
        wanted = [] if task is None else [task]
    else:
        wanted = [graph[k] for k in sorted(graph)]
    # The root is resolved and passed EXPLICITLY, the same way `orchestrator`
    # resolves it once and hands it down. Omitting it is not a smaller version of
    # passing it: `_symbol_slice` used to fall through to `symbols.default_root()`
    # -> `Path.cwd()`, and `cached_text` INDEXES the tree it is given and writes
    # `cache_path(project)`. This command's own help says it "reads only; starts
    # nothing", and it was indexing whatever directory the operator happened to be
    # standing in and leaving that behind for the next real wave to brief from.
    #
    # `worktree_root` answers None outside a checkout, which now gates the symbol
    # source rather than silently indexing the cwd — a preview run from anywhere
    # is worth slightly less and can no longer corrupt anything.
    ctx = br_mod.wave_context(project=project, evs=evs,
                              root=p_mod.worktree_root())
    return [(t, br_mod.build(t, ctx)) for t in wanted], ctx


def _describe(stats) -> str:
    """One line of counts, with the gated sources named rather than shown as 0.

    `failures`, `defused`, `dropped` and `authored` print ONLY when non-zero,
    unlike the four counts above. They are exceptions rather than measurements —
    a permanent `dropped=0  authored=0` on every line would train the operator to
    skim past exactly the segment that matters on the day it is not zero, which
    is the same argument the module docstring makes about an empty fence.

    `defused` is here because leaving it out reproduced the very symptom it was
    added to end. It counts a line the defuser CHANGED without emptying, which
    is the padded-banner case: `dropped` stays 0, so a brief under active attack
    printed a line byte-identical to a clean one and only a ledger query showed
    otherwise — `clean twice over`, the phrase the fence module uses for it. A
    counter that reaches the payload and not the operator has not been shipped.

    The `--json` branch carries them unconditionally, because a machine reader
    wants the key present rather than having to tell "absent" from "zero"."""
    parts = [f"skills={stats.skills}", f"sops={stats.sops}",
             f"constraints={stats.constraints}", f"symbols={stats.symbols}"]
    if stats.gated:
        parts.append("gated=" + ",".join(stats.gated))
    failures = tuple(getattr(stats, "failures", ()) or ())
    if failures:
        parts.append("failed=" + ",".join(failures))
    defused = int(getattr(stats, "defused", 0) or 0)
    if defused:
        parts.append(f"defused={defused}")
    dropped = int(getattr(stats, "dropped", 0) or 0)
    if dropped:
        parts.append(f"dropped={dropped}")
    authored = int(getattr(stats, "authored", 0) or 0)
    if authored:
        parts.append(f"withheld={authored}")
    return "  ".join(parts)


def cmd_brief(args) -> int | None:
    """Print the briefing for one task, or for the whole graph.

    Refuses with `REFUSED` on stderr when the named task does not exist — the
    same contract every sibling `mindnet` command holds. An EMPTY brief is NOT a
    refusal: it is a normal outcome with a reason attached, and exiting non-zero
    on it would make a CI step that wraps this fail for a task nobody has
    recorded anything about yet."""
    project = getattr(args, "project", None)
    task_id = getattr(args, "task_id", None)
    rows, ctx = _rows(task_id, project)

    if task_id is not None and not rows:
        # stderr and `REFUSED`, and BEFORE nothing: the first version printed
        # prose to stdout and returned 1, ahead of the `--json` branch, so
        # `--json` answered with non-JSON on the stream a caller was parsing.
        print(f"error: no task {task_id!r} on the graph — "
              f"`hive mindnet list` shows what is there", file=sys.stderr)
        return REFUSED

    if getattr(args, "as_json", False):
        print(json.dumps({
            "enabled": br_mod.enabled(),
            "gated": list(ctx.gated),
            # Context-level, beside `gated` and for the same reason: an operator
            # asking "why is every brief empty" needs to know the sidecar threw
            # before they go looking at the tasks.
            "failures": list(getattr(ctx, "failures", ()) or ()),
            # An OBJECT keyed by task id, matching `mindnet list --json`. An array
            # under the same key name in the same command family is two shapes one
            # consumer has to tell apart at runtime.
            "tasks": {t.task_id: {
                "task_id": t.task_id,
                "phase": br_mod.phase_for(ctx, t),
                "chars": len(b.text),
                "skills": b.stats.skills, "sops": b.stats.sops,
                "constraints": b.stats.constraints, "symbols": b.stats.symbols,
                "gated": list(b.stats.gated),
                "failures": list(getattr(b.stats, "failures", ()) or ()),
                "defused": int(getattr(b.stats, "defused", 0) or 0),
                "dropped": int(getattr(b.stats, "dropped", 0) or 0),
                "authored": int(getattr(b.stats, "authored", 0) or 0),
                "empty_reason": b.stats.empty_reason,
                "text": b.text if getattr(args, "verbose", False) else None,
            } for t, b in rows},
        }, indent=2, ensure_ascii=False, default=str))
        return None

    if not br_mod.enabled():
        print(f"mindnet brief: switched OFF — {br_mod.ENV_SWITCH} is set to a "
              f"false value, so no agent is being briefed at all")
    for task, built in rows:
        # Defused like every other consumer of these two. This command is the
        # operator's tool for judging whether a brief is SAFE, so it was the one
        # surface where a sidecar-supplied phase carrying newlines could forge
        # extra rows in the very listing being used to audit the feature.
        phase = br_mod.safe_phase(br_mod.phase_for(ctx, task)) or "?"
        print(f"{br_mod.safe_line(task.task_id)} [{phase}]  "
              f"{_describe(built.stats)}")
        if built.stats.empty_reason:
            print(f"    empty: {built.stats.empty_reason}")
        if getattr(args, "verbose", False) and built.text:
            for line in built.text.splitlines():
                print(f"    {line}")
    if not rows:
        print("mindnet brief: no tasks on the graph")
    return None
