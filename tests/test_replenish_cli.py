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
def _blind_to_the_real_checkout(monkeypatch):
    """Point the surface lenses at nothing.

    These tests are about the CLI's plumbing — does a round land steps, honour
    `--dry-run`, report a broken lens — and the surface lenses read THIS
    repository, which genuinely has oversized modules and untested files. Left
    live, every assertion here would depend on the current state of the checkout
    and would change the day someone refactored `workspace.py`. The lenses that
    read the tree get their own tests with their own temporary trees;
    `test_the_cli_surfaces_real_tree_candidates` below is the one case that opts
    back in."""
    monkeypatch.setattr(replenish, "resolve_trees",
                        lambda _ctx: replenish.SourceTrees())


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


# --- explain ------------------------------------------------------------------
# The surface that makes a dead lens visible. Everything about it is diagnostic,
# which is exactly why the side-effect tests below are the important ones: a
# diagnostic that changed what it was diagnosing would be worse than none, since
# running it once would spend the fingerprints it reported and silence them.

def test_explain_appends_nothing_and_spends_nothing(capsys):
    _outstanding()
    before = [(e.type, e.ts) for e in events.read_all()]
    assert main(["replenish", "explain"]) == 0
    assert [(e.type, e.ts) for e in events.read_all()] == before


def test_explain_does_not_call_a_sink(monkeypatch, capsys):
    """No roadmap step, no checklist item. `explain` never reaches a sink at all,
    and the plan staying empty is what proves it."""
    _outstanding()
    main(["replenish", "explain"])
    assert _plan() is None


def test_explain_scores_each_candidate_against_the_positions(capsys):
    _outstanding()
    main(["replenish", "explain"])
    out = capsys.readouterr().out
    assert "conservative>=40" in out and "ambitious>=15" in out
    assert "Run the test suite" in out
    assert "terms: base +10" in out


def test_explain_reports_where_a_candidate_would_land(capsys):
    _outstanding()
    main(["replenish", "explain", "--json"])
    report = json.loads(capsys.readouterr().out)
    gap = [row for row in report["candidates"]
           if row["lens"] == "evidence-gap"][0]
    assert gap["terms"]["total"] == gap["score"]
    # Not `conservative`: it needs 40, and it does not run this lens's peers
    # either. Both halves of `landing_positions` matter — a report that checked
    # only the threshold would claim positions that could never have run it.
    assert gap["lands_at"] == ["ambitious", "balanced"]


def test_explain_names_the_lenses_that_found_nothing(capsys):
    """A dead lens and a quiet lens both look like an absence, so the absence is
    named rather than left to be inferred."""
    _outstanding()
    main(["replenish", "explain", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert "coverage" in report["silent_lenses"]


def test_explain_never_spends_a_model_call_by_default(monkeypatch, capsys):
    fired = {"n": 0}

    def tripwire(_ctx):
        fired["n"] += 1
        return []

    monkeypatch.setitem(replenish.LENSES, "innovation", tripwire)
    main(["replenish", "explain"])
    main(["replenish", "explain", "--ambition", "ambitious"])
    assert fired["n"] == 0, (
        "asking what the record holds is not asking to pay for a proposal")
    main(["replenish", "explain", "--with-agentic"])
    assert fired["n"] == 1


def test_explain_scopes_to_one_position(capsys):
    _outstanding()
    main(["replenish", "explain", "--ambition", "conservative", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["lenses"] == ["evidence-gap", "repair"]


def test_explain_says_so_when_the_dial_word_is_not_understood(capsys):
    main(["replenish", "explain", "--ambition", "aggressive"])
    assert "unknown ambition" in capsys.readouterr().err


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
    # Against the registry, not a frozen literal: `status` reports which lenses
    # are installed, so the invariant is "it agrees with the registry" — pinning
    # the list meant every new lens broke this test for no reason.
    assert data["lenses"] == sorted(replenish.LENSES)
    # A digest carries no readable prefix, so lens attribution comes from the
    # row's own `lens` field — read back here rather than inferred. A bare
    # length-64 check would pass for a fingerprint minted by ANY lens, which is
    # weaker than the readable-prefix assertion this replaced.
    landed = [e.payload for e in events.read_all()
              if e.type == "work_injected"
              and (e.payload or {}).get("outcome") == "landed"]
    assert [p["lens"] for p in landed] == ["evidence-gap"]
    assert data["spent_fingerprints"] == [landed[0]["fingerprint"]]


def test_status_on_an_empty_ledger(capsys):
    assert main(["replenish", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"surface": "session", "last_recorded_round": 0,
                    "dry_rounds": 0, "spent_fingerprints": [],
                    "lenses": sorted(replenish.LENSES)}


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


def test_a_lens_failure_is_reported_and_does_not_claim_a_clean_record(
        monkeypatch, capsys):
    # Patched over a lens the PROFILE actually runs. Registering a new name in
    # LENSES no longer works, because this command runs the profile's lens set
    # rather than the whole registry — which is the point of that change.
    def boom(_ctx):
        raise RuntimeError("lens exploded")

    monkeypatch.setitem(replenish.LENSES, "coverage", boom)
    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    assert main(["replenish", "run", "--session", SID]) == 0
    out = capsys.readouterr()
    assert "lens coverage failed" in out.err
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


def test_the_cli_surfaces_real_tree_candidates(tmp_path, monkeypatch, capsys):
    """The one case that opts out of the blindfold above.

    Without it, nothing would prove the CLI reaches the tree-reading lenses at
    all — every other test here is deliberately blind to the checkout, and a
    plumbing bug that disconnected them would pass the whole file."""
    src_root = tmp_path / "src" / "mindhive"
    tests_root = tmp_path / "tests"
    src_root.mkdir(parents=True)
    tests_root.mkdir(parents=True)
    # Four thresholds deep, so it clears the session surface's `balanced` bar.
    # A file barely over the line scores the base and is correctly refused —
    # see test_a_module_barely_over_the_threshold_does_not_interrupt_work.
    (src_root / "enormous.py").write_text("# line\n" * 4200, encoding="utf-8")
    monkeypatch.setattr(
        replenish, "resolve_trees",
        lambda _ctx: replenish.SourceTrees(str(src_root), str(tests_root),
                                           str(tmp_path)))
    monkeypatch.setattr(replenish, "git_modified_paths", lambda _root: [])

    events.append_event("user_prompt", {"text": "ask"}, session_id=SID)
    assert main(["replenish", "run", "--session", SID]) == 0
    out = capsys.readouterr().out
    # An oversized module with no test file is BOTH oversized and uncovered, so
    # two lenses name the same path and the round fuses them into one systemic
    # intervention rather than two competing steps. The constituent lens still has
    # to be visible in the evidence — that is what proves the tree was read.
    assert "injected [resonance]" in out
    assert "enormous.py" in out
    assert "[tech-debt] Refactor and reduce line count" in out


def test_the_session_surface_never_spends_a_model_call(monkeypatch, capsys):
    """The defect this pins: `run_round` with no lens set runs the WHOLE registry.

    `innovation` is gated to `ambitious` because it costs a model call, and this
    command runs inside the SessionEnd hook — so without a profile every session
    end on this machine would fire a headless `claude`."""
    fired = {"n": 0}

    def tripwire(_ctx):
        fired["n"] += 1
        return []

    monkeypatch.setitem(replenish.LENSES, "innovation", tripwire)
    _outstanding()
    assert main(["replenish", "run", "--session", SID]) == 0
    capsys.readouterr()
    assert fired["n"] == 0


def test_the_session_surface_runs_the_deterministic_lenses(monkeypatch, capsys):
    # The mirror of the test above: proving innovation is skipped is only
    # meaningful if the other lenses are demonstrably reached.
    reached = set()

    def spy(name):
        def lens(_ctx):
            reached.add(name)
            return []
        return lens

    for name in replenish.PROFILES[replenish.DEFAULT_AMBITION].lens_names:
        monkeypatch.setitem(replenish.LENSES, name, spy(name))
    _outstanding()
    main(["replenish", "run", "--session", SID])
    capsys.readouterr()
    assert reached == set(
        replenish.PROFILES[replenish.DEFAULT_AMBITION].lens_names)
