# tests/test_recommend.py — deterministic recommendation engine (U6, KTD6).
# Pure-function fixtures throughout: skills are {name, plugin, description}
# dicts, stats mirror plugins.skill_stats output, incidents mirror
# incidents.summaries records. No store needed.
from mindhive import recommend


def _skill(name, description, plugin="p"):
    return {"name": name, "plugin": plugin, "description": description}


def _stat(calls, ok, streak=None):
    fail = calls - ok
    return {"calls": calls, "ok": ok, "fail": fail,
            "completion_rate": (ok / calls) if calls else None,
            "last_outcome": ("ok" if fail == 0 else "fail") if calls else None,
            "last_used": "2026-07-01T10:00:00Z",
            "streak": streak if streak is not None else (ok if fail == 0 else 0),
            "provenance": {"invoke": calls}, "sources": ["invoke_result"]}


def _names(payload):
    return [e["name"] for e in payload["recommendations"]]


NEEDS = ["tests keep failing in the pytest suite"]


def test_measured_skill_outranks_unmeasured_higher_breadth_for_same_need():
    # measured: 4 calls, 100%. unmeasured rival: description packed with topic
    # keywords (higher breadth prior) and matching the same need — but the
    # descriptions share almost no keywords, so no overlap grouping kicks in.
    measured = _skill("proven", "Fix failing pytest suite runs quickly.")
    broad = _skill("shiny", "Covers tests, security, frontend, backend, data, "
                            "deploy, debug and performance failing everywhere.")
    stats = {"proven": _stat(4, 4)}
    out = recommend.recommend([broad, measured], stats, needs=NEEDS)
    assert _names(out)[0] == "proven"
    proven = out["recommendations"][0]
    assert proven["provenance"] == "measured"
    assert proven["score"] > next(e for e in out["recommendations"]
                                  if e["name"] == "shiny")["score"]


def test_open_skill_incident_penalizes_below_clean_twin():
    # identical twins, no needs (so no overlap grouping) — the one with an
    # OPEN skill:* incident must rank below its clean twin
    a = _skill("clean-twin", "Use for database migration work.")
    b = _skill("dirty-twin", "Use for database migration work.")
    incidents = [{"signature": "skill:dirty-twin", "status": "active"}]
    out = recommend.recommend([a, b], {}, incidents=incidents, needs=[])
    assert _names(out) == ["clean-twin", "dirty-twin"]
    dirty = out["recommendations"][1]
    assert dirty["incident_penalty"] == recommend.OPEN_INCIDENT_PENALTY
    assert dirty["score"] < out["recommendations"][0]["score"]


def test_non_active_skill_incident_is_not_penalized():
    incidents = [{"signature": "skill:x", "status": "candidate"},
                 {"signature": "skill:x", "status": "demoted"},
                 {"signature": "pymodule:x", "status": "active"}]
    assert recommend.open_skill_incidents(incidents) == {}


def test_overlap_group_winner_by_measured_rate():
    # similar descriptions, same need -> ONE recommendation; tie-break level 1:
    # the higher measured completion rate wins
    a = _skill("high-rate", "Use when pytest suite tests fail during runs.")
    b = _skill("low-rate", "Use when pytest suite tests fail during checks.")
    stats = {"high-rate": _stat(4, 4), "low-rate": _stat(4, 2)}
    out = recommend.recommend([a, b], stats, needs=NEEDS)
    assert len(out["recommendations"]) == 1
    primary = out["recommendations"][0]
    assert primary["name"] == "high-rate"
    assert [alt["name"] for alt in primary["alternatives"]] == ["low-rate"]
    assert primary["alternatives"][0]["provenance"] == "measured"


def test_overlap_group_winner_by_description_clarity():
    # tie-break level 2: equal measured rates -> the description with the
    # explicit "use when" trigger beats the vaguer one
    a = _skill("clear", "Use when pytest suite tests fail during runs.")
    b = _skill("vague", "Helpful if pytest suite tests fail during runs.")
    stats = {"clear": _stat(4, 4), "vague": _stat(4, 4)}
    out = recommend.recommend([a, b], stats, needs=NEEDS)
    assert len(out["recommendations"]) == 1
    assert out["recommendations"][0]["name"] == "clear"


def test_overlap_group_clarity_shorter_description_wins():
    # both carry "use when": the SHORTER (less ambiguous) description wins
    a = _skill("terse", "Use when pytest suite tests fail.")
    b = _skill("rambling", "Use when pytest suite tests fail, or possibly other "
                           "situations too, depending on circumstances.")
    stats = {"terse": _stat(4, 4), "rambling": _stat(4, 4)}
    out = recommend.recommend([a, b], stats, needs=NEEDS)
    assert len(out["recommendations"]) == 1
    assert out["recommendations"][0]["name"] == "terse"


def test_overlap_group_winner_by_completion_streak():
    # tie-break level 3: equal rate + identical clarity (same trigger, same
    # length) -> the longer trailing failure-free streak wins
    desc_a = "Use when pytest suite tests fail on run A."
    desc_b = "Use when pytest suite tests fail on run B."
    assert len(desc_a) == len(desc_b)
    a = _skill("steady", desc_a)
    b = _skill("shaky", desc_b)
    stats = {"steady": _stat(4, 2, streak=2), "shaky": _stat(4, 2, streak=0)}
    out = recommend.recommend([a, b], stats, needs=NEEDS)
    assert len(out["recommendations"]) == 1
    assert out["recommendations"][0]["name"] == "steady"


def test_never_called_skills_are_estimated_never_measured():
    out = recommend.recommend([_skill("a", "Use for testing."),
                               _skill("b", "Use for deploys.")], {}, needs=[])
    assert all(e["provenance"] == "estimated" for e in out["recommendations"])
    assert all(e["calls"] == 0 for e in out["recommendations"])
    assert out["coverage"]["measured"] == 0


def test_below_min_calls_stays_estimated():
    stats = {"a": _stat(2, 2)}                 # 2 calls < MEASURED_MIN_CALLS
    out = recommend.recommend([_skill("a", "Use for testing.")], stats, needs=[])
    e = out["recommendations"][0]
    assert e["provenance"] == "estimated"
    assert e["breadth_prior"] is not None      # base fell back to the prior
    assert out["coverage"]["measured"] == 0


def test_unmeasured_high_need_match_surfaces_as_try_to_measure_alternative():
    # a measured incumbent wins the overlap group, but the unmeasured
    # challenger with high need_match rides along flagged try_to_measure
    incumbent = _skill("incumbent", "Use when pytest suite tests fail during runs.")
    challenger = _skill("challenger", "Use when pytest suite tests fail during triage.")
    stats = {"incumbent": _stat(4, 4)}
    out = recommend.recommend([incumbent, challenger], stats, needs=NEEDS)
    assert len(out["recommendations"]) == 1
    primary = out["recommendations"][0]
    assert primary["name"] == "incumbent" and primary["provenance"] == "measured"
    [alt] = primary["alternatives"]
    assert alt["name"] == "challenger"
    assert alt["provenance"] == "estimated"
    assert alt["try_to_measure"] is True


def test_exploration_bonus_is_deterministic_and_flagged():
    matched = recommend.score_skill(
        _skill("m", "Use when pytest suite tests fail."), {}, needs=NEEDS)
    unmatched = recommend.score_skill(
        _skill("u", "Use when kubernetes clusters drift."), {}, needs=NEEDS)
    assert matched["try_to_measure"] is True
    assert unmatched["try_to_measure"] is False
    again = recommend.score_skill(
        _skill("m", "Use when pytest suite tests fail."), {}, needs=NEEDS)
    assert again == matched                     # fully deterministic


def test_empty_ledger_falls_back_to_breadth_ranking():
    # no stats at all -> breadth priors rank the inventory without error
    a = _skill("narrow", "Use for docs.")                       # 1 topic
    b = _skill("wide", "Use for tests, security and deploy.")   # 3 topics
    out = recommend.recommend([a, b], {}, incidents=[], needs=[])
    assert out["coverage"] == {"measured": 0, "total": 2}
    assert _names(out)[0] == "wide"
    assert all(e["provenance"] == "estimated" for e in out["recommendations"])


def test_recommend_top_limits_entries():
    skills = [_skill(f"s{i}", "Use for testing.") for i in range(5)]
    out = recommend.recommend(skills, {}, top=2)
    assert len(out["recommendations"]) == 2
    assert out["coverage"]["total"] == 5        # coverage counts the whole pool


def test_flatten_inventory_covers_plugin_and_local_skills():
    inv = {"plugins": [{"name": "pl", "skills": [
        {"name": "a", "trigger": "Use for testing."}]}],
        "local_skills": [{"name": "solo", "trigger": "Use alone."}]}
    flat = recommend.flatten_inventory(inv)
    assert flat == [
        {"name": "a", "plugin": "pl", "description": "Use for testing."},
        {"name": "solo", "plugin": "local", "description": "Use alone."}]


def test_extract_needs_from_summaries_and_next_steps():
    class Ev:
        def __init__(self, type, payload):
            self.type, self.payload = type, payload
    summaries = [{"signature": "pscmd:pytest", "status": "active",
                  "category": "testing"}]
    evs = [Ev("next_step", {"text": "stabilize the flaky suite"}),
           Ev("decision", {"text": "ignored"})]
    needs = recommend.extract_needs(summaries, evs)
    assert "testing" in needs
    assert "pscmd:pytest" in needs
    assert "stabilize the flaky suite" in needs
    assert "ignored" not in needs


def test_every_entry_and_alternative_carries_provenance():
    a = _skill("a", "Use when pytest suite tests fail during runs.")
    b = _skill("b", "Use when pytest suite tests fail during triage.")
    c = _skill("c", "Use for kubernetes drift.")
    out = recommend.recommend([a, b, c], {"a": _stat(3, 3)}, needs=NEEDS)
    for e in out["recommendations"]:
        assert e["provenance"] in ("measured", "estimated")
        for alt in e["alternatives"]:
            assert alt["provenance"] in ("measured", "estimated")


# --- learning-gated execution: mode, categories, discontinuation (U6+U7) ---

def _fstat(calls, ok, fail_streak=0, agent_assessed=0, last=None):
    s = _stat(calls, ok)
    s["fail_streak"] = fail_streak
    if last:
        s["last_outcome"] = last
    if agent_assessed:
        s["provenance"] = {"invoke": calls - agent_assessed,
                           "agent-assessed": agent_assessed}
    return s


def test_proven_skill_earns_auto_mode():
    out = recommend.score_skill(_skill("a", "d"), {"a": _fstat(5, 5)})
    assert out["execution_mode"] == "auto"


def test_open_incident_forces_suggest_mode():
    out = recommend.score_skill(_skill("a", "d"), {"a": _fstat(5, 5)},
                                open_incidents={"a": 1})
    assert out["execution_mode"] == "suggest"


def test_unproven_skill_is_suggest_only():
    # 2 calls at 100% is below MEASURED_MIN_CALLS — never-used/unproven
    # skills must not run automatically
    assert recommend.score_skill(_skill("a", "d"),
                                 {"a": _fstat(2, 2)})["execution_mode"] == "suggest"
    assert recommend.score_skill(_skill("a", "d"), {})["execution_mode"] == "suggest"


def test_low_rate_is_suggest_only():
    out = recommend.score_skill(_skill("a", "d"), {"a": _fstat(5, 3)})
    assert out["execution_mode"] == "suggest"      # 0.6 < AUTO_MIN_RATE


def test_outcome_categories():
    assert recommend.outcome_category(None) is None
    assert recommend.outcome_category(_fstat(3, 2, last="fail")) == "failure"
    assert recommend.outcome_category(_fstat(4, 4, agent_assessed=2)) == "supervision-needed"
    assert recommend.outcome_category(_fstat(4, 4)) == "no-intervention-needed"
    assert recommend.outcome_category(_fstat(1, 1)) == "success"


def test_discontinued_with_active_rule_and_streak():
    incidents = [{"signature": "skill:x", "status": "active",
                  "active_rule": "never run x without a dry-run first"}]
    out = recommend.recommend([_skill("x", "d")],
                              {"x": _fstat(5, 2, fail_streak=3, last="fail")},
                              incidents=incidents, needs=[])
    assert _names(out) == []
    assert out["discontinued"][0]["name"] == "x"
    assert "dry-run" in out["discontinued"][0]["rule"]
    assert out["coverage"]["total"] == 1


def test_streak_below_threshold_stays_recommended():
    incidents = [{"signature": "skill:x", "status": "active",
                  "active_rule": "rule text"}]
    out = recommend.recommend([_skill("x", "d")],
                              {"x": _fstat(5, 3, fail_streak=2, last="fail")},
                              incidents=incidents, needs=[])
    assert _names(out) == ["x"]
    assert out["discontinued"] == []


def test_dismissed_rule_does_not_discontinue():
    incidents = [{"signature": "skill:x", "status": "demoted",
                  "active_rule": "rule text"}]
    out = recommend.recommend([_skill("x", "d")],
                              {"x": _fstat(5, 2, fail_streak=4, last="fail")},
                              incidents=incidents, needs=[])
    assert _names(out) == ["x"]


def test_successful_call_breaks_streak_and_restores():
    # fail_streak resets to 0 after an ok call → skill returns to the ranking
    incidents = [{"signature": "skill:x", "status": "active",
                  "active_rule": "rule text"}]
    out = recommend.recommend([_skill("x", "d")],
                              {"x": _fstat(6, 3, fail_streak=0)},
                              incidents=incidents, needs=[])
    assert _names(out) == ["x"]
