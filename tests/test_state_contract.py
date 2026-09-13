# tests/test_state_contract.py
"""The `state_set` contract, and the bound on the lists it renders.

`state_set` was the one semantic type with no entry in
`events._PAYLOAD_CONTRACTS`. That was not an oversight with no cause: every
other registry has an owning module that registered its own `validate_payload`,
and `state_set` had no module, because it has no subcommand -- `hive append
state_set '{…}' --json` is its only writer. So the surface with the highest
trust in the system (the top of STATE.md, which `sync` embeds verbatim in the
auto-loaded MEMORY.md) was the one surface nothing validated.

WHAT THESE TESTS ARE FOR, stated as the failure they must catch. The last
review's sharpest finding on this branch was a sanitizer proven as a UNIT and
never as WIRING: every call site could be reverted and the suite stayed green.
So the file is built in two halves, and the second is the one that matters.

  * `validate_payload` as a unit -- every refusal is a shape somebody can
    actually write, not a type-checker exercise.
  * The SAME refusals driven through `hive append`, and the bullet bound driven
    through the real `projector.build_state` and `briefing.build_briefing`
    output. Revert the registry row and the CLI tests go red; revert
    `safe_bullets`' limit and the render tests go red; revert `state.fields` to
    a bare `.payload` and the non-dict tests go red.

THE BOUND IS NOT TRUNCATION. `safe_bullets` capped each item's WIDTH and
nothing about item COUNT, so one `state_set` carrying 50,000 open_questions was
50,000 bullets at the top of the auto-loaded context. Both halves close it and
they close it differently on purpose: the write gate REFUSES (the caller still
holds the value and can act on the error), the renderer ELIDES and NAMES what
it elided (it is reading rows already in the ledger, which no write gate can
reach, and an operator who sees 20 questions with no marker believes there are
20). `test_the_gate_and_the_renderer_cannot_drift` is what keeps the two
numbers one number.
"""
import pytest

from mindhive import (__main__ as cli_mod, briefing, events, projector,
                      provenance, state)

NL = chr(10)
HUMAN = "39151937-fd17-48c0-bb12-fd5e596b1c1f"


def _raw(payload, session_id=HUMAN):
    """Append UNGATED, the way the rows that predate the contract got in.

    `events.append_event` is deliberately type-agnostic -- the gate lives at the
    two raw-append surfaces, not in the store -- so this is how a renderer test
    reaches a payload the gate would now refuse."""
    return events.append_event("state_set", payload, session_id=session_id)


# --- the registered contract -------------------------------------------------

def test_state_set_has_a_registered_contract_row():
    """The row itself. Without it `validate_typed_payload` returns silently for
    this type and every CLI test below would pass against no validator at
    all."""
    assert "state_set" in events._PAYLOAD_CONTRACTS
    module, hint = events._PAYLOAD_CONTRACTS["state_set"]
    assert module == ".state"
    assert "hive append state_set" in hint


def test_validate_typed_payload_dispatches_to_state():
    with pytest.raises(ValueError) as e:
        events.validate_typed_payload("state_set", ["not", "an", "object"])
    assert "must be an object" in str(e.value)      # the validator's reason
    assert "hive append state_set" in str(e.value)  # the registry's hint


# --- the refusals, as shapes somebody writes ---------------------------------

BAD = {
    "a list payload":            ["task", "x"],
    "a string payload":          "the whole state as one string",
    "task as a dict":            {"task": {"text": "x"}},
    "verified as a number":      {"verified": 7},
    "next_step as a list":       {"next_step": ["a", "b"]},
    "task over the width":       {"task": "x" * 2001},
    # The headline mistake: this reads perfectly and renders one bullet per
    # character.
    "open_questions as a string": {"open_questions": "one thing"},
    "deferred as bytes":         {"deferred": b"one thing"},
    "open_questions as a dict":  {"open_questions": {"a": 1}},
    "too many questions":        {"open_questions": ["q"] * 21},
    "a non-text entry":          {"deferred": ["fine", 3]},
    "an entry over the width":   {"open_questions": ["x" * 2001]},
    # Not a type error at all -- a payload the renderers cannot read, which
    # REPLACES the state block rather than sitting beside it.
    "the text wrapper":          {"text": "I meant to append a decision"},
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_the_contract_refuses(name):
    with pytest.raises(ValueError):
        state.validate_payload(BAD[name])


def test_every_refusal_says_what_to_do_instead():
    """A refusal an operator cannot act on is a worse outcome than the write.
    Each message must name the type and the reason, not just fail."""
    for name, payload in BAD.items():
        with pytest.raises(ValueError) as e:
            state.validate_payload(payload)
        msg = str(e.value)
        assert msg.startswith("state_set"), (name, msg)
        assert len(msg) > 40, (name, msg)


# --- the acceptances, which are what stop this being a filter that eats -------

GOOD = {
    "empty is an explicit clear": {},
    "one field":                  {"task": "ship the branch"},
    "every field": {"task": "t", "verified": "v", "next_step": "n",
                    "open_questions": ["q"], "deferred": ["d"]},
    "explicit nulls":             {"task": None, "open_questions": None},
    "empty lists":                {"open_questions": [], "deferred": []},
    # Forward compatibility: the store is append-only, so a contract that
    # refused tomorrow's key alongside a known one would refuse to read back
    # yesterday's events.
    "a future key beside a known one": {"task": "t", "blockers": ["x"]},
}


@pytest.mark.parametrize("name", sorted(GOOD))
def test_the_contract_accepts(name):
    state.validate_payload(GOOD[name])


def test_the_boundary_is_the_cap_not_one_below_it():
    """DISCRIMINATING pair. Both halves, or an off-by-one in either direction
    passes: a cap of MAX_ITEMS-1 satisfies every refusal test above."""
    state.validate_payload({"open_questions": ["q"] * state.MAX_ITEMS})
    with pytest.raises(ValueError):
        state.validate_payload({"open_questions": ["q"] * (state.MAX_ITEMS + 1)})
    state.validate_payload({"task": "x" * state.MAX_FIELD_CHARS})
    with pytest.raises(ValueError):
        state.validate_payload({"task": "x" * (state.MAX_FIELD_CHARS + 1)})


# --- WIRING: the same refusals, through the CLI -------------------------------

def test_cli_append_state_set_malformed_exits_2(capsys):
    """THE WIRING TEST. `hive append` is the only writer this type has, so a
    contract it does not reach is a contract that does not exist. Delete the
    `_PAYLOAD_CONTRACTS` row and this goes red while every unit test above
    stays green -- which is exactly the gap this file was written for."""
    bad = '{"open_questions": "one thing"}'
    assert cli_mod.main(["append", "state_set", bad, "--json"]) == 2
    err = capsys.readouterr().err
    assert "one bullet per character" in err     # the validator's own reason
    assert "hive append state_set" in err        # the corrective hint
    assert events.read_all() == []               # nothing entered the chain


def test_cli_append_refuses_the_payload_that_blanks_the_state(capsys):
    """`dashboard_post` wraps a plain string as a `text` field, and `state_set`
    is last-one-wins, so this shape does not add a useless row -- it REPLACES
    the operator's whole state block with an empty one, silently."""
    assert cli_mod.main(
        ["append", "state_set", '{"text": "oops"}', "--json"]) == 2
    assert "REPLACES" in capsys.readouterr().err
    assert events.read_all() == []


def test_cli_append_state_set_wellformed_still_lands(capsys):
    """CANARY. A gate that refused everything would satisfy both tests above
    and would also make `hive append state_set` -- the only way anyone records
    state at all -- permanently unusable."""
    ok = '{"task": "ship the branch", "open_questions": ["is it green?"]}'
    assert cli_mod.main(["append", "state_set", ok, "--json"]) == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].payload["task"] == "ship the branch"
    state.validate_payload(evs[0].payload)       # it round-trips its own gate


# --- WIRING: `state.fields` is what both renderers actually call --------------

@pytest.mark.parametrize("payload", [["a", "list"], "a string", 7])
def test_build_state_is_total_over_a_payload_that_predates_the_gate(payload):
    """The gate guards WRITES; this fold runs over rows already in the ledger.
    A renderer on the projection path that raises does not produce an error
    message, it produces no STATE.md at all -- so reverting `state.fields` to a
    bare `latest_state.payload` must go red here."""
    _raw(payload)
    out = projector.build_state(events.read_all())
    assert out.startswith("# STATE")
    assert "**Active task:** (none)" in out       # empty, not crashed


@pytest.mark.parametrize("payload", [["a", "list"], "a string", 7])
def test_build_briefing_is_total_over_the_same_row(payload):
    _raw(payload)
    out = briefing.build_briefing(events.read_all())
    assert "**task:** (none)" in out


def test_canary_the_renderers_still_render_a_real_state():
    """Without this, a `fields` that returned an empty dict unconditionally
    would satisfy every assertion above."""
    _raw({"task": "ship the branch", "verified": "suite green",
          "next_step": "open the PR", "open_questions": ["is it green?"],
          "deferred": ["the other thing"]})
    evs = events.read_all()
    st = projector.build_state(evs)
    br = briefing.build_briefing(evs)
    for text in (st, br):
        assert "ship the branch" in text
        assert "open the PR" in text
        assert "is it green?" in text
        assert "the other thing" in text
    assert "suite green" in st


# --- WIRING: the bullet bound, on the surface it exists to protect ------------

def test_an_over_long_question_list_is_bounded_at_the_rendered_surface():
    """The defect, end to end: one row with 50,000 questions was 50,000 bullets
    in the auto-loaded MEMORY.md. Drop the `limit` from `safe_bullets` and this
    goes red -- the unit test below alone would not."""
    _raw({"open_questions": [f"question {i}" for i in range(50)]})
    out = projector.build_state(events.read_all())
    bullets = [ln for ln in out.splitlines() if ln.startswith("- question ")]
    assert len(bullets) == provenance.MAX_BULLETS
    assert "question 0" in out and "question 19" in out
    assert "question 20" not in out


def test_what_was_elided_is_named_never_silently_dropped():
    """DISCRIMINATING against a plain slice, which satisfies the count
    assertion above exactly. "Never exclude silently" is this codebase's rule
    for a withheld SOP; a bullet list is not an exception to it."""
    _raw({"deferred": [f"item {i}" for i in range(50)]})
    out = projector.build_state(events.read_all())
    assert f"- … {50 - provenance.MAX_BULLETS} more (not shown)" in out


def test_the_briefing_is_bounded_by_the_same_rule():
    """Two renderers read this payload. A bound on one of them moves the
    problem rather than fixing it."""
    _raw({"open_questions": [f"question {i}" for i in range(50)]})
    out = briefing.build_briefing(events.read_all())
    bullets = [ln for ln in out.splitlines() if ln.startswith("- question ")]
    assert len(bullets) == provenance.MAX_BULLETS
    assert "more (not shown)" in out


def test_canary_a_short_list_is_rendered_whole_and_unmarked():
    """A marker on every list, or a cap that fired at zero, would satisfy both
    tests above while making the surface useless."""
    _raw({"open_questions": ["only", "three", "questions"]})
    out = projector.build_state(events.read_all())
    assert "- only" in out and "- three" in out and "- questions" in out
    assert "not shown" not in out


# --- the two halves are one number --------------------------------------------

def test_the_gate_and_the_renderer_cannot_drift():
    """The write gate refuses above MAX_ITEMS and the renderer elides above
    MAX_BULLETS. If those ever became two numbers, one of them would be dead:
    a lower gate makes the elision unreachable, a lower elision hides rows the
    gate just accepted."""
    assert state.MAX_ITEMS == provenance.MAX_BULLETS
    assert state.MAX_FIELD_CHARS == provenance.FIELD_CHARS
    # Not just equal constants -- the behaviours meet at the same point. A list
    # the gate accepts is a list the renderer shows whole.
    at_cap = [f"q{i}" for i in range(state.MAX_ITEMS)]
    state.validate_payload({"open_questions": at_cap})
    assert "not shown" not in " ".join(provenance.safe_bullets(at_cap))


def test_safe_bullets_bounds_the_count_as_a_unit():
    """The unit half, kept because the renderers are not the only future
    caller of this helper."""
    out = provenance.safe_bullets([f"q{i}" for i in range(25)])
    assert len(out) == provenance.MAX_BULLETS + 1
    assert out[-1] == "… 5 more (not shown)"
    assert provenance.safe_bullets(["a", "b"]) == ["a", "b"]
    assert provenance.safe_bullets([]) == ["(none)"]
    assert provenance.safe_bullets(None, empty="") == [""]


def test_the_briefings_deferred_list_is_bounded_too():
    """The briefing has TWO bullet lists and the bound was only proven on one
    of them here; the deferred-side proof ran through `projector.build_state`.
    Reverting `safe_bullets` at briefing.py's `deferred` line drops the bound
    AND the sanitizer together, and nothing was watching."""
    _raw({"deferred": [f"item {i}" for i in range(50)]})
    out = briefing.build_briefing(events.read_all())
    bullets = [ln for ln in out.splitlines() if ln.startswith("- item ")]
    assert len(bullets) == provenance.MAX_BULLETS
    assert "more (not shown)" in out


def test_the_briefing_renders_every_one_of_the_five_fields():
    """CANARY with teeth. The earlier canary set all five fields but pinned
    `verified` on the PROJECTOR only, so briefing.py's `verified` line could be
    DELETED -- not merely reverted -- with the suite staying green."""
    _raw({"task": "t-value", "verified": "v-value", "next_step": "n-value",
          "open_questions": ["q-value"], "deferred": ["d-value"]})
    out = briefing.build_briefing(events.read_all())
    for expected in ("t-value", "v-value", "n-value", "q-value", "d-value"):
        assert expected in out, f"the briefing dropped {expected}"


def test_a_bullet_cannot_mint_the_renderers_withholding_notice():
    """F4. `safe_bullets` appends "… N more (not shown)" as a claim the
    RENDERER makes about rows it dropped. `safe_line` stripped `AGENT_MARK` --
    the other token only the renderer may emit -- and not this one, so an item
    whose text IS the marker rendered verbatim and told the operator that 31
    questions were withheld when nothing was.

    Scope, stated because the review split on it: `(none)` is deliberately NOT
    stripped. It appears only for an EMPTY list, so an item reading "(none)"
    says of a one-item list exactly what the renderer says of a zero-item one —
    there is no false claim in it."""
    _raw({"open_questions": ["Is the suite green?",
                             provenance.ELISION.format(n=31)]})
    out = projector.build_state(events.read_all())
    assert "Is the suite green?" in out
    assert "31 more (not shown)" not in out


def test_canary_the_renderers_own_withholding_notice_still_renders():
    """Without this, a strip that ate the marker unconditionally would satisfy
    the test above and silently restore the 50,000-bullet defect."""
    _raw({"open_questions": [f"q{i}" for i in range(30)]})
    out = projector.build_state(events.read_all())
    assert provenance.ELISION.format(n=10) in out


# --- the injection guard the contract does NOT replace ------------------------

def test_the_contract_does_not_make_the_renderer_sanitizer_redundant():
    """A well-formed `state_set` can still carry a forged Markdown line: a
    newline inside `task` is a perfectly valid string. The gate is about SHAPE;
    `provenance.safe_line` is about CONTENT, and reading the new contract as
    permission to drop the sanitizer would reopen
    `tests/test_provenance_surfaces.py`'s whole subject."""
    forged = "Innocuous" + NL + "## FORGED HEADING"
    state.validate_payload({"task": forged})     # shape-legal, on purpose
    _raw({"task": forged})
    out = projector.build_state(events.read_all())
    assert "Innocuous" in out
    assert NL + "## FORGED HEADING" not in out
