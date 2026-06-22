# tests/test_harvest.py
import json
from mindhive import harvest, events


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def _tooluse(name, inp):
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})


def _write_transcript(dirpath, sid, lines):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_harvest_captures_edits_and_commands(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"}),
                                   _tooluse("Bash", {"command": "ls"})])
    r = harvest.harvest(tdir, "test")
    assert r["new_events"] == 2
    types = [e.type for e in events.read_all("test")]
    assert types.count("file_touched") == 1 and types.count("command_run") == 1


def test_harvest_is_idempotent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"})])
    harvest.harvest(tdir, "test")
    r2 = harvest.harvest(tdir, "test")            # cursor at end -> nothing new
    assert r2["new_events"] == 0
    assert len(events.read_all("test")) == 1


def test_skip_captured_avoids_double_counting(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # simulate a LIVE-HOOK capture of session s1 (no 'harvest' tag)
    events.append_event("file_touched", {"text": "a.py"}, session_id="s1", tags=["auto"], project="test")
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"})])   # same action in transcript
    _write_transcript(tdir, "s2", [_tooluse("Bash", {"command": "ls"})])
    r = harvest.harvest(tdir, "test", skip_captured=True)
    assert r["skipped_captured"] == 1
    s1 = [e for e in events.read_all("test") if e.session_id == "s1"]
    assert len(s1) == 1 and "harvest" not in (s1[0].tags or [])   # not re-harvested
    s2 = [e for e in events.read_all("test") if e.session_id == "s2"]
    assert len(s2) == 1 and "harvest" in s2[0].tags               # the missed one IS harvested


def test_catch_up_scans_transcript_dirs(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)   # MINDHIVE_PROJECT=test -> dir resolves to 'test'
    root = tmp_path / "projects"
    _write_transcript(root / "encoded-folder", "s9", [
        json.dumps({"type": "attachment", "cwd": "C:/whatever", "sessionId": "s9"}),
        _tooluse("Edit", {"file_path": "z.py"}),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    res = harvest.catch_up(known_only=False)       # don't require an existing namespace
    assert res["total_new"] == 1
    assert "test" in res["projects"]


def test_catch_up_skips_unknown_projects_by_default(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)   # empty store -> 'test' is not a known namespace yet
    root = tmp_path / "projects"
    _write_transcript(root / "encoded-folder", "s9", [
        json.dumps({"type": "attachment", "cwd": "C:/whatever", "sessionId": "s9"}),
        _tooluse("Edit", {"file_path": "z.py"}),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    res = harvest.catch_up()                        # known_only=True (default)
    assert res["total_new"] == 0                    # nothing harvested into a surprise namespace
    assert "test" in res["skipped_unknown"]
