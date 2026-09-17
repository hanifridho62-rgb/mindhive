# SPDX-License-Identifier: Apache-2.0
"""Verification handlers (blueprint P9b, MEB CLI-02): `verify`, `selftest`,
and the programmatic strict form `cmd_verify_chain`.

`cmd_selftest` moved here from `maintenance.py` byte for byte. `cmd_verify`
moved with it and grew two flags on top of an UNCHANGED default: with neither
flag it prints `json.dumps` of the verdict and exits 0 whatever the verdict
says, exactly as before -- the dashboard poll and the hooks read its stdout,
never its exit code, and a corrupt row is surfaced there, not by crashing.
`--json` prints the sorted, compact one-line form instead (keys sorted, no
spaces), a shape a script can diff across two runs or hand to `jq`.
`--strict` returns 1 when the chain is not `ok` or a corrupt row exists,
which no spelling of `hive verify` could say before. `cmd_verify_chain` is
`verify --json --strict` as one function, for `ci/` (P1g) and the migration
engine (P1i) to call without composing flags.
"""
import argparse
import json

from .. import (events,
                chain,
                selftest as selftest_mod)

__all__ = ["cmd_verify", "cmd_selftest", "cmd_verify_chain"]


def cmd_verify(args) -> int | None:
    # Chain integrity is the tamper-evidence check; corrupt_rows is the loud,
    # explicit surfacing of any torn/malformed rows that read_all() skips to stay
    # total. Reporting both here is where "fail loud" now lives — instead of
    # crashing every reader on a single bad line.
    result = chain.verify()
    bad = events.corrupt_rows()
    if bad:
        result = {**result, "corrupt_rows": bad}
    if getattr(args, "json", False):
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(result))
    if getattr(args, "strict", False) and not (result.get("ok") is True and not bad):
        return 1
    return None


def cmd_selftest(args) -> int | None:
    # The exit code IS the command's contract; `run` returns the whole result
    # because the dashboard's job reports the counts too.
    return selftest_mod.run()["exit_code"]


def cmd_verify_chain(args) -> int | None:
    """`hive verify --json --strict` as one call: the sorted one-line verdict
    on stdout, 1 when the chain is broken or a row is corrupt, else None
    (exit 0). Whatever else *args* carries passes through untouched; `None`
    is accepted for a caller that has no namespace to hand over."""
    carried = vars(args) if args is not None else {}
    return cmd_verify(argparse.Namespace(**{**carried, "json": True, "strict": True}))
