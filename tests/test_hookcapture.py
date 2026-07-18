# tests/test_hookcapture.py
# The PostToolUse hook used to parse the Claude payload in PowerShell and
# swallow every failure with `catch {}` — untestable and silent. hookcapture
# moves the extraction into Python so the fragile field-name coupling is
# regression-tested, and records failures to hooks_errors.log instead of
# dropping them silently.
import json
from mindhive import hookcapture, events, paths


def test_extract_file_touched_for_edit_write_notebook():
    for tool in ("Edit", "Write", "NotebookEdit"):
        payload = {"tool_name": tool, "tool_input": {"file_path": "src/foo.py"}}
        out = hookcapture.extract_events(payload)
        # No old/new strings and no patch -> stats unknown, event still logged.
        assert out == [("file_touched", {"text": "src/foo.py", "tool": tool})]


def test_extract_command_run_truncated_to_180():
    long_cmd = "echo " + "x" * 300
    payload = {"tool_name": "Bash", "tool_input": {"command": long_cmd}}
    out = hookcapture.extract_events(payload)
    assert out[0][0] == "command_run"
    assert len(out[0][1]["text"]) == 180


def test_line_stats_prefers_structured_patch():
    resp = {"structuredPatch": [
        {"lines": ["-old line", "+new line", "+added", " context"]},
        {"lines": ["-gone"]},
    ]}
    assert hookcapture.line_stats("Edit", {}, resp) == (2, 2)


def test_line_stats_falls_back_to_old_new_strings():
    inp = {"old_string": "a\nb", "new_string": "a\nb\nc"}
    assert hookcapture.line_stats("Edit", inp) == (3, 2)


def test_line_stats_write_uses_content_line_count():
    assert hookcapture.line_stats("Write", {"content": "x\ny\nz"}) == (3, 0)


def test_line_stats_unknown_when_underivable():
    assert hookcapture.line_stats("Edit", {}) == (None, None)


def test_line_stats_falls_back_on_malformed_hunk_lines():
    """A hunk whose "lines" is not a list (e.g. a string) must not be iterated
    char-by-char — the patch is unusable, so fall back to the strings."""
    resp = {"structuredPatch": [{"lines": "+not-a-list"}]}
    inp = {"old_string": "a", "new_string": "a\nb"}
    assert hookcapture.line_stats("Edit", inp, resp) == (2, 1)


def test_zero_change_edit_is_skipped():
    payload = {"tool_name": "Edit",
               "tool_input": {"file_path": "a.py", "old_string": "", "new_string": ""},
               "tool_response": {"structuredPatch": []}}
    assert hookcapture.extract_events(payload) == []


def test_zero_change_structured_patch_is_skipped():
    """A non-empty structuredPatch whose hunks net to +0 -0 (context-only
    lines) exercises the patch-counting loop, not the string fallback."""
    payload = {"tool_name": "Edit",
               "tool_input": {"file_path": "a.py"},
               "tool_response": {"structuredPatch": [
                   {"lines": [" context one", " context two"]}]}}
    assert hookcapture.extract_events(payload) == []


def test_pure_deletion_is_not_skipped():
    payload = {"tool_name": "Edit",
               "tool_input": {"file_path": "a.py",
                              "old_string": "one\ntwo", "new_string": ""}}
    out = hookcapture.extract_events(payload)
    assert out == [("file_touched",
                    {"text": "a.py", "tool": "Edit", "plus": 0, "minus": 2})]


def test_edit_payload_carries_plus_minus():
    payload = {"tool_name": "Edit",
               "tool_input": {"file_path": "a.py",
                              "old_string": "one\ntwo", "new_string": "one\ntwo\nthree"}}
    out = hookcapture.extract_events(payload)
    assert out == [("file_touched",
                    {"text": "a.py", "tool": "Edit", "plus": 3, "minus": 2})]


def test_command_payload_carries_description():
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "pytest -q",
                              "description": "Run full test suite"}}
    out = hookcapture.extract_events(payload)
    assert out == [("command_run",
                    {"text": "pytest -q", "desc": "Run full test suite"})]


def test_extract_ignores_unknown_tools_and_missing_fields():
    assert hookcapture.extract_events({"tool_name": "Read", "tool_input": {}}) == []
    assert hookcapture.extract_events({"tool_name": "Edit", "tool_input": {}}) == []
    assert hookcapture.extract_events({}) == []


def test_record_appends_events_with_session_and_auto_tag():
    raw = json.dumps({"session_id": "sess-1", "tool_name": "Edit",
                      "tool_input": {"file_path": "a/b.py"}})
    res = hookcapture.record_post_tool_use(raw, project="test")
    assert res["appended"] == 1
    evs = events.read_all("test")
    assert len(evs) == 1
    assert evs[0].type == "file_touched"
    assert evs[0].session_id == "sess-1"
    assert "auto" in evs[0].tags
    assert evs[0].payload.get("text") == "a/b.py"


def test_extract_skill_used_from_skill_tool_payload():
    """Pins the expected Skill-tool PostToolUse shape: name from
    tool_input.skill (plugin:skill namespacing split), ok from the
    tool_response error markers, text a short excerpt."""
    payload = {"tool_name": "Skill",
               "tool_input": {"skill": "compound-engineering:ce-plan", "args": ""},
               "tool_response": {"content": "planned the work", "is_error": False}}
    assert hookcapture.extract_events(payload) == [
        ("skill_used", {"name": "ce-plan", "ok": True,
                        "text": "planned the work",
                        "plugin": "compound-engineering"})]


def test_extract_skill_used_error_marks_fail():
    payload = {"tool_name": "Skill", "tool_input": {"skill": "ce-plan"},
               "tool_response": {"is_error": True, "error": "boom"}}
    [(etype, p)] = hookcapture.extract_events(payload)
    assert etype == "skill_used"
    assert p["name"] == "ce-plan"
    assert p["ok"] is False
    assert p["text"] == "boom"
    assert "plugin" not in p             # non-namespaced name -> no plugin field


def test_extract_skill_used_error_key_alone_marks_fail():
    payload = {"tool_name": "Skill", "tool_input": {"skill": "x"},
               "tool_response": {"error": "not found"}}
    [(_, p)] = hookcapture.extract_events(payload)
    assert p["ok"] is False


def test_extract_skill_missing_name_is_noop():
    assert hookcapture.extract_events({"tool_name": "Skill",
                                       "tool_input": {}}) == []
    assert hookcapture.extract_events({"tool_name": "Skill",
                                       "tool_input": {"skill": "   "}}) == []


def test_extract_skill_response_content_blocks_and_truncation():
    payload = {"tool_name": "Skill", "tool_input": {"skill": "s"},
               "tool_response": {"content": [{"type": "text", "text": "y" * 500}]}}
    [(_, p)] = hookcapture.extract_events(payload)
    assert p["ok"] is True
    assert len(p["text"]) == 200


def test_extract_skill_args_captured_and_capped():
    """The invocation ask rides the payload as optional ``args`` (the "why"
    Oversee's skill trail shows), capped like the other captured strings."""
    payload = {"tool_name": "Skill",
               "tool_input": {"skill": "p:s",
                              "args": "  why this fired  " + "x" * 500},
               "tool_response": {}}
    [(_, p)] = hookcapture.extract_events(payload)
    assert p["args"].startswith("why this fired")
    assert len(p["args"]) == hookcapture.SKILL_ARGS_MAX


def test_extract_skill_non_string_or_blank_args_omitted():
    for args in (42, None, "   "):
        payload = {"tool_name": "Skill",
                   "tool_input": {"skill": "s", "args": args},
                   "tool_response": {}}
        [(_, p)] = hookcapture.extract_events(payload)
        assert "args" not in p


def test_record_skill_payload_appends_skill_used_event():
    raw = json.dumps({"session_id": "s1", "tool_name": "Skill",
                      "tool_input": {"skill": "superpowers:brainstorming"},
                      "tool_response": {"is_error": False}})
    res = hookcapture.record_post_tool_use(raw, project="test")
    assert res["appended"] == 1
    ev = events.read_all("test")[0]
    assert ev.type == "skill_used"
    assert ev.payload["name"] == "brainstorming"
    assert ev.payload["plugin"] == "superpowers"
    assert ev.payload["ok"] is True
    assert "auto" in ev.tags


def test_malformed_payload_is_logged_not_raised():
    res = hookcapture.record_post_tool_use("this is not json{", project="test")
    assert res.get("error")                       # returned, not raised
    log = paths.store_home() / "hooks_errors.log"
    assert log.exists()
    assert "post_tool_use" in log.read_text(encoding="utf-8")


def test_empty_input_is_noop():
    assert hookcapture.record_post_tool_use("", project="test") == {"appended": 0}
    assert hookcapture.record_post_tool_use("   ", project="test") == {"appended": 0}


def test_record_todowrite_syncs_session_roadmap():
    todos = [{"content": "step one", "status": "in_progress"},
             {"content": "step two", "status": "pending"}]
    raw = json.dumps({"session_id": "sess-2", "tool_name": "TodoWrite",
                      "tool_input": {"todos": todos}})
    res = hookcapture.record_post_tool_use(raw, project="test")
    assert res == {"appended": 1}
    [ev] = events.read_all("test")
    assert ev.type == "roadmap"
    assert ev.session_id == "sess-2"
    assert ev.payload["plan_id"] == "sess-2"
    assert [s["status"] for s in ev.payload["steps"]] == ["active", "pending"]
    assert "auto" in ev.tags and "roadmap" in ev.tags


def test_record_todowrite_with_empty_todos_appends_nothing():
    raw = json.dumps({"session_id": "sess-2", "tool_name": "TodoWrite",
                      "tool_input": {"todos": []}})
    assert hookcapture.record_post_tool_use(raw, project="test") == {"appended": 0}
    assert events.read_all("test") == []


def test_extract_events_still_ignores_todowrite():
    # The pure mapping stays TodoWrite-free on purpose: harvest.py mirrors it,
    # and transcript replay must never rewrite the roadmap registry.
    assert hookcapture.extract_events(
        {"tool_name": "TodoWrite", "tool_input": {"todos": [
            {"content": "x", "status": "pending"}]}}) == []


def test_record_todowrite_sync_failure_is_logged_not_raised(monkeypatch):
    from mindhive import roadmap

    def boom(*a, **k):
        raise RuntimeError("sync exploded")
    monkeypatch.setattr(roadmap, "sync_from_todos", boom)
    raw = json.dumps({"session_id": "sess-2", "tool_name": "TodoWrite",
                      "tool_input": {"todos": [{"content": "x",
                                                "status": "pending"}]}})
    res = hookcapture.record_post_tool_use(raw, project="test")
    assert res == {"appended": 0}                 # capture contract intact
    log = paths.store_home() / "hooks_errors.log"
    assert "todo_roadmap" in log.read_text(encoding="utf-8")
