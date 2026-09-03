# tests/test_enrich.py
"""Prompt enrichment (Oversee): the default-on toggle, the three context
sources (questions / source code / skills), the Anthropic-SOP checklist, the
composed prompt, the audit events, the hook extension, and the CLI."""
import io
import json
import sys

import pytest

from mindhive import enrich, events, promptgate
from mindhive import __main__ as cli

STRONG = ("Refactor src/mindhive/enforce.py so evaluate() returns the "
          "manifest reason override, and add a regression test in "
          "tests/test_enforce.py asserting the override wins.")

_SKILLS = [
    {"name": "planner", "plugin": "p",
     "description": "Create structured plans for multi-step tasks and break "
                    "down goal requirements"},
    {"name": "dash-helper", "plugin": "p",
     "description": "dashboard poll retry handling helper"},
    {"name": "unrelated", "plugin": "p", "description": "draw seaborn charts"},
]


def _stats(rate_calls):
    out = {}
    for name, (ok, calls) in rate_calls.items():
        out[name] = {"calls": calls, "ok": ok, "fail": calls - ok,
                     "completion_rate": (ok / calls) if calls else None}
    return out


def _fixture_inventory():
    return {"changed": True, "signature": "x", "inventory": {
        "plugins": [{"name": "p", "skills": [
            {"name": s["name"], "trigger": s["description"]}
            for s in _SKILLS]}],
        "local_skills": []}}


# --- settings: default-on toggle ------------------------------------------------

def test_settings_default_on_when_sidecar_missing():
    st = enrich.settings("test")
    assert st["enabled"] is True and st["source"] == "default"


def test_settings_default_on_when_sidecar_corrupt():
    p = enrich._settings_path("test")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert enrich.settings("test")["enabled"] is True


def test_set_enabled_persists_and_audits():
    st = enrich.set_enabled(False, project="test", session_id="s1")
    assert st["enabled"] is False
    assert enrich.settings("test") == {"enabled": False,
                                       "updated": st["updated"],
                                       "source": "sidecar"}
    toggles = [e for e in events.read_all("test") if e.type == "enrich_toggled"]
    assert len(toggles) == 1 and toggles[0].payload["enabled"] is False
    enrich.set_enabled(True, project="test")
    assert enrich.settings("test")["enabled"] is True


# --- 1. asking the user ---------------------------------------------------------

def test_questions_per_dimension_plus_verification():
    v = promptgate.assess("fix it")
    qs = enrich.clarifying_questions(v, "fix it")
    ids = [q["id"] for q in qs]
    assert "too-short" in ids and "vague-verb" in ids
    assert "dangling-reference" in ids and "verification" in ids


def test_questions_capped():
    text = "maybe fix it somehow"        # 4 soft dimensions fire
    qs = enrich.clarifying_questions(promptgate.assess(text), text)
    assert len(qs) == enrich.QUESTION_CAP
    assert all(q["id"] != "verification" for q in qs)   # cap drops the extra


def test_no_verification_question_when_stated():
    qs = enrich.clarifying_questions(promptgate.assess(STRONG), STRONG)
    assert qs == []


# --- 2. checking the source code ------------------------------------------------

def _touch(path, ts, session="s1"):
    events.append_event("file_touched", {"path": path}, session_id=session,
                        ts=ts, project="test")


def test_source_candidates_rank_overlap_then_count_then_recency():
    _touch("src/mindhive/dashboard.py", "2026-07-01T00:00:00Z")
    _touch("src/mindhive/dashboard.py", "2026-07-02T00:00:00Z")
    _touch("src/mindhive/dashboard_page.py", "2026-07-03T00:00:00Z")
    _touch("src/mindhive/graph.py", "2026-07-04T00:00:00Z")
    evs = events.read_all("test")
    out = enrich.source_candidates("fix the dashboard page rendering",
                                   evs=evs, project="test")
    paths_ = [s["path"] for s in out]
    # dashboard_page matches 2 keywords (dashboard, page) -> first;
    # graph.py matches none -> excluded entirely
    assert paths_[0] == "src/mindhive/dashboard_page.py"
    assert "src/mindhive/dashboard.py" in paths_
    assert all("graph" not in p for p in paths_)
    assert out[0]["source"] == "history"


def test_source_candidates_empty_without_keywords():
    assert enrich.source_candidates("it", evs=[], project="test") == []


def test_repo_scan_matches_prunes_and_dedupes(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "src" / "dashboard_widget.py").write_text("x", encoding="utf-8")
    (root / ".git" / "dashboard_hidden.py").write_text("x", encoding="utf-8")
    (root / "src" / "unrelated.txt").write_text("x", encoding="utf-8")
    _touch("src/dashboard_history.py", "2026-07-01T00:00:00Z")
    out = enrich.source_candidates("fix the dashboard widget rendering",
                                   evs=events.read_all("test"),
                                   project="test", root=root)
    scan = [s for s in out if s["source"] == "scan"]
    assert [s["path"] for s in scan] == ["src/dashboard_widget.py"]
    # pruned dirs never surface, history entries stay first
    assert out[0]["path"] == "src/dashboard_history.py"


def test_repo_scan_fail_open_on_missing_root(tmp_path):
    out = enrich.source_candidates("fix the dashboard rendering", evs=[],
                                   project="test",
                                   root=tmp_path / "nope")
    assert out == []


# --- 3. injecting skills --------------------------------------------------------

def _seed_manifest(project="test"):
    man = promptgate.train(_SKILLS, _stats({"planner": (3, 3)}),
                           now="2026-07-14T00:00:00Z")
    promptgate._write_manifest(project, man)
    return man


def test_manifest_directives_dedup_by_skill():
    _seed_manifest()
    findings = promptgate.assess("fix it")["findings"]
    out = enrich.manifest_directives(findings, "test")
    assert len(out) == 1
    assert out[0]["skill"] == "planner" and out[0]["directive"] == "must"


def test_skill_suggestions_measured_first_and_excludes():
    text = "add dashboard poll retry handling"
    stats = _stats({"dash-helper": (3, 3)})
    out = enrich.skill_suggestions(text, skills=_SKILLS, stats=stats)
    assert out[0]["skill"] == "dash-helper"
    assert out[0]["provenance"] == "measured" and out[0]["directive"] == "must"
    assert all(s["skill"] != "unrelated" for s in out)
    out2 = enrich.skill_suggestions(text, skills=_SKILLS, stats=stats,
                                    exclude={"dash-helper"})
    assert all(s["skill"] != "dash-helper" for s in out2)


# --- SOP checklist --------------------------------------------------------------

def test_sop_checklist_weak_prompt():
    text = "fix it"
    by_id = {c["id"]: c["met"]
             for c in enrich.sop_checklist(text, promptgate.assess(text))}
    assert by_id == {"concrete-target": False, "sufficient-detail": False,
                     "unambiguous-intent": False, "clear-antecedents": False,
                     "method-intact": True, "verification-stated": False}


def test_sop_checklist_strong_prompt_all_met():
    checks = enrich.sop_checklist(STRONG, promptgate.assess(STRONG))
    assert all(c["met"] for c in checks)


def test_sop_checklist_method_skip_unmet():
    text = ("add retry handling to the dashboard poll loop endpoints, "
            "skip tests")
    by_id = {c["id"]: c["met"]
             for c in enrich.sop_checklist(text, promptgate.assess(text))}
    assert by_id["method-intact"] is False


# --- composition ----------------------------------------------------------------

def test_compose_folds_answers_and_marks_pending():
    text = "fix it"
    v = promptgate.assess(text)
    qs = enrich.clarifying_questions(v, text)
    checks = enrich.sop_checklist(text, v)
    out = enrich.compose(text, qs, {"verification": "pytest green"},
                         [{"path": "src/x.py", "touched": 2,
                           "last": "2026-07-01T00:00:00Z",
                           "source": "history", "why": "w"}],
                         [{"skill": "planner", "plugin": "p",
                           "provenance": "measured", "ok": 3, "calls": 3,
                           "directive": "must"}],
                         checks)
    assert "## Task" in out and "## Context — likely source files" in out
    assert "`src/x.py`" in out
    assert "## Clarifications" in out
    assert "- pytest green" in out                     # answer became the DoD
    assert "(unanswered — resolve before running)" in out
    assert "Use `planner` (p) via the Skill tool — measured 3/3" in out
    assert "## SOP gaps to close" in out
    assert "Unanswered clarifications remain" in out


def test_enrich_is_deterministic():
    _seed_manifest()
    kw = dict(project="test", evs=[], skills=_SKILLS,
              stats=_stats({"planner": (3, 3)}))
    a = enrich.enrich("fix it", **kw)
    b = enrich.enrich("fix it", **kw)
    assert a == b
    assert a["enabled"] is True
    assert a["skills"]["directives"][0]["skill"] == "planner"
    # a directive skill is never repeated as a suggestion
    assert all(s["skill"] != "planner" for s in a["skills"]["suggestions"])
    assert "## Task" in a["enriched_prompt"]


# --- audit event ----------------------------------------------------------------

def test_record_caps_text_and_appends_event():
    res = enrich.enrich("fix it " + "x" * 500, project="test", evs=[],
                        skills=[], stats={})
    enrich.record("fix it " + "x" * 500, res, project="test", session_id="s1")
    evs = [e for e in events.read_all("test") if e.type == "prompt_enriched"]
    assert len(evs) == 1
    p = evs[0].payload
    assert len(p["text"]) == enrich.RECORD_TEXT_CAP
    assert p["sop_total"] == 6 and "questions" in p and "sources" in p


# --- hook extension -------------------------------------------------------------

def test_extend_hook_context_appends_questions_by_default():
    _seed_manifest()
    raw = json.dumps({"prompt": "fix it"})
    ctx = promptgate.hook_context(raw, project="test")
    out = enrich.extend_hook_context(ctx, raw, project="test")
    add = out["hookSpecificOutput"]["additionalContext"]
    assert promptgate.TRIGGER_MARK in add          # original context intact
    assert enrich.ENRICH_MARK in add
    assert "What exactly should change" in add


def test_extend_hook_context_silent_when_disabled():
    _seed_manifest()
    enrich.set_enabled(False, project="test")
    raw = json.dumps({"prompt": "fix it"})
    ctx = promptgate.hook_context(raw, project="test")
    assert enrich.extend_hook_context(ctx, raw, project="test") == ctx


def test_extend_hook_context_passes_empty_context_through():
    assert enrich.extend_hook_context({}, json.dumps({"prompt": "fix it"}),
                                      project="test") == {}


@pytest.mark.parametrize("raw", ["not json{{", "", json.dumps(["list"])])
def test_extend_hook_context_fail_open_on_garbage(raw):
    ctx = {"hookSpecificOutput": {"additionalContext": "x"}}
    assert enrich.extend_hook_context(ctx, raw, project="test") == ctx


def test_cli_hook_stage_carries_enrichment(capsys, monkeypatch):
    _seed_manifest()
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(json.dumps({"prompt": "fix it"})))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["hook", "user_prompt_submit"]) == 0
    out = capsys.readouterr().out
    assert promptgate.TRIGGER_MARK in out and enrich.ENRICH_MARK in out

    enrich.set_enabled(False, project="test")
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(json.dumps({"prompt": "fix it"})))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["hook", "user_prompt_submit"]) == 0
    out = capsys.readouterr().out
    assert promptgate.TRIGGER_MARK in out and enrich.ENRICH_MARK not in out


# --- CLI ------------------------------------------------------------------------

def test_cli_status_on_off_round_trip(capsys):
    assert cli.main(["enrich", "status", "--project", "test"]) == 0
    assert "enabled" in capsys.readouterr().out
    assert cli.main(["enrich", "off", "--project", "test"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert cli.main(["enrich", "status", "--project", "test"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert cli.main(["enrich", "on", "--project", "test"]) == 0
    capsys.readouterr()
    assert enrich.settings("test")["enabled"] is True


def test_cli_preview_prints_composed_prompt(capsys, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "live_inventory",
                        lambda claude_dir=None: _fixture_inventory())
    _seed_manifest()
    assert cli.main(["enrich", "preview", "fix it",
                     "--project", "test"]) == 0
    out = capsys.readouterr().out
    assert "## Task" in out and "## Definition of done" in out

    assert cli.main(["enrich", "preview", "fix it", "--project", "test",
                     "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["enabled"] is True and "enriched_prompt" in obj
    assert obj["sop"] and obj["questions"]


# --------------------------------------------------------------------------
# Typed slash commands must not carry a skill over the measured bar here
# either. This module re-derives `measured` from the stats row, so fixing
# recommend.score_skill alone would leave this producer of the operator-facing
# directive promoting a skill nobody graded.
# --------------------------------------------------------------------------

def _thin_stats(name, calls, unobserved, ok):
    """skill_stats for a skill whose calls were mostly typed slash commands:
    the rate is over the observed ones, `calls` counts every invocation."""
    seen = calls - unobserved
    return {name: {"calls": calls, "ok": ok, "fail": 0,
                   "unobserved": unobserved,
                   "completion_rate": (ok / seen) if seen else None,
                   "last_outcome": "ok" if ok else None,
                   "streak": ok, "fail_streak": 0}}


def test_a_thinly_graded_skill_is_not_promoted_by_its_typed_calls():
    """55 invocations, 2 of them graded, and both completed — so the rate is a
    truthful 1.0. Only the DENOMINATOR of the measured bar decides this one:
    against raw `calls` it is 55 >= 3 and prints as proven; against observed
    calls it is 2, and two observations are not a measurement.

    Kills 'MEASURED_MIN_CALLS against st["calls"]' in enrich."""
    stats = _thin_stats("dash-helper", 55, 53, 2)
    out = enrich.skill_suggestions("add dashboard poll retry handling",
                                   skills=_SKILLS, stats=stats)
    [s] = [e for e in out if e["skill"] == "dash-helper"]
    assert s["provenance"] == "estimated"
    assert s["directive"] == "try"


def test_a_typed_only_skill_is_not_suggested_as_measured():
    """The rate path rather than the gate: with nothing observed at all,
    `skill_stats` leaves completion_rate None and no call count can read as
    proven. Held upstream (test_unobserved_calls_count_as_calls_but_not_as
    _completions); asserted here because this is where the operator sees it."""
    stats = _thin_stats("dash-helper", 4, 4, 0)
    out = enrich.skill_suggestions("add dashboard poll retry handling",
                                   skills=_SKILLS, stats=stats)
    [s] = [e for e in out if e["skill"] == "dash-helper"]
    assert s["provenance"] == "estimated"
    assert s["directive"] == "try"


def test_the_suggested_fraction_uses_the_observed_denominator():
    """'measured 3/10 completion' reads as a 30% rate for a skill that
    completed all three runs anybody watched."""
    stats = _thin_stats("dash-helper", 10, 7, 3)
    out = enrich.skill_suggestions("add dashboard poll retry handling",
                                   skills=_SKILLS, stats=stats)
    [s] = [e for e in out if e["skill"] == "dash-helper"]
    assert s["provenance"] == "measured" and s["directive"] == "must"
    assert (s["ok"], s["calls"]) == (3, 3)


def test_a_fully_observed_skill_is_still_measured_here():
    """CANARY: the gate must count observed calls, not refuse to count."""
    stats = _stats({"dash-helper": (3, 3)})
    out = enrich.skill_suggestions("add dashboard poll retry handling",
                                   skills=_SKILLS, stats=stats)
    assert out[0]["provenance"] == "measured" and out[0]["directive"] == "must"
