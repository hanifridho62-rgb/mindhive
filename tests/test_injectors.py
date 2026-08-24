"""Contracts for the injector registry and the per-turn injection record (T1-T4).

Encodes `.full-stack-feature/02-database-design.md` and `03-architecture.md`,
which derive from `docs/plans/2026-08-22-001-feat-injection-visibility-and-symbol-index-plan.md`
sections 11.1-11.3.

The feature exists because of a recorded accident: on 2026-08-23 a measurement
harness passed the hook payload as a `dict` where a JSON string was expected,
three injectors returned zero characters, `stderr` stayed empty, and nothing
anywhere could tell that 4,082 of 4,940 characters had failed to inject. Every
negative assertion below therefore carries a canary -- silence is only
meaningful from something proven able to speak.
"""
from __future__ import annotations

import io
import json
import sys

import pytest

from mindhive import events, injectors, sopgate
from mindhive import __main__ as cli
from mindhive.mindnet.telemetry import FACULTIES

# --- fixture builders -------------------------------------------------------

PROJECT = "test"


def inj(name, verdict="verbatim", chars=100, text="x" * 100, reason="ok"):
    """A valid `Injection`. Each test corrupts exactly one field."""
    return injectors.Injection(name=name, verdict=verdict, chars=chars,
                               tokens=chars // 4, ms=1, reason=reason, text=text)


def turn(*names, verdict="verbatim", text="body"):
    """A whole turn's worth of injections, all agreeing on *verdict*."""
    return [inj(n, verdict=verdict, chars=len(text), text=text) for n in names]


def rows(project=PROJECT):
    """Every `context_injected` row in the isolated store, oldest first."""
    return [e for e in events.read_all(project=project)
            if getattr(e, "type", None) == injectors.EVENT_TYPE]


# --- T1: the registry -------------------------------------------------------

def test_injector_names_and_faculties_are_disjoint_axes():
    # Two different questions: WHO injected vs WHICH faculty was consulted.
    # `FACULTIES` is pinned to the MN-5 decomposition by
    # tests/test_mindnet_telemetry.py; merging the two would silently retire
    # that pin.
    assert set(injectors.INJECTOR_NAMES).isdisjoint(set(FACULTIES))
    # CANARY: disjointness is vacuously true of an empty set. Prove both
    # sets can collide at all before trusting that they do not.
    assert len(injectors.INJECTOR_NAMES) >= 6
    assert len(FACULTIES) >= 6


def test_injector_names_have_no_duplicates():
    assert len(injectors.INJECTOR_NAMES) == len(set(injectors.INJECTOR_NAMES))


def test_the_six_live_injectors_are_all_registered():
    # The six `cmd_hook` actually composes, in its own order.
    for name in ("promptgate", "enrich", "mindnet", "premise", "sopgate", "style"):
        assert name in injectors.INJECTOR_NAMES


def test_verdicts_are_the_three_d4_states():
    assert injectors.VERDICTS == ("verbatim", "partial", "silent")


def test_verdicts_are_a_subset_of_the_sopgate_vocabulary():
    # sopgate's finer `pointers_only` survives inside `reason`, never as a
    # fourth verdict -- one legend, not two that can drift.
    assert set(injectors.VERDICTS) <= set(sopgate.VERDICTS)


def test_injection_carries_the_seven_recorded_fields():
    assert injectors.Injection._fields == (
        "name", "verdict", "chars", "tokens", "ms", "reason", "text")


def test_event_type_is_deterministic_telemetry_not_semantic():
    assert injectors.EVENT_TYPE in events.DETERMINISTIC_TYPES
    # The point of the type: no LEDGER markdown file, no Memory-view row, no
    # recall-corpus entry, per prompt, forever.
    assert injectors.EVENT_TYPE not in events.SEMANTIC_TYPES
    assert injectors.EVENT_TYPE not in events.MEMORY_VIEW_TYPES
    # CANARY: prove these three memberships are checkable at all -- a typo in
    # the constant names would make every assertion above pass vacuously.
    assert "code_change" in events.SEMANTIC_TYPES
    assert "code_change" in events.MEMORY_VIEW_TYPES
    assert "file_touched" in events.DETERMINISTIC_TYPES


def test_the_event_type_is_accepted_by_append_event():
    ev = events.append_event(injectors.EVENT_TYPE, {"probe": 1},
                             session_id="s-accept", project=PROJECT)
    assert ev.type == injectors.EVENT_TYPE


# --- T3: write-on-change storage --------------------------------------------

def test_first_turn_appends_one_row():
    res = injectors.record_turn(turn("sopgate", "style"),
                                session_id="s1", project=PROJECT)
    assert res["wrote_row"] is True
    assert len(rows()) == 1


def test_an_unchanged_second_turn_appends_nothing():
    payload = turn("sopgate", "style")
    injectors.record_turn(payload, session_id="s1", project=PROJECT)
    res = injectors.record_turn(payload, session_id="s1", project=PROJECT)
    assert res["wrote_row"] is False
    # The whole capacity design: an identical turn buys no row.
    assert len(rows()) == 1


def test_a_changed_turn_appends_a_row_carrying_the_closed_runs_length():
    same = turn("sopgate", "style")
    injectors.record_turn(same, session_id="s1", project=PROJECT)     # row 1
    injectors.record_turn(same, session_id="s1", project=PROJECT)     # silent
    injectors.record_turn(same, session_id="s1", project=PROJECT)     # silent
    injectors.record_turn(turn("sopgate", "style", text="DIFFERENT"),
                          session_id="s1", project=PROJECT)           # row 2
    got = rows()
    assert len(got) == 2
    # History stays exactly reconstructible: the two turns that bought no row
    # are counted on the row that closed their run.
    assert got[-1].payload["unchanged_turns"] == 2
    assert got[0].payload["unchanged_turns"] == 0


def test_a_verdict_change_alone_appends_a_row():
    # DISCRIMINATING: kills an implementation that hashes only the composed
    # text. `sopgate` degrading verbatim -> partial ships the same bytes and is
    # exactly the failure this feature exists to surface.
    body = "identical bytes"
    injectors.record_turn([inj("sopgate", verdict="verbatim", text=body)],
                          session_id="s1", project=PROJECT)
    res = injectors.record_turn([inj("sopgate", verdict="partial", text=body)],
                                session_id="s1", project=PROJECT)
    assert res["wrote_row"] is True
    assert len(rows()) == 2


def test_the_sidecar_holds_the_current_turn_even_when_no_row_is_written():
    payload = turn("sopgate")
    injectors.record_turn(payload, session_id="s1", project=PROJECT)
    injectors.record_turn(payload, session_id="s1", project=PROJECT)
    doc = injectors.read_last(project=PROJECT)
    assert [r["name"] for r in doc["injectors"]] == ["sopgate"]
    assert doc["unchanged_turns"] == 1          # the run currently open
    assert len(rows()) == 1                     # ...and still no second row


def test_stored_text_is_capped():
    huge = "z" * (injectors.MAX_TEXT_CHARS * 3)
    injectors.record_turn([inj("style", chars=len(huge), text=huge)],
                          session_id="s1", project=PROJECT)
    stored = rows()[0].payload["injectors"][0]["text"]
    assert len(stored) <= injectors.MAX_TEXT_CHARS
    # `chars` reports what was INJECTED, not what was stored -- truncating the
    # measurement would make the ledger disagree with the prompt.
    assert rows()[0].payload["injectors"][0]["chars"] == len(huge)


def test_record_turn_never_raises_when_the_append_fails(monkeypatch):
    # CANARY FIRST, before anything is patched: prove this call writes a row at
    # all, so the failure assertion below is about the fault and not about the
    # fixture.
    #
    # Deliberately NOT `monkeypatch.undo()` to re-arm the canary. `isolate_store`
    # is autouse and takes the SAME function-scoped `monkeypatch` instance, so
    # `undo()` unsets MINDHIVE_HOME and drops the remainder of the test onto the
    # real `~/.mindhive`. An earlier draft of this test did exactly that and
    # wrote two files into the live store home before it was caught.
    assert injectors.record_turn(turn("style"), session_id="s0",
                                 project=PROJECT)["wrote_row"] is True

    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(injectors.events, "append_event", boom)
    # A DIFFERENT signature, or the write-on-change rule would short-circuit
    # before reaching the append at all and the fault would never fire.
    res = injectors.record_turn(turn("premise", text="something else"),
                                session_id="s1", project=PROJECT)
    assert res["wrote_row"] is False
    assert res["error"]                       # reported, not swallowed to nothing


def test_a_corrupt_sidecar_is_treated_as_no_previous_turn():
    p = injectors.sidecar_path(PROJECT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json at all", encoding="utf-8")
    assert injectors.read_last(project=PROJECT) == {}
    # A turn still records: an unreadable sidecar must degrade to "no previous
    # turn", never to a crash on the hook path.
    assert injectors.record_turn(turn("style"), session_id="s1",
                                 project=PROJECT)["wrote_row"] is True


# --- T2: the probes ---------------------------------------------------------

# DERIVED, not retyped. The seventh injector (`symbols`, T8) arrived after this
# tuple was first written as six literals, and a hand-kept list would have left
# it with no probe coverage here while still calling itself "every live
# injector". `_MODULES` is the registry those probes are looked up through, so
# deriving from it is the same list by construction.
LIVE = tuple(sorted(injectors._MODULES))


def test_the_live_list_is_not_empty():
    """CANARY for every parametrized case below: a derived list that went empty
    would make all of them pass by running zero times."""
    assert len(LIVE) >= 7
    for expected in ("promptgate", "sopgate", "style", "symbols"):
        assert expected in LIVE


@pytest.mark.parametrize("name", LIVE)
def test_every_live_injector_exposes_a_probe(name):
    mod = injectors.module_for(name)
    assert callable(getattr(mod, "probe", None)), f"{name} has no probe()"


@pytest.mark.parametrize("name", LIVE)
def test_probe_returns_an_injection_naming_itself(name):
    got = injectors.probe_one(name)
    assert isinstance(got, injectors.Injection)
    assert got.name == name
    assert got.verdict in injectors.VERDICTS


@pytest.mark.parametrize("name", LIVE)
def test_probe_returns_silent_on_a_forced_fault(name, monkeypatch):
    # The real answer first, while nothing is patched -- see the note in
    # test_record_turn_never_raises_when_the_append_fails for why this is not
    # done with monkeypatch.undo().
    real = injectors.probe_one(name)

    mod = injectors.module_for(name)

    def boom(*a, **k):
        raise RuntimeError("forced")
    monkeypatch.setattr(mod, "probe", boom)
    got = injectors.probe_one(name)
    assert got.verdict == "silent"
    assert got.name == name
    # CANARY: the unpatched call must report something OTHER than this forced
    # silence, or the test proves nothing about the fault at all.
    assert real.reason != got.reason


def test_measure_reports_silent_for_a_zero_delta():
    got = injectors.measure("premise", before="abc", after="abc", ms=0)
    assert got.verdict == "silent"
    assert got.chars == 0


def test_measure_reports_the_delta_and_the_added_text():
    got = injectors.measure("style", before="abc", after="abcHELLO", ms=2)
    assert got.chars == len("HELLO")
    assert got.text == "HELLO"
    assert got.verdict == "verbatim"


def test_measure_never_reports_a_negative_delta():
    # An injector that SHRANK the context is a contract violation, not a
    # negative number: `chars` is a size, and a negative size would render as
    # a cat that injected minus-forty characters.
    got = injectors.measure("style", before="abcdef", after="abc", ms=0)
    assert got.chars == 0
    assert got.verdict == "silent"


# --- T4: the hook path ------------------------------------------------------

def _drive_hook(monkeypatch, prompt="fix the thing"):
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(json.dumps({"prompt": prompt,
                                                "session_id": "hook-s1"})))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    return cli.main(["hook", "user_prompt_submit"])


def test_hook_writes_exactly_one_row_per_turn_not_six(monkeypatch, capsys):
    assert _drive_hook(monkeypatch) == 0
    capsys.readouterr()
    # Six injectors ran; the ledger gets ONE row. Six rows/prompt against a
    # 41,470-event ledger is 10.56x the store in a year (02 section 3).
    assert len(rows()) == 1
    assert len(rows()[0].payload["injectors"]) >= 6


def test_hook_printed_context_is_unchanged_by_the_recorder(monkeypatch, capsys):
    _drive_hook(monkeypatch)
    with_recorder = capsys.readouterr().out

    monkeypatch.setattr(injectors, "record_turn",
                        lambda *a, **k: {"wrote_row": False})
    _drive_hook(monkeypatch)
    without = capsys.readouterr().out

    # Byte-identical. The recorder observes the prompt; it never shapes it.
    assert with_recorder == without
    # CANARY: the comparison is only meaningful if the hook printed something.
    assert with_recorder.strip()


def test_a_raising_recorder_does_not_disturb_the_printed_context(monkeypatch, capsys):
    _drive_hook(monkeypatch)
    clean = capsys.readouterr().out

    def boom(*a, **k):
        raise RuntimeError("recorder exploded")
    monkeypatch.setattr(injectors, "record_turn", boom)
    assert _drive_hook(monkeypatch) == 0        # exit code survives
    assert capsys.readouterr().out == clean     # and so does every byte


# --- merge_hook_context: the one writer of the house payload shape ----------
# The reader half (`context_text`) already lives here; this is its inverse. Five
# injectors (premise, sopgate, style, symbols, mindnet) hand-rolled the identical
# construction, differing only in append-vs-prepend and the separator. That
# duplication is exactly where `symbols` forgot the machine-prompt gate the other
# four had, so the shapes are collapsed to one function and pinned here.

def _ctx(text):
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": text}}


def test_merge_appends_with_the_default_double_newline():
    out = injectors.merge_hook_context(_ctx("A"), "B")
    assert injectors.context_text(out) == "A\n\nB"


def test_merge_prepends_when_asked():
    out = injectors.merge_hook_context(_ctx("A"), "B", prepend=True)
    assert injectors.context_text(out) == "B\n\nA"


def test_merge_honours_a_single_newline_separator():
    out = injectors.merge_hook_context(_ctx("A"), "B", sep="\n")
    assert injectors.context_text(out) == "A\nB"


def test_merge_of_empty_existing_is_just_the_text_no_separator():
    # The `if existing else block` branch every copy had: no leading/trailing sep.
    assert injectors.context_text(injectors.merge_hook_context(_ctx(""), "B")) == "B"
    assert injectors.context_text(
        injectors.merge_hook_context(_ctx(""), "B", prepend=True)) == "B"


def test_merge_builds_the_context_from_none_or_a_non_dict():
    # style/mindnet CREATE the context when the gate stayed silent.
    for empty in (None, "not a dict", 42, []):
        out = injectors.merge_hook_context(empty, "B")
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert injectors.context_text(out) == "B"


def test_merge_always_stamps_the_event_name():
    out = injectors.merge_hook_context({"hookSpecificOutput": {}}, "B")
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_merge_preserves_other_context_and_hso_keys():
    ctx = {"other": 1, "hookSpecificOutput": {"additionalContext": "A", "keep": 9}}
    out = injectors.merge_hook_context(ctx, "B")
    assert out["other"] == 1
    assert out["hookSpecificOutput"]["keep"] == 9
    assert injectors.context_text(out) == "A\n\nB"


def test_merge_does_not_mutate_the_input_context():
    ctx = _ctx("A")
    injectors.merge_hook_context(ctx, "B")
    assert ctx["hookSpecificOutput"]["additionalContext"] == "A"  # untouched
