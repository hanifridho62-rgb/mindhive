# SPDX-License-Identifier: Apache-2.0
"""`mindhive.server` — the ephemeral UI's own serving parts (blueprint P1k).

**This package imports nothing at module scope, and that is load-bearing.**
`server.lifecycle` is a clock and a counter; `server.routes` needs a handler
class; `server.app` starts an HTTP server and opens a browser; `server.asgi`
wants extras that may not be installed. A package `__init__` that reached for
any of them would make every one of those costs unconditional -- including for
`hive --help`, which builds a parser that names `cmd_ui`.

The same rule as `mindhive.core.__init__`, for the same reason, written down
in both places because the next person to add a convenience re-export will
read whichever one they are standing in.
"""
__all__ = []
