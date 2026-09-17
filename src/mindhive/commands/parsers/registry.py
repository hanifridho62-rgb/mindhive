# SPDX-License-Identifier: Apache-2.0
"""argparse wiring for the registries: `sop`, `roadmap`, `autoresume`,
`rules`, `triggers`.
"""
from ... import cliparse
from ..registry_roadmap import (cmd_autoresume_cancel,
                                cmd_autoresume_fire,
                                cmd_autoresume_list,
                                cmd_autoresume_schedule,
                                cmd_autoresume_wait,
                                cmd_roadmap_archive,
                                cmd_roadmap_autoclose,
                                cmd_roadmap_complete,
                                cmd_roadmap_interrupt,
                                cmd_roadmap_link,
                                cmd_roadmap_list,
                                cmd_roadmap_merge_state,
                                cmd_roadmap_resume,
                                cmd_roadmap_retire,
                                cmd_roadmap_set,
                                cmd_roadmap_show,
                                cmd_roadmap_step)
from ..registry_rules import (cmd_rules_audit,
                              cmd_rules_demote,
                              cmd_rules_enforce,
                              cmd_rules_inject,
                              cmd_rules_list,
                              cmd_rules_promote,
                              cmd_rules_reconcile,
                              cmd_rules_reflect,
                              cmd_rules_resolve,
                              cmd_rules_show,
                              cmd_rules_summaries,
                              cmd_triggers_check,
                              cmd_triggers_list,
                              cmd_triggers_train)
from ..registry_sop import (cmd_sop_add,
                            cmd_sop_delete,
                            cmd_sop_injection,
                            cmd_sop_list,
                            cmd_sop_preflight,
                            cmd_sop_show,
                            cmd_sop_update)

__all__ = ["add_sop", "add_roadmap", "add_autoresume", "add_rules",
    "add_triggers"]



def add_sop(sub) -> None:
    """Register `hive sop`."""
    # --- sop (managed standard operating procedures) ---
    so = sub.add_parser("sop", help="standard operating procedures "
                                    "(event-sourced registry, auto-loaded)")
    sosub = so.add_subparsers(dest="sop_cmd", required=True)
    soa = sosub.add_parser("add", help="add a new SOP")
    soa.add_argument("sop_id", help="stable kebab-case id, e.g. memo-hygiene")
    soa.add_argument("--title", required=True)
    soa.add_argument("--text", default=None, help="procedure body (inline)")
    soa.add_argument("--file", default=None, help="read the body from a file")
    soa.add_argument("--stdin", action="store_true",
                     help="read the body from stdin")
    soa.add_argument("--tags", default="")
    soa.add_argument("--session", type=cliparse.session_id_arg, default=None)
    # Tri-state on purpose (None = leave the tier alone). A plain store_true
    # would default to False, and every title-only `sop update` would then
    # silently demote a mandatory procedure to an ordinary one.
    soa.add_argument("--mandatory", dest="mandatory", action="store_true",
                     default=None,
                     help="holds on EVERY task: rendered first and un-elided "
                          "in STATE.md and re-asserted on every prompt")
    soa.add_argument("--no-mandatory", dest="mandatory", action="store_false",
                     help="ordinary, scope-matched SOP (the default)")
    soa.set_defaults(func=cmd_sop_add)
    sou = sosub.add_parser("update", help="revise an SOP's title and/or body")
    sou.add_argument("sop_id")
    sou.add_argument("--title", default=None)
    sou.add_argument("--text", default=None)
    sou.add_argument("--file", default=None)
    sou.add_argument("--stdin", action="store_true")
    sou.add_argument("--tags", default="")
    sou.add_argument("--session", type=cliparse.session_id_arg, default=None)
    sou.add_argument("--mandatory", dest="mandatory", action="store_true",
                     default=None, help="promote to the mandatory tier")
    sou.add_argument("--no-mandatory", dest="mandatory", action="store_false",
                     help="demote to an ordinary, scope-matched SOP")
    sou.set_defaults(func=cmd_sop_update)
    sod = sosub.add_parser("delete", help="retire an SOP (append-only tombstone)")
    sod.add_argument("sop_id")
    sod.add_argument("--reason", default=None)
    sod.add_argument("--session", type=cliparse.session_id_arg, default=None)
    sod.set_defaults(func=cmd_sop_delete)
    sol = sosub.add_parser("list", help="list active SOPs")
    sol.add_argument("--project", type=cliparse.project_arg, default=None)
    sol.add_argument("--json", action="store_true")
    sol.set_defaults(func=cmd_sop_list)
    sos = sosub.add_parser("show", help="print one SOP in full, with history")
    sos.add_argument("sop_id")
    sos.add_argument("--project", type=cliparse.project_arg, default=None)
    sos.add_argument("--json", action="store_true")
    sos.set_defaults(func=cmd_sop_show)
    sop_pf = sosub.add_parser(
        "preflight",
        help="pre-execution report: is this session NEW or a CONTINUATION "
             "(with the ledger evidence), which SOPs are mandatory, what the "
             "improvements record already warns about, and what is in the "
             "catalog")
    sop_pf.add_argument("--project", type=cliparse.project_arg, default=None)
    sop_pf.add_argument("--json", action="store_true")
    sop_pf.set_defaults(func=cmd_sop_preflight)
    sop_inj = sosub.add_parser(
        "injection",
        help="what the per-prompt mandatory-SOP injection actually delivers "
             "right now — bodies verbatim, or degraded to pointers")
    sop_inj.add_argument("--project", type=cliparse.project_arg, default=None)
    sop_inj.add_argument("--json", action="store_true")
    sop_inj.set_defaults(func=cmd_sop_injection)


def add_roadmap(sub) -> None:
    """Register `hive roadmap`."""
    # --- roadmap (event-sourced plan registry + interruption markers) ---
    from ...roadmap import STATUSES as _RM_STATUSES
    rm = sub.add_parser("roadmap", help="roadmaps: the ask, the plan, and "
                        "per-step progress (interrupted plans surface in "
                        "STATE.md and Oversee)")
    rmsub = rm.add_subparsers(dest="roadmap_cmd", required=True)

    def _add_work_link_flags(parser) -> None:
        """The durable work link, on every subcommand that can learn one.

        Four subcommands take the same trio, and hand-copying three
        `add_argument` calls four times is exactly how flag names and help text
        drift apart. All three are optional and independent because they become
        knowable at different moments — the branch when work starts, the commits
        as it lands, the PR number only once one is opened."""
        parser.add_argument("--branch", default=None,
                            help="branch this plan's work lives on")
        parser.add_argument("--pr", type=int, default=None,
                            help="pull-request number for this plan's work")
        parser.add_argument("--commit", action="append", default=None,
                            help="commit sha the work landed as (repeatable)")

    rms = rmsub.add_parser("set", help="declare (or replace) a plan")
    rms.add_argument("plan_id", help="stable kebab-case id, e.g. a run id")
    rms.add_argument("--prompt", required=True,
                     help="the original ask this plan was built from")
    rms.add_argument("--title", default=None)
    rms.add_argument("--step", action="append", default=None,
                     help="one plan step (repeatable, in order)")
    rms.add_argument("--file", default=None,
                     help="read steps from a file, one per line")
    rms.add_argument("--session", type=cliparse.session_id_arg, default=None)
    _add_work_link_flags(rms)
    rms.set_defaults(func=cmd_roadmap_set)
    rmp = rmsub.add_parser("step", help="update one step's status")
    rmp.add_argument("plan_id")
    rmp.add_argument("index", type=int, help="0-based step index")
    rmp.add_argument("--status", required=True, choices=_RM_STATUSES)
    rmp.add_argument("--note", default=None)
    rmp.add_argument("--session", type=cliparse.session_id_arg, default=None)
    rmp.set_defaults(func=cmd_roadmap_step)
    rmi = rmsub.add_parser("interrupt", help="mark the plan interrupted "
                           "(records the out-of-usage marker; usage-shaped "
                           "causes auto-schedule the delivery back into "
                           "Claude)")
    rmi.add_argument("plan_id")
    rmi.add_argument("--cause", required=True,
                     help="why work stopped, e.g. usage_limit")
    rmi.add_argument("--note", default=None)
    rmi.add_argument("--session", type=cliparse.session_id_arg, default=None)
    rmi.add_argument("--no-auto-resume", action="store_true",
                     dest="no_auto_resume",
                     help="record the marker only; do not schedule the "
                     "post-cooldown delivery")
    rmi.set_defaults(func=cmd_roadmap_interrupt)
    rmr = rmsub.add_parser("resume", help="pick an interrupted plan back up")
    rmr.add_argument("plan_id")
    rmr.add_argument("--session", type=cliparse.session_id_arg, default=None)
    rmr.set_defaults(func=cmd_roadmap_resume)
    rmc = rmsub.add_parser("complete", help="mark the plan finished")
    rmc.add_argument("plan_id")
    rmc.add_argument("--session", type=cliparse.session_id_arg, default=None)
    _add_work_link_flags(rmc)
    rmc.set_defaults(func=cmd_roadmap_complete)
    rmar = rmsub.add_parser(
        "archive", help="retire a plan whose last step nothing can verify, on "
                        "a stated reason — the honest exit between 'finished' "
                        "and 'still open'")
    rmar.add_argument("plan_id")
    rmar.add_argument("--reason", required=True,
                      help="what settled the unverifiable step, e.g. "
                           "'PR #103 merged' or 'superseded by the jobs plan'")
    rmar.add_argument("--session", type=cliparse.session_id_arg, default=None)
    _add_work_link_flags(rmar)
    rmar.set_defaults(func=cmd_roadmap_archive)
    rmk = rmsub.add_parser(
        "link", help="point a plan at the code it produced (branch, PR, "
                     "commits) without changing its status")
    rmk.add_argument("plan_id")
    rmk.add_argument("--session", type=cliparse.session_id_arg, default=None)
    _add_work_link_flags(rmk)
    rmk.set_defaults(func=cmd_roadmap_link)
    rmm = rmsub.add_parser(
        "merge-state", help="ask git and `gh` whether this plan's work landed. "
                            "Read-only, and answers 'unknown' with a reason "
                            "rather than guessing")
    rmm.add_argument("plan_id")
    rmm.add_argument("--project", type=cliparse.project_arg, default=None)
    rmm.add_argument("--json", action="store_true")
    rmm.set_defaults(func=cmd_roadmap_merge_state)
    rmt = rmsub.add_parser(
        "retire", help="retire every live plan whose work link PROVES the work "
                       "is finished (merged -> complete, closed unmerged -> "
                       "archive); reports every plan it examined")
    rmt.add_argument("--project", type=cliparse.project_arg, default=None)
    rmt.add_argument("--json", action="store_true")
    rmt.add_argument("--session", type=cliparse.session_id_arg, default=None)
    rmt.set_defaults(func=cmd_roadmap_retire)
    rma = rmsub.add_parser("autoclose", help="close a session's auto-synced "
                           "roadmap (run by the SessionEnd hook): complete "
                           "when all steps are done/skipped, else interrupt")
    rma.add_argument("--session", type=cliparse.session_id_arg, default=None,
                     help="session id (defaults to MINDHIVE_SESSION)")
    rma.set_defaults(func=cmd_roadmap_autoclose)
    rml = rmsub.add_parser("list", help="list roadmaps")
    rml.add_argument("--project", type=cliparse.project_arg, default=None)
    rml.add_argument("--json", action="store_true")
    rml.add_argument("--all", action="store_true",
                     help="include retired roadmaps (completed and archived)")
    rml.set_defaults(func=cmd_roadmap_list)
    rmw = rmsub.add_parser("show", help="print one roadmap in full, "
                           "with history")
    rmw.add_argument("plan_id")
    rmw.add_argument("--project", type=cliparse.project_arg, default=None)
    rmw.add_argument("--json", action="store_true")
    rmw.set_defaults(func=cmd_roadmap_show)


def add_autoresume(sub) -> None:
    """Register `hive autoresume`."""
    # --- autoresume (deliver the interruption marker back into Claude) ---
    ar = sub.add_parser("autoresume", help="auto-resume: after the usage "
                        "cooldown, deliver an interrupted plan's marker "
                        "(the ask, progress, next steps) straight back "
                        "into Claude")
    arsub = ar.add_subparsers(dest="autoresume_cmd", required=True)
    ars = arsub.add_parser("schedule", help="schedule the delivery "
                           "(default: interruption time + 5h)")
    ars.add_argument("plan_id", nargs="?", default=None,
                     help="roadmap id (default: the latest interrupted plan)")
    ars.add_argument("--in-hours", type=float, default=None, dest="in_hours",
                     help="cooldown hours from the interruption (default: 5)")
    ars.add_argument("--at", default=None,
                     help="explicit UTC due time, e.g. 2026-07-18T15:00:00Z")
    ars.add_argument("--model", default=None,
                     help="model for the delivered session (default: the "
                     "CLI's configured model)")
    ars.add_argument("--cwd", default=None,
                     help="where the resumed work runs (default: here)")
    ars.add_argument("--project", type=cliparse.project_arg, default=None)
    ars.add_argument("--session", type=cliparse.session_id_arg, default=None)
    ars.set_defaults(func=cmd_autoresume_schedule)
    arl = arsub.add_parser("list", help="list delivery records")
    arl.add_argument("--project", type=cliparse.project_arg, default=None)
    arl.add_argument("--json", action="store_true")
    arl.set_defaults(func=cmd_autoresume_list)
    arc = arsub.add_parser("cancel", help="withdraw a pending delivery "
                           "and its OS timer")
    arc.add_argument("plan_id")
    arc.add_argument("--project", type=cliparse.project_arg, default=None)
    arc.add_argument("--session", type=cliparse.session_id_arg, default=None)
    arc.set_defaults(func=cmd_autoresume_cancel)
    arf = arsub.add_parser("fire", help="deliver the marker now (what the "
                           "OS timer runs at the due time)")
    arf.add_argument("plan_id")
    arf.add_argument("--timeout", type=int, default=None,
                     help="seconds the delivered session may run "
                     "(default: 3600)")
    arf.add_argument("--project", type=cliparse.project_arg, default=None)
    arf.add_argument("--session", type=cliparse.session_id_arg, default=None)
    arf.set_defaults(func=cmd_autoresume_fire)
    arw = arsub.add_parser("wait", help="portable fallback: block until "
                           "each pending delivery is due, then fire it")
    arw.add_argument("--poll", type=float, default=60.0,
                     help="max seconds between due checks (default: 60)")
    arw.add_argument("--project", type=cliparse.project_arg, default=None)
    arw.set_defaults(func=cmd_autoresume_wait)


def add_rules(sub) -> None:
    """Register `hive rules`."""
    # --- rules (Learning Loop reflection stage) ---
    rp = sub.add_parser("rules", help="reflect on incidents; manage prevention rules")
    rsub = rp.add_subparsers(dest="rules_cmd", required=True)
    rl = rsub.add_parser("list", help="list incidents by occurrence")
    rl.add_argument("--project", type=cliparse.project_arg, default=None)
    rl.set_defaults(func=cmd_rules_list)
    rsm = rsub.add_parser("summaries",
                          help="structured lesson records (dashboard parity; --json for raw)")
    rsm.add_argument("--project", type=cliparse.project_arg, default=None)
    rsm.add_argument("--json", action="store_true", help="emit the raw list as JSON")
    rsm.set_defaults(func=cmd_rules_summaries)
    rsh = rsub.add_parser("show", help="print one incident record")
    rsh.add_argument("signature")
    rsh.add_argument("--project", type=cliparse.project_arg, default=None)
    rsh.set_defaults(func=cmd_rules_show)
    rr = rsub.add_parser("reflect",
                         help="bounded reflection pass over active un-reflected incidents")
    rr.add_argument("--project", type=cliparse.project_arg, default=None)
    rr.add_argument("--limit", type=int, default=3,
                    help="max incidents to reflect this batch (default: 3)")
    rr.add_argument("--cooldown-hours", type=float, default=6.0, dest="cooldown_hours",
                    help="skip when the last batch started within this window "
                         "(default: 6; pass 0 to disable)")
    rr.add_argument("--all-projects", dest="all_projects", action="store_true",
                    help="drain every registered namespace (manual backlog drain; "
                         "never invoked from hooks; LIVE_LOCK-named namespaces skipped)")
    rr.set_defaults(func=cmd_rules_reflect)
    rrs = rsub.add_parser("resolve",
                          help="actively resolve red (unresolved) rules: reflect "
                               "missing ones, reroute leaking ones to the best "
                               "alternative route")
    rrs.add_argument("--project", type=cliparse.project_arg, default=None)
    rrs.add_argument("--limit", type=int, default=3,
                     help="max red incidents to resolve this pass (default: 3)")
    rrs.add_argument("--list", action="store_true",
                     help="print the red set as JSON without resolving anything")
    rrs.set_defaults(func=cmd_rules_resolve)
    ra = rsub.add_parser("audit", help="stamp activations for newly-recurring signatures")
    ra.add_argument("--project", type=cliparse.project_arg, default=None)
    ra.set_defaults(func=cmd_rules_audit)
    ri = rsub.add_parser("inject", help="print the active prevention-rules block")
    ri.add_argument("--project", type=cliparse.project_arg, default=None)
    ri.set_defaults(func=cmd_rules_inject)
    rpr = rsub.add_parser("promote", help="promote a rule into canonical SOUL.md")
    rpr.add_argument("signature")
    rpr.add_argument("--project", type=cliparse.project_arg, default=None)
    rpr.set_defaults(func=cmd_rules_promote)
    rdm = rsub.add_parser("demote", help="manually demote a rule")
    rdm.add_argument("signature")
    rdm.add_argument("--project", type=cliparse.project_arg, default=None)
    rdm.set_defaults(func=cmd_rules_demote)
    rc = rsub.add_parser("reconcile", help="record human edits as rule_revised events")
    rc.add_argument("--project", type=cliparse.project_arg, default=None)
    rc.set_defaults(func=cmd_rules_reconcile)
    ren = rsub.add_parser("enforce",
                          help="manage the PreToolUse enforcement manifest (nudge->enforce ladder)")
    ren.add_argument("--project", type=cliparse.project_arg, default=None)
    ren.add_argument("--suggest", action="store_true",
                     help="add disabled stubs for ruled signatures recurring past the threshold")
    ren.add_argument("--enable", metavar="SIG", default=None,
                     help="turn on enforcement for a signature")
    ren.add_argument("--disable", metavar="SIG", default=None,
                     help="turn off enforcement for a signature")
    ren.set_defaults(func=cmd_rules_enforce)


def add_triggers(sub) -> None:
    """Register `hive triggers`."""
    # --- triggers (prompt-quality gate: train/list/check the manifest) ---
    tg = sub.add_parser("triggers",
                        help="prompt-quality trigger rules (UserPromptSubmit gate)")
    tgsub = tg.add_subparsers(dest="triggers_cmd", required=True)
    tgt = tgsub.add_parser("train",
                           help="derive trigger rules from the inventory + ledger "
                                "and store rules/triggers.json")
    tgt.add_argument("--project", type=cliparse.project_arg, default=None)
    tgt.add_argument("--max-age-hours", type=float, default=0.0,
                     dest="max_age_hours",
                     help="stale-gate: skip when the manifest is younger than "
                          "this (0 = always retrain)")
    tgt.set_defaults(func=cmd_triggers_train)
    tgl = tgsub.add_parser("list", help="print the trained trigger rules")
    tgl.add_argument("--project", type=cliparse.project_arg, default=None)
    tgl.add_argument("--json", action="store_true", help="emit the raw manifest")
    tgl.set_defaults(func=cmd_triggers_list)
    tgc = tgsub.add_parser("check",
                           help="assess one prompt offline and show what the "
                                "gate would inject")
    tgc.add_argument("prompt")
    tgc.add_argument("--project", type=cliparse.project_arg, default=None)
    tgc.set_defaults(func=cmd_triggers_check)
