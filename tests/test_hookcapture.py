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


# --------------------------------------------------------------------------
# post_tool_use_failure — structured live failure capture
# --------------------------------------------------------------------------
# SOP tool-mcp-integration records 260 legacy tool_failed events with no
# parseable `tool` field; the live failure stage exists to fix that pattern:
# every captured failure carries machine-readable {tool, category, retryable}
# alongside the classic {text, signature, action} transcript-harvest contract.

import pytest


@pytest.mark.parametrize("text,category,retryable", [
    ("Permission denied (os error 13)", "permission_denied", False),
    ("EACCES: access denied", "permission_denied", False),
    ("ENOENT: no such file or directory", "not_found", False),
    ("command timed out after 120000ms", "timeout", True),
    ("connect ECONNREFUSED 127.0.0.1:443", "network", True),
    ("SyntaxError: invalid syntax", "syntax", False),
    ("something exploded", "error", False),
])
def test_categorize_failure(text, category, retryable):
    got = hookcapture.categorize_failure(text)
    assert got == {"category": category, "retryable": retryable}


def _failure_raw(**over):
    payload = {"session_id": "sess-f", "tool_name": "Bash",
               "tool_input": {"command": "pytest -q"},
               "error": "Permission denied (os error 13)"}
    payload.update(over)
    return json.dumps(payload)


def test_record_failure_appends_structured_tool_failed():
    res = hookcapture.record_post_tool_use_failure(_failure_raw(), project="test")
    assert res == {"appended": 1}
    (ev,) = events.read_all("test")
    assert ev.type == "tool_failed"
    assert ev.session_id == "sess-f"
    p = ev.payload
    assert p["tool"] == "Bash"
    assert p["category"] == "permission_denied"
    assert p["retryable"] is False
    assert p["action"] == "pytest -q"            # byte-identical to harvest's
    assert p["signature"]                        # root-cause key present
    assert "Permission denied" in p["text"]


def test_record_failure_action_matches_transcript_harvest_key():
    # The (session, signature, action) idempotency key must match what
    # failures.capture_dir would produce so back-harvest dedups the live event.
    from mindhive import failures
    hookcapture.record_post_tool_use_failure(_failure_raw(), project="test")
    (ev,) = events.read_all("test")
    expected = failures.action_from_tool_use(
        {"name": "Bash", "input": {"command": "pytest -q"}})
    assert ev.payload["action"] == expected


def test_record_failure_rejection_becomes_judgment_signature():
    raw = _failure_raw(error="The user doesn't want to proceed with this tool use.")
    hookcapture.record_post_tool_use_failure(raw, project="test")
    (ev,) = events.read_all("test")
    assert ev.payload["signature"].startswith("judgment:rejected:bash:")
    assert ev.payload["category"] == "rejected"
    assert "judgment" in ev.tags


def test_record_failure_skips_infra_noise():
    raw = _failure_raw(error="[mindhive-rule] scoped grep required")
    assert hookcapture.record_post_tool_use_failure(raw, project="test") == \
        {"appended": 0}
    assert events.read_all("test") == []


def test_record_failure_reads_tool_response_text():
    raw = _failure_raw(error=None,
                       tool_response={"error": "ENOENT: no such file"})
    hookcapture.record_post_tool_use_failure(raw, project="test")
    (ev,) = events.read_all("test")
    assert ev.payload["category"] == "not_found"


def test_record_failure_empty_or_textless_appends_nothing():
    assert hookcapture.record_post_tool_use_failure("", project="test") == \
        {"appended": 0}
    raw = _failure_raw(error=None)
    assert hookcapture.record_post_tool_use_failure(raw, project="test") == \
        {"appended": 0}


def test_record_failure_malformed_is_logged_not_raised():
    res = hookcapture.record_post_tool_use_failure("{not json", project="test")
    assert "error" in res
    log = paths.store_home() / "hooks_errors.log"
    assert "post_tool_use_failure" in log.read_text(encoding="utf-8")


# --- the harness's per-item task tools ----------------------------------------
# `TodoWrite` posts a whole list and this hook has always handled it. It is not
# the tool this harness sends: across all 592 transcripts on this machine
# TodoWrite appears 0 times and TaskCreate/TaskUpdate 585 times, which is why
# the auto-sync had produced 0 roadmap events in 61 namespaces while the hook
# around it appended 23,973. These tests are the fixture that reproduces that:
# they were red against a hook gated on the name TodoWrite alone.

def _task_raw(tool, inp, response, session_id="sess-2"):
    return json.dumps({"session_id": session_id, "tool_name": tool,
                       "tool_input": inp, "tool_response": response})


def test_record_taskcreate_syncs_session_roadmap():
    raw = _task_raw("TaskCreate",
                    {"subject": "wire the mirror", "description": "…",
                     "activeForm": "wiring the mirror"},
                    "Task #1 created successfully: wire the mirror")
    assert hookcapture.record_post_tool_use(raw, project="test") == \
        {"appended": 1}
    (ev,) = [e for e in events.read_all("test") if e.type == "roadmap"]
    assert ev.payload["plan_id"] == "sess-2"
    assert ev.payload["steps"] == [{"text": "wire the mirror",
                                    "status": "pending"}]
    assert "auto" in ev.tags and "roadmap" in ev.tags


def test_record_taskupdate_moves_the_step_the_harness_named():
    """Two calls, one plan: the second event has to describe the SAME plan with
    the step advanced, not mint a second one."""
    hookcapture.record_post_tool_use(
        _task_raw("TaskCreate", {"subject": "step one"},
                  "Task #1 created successfully: step one"), project="test")
    hookcapture.record_post_tool_use(
        _task_raw("TaskCreate", {"subject": "step two"},
                  "Task #2 created successfully: step two"), project="test")
    hookcapture.record_post_tool_use(
        _task_raw("TaskUpdate", {"taskId": "2", "status": "completed"},
                  "Updated task #2 status"), project="test")
    evs = [e for e in events.read_all("test") if e.type == "roadmap"]
    assert {e.payload["plan_id"] for e in evs} == {"sess-2"}
    assert evs[-1].payload["steps"] == [{"text": "step one", "status": "pending"},
                                        {"text": "step two", "status": "done"}]


def test_a_task_call_with_nothing_to_sync_appends_nothing():
    raw = _task_raw("TaskUpdate", {"taskId": "9", "status": "completed"},
                    "Updated task #9 status")
    assert hookcapture.record_post_tool_use(raw, project="test") == \
        {"appended": 0}
    assert not events.read_all("test")


def test_extract_events_still_ignores_the_task_tools():
    """Same reason TodoWrite is excluded: the pure mapping is mirrored by
    harvest.py, and replaying historical task calls from a transcript could
    only reorder the registry, never improve it."""
    for tool, inp in (("TaskCreate", {"subject": "x"}),
                      ("TaskUpdate", {"taskId": "1", "status": "completed"})):
        assert hookcapture.extract_events({"tool_name": tool,
                                           "tool_input": inp}) == []


def test_task_sync_failure_is_logged_not_raised(monkeypatch):
    from mindhive import tasktool

    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(tasktool, "record", boom)
    raw = _task_raw("TaskCreate", {"subject": "x"},
                    "Task #1 created successfully: x")
    res = hookcapture.record_post_tool_use(raw, project="test")
    assert res == {"appended": 0}
    log = paths.store_home() / "hooks_errors.log"
    assert "todo_roadmap" in log.read_text(encoding="utf-8")


def test_the_hooks_tool_list_and_tasktools_own_list_agree():
    """The two spellings must not drift: a name that exists in one list and not
    the other is precisely how the auto-sync went silent for its whole life."""
    from mindhive import tasktool
    assert set(hookcapture._TODO_TOOLS) == \
        {"TodoWrite"} | set(tasktool.TASK_TOOLS)


def test_response_text_reads_a_bare_content_block_list():
    """A tool_response is not contractually a string — transcripts store
    results as exactly this block list, and `tasktool` reads task ids through
    the same function, so the shape has to count here too."""
    assert hookcapture._response_text(
        [{"type": "text", "text": "hello"},
         {"type": "text", "text": "world"}]) == "hello world"
    assert hookcapture._response_text([{"no": "text"}]) == ""
    assert hookcapture._response_text([]) == ""
