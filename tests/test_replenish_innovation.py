# tests/test_replenish_innovation.py
"""The one agentic lens, driven by a stub runner.

No test here makes a model call. `LensContext.runner` exists for exactly that
reason: a lens that resolved its own transport could only be tested by spending
money, and on this machine headless `claude` cannot authenticate at all.

Three properties are under test, and they are the three that make a model-driven
lens safe to run inside an autonomous loop:

**Its evidence is ours.** A candidate cites the changed paths and failure
signatures this process put IN the prompt. The model's claims about the record are
precisely what cannot be checked, so a candidate whose evidence came from the
reply would launder an invention into the audit row.

**Failure raises; emptiness returns.** A broken call must read as "the lens
failed", never as "the system is robust".

**The graph checks do not transfer.** `decompose.validate` checks proposals
against a task graph. A replenishment round has no graph — these are flat
candidates for a checklist — so what is reused is the tolerant parser and the id
slug, and nothing pretends to check cycles over a list with no edges.
"""
import json

import pytest

from mindhive import events, replenish

CHANGED = ("src/mindhive/replenish.py", "src/mindhive/loop/engine.py")


def _reply(*titles, description="because the record says so"):
    body = {"subtasks": [{"title": t, "description": description}
                         for t in titles]}
    return {"exit": 0, "stdout": json.dumps(body), "usage": None,
            "session_id": None}


def _ctx(runner, changed=CHANGED, evs=()):
    return replenish.LensContext(evs=tuple(evs), changed_paths=changed,
                                 runner=runner)


def _failure(ts, signature):
    return events.append_event("tool_failed",
                               {"text": "boom", "signature": signature,
                                "action": "do a thing"}, ts=ts)


# --- the happy path -----------------------------------------------------------

def test_two_valid_proposals_yield_two_candidates():
    cands = replenish.innovation(
        _ctx(lambda _p: _reply("Harden the fold", "Parameterise the cap")))
    assert len(cands) == 2
    assert all(c.lens == "innovation" for c in cands)
    assert all(c.evidence for c in cands)


def test_the_title_carries_the_description():
    cands = replenish.innovation(
        _ctx(lambda _p: _reply("Harden the fold", description="malformed rows")))
    assert "Harden the fold" in cands[0].title
    assert "malformed rows" in cands[0].title


def test_a_reply_wrapped_in_prose_and_fences_still_parses():
    def runner(_prompt):
        return {"exit": 0, "usage": None, "session_id": None,
                "stdout": ("Here is what I would do next:\n```json\n"
                           + json.dumps({"subtasks": [{"title": "Do the thing"}]})
                           + "\n```\nHope that helps.")}

    assert len(replenish.innovation(_ctx(runner))) == 1


def test_the_fingerprint_is_the_slug_so_the_same_proposal_dedups():
    first = replenish.innovation(_ctx(lambda _p: _reply("Harden the fold")))
    second = replenish.innovation(_ctx(lambda _p: _reply("Harden the fold")))
    assert first[0].fingerprint == second[0].fingerprint
    # Keyed on the slug, which the candidate also carries as its deferral key —
    # asserted exactly rather than by prefix, because the fingerprint is a digest.
    assert first[0].fingerprint == replenish.generate_fingerprint(
        "innovation", (first[0].deferral_key,))


def test_two_proposals_with_the_same_title_get_distinct_ids():
    cands = replenish.innovation(
        _ctx(lambda _p: _reply("Same thing", "Same thing")))
    assert len({c.fingerprint for c in cands}) == len(cands)


def test_more_than_five_proposals_are_truncated_to_five():
    cands = replenish.innovation(
        _ctx(lambda _p: _reply(*[f"Idea {i}" for i in range(9)])))
    assert len(cands) == replenish.INNOVATION_MAX_PROPOSALS == 5


# --- the evidence is ours, not the model's ------------------------------------

def test_evidence_cites_the_changed_paths_that_went_into_the_prompt():
    cands = replenish.innovation(_ctx(lambda _p: _reply("Do the thing")))
    assert any("src/mindhive/replenish.py" in e for e in cands[0].evidence)


def test_evidence_cites_recorded_failure_signatures():
    _failure("2026-08-02T00:00:00Z", "edit:not-read")
    cands = replenish.innovation(
        _ctx(lambda _p: _reply("Do the thing"), changed=(),
             evs=events.read_all()))
    assert any("edit:not-read" in e for e in cands[0].evidence)


def test_a_model_supplied_evidence_field_is_ignored():
    # The model may say anything. Nothing it claims about the record becomes
    # evidence, because nothing it claims can be checked.
    def runner(_prompt):
        body = {"subtasks": [{"title": "Do the thing",
                              "evidence": ["ledger row 999 says so"]}]}
        return {"exit": 0, "stdout": json.dumps(body), "usage": None,
                "session_id": None}

    cands = replenish.innovation(_ctx(runner))
    assert not any("999" in e for e in cands[0].evidence)


def test_with_nothing_to_cite_the_lens_does_not_even_ask():
    # Spending a model call to produce candidates the gate must then refuse for
    # having no evidence is pure waste.
    called = {"n": 0}

    def runner(_prompt):
        called["n"] += 1
        return _reply("Do the thing")

    assert replenish.innovation(_ctx(runner, changed=(), evs=())) == []
    assert called["n"] == 0


# --- failure raises so the lens-error channel sees it -------------------------

def test_a_non_zero_exit_raises():
    with pytest.raises(RuntimeError, match="exited 1"):
        replenish.innovation(_ctx(
            lambda _p: {"exit": 1, "stdout": "Not logged in", "usage": None,
                        "session_id": None}))


def test_a_timeout_exit_code_raises():
    with pytest.raises(RuntimeError, match="exited 124"):
        replenish.innovation(_ctx(
            lambda _p: {"exit": 124, "stdout": "", "usage": None,
                        "session_id": None}))


def test_unparseable_output_raises_with_the_parsers_reason():
    with pytest.raises(RuntimeError, match="no json object"):
        replenish.innovation(_ctx(
            lambda _p: {"exit": 0, "stdout": "I would rather not.",
                        "usage": None, "session_id": None}))


def test_a_reply_with_no_subtasks_key_raises():
    with pytest.raises(RuntimeError, match="subtasks"):
        replenish.innovation(_ctx(
            lambda _p: {"exit": 0, "stdout": '{"ideas": []}', "usage": None,
                        "session_id": None}))


def test_a_runner_returning_the_wrong_shape_raises():
    with pytest.raises(RuntimeError, match="not a result mapping"):
        replenish.innovation(_ctx(lambda _p: "just a string"))


def test_an_empty_subtasks_list_returns_nothing_and_does_not_raise():
    # A model that legitimately proposes nothing is not a failure. Collapsing the
    # two would make a broken lens read as a robust system.
    assert replenish.innovation(
        _ctx(lambda _p: {"exit": 0, "stdout": '{"subtasks": []}',
                         "usage": None, "session_id": None})) == []


def test_a_proposal_with_no_title_is_skipped():
    def runner(_prompt):
        body = {"subtasks": [{"title": "", "description": "d"},
                             {"title": "Real one"}]}
        return {"exit": 0, "stdout": json.dumps(body), "usage": None,
                "session_id": None}

    cands = replenish.innovation(_ctx(runner))
    assert [c.title for c in cands] == ["Real one"]


def test_a_lens_failure_reaches_the_round_as_one_recorded_error():
    res = replenish.run_round(
        [], surface="loop", sink=lambda kept: ([], 0),
        lenses={"innovation": replenish.innovation},
        ctx=_ctx(lambda _p: {"exit": 1, "stdout": "nope", "usage": None,
                             "session_id": None}))
    assert [name for name, _msg in res.lens_errors] == ["innovation"]
    assert res.landed == []


# --- the prompt ---------------------------------------------------------------

def test_the_prompt_states_the_shape_and_the_cap():
    prompt = replenish.innovation_prompt(list(CHANGED), ["edit:not-read"], [])
    assert '{"subtasks":' in prompt
    assert str(replenish.INNOVATION_MAX_PROPOSALS) in prompt
    assert "src/mindhive/replenish.py" in prompt
    assert "edit:not-read" in prompt


def test_the_prompt_forbids_extra_keys():
    prompt = replenish.innovation_prompt(list(CHANGED), [], [])
    assert "any key other than subtasks" in prompt


def test_the_prompt_says_none_when_nothing_changed():
    assert "- (none)" in replenish.innovation_prompt([], ["sig"], [])


# --- scoring and registration -------------------------------------------------

def test_a_proposal_never_claims_severity():
    # The record cannot say a suggestion about the future is blocking anything.
    # Asserting it would put a fabricated +20 on every model proposal.
    cand = replenish.innovation(_ctx(lambda _p: _reply("Do the thing")))[0]
    assert cand.severity is False
    assert replenish.score_candidate(cand).severity == 0


def test_a_proposal_scores_by_how_much_record_it_rests_on():
    # Two changed paths cited: base 10 + one repeat = 15.
    cand = replenish.innovation(_ctx(lambda _p: _reply("Do the thing")))[0]
    assert cand.occurrences == len(cand.evidence) == 2
    assert replenish.score_candidate(cand).total == 15


def test_a_weakly_grounded_proposal_does_not_clear_the_ambitious_threshold():
    # One cited fact scores the bare base, which is below 15. A lens that could
    # always land regardless of grounding would be a quota-filler.
    cand = replenish.innovation(
        _ctx(lambda _p: _reply("Do the thing"),
             changed=("src/mindhive/replenish.py",)))[0]
    assert len(cand.evidence) == 1
    assert (replenish.score_candidate(cand).total
            < replenish.PROFILES["ambitious"].min_score)


def test_a_well_grounded_proposal_clears_ambitious_but_not_balanced():
    cand = replenish.innovation(_ctx(lambda _p: _reply("Do the thing")))[0]
    score = replenish.score_candidate(cand).total
    assert score >= replenish.PROFILES["ambitious"].min_score
    assert score < replenish.PROFILES["balanced"].min_score


def test_the_lens_is_registered():
    assert replenish.LENSES["innovation"] is replenish.innovation


def test_the_default_runner_is_the_shared_headless_helper():
    # Not a private reimplementation of subprocess handling: `claude_cli` owns exe
    # resolution, the recursion guard, and envelope parsing for every caller.
    import inspect
    source = inspect.getsource(replenish._default_innovation_runner)
    assert "claude_cli" in source and "run_headless" in source
