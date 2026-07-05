# tests/test_projector.py
from mindhive import events, projector, paths


def test_state_reflects_latest_state_set():
    events.append_event("state_set",
                        {"task": "OLD", "next_step": "x", "open_questions": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    events.append_event("state_set",
                        {"task": "OFI verdict", "verified": "trio green",
                         "next_step": "recapture 24h", "open_questions": ["perp feed?"],
                         "deferred": ["F11 multi-asset"]},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "default OFF"},
                        session_id="s", ts="2026-06-20T10:30:00Z")
    projector.project()
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "OFI verdict" in state
    assert "OLD" not in state
    assert "recapture 24h" in state
    assert "perp feed?" in state
    assert "default OFF" in state

def test_ledger_one_file_per_semantic_event():
    events.append_event("decision", {"text": "d1"}, session_id="s",
                        ts="2026-06-20T10:00:00Z", tags=["strategy"])
    events.append_event("caveat", {"text": "c1"}, session_id="s",
                        ts="2026-06-20T11:00:00Z")
    events.append_event("file_touched", {"path": "x.py"}, session_id="s",
                        ts="2026-06-20T11:30:00Z")
    projector.project()
    ldir = paths.project_dir() / "LEDGER"
    files = list(ldir.glob("*.md"))
    assert len(files) == 2  # deterministic events are NOT ledger entries

def test_projection_is_idempotent():
    events.append_event("decision", {"text": "d1"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    projector.project()
    first = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    projector.project()
    second = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert first == second

def test_index_maps_tags():
    events.append_event("decision", {"text": "d1"}, session_id="s",
                        ts="2026-06-20T10:00:00Z", tags=["ofi"])
    projector.project()
    import json
    idx = json.loads((paths.project_dir() / "index.json").read_text(encoding="utf-8"))
    assert "ofi" in idx["by_tag"]
    assert "decision" in idx["by_type"]


# --- prevention-rules delivery via STATE.md (KTD2) ---------------------------

from mindhive import incidents, reflect


def _seed_reflected_rules(n):
    """n active incidents with reflected rules; later indices are more recent."""
    for i in range(n):
        sig = f"pymodule:sig{i:02d}"
        for s, ts in (("s1", f"2026-07-01T10:{i:02d}:00Z"),
                      ("s2", f"2026-07-02T10:{i:02d}:00Z")):
            events.append_event("tool_failed",
                                {"text": "boom", "signature": sig, "action": "x"},
                                session_id=s, tags=["auto", "failure"],
                                ts=ts, project="test")
    incidents.project(project="test", threshold=2)
    d = paths.project_dir("test") / "rules" / "incidents"
    for i in range(n):
        path = d / f"{incidents._safe_name(f'pymodule:sig{i:02d}')}.md"
        path.write_text(incidents._replace_section(
            path.read_text(encoding="utf-8"), "Auto Evaluation",
            f"- Prevention rule: avoid failure {i:02d}"), encoding="utf-8")
    incidents.project(project="test", threshold=2)


def _state() -> str:
    return (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")


def test_state_appends_rules_block_when_reflected_active():
    _seed_reflected_rules(1)
    projector.project()
    state = _state()
    assert "# PREVENTION RULES" in state
    assert "avoid failure 00" in state
    # rules ride AFTER the state body
    assert state.index("_events:") < state.index("# PREVENTION RULES")


def test_state_omits_rules_header_when_inject_empty():
    events.append_event("decision", {"text": "d1"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    projector.project()
    assert "PREVENTION RULES" not in _state()


def test_state_rules_block_bounded_with_trailer():
    _seed_reflected_rules(12)
    projector.project()
    state = _state()
    assert state.count("- [pymodule:sig") == incidents.INJECT_CAP
    assert "+2 more: hive rules list" in state
    assert "avoid failure 11" in state        # most recent kept
    assert "avoid failure 00" not in state    # oldest cut


def test_rule_demoted_after_leaks_disappears_from_next_state_build():
    def fail(session, ts):
        events.append_event("tool_failed",
                            {"text": "boom", "signature": "pymodule:leaky",
                             "action": "x"},
                            session_id=session, tags=["auto", "failure"],
                            ts=ts, project="test")
    fail("s1", "2026-07-01T10:00:00Z")
    fail("s2", "2026-07-01T11:00:00Z")            # activation crossing
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-leaky.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", "- Prevention rule: stop leaking"),
                    encoding="utf-8")
    incidents.project(project="test", threshold=2)
    projector.project()
    assert "stop leaking" in _state()
    # two post-activation recurrences -> auto rule demoted on re-projection
    fail("s3", "2026-07-01T12:00:00Z")
    fail("s4", "2026-07-01T13:00:00Z")
    incidents.project(project="test", threshold=2)
    projector.project()
    assert "stop leaking" not in _state()
    assert "PREVENTION RULES" not in _state()


def test_state_carries_reflection_health_warning():
    reflect.write_marker("test", {"last_attempt": "2026-07-05T10:00:00Z",
                                  "last_success": "2026-07-01T10:00:00Z",
                                  "last_error": "empty verdict"})
    projector.project()
    state = _state()
    assert "Reflection health" in state
    assert "empty verdict" in state
