# tests/test_replenish_sink.py
"""The roadmap sink and the permanent `new` step flag.

Two things are being protected here. The flag threads through a fold that every
projection reads — the Oversee API, the STATE.md notice, and the loop's
interruption snapshot — so the characterization tests come first: a step with no
`new` key must fold exactly as it did before the key existed.

And `new` is provenance, not recency. It says "this was not in the plan the work
started from". Completing the step does not make that untrue, so nothing clears
it.
"""
import pytest

from mindhive import events, replenish, replenish_sinks, roadmap

SID = "sess-0001"


def _set(steps, plan_id=SID, prompt="build the thing", tags=("roadmap", "auto")):
    payload = {"action": "set", "plan_id": plan_id, "prompt": prompt,
               "title": prompt, "steps": steps}
    roadmap.validate_payload(payload)
    return events.append_event("roadmap", payload, session_id=plan_id,
                               tags=list(tags))


def _rec(plan_id=SID):
    return roadmap.fold(events.read_all()).get(plan_id)


def _cand(fp="fp-1", title="Run the test suite"):
    return replenish.Candidate(lens="evidence-gap", title=title,
                               fingerprint=fp, evidence=("a real pointer",))


# --- characterization: the fold before and after the new key -----------------

def test_a_step_with_no_new_key_folds_as_not_new():
    _set([{"text": "step one", "status": "active"}])
    assert _rec()["steps"][0]["new"] is False


def test_a_plain_string_step_folds_as_not_new():
    _set(["step one"])
    step = _rec()["steps"][0]
    assert step["new"] is False
    assert step["text"] == "step one"
    assert step["status"] == "active"       # unchanged auto-active behavior


def test_existing_step_fields_are_untouched():
    _set([{"text": "step one", "status": "done", "note": "shipped"}])
    step = _rec()["steps"][0]
    assert (step["text"], step["status"], step["note"]) \
        == ("step one", "done", "shipped")


# --- the flag itself ----------------------------------------------------------

def test_a_new_step_survives_the_fold():
    _set([{"text": "injected", "status": "pending", "new": True}])
    assert _rec()["steps"][0]["new"] is True


def test_a_non_boolean_new_is_refused():
    with pytest.raises(ValueError, match="true or false"):
        roadmap.validate_payload(
            {"action": "set", "plan_id": SID, "prompt": "p",
             "steps": [{"text": "t", "new": "yes"}]})


def test_completing_a_new_step_does_not_clear_the_flag():
    # Provenance, not recency. This is the assertion that fails if someone later
    # "tidies up" the flag on completion.
    _set([{"text": "injected", "status": "pending", "new": True}])
    events.append_event("roadmap",
                        {"action": "step", "plan_id": SID, "index": 0,
                         "status": "done"}, session_id=SID, tags=["roadmap"])
    step = _rec()["steps"][0]
    assert step["status"] == "done"
    assert step["new"] is True


def test_the_relay_prompt_marks_injected_steps():
    # This is the leg that delivers an interrupted plan back INTO Claude. An
    # agent picking the work up could not tell injected work from the plan the
    # session started with — the one renderer where that matters most.
    from mindhive import autoresume
    _set([{"text": "from the plan", "status": "done"},
          {"text": "injected work", "status": "pending", "new": True}])
    events.append_event("roadmap",
                        {"action": "interrupt", "plan_id": SID,
                         "cause": "api_cap"}, session_id=SID, tags=["roadmap"])
    text = autoresume.resume_prompt(_rec())
    assert "(new) injected work" in text
    assert "(new) from the plan" not in text


def test_the_ledger_body_marks_injected_steps():
    # The LEDGER projection feeds keyword recall; without the marker, recall
    # cannot tell injected work from planned work.
    body = roadmap.ledger_body(
        {"action": "set", "plan_id": SID, "prompt": "ask",
         "steps": [{"text": "from the plan", "status": "done"},
                   {"text": "injected work", "status": "pending", "new": True}]})
    assert "(new) injected work" in body
    assert "(new) from the plan" not in body


def test_roadmap_show_marks_injected_steps(capsys):
    from mindhive.__main__ import main
    _set([{"text": "injected work", "status": "pending", "new": True}])
    assert main(["roadmap", "show", SID]) == 0
    assert "(new) injected work" in capsys.readouterr().out


def test_every_renderer_shares_one_marker():
    # Three renderers shipped without the marker because it was three separate
    # string literals. A fourth must not be able to drift the same way.
    assert roadmap.new_prefix({"new": True}) == roadmap.NEW_MARKER
    assert roadmap.new_prefix({"new": False}) == ""
    assert roadmap.new_prefix({}) == ""
    assert roadmap.new_prefix(None) == ""


def test_the_state_notice_distinguishes_a_new_step():
    _set([{"text": "from the plan", "status": "done"},
          {"text": "injected work", "status": "pending", "new": True}])
    events.append_event("roadmap",
                        {"action": "interrupt", "plan_id": SID,
                         "cause": "session_end"}, session_id=SID,
                        tags=["roadmap"])
    text = roadmap.notice(events.read_all())
    assert "(new) injected work" in text
    assert "(new) from the plan" not in text


# --- the sink -----------------------------------------------------------------

def test_the_sink_creates_a_plan_when_the_session_has_none():
    # AE1: a session that changed code without verifying it ends with work on
    # the board, and the board outlives the session.
    events.append_event("user_prompt", {"text": "add the parser"},
                        session_id=SID)
    placements, dropped = replenish_sinks.roadmap_sink(SID)([_cand()])
    assert dropped == 0
    rec = _rec()
    assert len(rec["steps"]) == 1
    assert rec["steps"][0]["new"] is True
    assert rec["steps"][0]["text"] == "Run the test suite"
    assert rec["prompt"] == "add the parser"
    assert placements[0].plan_id == SID and placements[0].step_index == 0


def test_the_sink_appends_without_disturbing_existing_steps():
    _set([{"text": "one", "status": "done", "note": "n1"},
          {"text": "two", "status": "active"},
          {"text": "three", "status": "pending"}])
    replenish_sinks.roadmap_sink(SID)([_cand()])
    steps = _rec()["steps"]
    assert len(steps) == 4
    assert [(s["text"], s["status"], s["note"], s["new"]) for s in steps[:3]] \
        == [("one", "done", "n1", False), ("two", "active", "", False),
            ("three", "pending", "", False)]
    assert steps[3]["new"] is True


def test_the_sink_preserves_an_earlier_injections_flag():
    _set([{"text": "older injection", "status": "pending", "new": True}])
    replenish_sinks.roadmap_sink(SID)([_cand("fp-2", "second")])
    steps = _rec()["steps"]
    assert [s["new"] for s in steps] == [True, True]


def test_the_sink_keeps_the_existing_plans_own_ask():
    _set([{"text": "one", "status": "active"}], prompt="the original ask")
    replenish_sinks.roadmap_sink(SID)([_cand()])
    assert _rec()["prompt"] == "the original ask"


def test_the_sink_tags_the_plan_so_autoclose_will_close_it():
    # Without AUTO_TAG the plan is treated as human-declared, autoclose skips
    # it, and the work never reaches STATE.md — a silent dead end.
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    replenish_sinks.roadmap_sink(SID)([_cand()])
    assert roadmap.AUTO_TAG in _rec()["tags"]
    roadmap.autoclose_session(SID)
    assert _rec()["status"] == "interrupted"


def test_the_injected_step_reaches_the_state_notice_end_to_end():
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    replenish_sinks.roadmap_sink(SID)([_cand()])
    roadmap.autoclose_session(SID)
    assert "(new) Run the test suite" in roadmap.notice(events.read_all())


def test_the_sink_reports_what_did_not_fit():
    _set([{"text": f"s{i}", "status": "pending"}
          for i in range(roadmap.MAX_STEPS)])
    placements, dropped = replenish_sinks.roadmap_sink(SID)(
        [_cand("fp-1"), _cand("fp-2", "second")])
    assert placements == [] and dropped == 2
    assert len(_rec()["steps"]) == roadmap.MAX_STEPS


def test_the_sink_lands_what_fits_and_reports_the_rest():
    _set([{"text": f"s{i}", "status": "pending"}
          for i in range(roadmap.MAX_STEPS - 1)])
    placements, dropped = replenish_sinks.roadmap_sink(SID)(
        [_cand("fp-1"), _cand("fp-2", "second")])
    assert len(placements) == 1 and dropped == 1


def test_a_later_todowrite_sync_does_not_delete_injected_steps():
    # `set` replaces the step list wholesale and sync_from_todos builds that
    # list from TodoWrite alone. One more TodoWrite after an injection — a
    # resumed session is enough — silently deleted the injected work.
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    roadmap.sync_from_todos(SID, [{"content": "todo one",
                                   "status": "in_progress"}])
    replenish_sinks.roadmap_sink(SID)([_cand()])
    roadmap.sync_from_todos(SID, [{"content": "todo one", "status": "completed"},
                                  {"content": "todo two", "status": "pending"}])
    steps = _rec()["steps"]
    assert [s["text"] for s in steps] == ["todo one", "todo two",
                                          "Run the test suite"]
    assert [s["new"] for s in steps] == [False, False, True]


def test_injected_steps_survive_a_todo_list_at_the_max_steps_cap():
    # The tie-break both reviewers caught: the first cut appended carried steps
    # then re-capped, so at exactly MAX_STEPS todos every injected step was
    # dropped — permanently, because the fingerprint was already spent.
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    replenish_sinks.roadmap_sink(SID)([_cand()])
    todos = [{"content": f"todo {i}", "status": "pending"}
             for i in range(roadmap.MAX_STEPS)]
    roadmap.sync_from_todos(SID, todos)
    steps = _rec()["steps"]
    assert len(steps) == roadmap.MAX_STEPS
    assert steps[-1]["text"] == "Run the test suite"
    assert steps[-1]["new"] is True


def test_the_trimmed_todo_is_the_one_that_gives_way():
    # Asymmetric on purpose: a trimmed todo returns on the next TodoWrite, an
    # injected step never does.
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    replenish_sinks.roadmap_sink(SID)([_cand("fp-1", "injected one"),
                                       _cand("fp-2", "injected two")])
    todos = [{"content": f"todo {i}", "status": "pending"}
             for i in range(roadmap.MAX_STEPS)]
    roadmap.sync_from_todos(SID, todos)
    texts = [s["text"] for s in _rec()["steps"]]
    assert texts[-2:] == ["injected one", "injected two"]
    assert "todo 47" in texts and "todo 48" not in texts


def test_a_todo_matching_an_injected_step_inherits_the_flag():
    # Copying "(new) Run the test suite" out of STATE.md into TodoWrite must not
    # quietly downgrade it to ordinary planned work.
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    replenish_sinks.roadmap_sink(SID)([_cand()])
    roadmap.sync_from_todos(SID, [{"content": "Run the test suite",
                                   "status": "in_progress"}])
    steps = _rec()["steps"]
    assert len(steps) == 1
    assert steps[0]["status"] == "active"      # _TODO_STATUS maps in_progress
    assert steps[0]["new"] is True


def test_a_todowrite_sync_that_changes_nothing_stays_a_no_op():
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    roadmap.sync_from_todos(SID, [{"content": "todo one", "status": "pending"}])
    replenish_sinks.roadmap_sink(SID)([_cand()])
    before = len([e for e in events.read_all() if e.type == "roadmap"])
    assert roadmap.sync_from_todos(
        SID, [{"content": "todo one", "status": "pending"}]) is None
    assert len([e for e in events.read_all() if e.type == "roadmap"]) == before


def test_append_false_previews_without_writing_the_plan():
    # The core's own append flag governs only the audit rows. Without this one a
    # --dry-run would still leave a real roadmap step behind.
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    placements, dropped = replenish_sinks.roadmap_sink(SID, append=False)(
        [_cand()])
    assert len(placements) == 1 and dropped == 0
    assert _rec() is None


def test_an_unusable_session_id_is_reported_not_swallowed():
    with pytest.raises(ValueError, match="plan_id"):
        replenish_sinks.roadmap_sink("NOT A VALID ID")


def test_a_full_round_through_the_real_sink_records_the_placement():
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    res = replenish.run_round(
        events.read_all(), surface="session",
        sink=replenish_sinks.roadmap_sink(SID),
        lenses={"l": lambda _e: [_cand()]}, session_id=SID)
    assert len(res.landed) == 1
    row = [e.payload for e in events.read_all()
           if e.type == "work_injected" and e.payload["outcome"] == "landed"][0]
    assert row["plan_id"] == SID and row["step_index"] == 0
