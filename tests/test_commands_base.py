# tests/test_commands_base.py
"""Blueprint P9b (MEB CLI-01..03): `commands/base.py` -- the `CommandModule`
Protocol, `COMMAND_DISPATCH` + `load_dispatch()`, `register_handler` -- and
`commands/verification.py`, where `verify` and `selftest` now live, with
`verify --json`, `verify --strict` and the programmatic `cmd_verify_chain`.

`tests/test_commands_reexport.py` keeps the surface, its order and the
handler identities pinned; this file pins what P9b added on top.
"""
import argparse
import importlib
import inspect
import json
import os
import subprocess
import sys
import typing
from pathlib import Path

import pytest

from mindhive import __main__ as cli_mod, chain, commands, events, paths
from mindhive.commands import base, maintenance, verification

REPO = Path(__file__).resolve().parents[1]


def _sub(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _leaves(parser, path=()):
    """(path, func) for every parser that binds a handler -- the walk
    `tests/test_commands_reexport.py` uses, so the two files see one tree."""
    func = parser.get_default("func")
    if func is not None:
        yield path, func
    sub = _sub(parser)
    if sub is not None:
        for name, child in sub.choices.items():
            yield from _leaves(child, path + (name,))


def _root():
    p = argparse.ArgumentParser(prog="hive")
    return p, p.add_subparsers(dest="cmd", required=True)


def _parser_module(name):
    return importlib.import_module(f"mindhive.commands.parsers.{name}")


def _child(code: str) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run([sys.executable, "-W", "error", "-c", code],
                         capture_output=True, text=True, env=env, cwd=str(REPO))
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _seal_one_row():
    events.append_event("decision", {"text": "a"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    chain.write_chain()
    return next(paths.events_dir().glob("*.jsonl"))


# --- COMMAND_DISPATCH ---------------------------------------------------------------

def test_command_dispatch_is_the_dispatch_table_by_identity():
    got = base.load_dispatch()
    assert got is base.COMMAND_DISPATCH
    assert base.load_dispatch() is got               # a second call is a lookup
    table = commands.dispatch_table()
    assert len(table) >= 100        # the floor `tests/test_commands_reexport.py` uses
    assert set(got) == set(table)
    for name, fn in table.items():
        assert got[name] is fn, name


def test_importing_base_is_free_and_the_map_starts_empty():
    """`COMMAND_DISPATCH` is filled by `load_dispatch()`, never at import: a
    cold `import mindhive.commands.base` loads no handler module."""
    loaded = _child("import sys, mindhive.commands.base as b; "
                    "print(len(b.COMMAND_DISPATCH), "
                    "sorted(m for m in sys.modules if m.startswith('mindhive')))")
    assert loaded == "0 ['mindhive', 'mindhive.commands', 'mindhive.commands.base']"


def test_load_dispatch_keeps_what_the_decorator_bound_first():
    def cmd_probe_kept(args):
        return 0
    base.COMMAND_DISPATCH.pop("cmd_probe_kept", None)
    try:
        assert base.register_handler("cmd_probe_kept")(cmd_probe_kept) is cmd_probe_kept
        assert base.load_dispatch()["cmd_probe_kept"] is cmd_probe_kept
        assert base.load_dispatch()["cmd_verify"] is verification.cmd_verify
        # binding the same object again is a no-op, not a conflict
        base.register_handler("cmd_probe_kept")(cmd_probe_kept)
    finally:
        base.COMMAND_DISPATCH.pop("cmd_probe_kept", None)


@pytest.mark.parametrize("name", ["verify", "cmd_", "", "CMD_verify", 3, None],
                         ids=repr)
def test_register_handler_refuses_a_name_that_is_not_cmd_verb(name):
    with pytest.raises(ValueError, match="cmd_"):
        base.register_handler(name)


def test_register_handler_refuses_a_function_of_another_name():
    def cmd_other(args):
        return 0
    with pytest.raises(ValueError, match="function's own name"):
        base.register_handler("cmd_probe_named")(cmd_other)
    with pytest.raises(ValueError, match="function's own name"):
        base.register_handler("cmd_probe_named")(lambda args: 0)
    assert "cmd_probe_named" not in base.COMMAND_DISPATCH


def test_a_name_cannot_be_rebound_to_a_different_object():
    """The drift the map exists to expose: two objects under one handler name
    is a leaf wired to a copy. A table entry is protected the same way."""
    base.load_dispatch()

    def cmd_verify(args):
        return 0
    with pytest.raises(ValueError, match="mindhive.commands.verification.cmd_verify"):
        base.register_handler("cmd_verify")(cmd_verify)
    assert base.COMMAND_DISPATCH["cmd_verify"] is verification.cmd_verify

    def cmd_probe_twice(args):
        return 0

    def other(args):
        return 1
    other.__name__ = "cmd_probe_twice"
    base.COMMAND_DISPATCH.pop("cmd_probe_twice", None)
    try:
        base.register_handler("cmd_probe_twice")(cmd_probe_twice)
        with pytest.raises(ValueError, match="already bound"):
            base.register_handler("cmd_probe_twice")(other)
        assert base.COMMAND_DISPATCH["cmd_probe_twice"] is cmd_probe_twice
    finally:
        base.COMMAND_DISPATCH.pop("cmd_probe_twice", None)


# --- CommandModule ------------------------------------------------------------------

def test_command_module_is_a_runtime_checkable_protocol_every_parser_module_satisfies():
    assert typing.Protocol in base.CommandModule.__mro__
    assert getattr(base.CommandModule, "_is_runtime_protocol", False) is True
    for name in commands.PARSER_MODULES:
        mod = _parser_module(name)
        assert isinstance(mod, base.CommandModule), name
        assert "register_parsers" in mod.__all__, name
        assert callable(mod.register_parsers)
    assert not isinstance(argparse, base.CommandModule)
    assert not isinstance(maintenance, base.CommandModule)   # a handler module is not one


@pytest.mark.parametrize("name", commands.PARSER_MODULES)
def test_register_parsers_mounts_the_whole_domain_in_the_live_order(name):
    mod = _parser_module(name)
    mine_parser, sub = _root()
    mod.register_parsers(sub)
    mine = list(sub.choices)
    assert mine, name
    # nothing left out: every `add_*` the module exports contributed
    expected = set()
    for attr in mod.__all__:
        if attr.startswith("add_"):
            _, scratch = _root()
            getattr(mod, attr)(scratch)
            expected |= set(scratch.choices)
    assert set(mine) == expected, name
    # the relative order is the live tree's, and every leaf binds the same
    # handler object the live tree binds at that path
    live_parser = cli_mod._build_parser()
    live = list(_sub(live_parser).choices)
    assert mine == [c for c in live if c in expected], name
    live_leaves = dict(_leaves(live_parser))
    seen = 0
    for path, fn in _leaves(mine_parser):
        assert live_leaves[path] is fn, path
        seen += 1
    assert seen >= 1


def test_the_six_domains_and_the_self_registering_groups_are_the_whole_surface():
    live_parser = cli_mod._build_parser()
    live = _sub(live_parser)
    domains = set()
    for name in commands.PARSER_MODULES:
        _, sub = _root()
        _parser_module(name).register_parsers(sub)
        assert domains.isdisjoint(sub.choices), name
        domains |= set(sub.choices)
    self_registered = {
        top for top, child in live.choices.items()
        if not any(fn.__module__.startswith("mindhive.commands.")
                   for _, fn in _leaves(child))}
    # `ci` joined in blueprint P1f: QUARANTINED is reachable and `manual_reset`
    # is its only exit, so the group had to ship WITH that state rather than in
    # P1g with the CI runner. Self-registering like the rest -- one named call
    # in `commands/parsers/__init__.py`, handlers bound beside their flags.
    assert self_registered == {"bench", "inject", "pg", "consensus", "clusters",
                               "lintester", "ci"}
    assert domains.isdisjoint(self_registered)
    assert domains | self_registered == set(live.choices)


def test_build_parser_still_names_every_add_call_itself():
    """The pinned surface order interleaves domains, so `_build_parser` cannot
    be a loop over `register_parsers`; the Protocol is the contract for
    mounting ONE domain, and this pins that the assembly did not quietly
    become the loop that would break the order pin."""
    from mindhive.commands import parsers
    src = inspect.getsource(parsers._build_parser)
    assert "register_parsers" not in src
    # 15 since blueprint P1h: `add_watcher`, appended at the TAIL of the
    # assembly so the pinned surface order is unchanged for everything above
    # it. The number is the point -- a loop would read as one call.
    assert src.count("maintenance_p.add_") == 15


# --- verification.py --------------------------------------------------------------

def test_verify_and_selftest_live_in_verification_and_left_maintenance():
    assert cli_mod.cmd_verify is verification.cmd_verify
    assert cli_mod.cmd_selftest is verification.cmd_selftest
    assert cli_mod.cmd_verify_chain is verification.cmd_verify_chain
    assert verification.cmd_verify.__module__ == "mindhive.commands.verification"
    for n in ("cmd_verify", "cmd_selftest"):
        assert n not in maintenance.__all__, n
        assert not hasattr(maintenance, n), n
    # The three watcher handlers joined at P1h for the reason the module
    # docstring gives: `verify`, `selftest` and `watcher status|recover` all
    # answer "is the store consistent with itself".
    assert verification.__all__ == ["cmd_verify", "cmd_selftest",
                                    "cmd_verify_chain", "cmd_watcher_run",
                                    "cmd_watcher_status", "cmd_watcher_recover"]
    mods = list(commands.HANDLER_MODULES)
    assert mods.index("verification") == mods.index("orchestration") - 1
    assert "base" not in mods
    assert [n for n in base.__all__ if n.startswith("cmd_")] == []
    table = commands.dispatch_table()
    assert table["cmd_verify"] is verification.cmd_verify
    assert table["cmd_selftest"] is verification.cmd_selftest
    assert table["cmd_verify_chain"] is verification.cmd_verify_chain
    leaves = {" ".join(p): fn for p, fn in _leaves(cli_mod._build_parser())}
    assert leaves["verify"] is verification.cmd_verify
    assert leaves["selftest"] is verification.cmd_selftest


def test_cmd_selftest_moved_byte_for_byte():
    assert inspect.getsource(verification.cmd_selftest) == (
        "def cmd_selftest(args) -> int | None:\n"
        "    # The exit code IS the command's contract; `run` returns the whole result\n"
        "    # because the dashboard's job reports the counts too.\n"
        "    return selftest_mod.run()[\"exit_code\"]\n")


def test_hive_verify_help_lists_both_flags():
    help_text = _sub(cli_mod._build_parser()).choices["verify"].format_help()
    assert "--json" in help_text and "--strict" in help_text


def test_verify_default_output_is_json_dumps_of_the_verdict_and_exit_zero(tmp_path, capsys):
    _seal_one_row()
    assert cli_mod.main(["verify"]) == 0
    out = capsys.readouterr().out
    assert out == json.dumps(chain.verify()) + "\n"
    assert json.loads(out)["ok"] is True


def test_verify_json_is_the_sorted_compact_one_line_form(tmp_path, capsys):
    _seal_one_row()
    assert cli_mod.main(["verify", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.endswith("\n") and out.count("\n") == 1
    verdict = json.loads(out)
    assert verdict == chain.verify()
    assert out == json.dumps(verdict, sort_keys=True, separators=(",", ":")) + "\n"
    assert list(verdict) == sorted(verdict)


def test_verify_strict_exits_one_on_a_corrupt_row_and_the_default_still_exits_zero(tmp_path, capsys):
    seg = _seal_one_row()
    assert cli_mod.main(["verify", "--strict"]) == 0
    with open(seg, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert cli_mod.main(["verify"]) == 0            # unchanged: stdout, never the exit code
    assert cli_mod.main(["verify", "--strict"]) == 1
    out = capsys.readouterr().out
    assert out.count("corrupt_rows") == 2


def test_verify_strict_exits_one_on_a_broken_chain_alone(tmp_path, capsys):
    """A sealed row rewritten as VALID json: no corrupt row, `ok` false."""
    seg = _seal_one_row()
    text = seg.read_text(encoding="utf-8")
    assert '"text":"a"' in text
    seg.write_text(text.replace('"text":"a"', '"text":"b"'), encoding="utf-8")
    assert cli_mod.main(["verify", "--strict"]) == 1
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["ok"] is False and "corrupt_rows" not in verdict


def test_cmd_verify_chain_is_verify_json_strict_as_one_call(tmp_path, capsys):
    seg = _seal_one_row()
    assert verification.cmd_verify_chain(argparse.Namespace(project=None)) is None
    out = capsys.readouterr().out
    verdict = json.loads(out)
    assert verdict["ok"] is True
    assert out == json.dumps(verdict, sort_keys=True, separators=(",", ":")) + "\n"
    with open(seg, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert verification.cmd_verify_chain(argparse.Namespace()) == 1
    assert verification.cmd_verify_chain(None) == 1
    assert capsys.readouterr().out.count("corrupt_rows") == 2


def test_the_readme_documents_the_two_flags():
    readme = (REPO / "README.md").read_text(encoding="utf-8", errors="replace")
    assert "`hive verify [--json] [--strict]`" in readme
