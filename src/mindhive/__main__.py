# SPDX-License-Identifier: Apache-2.0
"""`hive` console entry point (`python -m mindhive`, `hive`).

A shim. The handlers live in `mindhive.commands.<domain>` and the argparse
tree in `mindhive.commands.parsers`; this module re-exports every handler
BY IDENTITY (star-import over each module's `__all__`), so
`mindhive.__main__.cmd_hook is mindhive.commands.ledger.cmd_hook` and every
existing `from mindhive import __main__ as cli_mod` keeps resolving. Keep it
under 90 lines: `tests/test_commands_reexport.py` pins that, and a handler
added here instead of in `commands/` is the regression it exists to catch.

Only the names in each module's `__all__` cross the star-import. The handler
modules' own imports (`events`, `paths`, `os`, ...) are NOT attributes of
this module, so a test that patches one must patch it on
`mindhive.commands.<domain>`, not here.
"""
import json
import sys

from .commands.ledger import *  # noqa: F401,F403
from .commands.context_ops import *  # noqa: F401,F403
from .commands.maintenance import *  # noqa: F401,F403
from .commands.registry_sop import *  # noqa: F401,F403
from .commands.registry_roadmap import *  # noqa: F401,F403
from .commands.registry_rules import *  # noqa: F401,F403
from .commands.dashboard import *  # noqa: F401,F403
from .commands.skills import *  # noqa: F401,F403
from .commands.mindnet_tasks import *  # noqa: F401,F403
from .commands.mindnet_satisfy import *  # noqa: F401,F403
from .commands.mindnet_view import *  # noqa: F401,F403
from .commands.mindnet_orchestrate import *  # noqa: F401,F403
from .commands.verification import *  # noqa: F401,F403
from .commands.orchestration import *  # noqa: F401,F403
from .commands.parsers import _build_parser, _closest_command  # noqa: F401

build_cli_parser = _build_parser


def main(argv=None) -> int:
    """CLI entry point. read_all() is now TOTAL — it skips malformed/torn rows so
    a single corrupt line can never crash a `hive` command (it used to: one torn
    write took down every CLI command AND every dashboard section). Corruption is
    surfaced explicitly by `hive verify` (corrupt_rows + any hash mismatch), not
    by crashing readers. This catch stays as defense-in-depth for any other stray
    json.loads on a command path."""
    try:
        return _run(argv)
    except json.JSONDecodeError as e:
        print(f"error: corrupt event log ({e})", file=sys.stderr)
        print("       run `hive verify` to locate the corrupt line(s).",
              file=sys.stderr)
        return 1

def _run(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    rc = args.func(args)
    return rc if rc is not None else 0

if __name__ == "__main__":
    sys.exit(main())
