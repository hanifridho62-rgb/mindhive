#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Committed stand-in for a task's ``validation_script`` — MN-1 U6's test seam.

The executor's whole job is to run a string an agent authored and turn what
comes back into durable evidence, so the behaviour under test IS the exit code,
the captured streams, the timeout kill, and the argv that actually reached the
child. Mocking ``subprocess`` would assert only that the module calls a function
we wrote the return value of; it would have caught none of the defects this
seam exists to catch — a POSIX-mode ``shlex`` silently eating the backslashes
out of a Windows path, or a shell metacharacter spawning a second command.

Committed rather than written into ``tmp_path`` for the same reason
``scripts/self_reliant/mock_trainer.py`` is: the tests need one stable,
reviewable definition of what a validation run looks like, and a script
generated inside a test can drift from the one the reader is looking at.

Modes:
  ok      print to BOTH streams and exit 0            (the passing run)
  fail    print to both streams and exit --exit-code  (the failing run)
  hang    print a line, FLUSH it, then sleep forever  (the timeout run —
          the flush is what makes 'partial output is still durable' testable)
  spew    print --bytes bytes of output and exit 0    (the artifact cap)
  args    print every argument verbatim, one per line (the argv round trip:
          this is how a mangled Windows path becomes visible)

``--marker PATH`` APPENDS one line to PATH before anything else. Two properties
ride on it and both are load-bearing:

  * a refused command leaves NO marker file at all — "the mock never ran" is
    then provable by absence rather than by inference;
  * the line COUNT is the number of processes that ran, so
    ``python mock.py ... ; python mock.py ...`` under ``shell=False`` is proved
    to have spawned exactly one child rather than two.

Unknown arguments are tolerated (``parse_known_args``) on purpose: the
shell-metacharacter test hands this script the tail of a second command as
plain arguments, and an argparse error there would exit before the marker was
written and make the two failure modes indistinguishable.
"""
import argparse
import sys
import time

_HANG_S = 600.0
MODES = ("ok", "fail", "hang", "spew", "args")


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--mode", default="ok", choices=MODES)
    ap.add_argument("--exit-code", type=int, default=3)
    ap.add_argument("--marker", default="")
    ap.add_argument("--bytes", type=int, default=0)
    args, extra = ap.parse_known_args()

    if args.marker:
        # Append, never truncate: the caller counts lines to count processes.
        with open(args.marker, "a", encoding="utf-8") as fh:
            fh.write("ran\n")

    if args.mode == "args":
        for a in sys.argv[1:]:
            print(a, flush=True)
        return 0

    if args.mode == "hang":
        print("mock_validator: started", flush=True)
        time.sleep(_HANG_S)
        return 0

    if args.mode == "spew":
        line = "x" * 79
        written = 0
        while written < args.bytes:
            print(line, flush=True)
            written += 80
        return 0

    print("mock_validator: stdout line", flush=True)
    print("mock_validator: stderr line", file=sys.stderr, flush=True)
    if args.mode == "fail":
        return args.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
