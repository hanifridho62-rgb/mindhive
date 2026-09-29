# SPDX-License-Identifier: Apache-2.0
"""The command-module contract (blueprint P9b, MEB CLI-01), in three names.

`CommandModule` is the `typing.Protocol` every `commands/parsers/<domain>.py`
satisfies: one `register_parsers(sub)` that mounts the domain's commands in
the relative order `_build_parser` gives them. `_build_parser` itself still
names each `add_<command>` call, because the surface order it is pinned to
interleaves domains (`replenish` sits between `log` and `sop`, `loop` between
`autosync` and `flag`) and a per-domain call cannot reproduce that. So the
Protocol is the domain's own contract -- the whole domain, nothing left out,
in order -- for mounting ONE domain alone, and `tests/test_commands_base.py`
checks each module's `register_parsers` against the live tree, leaf by leaf
and handler by identity.

`COMMAND_DISPATCH` is the name -> handler map. It starts EMPTY, so importing
this module costs nothing; `load_dispatch()` fills it once from
`commands.dispatch_table()` -- the same function objects the parser binds --
keeping whatever `register_handler` put there first. `register_handler(name)`
is the decorator for a handler that lives OUTSIDE `HANDLER_MODULES` (the
self-registering groups: `bench`, `pgcli`, `consensus.cli`, the `storage`
CLI of P1i), so the map can name every handler the CLI reaches and not only
the ones `__main__` star-imports. The key is the function's own `__name__`,
exactly as `dispatch_table()` keys by attribute name, and a name may not be
re-bound to a different object: two objects under one handler name is a leaf
wired to a copy, the drift the map exists to expose.

Not a handler module: it exports no `cmd_*`, is not in `HANDLER_MODULES`,
and `__main__` does not star-import it.
"""
from typing import Callable, Protocol, runtime_checkable

__all__ = ["CommandModule", "COMMAND_DISPATCH", "load_dispatch", "register_handler"]


@runtime_checkable
class CommandModule(Protocol):
    """A domain's parser module: `register_parsers(sub)` mounts every one of
    its top-level commands onto *sub*, the root subparser set, in the
    relative order the live surface gives them."""

    def register_parsers(self, sub) -> None: ...


COMMAND_DISPATCH: dict[str, Callable] = {}
_LOADED = False


def load_dispatch() -> dict:
    """Fill `COMMAND_DISPATCH` from `dispatch_table()` on the first call and
    return it; every later call returns the same dict untouched. An entry the
    decorator bound earlier is kept, and one that disagrees with the table by
    identity raises -- see `_bind`."""
    global _LOADED
    if not _LOADED:
        from . import dispatch_table
        for name, fn in dispatch_table().items():
            _bind(name, fn)
        _LOADED = True
    return COMMAND_DISPATCH


def register_handler(name: str):
    """Decorator: enter a `cmd_*` handler defined outside `HANDLER_MODULES`
    into `COMMAND_DISPATCH` under *name*, which must be the function's own
    `__name__`. Refuses a name that is not `cmd_<verb>`, a function of
    another name, and a re-binding to a different object; binding the same
    object twice is a no-op."""
    if not isinstance(name, str) or not name.startswith("cmd_") or name == "cmd_":
        raise ValueError(f"a handler name is 'cmd_<verb>', got {name!r}")

    def bind(fn):
        if getattr(fn, "__name__", None) != name:
            raise ValueError(
                f"register_handler({name!r}) applied to "
                f"{getattr(fn, '__name__', fn)!r}: the key is the function's own name")
        _bind(name, fn)
        return fn
    return bind


def _bind(name: str, fn) -> None:
    cur = COMMAND_DISPATCH.get(name)
    if cur is not None and cur is not fn:
        raise ValueError(
            f"handler {name!r} is already bound to {cur.__module__}.{cur.__qualname__}; "
            f"refusing {fn.__module__}.{fn.__qualname__}")
    COMMAND_DISPATCH[name] = fn
