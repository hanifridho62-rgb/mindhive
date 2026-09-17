# tests/test_pgcli.py
"""`hive pg` — the human half of the Postgres mirror surface.

The group exists for three reasons, and the tests below are shaped by them:

* **The admin half has to be a CLI anyway.** `pgmirror` holds the owner DSN and
  must never be reachable from the AI path, so `migrate` / `rebuild` / `parity`
  are human commands by necessity rather than preference.
* **An MCP-only surface is debuggable only through a model.** When a tool
  returns nothing, `hive pg tool <name> --arg k=v` gives the identical code path
  with a readable envelope in one process and one stack trace.
* **It must not become a second implementation.** `hive pg tool` dispatches
  through `pgtools.TOOL_FUNCTIONS` — the same table `mcp_server` reads — so the
  debug surface cannot drift from the AI one. A test asserts the module holds no
  SQL and no caps of its own; that is the drift to refuse in review, and this is
  the check that notices it.

Everything here runs with no PostgreSQL and no `psycopg`, which is the state
that matters most for a CLI: the commands that cannot work must fail with a
message naming the fix, not with a traceback.
"""
import ast
import json
from pathlib import Path

import pytest

from mindhive import __main__ as cli
from mindhive import pgcli, pgstore, pgtools

SRC = Path(__file__).resolve().parents[1] / "src" / "mindhive"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MINDHIVE_PG_DSN_RO", raising=False)
    monkeypatch.delenv("MINDHIVE_PG_DSN_ADMIN", raising=False)
    pgstore.reset_state()
    yield
    pgstore.reset_state()


def _run(*argv):
    return cli.main(list(argv))


# --------------------------------------------------------------------------
# registration — the second module to wire itself onto __main__
# --------------------------------------------------------------------------

def test_the_group_is_registered_on_the_real_parser():
    """`pgcli.register(sub)` is two lines in the parser module instead of ~180.
    `bench.register`'s docstring said that if a second module ever registered
    this way the deviation becomes the convention — this is that second
    module."""
    import argparse
    parser = cli._build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    assert "pg" in sub.choices


def test_the_group_offers_exactly_the_eight_documented_subcommands():
    """Not one subcommand per AI tool: `tool <name> --arg k=v` covers all eight
    of those and keeps the group small. A new verb here is a design change, not
    a refactor.

    `refresh` is a distinct verb rather than a flag on `rebuild` on purpose:
    `rebuild` reloads every segment and `refresh` reloads only what moved, so an
    operator repairing a stale mirror in the middle of a session should not have
    to remember which flag makes the cheap one happen.

    `embed` is the eighth, and it is admin-side for the same structural reason
    every other loader here is: it WRITES, so it needs the owner DSN, so it can
    never be a tool the model calls. The model gets to search the index, not to
    build it."""
    import argparse
    parser = cli._build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    pg = sub.choices["pg"]
    inner = next(a for a in pg._actions
                 if isinstance(a, argparse._SubParsersAction))
    assert sorted(inner.choices) == ["embed", "mcp", "migrate", "parity",
                                     "rebuild", "refresh", "status", "tool"]


def test_the_group_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        _run("pg", "--help")
    assert e.value.code == 0
    assert "status" in capsys.readouterr().out


def test_the_group_with_no_subcommand_is_an_argparse_error():
    """`required=True` on the inner subparsers. A bare `hive pg` that silently
    did nothing would look like a command that ran."""
    with pytest.raises(SystemExit) as e:
        _run("pg")
    assert e.value.code == 2


# --------------------------------------------------------------------------
# status — the surface that makes degradation visible on demand
# --------------------------------------------------------------------------

def test_status_reports_the_mirror_as_unavailable_and_says_why(capsys):
    rc = _run("pg", "status", "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["available"] is False
    assert out["reason"] in pgstore.UNAVAILABLE_REASONS
    assert rc != 0, "an unavailable mirror is not a healthy exit"


def test_status_prints_the_failure_counters(capsys):
    """The defect the requirements name is a dead import degrading QUIETLY.
    Quiet on the hot path is right; invisible is not, and this is where the
    counter becomes visible."""
    _run("pg", "status", "--json")
    out = json.loads(capsys.readouterr().out)
    assert "failures" in out and "breaker" in out


def test_status_lists_the_migration_inventory(capsys):
    """The inventory needs no server, so `status` can answer it even here — and
    a zero-length inventory is exactly the packaging failure that would make
    `apply_migrations` silently do nothing."""
    _run("pg", "status", "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["migrations"] == [
        "001_schema.sql", "002_indexes.sql", "003_roles_and_views.sql",
        "004_mirror_state_view_and_ownership.sql",
        "005_drop_unmatchable_order_index.sql",
        "006_event_embeddings.sql"]


def test_status_without_json_prints_readable_lines(capsys):
    _run("pg", "status")
    text = capsys.readouterr().out
    assert "mirror" in text.lower()
    assert "MINDHIVE_PG_DSN_RO" in text, \
        "an operator cannot act on a status that never names the variable"


# --------------------------------------------------------------------------
# the admin commands — they must refuse, not pretend
# --------------------------------------------------------------------------

def test_migrate_refuses_and_names_the_variable_it_needs(capsys):
    rc = _run("pg", "migrate")
    err = capsys.readouterr().err
    assert rc == 1
    assert "MINDHIVE_PG_DSN_ADMIN" in err


def test_rebuild_refuses_rather_than_reporting_a_rebuild_that_did_not_happen(
        capsys):
    """A skeleton that answered `{"ok": true}` would tell an operator a mirror
    was rebuilt that does not exist, and the parity gate would then be checking
    a claim instead of a database."""
    rc = _run("pg", "rebuild")
    assert rc == 1
    assert "MINDHIVE_PG_DSN_ADMIN" in capsys.readouterr().err


def test_parity_answers_a_verdict_instead_of_raising(capsys):
    """"I could not check" is a verdict a gate must be able to give — and it
    must not be `ok`. A zero exit here is how a green pipeline comes to mean
    nothing."""
    rc = _run("pg", "parity", "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["reason"]
    assert rc == 1


def test_no_admin_command_prints_a_traceback(capsys):
    """An operator running `hive pg migrate` on an unconfigured machine should
    read one sentence, not a stack."""
    for cmd in ("migrate", "rebuild"):
        _run("pg", cmd)
        assert "Traceback" not in capsys.readouterr().err, cmd


# --------------------------------------------------------------------------
# `hive pg tool` — the same dispatch the model uses
# --------------------------------------------------------------------------

def test_tool_list_prints_the_four_names(capsys):
    rc = _run("pg", "tool", "--list")
    out = capsys.readouterr().out
    for name in pgtools.TOOL_FUNCTIONS:
        assert name in out
    assert rc in (0, None)


def test_tool_prints_the_envelope_the_model_would_receive(capsys):
    rc = _run("pg", "tool", "get_recent_events", "--arg", "session_id=s1",
              "--json")
    env = json.loads(capsys.readouterr().out)
    assert env["tool"] == "get_recent_events"
    assert env["ok"] is False
    assert env["error"]["code"] in pgtools.ERROR_CODES
    assert "freshness" in env and "truncated" in env
    assert rc == 1, "a failed tool call is a failed command"


def test_tool_arguments_keep_their_json_types(capsys):
    """`limit=50` must arrive as an integer, not the string "50" — the bound
    check compares against integers, and a string would be rejected as a bad
    argument for a reason that has nothing to do with what the operator typed."""
    parsed = pgcli._parse_args(["limit=50", "key=task_id", "value=true",
                                "nested={\"a\": 1}"])
    assert parsed == {"limit": 50, "key": "task_id", "value": True,
                      "nested": {"a": 1}}


def test_a_bare_word_stays_a_string_rather_than_failing_to_parse():
    """`session_id=manual` is not JSON. Treating a parse failure as a string is
    what makes the common case typeable without quoting."""
    assert pgcli._parse_args(["session_id=manual"]) == {"session_id": "manual"}


def test_an_argument_without_an_equals_sign_is_refused(capsys):
    rc = _run("pg", "tool", "get_recent_events", "--arg", "session_id")
    assert rc == 2
    assert "k=v" in capsys.readouterr().err


def test_an_unknown_tool_name_is_refused_by_the_parser(capsys):
    """The four names are argparse `choices`, so a typo is caught before any
    dispatch and the error lists what does exist."""
    with pytest.raises(SystemExit) as e:
        _run("pg", "tool", "drop_everything")
    assert e.value.code == 2


# --------------------------------------------------------------------------
# thin printer, not a second implementation
# --------------------------------------------------------------------------

def test_the_cli_holds_no_sql_and_no_caps_of_its_own():
    """The drift to refuse in review, made mechanical. If `pgcli` ever grows its
    own query or its own cap, the human surface and the AI surface stop being
    the same code path and the debug value evaporates."""
    text = (SRC / "pgcli.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("#"))
    for forbidden in ("SELECT ", "FROM mindhive", "max_rows =", "LIMIT ",
                      "jsonb"):
        assert forbidden not in code, f"pgcli contains {forbidden!r}"


def test_the_cli_dispatches_through_the_shared_registry():
    """Asserted structurally: `hive pg tool` must reach `pgtools.call` or
    `TOOL_FUNCTIONS`, not a private copy of the four functions."""
    src = (SRC / "pgcli.py").read_text(encoding="utf-8")
    assert "pgtools.call" in src or "TOOL_FUNCTIONS" in src


def test_importing_the_cli_module_costs_no_driver():
    """`__main__` imports `pgcli` at parser-build time, which every `hive`
    command pays. It must not pull `psycopg` or the MCP SDK in with it."""
    import subprocess
    import sys
    import os
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo / "src"))
    out = subprocess.run(
        [sys.executable, "-c",
         "import mindhive.pgcli, sys; "
         "print('psycopg' in sys.modules, 'mcp' in sys.modules)"],
        capture_output=True, text=True, cwd=str(repo), env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False", out.stdout


def test_the_registration_is_two_lines_in_main():
    """The whole point of the `register(sub)` convention, and the reason the
    parser module grows by 2 lines instead of ~180. That module was
    `__main__.py` (and its ratchet) when this was written; the P9 split moved
    `_build_parser` to `commands/parsers/__init__.py`, so the scan follows the
    function to its file. Asserted so a later edit that inlines the argparse
    block shows up as a failure rather than as a quiet ceiling raise."""
    import inspect
    src = Path(inspect.getsourcefile(cli._build_parser)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    build = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_build_parser")
    mentions = [n for n in ast.walk(build)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "register"]
    assert len(mentions) >= 2, "bench and pg both register themselves"
    pg_lines = [line for line in src.splitlines() if "pgcli" in line]
    assert len(pg_lines) == 2, f"pgcli is named on {len(pg_lines)} lines: {pg_lines}"
