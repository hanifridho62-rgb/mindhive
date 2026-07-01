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
