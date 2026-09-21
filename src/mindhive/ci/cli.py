# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/cli.py
"""`hive ci breaker` — the operator's door to the integration circuit (P1f).

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

from .. import cliparse, paths

__all__ = ["cmd_ci_breaker_reset", "cmd_ci_breaker_status", "register"]


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


def register(subparsers) -> None:
    """Wire the `ci` group onto the root subparser set built by
    `commands.parsers._build_parser`. `hive ci run` joins it in P1g."""
    ci = subparsers.add_parser(
        "ci", help="continuous integration: the integration circuit breaker")
    group = ci.add_subparsers(dest="ci_cmd", required=True)

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
