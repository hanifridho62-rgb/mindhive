from mindhive import failures


def _tool_result_line(is_error, stderr="", interrupted=False, content_text=""):
    return {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "is_error": is_error, "content": content_text},
        ]},
        "toolUseResult": {"stdout": "", "stderr": stderr, "interrupted": interrupted},
    }


def test_extracts_failure_with_signature_from_stderr():
    line = _tool_result_line(True, stderr="ModuleNotFoundError: No module named 'mindhive'")
    out = failures.extract_failures(line)
    assert len(out) == 1
    assert out[0]["tool_use_id"] == "tu_1"
    assert out[0]["signature"] == "pymodule:mindhive"
    assert "mindhive" in out[0]["error_excerpt"]


def test_success_and_interrupted_are_not_failures():
    assert failures.extract_failures(_tool_result_line(False, stderr="")) == []
    assert failures.extract_failures(
        _tool_result_line(True, stderr="boom", interrupted=True)) == []


def test_uses_block_content_when_stderr_empty():
    line = _tool_result_line(True, stderr="",
                             content_text="ModuleNotFoundError: No module named 'pypdf'")
    out = failures.extract_failures(line)
    assert out[0]["signature"] == "pymodule:pypdf"


def test_non_tool_result_lines_yield_nothing():
    assert failures.extract_failures({"type": "assistant", "message": {"content": []}}) == []
    assert failures.extract_failures({}) == []


import json
from mindhive import events


def _assistant_tool_use(tool_use_id, name, cmd=None, file_path=None):
    inp = {}
    if cmd is not None:
        inp["command"] = cmd
    if file_path is not None:
        inp["file_path"] = file_path
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": inp}]}}


def test_capture_writes_tool_failed_event_with_action(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    lines = [
        _assistant_tool_use("tu_1", "Bash", cmd="python mindhive.py state"),
        _tool_result_line(True, stderr="ModuleNotFoundError: No module named 'mindhive'"),
    ]
    lines[1]["message"]["content"][0]["tool_use_id"] = "tu_1"
    (tdir / "sess.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines), encoding="utf-8")

    res = failures.capture_dir(str(tdir), project="test")
    assert res["new_failures"] == 1
    evs = [e for e in events.read_all("test") if e.type == "tool_failed"]
    assert len(evs) == 1
    assert evs[0].payload["signature"] == "pymodule:mindhive"
    assert evs[0].payload["action"] == "python mindhive.py state"
    assert "failure" in evs[0].tags


def test_capture_is_idempotent(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    lines = [_assistant_tool_use("tu_1", "Bash", cmd="x"),
             _tool_result_line(True, stderr="No module named 'tools'")]
    lines[1]["message"]["content"][0]["tool_use_id"] = "tu_1"
    (tdir / "sess.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    failures.capture_dir(str(tdir), project="test")
    res2 = failures.capture_dir(str(tdir), project="test")
    assert res2["new_failures"] == 0
    evs = [e for e in events.read_all("test") if e.type == "tool_failed"]
    assert len(evs) == 1
