# tests/test_replenish_cli.py
"""`hive replenish`, and the session_end hook leg that runs it.

The hook-ordering test is not ceremony. Running `replenish run` after
`roadmap autoclose` fails silently in the worst way: the injected steps exist,
the plan is already interrupted, and `notice()` never renders them — so the work
is in the ledger and invisible to the next session, which is the exact outcome
the feature exists to prevent. Nothing at runtime catches that, so the file is
asserted directly.
"""
import json
from pathlib import Path

from mindhive import events, roadmap
from mindhive.__main__ import main

SID = "sess-cli-01"
HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session_end.ps1"


def _outstanding():
    """A ledger where code changed and nothing verified it since."""
    events.append_event("user_prompt", {"text": "add the parser"},
                        session_id=SID, ts="2026-08-01T00:00:00Z")
    events.append_event(
        "code_change",
        {"text": "changed something real", "category": "update",
         "files": ["src/mindhive/replenish.py"]}, ts="2026-08-02T00:00:00Z")


def _plan():
    return roadmap.fold(events.read_all()).get(SID)


def _rows():
    return [e.payload for e in events.read_all() if e.type == "work_injected"]


def test_run_injects_work_and_records_both_events(capsys):
    _outstanding()
    assert main(["replenish", "run", "--session", SID]) == 0
    out = capsys.readouterr().out
    assert "injected [evidence-gap]" in out
    assert "evidence:" in out

    plan = _plan()
    assert plan is not None
    assert plan["steps"][0]["new"] is True
    landed = [r for r in _rows() if r["outcome"] == "landed"]
    assert len(landed) == 1
    assert landed[0]["plan_id"] == SID


def test_dry_run_decides_without_writing(capsys):
    _outstanding()
    assert main(["replenish", "run", "--session", SID, "--dry-run"]) == 0
    assert "would inject" in capsys.readouterr().out
    assert _rows() == []
    assert _plan() is None


def test_run_on_a_clean_ledger_records_only_the_dry_round(capsys):
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    assert main(["replenish", "run", "--session", SID]) == 0
    assert "nothing outstanding" in capsys.readouterr().out
    assert [r["outcome"] for r in _rows()] == ["dry"]
    assert _plan() is None


def test_run_without_a_session_id_exits_non_zero(monkeypatch, capsys):
    monkeypatch.delenv("MINDHIVE_SESSION", raising=False)
    assert main(["replenish", "run"]) == 2
    assert "no session id" in capsys.readouterr().err


def test_run_reads_the_session_from_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("MINDHIVE_SESSION", SID)
    _outstanding()
    assert main(["replenish", "run"]) == 0
    assert _plan() is not None


def test_a_second_round_refuses_the_same_condition(capsys):
    _outstanding()
    main(["replenish", "run", "--session", SID])
    capsys.readouterr()
    main(["replenish", "run", "--session", SID])
    out = capsys.readouterr().out
    assert "refused [evidence-gap]" in out
    assert "already proposed" in out
    assert len(_plan()["steps"]) == 1        # no duplicate step


def test_status_reports_the_folded_state(capsys):
    _outstanding()
    main(["replenish", "run", "--session", SID])
    capsys.readouterr()
    assert main(["replenish", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["last_round"] == 1
    assert data["dry_rounds"] == 0
    assert data["lenses"] == ["evidence-gap"]
    assert any(f.startswith("evidence-gap:") for f in data["spent_fingerprints"])


def test_status_on_an_empty_ledger(capsys):
    assert main(["replenish", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"last_round": 0, "dry_rounds": 0,
                    "spent_fingerprints": [], "lenses": ["evidence-gap"]}


# --- the hook leg -------------------------------------------------------------

def _hook_text():
    return HOOK.read_text(encoding="utf-8")


def test_the_hook_runs_replenish():
    assert "mindhive replenish run --session $sid" in _hook_text()


def test_the_hook_runs_replenish_before_autoclose():
    text = _hook_text()
    assert text.index("replenish run") < text.index("roadmap autoclose")


def test_the_hook_runs_replenish_before_the_projection_rebuild():
    # `project` is what renders STATE.md. Injecting after it means the notice
    # is a session late.
    text = _hook_text()
    assert text.index("replenish run") < text.index("mindhive project")


def test_the_replenish_leg_stays_inside_the_fail_open_try():
    # The SessionEnd hook fires for every session on this machine; a replenish
    # failure must never cost the operator their session-end projections. The
    # enclosing try is the one nearest ABOVE the call — the script opens an
    # earlier try/catch to parse the session id off stdin.
    text = _hook_text()
    call = text.index("replenish run")
    assert text.rindex("try {", 0, call) < call < text.index("} catch {}", call)
