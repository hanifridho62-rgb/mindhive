# SPDX-License-Identifier: Apache-2.0
# src/mindhive/mcp_server.py
"""The MCP adapter over the four MindHive event tools — stdio, and thin.

**Transport: stdio.** The host is Claude Code on the same machine. stdio needs no
port, no bind address, no token and no TLS, and is reachable only by the parent
process that spawned it. HTTP/SSE would put a second unauthenticated local port
carrying an SQL surface on this machine, which the requirements already ruled out
for the dashboard; rejecting it here is that same decision applied consistently
rather than a new one.

**Thin, by rule.** No SQL, no caps, no shaping. Those live in `pgtools`, and a
second copy here would drift from the first silently — the model would then be
told about whichever set the description happened to quote. This module maps a
tool name onto `pgtools.TOOL_FUNCTIONS`, serialises the envelope, and gets out of
the way. A test asserts the absence.

Three properties hold this together:

1. **The tool list is answerable with everything down.** `pgschema` is stdlib
   only, and the `mcp` SDK is imported lazily inside `main()`. So the four tools
   list with no PostgreSQL, no `psycopg`, and no MCP SDK — a tool that vanishes
   when the backend is unreachable pushes the model back to inventing shell
   commands, which is the behaviour this feature exists to replace.
2. **No exception escapes a handler.** One inside an agent's turn is an outage,
   and the host renders it as the tool being broken rather than the backend
   being down. Every call ends in the structured envelope, with `isError` so the
   host still marks the failure while the model reads a code it has a rule for.
3. **Nothing but MCP frames reaches stdout.** A stray write corrupts the JSON-RPC
   frame stream and takes the server down mid-turn. Logging is stderr — the same
   rule `pre_tool_use.ps1`, `user_prompt_submit.ps1` and `stop.ps1` already live
   under — and a test scans this module's AST for stray writes.

## What is verified, and how `main()` shipped broken anyway

A real host now drives this over stdio in the test suite
(`test_a_real_host_can_initialise_list_and_call_over_stdio`): spawn, initialize,
`tools/list` returning the seven, `tools/call` returning an envelope. It needs no
Postgres — with no DSN the call answers `backend_unavailable`, which still proves
the loop.

That test exists because the loop was broken and nothing could see it. `main()`
registered its handlers with `@server.list_tools()` / `@server.call_tool()`, the
mcp 1.x decorator API; the installed SDK (2.0) takes `on_list_tools` /
`on_call_tool` constructor callables, so `hive pg mcp` died with AttributeError
on startup, every time. Thirty-seven tests passed throughout, because every one
of them stopped at `handle_call` — the right boundary for argument handling and
error shape, and the wrong one for "does the server start".

The lesson is narrow and worth keeping: a seam chosen so tests need no
dependency will not test the dependency. The only thing that catches an SDK API
change is running the SDK.
"""
import json
import logging
import sys

from . import pgschema

# stderr, always. `basicConfig` is deliberately NOT called here: it would
# configure the root logger for whatever process imports this module, and one of
# those is the test suite.
_log = logging.getLogger("mindhive.mcp_server")

SERVER_NAME = "mindhive-events"


def _require_mcp():
    """Raise with the install command if the MCP extra is missing.

    A SECOND guard rather than a shared one with `pgstore._require_pg`, because
    the two fail at different times and want different messages: telling somebody
    to install `.[pg]` when the SDK is what is absent costs a debugging session.
    Same shape as `vectors._require_deps`."""
    try:
        import mcp  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "the MCP server needs extras: pip install -e .[mcp]") from e


def sdk_available() -> bool:
    """True when the MCP SDK can be imported. Never raises — the tool list must
    be answerable either way."""
    try:
        import mcp  # noqa: F401
    except Exception:
        return False
    return True


# --- the registry -------------------------------------------------------------

def tool_definitions() -> list[dict]:
    """The four tools as plain dicts: `{name, description, inputSchema}`.

    Plain dicts rather than `mcp.types.Tool`, and that is the load-bearing
    choice: this must be answerable with the SDK absent, and it is also the
    structure `hive pg tool --list` prints, so one function serves the model and
    the operator. `main()` converts to the SDK's type at the boundary, where the
    SDK is guaranteed present.

    Sorted by name so the list is stable across runs — an ordering that moved
    would show up as a diff in every host that caches tool lists."""
    return [{"name": name,
             "description": pgschema.TOOL_DESCRIPTIONS[name],
             "inputSchema": pgschema.TOOL_SCHEMAS[name]}
            for name in sorted(pgschema.TOOL_SCHEMAS)]


# --- the call path ------------------------------------------------------------

def handle_call(name, arguments) -> tuple[dict, bool]:
    """`(envelope, is_error)` for one tool call. NEVER raises.

    `pgtools` is imported HERE, per call, not at module scope: the tool list must
    be answerable when `psycopg` is absent, and `pgtools` reaches `pgstore`.

    `is_error` is `not envelope["ok"]`, so the host renders a failure as a
    failure while the model still receives the structured code — the two are not
    alternatives. A handler that returned `ok:false` with `isError:false` would
    let a host render a backend outage as a successful answer."""
    try:
        from . import pgtools
        envelope = pgtools.call(name, arguments)
    except Exception as e:                    # pragma: no cover - last resort
        # `pgtools.call` has its own blanket catch; this one covers the import
        # itself and anything that could go wrong before that catch is in scope.
        _log.warning("tool %s failed outside the tool boundary: %s", name, e)
        envelope = {"ok": False, "tool": name, "rows": [], "row_count": 0,
                    "truncated": {"rows": False, "bytes": False,
                                  "payload": False},
                    "freshness": {"available": False},
                    "content_is_untrusted": True,
                    "error": {"code": "db_error", "retryable": False,
                              "detail": f"{type(e).__name__}: {e}",
                              "remedy": "report this; the adapter caught a "
                                        "failure rather than letting it escape"}}
    if not envelope.get("ok"):
        _log.warning("tool %s -> %s", name,
                     (envelope.get("error") or {}).get("code"))
    return envelope, not envelope.get("ok", False)


def call_text(name, arguments) -> str:
    """The tool result as the JSON text that rides the frame.

    The envelope ITSELF, not prose about it: the descriptions instruct the model
    to read `truncated`, `freshness` and `error.code`, and wrapping the result in
    a sentence would strip exactly those keys."""
    envelope, _ = handle_call(name, arguments)
    return json.dumps(envelope, ensure_ascii=False, default=str)


# --- the server ---------------------------------------------------------------

def main(argv=None) -> int:
    """Run the stdio MCP server. `hive pg mcp` and `python -m
    mindhive.mcp_server` are two spellings of this one body.

    `python -m` rather than the `hive` console script is the documented launch:
    on Windows the script shim can go stale against an editable install, and
    `-m` cannot.

    The breaker policy is switched to `"timed"` before anything else. This
    process is LONG-LIVED, and the hook path's one-strike default here would mean
    a single transient blip disables all four tools until an operator notices and
    restarts the server.

    Everything below `_require_mcp` is exercised by the stdio handshake test."""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    _require_mcp()

    import anyio
    import mcp.types as types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    from . import pgstore
    pgstore.set_breaker_policy("timed", window=30.0)

    # CONSTRUCTOR registration, not decorators. `@server.list_tools()` is the
    # 1.x API and raises AttributeError on the installed SDK (mcp 2.0.0); the
    # low-level `Server` now takes `on_list_tools` / `on_call_tool` callables.
    # Both handlers take `(ctx, params)` and return a Result object rather than
    # a bare list. This was written against the old shape and CRASHED ON START —
    # found by driving the server over stdio, not by any test in the suite,
    # because everything below `main()` is what the tests stop short of.
    async def _list_tools(ctx, params=None):
        return types.ListToolsResult(
            tools=[types.Tool(name=t["name"], description=t["description"],
                              inputSchema=t["inputSchema"])
                   for t in tool_definitions()])

    async def _call_tool(ctx, params):
        name, arguments = params.name, (params.arguments or {})
        # SYNCHRONOUS work inside an async handler, deliberately and with a
        # bound. `call_text` blocks the event loop for the length of one query,
        # which for a server whose transport is one stdio pipe and whose client
        # is one agent is the same wall clock either way — there is no second
        # request to serve meanwhile. The bound that makes it safe is not here:
        # `statement_timeout = 5s` is set on the `mindhive_reader` ROLE, so a
        # runaway query is cut off by the server rather than by hope, and
        # `connect_timeout` bounds the other half. A thread offload would buy
        # concurrency this transport cannot use, at the cost of a second failure
        # mode inside the agent's turn.
        text = call_text(name, arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)])

    server = Server(SERVER_NAME, on_list_tools=_list_tools,
                    on_call_tool=_call_tool)

    async def _serve():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(_serve)
    return 0


if __name__ == "__main__":                      # pragma: no cover
    raise SystemExit(main())
