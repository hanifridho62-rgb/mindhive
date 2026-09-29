# tests/test_fabricate.py
"""PRD assembly line: PRD parsing, the compile -> dispatch -> run lifecycle,
the fabrication event contract and fold, loop-mode auto-resume delivery,
and the `hive fabricate` CLI."""
import calendar
import json
import time
from pathlib import Path

import pytest

from mindhive import __main__ as cli_mod
from mindhive import autoresume, events, fabricate, roadmap

T0 = "2026-07-18T00:00:00Z"
T0_EPOCH = calendar.timegm(time.strptime(T0, "%Y-%m-%dT%H:%M:%SZ"))

PRD_MD = """TITLE: Wire the enrichment panel toggle
SUMMARY: Add a per-session toggle so enrichment can be muted.

## Problem & context

Users cannot mute enrichment today.

## Goal & success metric

A working toggle; suite stays green.

## Requirements

- No new dependencies.

## SOP

1. Use the python-development:python-code-style skill to survey the module.
2. Use the ui-design:create-component skill to build the toggle.
3. Use the tdd-workflows:tdd-cycle skill to cover it with tests.

## Acceptance criteria & verification

- Toggle persists across reloads.
- Full pytest suite green.
"""

PRD_CONTRACT_STYLE = """TITLE: Tune the recall ranker
SUMMARY: Rank pinned memories first.

(1) Problem & context — recall buries pinned caveats.
(2) Goal & success metric — pinned always in the top 3.
(3) Requirements — no schema change.
(4) SOP — a numbered build procedure:
1. Use mindhive-memory-search to map the ranker.
2. Use python-development:python-testing-patterns to add regression tests.
(5) Acceptance criteria & verification:
- Pinned caveat ranks first in the fixture store.
"""


def _no_timer(*a):
    return "none"


# --- PRD parsing --------------------------------------------------------------

def test_parse_prd_markdown_headings():
    prd = fabricate.parse_prd(PRD_MD)
    assert prd["title"] == "Wire the enrichment panel toggle"
    assert prd["summary"].startswith("Add a per-session toggle")
    assert "A working toggle" in prd["goal"]
    assert len(prd["sop"]) == 3
    assert prd["sop"][0].startswith("Use the python-development")
    assert prd["acceptance"] == ["Toggle persists across reloads.",
                                 "Full pytest suite green."]


def test_parse_prd_contract_numbering_style():
    prd = fabricate.parse_prd(PRD_CONTRACT_STYLE)
    assert prd["title"] == "Tune the recall ranker"
    assert len(prd["sop"]) == 2
    assert prd["sop"][1].startswith("Use python-development")
    assert prd["acceptance"] == \
        ["Pinned caveat ranks first in the fixture store."]


def test_parse_prd_without_sop_is_an_error():
    with pytest.raises(ValueError, match="no SOP"):
        fabricate.parse_prd("TITLE: X\nSUMMARY: Y\n\n## Goal\n\nnothing\n")


def test_slug_fits_the_id_contract():
    from mindhive.sop import ID_RE
    assert fabricate.slug("Wire the Enrichment Panel!") \
        == "fab-wire-the-enrichment-panel"
    assert ID_RE.match(fabricate.slug("??!!"))
    assert ID_RE.match(fabricate.slug("x" * 300))


# --- payload contract ---------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "free text",
    {"action": "assembled", "fab_id": "fab-x"},
    {"action": "compiled", "fab_id": "Bad!", "goal_dir": "d", "steps": 1},
    {"action": "compiled", "fab_id": "fab-x", "steps": 1},   # no goal_dir
    {"action": "compiled", "fab_id": "fab-x", "goal_dir": "d", "steps": 0},
    {"action": "compiled", "fab_id": "fab-x", "goal_dir": "d",
     "steps": True},
    {"action": "compiled", "fab_id": "fab-x", "goal_dir": "d" * 501,
     "steps": 1},
    {"action": "dispatched", "fab_id": "fab-x"},             # no due
    {"action": "dispatched", "fab_id": "fab-x", "due": "soon"},
    {"action": "dispatched", "fab_id": "fab-x",
     "due": "2026-07-18T05:00:00Z", "timer": "cron"},
    {"action": "run", "fab_id": "fab-x"},                    # no run_id
    {"action": "run", "fab_id": "fab-x", "run_id": "r1",
     "iterations": -1},
    {"action": "run", "fab_id": "fab-x", "run_id": "r1",
     "reason": "r" * 61},
])
def test_validate_payload_rejects(payload):
    with pytest.raises(ValueError):
        fabricate.validate_payload(payload)


def test_validate_payload_accepts_lifecycle():
    fabricate.validate_payload({"action": "compiled", "fab_id": "fab-x",
                                "goal_dir": "C:/repo/.hive-fab/fab-x",
                                "title": "T", "steps": 3})
    fabricate.validate_payload({"action": "dispatched", "fab_id": "fab-x",
                                "due": "2026-07-18T05:00:00Z",
                                "timer": "windows_task", "cwd": "C:/repo"})
    fabricate.validate_payload({"action": "run", "fab_id": "fab-x",
                                "run_id": "abc123", "reason": "completed",
                                "iterations": 4})


# --- compile ------------------------------------------------------------------

def test_compile_materializes_goal_dir_and_roadmap(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path,
                                       source="prd.md")
    assert rec["fab_id"] == "fab-wire-the-enrichment-panel-toggle"
    goal = tmp_path / ".hive-fab" / rec["fab_id"]
    assert str(goal) == rec["goal_dir"]
    prompt = (goal / "PROMPT.md").read_text(encoding="utf-8")
    assert prompt.startswith("# Wire the enrichment panel toggle")
    assert "Acceptance criteria" in prompt
    assert "Toggle persists across reloads." in prompt
    assert "hive log" in prompt                       # discipline rides along
    plan = (goal / "fix_plan.md").read_text(encoding="utf-8")
    assert plan.count("- [ ]") == 3
    assert "tdd-workflows:tdd-cycle" in plan
    assert (goal / "loop.toml").exists() and (goal / "AGENT.md").exists()
    rm = roadmap.get(events.read_all(), rec["fab_id"])
    assert rm["status"] == "active"
    assert fabricate.FAB_TAG in rm["tags"]
    assert [s["status"] for s in rm["steps"]] == \
        ["active", "pending", "pending"]
    fab = fabricate.fold(events.read_all())[rec["fab_id"]]
    assert fab["status"] == "compiled" and fab["steps"] == 3


def test_compile_refuses_to_stomp_foreign_roadmaps(tmp_path):
    roadmap.record("set", "fab-mine", prompt="human plan", steps=["a"])
    with pytest.raises(ValueError, match="not fabricate-owned"):
        fabricate.compile_work_order(PRD_MD, fab_id="fab-mine",
                                     base_dir=tmp_path)


def test_compile_recompile_is_a_replan(tmp_path):
    fabricate.compile_work_order(PRD_MD, fab_id="fab-x", base_dir=tmp_path)
    fabricate.compile_work_order(PRD_CONTRACT_STYLE, fab_id="fab-x",
                                 base_dir=tmp_path)
    rm = roadmap.get(events.read_all(), "fab-x")
    assert len(rm["steps"]) == 2 and rm["version"] == 2
    plan = (tmp_path / ".hive-fab" / "fab-x" / "fix_plan.md") \
        .read_text(encoding="utf-8")
    assert plan.count("- [ ]") == 2


# --- dispatch -----------------------------------------------------------------

def test_dispatch_defaults_to_near_immediate(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    seen = {}

    def register(name, due, cwd, arg):
        seen.update(name=name, due=due, cwd=cwd, arg=arg)
        return "windows_task"

    p = fabricate.dispatch(rec["fab_id"], register=register, now=T0_EPOCH)
    assert p["due"] == fabricate._iso(T0_EPOCH + fabricate.MIN_LEAD_S)
    assert p["timer"] == "windows_task"
    assert seen["name"] == f"MindHive Fabricate {rec['fab_id']}"
    assert f"-m mindhive fabricate run {rec['fab_id']}" in seen["arg"]
    assert "--project test" in seen["arg"]            # resolved namespace
    assert seen["cwd"] == str(tmp_path)               # the repo root


def test_dispatch_honors_at_and_in_hours(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    p = fabricate.dispatch(rec["fab_id"], at="2026-07-19T09:00:00Z",
                           register=_no_timer, now=T0_EPOCH)
    assert p["due"] == "2026-07-19T09:00:00Z"
    p = fabricate.dispatch(rec["fab_id"], in_hours=48.0,
                           register=_no_timer, now=T0_EPOCH)
    assert p["due"] == "2026-07-20T00:00:00Z"
    with pytest.raises(ValueError, match="--at"):
        fabricate.dispatch(rec["fab_id"], at="weekend", register=_no_timer)


def test_dispatch_requires_a_compiled_fabrication(tmp_path):
    with pytest.raises(ValueError, match="compile the PRD first"):
        fabricate.dispatch("fab-ghost", register=_no_timer)
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    (tmp_path / ".hive-fab" / rec["fab_id"] / "PROMPT.md").unlink()
    with pytest.raises(ValueError, match="vanished"):
        fabricate.dispatch(rec["fab_id"], register=_no_timer)


def test_dispatch_survives_a_broken_registrar(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)

    def boom(*a):
        raise OSError("no scheduler")

    p = fabricate.dispatch(rec["fab_id"], register=boom, now=T0_EPOCH)
    assert p["timer"] == "none"                       # event still recorded


# --- run ----------------------------------------------------------------------

def test_run_completed_closes_the_roadmap_and_timer(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    gone = []

    def fake_loop(project, goal_dir, plan_id, force=False):
        assert goal_dir == rec["goal_dir"] and plan_id == rec["fab_id"]
        return {"reason": "completed", "iterations": 4, "run_id": "r1"}

    p = fabricate.run(rec["fab_id"], run_loop_fn=fake_loop,
                      unregister=gone.append)
    assert p["reason"] == "completed" and p["run_id"] == "r1"
    assert gone == [f"MindHive Fabricate {rec['fab_id']}"]
    evs = events.read_all()
    assert roadmap.get(evs, rec["fab_id"])["status"] == "completed"
    fab = fabricate.fold(evs)[rec["fab_id"]]
    assert fab["status"] == "completed" and fab["iterations"] == 4


def test_run_usage_cap_keeps_the_plan_open(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    ran = []
    p = fabricate.run(rec["fab_id"],
                      run_loop_fn=lambda *a, **k: {"reason": "api_cap",
                                                   "iterations": 2,
                                                   "run_id": "r2"},
                      unregister=ran.append)
    assert p["reason"] == "api_cap"
    assert ran == []                                  # timer left alone
    evs = events.read_all()
    assert roadmap.get(evs, rec["fab_id"])["status"] == "active"  # engine's
    # job to interrupt; run() itself must not touch the plan on a cap
    assert fabricate.fold(evs)[rec["fab_id"]]["status"] == "stopped"


def test_run_guard_refusal_is_recorded_not_raised(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)

    def refuse(*a, **k):
        raise RuntimeError("LIVE_LOCK present")

    p = fabricate.run(rec["fab_id"], run_loop_fn=refuse)
    assert p["reason"] == "guard_refused"


def test_run_requires_a_compiled_fabrication():
    with pytest.raises(ValueError, match="compile the PRD first"):
        fabricate.run("fab-ghost", run_loop_fn=lambda *a, **k: {})


# --- fold totality ------------------------------------------------------------

def test_fold_is_total_on_junk_history():
    events.append_event("fabrication", "free text", session_id="s1")
    events.append_event("fabrication", {"action": "compiled",
                                        "fab_id": "no-dir"}, session_id="s1")
    events.append_event("fabrication", {"action": "dispatched",
                                        "fab_id": "bad-due", "due": "soon"},
                        session_id="s1")
    events.append_event("fabrication", {"action": "run", "fab_id": "orphan",
                                        "run_id": "r9", "reason": "api_cap"},
                        session_id="s1")
    recs = fabricate.fold(events.read_all())
    assert set(recs) == {"orphan"}
    assert recs["orphan"]["status"] == "stopped"


# --- the full unattended cycle: engine cap -> loop-mode autoresume ------------

def test_engine_cap_arms_loop_mode_resume_for_the_fabrication(tmp_path,
                                                              monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    from mindhive.loop import config as loop_config
    from mindhive.loop import engine as loop_engine
    from mindhive.loop.driver import DriverResult, Usage

    class CappedDriver:
        def run(self, prompt, loop_context, **kw):
            return DriverResult(text="", usage=Usage(input_tokens=10),
                                session_id="s1", num_turns=1,
                                rate_limited=True)

    result = loop_engine.run_loop(
        project="test", loop_dir=rec["goal_dir"],
        cfg=loop_config.LoopConfig(max_iterations=3), driver=CappedDriver(),
        now_fn=lambda: T0_EPOCH, sleep_fn=lambda s: None,
        progress_fn=lambda i: True, plan_id=rec["fab_id"])
    assert result["reason"] == "api_cap"
    evs = events.read_all()
    # the FABRICATION plan (not the run_id) carries the interruption
    rm = roadmap.get(evs, rec["fab_id"])
    assert rm["status"] == "interrupted" and rm["cause"] == "api_cap"
    ar = autoresume.fold(evs)[rec["fab_id"]]
    assert ar["status"] == "scheduled"
    assert ar["mode"] == "loop"
    assert ar["goal_dir"] == str(rec["goal_dir"])


def test_a_capped_loop_wakes_at_most_max_attempts_times(tmp_path,
                                                        monkeypatch):
    """The whole unattended chain, the engine's own snapshot included: every
    relaunch is capped again, and the OS timer fires whatever is armed. The
    snapshot used to re-arm each relaunch at attempt 1, so the chain never
    ended -- a loop that kept being capped woke every cooldown for ever, where
    the prompt-mode resume it replaced gave up after MAX_ATTEMPTS."""
    monkeypatch.chdir(tmp_path)
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    from mindhive.loop import config as loop_config
    from mindhive.loop import engine as loop_engine
    from mindhive.loop.driver import DriverResult, Usage

    class CappedDriver:
        def run(self, prompt, loop_context, **kw):
            return DriverResult(text="", usage=Usage(input_tokens=10),
                                session_id="s1", num_turns=1,
                                rate_limited=True)

    def capped_run(plan_id, goal_dir, project):
        result = loop_engine.run_loop(
            project="test", loop_dir=goal_dir,
            cfg=loop_config.LoopConfig(max_iterations=3),
            driver=CappedDriver(), now_fn=lambda: T0_EPOCH,
            sleep_fn=lambda s: None, progress_fn=lambda i: True,
            plan_id=plan_id)
        assert result["reason"] == "api_cap"
        return "still_limited", "loop api_cap"

    armed, gone = [], []
    monkeypatch.setattr(autoresume, "_register_timer",
                        lambda plan_id, *a: armed.append(plan_id)
                        or "windows_task")
    monkeypatch.setattr(autoresume, "_unregister_timer", gone.append)
    capped_run(rec["fab_id"], rec["goal_dir"], "test")      # the launch
    assert armed == [rec["fab_id"]]
    fires = 0
    while armed and fires <= autoresume.MAX_ATTEMPTS:        # the OS timer
        armed.pop()
        autoresume.fire(
            rec["fab_id"], loop_runner=capped_run,
            runner=lambda *a, **k: pytest.fail("prompt runner must not be "
                                               "used"))
        fires += 1
    assert fires == autoresume.MAX_ATTEMPTS
    assert armed == [] and gone == [rec["fab_id"]]
    evs = events.read_all()
    assert autoresume.pending(evs) == []
    assert [e.payload["attempt"] for e in evs if e.type == "autoresume"
            and e.payload.get("action") == "fired"] == \
        list(range(1, autoresume.MAX_ATTEMPTS + 1))


# Nine SOP steps: more than MAX_ATTEMPTS, so a chain that spends an attempt on
# every wake-up runs out before the plan does.
PRD_LONG = """TITLE: Build the nine-part panel
SUMMARY: Nine parts, each one usage window of work.

## Goal

All nine parts built.

## SOP

""" + "\n".join(f"{i}. Use the python-development:python-code-style skill to "
                f"build part {i}." for i in range(1, 10)) + """

## Acceptance criteria

- All nine parts built.
"""


def _capped_result():
    from mindhive.loop.driver import DriverResult, Usage
    return DriverResult(text="", usage=Usage(input_tokens=10),
                        session_id="s1", num_turns=1, rate_limited=True)


def _check_one(text):
    """The agent checks the next open item off."""
    return text.replace("- [ ]", "- [x]", 1)


def _toggle_first(text):
    """The agent flaps: it un-checks what it checked, then checks it again."""
    if "- [x]" in text:
        return text.replace("- [x]", "- [ ]", 1)
    return _check_one(text)


def _checked(rec):
    return (Path(rec["goal_dir"]) / "fix_plan.md").read_text(
        encoding="utf-8").count("- [x]")


def _wake_up_chain(monkeypatch, rec, edit, between=None, cap=40):
    """Launch the fabrication once, then let the OS timer fire every wake-up
    the chain arms until it ends -- or `cap` wake-ups, for a chain that would
    not. The launch and every relaunch are the real loop engine, driven by an
    agent that applies `edit` to fix_plan.md and hits the usage cap.
    `between(k)` runs before the k-th wake-up: what happens while the loop
    sleeps. Returns the attempt of every wake-up, and the timers left armed
    and dropped."""
    fix_plan = Path(rec["goal_dir"]) / "fix_plan.md"
    from mindhive.loop import config as loop_config
    from mindhive.loop import engine as loop_engine

    class EditThenCapped:
        def run(self, prompt, loop_context, **kw):
            fix_plan.write_text(edit(fix_plan.read_text(encoding="utf-8")),
                                encoding="utf-8")
            return _capped_result()

    def capped_run(plan_id, goal_dir, project):
        result = loop_engine.run_loop(
            project="test", loop_dir=goal_dir,
            cfg=loop_config.LoopConfig(max_iterations=3),
            driver=EditThenCapped(), now_fn=lambda: T0_EPOCH,
            sleep_fn=lambda s: None, progress_fn=lambda i: True,
            plan_id=plan_id)
        assert result["reason"] == "api_cap"
        return "still_limited", "loop api_cap"

    armed, gone = [], []
    monkeypatch.setattr(autoresume, "_register_timer",
                        lambda plan_id, *a: armed.append(plan_id)
                        or "windows_task")
    monkeypatch.setattr(autoresume, "_unregister_timer", gone.append)
    capped_run(rec["fab_id"], rec["goal_dir"], "test")    # the launch
    fires = 0
    while armed and fires < cap:                           # the OS timer
        if between is not None:
            between(fires)
        armed.pop()
        autoresume.fire(
            rec["fab_id"], loop_runner=capped_run,
            runner=lambda *a, **k: pytest.fail("prompt runner must not be "
                                               "used"))
        fires += 1
    attempts = [e.payload["attempt"] for e in events.read_all()
                if e.type == "autoresume"
                and e.payload.get("action") == "fired"]
    return attempts, armed, gone


def test_only_wake_ups_that_check_off_no_step_spend_an_attempt(tmp_path,
                                                             monkeypatch):
    """MAX_ATTEMPTS bounds a loop that is capped without getting anywhere, not
    one that is capped while it works: a fabrication that checks off one step
    per usage window is carried to the end of its plan. Every wake-up that
    checked a step off restarts the count, so once the plan stops moving --
    here every step is checked and the window is still closed -- MAX_ATTEMPTS
    more wake-ups end the chain. Counting every wake-up dropped the plan at
    7/9 with nothing left to resume it."""
    monkeypatch.chdir(tmp_path)
    rec = fabricate.compile_work_order(PRD_LONG, base_dir=tmp_path)
    attempts, armed, gone = _wake_up_chain(monkeypatch, rec, _check_one)
    evs = events.read_all()
    assert roadmap.get(evs, rec["fab_id"])["progress"] == \
        {"done": 9, "total": 9}
    # The launch checked step 1 off, wake-ups 1-8 the rest, each re-arming at
    # attempt 1; the ninth found nothing left, and from there every wake-up
    # spent one.
    assert attempts == [1] * 9 + list(range(2, autoresume.MAX_ATTEMPTS + 1))
    assert armed == [] and gone == [rec["fab_id"]]
    assert autoresume.pending(evs) == []


def test_a_step_checked_and_unchecked_by_turns_is_not_progress(tmp_path,
                                                               monkeypatch):
    """Progress is measured against the most items this chain has ever seen
    checked, not against where each wake-up began: an agent that checks a
    step off in one window and un-checks it in the next gets nowhere, and
    MAX_ATTEMPTS wake-ups end the chain. Measured from each wake-up's start,
    every second one checked a step off and the chain never ended."""
    monkeypatch.chdir(tmp_path)
    rec = fabricate.compile_work_order(PRD_LONG, base_dir=tmp_path)
    attempts, armed, gone = _wake_up_chain(monkeypatch, rec, _toggle_first)
    assert attempts == list(range(1, autoresume.MAX_ATTEMPTS + 1))
    assert armed == [] and gone == [rec["fab_id"]]
    # Each re-arm carries the mark forward: the one item the launch checked.
    assert [e.payload.get("checked_high") for e in events.read_all()
            if e.type == "autoresume"
            and e.payload.get("action") == "scheduled"] == \
        [None] + [1] * (autoresume.MAX_ATTEMPTS - 1)


def test_an_interactive_session_between_wake_ups_does_not_hide_progress(
        tmp_path, monkeypatch):
    """While the loop sleeps, a session can adopt its interrupted plan --
    re-setting the roadmap steps with its own todos -- and interrupt it again
    when it ends. The roadmap's done count is then the session's, not the
    loop's: measured there, no wake-up read as progress and the plan was
    dropped at 7/9. The loop's own checklist still says how far it got."""
    monkeypatch.chdir(tmp_path)
    rec = fabricate.compile_work_order(PRD_LONG, base_dir=tmp_path)

    def between(k):
        plan = roadmap.get(events.read_all(), rec["fab_id"])
        roadmap.record(
            "set", rec["fab_id"], prompt=plan["prompt"],
            steps=[{"text": s["text"], "status": s["status"]}
                   for s in plan["steps"]]
            + [{"text": f"session {k} todo {j}", "status": "done"}
               for j in (1, 2)])
        roadmap.record("interrupt", rec["fab_id"], cause="session_end")

    attempts, armed, gone = _wake_up_chain(monkeypatch, rec, _check_one,
                                           between=between)
    assert _checked(rec) == 9
    assert attempts == [1] * 9 + list(range(2, autoresume.MAX_ATTEMPTS + 1))
    assert armed == [] and gone == [rec["fab_id"]]


def test_progress_past_the_roadmap_step_cap_still_counts(tmp_path,
                                                          monkeypatch):
    """The roadmap keeps at most MAX_STEPS steps, checked ones first, so past
    that many checked items its done count stops moving. The checklist keeps
    counting: a long fabrication whose plan grew is carried to its end."""
    monkeypatch.chdir(tmp_path)
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    (Path(rec["goal_dir"]) / "fix_plan.md").write_text(
        "# Fix plan\n\n"
        + "".join(f"- [x] part {i}\n" for i in range(roadmap.MAX_STEPS))
        + "".join(f"- [ ] extra part {i}\n" for i in range(10)),
        encoding="utf-8")
    attempts, armed, gone = _wake_up_chain(monkeypatch, rec, _check_one)
    assert _checked(rec) == roadmap.MAX_STEPS + 10
    # The launch checked the first extra part off, wake-ups 1-9 the others.
    assert attempts == [1] * 10 + list(range(2, autoresume.MAX_ATTEMPTS + 1))
    assert armed == [] and gone == [rec["fab_id"]]


def test_a_checklist_the_wake_up_cannot_read_is_not_progress(tmp_path):
    """Reading the checklist is best-effort, like everything the wake-up does
    after the relaunch: one it cannot read counts as no progress, and the
    wake-up still records its outcome and re-arms."""
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop", goal_dir=rec["goal_dir"],
                        register=_no_timer, now=T0_EPOCH)
    fix_plan = Path(rec["goal_dir"]) / "fix_plan.md"

    def loop_runner(plan_id, goal_dir, project):
        fix_plan.unlink()
        fix_plan.mkdir()                    # reading it now raises OSError
        return "still_limited", "loop api_cap"

    payload = autoresume.fire(rec["fab_id"], loop_runner=loop_runner,
                              register=_no_timer, unregister=lambda p: None,
                              runner=_no_prompt)
    assert payload["outcome"] == "still_limited"
    ar = autoresume.fold(events.read_all())[rec["fab_id"]]
    assert (ar["status"], ar["attempt"]) == ("scheduled", 2)
    assert ar["checked_high"] == 0          # what it read before the relaunch


def test_loop_mode_fire_relaunches_the_loop(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop",
                        goal_dir=rec["goal_dir"], register=_no_timer,
                        now=T0_EPOCH)
    calls, gone = [], []

    def loop_runner(plan_id, goal_dir, project):
        calls.append((plan_id, goal_dir))
        return "delivered", "loop completed after 3 iteration(s)"

    payload = autoresume.fire(rec["fab_id"], loop_runner=loop_runner,
                              runner=lambda *a, **k: pytest.fail(
                                  "prompt runner must not be used"),
                              unregister=gone.append)
    assert payload["outcome"] == "delivered"
    assert calls == [(rec["fab_id"], rec["goal_dir"])]
    assert gone == [rec["fab_id"]]


def test_loop_mode_fire_still_limited_arms_the_next_attempt(tmp_path):
    """A relaunch that was capped again is the next attempt, not a fresh
    first one: the wake-up re-arms itself one attempt later, and tells the
    relaunched run it is one so the engine's snapshot leaves the timer be."""
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop",
                        goal_dir=rec["goal_dir"], register=_no_timer,
                        now=T0_EPOCH)
    gone, waking = [], []

    def loop_runner(plan_id, goal_dir, project):
        waking.append(autoresume.waking_attempt())
        return "still_limited", "loop api_cap"

    # `runner=` is not decoration: without it, a regression that drops the
    # loop mode falls through to the prompt path and spawns a REAL headless
    # `claude` (acceptEdits, 3600 s) in the checkout. It happened once under a
    # mutation probe (blueprint P10); the seam makes that a fast failure.
    payload = autoresume.fire(
        rec["fab_id"], loop_runner=loop_runner,
        runner=lambda *a, **k: pytest.fail("prompt runner must not be used"),
        register=_no_timer, unregister=gone.append)
    assert payload["outcome"] == "still_limited"
    assert waking == [1]
    assert autoresume.waking_attempt() is None      # reset once it returned
    assert gone == []                               # re-armed, not dropped
    ar = autoresume.fold(events.read_all())[rec["fab_id"]]
    assert (ar["status"], ar["attempt"], ar["mode"], ar["goal_dir"]) == \
        ("scheduled", 2, "loop", str(rec["goal_dir"]))


def test_loop_mode_fire_gives_up_at_max_attempts(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop",
                        goal_dir=rec["goal_dir"], register=_no_timer,
                        now=T0_EPOCH, attempt=autoresume.MAX_ATTEMPTS)
    gone = []
    payload = autoresume.fire(
        rec["fab_id"],
        loop_runner=lambda *a: ("still_limited", "loop api_cap"),
        runner=lambda *a, **k: pytest.fail("prompt runner must not be used"),
        register=lambda *a: pytest.fail("no attempt after the last"),
        unregister=gone.append)
    assert payload["outcome"] == "still_limited"
    assert "gave up" in payload["detail"]           # says why nothing is next
    assert autoresume.fold(events.read_all())[rec["fab_id"]]["status"] == \
        "fired"
    assert gone == [rec["fab_id"]]                  # gave up cleanly


def test_a_cancel_issued_while_the_relaunch_runs_is_honoured(tmp_path):
    """A relaunch runs for hours, and its `fired` row lands only when it
    returns, so a `hive autoresume cancel` (or the cancel inside `hive roadmap
    resume`) issued meanwhile finds the delivery still scheduled and succeeds.
    The wake-up must not then re-arm what the human just withdrew."""
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop",
                        goal_dir=rec["goal_dir"], register=_no_timer,
                        now=T0_EPOCH)
    gone = []

    def loop_runner(plan_id, goal_dir, project):
        autoresume.cancel(plan_id, unregister=gone.append)   # another shell
        return "still_limited", "loop api_cap"

    payload = autoresume.fire(
        rec["fab_id"], loop_runner=loop_runner,
        runner=lambda *a, **k: pytest.fail("prompt runner must not be used"),
        register=lambda *a: pytest.fail("a withdrawn delivery re-armed"),
        unregister=gone.append)
    assert payload["outcome"] == "still_limited"
    assert "cancelled" in payload["detail"]
    evs = events.read_all()
    assert autoresume.pending(evs) == []
    assert autoresume.fold(evs)[rec["fab_id"]]["status"] == "fired"
    assert gone == [rec["fab_id"]] * 2              # the cancel's, the fire's


def test_a_capped_relaunch_is_due_one_cooldown_after_it_returned(tmp_path):
    """The window just closed on the relaunch, so the next wake-up is one
    cooldown after it returned. Keyed to the recorded interruption instead, a
    relaunch whose best-effort snapshot recorded nothing re-armed off the OLD
    interruption, long past its cooldown: due a minute later into a window
    that is still closed, the remaining attempts spent a minute apart."""
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop",
                        goal_dir=rec["goal_dir"], register=_no_timer)
    due = autoresume.fold(events.read_all())[rec["fab_id"]]["due"]
    back = autoresume._parse_ts(due) + 1800     # returned half an hour later
    autoresume.fire(
        rec["fab_id"],
        loop_runner=lambda *a: ("still_limited", "loop api_cap"),
        runner=lambda *a, **k: pytest.fail("prompt runner must not be used"),
        register=_no_timer, now=back)
    ar = autoresume.fold(events.read_all())[rec["fab_id"]]
    assert (ar["status"], ar["attempt"]) == ("scheduled", 2)
    assert autoresume._parse_ts(ar["due"]) - back == \
        autoresume.COOLDOWN_HOURS * 3600


def test_loop_mode_fire_records_an_outcome_whatever_the_delivery_raises(
        tmp_path):
    """The OS timer runs `hive autoresume fire` once. A delivery that raised
    anything but the two errors it expects -- a malformed loop.toml, one it
    cannot read -- left a traceback and no `fired` row: the roadmap stayed
    interrupted under a schedule that looked live and never fired again.
    Every outcome is recorded, and the spent timer is dropped."""
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop",
                        goal_dir=rec["goal_dir"], register=_no_timer,
                        now=T0_EPOCH)
    gone = []

    def loop_runner(plan_id, goal_dir, project):
        raise KeyError("loop.toml has no [budget] table")

    payload = autoresume.fire(
        rec["fab_id"], loop_runner=loop_runner,
        runner=lambda *a, **k: pytest.fail("prompt runner must not be used"),
        unregister=gone.append)
    assert payload["outcome"] == "error"
    assert "KeyError" in payload["detail"]
    assert autoresume.waking_attempt() is None      # reset on the raise too
    fired = [e for e in events.read_all() if e.type == "autoresume"
             and e.payload.get("action") == "fired"]
    assert fired and fired[-1].payload["outcome"] == "error"
    assert gone == [rec["fab_id"]]


def test_loop_mode_degrades_to_prompt_when_goal_dir_vanished(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop",
                        goal_dir=str(tmp_path / "gone"),
                        register=_no_timer, now=T0_EPOCH)
    ok = json.dumps({"result": "resumed", "is_error": False})
    payload = autoresume.fire(
        rec["fab_id"],
        loop_runner=lambda *a: pytest.fail("no goal dir -> no loop"),
        runner=lambda *a, **k: {"exit": 0, "stdout": ok})
    assert payload["outcome"] == "delivered"


# --- a loop-mode wake-up of a fabrication goes through fabricate.run -----------
# Blueprint P10 follow-up. The wake-up used to call the bare loop engine, so a
# fabrication that slept on a usage cap and then finished never recorded its
# `run` event, never completed its roadmap and never dropped its dispatch
# timer. Every road to a real `claude` is closed in these tests: the bare
# engine and the CLI driver are recording fakes, the fabrication's own launch
# is `fabricate._run_loop` patched, and the prompt runner fails if touched.

def _no_prompt(*a, **k):
    pytest.fail("prompt runner must not be used")


def _fake_engine(monkeypatch):
    """Record, never launch: the bare engine returns `completed` and names the
    plan it was keyed to; every timer removal is captured by task name."""
    from mindhive.loop import driver as loop_driver
    from mindhive.loop import engine as loop_engine
    bare, gone = [], []

    def bare_run_loop(**kw):
        bare.append(kw.get("plan_id"))
        return {"reason": "completed", "iterations": 1, "run_id": "bare"}

    monkeypatch.setattr(loop_driver, "preflight", lambda *a, **k: True)
    monkeypatch.setattr(loop_driver, "CLIDriver", lambda *a, **k: object())
    monkeypatch.setattr(loop_engine, "run_loop", bare_run_loop)
    monkeypatch.setattr(autoresume, "unregister_windows_task", gone.append)
    return bare, gone


def _armed_fabrication(tmp_path, monkeypatch):
    """A compiled fabrication whose launch hit a usage cap: roadmap
    interrupted, a loop-mode delivery scheduled on its goal dir."""
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop", goal_dir=rec["goal_dir"],
                        register=_no_timer, now=T0_EPOCH)
    bare, gone = _fake_engine(monkeypatch)
    return rec, bare, gone


def test_loop_mode_wake_up_of_a_fabrication_runs_through_fabricate_run(
        tmp_path, monkeypatch):
    rec, bare, gone = _armed_fabrication(tmp_path, monkeypatch)
    launches = []

    def fab_loop(project, goal_dir, plan_id, force=False):
        launches.append((goal_dir, plan_id))
        return {"reason": "completed", "iterations": 4, "run_id": "loop-r2"}

    monkeypatch.setattr(fabricate, "_run_loop", fab_loop)
    payload = autoresume.fire(rec["fab_id"], runner=_no_prompt)
    assert payload["outcome"] == "delivered"
    assert launches == [(rec["goal_dir"], rec["fab_id"])]
    assert bare == []                      # never the bare engine
    evs = events.read_all()
    fab = fabricate.fold(evs)[rec["fab_id"]]
    assert (fab["status"], fab["run_id"], fab["iterations"]) == \
        ("completed", "loop-r2", 4)
    assert roadmap.get(evs, rec["fab_id"])["status"] == "completed"
    assert fabricate._task_name(rec["fab_id"]) in gone     # dispatch timer
    assert autoresume._task_name(rec["fab_id"]) in gone    # delivery timer


def test_loop_mode_wake_up_of_a_capped_fabrication_keeps_it_open(
        tmp_path, monkeypatch):
    rec, bare, gone = _armed_fabrication(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fabricate, "_run_loop",
        lambda *a, **k: {"reason": "api_cap", "iterations": 1,
                         "run_id": "loop-r3"})
    n_sched = len([e for e in events.read_all() if e.type == "autoresume"
                   and e.payload.get("action") == "scheduled"])
    payload = autoresume.fire(rec["fab_id"], runner=_no_prompt)
    assert payload["outcome"] == "still_limited"
    assert bare == []
    evs = events.read_all()
    fab = fabricate.fold(evs)[rec["fab_id"]]
    assert (fab["status"], fab["reason"], fab["run_id"]) == \
        ("stopped", "api_cap", "loop-r3")
    assert roadmap.get(evs, rec["fab_id"])["status"] == "interrupted"
    assert gone == []                      # both timers stay armed
    sched = [e for e in evs if e.type == "autoresume"
             and e.payload.get("action") == "scheduled"]
    assert len(sched) == n_sched + 1       # the wake-up re-armed itself
    assert (sched[-1].payload["attempt"], sched[-1].payload["mode"]) == \
        (2, "loop")


def test_a_fabrication_wake_up_capped_again_is_armed_once_by_the_wake_up(
        tmp_path, monkeypatch):
    """The real delivery path inside a wake-up -- `_run_loop_delivery` ->
    `fabricate.run` -> the engine and its snapshot: the snapshot records the
    new interruption and leaves the timer to the wake-up, so exactly one
    delivery is armed, at the next attempt, rather than a second one restarted
    at attempt 1."""
    monkeypatch.chdir(tmp_path)
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop", goal_dir=rec["goal_dir"],
                        register=_no_timer, now=T0_EPOCH)
    from mindhive.loop import config as loop_config
    from mindhive.loop import engine as loop_engine
    from mindhive.loop.driver import DriverResult, Usage

    class CappedDriver:
        def run(self, prompt, loop_context, **kw):
            return DriverResult(text="", usage=Usage(input_tokens=10),
                                session_id="s1", num_turns=1,
                                rate_limited=True)

    def fab_loop(project, goal_dir, plan_id, force=False):
        return loop_engine.run_loop(
            project=project, loop_dir=Path(goal_dir),
            cfg=loop_config.LoopConfig(max_iterations=3),
            driver=CappedDriver(), now_fn=lambda: T0_EPOCH,
            sleep_fn=lambda s: None, progress_fn=lambda i: True,
            plan_id=plan_id)

    monkeypatch.setattr(fabricate, "_run_loop", fab_loop)
    n_sched = len([e for e in events.read_all() if e.type == "autoresume"
                   and e.payload.get("action") == "scheduled"])
    payload = autoresume.fire(rec["fab_id"], runner=_no_prompt,
                              register=_no_timer)
    assert payload["outcome"] == "still_limited"
    evs = events.read_all()
    assert roadmap.get(evs, rec["fab_id"])["status"] == "interrupted"
    assert [e.payload["attempt"] for e in evs if e.type == "autoresume"
            and e.payload.get("action") == "scheduled"][n_sched:] == [2]


@pytest.mark.parametrize("launch", ["spawn_failed", "guard_refused"])
def test_loop_mode_wake_up_of_a_fabrication_that_cannot_launch_is_an_error(
        tmp_path, monkeypatch, launch):
    rec, bare, gone = _armed_fabrication(tmp_path, monkeypatch)

    def fab_loop(project, goal_dir, plan_id, force=False):
        if launch == "guard_refused":
            raise RuntimeError("V17 LIVE_LOCK active")
        return {"reason": "spawn_failed", "iterations": 0, "run_id": "none"}

    monkeypatch.setattr(fabricate, "_run_loop", fab_loop)
    payload = autoresume.fire(rec["fab_id"], runner=_no_prompt)
    assert payload["outcome"] == "error"
    assert launch in payload["detail"]
    assert bare == []
    fab = fabricate.fold(events.read_all())[rec["fab_id"]]
    assert (fab["status"], fab["reason"]) == ("stopped", launch)


def test_loop_mode_wake_up_of_a_plain_loop_keeps_the_bare_engine(
        tmp_path, monkeypatch):
    """The routing is for fabrications only: a `hive loop` run's plan is not
    in `fabricate.fold`, and its wake-up still relaunches the bare engine."""
    from mindhive.loop import scaffold as scaffold_mod
    goal_dir = tmp_path / ".hive-loop"
    scaffold_mod.scaffold(goal_dir)
    roadmap.record("set", "loop-run-1", prompt="Build it",
                   steps=["step one"])
    roadmap.record("interrupt", "loop-run-1", cause="api_cap")
    autoresume.schedule("loop-run-1", mode="loop", goal_dir=str(goal_dir),
                        register=_no_timer, now=T0_EPOCH)
    bare, _ = _fake_engine(monkeypatch)
    monkeypatch.setattr(fabricate, "_run_loop",
                        lambda *a, **k: pytest.fail("not a fabrication"))
    payload = autoresume.fire("loop-run-1", runner=_no_prompt)
    assert payload["outcome"] == "delivered"
    assert bare == ["loop-run-1"]
    assert "loop-run-1" not in fabricate.fold(events.read_all())


def test_autoresume_contract_rejects_bad_mode_payloads():
    with pytest.raises(ValueError, match="mode"):
        autoresume.validate_payload(
            {"action": "scheduled", "plan_id": "x-1",
             "due": "2026-07-18T05:00:00Z", "attempt": 1, "mode": "cron"})
    with pytest.raises(ValueError, match="goal_dir"):
        autoresume.validate_payload(
            {"action": "scheduled", "plan_id": "x-1",
             "due": "2026-07-18T05:00:00Z", "attempt": 1, "mode": "loop"})


def test_autoresume_contract_rejects_a_bad_checked_high():
    ok = {"action": "scheduled", "plan_id": "x-1",
          "due": "2026-07-18T05:00:00Z", "attempt": 1}
    autoresume.validate_payload(dict(ok, checked_high=0))
    for bad in (-1, True, "3", 2.5):
        with pytest.raises(ValueError, match="checked_high"):
            autoresume.validate_payload(dict(ok, checked_high=bad))


def test_a_schedule_without_a_checked_high_starts_the_count_over(tmp_path):
    """The mark belongs to the chain of wake-ups that carried it forward: a
    schedule without one -- a fresh launch's, a human's -- measures from the
    checklist as it stands."""
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop", goal_dir=rec["goal_dir"],
                        register=_no_timer, now=T0_EPOCH, checked_high=3)
    ar = autoresume.fold(events.read_all())[rec["fab_id"]]
    assert ar["checked_high"] == 3
    autoresume.schedule(rec["fab_id"], mode="loop", goal_dir=rec["goal_dir"],
                        register=_no_timer, now=T0_EPOCH)
    assert "checked_high" not in \
        autoresume.fold(events.read_all())[rec["fab_id"]]


# --- registry discipline ------------------------------------------------------

def test_fabrication_stays_out_of_semantic_projections():
    from mindhive import briefing, consolidate
    assert "fabrication" in events.DETERMINISTIC_TYPES
    assert "fabrication" not in consolidate.CONSOLIDATE_TYPES
    assert "fabrication" not in briefing._TIMELINE_TYPES
    assert "fabrication" not in events.MEMORY_VIEW_TYPES


# --- CLI ----------------------------------------------------------------------

def test_cli_compile_dispatch_list(tmp_path, capsys):
    prd = tmp_path / "prd.md"
    prd.write_text(PRD_MD, encoding="utf-8")
    assert cli_mod.main(["fabricate", "compile", str(prd),
                         "--dir", str(tmp_path), "--id", "fab-toggle"]) == 0
    out = capsys.readouterr().out
    assert "fabricate compiled: fab-toggle" in out
    assert "3 SOP step(s)" in out
    assert cli_mod.main(["fabricate", "dispatch", "fab-toggle"]) == 0
    out = capsys.readouterr().out
    assert "fabricate dispatched: fab-toggle" in out
    assert "timer: none" in out                       # env-gated off in tests
    assert "hive fabricate run fab-toggle" in out
    assert cli_mod.main(["fabricate", "list"]) == 0
    out = capsys.readouterr().out
    assert "[fab-toggle]" in out and "runs " in out


def test_cli_compile_from_learning(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    events.append_event("learning", {"text": "display text",
                                     "report": PRD_CONTRACT_STYLE},
                        session_id="research-r1", ts=T0)
    assert cli_mod.main(["fabricate", "compile", "--from-learning"]) == 0
    out = capsys.readouterr().out
    assert "fab-tune-the-recall-ranker" in out
    assert (tmp_path / ".hive-fab" / "fab-tune-the-recall-ranker"
            / "fix_plan.md").exists()


def test_cli_compile_errors(tmp_path, capsys):
    assert cli_mod.main(["fabricate", "compile"]) == 2
    assert "pass a PRD file" in capsys.readouterr().err
    assert cli_mod.main(["fabricate", "compile", "--from-learning"]) == 2
    assert "no Self-Reliant learning" in capsys.readouterr().err
    bad = tmp_path / "bad.md"
    bad.write_text("TITLE: X\n\nno sop here\n", encoding="utf-8")
    assert cli_mod.main(["fabricate", "compile", str(bad)]) == 2
    assert "no SOP" in capsys.readouterr().err


def test_cli_run_reports_reason(tmp_path, capsys, monkeypatch):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    monkeypatch.setattr(
        fabricate, "_run_loop",
        lambda project, goal_dir, plan_id, force=False:
        {"reason": "completed", "iterations": 5, "run_id": "r7"})
    assert cli_mod.main(["fabricate", "run", rec["fab_id"]]) == 0
    assert "-> completed (5 iteration(s)" in capsys.readouterr().out


def test_cli_append_enforces_fabrication_contract(capsys):
    assert cli_mod.main(["append", "fabrication", "free text"]) == 2
    assert "hive fabricate" in capsys.readouterr().err
    good = ('{"action": "compiled", "fab_id": "fab-x", '
            '"goal_dir": "C:/x", "steps": 2}')
    assert cli_mod.main(["append", "fabrication", good, "--json"]) == 0
    evs = events.read_all()
    assert len(evs) == 1 and evs[0].type == "fabrication"
