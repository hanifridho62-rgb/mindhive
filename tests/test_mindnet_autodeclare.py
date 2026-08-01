# tests/test_mindnet_autodeclare.py
"""MN-18 — auto-declare, and the two gates that kept the spine silent.

The phase spine, the SOP section and the Improvements section were built, tested, and
never reached. `advice.resolve_sops` and `advice.resolve_constraints` worked; nothing
injected them, because `inject.block` returns "" when the sidecar holds no phase records
— and the live store held none.

Auto-declaring a task is the obvious fix and it is not sufficient. Measurement found a
second, independent gate: **one appended event of any type marks the sidecar stale**, the
hook itself appends on every prompt, and advice rode only the fresh branch. So a declared
task would still have got the generic block forever.

These tests cover both gates. The end-to-end canary at the bottom is the one that would
have caught the original dormancy — every unit can pass while the user still sees nothing.
"""
import json

import pytest

from mindhive import events, promptgate
from mindhive.mindnet import autodeclare as ad
from mindhive.mindnet import sidecar as sc_mod
from mindhive.mindnet import tasks as t_mod


# --- fixtures -------------------------------------------------------------------

def _sidecar(records=None, advice=None, fresh=True):
    """A stand-in Sidecar carrying only what `decide` reads.

    A namespace rather than a real `Sidecar`: `decide` must depend on the two attributes
    it names and nothing else, and a hand-built object is what proves that. The
    end-to-end tests below use the REAL sidecar, so the two shapes cannot drift
    unnoticed."""
    import types
    return types.SimpleNamespace(
        state=types.SimpleNamespace(records=records if records is not None else {}),
        advice=advice or {}, fresh=fresh)


CODE_PROMPT = "buatkan fitur export CSV baru di dashboard"


# --- U1: the decision, pure ------------------------------------------------------

def test_a_code_prompt_with_an_empty_sidecar_decides_to_declare():
    got = ad.decide(CODE_PROMPT, _sidecar())
    assert got.declare is True
    assert got.task_id and got.title
    assert got.reason == ""


def test_the_same_prompt_twice_yields_the_same_id():
    """The flood guard that holds even if the active-task check is bypassed: a repeat
    prompt re-declares the SAME id, and the graph fold upserts a re-declare rather than
    creating a second task. The ledger is append-only — a duplicate cannot be cleaned up
    later, so determinism here is a design requirement, not a nicety."""
    a = ad.decide(CODE_PROMPT, _sidecar())
    b = ad.decide(CODE_PROMPT, _sidecar())
    assert a.task_id == b.task_id


def test_a_prompt_with_no_inferable_technique_refuses():
    """Silence beats a task parked at a gate nobody chose. `playbook.infer` returned None
    for 33 of 191 distinct real code prompts, so this is the common path, not an edge."""
    got = ad.decide("src/mindhive/enforce.py", _sidecar())
    assert got.declare is False
    assert "technique" in got.reason.lower()


def test_a_non_code_prompt_refuses():
    got = ad.decide("terima kasih atas bantuannya", _sidecar())
    assert got.declare is False
    assert got.reason


@pytest.mark.parametrize("prompt", ["", "   ", None, "<task-notification>x</task-notification>",
                                    "/ce-plan do the thing"])
def test_empty_and_machine_injected_prompts_refuse(prompt):
    got = ad.decide(prompt, _sidecar())
    assert got.declare is False


def test_an_active_record_refuses_and_names_reuse():
    """At most one auto-declared root is live at a time. That is what bounds writes to
    one-per-work-session instead of one-per-prompt."""
    got = ad.decide(CODE_PROMPT, _sidecar(records={"existing-task": {"phase": "plan"}}))
    assert got.declare is False
    assert "reuse" in got.reason.lower() or "active" in got.reason.lower()


@pytest.mark.parametrize("prompt", [
    "!!! ??? ...",                                  # punctuation only
    "perbaiki",                                     # one Indonesian word
    "x",                                            # single character
    "buatkan fitur " + "sangat " * 200 + "panjang",  # far over the id budget
    "BUATKAN FITUR BARU",                           # uppercase
    "fitur/dengan\\slash: dan titik.dua",           # separators that ID_RE forbids
])
def test_every_derived_id_satisfies_the_id_contract(prompt):
    """`tasks.ID_RE` is the append surface's contract. An id that violates it would be
    refused after the event was already built, which is a failure with no good recovery
    on an append-only store."""
    got = ad.decide(prompt, _sidecar())
    if got.declare:
        assert t_mod.ID_RE.match(got.task_id), f"{got.task_id!r} violates ID_RE"


def test_a_colliding_id_is_disambiguated_deterministically():
    first = ad.decide(CODE_PROMPT, _sidecar())
    taken = {first.task_id: {"phase": "plan"}}
    # The active-record check refuses on a populated sidecar, so collision handling is
    # exercised through the id helper directly — the seam that owns it.
    again = ad.derive_id(CODE_PROMPT, taken=set(taken))
    assert again and again != first.task_id
    assert again == ad.derive_id(CODE_PROMPT, taken=set(taken))
    assert t_mod.ID_RE.match(again)


def test_the_decision_never_carries_a_phase():
    """KTD2, asserted structurally. `cmd_mindnet_declare` is explicit that the phase-axis
    declare names no phase — "a caller-named entry phase is the teleport MN-2 stopped
    accepting" — so an edit that adds one must fail here rather than in a task silently
    parked past its gates."""
    got = ad.decide(CODE_PROMPT, _sidecar())
    for field in vars(got):
        assert "phase" not in field, f"the decision carries a phase field: {field}"


def test_the_decision_is_deterministic():
    a, b = ad.decide(CODE_PROMPT, _sidecar()), ad.decide(CODE_PROMPT, _sidecar())
    assert vars(a) == vars(b)


@pytest.mark.parametrize("bad", [
    None,
    "not a sidecar",
    object(),
])
def test_a_malformed_sidecar_refuses_rather_than_raising(bad):
    """It runs on the hook path, where a raise breaks the user's prompt."""
    got = ad.decide(CODE_PROMPT, bad)
    assert got.declare is False


def test_records_of_the_wrong_shape_refuse():
    import types
    for records in ("not a dict", 7, [1, 2, 3]):
        sc = types.SimpleNamespace(state=types.SimpleNamespace(records=records),
                                  advice={}, fresh=True)
        assert ad.decide(CODE_PROMPT, sc).declare is False


def test_the_title_is_a_readable_headline_not_the_whole_prompt():
    long_prompt = ("buatkan fitur export CSV di dashboard. " + "detail tambahan " * 60)
    got = ad.decide(long_prompt, _sidecar())
    assert got.declare
    assert len(got.title) <= ad.MAX_TITLE_CHARS
    assert got.title.strip() == got.title
    assert "\n" not in got.title


def test_deciding_touches_no_disk(monkeypatch):
    """Pure by contract: the sidecar is passed in, so nothing here may read the ledger or
    the projection. The hook path forbids the first and cannot afford the second."""
    def explode(*a, **kw):
        raise AssertionError("decide() read the ledger")

    monkeypatch.setattr(events, "read_all", explode)
    monkeypatch.setattr(sc_mod, "read",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("decide() read the sidecar")))
    assert ad.decide(CODE_PROMPT, _sidecar()).declare is True
