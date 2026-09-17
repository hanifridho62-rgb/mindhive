# SPDX-License-Identifier: Apache-2.0
"""The `hive` command handlers, one module per domain, and the argparse
wiring under `parsers/`. `mindhive.__main__` re-exports every name each
module lists in `__all__` BY IDENTITY, so `mindhive.__main__.cmd_x is
mindhive.commands.<module>.cmd_x` and tests that monkeypatch or
`inspect.getsource` a handler through `__main__` keep working.

Every module here is held under the blueprint's 500-line target by
`tests/test_commands_reexport.py`; a domain that outgrows it is split,
never allow-listed.
"""
import importlib

HANDLER_MODULES = (
    "ledger",
    "context_ops",
    "maintenance",
    "registry_sop",
    "registry_roadmap",
    "registry_rules",
    "dashboard",
    "skills",
    "mindnet_tasks",
    "mindnet_satisfy",
    "mindnet_view",
    "mindnet_orchestrate",
    "orchestration",
)
PARSER_MODULES = (
    "ledger",
    "maintenance",
    "registry",
    "mindnet",
    "dashboard",
    "orchestration",
)

__all__ = ["HANDLER_MODULES", "PARSER_MODULES", "dispatch_table"]


def dispatch_table() -> dict:
    """Every `cmd_*` handler by name -- the same function objects the parser
    binds with `set_defaults(func=...)`, so a parser leaf whose `func` is not
    a value here is wired to a copy, not to the handler."""
    out = {}
    for name in HANDLER_MODULES:
        mod = importlib.import_module(f"{__name__}.{name}")
        for attr in mod.__all__:
            if attr.startswith("cmd_"):
                out[attr] = getattr(mod, attr)
    return out
