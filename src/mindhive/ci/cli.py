# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/cli.py
"""`hive ci` — the native CI gate and the integration circuit (P1f, P1g).

`run` is the daily gate. `.github/workflows/ci.yml` is manual-and-tag only
(RP III.2) precisely because this one runs on the operator's own machine, in
a worktree of its own, offline if `gh` is not logged in. `hook-install` puts
it on `pre-push`, which is where a gate has to be to actually gate anything.

`status` reads what `run` wrote: the last `verification` carrying
`scope: "ci"`, whichever gate decided it. Exit codes are for scripts -- 0 the
last run was green, 1 it was red, 2 there has never been one -- because "no
CI run has ever been recorded" and "the last one passed" are different facts
and a status command that spells them the same way is not worth running.

`hive ci breaker` is the operator's door to the integration circuit (P1f).

QUARANTINED is reachable and `IntegrationBreaker.manual_reset` is its ONLY
exit, so shipping the state without the command would have shipped a circuit
that refuses every integration in a project permanently while telling the
operator to run something that does not exist. The adversarial review that
found it put the consequence plainly: the only recovery left was deleting
`~/.mindhive/<project>/state/integration_breaker.json` by hand, which nothing
in the message, the docs or `hive --help` mentioned.

Two verbs, and the split between them is the point:

* `status` READS. It never calls `check()`, because `check` is a transition --
  an OPEN circuit whose cooldown has elapsed moves itself to HALF_OPEN and
  appends an event as it answers. An operator asking what the circuit is doing
  must not move it, so the report is built from `state()` and `cooldown_left()`.
* `reset` WRITES, and says what it changed. It is total over every state: a
  reset from CLOSED is a no-op the report still names, so a script does not
  have to know the state before it asks.

Self-registering, the `pgcli.register` convention: one named call in
`commands/parsers/__init__.py`, every flag beside the handler that reads it,
and `orchestrator.breaker` imported INSIDE the handlers so `hive --help` does
not pay for the orchestrator package.
"""
import json
import os
import stat
import sys
from pathlib import Path

from .. import cliparse, paths

__all__ = ["PRE_PUSH_HOOK", "cmd_ci_breaker_reset", "cmd_ci_breaker_status",
           "cmd_ci_hook_install", "cmd_ci_run", "cmd_ci_status", "register", "root_of"]

#: The `pre-push` hook `hook-install` writes. POSIX `sh`, because that is what
#: git runs it with on every platform including Windows (Git for Windows ships
#: its own `sh`), and LF-only, because a CRLF `#!` line is not an interpreter.
PRE_PUSH_HOOK = """#!/bin/sh
# MindHive native CI (blueprint P1g). Written by `hive ci hook-install`.
#
# git feeds one line per ref being pushed on stdin:
#     <local ref> <local sha> <remote ref> <remote sha>
# A DELETION has an all-zero local sha and no commit to gate, so it is skipped
# rather than gated against a ref that is going away. A non-zero exit refuses
# the push, which is the only thing a pre-push hook can do.
status=0
while read -r local_ref local_sha remote_ref remote_sha; do
    case "$local_sha" in
        *[!0]*) ;;
        *) continue ;;
    esac
    case "$local_ref" in
        refs/heads/*) branch=${local_ref#refs/heads/} ;;
        *) continue ;;
    esac
    hive ci run --branch "$branch" || status=1
done
exit $status
"""


def _breaker(args):
    """The circuit for the namespace *args* names, and the file behind it."""
    from ..orchestrator import breaker as brk
    project = getattr(args, "project", None)
    return (brk.IntegrationBreaker(project=project),
            paths.state_dir(project) / brk.STATE_FILENAME)


def _report(breaker, path) -> dict:
    """The machine-readable shape both verbs print. Read-only."""
    st = breaker.state()
    return {
        "state": st.state,
        "failure_count": st.failure_count,
        "last_trip_ts": st.last_trip_ts,
        "trips_in_window": len(st.trips_in_window),
        "trip_window_s": breaker.TRIP_WINDOW_S,
        "max_trips_in_window": breaker.MAX_TRIPS_IN_WINDOW,
        "failure_threshold": breaker.FAILURE_THRESHOLD,
        "cooldown_period_s": breaker.COOLDOWN_PERIOD_S,
        "cooldown_left_s": round(breaker.cooldown_left(), 3),
        "quarantine_reason": st.quarantine_reason,
        "path": str(path),
        "exists": path.exists(),
    }


def _print(report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    print(f"state: {report['state']}")
    print(f"failures in a row: {report['failure_count']} "
          f"of {report['failure_threshold']}")
    print(f"trips in the window: {report['trips_in_window']} "
          f"of {report['max_trips_in_window']} "
          f"in {report['trip_window_s']:g}s")
    if report["cooldown_left_s"] > 0:
        print(f"cooldown left: {report['cooldown_left_s']:.0f}s "
              f"of {report['cooldown_period_s']:g}s")
    if report["quarantine_reason"]:
        print(f"quarantined: {report['quarantine_reason']}")
    print(f"record: {report['path']}"
          + ("" if report["exists"] else " (not written yet)"))


def cmd_ci_breaker_status(args) -> int:
    """What the integration circuit is doing, without moving it.

    Non-zero when the circuit is refusing, so a script can gate on it: 0 while
    it allows (CLOSED or HALF_OPEN), 1 while OPEN, 2 while QUARANTINED, which
    needs a person rather than a wait."""
    from .. import models
    breaker, path = _breaker(args)
    report = _report(breaker, path)
    _print(report, getattr(args, "json", False))
    if report["state"] == models.QUARANTINED:
        return 2
    return 1 if report["state"] == models.OPEN else 0


def cmd_ci_breaker_reset(args) -> int:
    """Close the circuit by hand, from ANY state, and name what changed.

    The trip history goes with it -- the operator has looked, so the next trip
    starts a fresh window. Always 0: a reset of an already-closed circuit is
    the state the caller asked for, not a failure."""
    breaker, path = _breaker(args)
    was = breaker.state().state
    breaker.manual_reset()
    report = _report(breaker, path)
    report["was"] = was
    report["changed"] = was != report["state"]
    if getattr(args, "json", False):
        _print(report, True)
        return 0
    if report["changed"]:
        print(f"integration breaker: {was} -> {report['state']}")
    else:
        print(f"integration breaker: already {report['state']}, nothing to reset")
    _print(report, False)
    return 0


# --- `run`: the four gates ---------------------------------------------------------------


def root_of(args):
    """The repository the verb acts on. `--root` when given, else this one.
    The pre-merge verbs (`premerge_cli`) resolve theirs the same way."""
    given = getattr(args, "root", None)
    if given:
        return Path(given)
    from .. import repo as repo_mod         # lazy: `hive --help` pays nothing
    return Path(repo_mod.worktree_root() or Path.cwd())


def _current_branch(root) -> str:
    """The branch checked out at *root*, or `""`.

    A DETACHED head answers `HEAD`, which is not a branch name -- gating a ref
    called `HEAD` would run the whole suite against something git cannot
    resolve back to the work, so it reads as "could not tell" and the caller
    refuses rather than guessing."""
    from . import runner as rn
    rc, out, _ = rn._default_runner([rn.GIT, "rev-parse", "--abbrev-ref", "HEAD"],
                                    cwd=str(root))
    name = (out or "").strip()
    return "" if rc != 0 or name in ("", "HEAD") else name


def cmd_ci_run(args) -> int:
    """Run the four gates for a branch. 0 green, 1 red, 2 it could not start."""
    from . import runner as rn
    root = root_of(args)
    branch = (getattr(args, "branch", None) or "").strip() or _current_branch(root)
    if not branch:
        print("hive ci run: no --branch given, and git could not name the "
              f"branch checked out at {root}", file=sys.stderr)
        return 2
    runner = rn.NativeCIRunner(root, project=getattr(args, "project", None))
    res = runner.run_pr_gate(int(getattr(args, "pr", 0) or 0), branch,
                             auto_merge=bool(getattr(args, "merge", False)))
    if getattr(args, "json", False):
        print(json.dumps(res.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(res.report_text)
    return 0 if res.ok else 1


# --- `status`: what the last run wrote ----------------------------------------------------


def _ci_rows(args) -> list:
    """Every recorded CI verification as ``(recorded_at, row)``, oldest first:
    only one PR's, one gate's, one branch's when `--pr`, `--gate`, `--branch`
    say so.

    Scope is checked, not assumed: a `hive selftest` run is a `verification`
    too, and reporting one as the last CI run would tell the operator a gate
    passed that never ran."""
    from .. import events as ev_mod
    from . import runner as rn
    pr = getattr(args, "pr", None)
    gate, branch = getattr(args, "gate", None), getattr(args, "branch", None)
    rows = []
    for e in ev_mod.read_all(getattr(args, "project", None)):
        payload = getattr(e, "payload", None)
        if e.type != "verification" or not isinstance(payload, dict):
            continue
        if payload.get("scope") != rn.SCOPE:
            continue
        if pr and int(payload.get("pr_id") or 0) != int(pr):
            continue
        if (gate and payload.get("gate") != gate
                or branch and payload.get("branch") != branch):
            continue
        rows.append((e.ts, payload))
    return rows


def _print_ci(row, recorded_at="") -> None:
    print(f"PR #{row.get('pr_id', 0)} on {row.get('branch') or '(no branch)'}")
    if recorded_at:
        print(f"recorded: {recorded_at}")
    print(f"{row.get('gate') or 'unknown gate'}: exit {row.get('exit_code')}")
    if row.get("verdict"):
        print(f"verdict: {row['verdict']}"
              + (f" -- {row['reason']}" if row.get("reason") else ""))
    merge = str(row.get("merge_state") or "unknown")
    if row.get("merge_reason"):
        merge += f" -- {row['merge_reason']}"
    print(f"mode: {row.get('mode') or 'unknown'}, merge: {merge}")
    counts = row.get("counts") or {}
    if counts:
        whose = f" ({row['counts_leg']})" if row.get("counts_leg") else ""
        print(f"counts{whose}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    legs = [leg for leg in row.get("legs") or [] if isinstance(leg, dict)]
    if legs:
        tally = {}
        for leg in legs:
            state = str(leg.get("state") or "unknown")
            tally[state] = tally.get(state, 0) + 1
        missing = [str(u.get("name")) for u in row.get("unavailable") or []
                   if isinstance(u, dict)]
        print("legs: " + ", ".join(f"{n} {s}" for s, n in sorted(tally.items()))
              + ("; not run: " + ", ".join(missing) if missing else ""))
    if row.get("commit_sha"):
        print(f"commit: {row['commit_sha'][:12]}")
    if row.get("artifact"):
        print(f"artifact: {row['artifact']}")


def _green(row) -> bool:
    """A pre-merge run is judged by its verdict: its exit 3 is a green run whose
    `--merge` did not happen, and calling that red would misreport the tests."""
    if row.get("gate") == "premerge":
        return row.get("verdict") == "green"
    return int(row.get("exit_code") or 0) == 0


def cmd_ci_status(args) -> int:
    """The last recorded CI run. 0 it was green, 1 red or an error, 2 there is
    none -- under `--json` that is said in JSON too, with the filters asked."""
    rows = _ci_rows(args)
    page = max(1, int(getattr(args, "page", 1) or 1))
    pr = getattr(args, "pr", None)
    gate, branch = getattr(args, "gate", None), getattr(args, "branch", None)
    if len(rows) < page:
        if getattr(args, "json", False):
            print(json.dumps({"found": False, "pr": pr, "gate": gate, "branch": branch,
                              "page": page}, ensure_ascii=False, sort_keys=True))
        else:
            print("hive ci status: no CI run recorded"
                  + (f" for PR #{pr}" if pr else "")
                  + (f" by gate {gate}" if gate else "")
                  + (f" on branch {branch}" if branch else "")
                  + (f" at page {page}" if page > 1 else ""))
        return 2
    recorded_at, row = rows[-page]
    if getattr(args, "json", False):
        print(json.dumps({**row, "recorded_at": recorded_at},
                         ensure_ascii=False, sort_keys=True))
    else:
        _print_ci(row, recorded_at)
    return 0 if _green(row) else 1


# --- `hook-install`: put the gate where pushes pass ---------------------------------------


def _hooks_dir(root):
    """`<common dir>/hooks`, or None outside a repository.

    The COMMON dir, asked of git rather than assembled: in a linked worktree
    `.git` is a FILE, so `<root>/.git/hooks` is both unwritable and the wrong
    place -- git runs hooks from the main repository's `.git/hooks` for every
    worktree."""
    from . import runner as rn
    rc, out, _ = rn._default_runner([rn.GIT, "rev-parse", "--git-common-dir"],
                                    cwd=str(root))
    if rc != 0 or not (out or "").strip():
        return None
    common = Path(out.strip())
    if not common.is_absolute():
        common = Path(root) / common
    return common / "hooks"


def cmd_ci_hook_install(args) -> int:
    """Write `pre-push`. 0 written, 1 one is already there, 2 not a repo."""
    root = root_of(args)
    hooks = _hooks_dir(root)
    if hooks is None:
        print(f"hive ci hook-install: {root} is not inside a git repository",
              file=sys.stderr)
        return 2
    hook = hooks / "pre-push"
    if hook.exists() and not getattr(args, "force", False):
        print(f"hive ci hook-install: {hook} already exists and was left "
              f"alone — pass --force to replace it")
        return 1
    hooks.mkdir(parents=True, exist_ok=True)
    # `newline="\n"`: text mode would translate to CRLF on Windows, and a
    # `#!/bin/sh\r` shebang names an interpreter that does not exist.
    hook.write_text(PRE_PUSH_HOOK, encoding="utf-8", newline="\n")
    try:
        mode = os.stat(hook).st_mode
        os.chmod(hook, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:                                     # noqa: BLE001
        pass                  # Windows has no x bit; git runs it regardless
    print(f"hive ci hook-install: wrote {hook}")
    return 0


def register(subparsers) -> None:
    """Wire the `ci` group onto the root subparser set built by
    `commands.parsers._build_parser`."""
    ci = subparsers.add_parser(
        "ci", help="continuous integration: the native gate and the "
                   "integration circuit breaker")
    group = ci.add_subparsers(dest="ci_cmd", required=True)

    run = group.add_parser(
        "run", help="the four gates, in a git worktree of their own")
    run.add_argument("--pr", type=int, default=0,
                     help="the PR this branch is (0: none, so no merge step)")
    run.add_argument("--branch", default=None,
                     help="the branch to gate (default: the one checked out)")
    run.add_argument("--merge", action="store_true",
                     help="land it when every gate is green")
    run.add_argument("--root", default=None,
                     help="the repository to gate (default: this one)")
    run.add_argument("--project", type=cliparse.project_arg, default=None,
                     help="namespace (default: this repo)")
    run.add_argument("--json", action="store_true",
                     help="machine-readable result")
    run.set_defaults(func=cmd_ci_run)

    report = group.add_parser(
        "status", help="the last recorded CI run (exit 1 red or error, 2 none)")
    report.add_argument("--pr", type=int, default=None,
                        help="only this PR's runs")
    report.add_argument("--gate", default=None,
                        help="only this gate's runs: premerge, or the gate that "
                             "decided a `hive ci run`, such as gate2_suite")
    report.add_argument("--branch", default=None, help="only this branch's runs")
    report.add_argument("--page", type=int, default=1,
                        help="1 is the newest run, 2 the one before it")
    report.add_argument("--project", type=cliparse.project_arg, default=None,
                        help="namespace (default: this repo)")
    report.add_argument("--json", action="store_true",
                        help="machine-readable report")
    report.set_defaults(func=cmd_ci_status)

    hook = group.add_parser(
        "hook-install", help="put `hive ci run` on this repo's pre-push hook")
    hook.add_argument("--root", default=None,
                      help="the repository (default: this one)")
    hook.add_argument("--force", action="store_true",
                      help="replace a pre-push hook that is already there")
    hook.set_defaults(func=cmd_ci_hook_install)

    breaker = group.add_parser(
        "breaker", help="the integration circuit: what it is doing, and the "
                        "only way out of QUARANTINED")
    verbs = breaker.add_subparsers(dest="ci_breaker_cmd", required=True)

    status = verbs.add_parser(
        "status", help="read the circuit without moving it (exit 1 OPEN, "
                       "2 QUARANTINED)")
    status.add_argument("--project", type=cliparse.project_arg, default=None,
                        help="namespace (default: this repo)")
    status.add_argument("--json", action="store_true",
                        help="machine-readable report")
    status.set_defaults(func=cmd_ci_breaker_status)

    reset = verbs.add_parser(
        "reset", help="close the circuit by hand and clear its trip history")
    reset.add_argument("--project", type=cliparse.project_arg, default=None,
                       help="namespace (default: this repo)")
    reset.add_argument("--json", action="store_true",
                       help="machine-readable report")
    reset.set_defaults(func=cmd_ci_breaker_reset)

    from . import premerge_cli      # lazy: its handlers import what they run
    premerge_cli.register(group)
