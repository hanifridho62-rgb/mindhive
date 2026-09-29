# SPDX-License-Identifier: Apache-2.0
"""argparse wiring for the autonomous loop and work sessions: `loop`,
`worksession` (blueprint P1b), and the PRD assembly line `fabricate`
(blueprint P10, spec Bug 9). Later blueprint phases add `watcher` here;
`ci` registers itself.
"""
import argparse

from ... import cliparse
from ..orchestration import (cmd_fabricate_compile,
                             cmd_fabricate_dispatch,
                             cmd_fabricate_list,
                             cmd_fabricate_run,
                             cmd_loop,
                             cmd_worksession)

__all__ = ["add_fabricate", "add_loop", "add_worksession",
    "register_parsers"]



def add_loop(sub) -> None:
    """Register `hive loop`."""
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


def _non_negative_int(text: str) -> int:
    """argparse type for ``--tokens-used``: a whole number, zero or more (a
    negative count would be stored for good in the append-only ledger)."""
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be zero or more, got {n}")
    return n


def _positive_int(text: str) -> int:
    """argparse type for ``--token-budget``: above zero, so the usage tag never
    divides by a budget that is not one."""
    n = _non_negative_int(text)
    if n == 0:
        raise argparse.ArgumentTypeError("must be above zero, got 0")
    return n


def _ws_common(p) -> None:
    p.add_argument("--project", type=cliparse.project_arg, default=None,
                   help="target namespace")
    p.add_argument("--json", action="store_true", help="print JSON")


def add_worksession(sub) -> None:
    """Register `hive worksession` (blueprint P1b, BAB 5)."""
    from ... import pgshape
    from ... import worksessions as ws
    wsp = sub.add_parser("worksession", help="work-session steps tagged by phase, "
                         "initiative and context usage (BAB 5)")
    wss = wsp.add_subparsers(dest="ws_cmd", required=True)

    rec = wss.add_parser("record", help="append one step: [WS] + [INITIATIVE|USER_SPEC] "
                         "+ [USAGE] tags")
    # No `choices=` on --phase: the refusal is the document's own message
    # ("Phase tag tidak valid: ..."), printed by the handler with exit 2.
    rec.add_argument("--phase", required=True,
                     help="one of: " + ", ".join(ws.CANONICAL_PHASES))
    rec.add_argument("--action", required=True,
                     help="a catalog key (e.g. TDD_RED, FIX_PATH_TRAVERSAL) or free text")
    rec.add_argument("--initiative", action="store_true",
                     help="an autonomous step, not the user's spec; the step's "
                          "[INITIATIVE] tag is its declaration")
    rec.add_argument("--reason", default=None,
                     help="why the initiative was taken (needs --initiative)")
    rec.add_argument("--ref", default=None,
                     help="the incident or finding it answers (needs --initiative)")
    rec.add_argument("--tokens-used", dest="tokens_used", type=_non_negative_int, default=0)
    rec.add_argument("--token-budget", dest="token_budget", type=_positive_int, default=None,
                     help=f"above zero; default {ws.DEFAULT_TOKEN_BUDGET}")
    rec.add_argument("--status", choices=ws.STEP_STATUSES, default=None,
                     help="initial status (default pending)")
    rec.add_argument("--session", type=cliparse.session_id_arg, default=None)
    _ws_common(rec)
    rec.set_defaults(func=cmd_worksession)

    st = wss.add_parser("status", help="move one step's status")
    st.add_argument("--step-id", dest="step_id", required=True,
                    help="a step id, as `hive worksession list --session S` prints it")
    st.add_argument("--status", required=True, choices=ws.STEP_STATUSES)
    st.add_argument("--session", type=cliparse.session_id_arg, default=None)
    _ws_common(st)
    st.set_defaults(func=cmd_worksession)

    ini = wss.add_parser("initiative", help="declare an autonomous initiative")
    ini.add_argument("--reason", required=True)
    ini.add_argument("--ref", default=None)
    ini.add_argument("--session", type=cliparse.session_id_arg, default=None)
    _ws_common(ini)
    ini.set_defaults(func=cmd_worksession)

    ls = wss.add_parser("list", help="work sessions, newest first; --session S lists "
                        "that session's steps and their ids")
    ls.add_argument("--initiatives", action="store_true",
                    help="list initiatives instead (declarations and [INITIATIVE] steps)")
    ls.add_argument("--session", type=cliparse.session_id_arg, default=None,
                    help="list this session's steps, with the ids `status` takes")
    # Both default to None: without either flag the whole listing prints, as
    # before; with one, the page comes from `pgshape.paginate_records`, which
    # also owns the clamping (a value the helper would not take is clamped
    # there, never refused here).
    ls.add_argument("--page", type=int, default=None,
                    help="print one page of the listing, counted from 1 (below 1 "
                         "reads as 1; past the end prints no rows and names the "
                         "last page); the footer then says which page this is "
                         "and how to ask for the next")
    ls.add_argument("--page-size", dest="page_size", type=int, default=None,
                    help=f"rows per page: default {pgshape.DEFAULT_PAGE_SIZE}, at most "
                         f"{pgshape.MAX_PAGE_SIZE_CEILING} (outside that range the value "
                         "is clamped, never refused); alone, it prints page 1")
    _ws_common(ls)
    ls.set_defaults(func=cmd_worksession)

    mg = wss.add_parser("migrate", help="copy roadmap steps into plan-phase "
                        "[USER_SPEC] steps (idempotent across sessions; the roadmap "
                        "is untouched)")
    mg.add_argument("--plan", default=None, help="only this plan id")
    mg.add_argument("--session", type=cliparse.session_id_arg, default=None,
                    help="target session for a plan not copied yet (default: the "
                         "plan's own; a plan already copied keeps its session)")
    mg.add_argument("--dry-run", dest="dry_run", action="store_true")
    _ws_common(mg)
    mg.set_defaults(func=cmd_worksession)


def add_fabricate(sub) -> None:
    """Register `hive fabricate` (blueprint P10, spec Bug 9): the PRD
    assembly line, compile -> dispatch -> run. Ported from branch
    claude/prd-assembly-line-40e466 (3a45aea), where this block sat in
    `__main__.py`; every `--project`/`--session` now takes the `cliparse`
    validator the rest of the surface is wired with."""
    # --- fabricate (PRD assembly line: compile -> dispatch -> run) ---
    fb = sub.add_parser("fabricate", help="PRD assembly line: compile an "
                        "Executable PRD into a loop work order, put it on "
                        "an OS timer, and run it unattended (usage caps "
                        "sleep/wake via autoresume)")
    fbsub = fb.add_subparsers(dest="fabricate_cmd", required=True)
    fbc = fbsub.add_parser("compile", help="PRD -> goal dir + roadmap")
    fbc.add_argument("prd_file", nargs="?", default=None,
                     help="path to a PRD markdown file")
    fbc.add_argument("--from-learning", action="store_true",
                     dest="from_learning",
                     help="use the latest Self-Reliant PRD from the ledger")
    fbc.add_argument("--id", default=None,
                     help="fab id (default: fab-<title-slug>)")
    fbc.add_argument("--dir", default=None,
                     help="repo root the work runs in (default: here)")
    fbc.add_argument("--project", type=cliparse.project_arg, default=None)
    fbc.add_argument("--session", type=cliparse.session_id_arg, default=None)
    fbc.set_defaults(func=cmd_fabricate_compile)
    fbd = fbsub.add_parser("dispatch", help="arm the OS timer that starts "
                           "the run (default: ~now)")
    fbd.add_argument("fab_id")
    fbd.add_argument("--at", default=None,
                     help="UTC start time, e.g. 2026-07-18T22:00:00Z")
    fbd.add_argument("--in-hours", type=float, default=None, dest="in_hours",
                     help="start this many hours from now")
    fbd.add_argument("--project", type=cliparse.project_arg, default=None)
    fbd.add_argument("--session", type=cliparse.session_id_arg, default=None)
    fbd.set_defaults(func=cmd_fabricate_dispatch)
    fbr = fbsub.add_parser("run", help="execute one loop launch now (what "
                           "the dispatch timer runs)")
    fbr.add_argument("fab_id")
    fbr.add_argument("--force", action="store_true",
                     help="bypass the LIVE_LOCK guard (use with care)")
    fbr.add_argument("--project", type=cliparse.project_arg, default=None)
    fbr.add_argument("--session", type=cliparse.session_id_arg, default=None)
    fbr.set_defaults(func=cmd_fabricate_run)
    fbl = fbsub.add_parser("list", help="list fabrications")
    fbl.add_argument("--project", type=cliparse.project_arg, default=None)
    fbl.add_argument("--json", action="store_true")
    fbl.set_defaults(func=cmd_fabricate_list)


def register_parsers(sub) -> None:
    """`base.CommandModule` (blueprint P9b): mount this domain's commands,
    in the relative order `_build_parser` gives them (`fabricate` sits
    after `autoresume` in the live tree, so it mounts first here)."""
    add_fabricate(sub)
    add_loop(sub)
    add_worksession(sub)
