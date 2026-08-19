# tests/test_mcp_server.py
"""The MCP adapter, tested at exactly the depth the absent SDK allows.

`mcp` is not installed on this machine and there is no PostgreSQL server. That
rules out driving a real in-process client. It rules out nothing about the three
properties that decide whether this server is safe to attach to a live agent:

* **The tool list is answerable with everything down.** A tool that vanishes
  when the backend is unreachable pushes the model back to inventing shell
  commands, which is the behaviour this feature exists to replace. So the
  registry is built from `pgschema` — stdlib-only — and importing this module
  must not import `mcp` or `psycopg`.
* **Every call fails structurally, fast, and without raising.** This is the free
  verification the architecture flags (R4/S10): with no server running, all four
  tools must return `ok:false, code:backend_unavailable` inside a 3-second wall
  clock and nothing may escape into the handler. It is *only* honestly available
  while no server exists, so it is taken now.
* **Nothing but MCP frames reaches stdout.** A stray `print` corrupts the
  JSON-RPC frame stream and takes the whole server down mid-turn — the same rule
  `pre_tool_use.ps1` / `stop.ps1` already live under.

What is NOT proven here: that the `mcp` SDK accepts these structures, that the
stdio transport frames them correctly, or that a real host renders `isError` the
way the design assumes. Those need the SDK installed and are the `mcp`-marked
work, not this.
"""
import ast
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mindhive import mcp_server, pgschema, pgstore, pgtools

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "mindhive"
TOOLS = ("query_database", "get_recent_events", "search_events_by_payload",
         "get_session_summary", "get_latest_event", "search_events_by_type",
         "get_current_state", "search_events_by_meaning")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MINDHIVE_PG_DSN_RO", raising=False)
    pgstore.reset_state()
    yield
    pgstore.reset_state()


def _args(name):
    return {"query_database": {"sql": "SELECT 1"},
            "get_recent_events": {"session_id": "s1"},
            "search_events_by_payload": {"key": "task_id"},
            "get_session_summary": {"session_id": "s1"},
            "get_latest_event": {"session_id": "s1"},
            "search_events_by_type": {"event_type": "decision"},
            "get_current_state": {},
            "search_events_by_meaning": {"query": "mirror staleness"}}[name]


# --------------------------------------------------------------------------
# the registry — answerable with the backend absent
# --------------------------------------------------------------------------

def test_the_server_lists_exactly_the_published_tools():
    listed = mcp_server.tool_definitions()
    assert [t["name"] for t in listed] == sorted(TOOLS), \
        "the tool list is not the published set"
    from mindhive import pgtools
    assert set(TOOLS) == set(pgtools.TOOL_FUNCTIONS), (
        "the server and the dispatch registry disagree about what exists")


def test_every_listed_tool_carries_a_description_and_an_input_schema():
    for tool in mcp_server.tool_definitions():
        assert tool["description"].strip(), tool["name"]
        assert tool["inputSchema"]["type"] == "object", tool["name"]


def test_every_description_contains_a_generated_schema_block_verbatim():
    """R6, description drift. The schema text is the model's ONLY map: if the
    DDL moves and the text does not, the model writes SQL against columns that
    no longer exist and cannot diagnose why. Splicing a RENDERED constant is what
    stops the descriptions from disagreeing with each other or with the DDL.

    TWO variants are legitimate; a third one written by hand is not.
    `query_database` and `search_events_by_meaning` can address any table and get
    the full map; the six targeted tools have a fixed statement over
    `mindhive_ro.events` and get the core map, which is ~2,000 characters shorter
    and spliced six times over. Both come out of `render_schema_doc` over the
    same `TABLES`, so this is a size decision, not a second source of truth."""
    for tool in mcp_server.tool_definitions():
        expected = (pgschema.SCHEMA_DOC
                    if tool["name"] in pgschema.FULL_MAP_TOOLS
                    else pgschema.SCHEMA_DOC_CORE)
        assert expected in tool["description"],             f"{tool['name']}'s description does not carry its schema block"


def test_the_schema_block_is_byte_identical_within_each_variant():
    """The de-duplication claim in the cost analysis only holds if the copies
    really are one string per variant. Two tools that each rendered their own
    would pass the test above and drift the moment one was edited."""
    blocks = {}
    for t in mcp_server.tool_definitions():
        text = t["description"]
        block = text[text.index("TABLE mindhive_ro.events"):
                     text.index('about "now".') + 12]
        blocks.setdefault(t["name"] in pgschema.FULL_MAP_TOOLS, set()).add(block)
    assert {k: len(v) for k, v in blocks.items()} == {True: 1, False: 1},         "a tool is carrying its own copy of the schema block"


def test_the_two_variants_really_are_different():
    """CANARY for the pair test: were `SCHEMA_DOC_CORE` accidentally the same
    string as `SCHEMA_DOC`, the split would be a no-op and the test above would
    still pass."""
    assert len(pgschema.SCHEMA_DOC_CORE) < len(pgschema.SCHEMA_DOC)
    assert "mindhive_ro.event_embeddings" in pgschema.SCHEMA_DOC
    assert "mindhive_ro.event_embeddings" not in pgschema.SCHEMA_DOC_CORE

def test_every_schema_refuses_a_hallucinated_parameter():
    """`additionalProperties: false` turns an invented argument into a loud
    rejection instead of a parameter the server silently ignores while answering
    a different question than the one the model asked."""
    for tool in mcp_server.tool_definitions():
        assert tool["inputSchema"]["additionalProperties"] is False, tool["name"]


def test_every_schema_publishes_the_bounds_it_enforces():
    """A bound the model cannot see is a bound it discovers as a rejection."""
    props = {t["name"]: t["inputSchema"]["properties"]
             for t in mcp_server.tool_definitions()}
    assert props["query_database"]["max_rows"]["maximum"] == pgtools.MAX_ROWS_CEILING
    assert props["query_database"]["timeout_ms"]["maximum"] == \
        pgtools.TIMEOUT_MS_CEILING


def test_the_registry_and_the_dispatch_table_hold_the_same_names():
    """One registry. `hive pg tool` and this server read the same table, so the
    human debug surface cannot drift from the AI one."""
    assert {t["name"] for t in mcp_server.tool_definitions()} == \
        set(pgtools.TOOL_FUNCTIONS)


# --------------------------------------------------------------------------
# the down-path — the verification only available while nothing is installed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", TOOLS)
def test_every_tool_fails_structurally_with_no_backend(name, monkeypatch):
    """S10's free gate. `deps_available` is forced True so this is about the
    SERVER being down rather than the driver being absent — on this machine both
    are true, and the unforced case is covered below."""
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    payload, is_error = mcp_server.handle_call(name, _args(name))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "backend_unavailable"
    assert is_error is True, "the host must render this as a tool failure"


@pytest.mark.parametrize("name", TOOLS)
def test_every_tool_returns_inside_the_wall_clock_budget(name, monkeypatch):
    """R4: a blocking connect inside a handler stalls an agent turn with no
    output and no error. The budget is 3 seconds, and with the breaker plus a
    2-second connect timeout it should be far under."""
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    start = time.monotonic()
    mcp_server.handle_call(name, _args(name))
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"{name} took {elapsed:.2f}s"


def test_every_tool_together_stays_inside_one_budget(monkeypatch):
    """The realistic shape: an agent calls several tools in one turn. Seven
    sequential 2-second connect timeouts would be 14 seconds of dead air; the
    breaker is what makes every call after the first free."""
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    start = time.monotonic()
    for name in TOOLS:
        mcp_server.handle_call(name, _args(name))
    assert time.monotonic() - start < 3.0


@pytest.mark.parametrize("name", TOOLS)
def test_no_exception_escapes_however_the_layer_beneath_fails(name, monkeypatch):
    """The blanket catch, and the reason it is deliberate: an exception reaching
    an MCP handler is an outage inside the agent's turn."""
    for boom in (RuntimeError("dead"), OSError("refused"), KeyError("k"),
                 ValueError("garbage")):
        monkeypatch.setattr(pgtools, "TOOL_FUNCTIONS",
                            {n: (lambda *a, e=boom, **k: (_ for _ in ()).throw(e))
                             for n in TOOLS})
        payload, is_error = mcp_server.handle_call(name, _args(name))
        assert is_error is True
        assert payload["ok"] is False
        assert payload["error"]["code"] in pgtools.ERROR_CODES


def test_an_unknown_tool_name_is_a_structured_refusal(monkeypatch):
    payload, is_error = mcp_server.handle_call("drop_everything", {})
    assert is_error is True
    assert payload["error"]["code"] == "bad_argument"
    assert "drop_everything" in payload["error"]["detail"]


def test_a_hallucinated_argument_is_refused_rather_than_ignored():
    """The second brace behind `additionalProperties: false`, for a host that
    does not validate: a silently dropped argument means the tool answers a
    different question than the one asked, and says nothing about it."""
    payload, is_error = mcp_server.handle_call(
        "get_recent_events", {"session_id": "s1", "ordr": "asc"})
    assert is_error is True
    assert payload["error"]["code"] == "bad_argument"


def test_the_response_is_json_serialisable(monkeypatch):
    """It crosses a JSON-RPC frame. A value `json.dumps` refuses takes the
    server down on the way out, after the work succeeded."""
    import json
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    for name in TOOLS:
        payload, _ = mcp_server.handle_call(name, _args(name))
        json.dumps(payload)


def test_the_text_frame_is_the_envelope_itself(monkeypatch):
    """The model reads the envelope. Wrapping it in prose, or returning only the
    error message, would strip the `truncated` and `freshness` keys the
    descriptions instruct the model to check."""
    import json
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    text = mcp_server.call_text("get_recent_events", {"session_id": "s1"})
    parsed = json.loads(text)
    assert parsed["tool"] == "get_recent_events"
    assert "freshness" in parsed and "truncated" in parsed


# --------------------------------------------------------------------------
# the optional dependency, and the stdout discipline
# --------------------------------------------------------------------------

def test_importing_the_server_imports_neither_the_sdk_nor_a_driver():
    """The whole reason the tool list is answerable with everything absent."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    out = subprocess.run(
        [sys.executable, "-c",
         "import mindhive.mcp_server, sys; "
         "print('mcp' in sys.modules, 'psycopg' in sys.modules)"],
        capture_output=True, text=True, cwd=str(REPO), env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False", out.stdout


def test_the_missing_sdk_names_its_own_extra_not_the_other_one():
    """Two guards rather than one, because they fail at different times and want
    different messages — `vectors._require_deps`'s precedent. Telling somebody
    to install `.[pg]` when the MCP SDK is what is missing costs a debugging
    session."""
    if mcp_server.sdk_available():
        pytest.skip("the mcp SDK is installed here; nothing to assert")
    with pytest.raises(RuntimeError) as e:
        mcp_server._require_mcp()
    assert ".[mcp]" in str(e.value)
    assert ".[pg]" not in str(e.value)


def test_main_refuses_with_the_install_command_rather_than_a_name_error():
    """What an operator actually sees when they run `python -m
    mindhive.mcp_server` today. A `NameError` or `ModuleNotFoundError` from deep
    inside would be a stack trace with no fix in it."""
    if mcp_server.sdk_available():
        pytest.skip("the mcp SDK is installed here")
    with pytest.raises(RuntimeError) as e:
        mcp_server.main()
    assert ".[mcp]" in str(e.value)


def test_nothing_in_this_module_writes_to_stdout():
    """**Nothing may print to stdout but MCP frames.** A stray write corrupts
    the frame stream and takes the server down mid-turn, and it does it in a way
    that looks like the host's fault. Logging is stderr, asserted over the AST
    rather than over a run, because the offending line is usually on a path a
    test never takes."""
    tree = ast.parse((SRC / "mcp_server.py").read_text(encoding="utf-8"))
    offences = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "print":
                offences.append(f"print() at line {node.lineno}")
            if isinstance(fn, ast.Attribute) and fn.attr == "write":
                target = fn.value
                if (isinstance(target, ast.Attribute)
                        and target.attr == "stdout"):
                    offences.append(f"stdout.write at line {node.lineno}")
    assert offences == [], offences


def test_the_stdout_scan_would_catch_one():
    """CANARY. The assertion above is an emptiness claim over an AST walk, and a
    walk that matched nothing would pass it forever."""
    planted = ast.parse("import sys\nprint('hi')\nsys.stdout.write('x')\n")
    hits = [n for n in ast.walk(planted) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == "print")
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == "write"))]
    assert len(hits) == 2


def test_the_module_holds_no_sql_and_no_caps():
    """Thin adapter, by rule. Shaping lives in `pgtools`; a second set of caps
    here would drift from the first one silently, and the model would be told
    about whichever the description happened to quote."""
    text = (SRC / "mcp_server.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("#"))
    for forbidden in ("SELECT ", "FROM mindhive", "max_rows =", "LIMIT "):
        assert forbidden not in code, f"mcp_server contains {forbidden!r}"


# --------------------------------------------------------------------------
# the stdio loop itself
# --------------------------------------------------------------------------
#
# Everything above stops at `handle_call`, which is the right boundary for
# argument handling and error shape — and is exactly why `main()` shipped
# broken. It registered handlers with `@server.list_tools()` / `@server.call_tool()`,
# the mcp 1.x decorator API; the installed SDK (2.0) takes `on_list_tools` /
# `on_call_tool` constructor callables and raises AttributeError on import of
# the decorator. `hive pg mcp` therefore died on startup, every time, and no
# test in this suite could see it.
#
# The only thing that can see it is a real host driving a real subprocess, so
# that is what this does. It needs no Postgres: with no DSN the call answers
# `backend_unavailable`, which still proves the loop — spawn, initialize,
# tools/list, tools/call, envelope back.

def _mcp_installed():
    import importlib.util
    return importlib.util.find_spec("mcp") is not None


@pytest.mark.skipif(not _mcp_installed(), reason="the mcp extra is not installed")
def test_a_real_host_can_initialise_list_and_call_over_stdio():
    import asyncio
    import json
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    # The worktree's own src, so the subprocess runs THIS checkout rather than
    # whatever the venv installed editable.
    env["PYTHONPATH"] = str(SRC.parent)

    async def drive():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "mindhive.mcp_server"],
                                       env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                called = await session.call_tool("get_current_state",
                                                 {"limit": 3})
                return (init.server_info.name,
                        sorted(t.name for t in listed.tools),
                        json.loads(called.content[0].text))

    name, names, envelope = asyncio.run(asyncio.wait_for(drive(), timeout=60))

    assert name == mcp_server.SERVER_NAME
    assert names == sorted(TOOLS), "the served list is not the published one"
    # The envelope, not the rows: with no server configured this is a structured
    # `backend_unavailable`, and that is still the loop working.
    assert set(("ok", "tool", "freshness")) <= set(envelope)
    assert envelope["tool"] == "get_current_state"
    if envelope["ok"] is False:
        assert envelope["error"]["code"] in pgtools.ERROR_CODES
