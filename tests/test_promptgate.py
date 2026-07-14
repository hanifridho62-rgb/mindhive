import json

import pytest

from mindhive import events, promptgate
from mindhive import __main__ as cli


# --- assess: deterministic prompt-quality verdicts -----------------------------

def test_vague_one_liner_fires():
    v = promptgate.assess("fix it")
    assert not v["skip"] and not v["clear"]
    dims = {f["dimension"] for f in v["findings"]}
    assert {"too-short", "vague-verb", "dangling-reference"} <= dims


def test_detailed_prompt_is_clear():
    v = promptgate.assess(
        "Refactor src/mindhive/enforce.py so evaluate() returns the manifest "
        "reason override, and add a regression test in tests/test_enforce.py "
        "asserting the override wins.")
    assert v["clear"] and not v["skip"]


def test_vague_verb_with_concrete_target_stands_down():
    v = promptgate.assess("improve the error message in src/mindhive/guard.py")
    assert v["clear"]


def test_method_skip_is_a_hard_single_finding():
    v = promptgate.assess(
        "add retry handling to the dashboard poll loop endpoints, skip tests")
    assert not v["clear"]
    assert any(f["dimension"] == "method-skip" and f["hard"]
               for f in v["findings"])


def test_one_soft_finding_alone_stays_clear():
    v = promptgate.assess("run tests")           # too-short only
    assert v["clear"]
    assert [f["dimension"] for f in v["findings"]] == ["too-short"]


def test_ambiguity_markers_plus_no_target_fire():
    v = promptgate.assess("maybe clean up the stuff somewhere in the repo")
    assert not v["clear"]
    dims = {f["dimension"] for f in v["findings"]}
    assert "ambiguity-markers" in dims and "vague-verb" in dims


@pytest.mark.parametrize("text", [
    "", "<task-notification>x</task-notification>", "/ce-plan do the thing",
    "Base directory for this skill: C:/x",
])
def test_machine_prompts_are_skipped(text):
    v = promptgate.assess(text)
    assert v["skip"] and v["clear"]


def test_apostrophes_are_not_a_quoted_target():
    # two apostrophes must not read as a quoted span and suppress the gate
    v = promptgate.assess("don't worry, it's fine, just improve it somehow")
    assert not v["clear"]


# --- training: measured skills win; history grounds observed counts ------------

_SKILLS = [
    {"name": "planner", "plugin": "p",
     "description": "Create structured plans for multi-step tasks and break "
                    "down goal requirements"},
    {"name": "planner-fancy", "plugin": "p",
     "description": "Plan breakdown of structured multi-step task goal "
                    "requirements with extra elaborate wording and length"},
    {"name": "unrelated", "plugin": "p", "description": "draw seaborn charts"},
]


def _stats(rate_calls):
    out = {}
    for name, (ok, calls) in rate_calls.items():
        out[name] = {"calls": calls, "ok": ok, "fail": calls - ok,
                     "completion_rate": (ok / calls) if calls else None}
    return out


def test_train_prefers_measured_over_estimated():
    stats = _stats({"planner": (3, 3)})          # measured 3/3; rival unmeasured
    man = promptgate.train(_SKILLS, stats, now="2026-07-14T00:00:00Z")
    rule = man["rules"]["too-short"]
    assert rule["skill"] == "planner"
    assert rule["provenance"] == "measured"
    assert rule["directive"] == "must"           # >= AUTO_MIN_RATE


def test_train_low_rate_measured_skill_is_try_not_must():
    stats = _stats({"planner": (2, 4)})          # measured but 0.5 < auto gate
    man = promptgate.train(_SKILLS, stats)
    assert man["rules"]["too-short"]["directive"] == "try"


def test_train_omits_dimension_with_no_matching_skill():
    man = promptgate.train([_SKILLS[2]], {})     # only the unrelated skill
    assert man["rules"] == {}
    assert man["coverage"]["rules"] == 0


def test_observed_counts_ground_rules_in_history():
    events.append_event("user_prompt", {"text": "fix it"}, project="test")
    events.append_event("user_prompt", {"text": "<task-notification>x"},
                        project="test")
    evs = events.read_all("test")
    man = promptgate.train(_SKILLS, _stats({"planner": (3, 3)}), evs=evs)
    assert man["coverage"]["prompts_scanned"] == 1   # machine prompt filtered
    assert man["rules"]["too-short"]["observed"] == 1


def test_train_and_store_round_trips_and_audits(tmp_path):
    res = promptgate.train_and_store(project="test")
    assert res["trained"] is True
    assert promptgate.load_manifest("test").get("trained_at")
    trained = [e for e in events.read_all("test") if e.type == "trigger_trained"]
    assert len(trained) == 1
    # stale gate: an immediate second call keeps the fresh manifest
    res2 = promptgate.train_and_store(project="test", max_age_hours=24)
    assert res2["trained"] is False


# --- hook_context: the UserPromptSubmit output contract -------------------------

def _seed_manifest(project="test"):
    man = promptgate.train(_SKILLS, _stats({"planner": (3, 3)}),
                           now="2026-07-14T00:00:00Z")
    promptgate._write_manifest(project, man)
    return man


def test_hook_context_emits_additional_context():
    _seed_manifest()
    raw = json.dumps({"prompt": "fix it", "session_id": "s1"})
    out = promptgate.hook_context(raw, project="test")
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    ctx = hso["additionalContext"]
    assert promptgate.TRIGGER_MARK in ctx
    assert "MUST USE: planner" in ctx
    assert "block" not in json.dumps(out)        # additive only, never a block


def test_hook_context_try_wording_for_unproven_skill():
    man = promptgate.train(_SKILLS, {}, now="2026-07-14T00:00:00Z")
    promptgate._write_manifest("test", man)
    out = promptgate.hook_context(json.dumps({"prompt": "fix it"}),
                                  project="test")
    assert "TRY & MEASURE" in out["hookSpecificOutput"]["additionalContext"]
    assert "MUST USE" not in out["hookSpecificOutput"]["additionalContext"]


def test_hook_context_silent_on_clear_prompt():
    _seed_manifest()
    raw = json.dumps({"prompt": "Refactor src/mindhive/enforce.py evaluate() "
                                "and extend tests/test_enforce.py"})
    assert promptgate.hook_context(raw, project="test") == {}


def test_hook_context_silent_without_manifest():
    raw = json.dumps({"prompt": "fix it"})
    assert promptgate.hook_context(raw, project="test") == {}


@pytest.mark.parametrize("raw", ["not json{{", "", json.dumps(["list"])])
def test_hook_context_fail_open_on_garbage(raw):
    assert promptgate.hook_context(raw, project="test") == {}


def test_hook_context_dedups_shared_skill_across_dimensions():
    _seed_manifest()
    out = promptgate.hook_context(json.dumps({"prompt": "fix it"}),
                                  project="test")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.count("planner") == 1


# --- CLI wiring -----------------------------------------------------------------

def test_cli_hook_stage_prints_context_only_when_firing(tmp_path, capsys, monkeypatch):
    import io
    import sys
    _seed_manifest()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "fix it"})))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["hook", "user_prompt_submit"]) == 0
    out = capsys.readouterr().out
    assert "UserPromptSubmit" in out and "MUST USE: planner" in out

    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(json.dumps({"prompt": "Refactor "
                                    "src/mindhive/enforce.py evaluate()"})))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["hook", "user_prompt_submit"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_cli_triggers_train_and_list(tmp_path, capsys):
    assert cli.main(["triggers", "train", "--project", "test"]) == 0
    out = capsys.readouterr().out
    assert "trigger rule(s)" in out
    assert promptgate._manifest_path("test").exists()

    assert cli.main(["triggers", "list", "--project", "test"]) == 0
    out = capsys.readouterr().out
    assert "trained" in out


def test_cli_triggers_check_reports_verdict(tmp_path, capsys):
    _seed_manifest()
    assert cli.main(["triggers", "check", "fix it", "--project", "test"]) == 0
    out = capsys.readouterr().out
    assert "fires:" in out and "planner" in out

    assert cli.main(["triggers", "check",
                     "Refactor src/mindhive/enforce.py evaluate() and extend "
                     "tests/test_enforce.py", "--project", "test"]) == 0
    assert "clear" in capsys.readouterr().out
