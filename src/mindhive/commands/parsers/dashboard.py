# SPDX-License-Identifier: Apache-2.0
"""argparse wiring for the dashboard side: `replenish`, `graph`, `plugins`,
`skills`, `sync`, `dashboard`, `harvest`, `autosync`.
"""
from ... import cliparse
from ..dashboard import (cmd_autosync_register,
                         cmd_autosync_run,
                         cmd_dashboard,
                         cmd_graph_build,
                         cmd_graph_export,
                         cmd_graph_related,
                         cmd_harvest,
                         cmd_replenish_explain,
                         cmd_replenish_run,
                         cmd_replenish_status,
                         cmd_sync,
                         cmd_ui)
from ..skills import (cmd_plugins_autoscan,
                      cmd_plugins_list,
                      cmd_plugins_recommend,
                      cmd_plugins_scan,
                      cmd_skills_invoke,
                      cmd_skills_list,
                      cmd_skills_recommend,
                      cmd_skills_record,
                      cmd_skills_stats,
                      cmd_skills_status,
                      cmd_skills_trail)

__all__ = ["add_replenish", "add_graph", "add_plugins", "add_skills",
    "add_sync", "add_dashboard", "add_harvest", "add_autosync", "add_ui",
    "register_parsers"]



def add_replenish(sub) -> None:
    """Register `hive replenish`."""
    # --- replenish (adaptive work injection) ---
    rp = sub.add_parser("replenish",
                        help="propose work the record proves is outstanding "
                             "(run by session_end)")
    rpsub = rp.add_subparsers(dest="replenish_cmd", required=True)
    rpr = rpsub.add_parser("run", help="run one replenishment round for a session")
    rpr.add_argument("--session", type=cliparse.session_id_arg, default=None,
                     help="session id to replenish (default: $MINDHIVE_SESSION)")
    rpr.add_argument("--dry-run", action="store_true",
                     help="decide and print without appending anything")
    rpr.set_defaults(func=cmd_replenish_run)
    rpe = rpsub.add_parser("explain",
                           help="score every lens's candidates against every "
                                "dial position, changing nothing")
    rpe.add_argument("--ambition", default=None,
                     help="report only the lenses this position runs "
                          "(conservative / balanced / ambitious)")
    rpe.add_argument("--with-agentic", action="store_true",
                     help="include the innovation lens, which spends a model "
                          "call (excluded by default even at --ambition "
                          "ambitious)")
    rpe.add_argument("--json", action="store_true",
                     help="machine-readable report")
    rpe.set_defaults(func=cmd_replenish_explain)
    rpsub.add_parser("status",
                     help="what the injector knows: rounds, dry streak, spent "
                          "fingerprints, active lenses"
                     ).set_defaults(func=cmd_replenish_status)


def add_graph(sub) -> None:
    """Register `hive graph`."""
    # --- graph build / graph related ---
    gp = sub.add_parser("graph", help="knowledge graph commands")
    gsub = gp.add_subparsers(dest="graph_cmd", required=True)
    gb = gsub.add_parser("build", help="build graph.json")
    gb.add_argument("--semantic", action="store_true")
    gb.set_defaults(func=cmd_graph_build)
    gr = gsub.add_parser("related", help="find related nodes")
    gr.add_argument("query")
    gr.add_argument("--depth", type=int, default=1)
    gr.set_defaults(func=cmd_graph_related)

    ge = gsub.add_parser("export", help="write standalone graph.html")
    ge.add_argument("--out", default=None)
    ge.add_argument("--semantic", action="store_true")
    ge.add_argument("--open", dest="open_after", action="store_true",
                    help="open the file in the default browser")
    ge.set_defaults(func=cmd_graph_export)


def add_plugins(sub) -> None:
    """Register `hive plugins`."""
    # --- plugins (scan free skills/plugins, score, recommend) ---
    pl = sub.add_parser("plugins", help="catalog & recommend free skills/plugins")
    plsub = pl.add_subparsers(dest="plugins_cmd", required=True)
    pll = plsub.add_parser("list", help="print the stored catalog: every plugin, free and paid")
    pll.add_argument("--json", action="store_true", help="emit the raw catalog snapshot JSON")
    pll.set_defaults(func=cmd_plugins_list)
    pls = plsub.add_parser("scan", help="scan cache, score, store a catalog snapshot")
    pls.add_argument("--cache", default=None, help="plugin cache dir (default: ~/.claude/plugins/cache)")
    pls.add_argument("--top", type=int, default=5, help="how many to recommend")
    pls.set_defaults(func=cmd_plugins_scan)
    pla = plsub.add_parser("autoscan",
                           help="scan only if the catalog is missing or stale (cheap; safe every session)")
    pla.add_argument("--cache", default=None, help="plugin cache dir (default: ~/.claude/plugins/cache)")
    pla.add_argument("--top", type=int, default=5, help="how many to recommend")
    pla.add_argument("--max-age-hours", type=float, default=24.0, dest="max_age_hours",
                     help="rescan when the latest snapshot is older than this (default: 24)")
    pla.add_argument("--force", action="store_true", help="rescan even if the catalog is fresh")
    pla.set_defaults(func=cmd_plugins_autoscan)
    plsub.add_parser("recommend", help="print the latest catalog's top recommendations").set_defaults(func=cmd_plugins_recommend)


def add_skills(sub) -> None:
    """Register `hive skills`."""
    # --- skills: the invoke channel /mindhive-orchestrator and /circle drive ---
    sk = sub.add_parser("skills", help="list, invoke, or check status of installed skills")
    sksub = sk.add_subparsers(dest="skills_cmd", required=True)
    skl = sksub.add_parser("list", help="list installed plugins + skills (live)")
    skl.add_argument("--json", action="store_true", help="emit the raw inventory JSON")
    skl.set_defaults(func=cmd_skills_list)
    ski = sksub.add_parser("invoke", help="call a skill via headless claude and record the outcome")
    ski.add_argument("name", help="skill name (as shown by `skills list`)")
    ski.add_argument("--plugin", default=None, help="owning plugin, when disambiguation is needed")
    ski.add_argument("--project", type=cliparse.project_arg, default=None, help="target namespace")
    ski.set_defaults(func=cmd_skills_invoke)
    sksub.add_parser("status", help="green/red last-call outcome per skill").set_defaults(func=cmd_skills_status)
    sks = sksub.add_parser("stats", help="per-skill completion-rate telemetry from the ledger")
    sks.add_argument("--json", action="store_true", help="emit the raw stats JSON")
    sks.add_argument("--project", type=cliparse.project_arg, default=None, help="target namespace")
    sks.set_defaults(func=cmd_skills_stats)
    skr = sksub.add_parser("record",
                           help="record a skill outcome (agent-assessed write-through)")
    skr.add_argument("name", help="skill name")
    outcome = skr.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--ok", action="store_true", help="the skill completed")
    outcome.add_argument("--fail", action="store_true", help="the skill failed")
    skr.add_argument("--note", default="", help="one-line context for the outcome")
    skr.add_argument("--plugin", default=None, help="owning plugin, if known")
    skr.add_argument("--project", type=cliparse.project_arg, default=None, help="target namespace")
    skr.set_defaults(func=cmd_skills_record)
    skc = sksub.add_parser("recommend",
                           help="deterministic most-needed-skills ranking from measured data")
    skc.add_argument("--json", action="store_true", help="emit the full ranked payload")
    skc.add_argument("--top", type=int, default=None, help="limit to the top N entries")
    skc.add_argument("--brief", action="store_true",
                     help="<= 3 one-line suggestions (fast; for the session hook)")
    skc.add_argument("--project", type=cliparse.project_arg, default=None, help="target namespace")
    skc.set_defaults(func=cmd_skills_recommend)
    skt = sksub.add_parser("trail",
                           help="per-session skill trail (which skills, when, why) "
                                "from the ledger — survives the live Oversee board")
    skt.add_argument("--session", type=cliparse.session_id_arg, default=None, help="one session id (default: all)")
    skt.add_argument("--all", action="store_true",
                     help="merge every namespace (HIVE MIND), not just this project")
    skt.add_argument("--json", action="store_true", help="emit the raw trail JSON")
    skt.add_argument("--project", type=cliparse.project_arg, default=None, help="target namespace")
    skt.set_defaults(func=cmd_skills_trail)


def add_sync(sub) -> None:
    """Register `hive sync`."""
    # --- sync ---
    sp = sub.add_parser("sync", help="write native MEMORY.md for desktop auto-load")
    sp.add_argument("--folder", default=None,
                    help="project folder to encode (default: cwd)")
    sp.add_argument("--no-briefing", action="store_true",
                    help="omit BRIEFING.md from native MEMORY.md")
    sp.set_defaults(func=cmd_sync)


def add_dashboard(sub) -> None:
    """Register `hive dashboard`."""
    # --- dashboard (one-window control panel; powers MindHive.exe) ---
    dp = sub.add_parser("dashboard", help="open the web control panel for all features")
    dp.add_argument("--port", type=int, default=None,
                    help="port (default: fixed 54461 for a stable URL; pass 0 for an ephemeral port)")
    dp.add_argument("--no-browser", dest="no_browser", action="store_true",
                    help="don't auto-open the browser")
    # blueprint P1k. Default None, not 60: ABSENT is what keeps the 24/7
    # panel's `serve` call exactly what it was. `hive ui` is this flag with a
    # number already in it.
    dp.add_argument("--idle-cutoff", dest="idle_cutoff", type=float,
                    default=None,
                    help="also shut down after this many seconds with no "
                         "open page (default: never)")
    dp.set_defaults(func=cmd_dashboard)


def add_ui(sub) -> None:
    """Register `hive ui`."""
    # --- ui (blueprint P1k) --- the dashboard as a page rather than a service.
    up = sub.add_parser("ui",
                        help="open the control panel and let it shut itself "
                             "down once you close the tab")
    up.add_argument("--port", type=int, default=None,
                    help="port (default: 54461, the dashboard's own — if the "
                         "24/7 panel is already there, this just opens it)")
    up.add_argument("--no-browser", dest="no_browser", action="store_true",
                    help="don't auto-open the browser")
    up.add_argument("--idle-cutoff", dest="idle_cutoff", type=float,
                    default=60.0,
                    help="shut down after this many seconds with no page "
                         "beating (default: 60)")
    up.add_argument("--asgi", action="store_true",
                    help="serve through FastAPI/uvicorn instead of the stdlib "
                         "server (needs `pip install -e .[ui]`)")
    up.set_defaults(func=cmd_ui)


def add_harvest(sub) -> None:
    """Register `hive harvest`."""
    # --- harvest (auto-capture from Claude Code transcripts; no hooks needed) ---
    hv = sub.add_parser("harvest", help="capture events from Claude Code transcripts")
    hv.add_argument("--transcripts", default=None,
                    help="transcript DIRECTORY (omit with --catch-up)")
    hv.add_argument("--project", type=cliparse.project_arg, default=None,
                    help="target namespace (omit with --catch-up — it auto-detects per session)")
    hv.add_argument("--catch-up", dest="catch_up", action="store_true",
                    help="scan ALL Claude transcript dirs and backfill sessions the hooks missed")
    hv.add_argument("--include-captured", dest="include_captured", action="store_true",
                    help="also (re)harvest sessions the live hooks already recorded (risks double-counting)")
    hv.add_argument("--all-projects", dest="all_projects", action="store_true",
                    help="with --catch-up, also harvest into namespaces that don't exist yet (default: only known projects)")
    hv.set_defaults(func=cmd_harvest)


def add_autosync(sub) -> None:
    """Register `hive autosync`."""
    # --- autosync (registry-driven: harvest + project + briefing + graph + sync) ---
    asp = sub.add_parser("autosync", help="automated sync for registered projects")
    asp.set_defaults(func=cmd_autosync_run)   # `autosync` with no subcommand -> run_all
    asub = asp.add_subparsers(dest="autosync_cmd", required=False)
    areg = asub.add_parser("register", help="register a project for autosync")
    areg.add_argument("--project", type=cliparse.project_arg, required=True)
    areg.add_argument("--transcripts", required=True)
    areg.add_argument("--folder", required=True)
    areg.set_defaults(func=cmd_autosync_register)


def register_parsers(sub) -> None:
    """`base.CommandModule` (blueprint P9b): mount this domain's commands,
    in the relative order `_build_parser` gives them."""
    add_replenish(sub)
    add_graph(sub)
    add_plugins(sub)
    add_skills(sub)
    add_sync(sub)
    add_dashboard(sub)
    add_harvest(sub)
    add_autosync(sub)
    add_ui(sub)                              # blueprint P1k — at the tail,
    # which is where `_build_parser` mounts it too: the order pin compares
    # this list against the live tree's, not against this file's reading order.
