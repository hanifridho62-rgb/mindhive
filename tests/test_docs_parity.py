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


# --------------------------------------------------------------------------
# the mirror's column contract — findable, and described in the present tense
# --------------------------------------------------------------------------
#
# WHY THESE EXIST. Four Postgres "constraints" were once put to this project as
# open problems — duplicate the payload as TEXT so it stays hashable, add an
# outbox to replace a dual-write, add a global sequence, loosen the constraints
# so odd rows are skipped rather than rejected — and all four were already
# solved, and already written down. The reader had not failed; the documents
# had. `docs/DATA_MODEL.md` §4.18 held every answer, nothing pointed at it, and
# its seam subsection still described a write path that had been DELETED,
# naming `pgstore.mirror_one` as current. A stale document is worse than a
# missing one: it answers, and it answers confidently.
#
# Both halves are gates now. The pointer has to resolve, and the section has to
# stop describing a dead function in the present tense.

def _data_model() -> str:
    return (REPO_ROOT / "docs" / "DATA_MODEL.md").read_text(
        encoding="utf-8", errors="replace")


def _seam_section() -> str:
    """§4.18's seam subsection: its start heading up to the next heading."""
    text = _data_model()
    start = text.index("#### The seam, and what it costs when unused")
    return text[start:text.index("\n#### ", start + 1)]


def test_the_status_contract_pointer_resolves_to_a_real_heading():
    """`hive pg status` names a doc section, so that section has to exist.

    A pointer at a renamed heading is how a cross-reference dies: nothing
    fails, and the reader concludes the documentation was never written."""
    from mindhive import pgcli
    assert pgcli.CONTRACT_DOC == "docs/DATA_MODEL.md"
    assert (REPO_ROOT / pgcli.CONTRACT_DOC).exists()
    assert f"#### {pgcli.CONTRACT_SECTION}" in _data_model(), (
        f"`hive pg status` points at {pgcli.CONTRACT_SECTION!r}, which is not "
        "a heading in docs/DATA_MODEL.md")


def test_the_readme_points_readers_at_the_column_contract():
    """The README is what people read first. Invariants reachable only by
    knowing to open a 2,000-line reference are not reachable."""
    from mindhive import pgcli
    readme = _readme()
    assert pgcli.CONTRACT_SECTION in readme, (
        "README.md does not point at the column contract — which is how four "
        "already-solved constraints came to be raised as open problems")
    assert "DATA_MODEL.md" in readme


# Identifiers §4.18's seam subsection presents as CURRENT. Curated on purpose: a
# gate over every identifier in a 2,000-line document would be noise, and this
# is the one subsection that has already gone stale.
SEAM_LIVE = {
    "events": ["_maybe_read_from_pg"],
    "pgmirror": ["refresh_changed", "scan_segments", "parity"],
}

# Names the same subsection presents as HISTORY. If one comes back, the prose
# calling it dead is the thing that is now wrong, and this failure asks for it.
SEAM_DEAD = {"pgstore": ["mirror_one"], "events": ["_maybe_mirror_to_pg"]}


def test_the_seam_section_names_only_functions_that_exist():
    import importlib
    section = _seam_section()
    for mod_name, names in SEAM_LIVE.items():
        mod = importlib.import_module(f"mindhive.{mod_name}")
        for name in names:
            assert name in section, (
                f"{name} is gate-checked but §4.18's seam section no longer "
                "mentions it — update SEAM_LIVE or the section")
            assert hasattr(mod, name), (
                f"docs/DATA_MODEL.md §4.18 presents {mod_name}.{name} as "
                "current, and it does not exist")


def test_the_seam_section_does_not_present_a_deleted_writer_as_live():
    """The exact staleness that already happened, pinned so it cannot recur.

    `mirror_one` and `_maybe_mirror_to_pg` may be NAMED — a reader who meets
    them in an old plan needs somewhere to learn they are dead — but they must
    not exist in the code while the section calls them gone."""
    import importlib
    for mod_name, names in SEAM_DEAD.items():
        mod = importlib.import_module(f"mindhive.{mod_name}")
        for name in names:
            assert not hasattr(mod, name), (
                f"{mod_name}.{name} exists again, while docs/DATA_MODEL.md "
                "§4.18 still describes it as deleted. Fix whichever is wrong.")


def test_the_seam_section_says_the_one_seam_is_a_read():
    """The claim README.md already makes, and the one the stale draft
    contradicted. Checked as substance rather than as a phrase count: the
    section must locate the seam on the read side and speak about the append
    path."""
    section = _seam_section().lower()
    assert "read side" in section or "read-side" in section
    assert "append path" in section
