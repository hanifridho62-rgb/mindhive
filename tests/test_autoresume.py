# tests/test_autoresume.py
"""Auto-resume delivery: the event contract, the deterministic fold, the
composed resume prompt, schedule/cancel/fire/wait lifecycle, the STATE.md
delivery-status lines, and the `hive autoresume` CLI."""
import calendar
import json
import time

import pytest

from mindhive import __main__ as cli_mod
from mindhive import autoresume, events, paths, projector, roadmap

PROMPT = "Ship the session-resumption marker end to end"
STEPS = ["capture the marker", "schedule the delivery", "fire into claude"]

T0 = "2026-07-18T00:00:00Z"           # interruption time used across tests
T0_EPOCH = calendar.timegm(time.strptime(T0, "%Y-%m-%dT%H:%M:%SZ"))

OK_ENVELOPE = json.dumps({"result": "continued work; marker delivered",
                          "is_error": False, "session_id": "x"})
LIMIT_ENVELOPE = json.dumps({"is_error": True, "api_error_status": 429,
                             "result": "You've hit your usage limit; "
                                       "resets at 3pm"})
ERROR_ENVELOPE = json.dumps({"is_error": True, "api_error_status": 500,
                             "result": "something broke"})


def _interrupted(plan_id="mark-1", cause="usage_limit", ts=T0):
    events.append_event(
        "roadmap", {"action": "set", "plan_id": plan_id, "prompt": PROMPT,
                    "steps": [{"text": STEPS[0], "status": "done"},
                              {"text": STEPS[1], "status": "active",
                               "note": "half-built"},
                              {"text": STEPS[2], "status": "pending"}]},
        session_id="s1", ts="2026-07-17T23:00:00Z", tags=["roadmap"])
    events.append_event(
        "roadmap", {"action": "interrupt", "plan_id": plan_id,
                    "cause": cause},
        session_id="s1", ts=ts, tags=["roadmap"])


def _no_timer(plan_id, due, cwd, project):
    return "none"


# --- payload contract ---------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "free text",                                                # not a dict
    {"action": "snoozed", "plan_id": "x-1"},                    # bad action
    {"action": "scheduled", "plan_id": "Bad_ID!",
     "due": "2026-07-18T05:00:00Z", "attempt": 1},
    {"action": "scheduled", "plan_id": "x-1", "attempt": 1},    # no due
    {"action": "scheduled", "plan_id": "x-1", "due": "tomorrow",
     "attempt": 1},
    {"action": "scheduled", "plan_id": "x-1",
     "due": "2026-07-18T05:00:00Z"},                            # no attempt
    {"action": "scheduled", "plan_id": "x-1",
     "due": "2026-07-18T05:00:00Z", "attempt": 0},
    {"action": "scheduled", "plan_id": "x-1",
     "due": "2026-07-18T05:00:00Z", "attempt": True},
    {"action": "scheduled", "plan_id": "x-1",
     "due": "2026-07-18T05:00:00Z", "attempt": 1,
     "cooldown_hours": 0},
    {"action": "scheduled", "plan_id": "x-1",
     "due": "2026-07-18T05:00:00Z", "attempt": 1, "cwd": "c" * 501},
    {"action": "scheduled", "plan_id": "x-1",
     "due": "2026-07-18T05:00:00Z", "attempt": 1, "model": "m" * 101},
    {"action": "scheduled", "plan_id": "x-1",
     "due": "2026-07-18T05:00:00Z", "attempt": 1, "timer": "cron"},
    {"action": "fired", "plan_id": "x-1", "attempt": 1},        # no outcome
    {"action": "fired", "plan_id": "x-1", "attempt": 1,
     "outcome": "great"},
    {"action": "fired", "plan_id": "x-1", "outcome": "delivered"},
    {"action": "fired", "plan_id": "x-1", "attempt": 1,
     "outcome": "error", "detail": "d" * 501},
])
def test_validate_payload_rejects(payload):
    with pytest.raises(ValueError):
        autoresume.validate_payload(payload)


def test_validate_payload_accepts_lifecycle():
    autoresume.validate_payload(
        {"action": "scheduled", "plan_id": "x-1",
         "due": "2026-07-18T05:00:00Z", "attempt": 1,
         "cooldown_hours": 5.0, "cwd": "C:/repo", "model": "",
         "timer": "windows_task"})
    autoresume.validate_payload(
        {"action": "fired", "plan_id": "x-1", "attempt": 2,
         "outcome": "still_limited", "detail": "429"})
    autoresume.validate_payload({"action": "cancelled", "plan_id": "x-1"})


def test_is_usage_cause():
    assert autoresume.is_usage_cause("usage_limit")
    assert autoresume.is_usage_cause("api_cap")
    assert autoresume.is_usage_cause("budget_cap")
    assert autoresume.is_usage_cause("weekly usage exhausted")
    assert autoresume.is_usage_cause("Rate-LIMIT hit")
    assert not autoresume.is_usage_cause("session_end")
    assert not autoresume.is_usage_cause("")
    assert not autoresume.is_usage_cause(None)


def test_due_at_adds_cooldown_and_never_lands_in_the_past():
    assert autoresume.due_at(T0, 5.0, now=T0_EPOCH) == "2026-07-18T05:00:00Z"
    # cooldown already elapsed at scheduling time -> near-immediate delivery
    late = T0_EPOCH + 6 * 3600
    assert autoresume.due_at(T0, 5.0, now=late) == \
        autoresume._iso(late + autoresume.MIN_LEAD_S)
    # unparseable interruption time falls back to now + cooldown
    assert autoresume.due_at("", 5.0, now=T0_EPOCH) == "2026-07-18T05:00:00Z"


# --- fold ---------------------------------------------------------------------

def test_fold_tracks_schedule_fire_cancel():
    events.append_event("autoresume",
                        {"action": "scheduled", "plan_id": "mark-1",
                         "due": "2026-07-18T05:00:00Z", "attempt": 1,
                         "cooldown_hours": 5.0, "cwd": "C:/repo",
                         "model": "opus", "timer": "windows_task"},
                        session_id="s1", ts="2026-07-18T00:00:01Z")
    rec = autoresume.fold(events.read_all())["mark-1"]
    assert rec["status"] == "scheduled"
    assert rec["due"] == "2026-07-18T05:00:00Z"
    assert rec["attempt"] == 1 and rec["timer"] == "windows_task"
    assert rec["cwd"] == "C:/repo" and rec["model"] == "opus"
    events.append_event("autoresume",
                        {"action": "fired", "plan_id": "mark-1",
                         "attempt": 1, "outcome": "delivered",
                         "detail": "ok"},
                        session_id="s1", ts="2026-07-18T05:00:02Z")
    rec = autoresume.fold(events.read_all())["mark-1"]
    assert rec["status"] == "fired" and rec["outcome"] == "delivered"
    assert rec["cwd"] == "C:/repo"                    # carried across actions
    events.append_event("autoresume",
                        {"action": "cancelled", "plan_id": "mark-1"},
                        session_id="s1", ts="2026-07-18T05:00:03Z")
    assert autoresume.fold(events.read_all())["mark-1"]["status"] \
        == "cancelled"


def test_fold_is_total_on_junk_history():
    events.append_event("autoresume", "free text", session_id="s1")
    events.append_event("autoresume", {"action": "scheduled",
                                       "plan_id": "bad-due", "due": "soon",
                                       "attempt": 1}, session_id="s1")
    events.append_event("autoresume", {"action": "fired", "plan_id": "odd",
                                       "attempt": 1, "outcome": "great"},
                        session_id="s1")
    events.append_event("autoresume", {"action": "snooze",
                                       "plan_id": "mark-1"}, session_id="s1")
    # a fired row without a prior schedule still folds (total on history)
    events.append_event("autoresume", {"action": "fired", "plan_id": "orphan",
                                       "attempt": 3, "outcome": "skipped"},
                        session_id="s1")
    recs = autoresume.fold(events.read_all())
    assert set(recs) == {"orphan"}
    assert recs["orphan"]["status"] == "fired"
    assert recs["orphan"]["attempt"] == 3


def test_pending_orders_by_due():
    for pid, due in (("b-plan", "2026-07-18T06:00:00Z"),
                     ("a-plan", "2026-07-18T05:00:00Z")):
        events.append_event("autoresume",
                            {"action": "scheduled", "plan_id": pid,
                             "due": due, "attempt": 1}, session_id="s1")
    assert [r["plan_id"] for r in autoresume.pending(events.read_all())] \
        == ["a-plan", "b-plan"]


# --- the composed resume prompt ----------------------------------------------

def test_resume_prompt_carries_ask_progress_and_directives():
    _interrupted()
    rec = roadmap.get(events.read_all(), "mark-1")
    text = autoresume.resume_prompt(rec, attempt=2)
    assert PROMPT in text                               # the ask, verbatim
    assert "cause: usage_limit" in text and T0 in text
    assert "delivery attempt 2" in text
    assert "1/3 steps done" in text
    assert "0. [x] capture the marker" in text
    assert "1. [>] schedule the delivery — half-built  <- NEXT" in text
    assert "2. [ ] fire into claude" in text
    assert "hive roadmap resume mark-1" in text
    assert "hive roadmap step mark-1" in text
    assert "hive roadmap complete mark-1" in text
    assert "hive roadmap interrupt mark-1" in text
    assert "hive log" in text                           # repo discipline rides


# --- schedule -----------------------------------------------------------------

def test_schedule_requires_an_interrupted_plan():
    with pytest.raises(ValueError, match="no roadmap"):
        autoresume.schedule("ghost", register=_no_timer)
    _interrupted()
    events.append_event("roadmap", {"action": "resume", "plan_id": "mark-1"},
                        session_id="s1", ts="2026-07-18T00:30:00Z")
    with pytest.raises(ValueError, match="not interrupted"):
        autoresume.schedule("mark-1", register=_no_timer)


def test_schedule_computes_due_from_interruption_plus_cooldown():
    _interrupted()
    seen = {}

    def register(plan_id, due, cwd, project):
        seen.update(plan_id=plan_id, due=due, cwd=cwd, project=project)
        return "windows_task"

    ev = autoresume.schedule("mark-1", cwd="C:/repo", model="opus",
                             register=register, now=T0_EPOCH)
    p = ev.payload
    assert p["due"] == "2026-07-18T05:00:00Z"           # T0 + 5h
    assert p["attempt"] == 1 and p["timer"] == "windows_task"
    assert p["cwd"] == "C:/repo" and p["model"] == "opus"
    # the registrar gets the RESOLVED namespace (conftest pins "test"), so
    # the OS task's `fire --project …` reads this store wherever it runs
    assert seen == {"plan_id": "mark-1", "due": "2026-07-18T05:00:00Z",
                    "cwd": "C:/repo", "project": "test"}
    assert ev.tags == ["autoresume"]


def test_schedule_honors_explicit_at_and_rejects_junk_at():
    _interrupted()
    ev = autoresume.schedule("mark-1", at="2026-07-19T09:00:00Z",
                             register=_no_timer, now=T0_EPOCH)
    assert ev.payload["due"] == "2026-07-19T09:00:00Z"
    with pytest.raises(ValueError, match="--at"):
        autoresume.schedule("mark-1", at="five hours", register=_no_timer)


def test_schedule_survives_a_broken_timer_registrar():
    _interrupted()

    def boom(*a):
        raise OSError("no schtasks here")

    ev = autoresume.schedule("mark-1", register=boom, now=T0_EPOCH)
    assert ev.payload["timer"] == "none"                # event still recorded
    assert autoresume.pending(events.read_all())        # wait() can deliver


def test_register_timer_is_env_gated_off():
    # conftest sets MINDHIVE_AUTORESUME_TIMER=off: the default registrar must
    # not touch the real Task Scheduler from inside the suite.
    assert autoresume._register_timer(
        "mark-1", "2026-07-18T05:00:00Z", "C:/repo", "test") == "none"


# --- cancel -------------------------------------------------------------------

def test_cancel_requires_a_pending_delivery():
    with pytest.raises(ValueError, match="nothing to cancel"):
        autoresume.cancel("mark-1")


def test_cancel_withdraws_schedule_and_timer():
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)
    gone = []
    autoresume.cancel("mark-1", unregister=gone.append)
    assert gone == ["mark-1"]
    assert autoresume.fold(events.read_all())["mark-1"]["status"] \
        == "cancelled"
    assert autoresume.pending(events.read_all()) == []


# --- fire ---------------------------------------------------------------------

def test_fire_delivers_the_marker_prompt_to_claude():
    _interrupted()
    autoresume.schedule("mark-1", cwd="C:/definitely/missing/dir",
                        model="opus", register=_no_timer, now=T0_EPOCH)
    calls, gone = {}, []

    def runner(prompt, model=None, cwd=None, timeout_s=None):
        calls.update(prompt=prompt, model=model, cwd=cwd,
                     timeout_s=timeout_s)
        return {"exit": 0, "stdout": OK_ENVELOPE}

    payload = autoresume.fire("mark-1", runner=runner,
                              unregister=gone.append)
    assert payload["outcome"] == "delivered"
    assert payload["detail"] == "continued work; marker delivered"
    assert PROMPT in calls["prompt"] and "<- NEXT" in calls["prompt"]
    assert calls["model"] == "opus"
    assert calls["cwd"] is None            # vanished cwd degrades to default
    assert calls["timeout_s"] == autoresume.FIRE_TIMEOUT_S
    assert gone == ["mark-1"]              # delivered -> timer cleaned up
    rec = autoresume.fold(events.read_all())["mark-1"]
    assert rec["status"] == "fired" and rec["outcome"] == "delivered"


def test_fire_still_limited_reschedules_with_backoff():
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)
    now = T0_EPOCH + 5 * 3600
    payload = autoresume.fire(
        "mark-1", runner=lambda *a, **k: {"exit": 0,
                                          "stdout": LIMIT_ENVELOPE},
        register=_no_timer, now=now)
    assert payload["outcome"] == "still_limited"
    rec = autoresume.fold(events.read_all())["mark-1"]
    assert rec["status"] == "scheduled"                 # re-armed
    assert rec["attempt"] == 2
    assert rec["due"] == autoresume._iso(
        now + autoresume.RETRY_HOURS * 3600)


def test_fire_stops_retrying_at_max_attempts():
    _interrupted()
    events.append_event("autoresume",
                        {"action": "scheduled", "plan_id": "mark-1",
                         "due": "2026-07-18T05:00:00Z",
                         "attempt": autoresume.MAX_ATTEMPTS},
                        session_id="s1", ts="2026-07-18T00:30:00Z")
    gone = []
    payload = autoresume.fire(
        "mark-1", runner=lambda *a, **k: {"exit": 0,
                                          "stdout": LIMIT_ENVELOPE},
        register=_no_timer, unregister=gone.append)
    assert payload["outcome"] == "still_limited"
    assert "gave up" in payload["detail"]               # says why nothing is next
    assert autoresume.fold(events.read_all())["mark-1"]["status"] == "fired"
    assert gone == ["mark-1"]                           # gave up cleanly


def test_fire_honours_a_schedule_issued_while_the_delivery_ran():
    """The one-shot delivery runs for up to an hour; a `hive autoresume
    schedule --at` issued meanwhile is the human's word on when the next
    delivery is due, not the RETRY_HOURS backoff."""
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)
    quiet = autoresume._iso(time.time() + 30 * 3600)

    def runner(prompt, model=None, cwd=None, timeout_s=None):
        autoresume.schedule("mark-1", at=quiet, register=_no_timer)
        return {"exit": 0, "stdout": LIMIT_ENVELOPE}

    payload = autoresume.fire("mark-1", runner=runner, register=_no_timer,
                              now=T0_EPOCH + 5 * 3600)
    assert payload["outcome"] == "still_limited"
    assert "by hand" in payload["detail"]
    rec = autoresume.fold(events.read_all())["mark-1"]
    assert (rec["status"], rec["due"], rec["attempt"]) == \
        ("scheduled", quiet, 1)


def test_fire_does_not_re_arm_a_plan_resumed_while_the_delivery_ran():
    """A human who ran `hive roadmap resume` while the delivery was out has
    the plan: the still-limited delivery records why nothing is next instead
    of failing on the schedule's own guard after its row landed."""
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)

    def runner(prompt, model=None, cwd=None, timeout_s=None):
        roadmap.record("resume", "mark-1")
        return {"exit": 0, "stdout": LIMIT_ENVELOPE}

    gone = []
    payload = autoresume.fire(
        "mark-1", runner=runner, unregister=gone.append,
        register=lambda *a: pytest.fail("a resumed plan re-armed"))
    assert payload["outcome"] == "still_limited"
    assert "no longer interrupted (active)" in payload["detail"]
    assert autoresume.fold(events.read_all())["mark-1"]["status"] == "fired"
    assert gone == ["mark-1"]


def test_fire_error_records_without_retry():
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)
    payload = autoresume.fire(
        "mark-1", runner=lambda *a, **k: {"exit": 1,
                                          "stdout": ERROR_ENVELOPE})
    assert payload["outcome"] == "error"
    assert "500" in payload["detail"]
    assert autoresume.fold(events.read_all())["mark-1"]["status"] == "fired"


def test_fire_skips_a_plan_a_human_already_resumed():
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)
    events.append_event("roadmap", {"action": "resume", "plan_id": "mark-1"},
                        session_id="s1", ts="2026-07-18T01:00:00Z")
    ran = []
    payload = autoresume.fire("mark-1", runner=lambda *a, **k: ran.append(1))
    assert payload["outcome"] == "skipped"
    assert ran == []                                    # claude never invoked


def test_classify_maps_envelopes_to_outcomes():
    assert autoresume._classify({"exit": 0, "stdout": OK_ENVELOPE})[0] \
        == "delivered"
    assert autoresume._classify({"exit": 0, "stdout": LIMIT_ENVELOPE})[0] \
        == "still_limited"
    assert autoresume._classify({"exit": 1, "stdout": ERROR_ENVELOPE})[0] \
        == "error"
    assert autoresume._classify({"exit": 127, "stdout": ""}) \
        == ("error", "claude CLI not found")
    assert autoresume._classify({"exit": 124, "stdout": ""}) \
        == ("error", "timed out")


# --- wait (portable fallback) -------------------------------------------------

def test_wait_sleeps_until_due_then_fires():
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)
    due = T0_EPOCH + 5 * 3600
    clock = {"now": due - 90}
    naps, fired = [], []

    def sleep_fn(s):
        naps.append(s)
        clock["now"] += s

    def fire_fn(plan_id, project=None):
        fired.append(plan_id)
        autoresume.fire(plan_id, project=project,
                        runner=lambda *a, **k: {"exit": 0,
                                                "stdout": OK_ENVELOPE})

    n = autoresume.wait(poll_s=60, now_fn=lambda: clock["now"],
                        sleep_fn=sleep_fn, fire_fn=fire_fn)
    assert n == 1 and fired == ["mark-1"]
    assert naps and max(naps) <= 60                     # capped by poll_s


def test_wait_returns_immediately_when_nothing_is_scheduled():
    assert autoresume.wait(now_fn=lambda: 0,
                           sleep_fn=lambda s: pytest.fail("slept")) == 0


def test_wait_stuck_guard_exits_on_a_noop_fire():
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)
    n = autoresume.wait(now_fn=lambda: T0_EPOCH + 6 * 3600,
                        sleep_fn=lambda s: None,
                        fire_fn=lambda *a, **k: None)   # changes nothing
    assert n == 1                                       # fired once, no spin


# --- delivery status rides the marker (STATE.md) ------------------------------

def test_notice_shows_the_scheduled_delivery():
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)
    note = roadmap.notice(events.read_all())
    assert "Auto-resume:" in note
    assert "2026-07-18T05:00:00Z" in note
    assert "hive autoresume fire mark-1" in note
    assert "hive autoresume schedule <id>" in note      # header hint


def test_notice_shows_the_last_delivery_outcome():
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)
    autoresume.fire("mark-1",
                    runner=lambda *a, **k: {"exit": 1,
                                            "stdout": ERROR_ENVELOPE})
    note = roadmap.notice(events.read_all())
    assert "delivery error" in note


def test_projector_delivers_autoresume_status_into_state():
    _interrupted()
    autoresume.schedule("mark-1", register=_no_timer, now=T0_EPOCH)
    projector.project()
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "Auto-resume:" in state
    assert "hive autoresume fire mark-1" in state


# --- registry discipline ------------------------------------------------------

def test_autoresume_stays_out_of_semantic_projections():
    from mindhive import consolidate
    from mindhive import briefing
    assert "autoresume" in events.DETERMINISTIC_TYPES
    assert "autoresume" not in consolidate.CONSOLIDATE_TYPES
    assert "autoresume" not in briefing._TIMELINE_TYPES
    assert "autoresume" not in events.MEMORY_VIEW_TYPES


# --- CLI ----------------------------------------------------------------------

def test_cli_schedule_defaults_to_latest_interrupted_plan(capsys):
    _interrupted("old-mark", ts="2026-07-17T22:00:00Z")
    _interrupted()                                      # mark-1 @ T0 (newer)
    assert cli_mod.main(["autoresume", "schedule"]) == 0
    out = capsys.readouterr().out
    assert "autoresume scheduled: mark-1" in out
    assert "timer: none" in out                         # env-gated off
    assert "hive autoresume wait" in out                # fallback pointer


def test_cli_schedule_errors_without_an_interrupted_plan(capsys):
    assert cli_mod.main(["autoresume", "schedule"]) == 2
    assert "no interrupted roadmap" in capsys.readouterr().err
    _interrupted()
    assert cli_mod.main(["autoresume", "schedule", "ghost"]) == 2
    assert "no roadmap" in capsys.readouterr().err


def test_cli_list_and_cancel(capsys):
    _interrupted()
    assert cli_mod.main(["autoresume", "list"]) == 0
    assert "no auto-resume deliveries" in capsys.readouterr().out
    cli_mod.main(["autoresume", "schedule", "mark-1"])
    capsys.readouterr()
    assert cli_mod.main(["autoresume", "list"]) == 0
    assert "[mark-1] fires" in capsys.readouterr().out
    assert cli_mod.main(["autoresume", "cancel", "mark-1"]) == 0
    assert "autoresume cancelled" in capsys.readouterr().out
    assert cli_mod.main(["autoresume", "cancel", "mark-1"]) == 2
    assert "nothing to cancel" in capsys.readouterr().err


def test_cli_fire_uses_the_default_runner(capsys, monkeypatch):
    _interrupted()
    cli_mod.main(["autoresume", "schedule", "mark-1"])
    capsys.readouterr()
    seen = {}

    def fake_run(prompt, model=None, cwd=None, timeout_s=None):
        seen.update(prompt=prompt, timeout_s=timeout_s)
        return {"exit": 0, "stdout": OK_ENVELOPE}

    monkeypatch.setattr(autoresume, "_run_claude", fake_run)
    assert cli_mod.main(["autoresume", "fire", "mark-1",
                         "--timeout", "120"]) == 0
    assert "-> delivered" in capsys.readouterr().out
    assert PROMPT in seen["prompt"] and seen["timeout_s"] == 120


def test_cli_fire_error_exits_1(capsys, monkeypatch):
    _interrupted()
    monkeypatch.setattr(
        autoresume, "_run_claude",
        lambda *a, **k: {"exit": 1, "stdout": ERROR_ENVELOPE})
    assert cli_mod.main(["autoresume", "fire", "mark-1"]) == 1
    assert "-> error" in capsys.readouterr().out


def test_cli_roadmap_interrupt_auto_schedules_usage_causes(capsys):
    events.append_event(
        "roadmap", {"action": "set", "plan_id": "mark-1", "prompt": PROMPT,
                    "steps": STEPS}, session_id="s1", ts=T0, tags=["roadmap"])
    assert cli_mod.main(["roadmap", "interrupt", "mark-1",
                         "--cause", "usage_limit"]) == 0
    out = capsys.readouterr().out
    assert "autoresume scheduled: fires" in out
    assert autoresume.pending(events.read_all())


def test_cli_roadmap_interrupt_respects_no_auto_resume_and_cause(capsys):
    events.append_event(
        "roadmap", {"action": "set", "plan_id": "mark-1", "prompt": PROMPT,
                    "steps": STEPS}, session_id="s1", ts=T0, tags=["roadmap"])
    assert cli_mod.main(["roadmap", "interrupt", "mark-1",
                         "--cause", "usage_limit", "--no-auto-resume"]) == 0
    assert autoresume.fold(events.read_all()) == {}
    cli_mod.main(["roadmap", "resume", "mark-1"])
    assert cli_mod.main(["roadmap", "interrupt", "mark-1",
                         "--cause", "lunch_break"]) == 0
    assert autoresume.fold(events.read_all()) == {}     # not usage-shaped


def test_cli_roadmap_resume_cancels_the_pending_delivery(capsys):
    _interrupted()
    cli_mod.main(["autoresume", "schedule", "mark-1"])
    capsys.readouterr()
    assert cli_mod.main(["roadmap", "resume", "mark-1"]) == 0
    assert "autoresume cancelled: mark-1" in capsys.readouterr().out
    assert autoresume.fold(events.read_all())["mark-1"]["status"] \
        == "cancelled"


def test_cli_roadmap_complete_cancels_the_pending_delivery(capsys):
    _interrupted()
    cli_mod.main(["autoresume", "schedule", "mark-1"])
    capsys.readouterr()
    assert cli_mod.main(["roadmap", "complete", "mark-1"]) == 0
    assert "autoresume cancelled: mark-1" in capsys.readouterr().out


def test_cli_append_enforces_autoresume_contract(capsys):
    assert cli_mod.main(["append", "autoresume", "free text"]) == 2
    assert "hive autoresume" in capsys.readouterr().err
    good = ('{"action": "scheduled", "plan_id": "mark-1", '
            '"due": "2026-07-18T05:00:00Z", "attempt": 1}')
    assert cli_mod.main(["append", "autoresume", good, "--json"]) == 0
    evs = events.read_all()
    assert len(evs) == 1 and evs[0].type == "autoresume"
