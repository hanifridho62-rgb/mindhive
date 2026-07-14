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


# Envelope parsing (is_error / garbage handling) now lives in claude_cli and is
# covered by tests/test_claude_cli.py — reflect just delegates to it.

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
    # raw:* has no deterministic fallback (unknown root cause), so a garbage
    # model response still yields no verdict — curated families now fall back
    # instead (see test_learning_autonomy.py).
    for s in ("s1", "s2"):
        events.append_event("tool_failed",
                            {"text": "weird failure", "signature": "raw:feedbeef01",
                             "action": "x"},
                            session_id=s, tags=["auto", "failure"], project="test")
    incidents.project(project="test", threshold=2)
    res = reflect.reflect_incident("raw:feedbeef01", project="test",
                                   runner=lambda p, timeout_s=120: "garbage")
    assert res["reflected"] is False


# --- reflect_batch: bounded, cooldown-gated auto-reflection (KTD3) -----------

_GOOD = ('{"root_cause": "rc", "lesson": "le", '
         '"prevention_rule": "always pin the venv", "task_category": "tool_use"}')


def _seed_unreflected(n):
    # raw:* signatures on purpose: no deterministic fallback, so a failing
    # runner really does leave the incident unreflected (what the cooldown,
    # rollback, and error-marker tests below exercise).
    for i in range(n):
        sig = f"raw:batch{i}"
        for s in ("s1", "s2"):
            events.append_event("tool_failed",
                                {"text": "boom", "signature": sig, "action": "x"},
                                session_id=s, tags=["auto", "failure"], project="test")
    incidents.project(project="test", threshold=2)


def test_reflect_batch_limits_and_marker_written_before_first_call():
    _seed_unreflected(5)
    marker_seen = []

    def stub(prompt, timeout_s=120):
        marker_seen.append(reflect._marker_path("test").exists())
        return _GOOD

    res = reflect.reflect_batch(project="test", limit=3, cooldown_hours=6,
                                runner=stub)
    assert res["reflected"] == 3 and res["attempted"] == 3
    assert len(marker_seen) == 3
    assert marker_seen[0] is True          # cooldown consumed BEFORE first call
    m = reflect.read_marker("test")
    assert m["last_success"] and m["last_error"] == ""


def test_reflect_batch_second_call_inside_cooldown_is_noop():
    _seed_unreflected(2)
    calls = {"n": 0}

    def stub(prompt, timeout_s=120):
        calls["n"] += 1
        return _GOOD

    reflect.reflect_batch(project="test", limit=1, cooldown_hours=6, runner=stub)
    assert calls["n"] == 1
    res = reflect.reflect_batch(project="test", limit=1, cooldown_hours=6,
                                runner=stub)
    assert res["skipped"] == "cooldown"
    assert calls["n"] == 1                 # no runner calls inside the window


def test_reflect_batch_failed_batch_rolls_cooldown_back():
    _seed_unreflected(2)
    res = reflect.reflect_batch(project="test", limit=3, cooldown_hours=6,
                                runner=lambda p, timeout_s=120: "garbage")
    assert res["reflected"] == 0 and res["attempted"] == 2
    m = reflect.read_marker("test")
    assert not m.get("cooldown_started")   # window NOT consumed by failure
    assert m["last_attempt"] and m["last_error"] == "empty verdict"
    # the very next invocation reaches the runner again
    calls = {"n": 0}

    def stub(prompt, timeout_s=120):
        calls["n"] += 1
        return "garbage"

    reflect.reflect_batch(project="test", limit=1, cooldown_hours=6, runner=stub)
    assert calls["n"] == 1


def test_reflect_batch_runner_exception_recorded_without_raising():
    _seed_unreflected(1)

    def boom(prompt, timeout_s=120):
        raise RuntimeError("auth expired")

    res = reflect.reflect_batch(project="test", limit=3, cooldown_hours=6,
                                runner=boom)
    assert res["reflected"] == 0
    # the marker carries the exception's MESSAGE (the real cause), not the
    # class name — "RuntimeError" tells the health banner nothing actionable
    assert reflect.read_marker("test")["last_error"] == "auth expired"


def test_reflect_batch_success_refreshes_state_projection():
    _seed_unreflected(1)
    reflect.reflect_batch(project="test", limit=3, cooldown_hours=6,
                          runner=lambda p, timeout_s=120: _GOOD)
    state = (paths.project_dir("test") / "STATE.md").read_text(encoding="utf-8")
    assert "PREVENTION RULES" in state
    assert "always pin the venv" in state


def test_reflect_batch_noop_when_nothing_active_unreflected():
    calls = {"n": 0}

    def stub(prompt, timeout_s=120):
        calls["n"] += 1
        return _GOOD

    res = reflect.reflect_batch(project="test", limit=3, cooldown_hours=6,
                                runner=stub)
    assert res["skipped"] == "nothing to reflect"
    assert calls["n"] == 0
    assert reflect.read_marker("test") == {}   # nothing-to-do writes no marker


def test_reflect_batch_reroutes_red_leaking_rules_in_same_pass():
    # one unreflected incident + one whose auto rule leaked: the same bounded
    # pass reflects the first and reroutes the second (active resolution).
    _seed_unreflected(1)                            # raw:batch0, no rule yet
    sig = "raw:leaky"
    for s, ts in (("s1", "2026-07-01T10:00:00Z"), ("s2", "2026-07-01T11:00:00Z")):
        events.append_event("tool_failed",
                            {"text": "boom", "signature": sig, "action": "x"},
                            session_id=s, tags=["auto", "failure"], ts=ts,
                            project="test")
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    path = paths.project_dir("test") / "rules" / "incidents" / "raw-leaky.md"
    path.write_text(incidents._replace_section(
        path.read_text(encoding="utf-8"), "Auto Evaluation",
        "- Prevention rule: the failed rule"), encoding="utf-8")
    incidents.project(project="test", threshold=2)
    events.append_event("tool_failed",
                        {"text": "boom", "signature": sig, "action": "x"},
                        session_id="s3", tags=["auto", "failure"],
                        ts="2026-07-01T12:00:00Z", project="test")   # leak

    def stub(prompt, timeout_s=120):
        if "FAILED RULES" in prompt:                # the reroute prompt
            return ('{"routes": [{"prevention_rule": "alt route", '
                    '"rationale": "different mechanism", "confidence": 0.9}]}')
        return _GOOD

    res = reflect.reflect_batch(project="test", limit=3, cooldown_hours=6,
                                runner=stub)
    assert res["reflected"] == 1 and res["rerouted"] == 1
    assert res["attempted"] == 2
    rec = {r["signature"]: r for r in incidents.summaries(project="test")}
    assert rec["raw:leaky"]["active_rule"] == "alt route"
    m = reflect.read_marker("test")
    assert m["last_success"] and m["last_error"] == ""


def test_write_marker_is_atomic_and_always_valid_json():
    import json
    reflect.write_marker("test", {"last_attempt": "2026-07-05T10:00:00Z"})
    p = reflect._marker_path("test")
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "last_attempt": "2026-07-05T10:00:00Z"}
    assert not p.with_name(p.name + ".tmp").exists()   # temp cleaned by os.replace


def test_reflect_batch_rollback_cas_skips_when_marker_replaced_mid_batch():
    # Another process starts its own batch mid-flight and writes a NEW
    # cooldown_started; our failing batch must NOT roll that newer window back.
    _seed_unreflected(1)
    foreign = "2099-01-01T00:00:00Z"

    def stub(prompt, timeout_s=120):
        m = reflect.read_marker("test")
        m["cooldown_started"] = foreign        # foreign batch replaces the window
        reflect.write_marker("test", m)
        return "garbage"                       # this batch reflects zero

    res = reflect.reflect_batch(project="test", limit=3, cooldown_hours=6,
                                runner=stub)
    assert res["reflected"] == 0
    m = reflect.read_marker("test")
    assert m["cooldown_started"] == foreign    # CAS: rollback skipped
    assert m["last_error"] == "empty verdict"


# --- LIVE_LOCK gate inside reflect_batch (whole-token namespace match) -------

def test_reflect_batch_live_lock_exact_token_skips_without_touching_marker():
    _seed_unreflected(1)
    (paths.store_home() / "LIVE_LOCK").write_text(
        "V17 live: test, hands off\n", encoding="utf-8")
    calls = {"n": 0}

    def stub(prompt, timeout_s=120):
        calls["n"] += 1
        return _GOOD

    res = reflect.reflect_batch(project="test", limit=3, cooldown_hours=6,
                                runner=stub)
    assert res == {"reflected": 0, "attempted": 0, "skipped": "live_lock"}
    assert calls["n"] == 0
    assert reflect.read_marker("test") == {}   # marker never touched


def test_reflect_batch_live_lock_substring_of_longer_token_not_skipped():
    _seed_unreflected(1)
    (paths.store_home() / "LIVE_LOCK").write_text("test-extended v17\n",
                                                  encoding="utf-8")
    res = reflect.reflect_batch(project="test", limit=3, cooldown_hours=6,
                                runner=lambda p, timeout_s=120: _GOOD)
    assert res["reflected"] == 1               # "test" != token "test-extended"


def test_reflect_batch_unreadable_live_lock_proceeds():
    _seed_unreflected(1)
    (paths.store_home() / "LIVE_LOCK").mkdir()  # read_text raises OSError
    res = reflect.reflect_batch(project="test", limit=3, cooldown_hours=6,
                                runner=lambda p, timeout_s=120: _GOOD)
    assert res["reflected"] == 1


def test_health_note_warns_when_success_lags_attempt():
    reflect.write_marker("test", {"last_attempt": "2026-07-05T10:00:00Z",
                                  "last_success": "2026-07-01T10:00:00Z",
                                  "last_error": "empty verdict"})
    note = reflect.health_note("test")
    assert "Reflection health" in note and "empty verdict" in note


def test_health_note_empty_when_healthy_or_untried():
    assert reflect.health_note("test") == ""   # no marker yet
    reflect.write_marker("test", {"last_attempt": "2026-07-05T10:00:00Z",
                                  "last_success": "2026-07-05T10:01:00Z",
                                  "last_error": ""})
    assert reflect.health_note("test") == ""


def test_run_headless_pins_headless_env(monkeypatch):
    """The runner subprocess env sets MINDHIVE_HEADLESS=1 so hooks/*.ps1 exit
    immediately in spawned headless sessions (no recursion)."""
    from mindhive import claude_cli
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "{}"

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    claude_cli.run_headless("hello")
    assert captured["env"] is not None
    assert captured["env"]["MINDHIVE_HEADLESS"] == "1"


# --- honest failure causes: the marker must carry the REAL error --------------

def test_claude_runner_raises_with_envelope_error(monkeypatch):
    monkeypatch.setattr(reflect.claude_cli, "run_headless",
                        lambda p, timeout_s=120: {"exit": 1, "stdout":
                            '{"is_error": true, "api_error_status": 429, '
                            '"result": "weekly limit hit"}'})
    import pytest
    with pytest.raises(RuntimeError, match="429: weekly limit hit"):
        reflect._claude_runner("prompt")


def test_claude_runner_raises_on_missing_cli(monkeypatch):
    monkeypatch.setattr(reflect.claude_cli, "run_headless",
                        lambda p, timeout_s=120: {"exit": 127, "stdout": ""})
    import pytest
    with pytest.raises(RuntimeError, match="not found"):
        reflect._claude_runner("prompt")


def test_reflect_incident_records_real_cause_not_class_name():
    """A raising runner's message must reach the health marker verbatim —
    'empty verdict' hid a 429 quota exhaustion behind an auth-shaped banner."""
    _seed_unreflected(1)

    def quota_runner(prompt, timeout_s=120):
        raise RuntimeError("429: weekly limit hit")

    res = reflect.reflect_batch(project="test", limit=1, cooldown_hours=6,
                                runner=quota_runner)
    assert res["reflected"] == 0
    assert reflect.read_marker("test")["last_error"] == "429: weekly limit hit"
