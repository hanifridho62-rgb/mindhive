# SPDX-License-Identifier: Apache-2.0
# src/mindhive/consensus/cli.py
"""The `hive consensus` group: its parser and its handlers, in one file.

WHY THIS IS NOT IN THE PARSER MODULE. When this was written that module was
`__main__.py`, nearly four times the 1000-line cap and on
`tests/test_file_size_cap.py`'s allowlist at a recorded length — a ratchet, not
a licence, so a new group adding a hundred lines there was a test failure and
deserved to be. Since the P9 split the wiring is `commands/parsers/`, capped at
500 lines per module by `tests/test_commands_reexport.py`, and the argument
holds unchanged. `pgcli.register` (after `bench`) made self-registration the
convention for a multi-subcommand group: two lines in the parser module — one
import, one `register(sub)` call — and every flag sits beside the handler that
reads it.

WHY THE HANDLERS SIT BESIDE THE PARSER RATHER THAN BESIDE THE FOLD. The fold in
`__init__.py` takes events and returns data — no printing, no exit codes, no
argv. Everything in this file is about the terminal: parsing a string into a
number, choosing an exit code, rendering a row a human reads. Keeping that out of
the fold is what lets the HTTP route and a job reuse the fold without inheriting
a CLI's opinions.

The package is imported at module scope rather than per-handler, and that is not
a deviation from `pgcli`'s deferred-import rule: it is that rule applied. `pg`
defers because `psycopg` and NumPy are expensive and optional. Importing THIS
package costs `hashlib`, `fractions`, and `re`, and importing this module already
pays it — a lazy import of your own package you have already loaded buys a
reader's confusion and no startup.

THE EXIT-CODE CONTRACT, and why each code is what it is:

* ``0`` on a successful append or fold, AND on an empty window. `nothing_to_do`
  is a success in this codebase's vocabulary (`outcomes.py`), and a scheduled
  weekly fold that exits non-zero every quiet week trains whoever wired it to
  stop reading the code.
* ``1`` on a refused body — an expected domain rejection, reported with the
  contract's own corrective hint instead of a traceback.
* ``1`` from `trace` when any input resolves to no event. The gate semantics
  `hive pg parity` already sets: non-zero when the claim cannot be PROVEN, not
  only when it is known false.
"""
import json

from .. import events as events_mod
from .. import cliparse
from . import (contribute, event_ref, fold_and_record, latest_record, trace)


def _scalar(raw):
    """A CLI argument is always a string; a frame's value usually is not.

    Parse as JSON and keep the result ONLY when it is a real number. Everything
    else — ``true``, ``null``, ``[1,2]`` — stays the string the operator typed.
    That is both the friendlier reading (``--value true`` is a category name in a
    categorical frame) and the safe one: ``True`` is an ``int`` in Python and
    would otherwise fold as 1 into a numeric frame it does not belong to.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(parsed, bool):
        return raw
    return parsed if isinstance(parsed, (int, float)) else raw


def cmd_contribute(args) -> int | None:
    """Append one framed contribution."""
    confidence = None
    if args.confidence is not None:
        try:
            confidence = float(args.confidence)
        except (TypeError, ValueError):
            print("--confidence must be a number in [0, 1]")
            return 1
    value = _scalar(args.value)
    try:
        ev = contribute(args.kind, args.frame, args.subject, value,
                        confidence=confidence, session_id=args.session,
                        project=args.project)
    except ValueError as e:
        print(f"refused: {e}")
        return 1
    print(f"contribution recorded  frame={args.frame}  subject={args.subject}  "
          f"value={value!r}")
    # The ref is printed because it is what a later `trace` resolves; without it
    # the operator has no handle on the row they just wrote.
    print(f"ref {event_ref(ev)}")
    return 0


def cmd_fold(args) -> int | None:
    """Fold a frame's window into a `consensus_record`."""
    try:
        ev = fold_and_record(args.frame, start=args.start, end=args.end,
                             session_id=args.session, project=args.project)
    except ValueError as e:
        print(f"refused: {e}")
        return 1
    if ev is None:
        print(f"nothing to fold for frame {args.frame!r} — no well-formed "
              "contribution in the window")
        return 0
    payload = ev.payload
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"consensus {payload['value']!r} ({payload['value_kind']}) over "
          f"{payload['subjects']} subject(s) from {len(payload['inputs'])} "
          "input(s)")
    print(f"rule {payload['rule_version']}")
    print(f"window {payload['window']['start']} .. {payload['window']['end']} "
          "(inclusive)")
    return 0


def cmd_trace(args) -> int | None:
    """Walk a frame's newest record back to the events it folded."""
    evs = events_mod.read_all(args.project)
    record = latest_record(evs, args.frame)
    if record is None:
        print(f"no consensus_record for frame {args.frame!r} yet — run "
              f"`hive consensus fold --frame {args.frame}`")
        return 0
    walked = trace(evs, record)
    if args.json:
        print(json.dumps({"record": record, "trace": walked},
                         indent=2, sort_keys=True))
    else:
        print(f"frame {args.frame}  value {record['value']!r}  "
              f"rule {record['rule_version']}")
        for row in walked:
            mark = "ok     " if row["found"] else "MISSING"
            # The occurrence is shown because a subject may send the SAME value
            # twice in one second, which makes two events with one content
            # address; "#2" is what tells the reader these are two rows.
            line = (f"  {mark} {str(row['ref'])[:12]}#{row['occurrence']}"
                    f"  weight {row['weight']}  — {row['reason']}")
            if row["found"]:
                payload = row["event"].get("payload") or {}
                line += (f"  [subject {payload.get('subject')!r} "
                         f"value {payload.get('value')!r}]")
            print(line)
    unresolved = [row for row in walked if not row["found"]]
    if unresolved:
        print(f"{len(unresolved)} of {len(walked)} input(s) resolve to no event "
              "in this ledger — the record cannot be proven against it")
        return 1
    return 0


def register(subparsers) -> None:
    """Wire the `consensus` group onto the root subparser set built by
    `commands.parsers._build_parser`.

    Three verbs and not one per aggregation shape: `fold` reads the frame's own
    data to decide numeric-vs-categorical, so an operator never has to know which
    kind of frame they are folding. `trace` is a separate verb rather than a flag
    on `fold` because it is the AUDIT surface — the thing you run when you
    distrust a number, which is exactly when you must not have to re-fold (and
    so re-write) it in order to look at it.
    """
    cs = subparsers.add_parser(
        "consensus", help="participatory consensus: framed contributions "
                          "folded into one traceable number")
    sub = cs.add_subparsers(dest="consensus_cmd", required=True)

    contribute_p = sub.add_parser("contribute",
                                  help="append one framed contribution")
    contribute_p.add_argument("--frame", required=True,
                              help="frame id — the framed question answered")
    contribute_p.add_argument("--kind", required=True,
                              help="instrument id, e.g. biometric.resting_hr "
                                   "or strategic.direction — the ONLY thing "
                                   "separating one population's inputs from "
                                   "another's")
    contribute_p.add_argument("--subject", required=True,
                              help="participant id. Every subject carries "
                                   "total weight 1 however many times they "
                                   "contribute")
    contribute_p.add_argument("--value", required=True,
                              help="the measurement. Parsed as a number when "
                                   "it parses, kept as a string when it does "
                                   "not")
    contribute_p.add_argument("--confidence", default=None,
                              help="optional 0..1 multiplier the contributor "
                                   "states. It scales the weight, so it is "
                                   "bounded")
    contribute_p.add_argument("--session", type=cliparse.session_id_arg, default=None)
    contribute_p.add_argument("--project", type=cliparse.project_arg, default=None)
    contribute_p.set_defaults(func=cmd_contribute)

    fold_p = sub.add_parser("fold",
                            help="fold a frame's window into a consensus_record")
    fold_p.add_argument("--frame", required=True)
    fold_p.add_argument("--start", default=None,
                        help="ISO timestamp, inclusive (default: earliest input)")
    fold_p.add_argument("--end", default=None,
                        help="ISO timestamp, inclusive (default: latest input)")
    fold_p.add_argument("--json", action="store_true")
    fold_p.add_argument("--session", type=cliparse.session_id_arg, default=None)
    fold_p.add_argument("--project", type=cliparse.project_arg, default=None)
    fold_p.set_defaults(func=cmd_fold)

    trace_p = sub.add_parser("trace",
                             help="walk a frame's newest record back to the "
                                  "events it folded. Exits non-zero when an "
                                  "input cannot be resolved")
    trace_p.add_argument("--frame", required=True)
    trace_p.add_argument("--json", action="store_true")
    trace_p.add_argument("--project", type=cliparse.project_arg, default=None)
    trace_p.set_defaults(func=cmd_trace)
