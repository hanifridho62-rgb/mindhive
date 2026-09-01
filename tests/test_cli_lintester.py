# tests/test_cli_lintester.py
"""U12 — `hive lintester "<intent>"`, the PRD's single user-facing instruction.

Written against
``docs/plans/2026-09-01-001-feat-lintester-orchestrator-full-prd-plan.md`` U12.

The property worth the most here is the one in
`test_a_bare_invocation_starts_nothing`. A front door that fans real sub-agents
at a repository the moment somebody types it is a hazard, and the difference
between looking and doing must be an explicit flag — not a default anybody can
flip by editing one line.

`test_the_registration_stays_within_the_ratchet` guards the reason this command
lives in its own module at all: `src/mindhive/__main__.py` is under a
shrink-only cap in `tests/test_file_size_cap.py`, and the self-registering
pattern is what keeps a new command from eating it.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import __main__ as cli_mod
from mindhive.orchestrator import cli as lint_cli

REPO = Path(__file__).resolve().parents[1]


def _cli(*argv):
    return cli_mod.main(list(argv))


def _subparser_action(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subparser action")


# --- registration -------------------------------------------------------------

def test_the_top_level_surface_resolves_lintester():
    surface = _subparser_action(cli_mod._build_parser()).choices
    assert "lintester" in surface


def test_the_parser_carries_the_positional_intent():
    parser = _subparser_action(cli_mod._build_parser()).choices["lintester"]
    dests = {a.dest for a in parser._actions}
    assert "intent" in dests
    assert {"lanes", "root", "project", "json", "checkpoint_dir"} <= dests


def test_the_registration_stays_within_the_ratchet():
    """The whole reason `cli.py` exists rather than 90 more lines in `__main__`."""
    import re

    from mindhive import review as review_mod

    # The cap is read out of the one file that owns it rather than copied here.
    # A copy is a second authority, and the two drift the first time somebody
    # lowers the real one.
    cap_src = (REPO / "tests" / "test_file_size_cap.py").read_text(
        encoding="utf-8")
    match = re.search(r'"__main__\.py":\s*(\d+)', cap_src)
    assert match, "the ratchet entry moved or was renamed"

    text = (REPO / "src" / "mindhive" / "__main__.py").read_text(
        encoding="utf-8")
    assert review_mod._line_count(text) <= int(match.group(1))

    block = [ln for ln in text.splitlines() if "lintester" in ln]
    assert len(block) <= 4, (
        f"the command's wiring is leaking back into __main__.py: {block}")


def test_the_help_text_names_the_start_flag_without_minting_a_synonym():
    """Invariant 10 scans `src/mindhive` as TEXT, comments included. The flag's
    long option carries a leading dash so it is not a bare quoted token, and the
    module must read it off `args` rather than by quoted name."""
    source = (REPO / "src" / "mindhive" / "orchestrator" /
              "cli.py").read_text(encoding="utf-8")
    assert "--execute" in source
    assert 'getattr(args, "exe' not in source


# --- behaviour ----------------------------------------------------------------

def test_help_exits_zero_and_shows_the_positional(capsys):
    with pytest.raises(SystemExit) as exc:
        _cli("lintester", "--help")
    assert exc.value.code == 0
    assert "intent" in capsys.readouterr().out


def test_a_bare_invocation_starts_nothing(capsys, monkeypatch):
    """The hazard this default exists to prevent."""
    started = []

    from mindhive.orchestrator import lintester as lint_mod

    real = lint_mod.LintesterOrchestrator.execute_mission

    def spy(self, intent, dry_run=True, checkpoint_dir=None):
        started.append(dry_run)
        return real(self, intent, dry_run=dry_run, checkpoint_dir=checkpoint_dir)

    monkeypatch.setattr(lint_mod.LintesterOrchestrator, "execute_mission", spy)
    assert _cli("lintester", "make the writer retry") == 0
    assert started == [True], "a bare invocation was not a dry run"
    assert "dry-run" in capsys.readouterr().out


def test_an_empty_intent_is_refused_with_a_nonzero_status(capsys):
    assert _cli("lintester", "") == 2
    assert "an intent is required" in capsys.readouterr().out


def test_json_output_is_parseable_and_carries_the_report(capsys):
    assert _cli("lintester", "rotate the ledger segment", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "dry-run"
    assert payload["dry_run"] is True
    assert isinstance(payload["lanes"], list) and payload["lanes"]
    assert isinstance(payload["tasks"], list) and payload["tasks"]
    assert payload["enforcement"] in ("kernel", "timeout-only")


def test_a_non_numeric_lane_count_is_a_caller_error_not_a_silent_default():
    with pytest.raises(SystemExit) as exc:
        _cli("lintester", "x", "--lanes", "abc")
    assert exc.value.code == 2


def test_the_plain_report_names_every_lane_and_unit(capsys):
    assert _cli("lintester", "rotate the ledger segment", "--lanes", "1") == 0
    out = capsys.readouterr().out
    assert "mission " in out and "dry-run" in out
    assert "lane 0:" in out
    assert "fencing:" in out


def test_the_did_you_mean_suggestion_covers_the_new_command_too():
    """A new top-level command must join the suggestion surface, not break it.

    The second assertion is the one that would catch a regression: adding a
    command that is a near-miss for an existing one can make the suggester start
    answering the wrong word for inputs it used to get right."""
    assert cli_mod._closest_command("lintestr") == "lintester"
    assert cli_mod._closest_command("stat") == "state"
    assert cli_mod._closest_command("zzzzzzz") is None


# --- the real flow ------------------------------------------------------------

def test_the_command_runs_for_real_as_a_subprocess(tmp_path):
    """Exercised as an operator would, not through an in-process parser."""
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env["MINDHIVE_HOME"] = str(tmp_path)
    env["MINDHIVE_PROJECT"] = "test"
    proc = subprocess.run(
        [sys.executable, "-m", "mindhive", "lintester",
         "rotate the ledger segment", "--json"],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=180)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["outcome"] == "dry-run"
    assert payload["tasks"], "the real invocation composed no unit"


def test_register_is_the_only_public_wiring_this_module_exposes():
    assert callable(lint_cli.register)
    assert callable(lint_cli.cmd_lintester)
