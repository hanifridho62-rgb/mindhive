# SPDX-License-Identifier: Apache-2.0
"""argparse wiring for store hygiene and verification: `verify`,
`selftest`, `premise`, `recall`, `consolidate`, `gc`, `export`, `embed`,
`briefing`, `review`, `log`, `flag`, `failures`, `enrich`.
"""
from ... import cliparse
from ..context_ops import cmd_briefing, cmd_premise_check
from ..ledger import cmd_log_add, cmd_log_pending, cmd_log_queue, cmd_log_show
from ..maintenance import (cmd_consolidate,
                           cmd_embed,
                           cmd_enrich,
                           cmd_export,
                           cmd_failures_capture,
                           cmd_failures_list,
                           cmd_flag,
                           cmd_gc,
                           cmd_recall,
                           cmd_review_clear,
                           cmd_review_queue,
                           cmd_review_show)
from ..verification import cmd_selftest, cmd_verify

__all__ = ["add_verify", "add_selftest", "add_premise", "add_recall",
    "add_consolidate", "add_gc", "add_export", "add_embed", "add_briefing",
    "add_review", "add_log", "add_flag", "add_failures", "add_enrich",
    "register_parsers"]



def add_verify(sub) -> None:
    """Register `hive verify` (`--json`, `--strict`: blueprint P9b)."""
    p = sub.add_parser("verify", help="verify hash chain")
    p.add_argument("--json", action="store_true",
                   help="sorted one-line JSON (keys sorted, no spaces), for scripts and diffs")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 when the chain is not ok or a corrupt row exists "
                        "(the default reports on stdout and always exits 0)")
    p.set_defaults(func=cmd_verify)


def add_selftest(sub) -> None:
    """Register `hive selftest`."""
    sub.add_parser("selftest", help="run test suite").set_defaults(func=cmd_selftest)


def add_premise(sub) -> None:
    """Register `hive premise`."""
    # §0's operator-facing half. Deliberately NOT folded into `hive verify`,
    # which already means "verify the hash chain" — one command answering two
    # unrelated questions is how an operator ends up trusting the wrong answer.
    pre = sub.add_parser(
        "premise",
        help="check a prompt or handoff doc's factual claims against the record")
    pres = pre.add_subparsers(dest="premise_cmd", required=True)
    prec = pres.add_parser(
        "check",
        help="report asserted paths, roadmap ids and test counts the record "
             "contradicts (exits non-zero on any contradiction)")
    prec.add_argument("--file", default=None,
                      help="read the text from PATH (default: stdin)")
    prec.add_argument("--project", type=cliparse.project_arg, default=None,
                      help="target namespace explicitly (default: detect from cwd)")
    prec.set_defaults(func=cmd_premise_check)


def add_recall(sub) -> None:
    """Register `hive recall`."""
    r = sub.add_parser("recall", help="search ledger")
    r.add_argument("query")
    r.add_argument("--project", type=cliparse.project_arg, default=None,
                   help="target namespace explicitly (default: detect from cwd)")
    r.add_argument("--semantic", action="store_true")
    r.add_argument("--limit", type=int, default=10)
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_recall)


def add_consolidate(sub) -> None:
    """Register `hive consolidate`."""
    cons = sub.add_parser("consolidate",
                          help="dreaming pass: merge near-duplicate memories "
                               "into survivors (append-only, bounded)")
    cons.add_argument("--project", type=cliparse.project_arg, default=None)
    cons.add_argument("--limit", type=int, default=3,
                      help="max duplicate clusters merged per pass")
    cons.add_argument("--cooldown-hours", dest="cooldown_hours", type=float,
                      default=6.0, help="skip when a pass ran within this window "
                                        "(0 disables)")
    cons.add_argument("--force", action="store_true",
                      help="override the LIVE_LOCK guard")
    cons.set_defaults(func=cmd_consolidate)


def add_gc(sub) -> None:
    """Register `hive gc`."""
    gc = sub.add_parser("gc", help="store hygiene: report sizes; prune junk (opt-in)")
    gc.add_argument("target", nargs="?", default=None,
                    help="namespace to prune (requires --prune)")
    gc.add_argument("--prune", action="store_true",
                    help="delete the target (default: report only)")
    gc.add_argument("--orphans", action="store_true",
                    help="with --prune: remove all unregistered namespaces")
    gc.add_argument("--graph-blobs", dest="graph_blobs", action="store_true",
                    help="with --prune: delete graph.json/graph.html blobs only")
    gc.add_argument("--force", action="store_true",
                    help="override the registered-namespace / LIVE_LOCK guard")
    gc.set_defaults(func=cmd_gc)


def add_export(sub) -> None:
    """Register `hive export`."""
    e = sub.add_parser("export", help="write handoff.md")
    e.add_argument("--out", default=None)
    e.add_argument("--recent", type=int, default=20)
    e.set_defaults(func=cmd_export)


def add_embed(sub) -> None:
    """Register `hive embed`."""
    em = sub.add_parser("embed", help="build vector index (on-demand)")
    em.add_argument("--force", action="store_true")
    em.set_defaults(func=cmd_embed)


def add_briefing(sub) -> None:
    """Register `hive briefing`."""
    # --- briefing ---
    sub.add_parser("briefing", help="build and print BRIEFING.md").set_defaults(func=cmd_briefing)


def add_review(sub) -> None:
    """Register `hive review`."""
    # --- review (thermo-nuclear review queue) ---
    rv = sub.add_parser("review", help="thermo-nuclear review queue")
    rvsub = rv.add_subparsers(dest="review_cmd", required=True)
    rvq = rvsub.add_parser("queue", help="queue this session's touched source files for review")
    rvq.add_argument("--session", type=cliparse.session_id_arg, default=None,
                     help="session id to queue (default: $MINDHIVE_SESSION)")
    rvq.set_defaults(func=cmd_review_queue)
    rvsub.add_parser("clear", help="clear the pending-review marker (after the review runs)").set_defaults(func=cmd_review_clear)
    rvsub.add_parser("show", help="print the pending-review marker, if any").set_defaults(func=cmd_review_show)


def add_log(sub) -> None:
    """Register `hive log`."""
    # --- log (mandatory code change-log) ---
    from ...changelog import CATEGORIES as _LOG_CATS
    lg = sub.add_parser("log", help="mandatory code change-log (code_change events)")
    lgsub = lg.add_subparsers(dest="log_cmd", required=True)
    for _cat in _LOG_CATS:
        lc = lgsub.add_parser(_cat, help=f"record a {_cat} entry for the current change-set")
        lc.add_argument("explanation", help="brief explanation of what changed (mandatory)")
        lc.add_argument("--files", nargs="*", default=None,
                        help="affected files (default: pending marker, then session, then git)")
        lc.add_argument("--session", type=cliparse.session_id_arg, default=None,
                        help="session id (default: $MINDHIVE_SESSION)")
        # The MindNetwork join key (KTD16). `execution` is the one phase whose
        # evidence is an EVENT rather than a file, and it looks for a
        # code_change carrying this task_id — so without a way to stamp it from
        # the surface that writes every change-log entry, the contract accepts
        # a field nothing produces. Omitted entirely when absent, so an
        # untracked entry stays byte-identical to every historical one.
        lc.add_argument("--task", default=None, metavar="TASK_ID",
                        help="mindnet task this change-set belongs to; stamps "
                             "the join key its `execution` phase looks for")
        lc.set_defaults(func=cmd_log_add, category=_cat)
    lgq = lgsub.add_parser("queue", help="queue this session's unlogged source files (run by session_end)")
    lgq.add_argument("--session", type=cliparse.session_id_arg, default=None,
                     help="session id to queue (default: $MINDHIVE_SESSION)")
    lgq.set_defaults(func=cmd_log_queue)
    lgsub.add_parser("show", help="print change-log entries, newest first").set_defaults(func=cmd_log_show)
    lgsub.add_parser("pending", help="print the pending unlogged-changes marker, if any").set_defaults(func=cmd_log_pending)


def add_flag(sub) -> None:
    """Register `hive flag`."""
    # --- flag (Learning Loop judgment-error capture, KTD4) ---
    fg = sub.add_parser("flag", help="self-flag a judgment error (active at 1 occurrence)")
    fg.add_argument("text", help="one line: what was misjudged")
    fg.add_argument("--category", default=None,
                    help="optional category tag (e.g. git, scope, permissions)")
    fg.add_argument("--project", type=cliparse.project_arg, default=None, help="target namespace")
    fg.set_defaults(func=cmd_flag)


def add_failures(sub) -> None:
    """Register `hive failures`."""
    # --- failures (Learning Loop capture stage) ---
    fp = sub.add_parser("failures", help="capture tool failures + list incidents")
    fsub = fp.add_subparsers(dest="failures_cmd", required=True)
    fc = fsub.add_parser("capture", help="scan a transcript dir, store tool_failed events")
    fc.add_argument("--transcripts", required=True, help="transcript DIRECTORY")
    fc.add_argument("--project", type=cliparse.project_arg, default=None, help="target namespace")
    fc.set_defaults(func=cmd_failures_capture)
    fl = fsub.add_parser("list", help="list incident signatures by occurrence")
    fl.add_argument("--project", type=cliparse.project_arg, default=None)
    fl.set_defaults(func=cmd_failures_list)


def add_enrich(sub) -> None:
    """Register `hive enrich`."""
    # --- enrich (Oversee prompt enrichment: default-on toggle + preview) ---
    en = sub.add_parser("enrich",
                        help="prompt enrichment (Oversee): default-on toggle "
                             "and deterministic prompt preview")
    ensub = en.add_subparsers(dest="enrich_cmd", required=True)
    for _name, _help in (("status", "show whether prompt enrichment is on"),
                         ("on", "enable prompt enrichment"),
                         ("off", "disable prompt enrichment")):
        sp = ensub.add_parser(_name, help=_help)
        sp.add_argument("--project", type=cliparse.project_arg, default=None)
        sp.set_defaults(func=cmd_enrich)
    env = ensub.add_parser("preview",
                           help="enrich one draft prompt: questions, source "
                                "candidates, skills, SOP check, composed text")
    env.add_argument("prompt")
    env.add_argument("--project", type=cliparse.project_arg, default=None)
    env.add_argument("--json", action="store_true",
                     help="emit the full enrichment result as JSON")
    env.set_defaults(func=cmd_enrich)


def register_parsers(sub) -> None:
    """`base.CommandModule` (blueprint P9b): mount this domain's commands,
    in the relative order `_build_parser` gives them."""
    add_verify(sub)
    add_selftest(sub)
    add_premise(sub)
    add_recall(sub)
    add_consolidate(sub)
    add_gc(sub)
    add_export(sub)
    add_embed(sub)
    add_briefing(sub)
    add_review(sub)
    add_log(sub)
    add_flag(sub)
    add_failures(sub)
    add_enrich(sub)
