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
        assert hookcapture.extract_events(payload) == [("file_touched", "src/foo.py")]


def test_extract_command_run_truncated_to_180():
    long_cmd = "echo " + "x" * 300
    payload = {"tool_name": "Bash", "tool_input": {"command": long_cmd}}
    out = hookcapture.extract_events(payload)
    assert out[0][0] == "command_run"
    assert len(out[0][1]) == 180


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
