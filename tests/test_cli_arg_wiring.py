# tests/test_cli_arg_wiring.py
"""Every `--session` and `--project` on the BUILT `hive` parser carries its validator.

## The defect

`cliparse.session_id_arg` was proven as a UNIT, at one subcommand (`hive append
--session`), and never as WIRING. Its docstring said it guarded "all 32
`--session` arguments across the surface" -- which was `__main__.py`'s count, not
the surface's. `consensus/cli.py` (`contribute`, `fold`) and `mindnet/briefcli.py`
(`brief`) register three more into the same parser, and all three were unguarded.
Two of them hand the value straight to `events.append_event`, so
`hive consensus contribute ... --session ../../pwned` exited 0 and filed the row
under a quarantine hash without a word -- the answer the validator's own
docstring calls worse than refusing.

`--project` was the other half of the same path expression,
`store_home() / project / "events" / segment_name(session_id)`, and carried no
`type=` anywhere: `--project ../../pwned` wrote the ledger two directories ABOVE
`MINDHIVE_HOME`, with a perfectly conforming session id.

## Why this walks the parser instead of grepping the source

A grep for `add_argument("--session"` counts SPELLINGS. It cannot see a flag added
inside a loop (one spelling, several actions), and it only searches the modules
somebody thought to list -- which is precisely how three sites registered from
other modules were missed. The built parser is what argv is matched against, so
it is the one inventory that cannot be incomplete the way the defect was.

## The loud half has to agree with the quiet half

`events.segment_name` and `paths.namespace_name` quarantine silently; the
validators refuse out loud. `test_every_value_a_validator_accepts_is_its_own_name_at_the_sink`
pins them together: a value the validator accepts must be its own name at the
sink, or the CLI exits 0 and files a typed value under a hash without saying so
-- which is what a reserved `unsafe-` value did once the sinks reserved the
prefix and the validators did not.

## The canaries

* `test_the_unguarded_check_can_fire` -- an empty "unguarded" list means nothing
  unless the same check names a deliberately unwired flag two levels down.
* `test_the_walk_reaches_the_sites_that_were_unwired` -- a walker that never
  recursed into a registered module's subparsers would report all-clear.
* `test_the_walk_visits_the_whole_surface` -- a walker that visits nothing passes
  every "all of them" assertion vacuously.
* `test_a_conforming_project_and_session_still_parse_and_write` -- a validator
  that refused everything would pass every refusal test here and break the CLI.
"""
import argparse

import pytest

from mindhive import __main__ as cli_mod
from mindhive import cliparse, events, paths

B = chr(92)          # a literal backslash, kept out of the string literals below

VALIDATORS = {"--session": cliparse.session_id_arg,
              "--project": cliparse.project_arg}

# Measured 2026-09-13 on the built parser: 40 `--session` and 73 `--project`
# actions -- more than a grep of `add_argument` spellings finds, because a flag
# added inside a loop is one spelling and several actions. The floors sit a
# little below the measurement, so retiring a command does not go red for a
# reason unrelated to this file; a walker that stops at the top level, or a
# registered module that falls off the parser, still does.
SESSION_FLOOR = 35
PROJECT_FLOOR = 70

# The sites the old "all 32" missed. Every one is registered by a module other
# than `__main__`, which is why counting `__main__.py` could not see them.
ONCE_UNWIRED = [("consensus", "contribute"), ("consensus", "fold"),
                ("mindnet", "brief")]


# --- the walk ------------------------------------------------------------------

def _walk(parser, path=()):
    """``(subcommand path, action)`` for every non-subparser action reachable
    from *parser*, recursing through every `_SubParsersAction` at every depth."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _walk(child, path + (name,))
        else:
            yield path, action


def _sites(parser, flag):
    return [(path, action) for path, action in _walk(parser)
            if flag in action.option_strings]


def _unguarded(parser, flag):
    """The subcommand paths whose *flag* is NOT typed with its validator.
    Identity, not name: a local function that happens to be called
    `session_id_arg` is a second copy of the rule, which is the thing this file
    exists to prevent."""
    validator = VALIDATORS[flag]
    return [" ".join(path) or "<root>" for path, action in _sites(parser, flag)
            if action.type is not validator]


@pytest.fixture
def parser():
    """Built per test, inside the autouse isolated store -- a module-scoped
    fixture would be constructed BEFORE that isolation applies."""
    return cli_mod._build_parser()


# --- the wiring ----------------------------------------------------------------

@pytest.mark.parametrize("flag", sorted(VALIDATORS))
def test_every_flag_on_the_built_parser_carries_its_validator(parser, flag):
    """The assertion the defect needed and nothing made: every `--session` is
    `type=cliparse.session_id_arg` and every `--project` is
    `type=cliparse.project_arg`, on every subcommand, whichever module
    registered it. Deleting the `type=` from any one site fails here by name."""
    assert _unguarded(parser, flag) == [], (
        "%s without %s on: %s" % (flag, VALIDATORS[flag].__name__,
                                  _unguarded(parser, flag)))


def test_the_unguarded_check_can_fire():
    """THE CANARY for the wiring test. An empty result there means "all wired"
    only if the same check reports a deliberately unwired flag -- nested two
    subparser levels down, the depth `consensus contribute` sits at -- and stays
    quiet about a correctly wired sibling."""
    root = argparse.ArgumentParser(prog="toy")
    group = root.add_subparsers(dest="cmd").add_parser("group")
    leaves = group.add_subparsers(dest="leaf")
    bare = leaves.add_parser("bare")
    bare.add_argument("--session", default=None)
    bare.add_argument("--project", type=str, default=None)
    wired = leaves.add_parser("wired")
    wired.add_argument("--session", type=cliparse.session_id_arg, default=None)
    wired.add_argument("--project", type=cliparse.project_arg, default=None)

    assert _unguarded(root, "--session") == ["group bare"]
    assert _unguarded(root, "--project") == ["group bare"]


@pytest.mark.parametrize("path", ONCE_UNWIRED, ids="-".join)
def test_the_walk_reaches_the_sites_that_were_unwired(parser, path):
    """THE CANARY for reach: the three sites the old count missed are visited,
    for both flags. A walker that stopped short of self-registering modules
    would pass the wiring test while seeing none of them."""
    for flag in VALIDATORS:
        assert path in {p for p, _ in _sites(parser, flag)}, (flag, path)


def test_the_walk_visits_the_whole_surface(parser):
    """THE CANARY for volume: a walker that visits nothing satisfies "every
    visited flag is wired" trivially. See the floors' comment for the measured
    counts and why the floor is not the exact number."""
    assert len(_sites(parser, "--session")) >= SESSION_FLOOR
    assert len(_sites(parser, "--project")) >= PROJECT_FLOOR


# --- a validator must not refuse the value the parser supplies itself ----------

def test_an_omitted_flag_is_not_refused_by_its_own_default(parser):
    """argparse runs a STRING default through `type=` when the flag is omitted
    (`None` is left alone). So a validator attached to a flag whose default is
    `""` refuses the command whenever the operator leaves the flag OFF, with an
    error naming a value nobody typed. Wiring the validator is only half the
    change; every site it is wired to must default to something it accepts."""
    refused = []
    for path, action in _walk(parser):
        if action.type in VALIDATORS.values() and isinstance(action.default, str):
            try:
                action.type(action.default)
            except argparse.ArgumentTypeError:
                refused.append((" ".join(path), action.option_strings[0],
                                action.default))
    assert refused == [], refused


def test_a_string_default_really_is_passed_through_the_validator():
    """THE CANARY for the test above: its premise is argparse behaviour, so it is
    proven on a toy parser rather than trusted. `""` exits 2; `None` parses."""
    toy = argparse.ArgumentParser(prog="toy")
    toy.add_argument("--project", type=cliparse.project_arg, default="")
    with pytest.raises(SystemExit) as exc:
        toy.parse_args([])
    assert exc.value.code == 2

    fine = argparse.ArgumentParser(prog="toy")
    fine.add_argument("--project", type=cliparse.project_arg, default=None)
    assert fine.parse_args([]).project is None


def test_bench_dashboard_parses_without_project(parser):
    """The live instance of that hazard. `hive bench dashboard --url ... --label
    ...` deliberately omits `--project` so the SERVER resolves its own
    namespace; with `default=""` beside `type=cliparse.project_arg` it exits 2
    with "invalid project ''" instead. Parse only -- nothing is contacted."""
    ns = parser.parse_args(["bench", "dashboard", "--url", "http://127.0.0.1:9",
                            "--label", "probe"])
    assert not ns.project


# --- the loud half accepts only what the quiet half takes at face value --------

# Conforming by both whitelists, and inside the prefix the sinks reserve.
RESERVED = events.QUARANTINE_PREFIX + "0" * 16


def _accepts(validator, value) -> bool:
    try:
        validator(value)
    except argparse.ArgumentTypeError:
        return False
    return True


def test_every_value_a_validator_accepts_is_its_own_name_at_the_sink():
    """One rule, two surfaces: the validator refuses a human out loud, and
    `events.segment_name` / `paths.namespace_name` quarantine silently where a
    throw would break a hook. That split is only honest if every value the loud
    half ACCEPTS is taken at face value by the quiet half. Otherwise a typed
    value passes the validator, exits 0, and is filed under a hash nobody was
    told about -- the answer `session_id_arg`'s docstring calls worse than
    refusing.

    The sinks RESERVE the quarantine prefix; the validators did not. Observed
    through `main()` against a scratch store: `consensus contribute ...
    --session unsafe-9cdf6a50100a862e` exited 0 and wrote
    `unsafe-9d11fe0ceb825afd.jsonl`, and `--project unsafe-0000000000000000`
    exited 0 and wrote under `unsafe-dc2533ed3b0e0f5c/`. Delegating the
    validator's accept test to its sink closes this by construction; the case
    spellings are here so a sink that learns to reserve `UNSAFE-` cannot leave
    its validator behind."""
    # The fixture reaches the gap, or this proves nothing: RESERVED passes both
    # whitelists, so only the reservation can tell it from a real name.
    assert events.SESSION_ID_RE.fullmatch(RESERVED)
    assert paths.PROJECT_RE.fullmatch(RESERVED)
    # And the loop below compares something: a plain name is accepted by both.
    assert _accepts(cliparse.session_id_arg, "ok-session-1")
    assert _accepts(cliparse.project_arg, "ok-session-1")

    # The last row is what Windows stores as something else: quarantined on
    # `nt`, their own names elsewhere. Either way the two halves must agree on
    # the runner in hand, so the row is platform-neutral as an assertion.
    candidates = ["ok-session-1", "__hivemind__", "0", "unsafe_mode",
                  RESERVED, RESERVED.upper(), "Unsafe-" + "0" * 16,
                  "../../x", "",
                  "NUL", "com1", "nul.jsonl", "abc."]
    disagree = []
    for value in candidates:
        if (_accepts(cliparse.session_id_arg, value)
                and events.segment_name(value) != value + ".jsonl"):
            disagree.append(("--session", value))
        if (_accepts(cliparse.project_arg, value)
                and paths.namespace_name(value) != value):
            disagree.append(("--project", value))
    assert disagree == [], disagree


# --- end to end: the loud refusal, at the surface a human typed ----------------

CONTRIBUTE = ["consensus", "contribute", "--frame", "f1", "--kind", "test",
              "--subject", "s1", "--value", "3"]
FOLD = ["consensus", "fold", "--frame", "f1"]
BRIEF = ["mindnet", "brief"]

REFUSALS = [
    pytest.param(CONTRIBUTE, "--session", "../../pwned", "invalid session id",
                 id="contribute-session"),
    pytest.param(CONTRIBUTE, "--project", "../../pwned", "invalid project",
                 id="contribute-project"),
    pytest.param(CONTRIBUTE, "--project", ".." + B + ".." + B + "pwned",
                 "invalid project", id="contribute-project-backslash"),
    pytest.param(FOLD, "--session", "../../pwned", "invalid session id",
                 id="fold-session"),
    pytest.param(FOLD, "--project", "../../pwned", "invalid project",
                 id="fold-project"),
    pytest.param(BRIEF, "--session", "../../pwned", "invalid session id",
                 id="brief-session"),
    pytest.param(BRIEF, "--project", "../../pwned", "invalid project",
                 id="brief-project"),
    # Conforming by the whitelist but reserved by the sink: without a refusal
    # here the command exits 0 and quietly writes under a DIFFERENT hash.
    pytest.param(CONTRIBUTE, "--session", RESERVED, "invalid session id",
                 id="contribute-session-reserved"),
    pytest.param(CONTRIBUTE, "--project", RESERVED, "invalid project",
                 id="contribute-project-reserved"),
]


def _store_two_levels_down(tmp_path, monkeypatch):
    """`MINDHIVE_HOME` at `tmp_path/a/b`, so `../../pwned` resolved against the
    store lands at `tmp_path/pwned` -- where the test can see it, and never
    anywhere real, even with every guard reverted."""
    home = tmp_path / "a" / "b"
    home.mkdir(parents=True)
    monkeypatch.setenv("MINDHIVE_HOME", str(home))
    monkeypatch.setenv("MINDHIVE_PROJECT", "probe")
    return home


@pytest.mark.parametrize("argv, flag, value, reason", REFUSALS)
def test_the_cli_refuses_a_hostile_value_before_writing(
        tmp_path, monkeypatch, capsys, argv, flag, value, reason):
    """Through `main()`, the real argv path. Exit 2 with the reason, and not one
    file anywhere under `tmp_path` -- which is ABOVE the store, so it covers both
    "nothing escaped" and "nothing was quietly quarantined inside". The sink
    guards (`segment_name`, `namespace_name`) would keep a reverted surface's
    write inside the store and exit 0; the exit code is what tells a loud
    refusal from a silent quarantine."""
    _store_two_levels_down(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(argv + [flag, value])

    assert exc.value.code == 2
    assert reason in capsys.readouterr().err
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_a_conforming_project_and_session_still_parse_and_write(
        tmp_path, monkeypatch, capsys):
    """THE CANARY for the refusals: the same command with a conforming
    `--project` and `--session` exits 0 and lands exactly at
    `<store>/<project>/events/<session>.jsonl`, with nothing outside the store."""
    home = _store_two_levels_down(tmp_path, monkeypatch)

    rc = cli_mod.main(CONTRIBUTE + ["--project", "okproj",
                                    "--session", "ok-session-1"])

    assert rc == 0, capsys.readouterr()
    assert (home / "okproj" / "events" / "ok-session-1.jsonl").is_file()
    outside = [p for p in tmp_path.rglob("*")
               if p.is_file() and home not in p.parents]
    assert outside == []
