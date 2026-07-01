import json
from mindhive import __main__ as cli, events


def _write_transcript(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu_1", "name": "Bash",
             "input": {"command": "python mindhive.py"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "is_error": True, "content": ""}]},
         "toolUseResult": {"stderr": "No module named 'mindhive'", "interrupted": False}},
    ]
    (tdir / "s.jsonl").write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    return tdir


def test_failures_capture_then_list(tmp_path, capsys):
    tdir = _write_transcript(tmp_path)
    rc = cli.main(["failures", "capture", "--transcripts", str(tdir), "--project", "test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"new_failures": 1' in out

    rc = cli.main(["failures", "list", "--project", "test"])
    assert rc == 0
    listing = capsys.readouterr().out
    assert "pymodule:mindhive" in listing
    assert "candidate" in listing        # 1 occurrence < threshold
