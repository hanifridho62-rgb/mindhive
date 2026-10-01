# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/premerge_cli.py
"""The pre-merge verbs of `hive ci` (spec 2026-09-30): `premerge`, `watch`,
`conflicts` and `legs`.

Registered by `ci.cli.register`, beside the verbs `hive ci` already had. The
modules the verbs run are imported inside each handler, so `hive --help`
pays for argparse and nothing else -- the `ci.cli` convention.

Exit codes are for scripts and scheduled tasks:

* `premerge`: 0 green; 1 red; 2 the check could not be made; 3 green, but
  `--merge` did not merge; 130 stopped by hand (Ctrl+C) -- every leg it
  started is stopped, and a run that got as far as fetching is recorded as an
  error.
* `watch --once`: 0 every PR it looked at is green; 1 one is red; 2 one
  could not be checked; 130 stopped by hand. Without `--once` the watch runs
  until Ctrl+C, which exits 0.
* `conflicts`: 0 none; 1 a PR conflicts with its base or with another PR; 2
  the check could not be made.
* `legs`: 0 at least one full-suite leg can run here; 2 none can, or `--legs`
  names no leg.

Under `--json` stdout is JSON whatever happens: a refusal, a check that could
not be made and a stop by hand print one object, `{"verdict": "error",
"exit_code": N, "reason": "..."}`, beside the line on stderr. Each verb's
`--help` ends with its exit codes.
"""
import argparse
import json
import sys

from .. import cliparse

__all__ = ["cmd_ci_conflicts", "cmd_ci_legs", "cmd_ci_premerge", "cmd_ci_watch",
           "register"]

#: `premerge.DEFAULT_JOBS`, `legrun.LEG_TIMEOUT_S` and `watch.DEFAULT_INTERVAL_S`,
#: spelled here so registering imports nothing; a test holds them equal.
JOBS, TIMEOUT_S, INTERVAL_S = 3, 5400.0, 300.0
#: A verb stopped by hand exits as a shell reports SIGINT: 128 + 2.
INTERRUPTED_EXIT = 130


def _progress(text) -> None:
    print(text, file=sys.stderr, flush=True)


def _fail(args, verb, why, code=2) -> int:
    """Say why *verb* stopped -- one line on stderr, and under `--json` one
    JSON object on stdout, so a script parsing stdout always gets JSON.
    Returns *code*."""
    print(f"hive ci {verb}: {why}", file=sys.stderr)
    if getattr(args, "json", False):
        print(json.dumps({"verdict": "error", "exit_code": int(code), "reason": str(why)},
                         ensure_ascii=False, sort_keys=True))
    return code


def _premerge(args, *, statuses):
    from . import premerge
    from .cli import root_of
    return premerge.Premerge(root_of(args), project=getattr(args, "project", None),
                             jobs=args.jobs, timeout_s=args.timeout,
                             post_statuses=statuses and not args.no_status,
                             keep=args.keep, work_root=args.work_root, say=_progress)


def cmd_ci_premerge(args) -> int:
    """Test one PR (or a local branch) the way GitHub CI would."""
    branch = (args.branch or "").strip()
    refusal = ""
    if bool(args.pr) == bool(branch):
        refusal = "give exactly one of a PR number or --branch"
    elif branch and args.merge:
        refusal = "--merge needs a PR number: a local branch is never merged"
    elif args.merge and args.legs:
        refusal = "--merge tests every leg: drop --legs"
    elif args.allow_base_drift and not args.merge:
        refusal = "--allow-base-drift only means something with --merge"
    if refusal:
        return _fail(args, "premerge", refusal)
    runner = _premerge(args, statuses=bool(args.pr))
    try:
        if args.pr:
            res = runner.run_pr(args.pr, legs_spec=args.legs, merge=args.merge,
                                allow_base_drift=args.allow_base_drift,
                                trust=args.trust)
        else:
            res = runner.run_branch(branch, base=args.base, legs_spec=args.legs)
    except KeyboardInterrupt:
        return _fail(args, "premerge", "interrupted; every leg it started was stopped",
                     INTERRUPTED_EXIT)
    if args.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(res.report_text)
    return int(res.exit_code)


def cmd_ci_watch(args) -> int:
    """Keep every open PR's verdict current. Never merges."""
    from . import legs, watch
    try:
        legs.select(args.legs)
    except ValueError as exc:
        return _fail(args, "watch", exc)
    watcher = watch.Watcher(_premerge(args, statuses=True), legs_spec=args.legs,
                            skip_drafts=args.skip_drafts, interval_s=args.interval,
                            say=_progress)
    if not args.once:
        def line(actions):
            print(json.dumps(actions, ensure_ascii=False, sort_keys=True), flush=True)
        try:
            if args.json:
                watcher.loop(report=line)
            else:
                watcher.loop()
        except KeyboardInterrupt:
            pass
        return 0
    try:
        actions = watcher.once()
    except KeyboardInterrupt:
        return _fail(args, "watch", "interrupted; every leg it started was stopped",
                     INTERRUPTED_EXIT)
    if args.json:
        print(json.dumps(actions, ensure_ascii=False, sort_keys=True))
    verdicts = {a.get("verdict") for a in actions}
    if "red" in verdicts:
        return 1
    if "error" in verdicts or any(a.get("action") == "error" for a in actions):
        return 2
    return 0


def cmd_ci_conflicts(args) -> int:
    """Which open PRs into --base conflict with it, and with each other."""
    from . import github as gh_mod
    from . import premerge, preview
    from .cli import root_of
    root = root_of(args)
    runner = premerge.Premerge(root, post_statuses=False)
    try:
        prs = [p for p in runner.github.open_prs()
               if str(p.get("baseRefName") or "") == args.base]
    except gh_mod.GitHubError as exc:
        return _fail(args, "conflicts", exc)
    numbers = sorted(int(p.get("number") or 0) for p in prs)
    base_sha, matrix = "", {"base": {}, "pairs": [], "errors": {}}
    if numbers:
        base_sha, heads, error = runner.fetch_heads(numbers, args.base)
        if error:
            return _fail(args, "conflicts", error)
        matrix = preview.conflict_matrix(root, base_sha, heads, run=runner.spawn)
    if args.json:
        print(json.dumps({"base": args.base, "base_sha": base_sha, "prs": numbers,
                          "against_base": {str(k): v for k, v in matrix["base"].items()},
                          "pairs": matrix["pairs"], "errors": matrix["errors"]},
                         ensure_ascii=False, sort_keys=True))
    else:
        _print_matrix(args.base, base_sha, numbers, matrix)
    if matrix["errors"]:
        return 2
    return 1 if any(matrix["base"].values()) or matrix["pairs"] else 0


def _print_matrix(base, base_sha, numbers, matrix) -> None:
    if not numbers:
        print(f"no open PR targets {base}")
        return
    print(f"open PRs into {base} at {base_sha[:12]}: "
          + " ".join(f"#{n}" for n in numbers))
    print(f"against {base}:")
    for n in numbers:
        if n in matrix["base"]:
            found = matrix["base"][n]
            print(f"  #{n}  " + ("CONFLICTS: " + ", ".join(found) if found else "clean"))
    if matrix["pairs"]:
        print("pairs that cannot both land as they stand:")
        for pair in matrix["pairs"]:
            print(f"  #{pair['a']} + #{pair['b']}: " + ", ".join(pair["paths"]))
    else:
        print("pairs: none conflict")
    for key, why in sorted(matrix["errors"].items()):
        print(f"  could not check {key}: {why}")


def cmd_ci_legs(args) -> int:
    """The ci.yml legs, and whether this machine can run each."""
    from . import legs
    try:
        chosen = legs.select(args.legs)
    except ValueError as exc:
        return _fail(args, "legs", exc)
    probe = legs.Probe()
    rows = []
    for leg in chosen:
        avail = probe.check(leg)
        rows.append({"name": leg.name, "job": leg.job, "ci_name": leg.ci_name,
                     "tier": leg.tier,
                     "ok": avail.ok, "via": avail.via, "python": avail.python,
                     "reason": avail.reason, "note": leg.note})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
    else:
        width = max(len(r["name"]) for r in rows)
        for r in rows:
            what = (f"ready ({r['via']}: {r['python']})" if r["ok"]
                    else f"NOT RUNNABLE: {r['reason']}")
            print(f"{r['name']:<{width}}  {what}")
    return 0 if any(r["ok"] and r["tier"] == legs.TIER_DEFAULT for r in rows) else 2


#: What `--json` prints for a refusal or a stop, as each verb's `--help` shows it.
_JSON_REFUSAL = '  {"verdict": "error", "exit_code": N, "reason": "..."}'
_EXITS = {
    "premerge": (("0", "green"), ("1", "red"), ("2", "the check could not be made"),
                 ("3", "green, but --merge did not merge"),
                 ("130", "stopped by hand (Ctrl+C): every leg it started is stopped")),
    "watch": (("0", "every PR the pass looked at is green; a loop stopped by Ctrl+C"),
              ("1", "a PR is red"), ("2", "a PR could not be checked"),
              ("130", "--once stopped by hand (Ctrl+C)")),
    "conflicts": (("0", "no conflict"),
                  ("1", "a PR conflicts with its base or with another PR"),
                  ("2", "the check could not be made")),
    "legs": (("0", "a full-suite leg can run here"),
             ("2", "none can, or --legs names no leg")),
}
_JSON_NOTES = {
    "premerge": "Under --json a refusal or a stop is one JSON object too:",
    "watch": ("Under --json, --once prints the pass's actions as one array and a loop\n"
              "one line per pass; every action has pr, action, verdict, exit_code and\n"
              "detail. A refusal or a stop is one JSON object:"),
    "conflicts": "Under --json a check that could not be made is one JSON object:",
    "legs": "Under --json a refusal is one JSON object:",
}


def _verb(group, name, help_text):
    """A verb's parser, its `--help` ending with the verb's exit codes."""
    epilog = ("exit codes:\n"
              + "\n".join(f"  {code:<4} {what}" for code, what in _EXITS[name])
              + f"\n\n{_JSON_NOTES[name]}\n{_JSON_REFUSAL}")
    return group.add_parser(name, help=help_text, epilog=epilog,
                            formatter_class=argparse.RawDescriptionHelpFormatter)


def _common(p) -> None:
    p.add_argument("--legs", default=None,
                   help="comma-separated leg names or globs (default: every leg; "
                        "see `hive ci legs`)")
    p.add_argument("--jobs", type=int, default=JOBS,
                   help=f"legs run at once (default {JOBS})")
    p.add_argument("--timeout", type=float, default=TIMEOUT_S,
                   help=f"seconds each leg's pytest may run (default {TIMEOUT_S:.0f})")
    p.add_argument("--no-status", action="store_true",
                   help="post no commit status to GitHub")
    p.add_argument("--keep", action="store_true",
                   help="keep each leg's directory for inspection")
    p.add_argument("--work-root", default=None,
                   help="where the legs run (default <tempdir>/mindhive-ci)")
    p.add_argument("--root", default=None,
                   help="the repository (default: the one this runs in)")
    p.add_argument("--project", type=cliparse.project_arg, default=None,
                   help="the MindHive namespace the run is recorded in")


def register(group) -> None:
    """Add the four verbs to the `hive ci` subparser set *group*."""
    pm = _verb(group, "premerge",
               "test a PR the way GitHub CI did -- every ci.yml leg, on this "
               "machine -- and post the verdict as commit statuses")
    pm.add_argument("pr", nargs="?", type=int, default=None, help="the PR number")
    pm.add_argument("--branch", default=None,
                    help="test a local branch instead: no statuses, never merged")
    pm.add_argument("--base", default="master",
                    help="the base a --branch merges into (default master)")
    _common(pm)
    pm.add_argument("--merge", action="store_true",
                    help="when green, merge: a merge commit, the tested head pinned")
    pm.add_argument("--allow-base-drift", action="store_true",
                    help="with --merge: merge even when the base moved to a merge "
                         "tree this run did not test")
    pm.add_argument("--trust", action="store_true",
                    help="run a PR from a fork: its code runs on this machine as you, "
                         "so read its diff first")
    pm.add_argument("--json", action="store_true", help="print the result as JSON")
    pm.set_defaults(func=cmd_ci_premerge)

    w = _verb(group, "watch",
              "test each open PR whose head moved, re-check each whose base "
              "moved; never merges")
    _common(w)
    w.add_argument("--once", action="store_true",
                   help="one pass, then exit (for a scheduled task)")
    w.add_argument("--interval", type=float, default=INTERVAL_S,
                   help=f"seconds between passes (default {INTERVAL_S:.0f})")
    w.add_argument("--skip-drafts", action="store_true", help="leave draft PRs alone")
    w.add_argument("--json", action="store_true",
                   help="print what each pass did as JSON: --once one array, "
                        "a loop one line per pass")
    w.set_defaults(func=cmd_ci_watch)

    c = _verb(group, "conflicts",
              "which open PRs conflict with their base, and with each other")
    c.add_argument("--base", default="master",
                   help="the base branch whose PRs are compared (default master)")
    c.add_argument("--root", default=None,
                   help="the repository (default: the one this runs in)")
    c.add_argument("--json", action="store_true", help="print the matrix as JSON")
    c.set_defaults(func=cmd_ci_conflicts)

    lg = _verb(group, "legs", "the ci.yml legs, and whether this machine can run each")
    lg.add_argument("--legs", default=None, help="comma-separated names or globs")
    lg.add_argument("--json", action="store_true", help="print the legs as JSON")
    lg.set_defaults(func=cmd_ci_legs)
