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


def _usermsg(text):
    return json.dumps({"type": "user", "message": {"content": text}})


def test_harvest_stores_full_memo_without_truncation(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    long_memo = "x " * 400 + "tail"               # ~800 chars, well past the old 300 cap
    _write_transcript(tdir, "s1", [_usermsg("memo: " + long_memo)])
    harvest.harvest(tdir, "test")
    memos = [e for e in events.read_all("test") if e.type in events.SEMANTIC_TYPES]
    assert len(memos) == 1
    assert memos[0].payload["text"] == long_memo  # full text, nothing cut off


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


def _tooluse_at(ts, name, inp):
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})


def _usermsg_at(ts, text):
    return json.dumps({"type": "user", "timestamp": ts, "message": {"content": text}})


# --- marker classification (item 3) ---

def test_explicit_decision_not_reclassified_as_caveat(tmp_path, monkeypatch):
    """An explicit `decision:` marker must stay a decision even when the text
    contains a caveat-hint word like 'avoid'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("decision: avoid the global lock, use a queue")])
    harvest.harvest(tdir, "test")
    evs = events.read_all("test")
    assert len(evs) == 1
    assert evs[0].type == "decision"


def test_explicit_caveat_marker_is_caveat(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("caveat: the cache can go stale")])
    harvest.harvest(tdir, "test")
    assert events.read_all("test")[0].type == "caveat"


def test_marker_picked_by_textual_order_not_list_order(tmp_path, monkeypatch):
    """When several markers appear, the FIRST one in the text wins — here
    `decision:` precedes `memo:` even though `memo:` is earlier in _MARKERS."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("decision: ship the queue. memo: trivial note")])
    harvest.harvest(tdir, "test")
    ev = events.read_all("test")[0]
    assert ev.type == "decision"
    assert ev.payload["text"].startswith("ship the queue")


def test_ambiguous_memo_with_hint_is_caveat(tmp_path, monkeypatch):
    """remember:/memo: are ambiguous, so a hint word still infers a caveat."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("memo: this broke prod last night")])
    harvest.harvest(tdir, "test")
    assert events.read_all("test")[0].type == "caveat"


def test_ambiguous_memo_without_hint_is_decision(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("memo: we will adopt the new layout")])
    harvest.harvest(tdir, "test")
    assert events.read_all("test")[0].type == "decision"


# --- timestamp integrity (item 9) ---

def test_harvest_uses_transcript_timestamp(tmp_path, monkeypatch):
    """The event ts must come from the transcript line, normalized to second
    resolution — NOT the wall-clock time the harvest script happens to run."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse_at("2026-06-20T09:15:30.500Z", "Edit", {"file_path": "a.py"})])
    harvest.harvest(tdir, "test", now="2099-01-01T00:00:00Z")
    ev = events.read_all("test")[0]
    assert ev.ts == "2026-06-20T09:15:30Z"


def test_harvest_falls_back_to_now_without_timestamp(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"})])  # no timestamp
    harvest.harvest(tdir, "test", now="2026-03-03T03:03:03Z")
    assert events.read_all("test")[0].ts == "2026-03-03T03:03:03Z"


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
