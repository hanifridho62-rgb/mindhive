# tests/test_roadmap.py
"""Roadmap registry: the event contract, the deterministic fold, registry
guards, the interrupted-plan STATE/LEDGER delivery, and the `hive roadmap`
CLI."""
import pytest
from mindhive import (__main__ as cli_mod, events, outcomes, paths, projector,
                      roadmap)

PROMPT = "Add an interactive roadmap to the Oversee section"
STEPS = ["design the data model", "build the fold", "render the panel"]


def _set(plan_id="oversee-roadmap", prompt=PROMPT, steps=None, ts=None,
         session="s1", **kw):
    return events.append_event(
        "roadmap", {"action": "set", "plan_id": plan_id, "prompt": prompt,
                    "steps": list(STEPS) if steps is None else steps, **kw},
        session_id=session, ts=ts, tags=["roadmap"])


def _ev(action, plan_id="oversee-roadmap", ts=None, session="s1", **kw):
    return events.append_event(
        "roadmap", {"action": action, "plan_id": plan_id, **kw},
        session_id=session, ts=ts, tags=["roadmap"])


# --- payload contract ---------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "free text",                                                # not a dict
    {"action": "pause", "plan_id": "x-1"},                      # bad action
    {"action": "set", "plan_id": "Bad_ID!", "prompt": PROMPT,
     "steps": STEPS},
    {"action": "set", "plan_id": "x-1", "steps": STEPS},        # no prompt
    {"action": "set", "plan_id": "x-1", "prompt": PROMPT},      # no steps
    {"action": "set", "plan_id": "x-1", "prompt": PROMPT, "steps": []},
    {"action": "set", "plan_id": "x-1", "prompt": PROMPT, "steps": [" "]},
    {"action": "set", "plan_id": "x-1", "prompt": PROMPT,
     "steps": ["y" * 301]},
    {"action": "set", "plan_id": "x-1", "prompt": PROMPT,
     "steps": ["a"] * 51},
    {"action": "set", "plan_id": "x-1", "prompt": "p" * 2001,
     "steps": STEPS},
    {"action": "set", "plan_id": "x-1", "prompt": PROMPT,
     "steps": [{"text": "a", "status": "paused"}]},
    {"action": "step", "plan_id": "x-1", "status": "done"},     # no index
    {"action": "step", "plan_id": "x-1", "index": -1, "status": "done"},
    {"action": "step", "plan_id": "x-1", "index": True, "status": "done"},
    {"action": "step", "plan_id": "x-1", "index": 0},           # no status
    {"action": "step", "plan_id": "x-1", "index": 0, "status": "finished"},
    {"action": "interrupt", "plan_id": "x-1"},                  # no cause
    {"action": "interrupt", "plan_id": "x-1", "cause": "c" * 121},
])
def test_validate_payload_rejects(payload):
    with pytest.raises(ValueError):
        roadmap.validate_payload(payload)


def test_validate_payload_accepts_lifecycle_with_id_only():
    roadmap.validate_payload({"action": "resume", "plan_id": "x-1"})
    roadmap.validate_payload({"action": "complete", "plan_id": "x-1"})


# --- fold / registry -----------------------------------------------------------

def test_set_defaults_first_step_active():
    _set()
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert [s["status"] for s in rec["steps"]] == \
        ["active", "pending", "pending"]
    assert rec["status"] == "active"
    assert rec["progress"] == {"done": 0, "total": 3}


def test_set_honors_explicit_statuses():
    _set(steps=[{"text": "a", "status": "done"},
                {"text": "b", "status": "active"},
                {"text": "c"}])
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert [s["status"] for s in rec["steps"]] == ["done", "active", "pending"]
    assert rec["progress"] == {"done": 1, "total": 3}


def test_step_done_advances_the_active_pointer():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("step", index=0, status="done", ts="2026-07-17T10:01:00Z")
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert [s["status"] for s in rec["steps"]] == ["done", "active", "pending"]
    assert roadmap.next_step(rec)["text"] == "build the fold"
    assert rec["progress"] == {"done": 1, "total": 3}
    assert rec["version"] == 2


def test_step_active_is_single():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("step", index=2, status="active", ts="2026-07-17T10:01:00Z")
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert [s["status"] for s in rec["steps"]] == \
        ["pending", "pending", "active"]


def test_step_records_note():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("step", index=1, status="done", note="fold mirrors sop.py",
        ts="2026-07-17T10:01:00Z")
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert rec["steps"][1]["note"] == "fold mirrors sop.py"


def test_interrupt_resume_complete_lifecycle():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("interrupt", cause="usage_limit", ts="2026-07-17T11:00:00Z")
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert rec["status"] == "interrupted"
    assert rec["cause"] == "usage_limit"
    assert rec["interrupted_at"] == "2026-07-17T11:00:00Z"
    _ev("resume", ts="2026-07-17T12:00:00Z")
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert rec["status"] == "active" and rec["cause"] == ""
    _ev("complete", ts="2026-07-17T13:00:00Z")
    evs = events.read_all()
    rec = roadmap.get(evs, "oversee-roadmap")
    assert rec["status"] == "completed"
    assert roadmap.plans(evs) == []                       # hidden by default
    assert roadmap.plans(evs, include_completed=True) != []


def test_reset_revives_with_created_and_version_continuity():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("complete", ts="2026-07-17T11:00:00Z")
    _set(steps=["new step"], ts="2026-07-17T12:00:00Z")
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert rec["status"] == "active"
    assert rec["created"] == "2026-07-17T10:00:00Z"
    assert rec["version"] == 3
    assert [s["text"] for s in rec["steps"]] == ["new step"]


def test_fold_is_total_on_junk_history():
    """Every historical record predates every field added since, so the fold
    has to read a valid-but-old event as ordinary rather than degraded. The
    work-link keys join `new` and the relay fields in that guarantee: absent is
    the normal case, and asking a linkless plan for its link answers empty
    instead of raising."""
    _ev("step", plan_id="ghost", index=0, status="done")
    _ev("interrupt", plan_id="ghost2", cause="usage_limit")
    _set(ts="2026-07-17T10:00:00Z")
    _ev("step", index=99, status="done", ts="2026-07-17T10:01:00Z")
    events.append_event("roadmap", {"action": "set", "plan_id": "half"},
                        session_id="s1")                  # no prompt/steps
    events.append_event("roadmap", "free text", session_id="s1")
    # A valid-but-old `archive`, from before the reason was required, and a
    # `link` carrying nothing: both must fold away without touching the plan.
    _ev("archive", ts="2026-07-17T10:02:00Z")
    _ev("link", ts="2026-07-17T10:03:00Z")
    evs = events.read_all()
    rec = roadmap.get(evs, "oversee-roadmap")
    assert rec["progress"] == {"done": 0, "total": 3}     # bad step skipped
    assert rec["status"] == "active"                     # reasonless archive
    assert set(roadmap.fold(evs)) == {"oversee-roadmap"}
    assert not any(key in rec for key in roadmap.WORK_KEYS)
    assert roadmap.work_link(rec) == {"branch": "", "pr": 0, "commits": []}
    assert roadmap.has_work_link(rec) is False
    assert roadmap.describe_work_link(rec) == ""
    assert roadmap.work_link(None) == {"branch": "", "pr": 0, "commits": []}


def test_plans_order_by_created_then_id():
    _set("b-plan", ts="2026-07-17T11:00:00Z")
    _set("a-plan", ts="2026-07-17T10:00:00Z")
    _set("c-plan", ts="2026-07-17T10:00:00Z")
    ids = [r["plan_id"] for r in roadmap.plans(events.read_all())]
    assert ids == ["a-plan", "c-plan", "b-plan"]


def test_steps_from_plan_items_maps_statuses():
    # Bare strings on purpose: the helper must still fold a plain checklist line,
    # because every payload stored before the parse existed holds those.
    steps = roadmap.steps_from_plan_items(
        {"done": ["a"], "planned": ["b", "c"], "not_yet": ["d"]})
    assert steps == [{"text": "a", "status": "done", "new": False},
                     {"text": "b", "status": "active", "new": False},
                     {"text": "c", "status": "pending", "new": False},
                     {"text": "d", "status": "skipped", "new": False}]
    assert roadmap.steps_from_plan_items(
        {"done": [], "planned": [], "not_yet": []}) == []
    # oversize checklist items truncate instead of invalidating the marker
    long = roadmap.steps_from_plan_items({"done": [], "planned": ["x" * 999],
                                          "not_yet": []})
    assert len(long[0]["text"]) == roadmap.MAX_STEP_CHARS
    roadmap.validate_payload({"action": "set", "plan_id": "x-1",
                              "prompt": PROMPT, "steps": long})


# --- record guards -------------------------------------------------------------

def test_record_set_then_step_round_trip():
    roadmap.record("set", "auth-refactor", prompt=PROMPT, steps=list(STEPS))
    roadmap.record("step", "auth-refactor", index=0, status="done")
    rec = roadmap.get(events.read_all(), "auth-refactor")
    assert rec["progress"] == {"done": 1, "total": 3}


def test_record_lifecycle_on_missing_plan_is_an_error():
    with pytest.raises(ValueError, match="hive roadmap set"):
        roadmap.record("step", "nope", index=0, status="done")
    with pytest.raises(ValueError, match="hive roadmap set"):
        roadmap.record("interrupt", "nope", cause="usage_limit")
    assert events.read_all() == []


def test_record_guards_state_transitions():
    roadmap.record("set", "auth-refactor", prompt=PROMPT, steps=list(STEPS))
    with pytest.raises(ValueError, match="not interrupted"):
        roadmap.record("resume", "auth-refactor")
    with pytest.raises(ValueError, match="out of range"):
        roadmap.record("step", "auth-refactor", index=9, status="done")
    roadmap.record("interrupt", "auth-refactor", cause="usage_limit")
    with pytest.raises(ValueError, match="already interrupted"):
        roadmap.record("interrupt", "auth-refactor", cause="usage_limit")
    roadmap.record("complete", "auth-refactor")
    with pytest.raises(ValueError, match="already completed"):
        roadmap.record("complete", "auth-refactor")
    with pytest.raises(ValueError, match="revive"):
        roadmap.record("step", "auth-refactor", index=0, status="done")


def test_record_stamps_roadmap_tag():
    ev = roadmap.record("set", "auth-refactor", prompt=PROMPT,
                        steps=list(STEPS), tags=["work", "roadmap"])
    assert ev.tags[0] == "roadmap"
    assert ev.tags.count("roadmap") == 1
    assert "work" in ev.tags


# --- the out-of-usage marker: notice + delivery ---------------------------------

def test_notice_empty_without_interrupted_plans():
    assert roadmap.notice([]) == ""
    _set()
    assert roadmap.notice(events.read_all()) == ""        # active, not marked


def test_notice_renders_ask_progress_and_next_step():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("step", index=0, status="done", ts="2026-07-17T10:01:00Z")
    _ev("interrupt", cause="usage_limit", ts="2026-07-17T11:00:00Z")
    note = roadmap.notice(events.read_all())
    assert "⚠ Interrupted plan" in note
    assert PROMPT in note
    assert "1/3 steps done" in note
    assert "cause: usage_limit" in note
    assert "NEXT: build the fold" in note
    assert "hive roadmap resume" in note
    assert "hive roadmap show" in note


def test_notice_caps_done_and_pending_lists():
    steps = [{"text": f"step-{i:02d}", "status": "done"} for i in range(6)] \
        + [{"text": f"todo-{i:02d}", "status": "pending"} for i in range(6)]
    _set(steps=steps, ts="2026-07-17T10:00:00Z")
    _ev("interrupt", cause="usage_limit", ts="2026-07-17T11:00:00Z")
    note = roadmap.notice(events.read_all())
    assert "… 3 earlier step(s) done" in note
    assert "step-05" in note and "step-00" not in note
    # the first pending step is the NEXT pointer; 3 more render; 2 overflow
    assert "NEXT: todo-00" in note
    assert "todo-03" in note and "todo-04" not in note
    assert "… 2 more step(s)" in note


def test_notice_clears_after_resume():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("interrupt", cause="usage_limit", ts="2026-07-17T11:00:00Z")
    _ev("resume", ts="2026-07-17T12:00:00Z")
    assert roadmap.notice(events.read_all()) == ""


def test_projector_delivers_notice_and_ledger_entry():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("interrupt", cause="usage_limit", ts="2026-07-17T11:00:00Z")
    projector.project()
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "⚠ Interrupted plan" in state
    assert PROMPT in state
    ledger = list((paths.project_dir() / "LEDGER").glob("*-roadmap-*.md"))
    assert len(ledger) == 2                                # set + interrupt
    bodies = " ".join(p.read_text(encoding="utf-8") for p in ledger)
    assert "[set] roadmap oversee-roadmap" in bodies
    assert f"Asked: {PROMPT}" in bodies
    assert "cause: usage_limit" in bodies


def test_memory_md_variant_keeps_the_notice():
    # sync embeds compose_state(include_rules=False) into MEMORY.md — the
    # marker must ride the one file every session auto-loads.
    _set(ts="2026-07-17T10:00:00Z")
    _ev("interrupt", cause="usage_limit", ts="2026-07-17T11:00:00Z")
    evs = events.read_all()
    assert "⚠ Interrupted plan" in \
        projector.compose_state(evs, include_rules=False, suggestions="")


def test_briefing_timeline_excludes_roadmap_events():
    # roadmap payloads have no "text": a timeline line would be a blank
    # headline eating the cap — delivery is the STATE notice + Oversee.
    from mindhive.briefing import build_briefing
    _set(ts="2026-07-17T10:00:00Z")
    assert "[roadmap]" not in build_briefing(events.read_all())


# --- registry content is audit-grade: never merged ------------------------------

def test_roadmap_exempt_from_consolidation():
    from mindhive import consolidate
    assert "roadmap" not in consolidate.CONSOLIDATE_TYPES
    assert "decision" in consolidate.CONSOLIDATE_TYPES    # sanity: set intact


def test_display_dedupe_never_collapses_roadmap_events():
    from mindhive import dedupe
    items = [
        {"type": "roadmap", "project": "p", "ts": "2026-07-17T11:00:00Z",
         "text": "Roadmap step: build the fold over roadmap events."},
        {"type": "roadmap", "project": "p", "ts": "2026-07-17T10:00:00Z",
         "text": "Roadmap step: build the fold over roadmap events."},
    ]
    out = dedupe.collapse(items)
    assert len(out) == 2 and all(i["similar_count"] == 0 for i in out)


# --- CLI ------------------------------------------------------------------------

def test_cli_roadmap_set_list_show(capsys):
    assert cli_mod.main(["roadmap", "set", "auth-refactor",
                         "--prompt", PROMPT, "--title", "Auth refactor",
                         "--step", STEPS[0], "--step", STEPS[1]]) == 0
    assert cli_mod.main(["roadmap", "list"]) == 0
    out = capsys.readouterr().out
    assert "[auth-refactor v1] Auth refactor" in out
    assert "0/2 done" in out and "(active)" in out
    assert cli_mod.main(["roadmap", "show", "auth-refactor"]) == 0
    out = capsys.readouterr().out
    assert PROMPT in out
    assert "[>] design the data model" in out
    assert "history (oldest first):" in out


def test_cli_roadmap_steps_from_file(tmp_path):
    f = tmp_path / "steps.txt"
    f.write_text("\n".join(STEPS) + "\n\n", encoding="utf-8")
    assert cli_mod.main(["roadmap", "set", "auth-refactor",
                         "--prompt", PROMPT, "--file", str(f)]) == 0
    rec = roadmap.get(events.read_all(), "auth-refactor")
    assert [s["text"] for s in rec["steps"]] == STEPS


def test_cli_roadmap_step_interrupt_resume_complete(capsys):
    cli_mod.main(["roadmap", "set", "auth-refactor", "--prompt", PROMPT,
                  "--step", STEPS[0], "--step", STEPS[1]])
    assert cli_mod.main(["roadmap", "step", "auth-refactor", "0",
                         "--status", "done", "--note", "landed"]) == 0
    assert cli_mod.main(["roadmap", "interrupt", "auth-refactor",
                         "--cause", "usage_limit"]) == 0
    cli_mod.main(["roadmap", "list"])
    assert "(interrupted)" in capsys.readouterr().out
    assert cli_mod.main(["roadmap", "resume", "auth-refactor"]) == 0
    assert cli_mod.main(["roadmap", "complete", "auth-refactor"]) == 0
    evs = events.read_all()
    assert [e.payload["action"] for e in evs if e.type == "roadmap"] == \
        ["set", "step", "interrupt", "resume", "complete"]
    # the usage-shaped interrupt armed the delivery leg; the human resume
    # withdrew it (see autoresume.py)
    assert [e.payload["action"] for e in evs if e.type == "autoresume"] == \
        ["scheduled", "cancelled"]
    cli_mod.main(["roadmap", "list"])
    assert "no roadmaps" in capsys.readouterr().out       # completed hidden
    cli_mod.main(["roadmap", "list", "--all"])
    assert "(completed)" in capsys.readouterr().out


def test_cli_roadmap_errors_exit_2(capsys):
    assert cli_mod.main(["roadmap", "step", "nope", "0",
                         "--status", "done"]) == 2
    assert "hive roadmap set" in capsys.readouterr().err
    cli_mod.main(["roadmap", "set", "auth-refactor", "--prompt", PROMPT,
                  "--step", STEPS[0]])
    assert cli_mod.main(["roadmap", "resume", "auth-refactor"]) == 2
    assert "not interrupted" in capsys.readouterr().err


def test_cli_append_enforces_roadmap_contract(capsys):
    assert cli_mod.main(["append", "roadmap", "free text, no action"]) == 2
    assert "hive roadmap" in capsys.readouterr().err
    good = ('{"action": "set", "plan_id": "auth-refactor", '
            '"prompt": "' + PROMPT + '", "steps": ["one step"]}')
    assert cli_mod.main(["append", "roadmap", good, "--json"]) == 0
    evs = events.read_all()
    assert len(evs) == 1 and evs[0].type == "roadmap"


# --- session auto-sync (TodoWrite -> roadmap) ---------------------------------

SID = "sess-abc-123"
TODOS = [{"content": "design the data model", "status": "completed"},
         {"content": "build the fold", "status": "in_progress"},
         {"content": "render the panel", "status": "pending"}]


def test_sync_declares_plan_with_mapped_statuses_and_auto_tag():
    assert roadmap.sync_from_todos(SID, TODOS) is not None
    rec = roadmap.get(events.read_all(), SID)
    assert [s["status"] for s in rec["steps"]] == ["done", "active", "pending"]
    assert rec["progress"] == {"done": 1, "total": 3}
    assert roadmap.AUTO_TAG in rec["tags"]
    assert rec["prompt"] == f"session {SID}"            # no user_prompt yet


def test_sync_takes_prompt_from_first_user_prompt_event():
    events.append_event("user_prompt", {"text": "please fix the roadmap"},
                        session_id=SID)
    roadmap.sync_from_todos(SID, TODOS)
    rec = roadmap.get(events.read_all(), SID)
    assert rec["prompt"] == "please fix the roadmap"
    assert rec["title"] == "please fix the roadmap"


def test_sync_progress_update_appends_new_version():
    roadmap.sync_from_todos(SID, TODOS)
    ev = roadmap.sync_from_todos(
        SID, [dict(t, status="completed") for t in TODOS])
    assert ev is not None
    rec = roadmap.get(events.read_all(), SID)
    assert rec["version"] == 2
    assert rec["progress"] == {"done": 3, "total": 3}


def test_sync_identical_todos_is_noop():
    roadmap.sync_from_todos(SID, TODOS)
    assert roadmap.sync_from_todos(SID, TODOS) is None
    assert len(events.read_all()) == 1


def test_sync_prompt_upgrade_is_material_change():
    roadmap.sync_from_todos(SID, TODOS)                 # placeholder prompt
    events.append_event("user_prompt", {"text": "the real ask"},
                        session_id=SID)
    assert roadmap.sync_from_todos(SID, TODOS) is not None  # same steps
    assert roadmap.get(events.read_all(), SID)["prompt"] == "the real ask"


def test_sync_never_touches_manual_plans():
    _set(plan_id=SID, session=SID)                      # human-declared
    assert roadmap.sync_from_todos(SID, TODOS) is None
    rec = roadmap.get(events.read_all(), SID)
    assert [s["text"] for s in rec["steps"]] == STEPS   # untouched


def test_sync_skips_bad_session_ids_and_bad_todos():
    assert roadmap.sync_from_todos("", TODOS) is None
    assert roadmap.sync_from_todos(None, TODOS) is None
    assert roadmap.sync_from_todos("Bad_ID", TODOS) is None
    assert roadmap.sync_from_todos(SID, []) is None
    assert roadmap.sync_from_todos(SID, "junk") is None
    assert roadmap.sync_from_todos(
        SID, [{"content": " ", "status": "pending"},
              {"content": "x", "status": "odd"}]) is None
    assert events.read_all() == []


def test_sync_truncates_oversized_todos():
    todos = [{"content": "x" * 500, "status": "pending"}] \
        + [{"content": f"s{i}", "status": "pending"} for i in range(60)]
    roadmap.sync_from_todos(SID, todos)
    rec = roadmap.get(events.read_all(), SID)
    assert len(rec["steps"]) == roadmap.MAX_STEPS
    assert len(rec["steps"][0]["text"]) == roadmap.MAX_STEP_CHARS


def test_sync_revives_interrupted_plan():
    roadmap.sync_from_todos(SID, TODOS)
    roadmap.autoclose_session(SID)                      # -> interrupted
    assert roadmap.sync_from_todos(
        SID, [dict(t, status="completed") for t in TODOS]) is not None
    assert roadmap.get(events.read_all(), SID)["status"] == "active"


# --- session-end autoclose -----------------------------------------------------

def test_autoclose_completes_when_every_step_is_closed():
    roadmap.sync_from_todos(
        SID, [dict(t, status="completed") for t in TODOS])
    ev = roadmap.autoclose_session(SID)
    assert ev.payload["action"] == "complete"
    assert roadmap.get(events.read_all(), SID)["status"] == "completed"
    assert roadmap.plans(events.read_all()) == []       # left every view


def test_autoclose_interrupts_unfinished_plan_with_state_notice():
    roadmap.sync_from_todos(SID, TODOS)
    ev = roadmap.autoclose_session(SID)
    assert ev.payload["action"] == "interrupt"
    rec = roadmap.get(events.read_all(), SID)
    assert rec["status"] == "interrupted"
    assert rec["cause"] == "session_end"
    assert "Interrupted plan" in roadmap.notice(events.read_all())


def test_autoclose_noop_without_an_auto_owned_open_plan():
    assert roadmap.autoclose_session(SID) is None       # no plan at all
    _set(plan_id=SID, session=SID)                      # manual plan
    assert roadmap.autoclose_session(SID) is None
    assert roadmap.get(events.read_all(), SID)["status"] == "active"


def test_autoclose_is_idempotent():
    roadmap.sync_from_todos(SID, TODOS)
    roadmap.autoclose_session(SID)
    n = len(events.read_all())
    assert roadmap.autoclose_session(SID) is None       # already interrupted
    assert len(events.read_all()) == n


def test_cli_roadmap_autoclose(capsys, monkeypatch):
    monkeypatch.delenv("MINDHIVE_SESSION", raising=False)
    assert cli_mod.main(["roadmap", "autoclose"]) == 0  # no session: silent
    assert capsys.readouterr().out == ""
    roadmap.sync_from_todos(SID, TODOS)
    assert cli_mod.main(["roadmap", "autoclose", "--session", SID]) == 0
    assert "interrupt" in capsys.readouterr().out


# --- the work link: how a plan points at the code it produced -------------------
# Before it, `roadmap.fold` had no branch, PR, or commit field, so nothing
# checkable connected a plan to its work — and a card whose last step was
# "merge the PR" sat in Oversee forever, because retiring it would have meant
# asserting something no code could see.

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize("payload", [
    {"action": "link", "plan_id": "x-1"},                       # links nothing
    {"action": "set", "plan_id": "x-1", "prompt": PROMPT, "steps": STEPS,
     "work_branch": "   "},
    {"action": "set", "plan_id": "x-1", "prompt": PROMPT, "steps": STEPS,
     "work_branch": "b" * 251},
    {"action": "link", "plan_id": "x-1", "work_pr": 0},
    {"action": "link", "plan_id": "x-1", "work_pr": -3},
    {"action": "link", "plan_id": "x-1", "work_pr": True},       # bool is an int
    {"action": "link", "plan_id": "x-1", "work_pr": "103"},
    {"action": "link", "plan_id": "x-1", "work_commits": SHA},   # not a list
    {"action": "link", "plan_id": "x-1", "work_commits": [""]},
    {"action": "link", "plan_id": "x-1", "work_commits": ["x" * 41]},
    {"action": "link", "plan_id": "x-1", "work_commits": [SHA] * 21},
    {"action": "archive", "plan_id": "x-1"},                     # no reason
    {"action": "archive", "plan_id": "x-1", "reason": "  "},
    {"action": "archive", "plan_id": "x-1", "reason": "r" * 301},
])
def test_validate_payload_rejects_malformed_links_and_archives(payload):
    with pytest.raises(ValueError):
        roadmap.validate_payload(payload)


def test_link_records_the_work_without_touching_the_plan():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("link", work_pr=103, work_branch="claude/perf",
        work_commits=[SHA], ts="2026-07-17T10:05:00Z")
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert roadmap.work_link(rec) == {"branch": "claude/perf", "pr": 103,
                                      "commits": [SHA]}
    assert roadmap.has_work_link(rec) is True
    assert rec["status"] == "active"                      # status untouched
    assert [s["status"] for s in rec["steps"]] == \
        ["active", "pending", "pending"]                  # steps untouched
    assert "PR #103" in roadmap.describe_work_link(rec)
    assert "claude/perf" in roadmap.describe_work_link(rec)


def test_the_three_link_fields_accumulate_one_at_a_time():
    """They become knowable at different moments — branch when work starts, PR
    once one is opened — so a later `link` must merge rather than replace."""
    _set(ts="2026-07-17T10:00:00Z")
    _ev("link", work_branch="claude/perf", ts="2026-07-17T10:01:00Z")
    _ev("link", work_pr=103, ts="2026-07-17T10:02:00Z")
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert roadmap.work_link(rec)["branch"] == "claude/perf"
    assert roadmap.work_link(rec)["pr"] == 103


def test_a_replan_keeps_the_work_link():
    """`set` replaces the STEPS wholesale and says nothing about where the code
    went. `sync_from_todos` re-`set`s on every material TodoWrite change and
    never supplies a link, so without carry-forward one more todo edit would
    silently drop the PR number — the erasure the `new` flag already had to be
    protected from."""
    _set(ts="2026-07-17T10:00:00Z")
    _ev("link", work_pr=103, ts="2026-07-17T10:01:00Z")
    _set(steps=["a whole new plan"], ts="2026-07-17T11:00:00Z")
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert roadmap.work_link(rec)["pr"] == 103
    assert [s["text"] for s in rec["steps"]] == ["a whole new plan"]


def test_a_replan_may_repoint_the_link():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("link", work_pr=103, ts="2026-07-17T10:01:00Z")
    _set(steps=["moved"], work_pr=104, ts="2026-07-17T11:00:00Z")
    assert roadmap.get(events.read_all(), "oversee-roadmap")["work_pr"] == 104


def test_the_link_survives_interrupt_and_resume():
    """`resume` drops the relay because a consumed baton would mislead a later
    reader. The work link is the opposite: it is durable provenance, and the
    plan still points at the same code after it is picked back up."""
    _set(ts="2026-07-17T10:00:00Z")
    _ev("link", work_pr=103, ts="2026-07-17T10:01:00Z")
    _ev("interrupt", cause="usage_limit", relay_context="ctx",
        ts="2026-07-17T11:00:00Z")
    _ev("resume", ts="2026-07-17T12:00:00Z")
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert roadmap.work_link(rec)["pr"] == 103
    assert "relay_context" not in rec


def test_the_ledger_body_carries_the_link_so_recall_finds_it_by_pr():
    body = roadmap.ledger_body({"action": "link", "plan_id": "x-1",
                                "work_pr": 103, "work_branch": "claude/perf"})
    assert "PR #103" in body and "claude/perf" in body


# --- archive: the honest exit for a step nothing can check ----------------------

def test_archive_retires_the_plan_and_keeps_the_reason():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("archive", reason="PR #103 merged; the manual step is settled",
        ts="2026-07-17T11:00:00Z")
    evs = events.read_all()
    rec = roadmap.get(evs, "oversee-roadmap")
    assert rec["status"] == roadmap.ARCHIVED
    assert rec["archive_reason"] == "PR #103 merged; the manual step is settled"
    assert rec["archived_at"] == "2026-07-17T11:00:00Z"
    # Retired, but NOT claimed as finished: the progress it really reached
    # stays on the record, and it leaves the live views like a completed plan.
    assert rec["progress"] == {"done": 0, "total": 3}
    assert roadmap.plans(evs) == []
    assert roadmap.plans(evs, include_completed=True) != []
    assert roadmap.notice(evs) == ""                     # no longer a marker


def test_archive_is_reversible_by_replanning():
    _set(ts="2026-07-17T10:00:00Z")
    _ev("archive", reason="superseded", ts="2026-07-17T11:00:00Z")
    _set(steps=["back on"], ts="2026-07-17T12:00:00Z")
    assert roadmap.get(events.read_all(),
                       "oversee-roadmap")["status"] == "active"


def test_record_guards_the_archive_transitions():
    roadmap.record("set", "auth-refactor", prompt=PROMPT, steps=list(STEPS))
    with pytest.raises(ValueError, match="requires a reason"):
        roadmap.record("archive", "auth-refactor")
    roadmap.record("archive", "auth-refactor", reason="the human said so")
    with pytest.raises(ValueError, match="already archived"):
        roadmap.record("archive", "auth-refactor", reason="again")
    with pytest.raises(ValueError, match="revive"):
        roadmap.record("step", "auth-refactor", index=0, status="done")
    # `link` is the one action a retired plan still accepts: a PR number is
    # routinely learned after the plan has left every view.
    roadmap.record("link", "auth-refactor", work_pr=103)
    assert roadmap.get(events.read_all(), "auth-refactor")["work_pr"] == 103


def test_archiving_a_finished_plan_is_refused():
    roadmap.record("set", "auth-refactor", prompt=PROMPT, steps=list(STEPS))
    roadmap.record("complete", "auth-refactor")
    with pytest.raises(ValueError, match="already completed"):
        roadmap.record("archive", "auth-refactor", reason="tidy up")


# --- retirement on evidence ----------------------------------------------------

def _probe(state, reason="", evidence="e", detail="d"):
    """A stand-in for `repo.merge_state`: the retirement logic is what is under
    test here, and `tests/test_repo.py` owns the probe's own honesty."""
    def probe(root=None, branch="", pr=0, commits=()):
        return {"state": state, "reason": reason, "evidence": evidence,
                "detail": detail}
    return probe


def test_retire_completes_a_plan_whose_pr_is_merged():
    from mindhive import repo
    _set(ts="2026-07-17T10:00:00Z")
    _ev("link", work_pr=103, ts="2026-07-17T10:01:00Z")
    outcomes = roadmap.retire_landed(
        probe=_probe(repo.MERGED, evidence="PR #103 is merged"))
    assert [(o["plan_id"], o["action"]) for o in outcomes] == \
        [("oversee-roadmap", "complete")]
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert rec["status"] == "completed"


def test_retire_archives_a_plan_whose_pr_was_closed_unmerged():
    from mindhive import repo
    _set(ts="2026-07-17T10:00:00Z")
    _ev("link", work_pr=103, ts="2026-07-17T10:01:00Z")
    outcomes = roadmap.retire_landed(
        probe=_probe(repo.CLOSED, evidence="PR #103 was closed"))
    assert outcomes[0]["action"] == "archive"
    rec = roadmap.get(events.read_all(), "oversee-roadmap")
    assert rec["status"] == roadmap.ARCHIVED
    assert "closed" in rec["archive_reason"]


@pytest.mark.parametrize("state,reason", [
    ("open", ""), ("unknown", "no_gh_cli"), ("unknown", "no_work_link")])
def test_retire_leaves_everything_it_cannot_prove(state, reason):
    """The invariant that makes the sweep safe to run on a whole store: only
    proof retires a card. Everything else is REPORTED, not acted on."""
    _set(ts="2026-07-17T10:00:00Z")
    outcomes = roadmap.retire_landed(probe=_probe(state, reason))
    assert outcomes[0]["action"] == ""
    assert outcomes[0]["reason"] == reason
    assert roadmap.get(events.read_all(),
                       "oversee-roadmap")["status"] == "active"


def test_retire_reports_every_plan_it_examined():
    """A sweep that retired nothing because every PR is open must be
    distinguishable from one that retired nothing because `gh` is missing — and
    the only way to tell them apart is to report what was looked at."""
    from mindhive import repo
    _set(plan_id="a-plan", ts="2026-07-17T10:00:00Z")
    _set(plan_id="b-plan", ts="2026-07-17T10:01:00Z")
    outcomes = roadmap.retire_landed(
        probe=_probe(repo.UNKNOWN, reason="no_gh_cli", evidence=""))
    assert [o["plan_id"] for o in outcomes] == ["a-plan", "b-plan"]
    assert all(o["reason"] == "no_gh_cli" for o in outcomes)


def test_retire_skips_plans_that_already_retired():
    from mindhive import repo
    _set(ts="2026-07-17T10:00:00Z")
    _ev("complete", ts="2026-07-17T11:00:00Z")
    assert roadmap.retire_landed(probe=_probe(repo.MERGED)) == []


# --- the CLI surfaces ----------------------------------------------------------

def test_cli_link_and_show_render_the_work_link(capsys):
    cli_mod.main(["roadmap", "set", "auth-refactor", "--prompt", PROMPT,
                  "--step", STEPS[0], "--branch", "claude/perf"])
    assert cli_mod.main(["roadmap", "link", "auth-refactor", "--pr", "103",
                         "--commit", SHA]) == 0
    assert cli_mod.main(["roadmap", "show", "auth-refactor"]) == 0
    out = capsys.readouterr().out
    assert "work: PR #103 · branch claude/perf · 0123456789ab" in out


def test_cli_show_names_the_command_that_adds_a_missing_link(capsys):
    cli_mod.main(["roadmap", "set", "auth-refactor", "--prompt", PROMPT,
                  "--step", STEPS[0]])
    cli_mod.main(["roadmap", "show", "auth-refactor"])
    assert "hive roadmap link auth-refactor" in capsys.readouterr().out


def test_cli_archive_requires_a_reason_and_marks_the_card(capsys):
    cli_mod.main(["roadmap", "set", "auth-refactor", "--prompt", PROMPT,
                  "--step", STEPS[0]])
    with pytest.raises(SystemExit):                       # argparse: required
        cli_mod.main(["roadmap", "archive", "auth-refactor"])
    assert cli_mod.main(["roadmap", "archive", "auth-refactor",
                         "--reason", "PR #103 merged"]) == 0
    cli_mod.main(["roadmap", "list"])
    assert "no roadmaps" in capsys.readouterr().out       # left the live view
    cli_mod.main(["roadmap", "list", "--all"])
    assert "(archived)" in capsys.readouterr().out


def test_cli_merge_state_prints_the_reason_it_could_not_tell(capsys, monkeypatch):
    from mindhive import repo
    cli_mod.main(["roadmap", "set", "auth-refactor", "--prompt", PROMPT,
                  "--step", STEPS[0]])
    monkeypatch.setattr(repo, "merge_state", _probe(
        repo.UNKNOWN, reason="no_gh_cli", evidence="", detail="gh is not on PATH"))
    assert cli_mod.main(["roadmap", "merge-state", "auth-refactor"]) == 0
    out = capsys.readouterr().out
    assert "unknown" in out
    assert "reason: no_gh_cli" in out
    assert "detail: gh is not on PATH" in out


def test_cli_merge_state_on_a_missing_plan_exits_2(capsys):
    assert cli_mod.main(["roadmap", "merge-state", "nope"]) == 2
    assert "no roadmap 'nope'" in capsys.readouterr().err


def test_cli_retire_says_so_when_it_retires_nothing(capsys, monkeypatch):
    """The zero-honest case: "nothing was provably landed" must not read like
    "the sweep worked and your cards are gone"."""
    from mindhive import repo
    cli_mod.main(["roadmap", "set", "auth-refactor", "--prompt", PROMPT,
                  "--step", STEPS[0]])
    monkeypatch.setattr(repo, "merge_state",
                        _probe(repo.OPEN, evidence="PR #103 is still open"))
    assert cli_mod.main(["roadmap", "retire"]) == 0
    out = capsys.readouterr().out
    assert "0 of 1 roadmap(s) retired on evidence" in out
    assert "nothing retired" in out
    assert "not landed yet" in out


def test_cli_retire_completes_on_proof(capsys, monkeypatch):
    from mindhive import repo
    cli_mod.main(["roadmap", "set", "auth-refactor", "--prompt", PROMPT,
                  "--step", STEPS[0]])
    monkeypatch.setattr(repo, "merge_state",
                        _probe(repo.MERGED, evidence="PR #103 is merged"))
    assert cli_mod.main(["roadmap", "retire"]) == 0
    out = capsys.readouterr().out
    assert "1 of 1 roadmap(s) retired on evidence" in out
    assert roadmap.get(events.read_all(),
                       "auth-refactor")["status"] == "completed"


def test_cli_retire_on_an_empty_store_says_there_was_nothing_to_examine(capsys):
    assert cli_mod.main(["roadmap", "retire"]) == 0
    assert "no live roadmaps to examine" in capsys.readouterr().out


# --- the outcomes-registry probe -----------------------------------------------
# `roadmap._probe` is the module's registry probe; the bare `_probe` in this file
# is the fake merge_state factory above. Always qualified here for that reason.

def test_probe_on_an_empty_store_reports_nothing_waiting():
    """The honest zero. No plans is a success, and must not read like a store the
    probe failed to open."""
    report = roadmap._probe()
    assert report.outcome == outcomes.NOTHING_TO_DO
    assert report.worked
    assert "no live roadmap plan" in report.summary


def test_probe_is_degraded_when_no_live_plan_carries_a_work_link():
    """The state that makes retirement unreachable: `merge_state` on a plan with
    no branch, PR or commit can only ever answer unknown/no_work_link, so the
    sweep will never retire this card however often it runs."""
    _set()
    report = roadmap._probe()
    assert report.outcome == outcomes.DEGRADED
    assert not report.worked
    assert "no_work_link" in report.summary


def test_probe_names_the_unlinked_share_when_only_some_are_linked():
    """A partially linked store is still degraded, and the count is the actionable
    part — "some plans are fine" does not tell anyone which to fix."""
    _set(plan_id="linked-plan")
    _ev("link", plan_id="linked-plan", work_pr=103)
    _set(plan_id="unlinked-plan")
    report = roadmap._probe()
    assert report.outcome == outcomes.DEGRADED
    assert "1 of 2" in report.summary


def test_probe_reports_did_work_when_every_live_plan_is_checkable():
    _set(plan_id="linked-plan")
    _ev("link", plan_id="linked-plan", work_branch="claude/perf")
    report = roadmap._probe()
    assert report.outcome == outcomes.DID_WORK
    assert "1 live plan(s)" in report.summary


def test_probe_ignores_plans_that_already_left():
    """A retired plan with no work link is not a broken loop — it is a closed one.
    Counting it would make every finished plan degrade the report forever."""
    _set(plan_id="done-plan")
    _ev("complete", plan_id="done-plan")
    _set(plan_id="gone-plan")
    _ev("archive", plan_id="gone-plan", reason="superseded by the new plan")
    report = roadmap._probe()
    assert report.outcome == outcomes.NOTHING_TO_DO


def test_probe_is_registered_under_its_surface_name():
    surface = outcomes.get("roadmap-retirement")
    assert surface is not None
    assert surface.probe is roadmap._probe


# --- which plan does this session's auto-managed work belong to? --------------
#
# WHY THIS EXISTS. "plan_id == session_id" was the answer in four places, and
# it is the reason the dashboard grew a permanent pile under "N awaiting
# resume": a session ends, `autoclose_session` interrupts its plan, the next
# session has a new id and therefore mints a NEW plan, and the old one can
# never be matched to a live card again. Progress froze by construction, and
# the replenishment injector's `(new)` steps landed on whichever plan the
# session happened to own rather than on the work actually being continued.

def _auto_set(plan_id, session, ts, steps=None, prompt=PROMPT):
    """An AUTO-tagged plan, the shape `sync_from_todos` writes."""
    return events.append_event(
        "roadmap", {"action": "set", "plan_id": plan_id, "prompt": prompt,
                    "steps": list(STEPS) if steps is None else steps},
        session_id=session, ts=ts, tags=["roadmap", roadmap.AUTO_TAG])


def _auto_ev(action, plan_id, session, ts, **kw):
    return events.append_event(
        "roadmap", {"action": action, "plan_id": plan_id, **kw},
        session_id=session, ts=ts, tags=["roadmap", roadmap.AUTO_TAG])


NOW = "2026-08-20T12:00:00Z"


def _resolve(sid, now=NOW):
    return roadmap.auto_plan_id(sid, roadmap.fold(events.read_all()), now=now)


def test_auto_plan_id_prefers_the_sessions_own_plan():
    """A session that already owns a plan keeps it — adoption must never
    redirect work away from the plan this session itself declared."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    _auto_set("sess-b", "sess-b", "2026-08-20T11:00:00Z")
    assert _resolve("sess-b") == "sess-b"


def test_auto_plan_id_adopts_the_plan_the_previous_session_left_interrupted():
    """The defect, inverted: the new session continues the paused plan."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    assert _resolve("sess-b") == "sess-a"


def test_auto_plan_id_adopts_a_usage_capped_plan_too():
    """`session_end` is not the only pause. The module's whole reason for the
    interrupt marker is the usage cap, and a rotation successor has to pick up
    exactly that plan — so the rule is the STATUS, never the cause string."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="api_cap")
    assert _resolve("sess-b") == "sess-a"


def test_auto_plan_id_leaves_an_active_plan_to_the_session_that_owns_it():
    """Two Claudes at once is normal here. An ACTIVE plan has a live owner, and
    stealing it would make two sessions overwrite one record's steps."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    assert _resolve("sess-b") == "sess-b"


@pytest.mark.parametrize("action,kw", [("complete", {}),
                                       ("archive", {"reason": "superseded"})])
def test_auto_plan_id_never_revives_a_retired_plan(action, kw):
    """Completing or archiving a plan IS the user's way of saying "not this
    one" — it has to be the escape hatch from adoption, or there is none."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev(action, "sess-a", "sess-a", "2026-08-20T10:30:00Z", **kw)
    assert _resolve("sess-b") == "sess-b"


def test_auto_plan_id_never_adopts_a_human_declared_plan():
    """The guard all three writers already enforce, now enforced one layer
    earlier: a hand-written `hive roadmap set` is nobody's to adopt."""
    _set(plan_id="sess-a", ts="2026-08-20T10:00:00Z", session="sess-a")
    _ev("interrupt", plan_id="sess-a", ts="2026-08-20T10:30:00Z",
        session="sess-a", cause="session_end")
    assert _resolve("sess-b") == "sess-b"


def test_auto_plan_id_does_not_resurrect_a_plan_abandoned_long_ago():
    """The bound that keeps adoption from being permanent. Without it, a plan
    paused in March is still "the work" in August, and an unrelated task
    inherits its ask forever."""
    _auto_set("sess-a", "sess-a", "2026-05-01T10:00:00Z")
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-05-01T10:30:00Z",
             cause="session_end")
    assert _resolve("sess-b") == "sess-b"


def test_auto_plan_id_adopts_the_newest_of_several_candidates():
    """Discriminating on purpose: with two adoptable plans, an implementation
    that returns the first match in fold order passes every test above and
    fails this one."""
    _auto_set("sess-old", "sess-old", "2026-08-19T10:00:00Z")
    _auto_ev("interrupt", "sess-old", "sess-old", "2026-08-19T10:30:00Z",
             cause="session_end")
    _auto_set("sess-new", "sess-new", "2026-08-20T09:00:00Z")
    _auto_ev("interrupt", "sess-new", "sess-new", "2026-08-20T09:30:00Z",
             cause="session_end")
    assert _resolve("sess-b") == "sess-new"


def test_auto_plan_id_stays_on_the_plan_this_session_already_continued():
    """The canary for a defect adoption creates on its own. Once session B has
    adopted A, A is ACTIVE again — so the interrupted-plan rule can no longer
    find it, and `autoclose_session("sess-b")` would resolve to a plan that
    does not exist and leave A active forever with nobody working it."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    _auto_set("sess-a", "sess-b", "2026-08-20T11:00:00Z")   # B adopted it
    assert _resolve("sess-b") == "sess-a"


def test_auto_plan_id_does_not_claim_a_plan_this_session_merely_retired():
    """`session_id` is stamped by the LAST event, and completing a plan is an
    event. Without the retirement check, the session that closed a plan would
    keep resolving to it and re-open it on the next TodoWrite."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("complete", "sess-a", "sess-b", "2026-08-20T11:00:00Z")
    assert _resolve("sess-b") == "sess-b"


# --- the writers, now that the question has one answer ------------------------

def _todo(text, status="pending"):
    return {"content": text, "status": status}


def test_sync_from_todos_continues_the_paused_plan_instead_of_minting_one():
    """The user-visible defect: a second session used to add a whole new plan
    while the real one sat under awaiting-resume, matching no live card."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    ev = roadmap.sync_from_todos("sess-b", [_todo("two", "in_progress")],
                                 ts=NOW)
    assert ev is not None and ev.payload["plan_id"] == "sess-a"
    reg = roadmap.fold(events.read_all())
    assert "sess-b" not in reg, "a second plan was minted anyway"
    assert reg["sess-a"]["status"] == "active", "the plan was not revived"
    # THE line that makes the dashboard able to find it again.
    assert reg["sess-a"]["session_id"] == "sess-b"


def test_continuing_a_plan_keeps_the_progress_it_already_made():
    """Discriminating, and the trap adoption sets for itself: `set` replaces the
    step list WHOLESALE, so carrying a plan forward while rebuilding its steps
    from the new session's TodoWrite alone would reset its progress to zero in
    the same breath as continuing it — the exact opposite of the ask."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z",
              steps=[{"text": "one", "status": "done"},
                     {"text": "two", "status": "pending"}])
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    ev = roadmap.sync_from_todos("sess-b", [_todo("two", "in_progress")],
                                ts=NOW)
    # Asserted on the event this call WROTE, not on the record it may have
    # left alone. Reading the fold straight away passes without the fix for
    # the emptiest possible reason: plan sess-a still holds its own untouched
    # steps while the sync quietly built a second plan beside it.
    assert ev is not None and ev.payload["plan_id"] == "sess-a"
    assert [s["text"] for s in ev.payload["steps"]] == ["one", "two"]
    rec = roadmap.fold(events.read_all())["sess-a"]
    assert rec["progress"]["done"] == 1 and rec["progress"]["total"] == 2


def test_continuing_a_plan_keeps_the_original_ask_and_retitles_it():
    """`Asked:` is the plan's provenance — the one field a resumed session must
    not overwrite. The title is the opposite: it names what is being worked
    NOW, so the card does not read as last session's job."""
    events.append_event("user_prompt", {"text": "carry on with the mirror"},
                        session_id="sess-b", ts="2026-08-20T11:00:00Z")
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    ev = roadmap.sync_from_todos("sess-b", [_todo("two", "in_progress")],
                                ts=NOW)
    assert ev is not None and ev.payload["plan_id"] == "sess-a"
    assert ev.payload["prompt"] == PROMPT
    assert ev.payload["title"] == "carry on with the mirror"


def test_its_own_plan_still_replaces_its_steps_wholesale():
    """The counterweight. History is carried for a CONTINUED plan only; inside
    the session that owns it, deleting a todo must still delete the step, or
    every plan grows forever and TodoWrite stops being authoritative."""
    roadmap.sync_from_todos("sess-a", [_todo("one", "completed"),
                                       _todo("two")], ts=NOW)
    roadmap.sync_from_todos("sess-a", [_todo("two")], ts=NOW)
    rec = roadmap.fold(events.read_all())["sess-a"]
    assert [s["text"] for s in rec["steps"]] == ["two"]


def test_autoclose_interrupts_the_plan_the_session_was_continuing():
    """Without the resolver this returned None: the session had no plan of its
    own, so nothing was interrupted and the plan stayed active with nobody
    working it — invisible in the awaiting-resume list it belongs in."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    roadmap.sync_from_todos("sess-b", [_todo("two", "in_progress")], ts=NOW)
    ev = roadmap.autoclose_session("sess-b", ts="2026-08-20T13:00:00Z")
    assert ev is not None and ev.payload["plan_id"] == "sess-a"
    rec = roadmap.fold(events.read_all())["sess-a"]
    assert rec["status"] == "interrupted"


def test_autoclose_completes_a_continued_plan_whose_work_is_finished():
    """The earned exit still has to be reachable through a continuation, or a
    plan carried across sessions can only ever be interrupted, never done."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z",
              steps=[{"text": "one", "status": "done"}])
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    roadmap.sync_from_todos("sess-b", [_todo("one", "completed")], ts=NOW)
    ev = roadmap.autoclose_session("sess-b", ts="2026-08-20T13:00:00Z")
    assert ev is not None and ev.payload["action"] == "complete"
    assert roadmap.fold(events.read_all())["sess-a"]["status"] == "completed"


# --- the index every reader of "whose plan is this" needs ---------------------

def test_by_session_maps_a_plan_to_the_session_working_it():
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("resume", "sess-a", "sess-b", "2026-08-20T11:00:00Z")
    idx = roadmap.by_session(roadmap.fold(events.read_all()))
    assert set(idx) == {"sess-b"}
    assert idx["sess-b"]["plan_id"] == "sess-a"


def test_by_session_drops_a_completed_plan():
    """A finished plan is not what a session is working on, and showing it on a
    live card is how the board claims progress that already ended."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("complete", "sess-a", "sess-a", "2026-08-20T11:00:00Z")
    assert roadmap.by_session(roadmap.fold(events.read_all())) == {}


def test_by_session_keeps_the_plan_the_session_touched_last():
    """Discriminating: a session that wrote to two plans has one current one,
    and an implementation that lets dict order decide passes without this."""
    _auto_set("plan-old", "sess-a", "2026-08-20T10:00:00Z")
    _auto_set("plan-new", "sess-a", "2026-08-20T11:00:00Z")
    idx = roadmap.by_session(roadmap.fold(events.read_all()))
    assert idx["sess-a"]["plan_id"] == "plan-new"


def test_by_session_drops_an_archived_plan():
    """F6. `RETIRED_STATUSES` is completed AND archived, and archiving is the
    DELIBERATE exit -- the user attached a reason to it. Reading only
    `completed` here put a plan the user had explicitly retired back onto a live
    Oversee card and back into the session cat's phase line."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("archive", "sess-a", "sess-a", "2026-08-20T11:00:00Z",
             reason="superseded by the mirror work")
    assert roadmap.by_session(roadmap.fold(events.read_all())) == {}


def test_by_session_ignores_a_human_declared_plan():
    """F7, first half: the AUTO predicate `auto_plan_id` already applies. A
    session that ran `hive roadmap step` against a hand-written plan was
    otherwise indexed here as working it, while the card kept showing the
    session's own auto plan -- one session, two surfaces, two answers."""
    _set(plan_id="plan-human", ts="2026-08-20T10:00:00Z", session="sess-a")
    assert roadmap.by_session(roadmap.fold(events.read_all())) == {}


def test_by_session_breaks_a_same_second_tie_the_way_the_writer_does():
    """F7, second half, and discriminating: two plans updated in the SAME
    second. Comparing `updated` alone leaves the winner to fold order, and
    same-second writes are routine enough here that the ledger carries its own
    tie-break for them. The writer compares `(updated, plan_id)`, so the reader
    has to land on `plan-b` -- fold order would hand back `plan-a`."""
    _auto_set("plan-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_set("plan-b", "sess-a", "2026-08-20T10:00:00Z")
    idx = roadmap.by_session(roadmap.fold(events.read_all()))
    assert idx["sess-a"]["plan_id"] == "plan-b"


def test_plan_for_session_prefers_the_plan_the_session_declared_itself():
    """The composition used to be spelled inline on the Oversee card while the
    session cat consulted the adoption index alone. Here they differ: the index
    names `plan-other` (newer), and the right answer is the session's own."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_set("plan-other", "sess-a", "2026-08-20T11:00:00Z")
    reg = roadmap.fold(events.read_all())
    assert roadmap.by_session(reg)["sess-a"]["plan_id"] == "plan-other"
    assert roadmap.plan_for_session(reg, "sess-a")["plan_id"] == "sess-a"


def test_plan_for_session_falls_through_when_its_own_plan_is_retired():
    """Completing a plan is how the user says not-this-one, and it has to mean
    that on every surface -- not only inside `plans()`. The session then shows
    the plan it adopted rather than the one it finished."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("complete", "sess-a", "sess-a", "2026-08-20T10:30:00Z")
    _auto_set("plan-other", "sess-a", "2026-08-20T11:00:00Z")
    reg = roadmap.fold(events.read_all())
    assert roadmap.plan_for_session(reg, "sess-a")["plan_id"] == "plan-other"


def test_plan_for_session_is_total_about_having_no_answer():
    """A session with no plan is the COMMON case -- most sessions never declare
    one -- so the helper returns None rather than raising, and an empty or
    missing registry is not a special case either."""
    assert roadmap.plan_for_session({}, "sess-a") is None
    assert roadmap.plan_for_session(None, "") is None


# --- the carry: what a continued plan keeps, and what has to make room --------

def test_continuing_a_plan_keeps_the_steps_that_are_still_left():
    """F1, and the half the progress test above cannot see. `set` replaces the
    step list WHOLESALE and the writer rebuilt it from this session's TodoWrite
    alone, so a step the adopted plan had NOT finished -- and that this session
    has not listed yet -- was deleted. The plan visibly shrank while `progress`
    rose, and "what is still left" is the thing the feature exists to show."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z",
              steps=[{"text": "one", "status": "done"},
                     {"text": "two", "status": "pending"},
                     {"text": "three", "status": "pending"}])
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    ev = roadmap.sync_from_todos("sess-b", [_todo("two", "in_progress")],
                                ts=NOW)
    assert ev is not None and ev.payload["plan_id"] == "sess-a"
    assert [s["text"] for s in ev.payload["steps"]] == ["one", "two", "three"]
    got = {s["text"]: s["status"] for s in ev.payload["steps"]}
    assert got["one"] == "done", "recorded progress was undone"
    assert got["two"] == "active", "the live session's own status lost"
    assert got["three"] == "pending"


def test_a_continued_plan_at_the_cap_keeps_this_sessions_own_todos():
    """F3. The trim used to cut the tail, which is where the LIVE session's
    steps sit -- and `autoclose_session` then read a plan with nothing
    unfinished and marked it COMPLETE. One cap collision and the session's whole
    visible workload left every view. Room is reserved for the live todos
    instead: a finished carried step already recorded its progress in the plan's
    history, a live todo has nowhere else to be."""
    steps = [{"text": f"old {i}", "status": "done"}
             for i in range(roadmap.MAX_STEPS)]
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z", steps=steps)
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    ev = roadmap.sync_from_todos("sess-b", [_todo("mine", "in_progress")],
                                ts=NOW)
    assert ev is not None and ev.payload["plan_id"] == "sess-a"
    texts = [s["text"] for s in ev.payload["steps"]]
    assert len(texts) == roadmap.MAX_STEPS, texts
    assert "mine" in texts, "the live session's own todo was trimmed away"
    assert "old 0" not in texts, "the trim took a live step over the oldest one"
    # The second half of the defect, and the reason it was worth a P1: a plan
    # trimmed down to finished steps looks DONE to the session-end writer.
    assert roadmap.autoclose_session(
        "sess-b", ts="2026-08-20T13:00:00Z").payload["action"] == "interrupt"


# --- session end: authorship, not adoption -----------------------------------

def test_autoclose_does_nothing_for_a_session_that_wrote_no_roadmap_event():
    """F5. `autoclose_session` resolved through `auto_plan_id`, which ADOPTS an
    interrupted plan inside the resume window -- right for a writer STARTING
    work, wrong for one ending it. Adoption at session end cannot mean the
    session worked the plan, and the all-steps-done branch would then `complete`
    a stranger's plan on the strength of it, retiring from every view a plan its
    real owner had only paused."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z",
              steps=[{"text": "one", "status": "done"}])
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:30:00Z",
             cause="session_end")
    n = len(events.read_all())
    assert roadmap.autoclose_session("sess-b", ts="2026-08-20T13:00:00Z") is None
    assert len(events.read_all()) == n, "an event was written about someone else"
    assert roadmap.fold(events.read_all())["sess-a"]["status"] == "interrupted"


def test_autoclose_closes_the_plan_this_session_authored_not_one_it_could_adopt():
    """The discriminating case for resolving by AUTHORSHIP, and the one a
    `session_id` stamp cannot answer. B worked plan A; C then wrote to the same
    record, so the fold's `session_id` is C's and "the plan whose session_id is
    B" finds nothing. Plan D meanwhile sits interrupted inside the window --
    resolving the way a STARTING writer does hands B plan D, a plan it never
    touched, and closes that instead. B's own `set` is still in the log."""
    _auto_set("sess-a", "sess-a", "2026-08-20T10:00:00Z")
    _auto_ev("interrupt", "sess-a", "sess-a", "2026-08-20T10:15:00Z",
             cause="session_end")
    roadmap.sync_from_todos("sess-b", [_todo("two", "in_progress")],
                            ts="2026-08-20T10:30:00Z")     # B continues A
    _auto_set("sess-a", "sess-c", "2026-08-20T11:00:00Z")  # C overwrites it
    _auto_set("plan-d", "sess-d", "2026-08-20T11:30:00Z")  # an adoptable...
    _auto_ev("interrupt", "plan-d", "sess-d", "2026-08-20T11:45:00Z",
             cause="session_end")                          # ...paused plan
    ev = roadmap.autoclose_session("sess-b", ts="2026-08-20T13:00:00Z")
    assert ev is not None and ev.payload["plan_id"] == "sess-a", (
        "session end closed a plan this session never wrote to")
    assert roadmap.fold(events.read_all())["sess-a"]["status"] == "interrupted"


def test_a_pause_stamped_in_the_future_is_not_a_recent_pause():
    """F13. Candidates are RANKED by that same string, so an unbounded pause
    wins every round forever and every genuinely recent plan loses to it -- one
    bad clock or one hand-written row redirects adoption permanently. The window
    has a far edge for the same reason it has a near one."""
    _auto_set("plan-future", "sess-x", "2026-08-20T10:00:00Z")
    _auto_ev("interrupt", "plan-future", "sess-x", "2027-01-01T00:00:00Z",
             cause="session_end")
    _auto_set("plan-real", "sess-y", "2026-08-20T11:00:00Z")
    _auto_ev("interrupt", "plan-real", "sess-y", "2026-08-20T11:30:00Z",
             cause="session_end")
    assert _resolve("sess-b") == "plan-real"


@pytest.mark.parametrize("action,kw", [("complete", {}),
                                       ("archive", {"reason": "superseded"})])
def test_a_session_revives_its_own_retired_plan_on_the_next_todowrite(action, kw):
    """The limit of the escape hatch, measured rather than assumed. `complete`
    and `archive` stop ADOPTION dead -- both other rules test RETIRED_STATUSES --
    but rule 1 returns the session's own plan without looking at its status, so
    one more TodoWrite sets it active again.

    Pinned rather than fixed. The behaviour predates the resolver (`plan_id` was
    unconditionally `sid`, with no retired guard anywhere), and refusing it needs
    a second plan id for post-retirement work in the same session -- a schema
    decision, not a defect fix. What was wrong was the docstring claiming
    otherwise; this test is what makes the claim checkable either way, so a
    future change of mind goes red here instead of surprising a reader."""
    roadmap.sync_from_todos(SID, TODOS, ts="2026-08-20T10:00:00Z")
    _auto_ev(action, SID, SID, "2026-08-20T10:30:00Z", **kw)
    reg = roadmap.fold(events.read_all())
    assert reg[SID]["status"] == ("completed" if action == "complete"
                                 else "archived")
    assert roadmap.auto_plan_id(SID, reg, now="2026-08-20T11:00:00Z") == SID
    assert roadmap.sync_from_todos(
        SID, [_todo("something new")], ts="2026-08-20T11:00:00Z") is not None
    assert roadmap.fold(events.read_all())[SID]["status"] == "active"
    # The counterweight, and the half that IS guarded: another session gets
    # nothing from a retired plan, whichever way it was retired.
    assert roadmap.auto_plan_id(
        "sess-other", roadmap.fold(events.read_all()),
        now="2026-08-20T11:30:00Z") == "sess-other"
