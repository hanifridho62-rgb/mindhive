# SPDX-License-Identifier: Apache-2.0
"""argparse wiring for the ledger and context surfaces: `append`,
`symbols`, `hook`, `session`, `project`, `state`, `projects`,
`directives`, `context`, `repo`, `packs`. One `add_<command>(sub)` per
top-level command; `mindhive.commands.parsers._build_parser` calls them
in the surface's historical order.
"""
from ... import cliparse
from ..context_ops import (_repo_default_max_age,
                           cmd_context,
                           cmd_directives,
                           cmd_packs_check,
                           cmd_packs_report,
                           cmd_repo_stale,
                           cmd_state,
                           cmd_symbols)
from ..ledger import (cmd_append,
                      cmd_hook,
                      cmd_project,
                      cmd_projects,
                      cmd_session_install_hook,
                      cmd_session_run,
                      rotation_supervisor_defaults)

__all__ = ["add_append", "add_symbols", "add_hook", "add_session",
    "add_project", "add_state", "add_projects", "add_directives",
    "add_context", "add_repo", "add_packs",
    "register_parsers"]


def _adaptive_tail_spec(text):
    """argparse type for --set-adaptive-tail: the parsed settings, or a usage
    error naming the bad part. Lazy: only this flag needs `adaptivetail`."""
    import argparse
    from ...adaptivetail import parse_adaptive_tail_spec
    try:
        return parse_adaptive_tail_spec(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None



def add_append(sub) -> None:
    """Register `hive append`."""
    a = sub.add_parser("append", help="append an event")
    a.add_argument("type")
    a.add_argument("payload", nargs="?", default=None)
    a.add_argument("--tags", default="")
    a.add_argument("--session", type=cliparse.session_id_arg, default=None)
    a.add_argument("--json", action="store_true", help="parse payload as JSON dict")
    a.add_argument("--stdin", action="store_true", help="read payload from stdin")
    a.set_defaults(func=cmd_append)


def add_symbols(sub) -> None:
    """Register `hive symbols`."""
    sy = sub.add_parser("symbols",
                        help="zero-token symbol index of a source tree")
    sy.add_argument("path", nargs="?", default=None,
                    help="tree to map (default: the current directory)")
    sy.add_argument("--budget", type=int, default=None, help="token budget")
    sy.add_argument("--json", action="store_true")
    sy.set_defaults(func=cmd_symbols)


def add_hook(sub) -> None:
    """Register `hive hook`."""
    hk = sub.add_parser("hook", help="handle a live Claude Code hook payload (reads stdin)")
    hk.add_argument("stage",
                    choices=["post_tool_use", "post_tool_use_failure",
                             "pre_tool_use", "user_prompt_submit", "stop"],
                    help="which hook fired")
    hk.set_defaults(func=cmd_hook)


def add_session(sub) -> None:
    """Register `hive session`."""
    # Smart-zone rotation for INTERACTIVE sessions. `hive loop` already does this
    # headlessly; nothing inside an interactive session can clear its own context,
    # so the loop has to live outside it — here.
    se = sub.add_parser("session", help="supervise an interactive Claude session "
                                        "and rotate it when context fills")
    se_sub = se.add_subparsers(dest="session_cmd", required=True)

    se_run = se_sub.add_parser("run", help="run an interactive session under "
                                           "smart-zone rotation")
    se_run.add_argument("--project", type=cliparse.project_arg, default=None,
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
                             "MINDHIVE_ROTATION is set; the shim registered "
                             "matches the platform - stop.ps1 or stop.sh)")
    se_install.add_argument("settings", help="path to the settings.json to edit")
    se_install.add_argument("--command", default=None,
                            help="the hook command to register (default: this "
                                 "checkout's platform-matched stop shim). Worth "
                                 "setting from a worktree, where the default "
                                 "resolves to the worktree's own copy)")
    se_install.add_argument("--dry-run", action="store_true",
                            help="report what would change without writing")
    se_install.set_defaults(func=cmd_session_install_hook)


def add_project(sub) -> None:
    """Register `hive project`."""
    sub.add_parser("project", help="rebuild projections").set_defaults(func=cmd_project)


def add_state(sub) -> None:
    """Register `hive state`."""
    st = sub.add_parser("state", help="print STATE.md")
    st.add_argument("--project", type=cliparse.project_arg, default=None,
                    help="target namespace explicitly (default: detect from cwd)")
    st.add_argument("--brief", action="store_true",
                    help="print the brief the session-start hook injects "
                         "(STATE_BRIEF.md, under its share of the 6,000-character "
                         "session-start cap) instead of the whole STATE.md")
    st.set_defaults(func=cmd_state)


def add_projects(sub) -> None:
    """Register `hive projects`."""
    sub.add_parser("projects", help="list namespaces (registered vs unregistered)").set_defaults(func=cmd_projects)


def add_directives(sub) -> None:
    """Register `hive directives`."""
    di = sub.add_parser("directives", help="print the session operating directives")
    di.add_argument("--brief", action="store_true",
                    help="print the brief form the session-start hook injects")
    di.set_defaults(func=cmd_directives)


def add_context(sub) -> None:
    """Register `hive context`."""
    cx = sub.add_parser("context",
                        help="measure the auto-loaded context budget (read-only)")
    cx.add_argument("--project", type=cliparse.project_arg, default=None,
                    help="target namespace explicitly (default: detect from cwd)")
    cx.add_argument("--json", action="store_true", help="machine-readable report")
    cx.add_argument("--budget", type=int, default=None,
                    help="compare against a one-off token budget "
                         "(overrides the persisted one)")
    cx.add_argument("--set-budget", type=int, default=None, dest="set_budget",
                    help="persist the namespace's token budget and exit")
    cx.add_argument("--set-memory-lines", type=int, default=None,
                    dest="set_memory_lines",
                    help="persist this namespace's MEMORY.md line budget "
                         "(default: the native loader's own limit). Raising it "
                         "is a decision that the loader will read only part of "
                         "the file")
    cx.add_argument("--set-memory-bytes", type=int, default=None,
                    dest="set_memory_bytes",
                    help="persist this namespace's MEMORY.md byte budget "
                         "(default: the native loader's own limit, measured on "
                         "LF-normalised content — not the on-disk size). "
                         "Raising it is a decision that the loader will read "
                         "only part of the file")
    cx.add_argument("--set-adaptive-tail", type=_adaptive_tail_spec, default=None,
                    dest="set_adaptive_tail", metavar="SPEC",
                    help="persist this namespace's adaptive MEMORY.md tail as "
                         "lessons=N,decisions=N,enabled=0|1 (any subset). "
                         "Enabled, `hive sync` fills lessons and decisions newest "
                         "first inside the composer's safe budget (24,500 bytes / "
                         "195 lines, or the same margin under a lower "
                         "--set-memory-lines/--set-memory-bytes cut), never cuts a "
                         "pinned lesson, and counts what "
                         "it leaves out in a rollover notice pointing at "
                         "`hive recall --semantic`")
    cx.add_argument("--check", action="store_true",
                    help="exit 1 when the measured total exceeds the budget, or "
                         "when MEMORY.md is past its line OR byte budget with no "
                         "rollover notice before the cut (the loader then drops "
                         "the digest's tail sections); a managed truncation, or a "
                         "digest past the composer's safe budget, only warns")
    cx.add_argument("--top", type=int, default=None,
                    help="show only the N heaviest contributors")
    cx.add_argument("--origin", choices=["hive", "claude", "hook", "all"],
                    default="all",
                    help="limit the report to one origin (default: all)")
    cx.set_defaults(func=cmd_context)


def add_repo(sub) -> None:
    """Register `hive repo`."""
    # --- repo (what git can prove about this checkout) ---
    rp = sub.add_parser("repo", help="what git can prove about this checkout "
                                     "(read-only, never fetches)")
    rpsub = rp.add_subparsers(dest="repo_cmd", required=True)
    rps = rpsub.add_parser("stale", help="how far this checkout has drifted from "
                                         "its integration ref, and how old the "
                                         "comparison is")
    rps.add_argument("--max-age-hours", type=float,
                     default=None, dest="max_age_hours",
                     help="how old the last fetch may be before the comparison "
                          "is flagged as untrustworthy (default: "
                          f"{_repo_default_max_age():g})")
    rps.add_argument("--json", action="store_true")
    rps.set_defaults(func=cmd_repo_stale)


def add_packs(sub) -> None:
    """Register `hive packs`."""
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


def register_parsers(sub) -> None:
    """`base.CommandModule` (blueprint P9b): mount this domain's commands,
    in the relative order `_build_parser` gives them."""
    add_append(sub)
    add_symbols(sub)
    add_hook(sub)
    add_session(sub)
    add_project(sub)
    add_state(sub)
    add_projects(sub)
    add_directives(sub)
    add_context(sub)
    add_repo(sub)
    add_packs(sub)
