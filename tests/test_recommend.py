# tests/test_recommend.py — deterministic recommendation engine (U6, KTD6).
# Pure-function fixtures throughout: skills are {name, plugin, description}
# dicts, stats mirror plugins.skill_stats output, incidents mirror
# incidents.summaries records. No store needed.
import math

import pytest

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
    # Four calls, not five: a two-streak multiplies the MEB score by e^-1, so
    # at five or more calls ANY two-streak is below 0.50 and the K2 score door
    # (next section) would discontinue it. Under DISCONTINUE_MIN_CALLS the
    # rule path's own threshold is the only thing being tested.
    incidents = [{"signature": "skill:x", "status": "active",
                  "active_rule": "rule text"}]
    out = recommend.recommend([_skill("x", "d")],
                              {"x": _fstat(4, 2, fail_streak=2, last="fail")},
                              incidents=incidents, needs=[])
    assert _names(out) == ["x"]
    assert out["discontinued"] == []
    assert out["recommendations"][0]["promotion"] == "PROBATION"


def test_a_dismissed_rule_never_reaches_the_rule_path_but_the_meb_score_still_judges():
    """Before P4 a dismissed rule meant no discontinuation at all. The MEB
    rule (K2) judges the RECORD: 2 of 5 with four trailing failures is
    DISCONTINUED whatever the incident's status -- by the score door, with
    the Laplace reason and no rule attached."""
    incidents = [{"signature": "skill:x", "status": "demoted",
                  "active_rule": "rule text"}]
    out = recommend.recommend([_skill("x", "d")],
                              {"x": _fstat(5, 2, fail_streak=4, last="fail")},
                              incidents=incidents, needs=[])
    assert _names(out) == []
    d = out["discontinued"][0]
    assert d["rule"] is None and d["promotion"] == "DISCONTINUED"
    assert d["reason"].startswith("discontinued: skor Laplace ")
    # CANARY: the same dismissed rule over a clean record discontinues nothing.
    out = recommend.recommend([_skill("x", "d")], {"x": _fstat(5, 5)},
                              incidents=incidents, needs=[])
    assert _names(out) == ["x"] and out["discontinued"] == []


def test_successful_call_breaks_streak_and_restores():
    # fail_streak resets to 0 after an ok call → skill returns to the ranking
    incidents = [{"signature": "skill:x", "status": "active",
                  "active_rule": "rule text"}]
    out = recommend.recommend([_skill("x", "d")],
                              {"x": _fstat(6, 3, fail_streak=0)},
                              incidents=incidents, needs=[])
    assert _names(out) == ["x"]


# --- blueprint P4 (MEB §2.3 C, decision K2): the Laplace promotion ----------

def test_a_scored_entry_carries_the_laplace_score_and_its_promotion():
    out = recommend.score_skill(_skill("a", "d"), {"a": _fstat(5, 5)})
    assert out["laplace_rate"] == pytest.approx(6 / 7)
    assert out["skill_score"] == pytest.approx(6 / 7)
    assert out["promotion"] == "QUALIFIED" and out["execution_mode"] == "auto"
    # the keys the projector and the dashboard already read are untouched
    assert out["completion_rate"] == 1.0 and out["score"] == 1.0
    assert "measured 5/5 completion" in out["reason"]


def test_a_never_called_skill_scores_the_flat_prior_and_stays_on_probation():
    out = recommend.score_skill(_skill("a", "d"), {})
    assert out["laplace_rate"] == 0.5 and out["skill_score"] == 0.5
    assert out["promotion"] == "PROBATION" and out["execution_mode"] == "suggest"


def test_a_streak_takes_the_promotion_down_with_the_meb_penalty():
    out = recommend.score_skill(_skill("a", "d"),
                                {"a": _fstat(10, 7, fail_streak=1, last="fail")})
    assert out["skill_score"] == pytest.approx((8 / 12) * math.exp(-0.5))
    assert out["promotion"] == "DISCONTINUED"      # 0.404 < 0.50 at ten calls


def test_auto_mode_is_exactly_the_qualified_status():
    for st, mode in ((_fstat(4, 4), "auto"), (_fstat(3, 3), "suggest"),
                     (_fstat(5, 3), "suggest")):
        out = recommend.score_skill(_skill("a", "d"), {"a": st})
        assert out["execution_mode"] == mode, (st, out["promotion"])
        assert (out["promotion"] == "QUALIFIED") == (mode == "auto")


def test_bad_bookkeeping_never_raises_out_of_score_skill():
    st = _fstat(5, 2, fail_streak=4, last="fail")       # a streak longer than the failures
    out = recommend.score_skill(_skill("a", "d"), {"a": st})
    assert out["promotion"] == "DISCONTINUED"
    st = dict(_fstat(5, 5), ok=9)                        # more successes than calls
    out = recommend.score_skill(_skill("a", "d"), {"a": st})
    assert out["promotion"] == "PROBATION" and out["execution_mode"] == "suggest"
    assert 0.0 <= out["laplace_rate"] <= 1.0 and 0.0 <= out["skill_score"] <= 1.0


def test_the_meb_score_rule_discontinues_without_any_incident():
    """`recommend()`'s rule path (active rule + streak) stays; the MEB adds a
    second door: a score below 0.50 after five calls, no incident needed."""
    out = recommend.recommend([_skill("x", "d")], {"x": _fstat(6, 2, fail_streak=0)},
                              needs=[])
    assert _names(out) == []
    d = out["discontinued"][0]
    assert d["name"] == "x" and d["rule"] is None and d["promotion"] == "DISCONTINUED"
    assert d["reason"] == "discontinued: skor Laplace 0.38 di bawah 0.50 setelah 6 panggilan"
    assert out["coverage"] == {"measured": 1, "total": 1}


def test_the_rule_path_keeps_its_reason_when_both_doors_would_open():
    incidents = [{"signature": "skill:x", "status": "active",
                  "active_rule": "dry-run first"}]
    out = recommend.recommend([_skill("x", "d")],
                              {"x": _fstat(5, 2, fail_streak=3, last="fail")},
                              incidents=incidents, needs=[])
    d = out["discontinued"][0]
    assert d["rule"] == "dry-run first"
    assert d["reason"].startswith("discontinued: 3 consecutive failures")
    assert d["promotion"] == "DISCONTINUED"


# --------------------------------------------------------------------------
# brief_line — the shared one-line renderer that surfaces intended use so an
# agent grasps a skill's scope from the recommendation itself.
# --------------------------------------------------------------------------

def test_brief_line_measured_carries_provenance_and_intended_use():
    out = recommend.recommend(
        [_skill("proven", "Fix failing pytest suite runs quickly.")],
        {"proven": _stat(4, 4)}, needs=NEEDS)
    line = recommend.brief_line(out["recommendations"][0])
    assert line.startswith("proven — ")
    assert "measured 4/4 completion" in line
    assert "(measured)" in line
    assert "intended use: Fix failing pytest suite runs quickly." in line


def test_brief_line_estimated_never_called_keeps_estimated_label():
    out = recommend.recommend([_skill("newbie", "Handles beta deployments.")],
                              {}, needs=[])
    line = recommend.brief_line(out["recommendations"][0])
    assert "never called — breadth-estimated" in line
    assert "(estimated)" in line          # an estimate must never read as measured
    assert "intended use: Handles beta deployments." in line


def test_brief_line_truncates_long_intended_use():
    long_desc = "word " * 100
    entry = {"name": "verbose", "reason": "never called — breadth-estimated",
             "provenance": "estimated", "description": long_desc}
    line = recommend.brief_line(entry, width=40)
    used = line.split("intended use: ", 1)[1]
    assert used.endswith("…")
    assert len(used) <= 40


def test_brief_line_omits_clause_when_description_empty():
    entry = {"name": "bare", "reason": "never called — breadth-estimated",
             "provenance": "estimated", "description": "   "}
    line = recommend.brief_line(entry)
    assert "intended use:" not in line
    assert line == "bare — never called — breadth-estimated (estimated)"


def test_brief_line_handles_delimiter_in_description():
    # a description containing the ' · intended use:' delimiter text must not
    # break rendering — brief_line does pure concatenation, no parsing
    entry = {"name": "meta", "reason": "never called — breadth-estimated",
             "provenance": "estimated",
             "description": "Use · intended use: nested markers stay literal"}
    line = recommend.brief_line(entry)
    assert line.count("(estimated)") == 1
    assert line.endswith("Use · intended use: nested markers stay literal")
    assert line.startswith("meta — never called")


# --------------------------------------------------------------------------
# Skills whose only record is slash-command captures. Those calls have no
# observed outcome, so plugins.skill_stats leaves completion_rate None. This
# is where that matters: `measured` gates the "MUST USE <name> - measured N/N
# completion. Proven skill" line that projector injects into MEMORY.md.
# --------------------------------------------------------------------------

def _unobserved_stat(calls):
    """What skill_stats reports for a skill invoked only by slash command."""
    return {"calls": calls, "ok": 0, "fail": 0, "unobserved": calls,
            "completion_rate": None, "last_outcome": None,
            "last_used": "2026-07-01T10:00:00Z", "streak": 0, "fail_streak": 0,
            "provenance": {"passive": calls}, "sources": ["skill_used"]}


def test_a_slash_only_skill_is_never_promoted_to_measured():
    """Kills 'calls alone make a skill measured'. Typing a slash command three
    times must not read as a proven skill: nothing observed whether any of
    those runs completed."""
    out = recommend.score_skill(_skill("a", "d"), {"a": _unobserved_stat(5)})
    assert out["completion_rate"] is None
    assert out["provenance"] == "estimated"
    assert out["execution_mode"] == "suggest"


def test_a_slash_only_skill_has_no_outcome_category():
    """outcome_category returned 'success' for these -- 'last call ok, record
    still thin' -- when no call had a recorded outcome at all.

    Kills 'treat an unwitnessed call as a success'."""
    assert recommend.outcome_category(_unobserved_stat(4)) is None


def test_a_slash_only_skill_is_not_no_intervention_needed():
    """The streak path is the other route to a proven-looking verdict, and the
    guard has to close it too.

    Note the fixture: ``streak`` carries a full clean run, which
    `plugins.skill_stats` does NOT emit for an unobserved-only skill (it stops
    unobserved calls extending the streak -- see
    test_unobserved_calls_do_not_extend_the_completion_streak, the test that
    actually holds that line). So this is the SECOND line of defence, pinned
    on the input a regression in that upstream counter would produce. An
    earlier version of this test used streak=0 and passed no matter what
    `outcome_category` did -- it asserted on a field the fixture had pinned
    shut, and mutating the guard away left it green."""
    st = _unobserved_stat(9)
    st["streak"] = recommend.MEASURED_MIN_CALLS
    assert recommend.outcome_category(st) is None


def test_an_observed_record_still_reaches_measured_and_auto():
    """CANARY: the gate above must not blank out genuinely measured skills."""
    out = recommend.score_skill(_skill("a", "d"), {"a": _fstat(5, 5)})
    assert out["provenance"] == "measured"
    assert out["execution_mode"] == "auto"
    assert recommend.outcome_category(_fstat(5, 5)) == "no-intervention-needed"


# --------------------------------------------------------------------------
# The measured GATE counts observed calls, not raw calls. This is the second
# half of the same finding as the rate itself: a slash-only record was kept
# out of `completion_rate`, but MEASURED_MIN_CALLS still read `calls`, so a
# skill typed 55 times and graded twice cleared a three-call bar and — above
# AUTO_MIN_RATE — reached execution_mode 'auto'.
# --------------------------------------------------------------------------

def _partly_observed(calls, unobserved, ok):
    """What skill_stats reports for a skill with both kinds of call: the rate
    is over the observed ones, `calls` counts every invocation."""
    return {"calls": calls, "ok": ok, "fail": 0, "unobserved": unobserved,
            "completion_rate": ok / (calls - unobserved),
            "last_outcome": "ok", "last_used": "2026-07-01T10:00:00Z",
            "streak": ok, "fail_streak": 0,
            "provenance": {"passive": unobserved, "invoke": ok},
            "sources": ["invoke_result", "skill_used"]}


def test_observed_calls_subtracts_the_unwitnessed_ones():
    assert recommend.observed_calls(_partly_observed(55, 53, 2)) == 2
    # A stat from before this field existed: every call counted as observed.
    assert recommend.observed_calls({"calls": 4}) == 4
    assert recommend.observed_calls({}) == 0
    assert recommend.observed_calls(None) == 0
    # Never negative, whatever a malformed row claims.
    assert recommend.observed_calls({"calls": 2, "unobserved": 9}) == 0


def test_the_measured_gate_counts_observed_calls_not_typed_ones():
    """55 calls, 2 of them graded, rate 1.0. Against raw `calls` this is
    'measured' and, at rate 1.0 >= AUTO_MIN_RATE, runs AUTOMATICALLY.

    Kills 'MEASURED_MIN_CALLS against st["calls"]'."""
    st = _partly_observed(55, 53, 2)
    out = recommend.score_skill(_skill("a", "d"), {"a": st})
    assert out["completion_rate"] == 1.0        # the rate itself is honest
    assert out["observed"] == 2
    assert out["provenance"] == "estimated"     # ...but 2 observations is thin
    assert out["execution_mode"] == "suggest"


def test_enough_observed_calls_still_reach_measured(report=None):
    """CANARY: the gate must count observed calls, not refuse to count. Three
    observed calls are MEASURED (the provenance label); the MEB's promotion
    wants four (`QUALIFIED_MIN_CALLS`, K2), so auto takes one more witnessed
    call -- two bars, on purpose."""
    st = _partly_observed(30, 27, 3)
    out = recommend.score_skill(_skill("a", "d"), {"a": st})
    assert out["observed"] == 3
    assert out["provenance"] == "measured"
    assert out["promotion"] == "PROBATION" and out["execution_mode"] == "suggest"
    st = _partly_observed(30, 26, 4)
    out = recommend.score_skill(_skill("a", "d"), {"a": st})
    assert out["observed"] == 4 and out["provenance"] == "measured"
    assert out["promotion"] == "QUALIFIED" and out["execution_mode"] == "auto"


def test_the_measured_reason_names_the_observed_denominator():
    """This string is what the projector injects into MEMORY.md as the proof
    behind 'MUST USE'. ok/calls would read '4/10 completion' -- a 40% rate --
    for a skill that completed every one of the 4 runs anybody watched.

    Kills 'render ok/calls as the completion fraction'."""
    st = _partly_observed(10, 6, 4)
    out = recommend.score_skill(_skill("a", "d"), {"a": st})
    assert "measured 4/4 completion" in out["reason"], out["reason"]
    assert "4/10" not in out["reason"]


def test_a_fully_observed_record_reads_exactly_as_before():
    """CANARY: with nothing unobserved the fraction is unchanged."""
    out = recommend.score_skill(_skill("a", "d"), {"a": _fstat(5, 5)})
    assert "measured 5/5 completion" in out["reason"]
    assert out["observed"] == 5


def test_a_typed_only_record_is_not_described_as_never_called():
    """Zero observed calls is not zero calls. Surfaced by a real run:
    `plan-devex-review`, invoked 16 times by slash command, read back as
    'never called — breadth-estimated' once the observed count became the
    denominator. That is the mirror of the inflation the gate removes.

    Kills 'observed == 0 means the skill never ran'."""
    st = _unobserved_stat(16)
    out = recommend.score_skill(_skill("a", "d"), {"a": st})
    assert "never called" not in out["reason"], out["reason"]
    assert "16 call(s) recorded" in out["reason"]
    assert "none with an observed outcome" in out["reason"]


def test_a_genuinely_uncalled_skill_still_says_never_called():
    """CANARY for the branch above: it must not swallow the real zero."""
    out = recommend.score_skill(_skill("a", "d"), {})
    assert "never called" in out["reason"]
