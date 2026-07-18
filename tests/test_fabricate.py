# tests/test_fabricate.py
"""PRD assembly line: PRD parsing, the compile -> dispatch -> run lifecycle,
the fabrication event contract and fold, loop-mode auto-resume delivery,
and the `hive fabricate` CLI."""
import calendar
import json
import time

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


def test_loop_mode_fire_still_limited_never_self_reschedules(tmp_path):
    rec = fabricate.compile_work_order(PRD_MD, base_dir=tmp_path)
    roadmap.record("interrupt", rec["fab_id"], cause="api_cap")
    autoresume.schedule(rec["fab_id"], mode="loop",
                        goal_dir=rec["goal_dir"], register=_no_timer,
                        now=T0_EPOCH)
    gone = []
    n_before = len([e for e in events.read_all()
                    if e.type == "autoresume"
                    and e.payload.get("action") == "scheduled"])
    payload = autoresume.fire(
        rec["fab_id"],
        loop_runner=lambda *a: ("still_limited", "loop api_cap"),
        unregister=gone.append)
    assert payload["outcome"] == "still_limited"
    assert gone == []                     # the engine owns the re-armed timer
    n_after = len([e for e in events.read_all()
                   if e.type == "autoresume"
                   and e.payload.get("action") == "scheduled"])
    assert n_after == n_before            # no self-reschedule in loop mode


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


def test_autoresume_contract_rejects_bad_mode_payloads():
    with pytest.raises(ValueError, match="mode"):
        autoresume.validate_payload(
            {"action": "scheduled", "plan_id": "x-1",
             "due": "2026-07-18T05:00:00Z", "attempt": 1, "mode": "cron"})
    with pytest.raises(ValueError, match="goal_dir"):
        autoresume.validate_payload(
            {"action": "scheduled", "plan_id": "x-1",
             "due": "2026-07-18T05:00:00Z", "attempt": 1, "mode": "loop"})


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
