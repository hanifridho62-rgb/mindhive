# src/mindhive/cliparse.py
"""Argparse behaviour shared by the whole `hive` surface.

Separate from `__main__.py` on purpose. That file is the CLI's *wiring* — one
`add_parser` block and one handler per command — and it is already four times
the review threshold. This is not wiring: it is how the parser behaves when the
wiring is given something it does not recognise, it applies to all 21 subparser
groups at once, and it is testable without constructing the 39-command parser.

Everything here is agent-facing ergonomics. `hive`'s primary user is a Claude
session, and argparse's stock answer to an unknown command spends 78 names to
say one word was wrong while never naming the near miss.
"""
import argparse
import difflib
import re

_INVALID_CHOICE_RE = re.compile(r"invalid choice: '(?P<bad>[^']*)'")

# difflib similarity below which no suggestion is offered. Deliberately strict:
# a suggester that fires on every typo is noise, and a WRONG suggestion sends an
# agent down a second dead end instead of the first. 'stat'/'state' scores 0.89.
SUGGESTION_CUTOFF = 0.6


def subparser_choices(parser: argparse.ArgumentParser) -> list:
    """The command names `parser` accepts, read off the parser itself.

    Off the live object rather than a hardcoded list, so a command added
    tomorrow is suggestible the same day."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices)
    return []


def closest_command(token: str, choices) -> str | None:
    """The command `token` most likely meant, or None when nothing is close."""
    hit = difflib.get_close_matches(token, sorted(choices), n=1,
                                    cutoff=SUGGESTION_CUTOFF)
    return hit[0] if hit else None


class SuggestingParser(argparse.ArgumentParser):
    """An argparse parser that names the command you probably meant.

    argparse's stock answer to an unknown command is to print every choice
    TWICE — once in the usage line, once inside `(choose from ...)`. With 39
    top-level commands that is 78 names spent to say one word was wrong, and it
    still never names the near miss. This override replaces the body with a
    single suggestion when one is close enough, and otherwise points at
    `--help`; the usage line is shortened separately by giving the subparsers
    action a `metavar`.

    `add_subparsers` defaults `parser_class` to `type(self)`, so every nested
    group (`hive plugins`, `hive sop`, …) inherits this for free — the fix is
    made once at the root and reaches all 21 groups.

    Exit code stays 2: this is still a usage error, and scripts gate on that.
    """

    def error(self, message):                       # pragma: no branch - thin
        m = _INVALID_CHOICE_RE.search(message)
        if m:
            bad = m.group("bad")
            guess = closest_command(bad, subparser_choices(self))
            hint = (f"did you mean '{guess}'?" if guess
                    else f"run `{self.prog} --help` for the full list")
            message = f"invalid choice: '{bad}' — {hint}"
        super().error(message)
