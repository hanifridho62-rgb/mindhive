# tests/test_harvest.py
import json
import os
import time
from mindhive import harvest, events, paths


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


def test_partial_capture_backfills_missed_events(tmp_path, monkeypatch):
    """A session the hooks captured only PARTIALLY (e.g. hook crashed after the
    first tool call) must still get its missed events backfilled — not be
    fast-forwarded past forever."""
    _isolate(tmp_path, monkeypatch)
    # Hook captured only the FIRST of two tool calls in session s1.
    events.append_event("file_touched", {"text": "a.py"}, session_id="s1",
                        tags=["auto"], project="test")
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"}),   # already captured
                                   _tooluse("Edit", {"file_path": "b.py"})])  # missed by the hook
    harvest.harvest(tdir, "test", skip_captured=True)
    s1 = [e for e in events.read_all("test") if e.session_id == "s1"]
    paths_touched = sorted(e.payload["text"] for e in s1)
    assert paths_touched == ["a.py", "b.py"]          # a.py once (not doubled), b.py backfilled
    # The backfilled one carries the harvest tag; the original hook one does not.
    b = next(e for e in s1 if e.payload["text"] == "b.py")
    assert "harvest" in b.tags


def test_partial_capture_backfill_is_idempotent(tmp_path, monkeypatch):
    """Re-running harvest after a partial-capture backfill adds nothing more."""
    _isolate(tmp_path, monkeypatch)
    events.append_event("file_touched", {"text": "a.py"}, session_id="s1",
                        tags=["auto"], project="test")
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"}),
                                   _tooluse("Edit", {"file_path": "b.py"})])
    harvest.harvest(tdir, "test", skip_captured=True)
    before = len(events.read_all("test"))
    r2 = harvest.harvest(tdir, "test", skip_captured=True)
    assert r2["new_events"] == 0
    assert len(events.read_all("test")) == before


# --- user_prompt capture (every typed prompt enters memory) ---

def test_every_plain_prompt_becomes_user_prompt(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("how do I run the tests?")])
    harvest.harvest(tdir, "test")
    prompts = [e for e in events.read_all("test") if e.type == "user_prompt"]
    assert len(prompts) == 1
    assert prompts[0].payload["text"] == "how do I run the tests?"
    assert "prompt" in prompts[0].tags


def test_marker_prompt_yields_both_memo_and_prompt(tmp_path, monkeypatch):
    """A prompt with a marker still produces its typed-decision memo AND the
    full prompt event — every prompt enters, markers add the curated memo."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("decision: ship the queue")])
    harvest.harvest(tdir, "test")
    types = sorted(e.type for e in events.read_all("test"))
    assert types == ["decision", "user_prompt"]


def test_slash_command_args_captured_as_prompt(tmp_path, monkeypatch):
    """For a slash command, the user's real input lives in <command-args>."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    msg = ("<command-message>x</command-message>\n"
           "<command-name>/foo</command-name>\n"
           "<command-args>please refactor the parser</command-args>")
    _write_transcript(tdir, "s1", [_usermsg(msg)])
    harvest.harvest(tdir, "test")
    prompts = [e for e in events.read_all("test") if e.type == "user_prompt"]
    assert len(prompts) == 1
    assert prompts[0].payload["text"] == "please refactor the parser"


def test_injected_skill_body_not_captured_as_prompt(tmp_path, monkeypatch):
    """Skill bodies are loaded as user-role messages but are not prompts."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("Base directory for this skill: C:/x\n\nsome skill body text")])
    harvest.harvest(tdir, "test")
    prompts = [e for e in events.read_all("test") if e.type == "user_prompt"]
    assert prompts == []


def test_system_reminder_stripped_from_prompt(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1",
                      [_usermsg("real question here\n<system-reminder>injected noise</system-reminder>")])
    harvest.harvest(tdir, "test")
    prompts = [e for e in events.read_all("test") if e.type == "user_prompt"]
    assert len(prompts) == 1
    assert prompts[0].payload["text"] == "real question here"


def test_tool_result_user_message_not_captured(tmp_path, monkeypatch):
    """A user-role message that is a tool_result (no text) is not a prompt."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    line = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "ok"}]}})
    _write_transcript(tdir, "s1", [line])
    harvest.harvest(tdir, "test")
    prompts = [e for e in events.read_all("test") if e.type == "user_prompt"]
    assert prompts == []


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
    # the marker yields a memo (+ a user_prompt for the raw prompt); the memo
    # must be a decision, NOT reclassified to caveat by the 'avoid' hint word.
    memos = [e for e in events.read_all("test") if e.type in ("decision", "caveat")]
    assert len(memos) == 1
    assert memos[0].type == "decision"


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


# --- cross-process harvest lock (low-latency loop must not race the task) ---

def test_catch_up_skips_when_locked(tmp_path, monkeypatch):
    """If another harvester holds the lock, catch_up no-ops (no double-write)."""
    _isolate(tmp_path, monkeypatch)
    lock = paths.store_home() / "harvest.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("99999", encoding="utf-8")          # fresh lock held by "someone"
    root = tmp_path / "projects"
    _write_transcript(root / "enc", "s1", [
        json.dumps({"type": "attachment", "cwd": "C:/x", "sessionId": "s1"}),
        _tooluse("Edit", {"file_path": "a.py"}),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    res = harvest.catch_up(known_only=False)
    assert res.get("locked") is True
    assert res["total_new"] == 0
    assert [e for e in events.read_all("test") if e.type == "file_touched"] == []


def test_catch_up_steals_stale_lock(tmp_path, monkeypatch):
    """A lock older than the stale window is treated as orphaned and stolen."""
    _isolate(tmp_path, monkeypatch)
    lock = paths.store_home() / "harvest.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("dead", encoding="utf-8")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    root = tmp_path / "projects"
    _write_transcript(root / "enc", "s1", [
        json.dumps({"type": "attachment", "cwd": "C:/x", "sessionId": "s1"}),
        _tooluse("Edit", {"file_path": "a.py"}),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    res = harvest.catch_up(known_only=False)
    assert not res.get("locked")
    assert res["total_new"] == 1           # stale lock stolen, harvest proceeded


def test_catch_up_releases_lock(tmp_path, monkeypatch):
    """The lock is released after a normal run so the next tick can acquire it."""
    _isolate(tmp_path, monkeypatch)
    root = tmp_path / "projects"
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    harvest.catch_up(known_only=False)
    assert not (paths.store_home() / "harvest.lock").exists()


# --- per-bubble 5,000-word cap (one memory entry == one bubble) ---

def test_prompt_capped_at_5000_words(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("word " * 6000)])     # 6000 words
    harvest.harvest(tdir, "test")
    p = [e for e in events.read_all("test") if e.type == "user_prompt"][0]
    toks = p.payload["text"].split()
    assert toks[:3] == ["word", "word", "word"]    # kept the leading words
    assert "[truncated]" in p.payload["text"]       # marked as cut
    assert len(toks) == 5001                         # 5000 words + the marker token


def test_normal_prompt_not_capped(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("a short normal prompt")])
    harvest.harvest(tdir, "test")
    p = [e for e in events.read_all("test") if e.type == "user_prompt"][0]
    assert p.payload["text"] == "a short normal prompt"
    assert "[truncated]" not in p.payload["text"]


# --- assistant_response capture (Claude's output enters the hive too) ---

def _assistant(text=None, tool=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "name": tool[0], "input": tool[1]})
    return json.dumps({"type": "assistant", "message": {"content": content}})


def test_assistant_text_captured_as_response(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_assistant(text="Here is the plan: do X then Y.")])
    harvest.harvest(tdir, "test")
    rs = [e for e in events.read_all("test") if e.type == "assistant_response"]
    assert len(rs) == 1
    assert rs[0].payload["text"] == "Here is the plan: do X then Y."
    assert "response" in rs[0].tags


def test_tool_only_assistant_has_no_response(tmp_path, monkeypatch):
    """An assistant turn that is only a tool call (no prose) yields no response
    event, but the tool action is still captured."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_assistant(tool=("Edit", {"file_path": "a.py"}))])
    harvest.harvest(tdir, "test")
    evs = events.read_all("test")
    assert [e for e in evs if e.type == "assistant_response"] == []
    assert any(e.type == "file_touched" for e in evs)


def test_assistant_response_with_text_and_tool(tmp_path, monkeypatch):
    """A turn with both prose and a tool call captures both."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_assistant(text="Editing the file now.",
                                              tool=("Edit", {"file_path": "a.py"}))])
    harvest.harvest(tdir, "test")
    types = sorted(e.type for e in events.read_all("test"))
    assert types == ["assistant_response", "file_touched"]


def test_assistant_response_capped_at_5000_words(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_assistant(text="word " * 6000)])
    harvest.harvest(tdir, "test")
    r = [e for e in events.read_all("test") if e.type == "assistant_response"][0]
    assert len(r.payload["text"].split()) == 5001
    assert "[truncated]" in r.payload["text"]


# --- project_for_transcript_dir streams (stops at first cwd, no full-file read) ---

def test_project_for_transcript_dir_extracts_first_cwd(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(harvest.paths, "detect_project", lambda p: p.name)
    d = tmp_path / "enc"
    d.mkdir()
    lines = [json.dumps({"cwd": "C:/ProjX"})] + [json.dumps({"i": i}) for i in range(200)]
    (d / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert harvest.project_for_transcript_dir(d) == "ProjX"


def test_project_for_transcript_dir_none_without_cwd(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    d = tmp_path / "enc"
    d.mkdir()
    (d / "s.jsonl").write_text(json.dumps({"type": "x"}) + "\n", encoding="utf-8")
    assert harvest.project_for_transcript_dir(d) is None


def test_project_for_transcript_dir_skips_malformed_cwd_line(tmp_path, monkeypatch):
    """A malformed line containing '"cwd"' must not abandon the whole file — a
    later valid cwd line still resolves the project."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(harvest.paths, "detect_project", lambda p: p.name)
    d = tmp_path / "enc"
    d.mkdir()
    lines = [
        '{"cwd": "C:/Broken"',                       # truncated JSON, has "cwd"
        json.dumps({"cwd": "C:/ProjY"}),             # valid, later
    ]
    (d / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert harvest.project_for_transcript_dir(d) == "ProjY"


# --- capture enrichment parity: line stats + command descriptions (item: Task 1) ---

def test_harvest_items_carry_line_stats_and_desc():
    o = json.loads(_tooluse("Edit", {
        "file_path": "src/a.py", "old_string": "x", "new_string": "x\ny"}))
    items = harvest._items_from_obj(o)
    assert items == [{"kind": "file_touched", "value": "src/a.py",
                      "tool": "Edit", "plus": 2, "minus": 1}]
    o2 = json.loads(_tooluse("Bash", {
        "command": "pytest -q", "description": "Run tests"}))
    items2 = harvest._items_from_obj(o2)
    assert items2 == [{"kind": "command_run", "value": "pytest -q",
                       "desc": "Run tests"}]


def test_harvest_skips_zero_change_edit():
    o = json.loads(_tooluse("Edit", {
        "file_path": "src/a.py", "old_string": "", "new_string": ""}))
    assert harvest._items_from_obj(o) == []


def test_harvest_drops_whitespace_only_description():
    o = json.loads(_tooluse("Bash", {"command": "ls", "description": "   "}))
    assert harvest._items_from_obj(o) == [{"kind": "command_run", "value": "ls"}]


def test_event_for_item_passes_extra_keys_but_text_is_unchanged():
    etype, payload, tags = harvest._event_for_item(
        {"kind": "file_touched", "value": "src/a.py",
         "tool": "Edit", "plus": 2, "minus": 1})
    assert etype == "file_touched"
    assert payload["text"] == "src/a.py"          # dedup key stability
    assert payload["plus"] == 2 and payload["minus"] == 1 and payload["tool"] == "Edit"
    etype2, payload2, _ = harvest._event_for_item(
        {"kind": "command_run", "value": "pytest -q", "desc": "Run tests"})
    assert payload2 == {"text": "pytest -q", "desc": "Run tests"}


# --- Skill invocation capture (SE Task 2: retroactive back-fill) ---

def test_harvest_captures_skill_invocations():
    o = json.loads(_tooluse("Skill", {"skill": "mindhive:circle"}))
    assert harvest._items_from_obj(o) == [
        {"kind": "skill_used",
         "value": {"name": "circle", "plugin": "mindhive", "ok": True,
                   "text": "harvested Skill tool call"}}]


def test_event_for_item_maps_skill_used():
    value = {"name": "circle", "plugin": "mindhive", "ok": True,
             "text": "harvested Skill tool call"}
    etype, payload, tags = harvest._event_for_item(
        {"kind": "skill_used", "value": value})
    assert etype == "skill_used"
    assert payload == value
    assert tags == ["harvest", "auto", "skill"]
