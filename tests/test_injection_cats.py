"""T5/T6 — the injection cats: the ring the UI reads, and the row it draws.

Two halves, and they are tested as two halves on purpose. The BACKEND half is
the bounded ring inside the sidecar plus `/api/injection`; the FRONTEND half is
what the emitted page actually contains. A page test that passes while the
endpoint returns nothing proves only that somebody typed a string, so the
canary here is the one `test_cat_board_shared.py` already uses: markup plus an
endpoint proves nothing unless a CALL joins them.

The founding ask was to see "apa saja yang berhasil di injeksi dan yang tidak".
Absence cannot express failure -- `cat_board.py` has paid for that lesson once
already, which is why the amber `.blind` class exists -- so a silent injector
gets its own visible state here rather than simply not being drawn.
"""
from __future__ import annotations

import json

import pytest

from mindhive import cat_board, injectors
from mindhive.dashboard_page import render_page

PROJECT = "test"


def inj(name, verdict="verbatim", chars=10, text="x" * 10):
    return injectors.Injection(name=name, verdict=verdict, chars=chars,
                               tokens=chars // 4, ms=1, reason=f"{name} says {verdict}",
                               text=text)


@pytest.fixture(scope="module")
def dash():
    return render_page("tok")


# --- the ring the dashboard reads ------------------------------------------

def test_record_turn_keeps_a_ring_of_recent_turns():
    for n in range(3):
        injectors.record_turn([inj("style", text=f"turn-{n}")],
                              session_id="s1", project=PROJECT)
    ring = injectors.recent_turns(project=PROJECT)
    assert len(ring) == 3


def test_the_ring_is_newest_first():
    for n in range(3):
        injectors.record_turn([inj("style", text=f"turn-{n}")],
                              session_id="s1", project=PROJECT)
    ring = injectors.recent_turns(project=PROJECT)
    # The UI draws top-down and the operator wants the last prompt at the top.
    assert ring[0]["total_chars"] == 10
    assert len(ring) == 3
    # Distinct signatures, so no entry collapsed into another.
    assert len({r["signature"] for r in ring}) == 3


def test_an_unchanged_turn_bumps_repeats_instead_of_adding_a_row():
    payload = [inj("style")]
    injectors.record_turn(payload, session_id="s1", project=PROJECT)
    injectors.record_turn(payload, session_id="s1", project=PROJECT)
    injectors.record_turn(payload, session_id="s1", project=PROJECT)
    ring = injectors.recent_turns(project=PROJECT)
    # Twelve identical rows would be noise, not signal. One row, counted.
    assert len(ring) == 1
    assert ring[0]["repeats"] == 3


def test_the_ring_is_bounded():
    for n in range(injectors.RECENT_CAP + 6):
        injectors.record_turn([inj("style", text=f"t{n}")],
                              session_id="s1", project=PROJECT)
    assert len(injectors.recent_turns(project=PROJECT)) == injectors.RECENT_CAP


def test_ring_entries_carry_no_injected_text():
    # The sidecar is rewritten on EVERY prompt. A ring carrying 12 turns of
    # capped text would make that write hundreds of kilobytes; the ring is the
    # index, and the full text lives on the current turn and in the ledger.
    injectors.record_turn([inj("style", text="z" * 500)],
                          session_id="s1", project=PROJECT)
    entry = injectors.recent_turns(project=PROJECT)[0]
    assert "text" not in entry["injectors"][0]
    assert entry["injectors"][0]["reason"]          # ...but the WHY survives


def test_the_current_turn_still_carries_full_text():
    injectors.record_turn([inj("style", text="the whole block")],
                          session_id="s1", project=PROJECT)
    doc = injectors.read_last(project=PROJECT)
    assert doc["injectors"][0]["text"] == "the whole block"
    assert doc["block"] == "the whole block"


def test_recent_turns_is_empty_and_does_not_raise_without_a_sidecar():
    assert injectors.recent_turns(project="never-written") == []


# --- the page ---------------------------------------------------------------

def test_the_dashboard_renders_the_injection_container(dash):
    assert 'id="mn-inject"' in dash


def test_the_dashboard_polls_the_injection_endpoint(dash):
    assert "/api/injection" in dash


def test_the_page_paints_what_it_polls(dash):
    """CANARY for the two above. Markup plus an endpoint proves nothing if no
    call joins them -- the same property `test_cat_board_shared` pins."""
    assert "paintInjections" in dash


def test_the_three_verdict_classes_are_all_present_and_distinct(dash):
    for verdict in injectors.VERDICTS:
        assert f".mn-inj.{verdict}" in cat_board.CSS, verdict
    # Distinct RULES, not just distinct names: three classes that resolve to the
    # same paint would render a failure and a success identically, which is the
    # whole defect this row exists to end.
    bodies = {v: cat_board.CSS.split(f".mn-inj.{v}")[1].split("}")[0]
              for v in injectors.VERDICTS}
    assert len(set(bodies.values())) == 3


def test_a_silent_injection_is_drawn_rather_than_omitted(dash):
    # E2: absence cannot express failure. `cat_board.py` already carries the
    # amber `.blind` class because "nothing is happening" and "I cannot know"
    # once rendered identically.
    assert "grayscale" in cat_board.CSS


def test_panel_owner_gains_inj_without_losing_the_two_existing_owners(dash):
    for owner in ("'cat'", "'sess'", "'inj'"):
        assert owner in cat_board.script(), owner


def test_the_injection_cats_carry_the_reduced_motion_hook(dash):
    # The guard keys on `.mn-cat-sprite`, which `catSVG` stamps on every cat it
    # draws -- so an injection cat inherits it for free, and this test is what
    # notices if the row ever stops using `catSVG`.
    assert "catSVG" in cat_board.script()
    assert ".mn-cat-sprite { animation:none !important; }" in cat_board.CSS


def test_the_panel_can_replay_the_whole_turn(dash):
    # E4: the question an operator has is what Claude READ, which is the
    # composed order, not any one pass's fragment.
    assert "whole turn" in cat_board.script()


def test_injected_text_is_rendered_as_text_not_markup():
    # The replayed block is arbitrary text from prompts and SOP bodies. It goes
    # into the DOM as textContent; an innerHTML path here would make every
    # injected block a script vector in the operator's own dashboard.
    assert "textContent" in cat_board.script()


# --- G6: the naming collision ----------------------------------------------

def test_the_replenishment_pill_no_longer_says_plain_injected():
    """G6. `ovCard` already had a pill reading "N injected" for the
    REPLENISHMENT injector -- work added to a run's plan from the record. Two
    unrelated meanings of "injected" in one card is a card that teaches the
    wrong thing, and the older meaning is the one that moves."""
    from mindhive import dashboard_js_oversee as ov
    assert "injected</span>" not in ov.JS


def test_the_replenishment_pill_still_says_what_it_counts():
    """The canary for the assertion above, which is bare-negative without it.

    "The word is gone" is satisfied just as well by a card that lost the pill
    altogether, and the pill is the whole point -- it is what makes a loop
    growing its own work visible. This file's own header says a silence is only
    meaningful from something proven able to speak; the pill is the speaker."""
    from mindhive import dashboard_js_oversee as ov
    assert "added to plan</span>" in ov.JS, (
        "the replenishment pill is gone, not renamed -- G6 asked for one word "
        "to change, not for the count to stop being shown")
    assert "r.injected_count" in ov.JS, (
        "the pill no longer reads the field it counts, so it can only ever "
        "render as absent")


def test_an_unchanged_turn_still_seeds_an_empty_ring():
    """Found by driving the REAL hook, not by a test.

    Four prompts through `hive hook user_prompt_submit` produced only two ring
    entries: the first two turns were `unchanged` relative to a sidecar written
    before the ring existed, and an unchanged turn only bumped a head entry that
    was not there. A steady state that began before the ring -- or after any
    sidecar reset -- would render as "nothing injected yet" while an injection
    was in fact happening on every single prompt.

    That is precisely the false silence this whole feature exists to end, so it
    gets the discriminating test it deserves: this kills an implementation that
    inserts on change alone.
    """
    payload = [inj("style")]
    injectors.record_turn(payload, session_id="s1", project=PROJECT)
    doc = injectors.read_last(project=PROJECT)
    doc.pop("recent", None)                    # a sidecar from before the ring
    injectors._write_sidecar(doc, project=PROJECT)
    assert injectors.recent_turns(project=PROJECT) == []

    injectors.record_turn(payload, session_id="s1", project=PROJECT)   # UNCHANGED
    ring = injectors.recent_turns(project=PROJECT)
    assert len(ring) == 1
    assert ring[0]["injectors"][0]["name"] == "style"
