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
