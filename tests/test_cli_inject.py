"""T10 — `hive inject show`, the entry point the injection record had no surface for.

Six passes composed every prompt and the only way to ask what any of them
delivered was `hive sop injection`, which answers for one of them. The record
existed from T1-T4; nothing could read it back.

The load-bearing claim here is NOT "the command prints the sidecar". It is that
the command reports one row per REGISTERED pass, whether or not that pass wrote
a row this turn. A report folded from the record alone shows six passes where
seven are registered and says nothing about the seventh — which is the same
blindness `injectors.py` was built to end, reproduced one layer up in the tool
built to look at it. Every test that asserts a row is present therefore also has
a sibling asserting the row survives the pass being absent from the record.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindhive import injectors
from mindhive import __main__ as cli_mod

PROJECT = "test"


def _sidecar(project=PROJECT, **over):
    """Write a sidecar the way `record_turn` would, minus the ledger row.

    Built through `injectors._write_sidecar` rather than by hand so a change to
    the sidecar's shape breaks these tests instead of leaving them asserting
    against a shape the code stopped writing."""
    doc = {
        "session_id": "s-1",
        "signature": "sig-1",
        "injectors": [
            {"name": "promptgate", "verdict": "verbatim", "chars": 200,
             "tokens": 50, "ms": 3, "reason": "delivered", "text": "p" * 200},
            {"name": "sopgate", "verdict": "partial", "chars": 40,
             "tokens": 10, "ms": 1, "reason": "pointers_only: every body "
                                              "degraded to a pointer",
             "text": "s" * 40},
        ],
        "total_chars": 240,
        "total_tokens": 60,
        "block": "p" * 200 + "s" * 40,
        "unchanged_turns": 0,
        "recent": [],
    }
    doc.update(over)
    injectors._write_sidecar(doc, project=project)
    return doc


def _show(*argv, project=PROJECT):
    return cli_mod.main(["inject", "show", "--project", project, *argv])


# --- the registry is the shape of the report, not the record ----------------

def test_every_registered_pass_gets_a_row_even_when_the_turn_never_mentioned_it():
    """The whole reason this command reads the registry and not the record.

    The sidecar below names two passes. Seven are registered. A report that
    printed what it found would say "2 passes" on a pipeline of seven and give
    an operator no way to notice that five of them are unaccounted for."""
    _sidecar()
    rep = injectors.turn_report(project=PROJECT)
    names = [r["name"] for r in rep["injectors"]]
    assert names == list(injectors.PIPELINE_ORDER), (
        f"the report lists {names}, not the registry in pipeline order")
    missing = [r for r in rep["injectors"] if not r["recorded"]]
    assert {r["name"] for r in missing} == set(injectors.PIPELINE_ORDER) - {
        "promptgate", "sopgate"}
    assert all("no row recorded" in r["reason"] for r in missing), (
        "a pass the turn never mentioned must say so, not inherit a blank "
        "reason that reads like a delivered silence")


def test_a_recorded_pass_keeps_its_own_verdict_and_reason():
    """The canary for the test above: `recorded` has to be able to be true.

    Without this, a `registry_rows` that lost the record entirely would still
    satisfy every assertion about missing passes."""
    _sidecar()
    by = {r["name"]: r for r in injectors.turn_report(project=PROJECT)["injectors"]}
    assert by["promptgate"]["recorded"] is True
    assert by["promptgate"]["chars"] == 200 and by["promptgate"]["tokens"] == 50
    assert by["sopgate"]["verdict"] == "partial"
    assert "pointers_only" in by["sopgate"]["reason"], (
        "sopgate's finer word lives in `reason` — dropping it is how the three-"
        "verdict legend silently becomes a lie about a four-verdict pass")


def test_a_name_in_the_record_but_not_the_registry_is_flagged_not_dropped():
    """Drift the other way: a renamed or retired pass, and an older sidecar.

    Dropping the row would make a stale sidecar indistinguishable from a
    current one, which is exactly the comparison an operator is running."""
    _sidecar(injectors=[{"name": "retired_pass", "verdict": "verbatim",
                         "chars": 9, "tokens": 2, "ms": 0, "reason": "old",
                         "text": "x"}])
    rows = injectors.turn_report(project=PROJECT)["injectors"]
    odd = [r for r in rows if r.get("unregistered")]
    assert [r["name"] for r in odd] == ["retired_pass"]
    assert len(rows) == len(injectors.PIPELINE_ORDER) + 1


def test_the_registry_and_the_pipeline_order_describe_the_same_seven_passes():
    """`registry_rows` walks PIPELINE_ORDER and calls the result "registered".

    That is only true while the two tuples hold the same names. They are
    separate constants on purpose — one is membership, one is composition order
    — and nothing else asserts they cannot drift apart."""
    assert set(injectors.PIPELINE_ORDER) == set(injectors.INJECTOR_NAMES)
    assert len(injectors.PIPELINE_ORDER) == len(injectors.INJECTOR_NAMES)


# --- the command ------------------------------------------------------------

def test_show_exits_zero_and_prints_a_row_per_registered_pass(capsys):
    """The plan's verify line for T10, against the real argparse surface."""
    _sidecar()
    assert _show() == 0
    out = capsys.readouterr().out
    for name in injectors.INJECTOR_NAMES:
        assert name in out, f"{name} is registered and absent from the report"
    assert "%d pass(es)" % len(injectors.INJECTOR_NAMES) in out


def test_show_json_is_parseable_and_carries_the_composed_block(capsys):
    _sidecar()
    assert _show("--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["injectors"]) == len(injectors.PIPELINE_ORDER)
    assert doc["block"] == "p" * 200 + "s" * 40
    assert doc["has_text"] is True


def test_the_empty_case_still_parses_under_json(capsys):
    """`--json` is a stream contract and the empty case is where it breaks.

    The machine guaranteed to reach this branch is the one whose hook has never
    run — the first one anybody debugs. Its stdout still has to parse, so the
    hint goes to stderr. Exit 0: nothing recorded yet is an answer."""
    assert _show("--json", project="never-injected-here") == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out) == {}
    assert "user_prompt_submit" in cap.err
    assert cap.err.strip(), "the human still needs telling why it is empty"


def test_the_empty_case_puts_the_hint_on_stdout_without_json(capsys):
    """The canary for the split above: without `--json` there is no stream to
    protect, and sending the only output to stderr would make the command look
    like it printed nothing at all."""
    assert _show(project="never-injected-here") == 0
    cap = capsys.readouterr()
    assert "user_prompt_submit" in cap.out
    assert cap.err == ""


# --- the ring ---------------------------------------------------------------

def test_a_ring_turn_reports_that_it_carries_no_text(capsys):
    """The ring is the index, not the archive.

    Its entries hold no injected text by the same design that bounds them, so
    an empty `block` on a ring turn must not read as "this turn injected
    nothing" — which is the one reading this whole feature exists to prevent."""
    _sidecar(recent=[{"ts": "2026-08-27T00:00:00Z", "session_id": "s-0",
                      "signature": "sig-0", "total_chars": 11, "total_tokens": 2,
                      "repeats": 3,
                      "injectors": [{"name": "style", "verdict": "verbatim",
                                     "chars": 11, "tokens": 2, "ms": 0,
                                     "reason": "delivered"}]}])
    rep = injectors.turn_report("0", project=PROJECT)
    assert rep["has_text"] is False and rep["block"] == ""
    assert rep["repeats"] == 3
    assert _show("--turn", "0") == 0
    out = capsys.readouterr().out
    assert "index only" in out and "repeated 3 times" in out


def test_last_only_claims_a_timestamp_when_the_ring_head_is_that_turn():
    """A ring head with a different signature is an OLDER turn. Borrowing its
    `ts` would date the current injection with the moment some earlier one was
    recorded, which is a wrong answer that looks exactly like a right one."""
    _sidecar(signature="sig-now",
             recent=[{"ts": "2019-01-01T00:00:00Z", "signature": "sig-old",
                      "total_chars": 0, "total_tokens": 0, "injectors": []}])
    assert injectors.turn_report(project=PROJECT)["ts"] == ""
    _sidecar(signature="sig-now",
             recent=[{"ts": "2026-08-27T09:00:00Z", "signature": "sig-now",
                      "total_chars": 0, "total_tokens": 0, "injectors": []}])
    assert injectors.turn_report(project=PROJECT)["ts"] == "2026-08-27T09:00:00Z"


@pytest.mark.parametrize("bad,why", [("99", "outside the recent ring"),
                                     ("bogus", "unknown turn")])
def test_an_impossible_turn_fails_loudly_and_still_parses(capsys, bad, why):
    # A ring with something in it, so index 99 is out of RANGE rather than out
    # of a ring that does not exist — those are the two distinct errors the
    # test below pins, and seeding an empty ring here tested the wrong one.
    _sidecar(recent=[{"ts": "2026-08-27T00:00:00Z", "signature": "sig-1",
                      "total_chars": 0, "total_tokens": 0, "injectors": []}])
    assert _show("--turn", bad, "--json") == 2
    cap = capsys.readouterr()
    assert json.loads(cap.out) == {}, "stdout stays parseable even on the error"
    assert why in cap.err


def test_no_recorded_turns_is_a_different_error_from_no_such_index():
    """"The hook has never run here" and "there is no turn 9" call for
    different fixes, so they are not folded into one message."""
    rep = injectors.turn_report("3", project="never-injected-here")
    assert "no recorded turns" in rep["error"]


# --- rendering --------------------------------------------------------------

def test_the_human_table_is_pure_ascii():
    """A Windows console is cp1252 unless something changed it, and `print` of a
    character outside that page raises `UnicodeEncodeError` — turning a
    read-only report into a traceback on the platform this is developed on."""
    _sidecar()
    text = injectors.render_turn(injectors.turn_report(project=PROJECT))
    text.encode("cp1252")          # raises if a pass smuggled in a bullet
    assert text.isascii(), [c for c in text if not c.isascii()][:5]


def test_the_group_is_registered_on_the_real_parser():
    """`injectors.register(sub)` — the fourth module to take the convention that
    keeps `__main__.py` at one line per group instead of ten."""
    parser = cli_mod._build_parser()
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    assert "inject" in sub.choices
    assert "show" in sub.choices["inject"]._subparsers._group_actions[0].choices


# --- T12: the documented pipeline is generated, not maintained --------------

DOCS = [Path(__file__).resolve().parents[1] / "README.md",
        Path(__file__).resolve().parents[1] / "docs" / "session-legend.md"]


def test_every_registered_pass_has_a_line_in_the_generated_table():
    """A pass that reaches the prompt path undocumented is the whole failure.

    `pipeline_table` raises `KeyError` rather than emitting a blank row, so this
    is the test that turns "somebody added a seventh injector" into a red test
    instead of into a document that quietly describes six."""
    missing = [n for n in injectors.PIPELINE_ORDER
               if n not in injectors.PIPELINE_NOTES]
    assert not missing, (
        f"registered with no line in PIPELINE_NOTES: {missing}. Add one — the "
        f"table in README.md and docs/session-legend.md is generated from it")
    table = injectors.pipeline_table()
    for i, name in enumerate(injectors.PIPELINE_ORDER, 1):
        assert f"| {i} | `{name}` |" in table


def test_a_note_for_a_pass_that_is_not_registered_is_also_caught():
    """The reverse drift: a pass retired from the registry, its line left behind.

    Without this the notes become a graveyard, and a reader cannot tell which
    rows describe something that still runs."""
    extra = [n for n in injectors.PIPELINE_NOTES
             if n not in injectors.PIPELINE_ORDER]
    assert not extra, f"documented but not registered: {extra}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_the_document_carries_the_current_generated_table(doc):
    """G7: the composition order lived only in a function body.

    `docs/session-legend.md` carried a hand-written numbered list of this
    pipeline. By the time anything checked, it named FIVE passes out of seven —
    `sopgate` and `symbols` had each been added exactly where that list said to
    add them, and the list was never touched either time. Prose has nothing to
    disagree with; this comparison is the disagreement."""
    text = doc.read_text(encoding="utf-8")
    found = injectors.extract_doc_block(text)
    assert found, (
        f"{doc.name} carries no generated block — paste the output of "
        f"`hive inject table --markers` into it")
    assert found == injectors.doc_block(), (
        f"{doc.name}'s pipeline table is stale. Regenerate it: "
        f"hive inject table --markers")


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_the_document_names_every_pass(doc):
    """The blunt claim, independent of the block markers.

    If the marker convention is ever changed or dropped, this still fails for a
    document that has stopped naming a pass that runs."""
    text = doc.read_text(encoding="utf-8")
    missing = [n for n in injectors.PIPELINE_ORDER if f"`{n}`" not in text]
    assert not missing, f"{doc.name} never names: {missing}"


def test_extract_returns_empty_rather_than_raising_on_a_doc_without_markers():
    """The canary for the extractor: a document that lost its markers has to
    report a MISSING block, not crash the test that was going to say so."""
    assert injectors.extract_doc_block("no markers here at all") == ""
    assert injectors.extract_doc_block(injectors.doc_block()) == injectors.doc_block()
