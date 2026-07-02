from mindhive import reflect


def test_build_prompt_contains_error_and_demands_json():
    p = reflect.build_prompt(signature="pymodule:mindhive",
                             error_excerpt="No module named 'mindhive'",
                             actions=["python mindhive.py state"])
    assert "No module named 'mindhive'" in p
    assert "python mindhive.py state" in p
    assert "JSON" in p
    assert "prevention_rule" in p


def test_parse_reflection_extracts_json_even_with_fences():
    raw = ('Sure!\n```json\n{"root_cause": "wrong cwd", "lesson": "run as module", '
           '"prevention_rule": "use -m from repo root", "task_category": "dependency"}\n```')
    out = reflect.parse_reflection(raw)
    assert out["prevention_rule"] == "use -m from repo root"
    assert out["task_category"] == "dependency"


def test_parse_reflection_returns_empty_on_garbage():
    assert reflect.parse_reflection("no json here") == {}
    assert reflect.parse_reflection("") == {}


def test_result_text_empty_on_api_error_envelope():
    # Real 401 seen in the smoke test: is_error true, result is an error string.
    env = '{"is_error": true, "result": "Failed to authenticate. API Error: 401"}'
    assert reflect._result_text(env) == ""


def test_result_text_returns_result_on_success():
    env = '{"is_error": false, "result": "{\\"prevention_rule\\": \\"x\\"}"}'
    assert reflect._result_text(env) == '{"prevention_rule": "x"}'


def test_result_text_empty_on_garbage():
    assert reflect._result_text("not json") == ""
    assert reflect._result_text("") == ""


from mindhive import events, incidents, paths


def _seed_active_incident():
    for s in ("s1", "s2"):
        events.append_event("tool_failed",
                            {"text": "No module named 'mindhive'",
                             "signature": "pymodule:mindhive", "action": "python x"},
                            session_id=s, tags=["auto", "failure"], project="test")
    incidents.project(project="test", threshold=2)


def test_reflect_incident_writes_auto_and_sets_active(tmp_path):
    _seed_active_incident()

    def fake_runner(prompt, timeout_s=120):
        return ('{"root_cause": "wrong cwd", "lesson": "run as module", '
                '"prevention_rule": "use -m from repo root", "task_category": "dependency"}')

    res = reflect.reflect_incident("pymodule:mindhive", project="test", runner=fake_runner)
    assert res["reflected"] is True
    body = (paths.project_dir("test") / "rules" / "incidents"
            / "pymodule-mindhive.md").read_text(encoding="utf-8")
    assert "use -m from repo root" in body
    assert "> use -m from repo root" in body               # Active == Auto (no human)
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["rule_source"] == "auto"


def test_reflect_incident_noop_on_empty_verdict(tmp_path):
    _seed_active_incident()
    res = reflect.reflect_incident("pymodule:mindhive", project="test",
                                   runner=lambda p, timeout_s=120: "garbage")
    assert res["reflected"] is False
