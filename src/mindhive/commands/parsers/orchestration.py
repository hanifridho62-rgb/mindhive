# SPDX-License-Identifier: Apache-2.0
"""argparse wiring for the autonomous loop and work sessions: `loop`,
`worksession` (blueprint P1b). Later blueprint phases add `watcher` here;
`ci` registers itself.
"""
import argparse

from ... import cliparse
from ..orchestration import cmd_loop, cmd_worksession

__all__ = ["add_loop", "add_worksession",
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


def register_parsers(sub) -> None:
    """`base.CommandModule` (blueprint P9b): mount this domain's commands,
    in the relative order `_build_parser` gives them."""
    add_loop(sub)
    add_worksession(sub)
