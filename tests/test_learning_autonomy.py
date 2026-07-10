# tests/test_learning_autonomy.py
"""The self-sustaining learning loop: deterministic fallback reflection,
auto-promotion to SOUL.md (+ its session injection), enforcement
auto-escalation, binding skill directives, transcript skill capture, and the
HIVE MIND aggregation of the skills telemetry endpoints.

Store isolation comes from the autouse `isolate_store` fixture (conftest)."""
import json

from mindhive import (dashboard, enforce, events, harvest, incidents, plugins,
                      projector, reflect)


def _fail(sig, session, ts, text="boom"):
    events.append_event("tool_failed",
                        {"text": text, "signature": sig, "action": "act"},
                        session_id=session, tags=["auto"], ts=ts, project="test")


def _human_rule(sig, rule):
    p = incidents._incident_dir("test") / f"{incidents._safe_name(sig)}.md"
    txt = incidents._replace_section(p.read_text(encoding="utf-8"),
                                     "Human Evaluation", rule)
    incidents._atomic_write(p, txt)
    incidents.project(project="test")


# --- reflect: deterministic fallback ------------------------------------------

def test_fallback_verdict_covers_curated_families():
    v = reflect.fallback_verdict("pscmd:head")
    assert "head" in v["prevention_rule"] and v["task_category"] == "tool_use"
    v = reflect.fallback_verdict("pymodule:numpy")
    assert "numpy" in v["prevention_rule"] and v["task_category"] == "dependency"
    assert reflect.fallback_verdict("edit:not-read")["prevention_rule"]
    assert reflect.fallback_verdict("git:worktree-locked")["task_category"] == "git_merge"


def test_fallback_verdict_never_fabricates_for_unknown_families():
    assert reflect.fallback_verdict("raw:abc1234567") == {}
    assert reflect.fallback_verdict("judgment:rejected-x-y") == {}
    assert reflect.fallback_verdict("") == {}


def test_reflect_incident_falls_back_when_runner_returns_nothing():
    _fail("pscmd:head", "A", "2026-01-01T00:00:00Z")
    incidents.project(project="test")
    res = reflect.reflect_incident("pscmd:head", project="test",
                                   runner=lambda p: "")
    assert res["reflected"] is True
    row = [r for r in incidents.summaries("test")
           if r["signature"] == "pscmd:head"][0]
    assert row["rule_source"] == "auto" and row["prevention_rule"]


def test_reflect_incident_falls_back_when_runner_raises():
    _fail("edit:string-not-found", "A", "2026-01-01T00:00:00Z")
    incidents.project(project="test")

    def bomb(prompt):
        raise RuntimeError("cli down")

    assert reflect.reflect_incident("edit:string-not-found", project="test",
                                    runner=bomb)["reflected"] is True


def test_reflect_incident_still_fails_for_raw_signature():
    _fail("raw:abc1234567", "A", "2026-01-01T00:00:00Z")
    incidents.project(project="test")
    res = reflect.reflect_incident("raw:abc1234567", project="test",
                                   runner=lambda p: "")
    assert res == {"reflected": False, "reason": "empty verdict"}


# --- incidents: auto-promotion + SOUL injection --------------------------------

def _resolved_incident(sig="pscmd:head"):
    """Two failures (threshold), an active human rule, post-activation exposure
    from another session, zero leaks — i.e. classify_resolution == resolved."""
    _fail(sig, "A", "2026-01-01T00:00:00Z")
    _fail(sig, "A", "2026-01-01T00:01:00Z")
    incidents.audit(project="test")
    incidents.project(project="test")
    _human_rule(sig, "Use the effective method instead.")
    events.append_event("command_run", {"text": "ls"}, session_id="B",
                        ts="2026-01-02T00:00:00Z", project="test")
    return sig


def test_auto_promote_graduates_resolved_rule():
    sig = _resolved_incident()
    res = incidents.auto_promote(project="test")
    assert res["promoted"] == [sig]
    assert sig in incidents.soul_path("test").read_text(encoding="utf-8")
    assert incidents.load(sig, project="test")["status"] == "promoted"
    # idempotent — a promoted rule never re-promotes
    assert incidents.auto_promote(project="test")["promoted"] == []


def test_auto_promote_skips_leaking_and_unruled():
    # unruled at threshold -> unresolved, never promoted
    _fail("pymodule:foo", "A", "2026-01-01T00:00:00Z")
    _fail("pymodule:foo", "A", "2026-01-01T00:01:00Z")
    incidents.audit(project="test")
    incidents.project(project="test")
    assert incidents.auto_promote(project="test")["promoted"] == []


def test_soul_block_empty_without_file_and_caps_content():
    assert incidents.soul_block("test") == ""
    sp = incidents.soul_path("test")
    sp.parent.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(f"- [sig{i}] rule {i}" for i in range(12))
    sp.write_text(f"# SOUL — canonical operating rules\n\n{lines}\n",
                  encoding="utf-8")
    block = incidents.soul_block("test")
    assert block.startswith("# SOUL — permanent operating rules")
    assert "rule 11" in block and "rule 0" not in block     # newest win the cap
    assert "+2 more in SOUL.md" in block


def test_compose_state_injects_soul_block():
    sp = incidents.soul_path("test")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("# SOUL — canonical operating rules\n\n- [s] Always do X\n",
                  encoding="utf-8")
    body = projector.compose_state([], project="test", suggestions="")
    assert "SOUL — permanent operating rules" in body
    assert "Always do X" in body


# --- enforce: auto-escalation ---------------------------------------------------

def _leaking_ruled_matcher(sig="tool:ripgrep-timeout"):
    _fail(sig, "A", "2026-01-01T00:00:00Z")
    _fail(sig, "A", "2026-01-01T00:01:00Z")
    incidents.audit(project="test")
    incidents.project(project="test")
    _human_rule(sig, "Always bound Grep with a path/glob/type.")
    # two post-activation recurrences from OTHER sessions = leaks >= LEAK_LIMIT
    _fail(sig, "B", "2026-01-02T00:00:00Z")
    _fail(sig, "C", "2026-01-03T00:00:00Z")
    incidents.project(project="test")
    return sig


def test_auto_escalate_enables_leaking_ruled_matcher():
    sig = _leaking_ruled_matcher()
    assert enforce.auto_escalate(project="test") == [sig]
    man = enforce.load_manifest("test")
    assert man[sig]["enabled"] is True and man[sig]["auto_enabled"] is True
    assert any(e.type == "rule_enforced" for e in events.read_all("test"))
    # second pass is a no-op
    assert enforce.auto_escalate(project="test") == []


def test_auto_escalate_never_overrides_a_human_pin():
    sig = _leaking_ruled_matcher()
    enforce.set_enabled("test", sig, False)          # explicit human decision
    assert enforce.auto_escalate(project="test") == []
    assert enforce.load_manifest("test")[sig]["enabled"] is False


def test_auto_escalate_ignores_unruled_and_unmatched_signatures():
    # matcher exists but no rule yet -> stay on the nudge rung
    _fail("tool:ripgrep-timeout", "A", "2026-01-01T00:00:00Z")
    _fail("tool:ripgrep-timeout", "B", "2026-01-02T00:00:00Z")
    incidents.audit(project="test")
    incidents.project(project="test")
    assert enforce.auto_escalate(project="test") == []


# --- projector: binding skill directives ----------------------------------------

def test_suggested_skills_block_emits_directives(monkeypatch):
    def fake(project=None, evs=None, top=None, incident_summaries=None):
        return {"recommendations": [
            {"name": "proven", "reason": "measured 5/5 completion",
             "provenance": "measured", "execution_mode": "auto",
             "try_to_measure": False},
            {"name": "candidate", "reason": "never called",
             "provenance": "estimated", "execution_mode": "suggest",
             "try_to_measure": True},
            {"name": "plain", "reason": "low match", "provenance": "estimated",
             "execution_mode": "suggest", "try_to_measure": False},
        ]}
    monkeypatch.setattr(plugins, "recommend_for", fake)
    block = projector.suggested_skills_block("test")
    assert "MUST USE: proven" in block and "Skill tool" in block
    assert "TRY & MEASURE: candidate" in block
    assert "- plain — low match (estimated)" in block


# --- harvest: skill invocation capture -------------------------------------------

def _tooluse(name, inp):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": inp}]}})


def _usermsg(text):
    return json.dumps({"type": "user", "message": {"content": text}})


def _write_transcript(dirpath, sid, lines):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{sid}.jsonl").write_text("\n".join(lines) + "\n",
                                          encoding="utf-8")


SLASH = ("<command-message>circle</command-message>"
         "<command-name>/agent-teams:parallel-debugging</command-name>"
         "<command-args>go</command-args>\n"
         "Base directory for this skill: C:\\x\\y\n\n# Parallel Debugging")


def test_harvest_captures_skill_tool_use(tmp_path):
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1",
                      [_tooluse("Skill", {"skill": "superpowers:brainstorming"})])
    harvest.harvest(tdir, "test")
    used = [e for e in events.read_all("test") if e.type == "skill_used"]
    assert len(used) == 1
    assert used[0].payload["name"] == "brainstorming"
    assert used[0].payload["plugin"] == "superpowers"
    assert used[0].payload["ok"] is True


def test_harvest_captures_slash_command_skill_invocation(tmp_path):
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg(SLASH)])
    harvest.harvest(tdir, "test")
    used = [e for e in events.read_all("test") if e.type == "skill_used"]
    assert len(used) == 1
    assert used[0].payload["name"] == "parallel-debugging"
    assert used[0].payload["plugin"] == "agent-teams"


def test_harvest_ignores_command_without_skill_body(tmp_path):
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg(
        "<command-name>/clear</command-name> just a builtin")])
    harvest.harvest(tdir, "test")
    assert [e for e in events.read_all("test") if e.type == "skill_used"] == []


def test_harvest_skill_dedups_against_hook_capture(tmp_path):
    # the live hook already recorded this invocation (different text field)
    events.append_event("skill_used",
                        {"name": "brainstorming", "plugin": "superpowers",
                         "ok": True, "text": "hook excerpt"},
                        session_id="s1", tags=["auto"], project="test")
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1",
                      [_tooluse("Skill", {"skill": "superpowers:brainstorming"})])
    harvest.harvest(tdir, "test", skip_captured=True)
    used = [e for e in events.read_all("test") if e.type == "skill_used"]
    assert len(used) == 1                       # not double-counted


def test_backfill_skills_is_idempotent_and_respects_existing(tmp_path):
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg(SLASH)])
    _write_transcript(tdir, "s2",
                      [_tooluse("Skill", {"skill": "superpowers:brainstorming"})])
    # s2's invocation was already hook-captured -> backfill must skip it
    events.append_event("skill_used", {"name": "brainstorming", "ok": True,
                                       "text": "hook"},
                        session_id="s2", tags=["auto"], project="test")
    r = harvest.backfill_skills(tdir, "test")
    assert r["new_skill_used"] == 1             # only s1's slash invocation
    r2 = harvest.backfill_skills(tdir, "test")
    assert r2["new_skill_used"] == 0            # idempotent
    used = [e for e in events.read_all("test") if e.type == "skill_used"]
    assert len(used) == 2


# --- plugins/dashboard: HIVE MIND aggregation -------------------------------------

def test_call_stats_accepts_prefetched_events():
    events.append_event("invoke_result", {"name": "circle", "ok": True,
                                          "text": "ok"},
                        session_id="s1", tags=["invoke"], project="test")
    evs = events.read_all("test")
    direct = plugins.call_stats(project="test")
    via_evs = plugins.call_stats(evs=evs)
    assert direct == via_evs
    assert via_evs["counts"] == {"circle": 1}


def test_skills_stats_endpoint_aggregates_hive_mind():
    events.append_event("skill_used", {"name": "brainstorming", "ok": True,
                                       "text": "x"},
                        session_id="s1", tags=["auto"], project="test")
    events.append_event("invoke_result", {"name": "circle", "ok": True,
                                          "text": "ok"},
                        session_id="s1", tags=["invoke"], project="test")
    import threading
    from http.server import ThreadingHTTPServer
    import urllib.request
    handler = dashboard.make_handler("tok")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        def get(path):
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}&token=tok", timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        # single-project view sees the events ...
        assert "brainstorming" in get("/api/skills/stats?project=test")["stats"]
        # ... and so does HIVE MIND (this was the '0 of N measured' bug)
        assert "brainstorming" in get("/api/skills/stats?project=*")["stats"]
        assert get("/api/skills/status?project=*")["counts"] == {"circle": 1}
        hist = get("/api/skills/history?project=*&name=circle")
        assert len(hist) == 1 and hist[0]["ok"] is True
    finally:
        server.shutdown()
