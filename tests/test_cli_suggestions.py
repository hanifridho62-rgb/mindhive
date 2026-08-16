# tests/test_cli_suggestions.py — did-you-mean on an unknown top-level command.
"""This CLI's primary user is an agent, so an unknown command must teach rather
than dump.

Before this gate, `hive stat` answered by printing all 39 command names TWICE —
once in the usage line, once inside `(choose from ...)` — and never named the one
command the operator obviously meant. The agent-facing cost was worse than the
noise: the MANDATORY SOP resident in MEMORY.md and re-injected on every prompt
carried the sentence "hive plugins list still does not run", i.e. the memory
system was spending tokens every turn warning about broken affordances instead
of the CLI simply saying what it meant.

Scope: the top-level command list only. Routing an *unrecognized argument* to the
owning subparser's usage (`hive recall --semantics x` prints the ROOT usage) is
deliberately NOT fixed here — argparse raises that from the root parser after all
subparsers have run, so it would need `set_defaults(_parser=sp)` threaded through
127 `add_parser` call sites. That stays open.
"""
import pytest

from mindhive import __main__ as cli_mod


def _exit_code(argv) -> int:
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(argv)
    return exc.value.code


def test_a_near_miss_command_is_told_what_was_meant(capsys):
    code = _exit_code(["stat"])
    err = capsys.readouterr().err
    assert code == 2                       # unchanged: still a usage error
    assert "did you mean" in err.lower()
    assert "state" in err


def test_a_garbage_command_gets_no_suggestion(capsys):
    """A suggester that fires on anything is noise, not help."""
    code = _exit_code(["zzzzzzz"])
    err = capsys.readouterr().err
    assert code == 2
    assert "did you mean" not in err.lower()


def test_the_failure_does_not_dump_the_whole_command_list_twice(capsys):
    """The pre-fix message printed every command name in the usage line AND
    again in the error body — 39 names, twice, to say one word was wrong."""
    _exit_code(["stat"])
    err = capsys.readouterr().err
    assert err.count("autoresume") <= 1


def test_the_suggester_is_conservative_on_its_own(capsys):
    """CANARY: a helper that returned a match for every input would make the
    near-miss test above pass while proving nothing."""
    assert cli_mod._closest_command("stat") == "state"
    assert cli_mod._closest_command("zzzzzzz") is None


def test_a_valid_command_still_dispatches(capsys):
    """CANARY for the override itself: replacing ArgumentParser.error must not
    change the success path."""
    assert cli_mod.main(["state"]) == 0
