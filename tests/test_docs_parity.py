# tests/test_docs_parity.py
"""Every shipped command is documented — enumerated at runtime, never counted.

A previous version of this gate was a number in a plan document ("the 39
subcommands"), and it was wrong by the time it was written: a count cannot say
WHICH command is undocumented, goes stale on the next `add_parser`, and fails in
a way that teaches nothing. So this file asks argparse what actually ships and
checks each name against the docs. Adding a command is then either documented or
a failing test naming the exact command — no maintenance of a number.

Scope is deliberately the top-level surface. Sub-subcommands (`hive sop show`,
`hive roadmap set`) are dozens of names whose individual documentation is a
judgement call; a group appearing nowhere in the README is not.
"""
import argparse
from pathlib import Path

from mindhive import __main__ as cli

REPO_ROOT = Path(__file__).resolve().parents[1]

# Documented deliberately nowhere: hook stages are invoked BY Claude Code, never
# typed by a person, and the README is a user surface. `docs/session-legend.md`
# owns the hook wiring.
UNDOCUMENTED_BY_DESIGN = {"hook"}


def _top_level_commands() -> list[str]:
    """The commands this build actually offers, read off the live parser."""
    parser = cli._build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("hive has no subparsers — the CLI shape changed")


def _readme() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8", errors="replace")


def test_every_command_the_cli_offers_is_in_the_readme():
    readme = _readme()
    missing = [c for c in _top_level_commands()
               if c not in UNDOCUMENTED_BY_DESIGN
               and f"hive {c}" not in readme]
    assert not missing, ("commands ship but are absent from README.md: "
                         + ", ".join(missing))


def test_the_enumeration_actually_finds_the_surface():
    """CANARY: a parser walk that returned [] would make the test above pass
    vacuously forever."""
    commands = _top_level_commands()
    assert len(commands) > 20
    for expected in ("append", "state", "sop", "roadmap", "context", "repo"):
        assert expected in commands


def test_the_readme_check_can_fail():
    """CANARY for the gate itself: a name that does not ship must be reported
    missing, or the substring match is matching something it should not."""
    assert "hive definitely-not-a-command" not in _readme()


# --------------------------------------------------------------------------
# D2: the `pg` group is the one place sub-subcommands ARE enumerated
# --------------------------------------------------------------------------
#
# The module docstring says sub-subcommands are out of scope, and that stays
# true for `hive sop show` / `hive roadmap set` — dozens of names whose
# individual documentation is a judgement call. `hive pg` is the deliberate
# exception, for a reason specific to it: its subcommands are the ONLY interface
# to an admin credential no other surface can name, and one of them
# (`hive pg refresh`) is quoted verbatim to a model inside a stale-mirror notice.
# A command an AI is told to hand to an operator, but which appears in no
# human-readable document, is a dead end with extra steps.

def _pg_subcommands() -> list[str]:
    parser = cli._build_parser()
    top = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    pg = top.choices["pg"]
    inner = next(a for a in pg._actions
                 if isinstance(a, argparse._SubParsersAction))
    return sorted(inner.choices)


def test_every_pg_subcommand_is_documented_in_the_readme():
    """D2. Each `hive pg <verb>` this build ships must appear in README.md."""
    readme = _readme()
    missing = [c for c in _pg_subcommands() if f"hive pg {c}" not in readme]
    assert not missing, ("`hive pg` subcommands ship but are absent from "
                         "README.md: " + ", ".join(missing))


def test_the_repair_command_the_ai_is_told_to_hand_over_is_documented():
    """The specific case, named rather than left to the loop above: `pgtools`
    tells a model to ask its operator for `hive pg refresh`. If that string ever
    stops being a real, documented command, the notice becomes a fabrication —
    and it would be a fabrication the model repeats with confidence."""
    import re

    from mindhive import pgtools
    notice = " ".join(pgtools._stale_notice(
        {"stale": True, "stale_reason": "x"}))
    quoted = re.findall(r"`(hive pg [a-z-]+)`", notice)
    assert quoted, f"the stale notice quotes no `hive pg` command at all: {notice!r}"
    for command in quoted:
        verb = command.split()[-1]
        assert verb in _pg_subcommands(), \
            f"the stale notice tells a model to run `{verb}`, which does not ship"
        assert command in _readme(), \
            f"the stale notice quotes `{command}`, which the README does not document"


def test_the_pg_enumeration_actually_finds_the_group():
    """CANARY. An empty subcommand list would make both assertions above pass
    forever, and the walk goes three levels deep — plenty of places to break."""
    subs = _pg_subcommands()
    assert len(subs) >= 7, subs
    for expected in ("migrate", "rebuild", "refresh", "parity", "tool"):
        assert expected in subs
