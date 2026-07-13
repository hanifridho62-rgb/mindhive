# tests/test_resolve.py
"""Active rule resolution: red (unresolved) lessons get alternative routes."""
import json

from mindhive import events, incidents, paths, reflect, resolve


# --- pure helpers -------------------------------------------------------------

def test_build_route_prompt_lists_failed_rules_and_demands_json():
    p = resolve.build_route_prompt(
        "pymodule:mindhive", "No module named 'mindhive'",
        ["always activate the venv"], 2)
    assert "always activate the venv" in p
    assert "No module named 'mindhive'" in p
    assert "routes" in p and "prevention_rule" in p
    assert "RECURRENCES DESPITE THE RULE: 2" in p


def test_parse_routes_tolerates_fences_and_filters_garbage():
    raw = ('Sure!\n```json\n{"routes": ['
           '{"prevention_rule": "route A", "rationale": "r", "confidence": 0.9},'
           '{"prevention_rule": "", "rationale": "empty rule dropped"},'
           '"not a dict",'
           '{"prevention_rule": "route B", "confidence": 0.4}]}\n```')
    routes = resolve.parse_routes(raw)
    assert [r["prevention_rule"] for r in routes] == ["route A", "route B"]
    assert resolve.parse_routes("no json") == []
    assert resolve.parse_routes('{"routes": "not a list"}') == []


def test_choose_route_rejects_failed_rules_and_ranks_by_confidence():
    routes = [
        {"prevention_rule": "Always Activate the VENV.", "confidence": 0.95},
        {"prevention_rule": "pin python via py -3.12", "confidence": 0.6},
        {"prevention_rule": "run through the launcher", "confidence": 0.8},
    ]
    # the top route is a trivial rephrasing of the failed rule -> excluded
    best = resolve.choose_route(routes, ["always activate the venv"])
    assert best["prevention_rule"] == "run through the launcher"
    # nothing distinct -> empty dict, never fabricate
    assert resolve.choose_route(
        [{"prevention_rule": "always activate the venv!", "confidence": 1.0}],
        ["Always activate the venv"]) == {}
    # non-numeric confidence is treated as 0, not a crash
    assert resolve.choose_route(
        [{"prevention_rule": "a", "confidence": "high"},
         {"prevention_rule": "b", "confidence": 0.1}], [])["prevention_rule"] == "b"


# --- seeding helpers ----------------------------------------------------------

def _fail(sig, sess, ts):
    events.append_event("tool_failed",
                        {"text": f"err {sig}", "signature": sig, "action": "x"},
                        session_id=sess, tags=["auto", "failure"], ts=ts,
                        project="test")


def _write_rule(sig, rule):
    path = (paths.project_dir("test") / "rules" / "incidents"
            / f"{incidents._safe_name(sig)}.md")
    path.write_text(incidents._replace_section(
        path.read_text(encoding="utf-8"), "Auto Evaluation",
        f"- Root cause: rc\n- Lesson: le\n- Prevention rule: {rule}\n"
        "- Category: tool_use"), encoding="utf-8")


def _seed_leaking(sig="raw:leaky", rule="the failed rule"):
    """Active incident with an auto rule that leaked once -> unresolved."""
    _fail(sig, "s1", "2026-07-01T10:00:00Z")
    _fail(sig, "s2", "2026-07-01T11:00:00Z")       # activation crossing
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    _write_rule(sig, rule)
    incidents.project(project="test", threshold=2)
    _fail(sig, "s3", "2026-07-01T12:00:00Z")       # leak despite the rule
    incidents.project(project="test", threshold=2)
    return sig


_ROUTES = json.dumps({"routes": [
    {"prevention_rule": "take the alternative route", "rationale": "different "
     "mechanism entirely", "confidence": 0.9},
    {"prevention_rule": "the failed rule", "rationale": "same as before",
     "confidence": 0.99},
]})


# --- red_summaries: the automatic red filter ----------------------------------

def test_red_summaries_filters_unresolved_only():
    sig = _seed_leaking()
    _fail("raw:fresh", "s1", "2026-07-02T10:00:00Z")   # candidate -> watching
    incidents.project(project="test", threshold=2)
    reds = resolve.red_summaries("test")
    assert [r["signature"] for r in reds] == [sig]
    assert reds[0]["leaks"] == 1 and reds[0]["resolution"] == "unresolved"


# --- reroute_incident ---------------------------------------------------------

def test_reroute_deploys_best_distinct_route_and_reanchors():
    sig = _seed_leaking()
    assert incidents.classify_resolution(
        incidents.summaries("test")[0],
        incidents.signal_stats("test")[sig]) == "unresolved"

    res = resolve.reroute_incident(sig, project="test",
                                   runner=lambda p, timeout_s=120: _ROUTES)
    assert res == {"resolved": True, "mode": "rerouted", "attempt": 1}
    rec = [r for r in incidents.summaries("test") if r["signature"] == sig][0]
    # the 0.99 route equals the failed rule -> the distinct 0.9 route wins
    assert rec["active_rule"] == "take the alternative route"
    assert rec["status"] == "active"

    st = incidents.signal_stats("test")[sig]
    assert st["leaks"] == 0 and st["reroutes"] == 1 and st["last_reroute"]
    assert st["total_leaks"] == 1                  # cumulative view survives
    # fresh route, not yet tested -> watching (red is gone)
    assert incidents.classify_resolution(rec, st) == "watching"
    # clean activity from a new session strictly after the reroute (the event
    # is stamped at the wall clock, so use a far-future ts) -> proven -> green
    events.append_event("command_run", {"text": "ok"}, session_id="s9",
                        ts="2027-06-01T00:00:00Z", project="test")
    st = incidents.signal_stats("test")[sig]
    assert st["exposure"] is True
    assert incidents.classify_resolution(rec, st) == "resolved"


def test_reroute_event_records_old_text_and_routes():
    sig = _seed_leaking()                          # active rule: "the failed rule"
    resolve.reroute_incident(sig, project="test",
                             runner=lambda p, timeout_s=120: _ROUTES)
    ev = [e for e in events.read_all("test") if e.type == "rule_rerouted"][0]
    assert ev.payload["signature"] == sig
    assert ev.payload["old_text"] == "the failed rule"
    assert ev.payload["text"] == "take the alternative route"
    assert ev.payload["attempt"] == 1 and len(ev.payload["routes"]) == 2


def test_reroute_skips_when_no_distinct_route_offered():
    sig = _seed_leaking(rule="the failed rule")
    same = json.dumps({"routes": [
        {"prevention_rule": "The FAILED rule", "confidence": 1.0}]})
    res = resolve.reroute_incident(sig, project="test",
                                   runner=lambda p, timeout_s=120: same)
    assert res["resolved"] is False and res["mode"] == "failed"
    assert not [e for e in events.read_all("test") if e.type == "rule_rerouted"]


def test_reroute_dead_runner_uses_distinct_fallback_only():
    # curated family: fallback exists and differs from the failed rule
    sig = _seed_leaking(sig="edit:not-read", rule="some rule that failed")
    res = resolve.reroute_incident(sig, project="test",
                                   runner=lambda p, timeout_s=120: "garbage")
    assert res["resolved"] is True
    rec = [r for r in incidents.summaries("test") if r["signature"] == sig][0]
    assert "Read" in rec["active_rule"]

    # same dead runner, but the active rule IS the fallback -> nothing distinct
    sig2 = _seed_leaking(
        sig="tool:ripgrep-timeout",
        rule=reflect.fallback_verdict("tool:ripgrep-timeout")["prevention_rule"])
    res2 = resolve.reroute_incident(sig2, project="test",
                                    runner=lambda p, timeout_s=120: "garbage")
    assert res2["resolved"] is False
    assert res2["reason"] == "no distinct route"


def test_reroute_attempts_capped_and_flag_surfaces():
    sig = _seed_leaking()
    routes = [f"route {i}" for i in range(1, incidents.MAX_REROUTES + 2)]

    def runner_for(rule):
        return lambda p, timeout_s=120: json.dumps(
            {"routes": [{"prevention_rule": rule, "confidence": 0.9}]})

    for i, rule in enumerate(routes[:incidents.MAX_REROUTES], start=1):
        res = resolve.reroute_incident(sig, project="test",
                                       runner=runner_for(rule))
        assert res == {"resolved": True, "mode": "rerouted", "attempt": i}
        # each route leaks; far-future ts so it lands after the reroute event
        # (which is stamped at the real wall clock)
        _fail(sig, f"leak{i}", f"2027-0{i}-01T10:00:00Z")

    res = resolve.reroute_incident(sig, project="test",
                                   runner=runner_for(routes[-1]))
    assert res["resolved"] is False and "exhausted" in res["reason"]
    incidents.project(project="test", threshold=2)
    assert incidents.load(sig, project="test")["flag"] == "reroute_exhausted"


def test_resolve_incident_dispatch_reflect_and_human_skip():
    # no rule yet -> delegates to reflection
    _fail("raw:norule", "s1", "2026-07-01T10:00:00Z")
    _fail("raw:norule", "s2", "2026-07-01T11:00:00Z")
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    good = ('{"root_cause": "rc", "lesson": "le", "prevention_rule": '
            '"fresh rule", "task_category": "tool_use"}')
    res = resolve.resolve_incident("raw:norule", project="test",
                                   runner=lambda p, timeout_s=120: good)
    assert res["resolved"] is True and res["mode"] == "reflected"

    # human rule is authoritative -> never rerouted
    sig = _seed_leaking(sig="raw:humanruled")
    path = (paths.project_dir("test") / "rules" / "incidents"
            / f"{incidents._safe_name(sig)}.md")
    path.write_text(incidents._replace_section(
        path.read_text(encoding="utf-8"), "Human Evaluation", "human says so"),
        encoding="utf-8")
    incidents.project(project="test", threshold=2)
    res = resolve.resolve_incident(sig, project="test",
                                   runner=lambda p, timeout_s=120: _ROUTES)
    assert res["resolved"] is False and res["mode"] == "skipped"
    assert not [e for e in events.read_all("test") if e.type == "rule_rerouted"
                and e.payload.get("signature") == sig]


def test_reroute_undemotes_auto_demoted_rule():
    # two leaks auto-demote the rule (out of injection, red forever before this
    # feature); a reroute re-anchors leaks -> active again, back in injection.
    sig = _seed_leaking()
    _fail(sig, "s4", "2026-07-01T13:00:00Z")
    incidents.project(project="test", threshold=2)
    assert incidents.load(sig, project="test")["status"] == "demoted"
    res = resolve.reroute_incident(sig, project="test",
                                   runner=lambda p, timeout_s=120: _ROUTES)
    assert res["resolved"] is True
    assert incidents.load(sig, project="test")["status"] == "active"
    assert "take the alternative route" in incidents.inject(project="test")


# --- resolve_batch ------------------------------------------------------------

def test_resolve_batch_reroutes_and_reflects_red_only():
    leaky = _seed_leaking()
    _fail("raw:norule", "sa", "2026-07-01T10:00:00Z")
    _fail("raw:norule", "sb", "2026-07-01T11:00:00Z")
    _fail("raw:green", "sc", "2026-07-01T10:00:00Z")   # candidate, not red

    def runner(prompt, timeout_s=120):
        if "FAILED RULES" in prompt:
            return _ROUTES
        return ('{"root_cause": "rc", "lesson": "le", "prevention_rule": '
                '"fresh rule", "task_category": "tool_use"}')

    res = resolve.resolve_batch(project="test", limit=5, runner=runner)
    assert res["rerouted"] == 1 and res["reflected"] == 1
    assert res["failed"] == 0 and res["pending"] == 0
    rec = {r["signature"]: r for r in incidents.summaries("test")}
    assert rec[leaky]["active_rule"] == "take the alternative route"
    assert rec["raw:norule"]["active_rule"] == "fresh rule"


def test_resolve_batch_respects_limit_and_reports_pending():
    for i in range(3):
        _seed_leaking(sig=f"raw:leak{i}")
    calls = {"n": 0}

    def runner(prompt, timeout_s=120):
        calls["n"] += 1
        return _ROUTES

    res = resolve.resolve_batch(project="test", limit=1, runner=runner)
    assert calls["n"] == 1
    assert res["rerouted"] == 1 and res["red"] == 3 and res["pending"] == 2


def test_resolve_batch_noop_when_nothing_red():
    res = resolve.resolve_batch(project="test", limit=3,
                                runner=lambda p, timeout_s=120: _ROUTES)
    assert res["rerouted"] == 0 and res["reflected"] == 0
    assert res["attempted"] == 0 and res["red"] == 0


# --- injection marks the rerouted route as mandatory --------------------------

def test_inject_marks_rerouted_rules():
    sig = _seed_leaking()
    resolve.reroute_incident(sig, project="test",
                             runner=lambda p, timeout_s=120: _ROUTES)
    block = incidents.inject(project="test")
    assert "take the alternative route" in block
    assert "rerouted ×1" in block and "follow this route exactly" in block
