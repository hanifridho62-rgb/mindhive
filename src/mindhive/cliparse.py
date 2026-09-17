# src/mindhive/cliparse.py
"""Argparse behaviour shared by the whole `hive` surface.

Separate from `commands/parsers/` on purpose (it was `__main__.py` until the P9
split). That package is the CLI's *wiring* — one `add_<command>(sub)` per
command — and this is not wiring: it is how the parser behaves when the wiring
is given something it does not recognise, it applies to every subparser group
at once (24 today), and it is testable without constructing the full 46-command
parser.

Everything here is agent-facing ergonomics. `hive`'s primary user is a Claude
session, and argparse's stock answer to an unknown command spends 78 names to
say one word was wrong while never naming the near miss.
"""
import argparse
import difflib
import re

from . import events
from . import paths

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


def session_id_arg(value):
    """argparse ``type=`` for every ``--session``: the id, or a usage error.

    Here rather than in ``commands/parsers/`` for this module's founding reason:
    it is parser BEHAVIOUR shared by every ``--session`` argument across the
    surface, not the per-command wiring that package holds -- and each of its
    modules is under a 500-line cap (`tests/test_commands_reexport.py`) whose
    failure message says to split, not to raise the number.

    No count in this sentence, deliberately. It used to read "all 32", which was
    ``__main__.py``'s number and not the surface's: `consensus/cli.py` and
    `mindnet/briefcli.py` register three more into the same parser, and all
    three went unguarded for exactly as long as the docstring asserted they
    could not. `tests/test_cli_arg_wiring.py` now walks the built parser and
    fails on the first unguarded one, which is a claim that cannot rot.

    **Why this exists alongside ``events.segment_name``, which already makes the
    id safe.** ``segment_name`` quarantines a hostile id to
    ``unsafe-<sha256>.jsonl`` and never raises, because it sits on the hook path
    where ``post_tool_use`` fires on every tool call and a throw would break the
    session. That silence is right for a machine-generated payload and wrong for
    a human at a keyboard: filing somebody's events under a hash without telling
    them is a worse answer than refusing the id. Two layers, each matched to its
    surface -- not a redundant check.

    **The accept test is ``events.validate_session_id``, by delegation.** That
    is the anchored whitelist (``events.SESSION_ID_REGEX``) AND the sink's own
    answer at its strictest platform, ``segment_name(value, os_name="nt") ==
    value + ".jsonl"``. The whitelist alone (``events.SESSION_ID_RE``, shared
    with ``hooks/lib.sh`` and ``hooks/lib.ps1``) was the first spelling, and
    the sink outgrew it: it also reserves the quarantine prefix in any case and,
    on Windows, refuses device names. So ``--session unsafe-9cdf6a50100a862e``
    passed this check, exited 0 and was filed under a DIFFERENT hash -- the
    silent answer this function exists to replace. Asking the sink at ``nt``
    on every platform is what makes ``CON`` refused on Linux too: a store moves
    between machines, and an id a Windows runner would quarantine is a
    different segment the day the store is opened there. An accept at ``nt``
    implies an accept on the runner in hand, which is what keeps
    `tests/test_cli_arg_wiring.py`'s accept-implies-sink-identity invariant.

    The message composes BOTH texts: the ``invalid session id`` phrase two
    test files pin in stderr, and the ``ValueError`` text so the operator sees
    the rule they broke rather than a paraphrase of it. Chained from the
    ``ValueError`` -- one rule, stated once. ``ArgumentTypeError`` makes
    argparse exit 2 with the message before any handler runs, so a refused id
    writes nothing at all."""
    try:
        return events.validate_session_id(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            "invalid session id %r: %s" % (value, e)) from e


def project_arg(value):
    """argparse ``type=`` for every ``--project``: the namespace, or a usage
    error.

    `session_id_arg`'s sibling, and the pairing is the point: a namespace names
    a DIRECTORY under the store exactly as a session id names a file inside it,
    so the two are one hazard with one answer rather than two similar ones.
    `paths.namespace_name` is the total half that cannot throw (it runs under
    every projection read), this is the half that refuses a human out loud.

    Accepts exactly what ``paths.namespace_name`` takes at face value, asked of
    it directly, for `session_id_arg`'s reason: a whitelist-only check let
    ``--project unsafe-0000000000000000`` exit 0 and write under another
    hash. The same delegation refused the store root's own file names
    (``paths.STORE_ROOT_FILES``) the moment the sink began quarantining them;
    only the message below had to learn about it."""
    if isinstance(value, str) and paths.namespace_name(value) == value:
        return value
    raise argparse.ArgumentTypeError(
        "invalid project %r: it names a namespace directory in the store, so "
        "it must be 1-128 characters of letters, digits, '.', '_' or '-', "
        "starting with a letter, digit or '_' (no path separators, '..', "
        "drive letters or wildcards), must not start with the reserved "
        "'unsafe-' in any case, must not be one of the store root's own file "
        "names such as LIVE_LOCK or autosync.json, and on Windows must not be "
        "a device name such as CON, NUL or COM1 or end in '.'"
        % (value,))
