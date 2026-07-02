"""The automatic harvest sweep (harvest.catch_up, run by the scheduled autosync)
must also capture failures — otherwise the whole Learning Loop is inert unless a
human manually runs `hive failures capture`."""
import json
from mindhive import harvest, events, incidents


def _transcript_with_failure(root):
    tdir = root / "enc"
    tdir.mkdir(parents=True)
    lines = [
        {"cwd": "/whatever", "type": "user",
         "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python x"}}]}},
        {"timestamp": "2026-06-30T10:00:00Z", "type": "user",
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": ""}]},
         "toolUseResult": {"stderr": "No module named 'zzz'", "interrupted": False}},
    ]
    (tdir / "s.jsonl").write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")


def test_catch_up_auto_captures_failures(tmp_path, monkeypatch):
    proot = tmp_path / "claude_projects"
    _transcript_with_failure(proot)
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: proot)

    harvest.catch_up(known_only=False)

    evs = [e for e in events.read_all("test") if e.type == "tool_failed"]
    assert len(evs) == 1
    assert evs[0].payload["signature"] == "pymodule:zzz"
    # the failure keeps its REAL transcript time, not capture wall-clock
    assert evs[0].ts == "2026-06-30T10:00:00Z"
    # and the incident record was projected
    rec = incidents.load("pymodule:zzz", project="test")
    assert rec.get("occurrences") == 1
