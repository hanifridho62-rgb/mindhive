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

import pytest

from mindhive import events, replenish, roadmap
from mindhive.__main__ import main

SID = "sess-cli-01"
HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session_end.ps1"


@pytest.fixture(autouse=True)
def ledger_only_lenses(monkeypatch):
    """Run the CLI against the lenses whose evidence is the ISOLATED store.

    `isolate_store` redirects `MINDHIVE_HOME`; it cannot redirect the source
    tree, so `debt` and `coverage` read this checkout's real files. Left in,
    every assertion here would depend on the current line count of
    `src/mindhive/__main__.py` — which is exactly what broke when those two
    lenses landed. The CLI is what this file tests; the lenses have their own.

    The level's own lens tuple is emptied alongside the registry. An ambition
    level names lenses by NAME, so leaving `balanced`'s list in place would
    filter out anything a test registers of its own — including the deliberately
    exploding probe below, whose failure would then never be reported because it
    never ran."""
    monkeypatch.setattr(replenish, "LENSES",
                        {"evidence-gap": replenish.evidence_gap})
    monkeypatch.setitem(replenish.AMBITION, "balanced",
                        dict(replenish.AMBITION["balanced"], lenses=()))


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


def test_run_on_a_clean_ledger_writes_nothing(capsys):
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    assert main(["replenish", "run", "--session", SID]) == 0
    assert "nothing outstanding" in capsys.readouterr().out
    # A dry row here would be an identical event per session forever, and the
    # session surface has no dry-round exit to read it.
    assert _rows() == []
    assert _plan() is None


def test_repeated_quiet_sessions_do_not_switch_the_injector_off(capsys):
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    for _ in range(4):
        assert main(["replenish", "run", "--session", SID]) == 0
    capsys.readouterr()
    _outstanding()
    assert main(["replenish", "run", "--session", SID]) == 0
    assert "injected [evidence-gap]" in capsys.readouterr().out


def test_run_without_a_session_id_exits_non_zero(monkeypatch, capsys):
    monkeypatch.delenv("MINDHIVE_SESSION", raising=False)
    assert main(["replenish", "run"]) == 2
    assert "no session id" in capsys.readouterr().err


def test_the_hooks_unknown_fallback_cannot_own_a_plan(capsys):
    # session_end.ps1 falls back to "unknown" when it cannot parse the id off
    # stdin. Harmless while the hook only ran idempotent lifecycle actions;
    # injecting under it would pile every unparsed session into one shared plan.
    events.append_event(
        "code_change",
        {"text": "changed something real", "category": "update",
         "files": ["src/mindhive/replenish.py"]}, ts="2026-08-02T00:00:00Z")
    assert main(["replenish", "run", "--session", "unknown"]) == 0
    assert "cannot own a plan" in capsys.readouterr().out
    assert _rows() == []
    assert roadmap.fold(events.read_all()).get("unknown") is None


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
    assert data["last_recorded_round"] == 1
    assert data["dry_rounds"] == 0
    assert "evidence-gap" in data["lenses"]
    # Content-addressed since U1: a hex SHA-256, not a lens-prefixed string.
    assert data["spent_fingerprints"]
    assert all(len(f) == 64 and all(c in "0123456789abcdef" for c in f)
               for f in data["spent_fingerprints"])


def test_status_on_an_empty_ledger(capsys):
    assert main(["replenish", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    lenses = data.pop("lenses")
    data.pop("active_lenses")
    assert data == {"surface": "session", "last_recorded_round": 0,
                    "dry_rounds": 0, "spent_fingerprints": [],
                    "enabled": True, "min_score": 25, "max_per_round": 10}
    # Asserted by membership rather than equality: the registry grows as lenses
    # land, and a list-equality assertion here would fail on every addition
    # without saying anything about the state this test is actually about.
    assert "evidence-gap" in lenses and lenses == sorted(lenses)


def test_status_reports_the_dial_the_round_would_use(capsys):
    assert main(["replenish", "status", "--ambition", "conservative"]) == 0
    data = json.loads(capsys.readouterr().out)
    # An operator debugging "why did nothing land" needs the threshold and the
    # active set, not just the folded history.
    assert data["min_score"] == 40
    assert data["max_per_round"] == 3


def test_a_crash_is_reported_on_stderr_and_exits_non_zero(monkeypatch, capsys):
    # Inside session_end.ps1 stdout goes to Out-Null and a native non-zero exit
    # does not reach PowerShell's catch, so an unhandled traceback would make
    # the injector fail permanently and silently.
    from mindhive import replenish as rp_mod
    monkeypatch.setattr(rp_mod, "run_round",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("the round exploded")))
    _outstanding()
    assert main(["replenish", "run", "--session", SID]) == 1
    err = capsys.readouterr().err
    assert "replenish failed" in err and "the round exploded" in err


def test_a_lens_failure_is_reported_and_does_not_claim_a_clean_record(capsys):
    from mindhive import replenish as rp_mod

    def boom(_evs):
        raise RuntimeError("lens exploded")

    rp_mod.LENSES["probe-boom"] = boom
    try:
        events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
        assert main(["replenish", "run", "--session", SID]) == 0
    finally:
        rp_mod.LENSES.pop("probe-boom", None)
    out = capsys.readouterr()
    assert "lens probe-boom failed" in out.err
    assert "the record was not fully read" in out.out
    assert "nothing outstanding that the record proves" not in out.out


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
